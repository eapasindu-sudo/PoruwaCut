"""Audio checks for one edit range: silences, loudness, best mic, mic drop-outs, clipping, smart trim.

All work on 16 kHz mono. `mix` is the sum of every audio track (normalised); tracks are the raw
per-track sums (not normalised) so levels and clipping can be judged.
"""
from __future__ import annotations

import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d

from .media import extract_audio

RATE = 16000
TARGET_LUFS = {"speeches": -16.0, "church_mass": -16.0, "poruwa": -16.0, "pahana": -16.0}
DEFAULT_TARGET = -14.0


def track_audio(track: list[dict], t0: float, t1: float, rate: int = RATE) -> np.ndarray:
    buf = np.zeros(int((t1 - t0) * rate) + 1, np.float32)
    for cl in track:
        a, b = max(t0, cl["tl_start"]), min(t1, cl["tl_end"])
        if b - a < 0.3:
            continue
        x = extract_audio(cl["path"], rate, start=a - cl["offset"], duration=b - a)
        i = int((a - t0) * rate)
        n = min(len(x), len(buf) - i)
        if n > 0:
            buf[i:i + n] += x[:n]
    return buf


def _db(x, eps=1e-9):
    return 20 * np.log10(np.abs(x) + eps)


def frame_db(x: np.ndarray, rate: int = RATE, hop: float = 0.05) -> np.ndarray:
    n = int(rate * hop)
    m = len(x) // n
    if m == 0:
        return np.zeros(0)
    f = x[: m * n].reshape(m, n)
    return 10 * np.log10((f ** 2).mean(axis=1) + 1e-12)


def lufs(x: np.ndarray, rate: int = RATE) -> float:
    """Integrated loudness, BS.1770 style (K-weighting, 400 ms blocks, absolute + relative gate)."""
    if len(x) < rate:
        return -70.0
    b1, a1 = signal.butter(2, 1500 / (rate / 2), "high")        # ~ high-shelf boost stand-in
    hp = signal.lfilter(b1, a1, x) * 1.58 + x
    b2, a2 = signal.butter(2, 38 / (rate / 2), "high")
    k = signal.lfilter(b2, a2, hp)
    blk, step = int(0.4 * rate), int(0.1 * rate)
    ms = np.array([np.mean(k[i:i + blk] ** 2) for i in range(0, len(k) - blk, step)])
    if not len(ms):
        return -70.0
    l = -0.691 + 10 * np.log10(ms + 1e-12)
    g = ms[l > -70]
    if not len(g):
        return -70.0
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g2 = g[(-0.691 + 10 * np.log10(g + 1e-12)) > rel]
    return float(-0.691 + 10 * np.log10((g2 if len(g2) else g).mean()))


def silences(mix: np.ndarray, t0: float, min_len: float = 0.9, pad: float = 0.15) -> list[dict]:
    """Pauses in speech worth cutting out (relative to this range's own speech level)."""
    d = np.maximum(frame_db(mix), -80.0)
    if not len(d):
        return []
    floor, speech = np.percentile(d, 10), np.percentile(d, 90)
    thr = max(floor + 8, speech - 28)
    quiet = uniform_filter1d((d < thr).astype(float), 3) > 0.66
    out, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            a, b = i * 0.05 + pad, j * 0.05 - pad
            if b - a >= min_len - 2 * pad and i > 0 and j < len(quiet):
                out.append({"start": round(t0 + a, 3), "end": round(t0 + b, 3)})
            i = j
        else:
            i += 1
    return out


def smart_trim(mix: np.ndarray, t0: float, t1: float, dead: list[dict]) -> list[float] | None:
    """Empty head / tail: quiet audio (or every camera weak) before the action starts / after it ends."""
    d = np.maximum(frame_db(mix), -80.0)      # digital silence would drag the floor down
    if len(d) < 200:
        return None
    floor, loud = np.percentile(d, 10), np.percentile(d, 95)
    active = uniform_filter1d((d > floor + 0.35 * (loud - floor)).astype(float), 40) > 0.5   # 2 s
    idx = np.nonzero(active)[0]
    if not len(idx):
        return None
    a = t0 + max(0.0, idx[0] * 0.05 - 1.0)
    b = t0 + min(len(d) * 0.05, idx[-1] * 0.05 + 1.5)
    for dd in dead:        # a weak-picture stretch touching the start / end extends the trim
        if dd["start"] <= a + 0.5 and dd["end"] > a:
            a = dd["end"]
        if dd["end"] >= b - 0.5 and dd["start"] < b:
            b = dd["start"]
    if a - t0 < 3:
        a = t0
    if t1 - b < 3:
        b = t1
    if a == t0 and b == t1:
        return None
    return [round(a, 2), round(b, 2)]


def mic_analysis(tracks: list[np.ndarray], t0: float, names: list[str]) -> dict:
    """Best (clearest) track per stretch, drop-outs of the main mic, clipping."""
    win = int(2.0 * RATE)
    res = {"mic_choice": [], "mic_gaps": [], "clipping": []}
    if not tracks:
        return res
    n = min(len(t) for t in tracks)
    nw = n // win
    if nw == 0:
        return res
    b, a = signal.butter(4, [300 / (RATE / 2), 3400 / (RATE / 2)], "band")
    snr = np.full((len(tracks), nw), -99.0)
    lvl = np.full((len(tracks), nw), -99.0)
    for k, x in enumerate(tracks):
        sp = signal.lfilter(b, a, x[:nw * win]).reshape(nw, win)
        r = 10 * np.log10((sp ** 2).mean(axis=1) + 1e-12)
        floor = np.percentile(r, 10)
        lvl[k] = r
        snr[k] = np.where(r > floor + 6, r - floor, -99.0)
        # clipping: flat tops near full scale
        y = x[:nw * win]
        hot = np.abs(y) > 0.97
        if hot.any():
            runs = np.convolve(hot.astype(np.int8), np.ones(3, np.int8), "same") >= 3
            per = runs.reshape(nw, win).sum(axis=1)
            for w in np.nonzero(per > 0)[0]:
                res["clipping"].append({"track": k, "name": names[k], "start": round(t0 + w * 2.0, 2),
                                        "end": round(t0 + w * 2.0 + 2.0, 2), "samples": int(per[w])})
    best = snr.argmax(axis=0)
    best[snr.max(axis=0) <= -98] = -1
    # stretches of >= 6 s (short flips are noise)
    segs = []
    for w in range(nw):
        if segs and segs[-1]["track"] == int(best[w]):
            segs[-1]["end"] = t0 + (w + 1) * 2.0
        else:
            segs.append({"track": int(best[w]), "start": t0 + w * 2.0, "end": t0 + (w + 1) * 2.0})
    merged = []
    for s_ in segs:
        if merged and (s_["end"] - s_["start"] < 6 or s_["track"] == merged[-1]["track"]):
            merged[-1]["end"] = s_["end"]
        else:
            merged.append(s_)
    for s_ in merged:
        s_["name"] = names[s_["track"]] if s_["track"] >= 0 else ""
        s_["start"], s_["end"] = round(s_["start"], 2), round(s_["end"], 2)
    res["mic_choice"] = merged
    # drop-outs: the main mic goes (almost) silent for a moment while others still hear the scene
    main = int(np.bincount(best[best >= 0]).argmax()) if (best >= 0).any() else -1
    if main >= 0 and len(tracks) > 1:
        x = tracks[main][:n]
        d = frame_db(x)
        others = np.max([frame_db(t[:n]) for i, t in enumerate(tracks) if i != main], axis=0)
        m = min(len(d), len(others))
        drop = (d[:m] < -60) & (others[:m] > -40)
        i = 0
        while i < m:
            if drop[i]:
                j = i
                while j < m and drop[j]:
                    j += 1
                if (j - i) * 0.05 >= 0.3:
                    res["mic_gaps"].append({"track": main, "name": names[main], "start": round(t0 + i * 0.05, 2),
                                            "end": round(t0 + j * 0.05, 2)})
                i = j
            else:
                i += 1
    res["main_mic"] = main
    return res


def run_qc(project: dict, t0: float, t1: float, event: str, mix: np.ndarray, dead: list[dict],
           want_silence: bool, want_trim: bool) -> dict:
    out: dict = {}
    target = TARGET_LUFS.get(event, DEFAULT_TARGET)
    tracks = project.get("audio_tracks") or []
    raw = [track_audio(t, t0, t1) if t else np.zeros(int((t1 - t0) * RATE) + 1, np.float32) for t in tracks]
    names = [f"A{i + 1}" for i in range(len(tracks))]
    total = np.sum([r[: min(len(x) for x in raw)] for r in raw], axis=0) if raw else mix
    L = lufs(total)
    gain = float(np.clip(target - L, -12, 12)) if L > -60 else 0.0
    out["loudness"] = {"lufs": round(L, 1), "target": target, "gain_db": round(gain, 1)}
    out.update(mic_analysis(raw, t0, names) if raw else {"mic_choice": [], "mic_gaps": [], "clipping": []})
    if want_silence:
        out["silences"] = silences(mix, t0)
    if want_trim:
        out["trim"] = smart_trim(mix, t0, t1, dead)
    return out
