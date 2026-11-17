"""Multi-device audio sync (AutoWed "Sync" tab).

Any mix of files: 2-6 cameras (several files each), Zoom H8 / other recorder takes (one WAV per
track per take, sample-aligned with each other), DJI Mic recordings, drone clips. Files are lined
up by their audio alone; no timecode needed.

    place_files(files, ...) -> {"placements": [...], "unsynced": [...], "report": [...]}

How:
 1. every file -> a 100 Hz onset envelope (spectral flux), cached on disk, several files in parallel
 2. files of one recorder take (ZOOM0001_Tr1.WAV, ZOOM0001_LR.WAV ...) form one unit with one offset
 3. the longest unit is the anchor; the others are matched, longest first, against a composite of
    everything placed so far (so a camera that only overlaps another camera still finds its place)
 4. each match is refined to the sample with GCC-PHAT against the placed file it overlaps most;
    long overlaps are measured at both ends to report clock drift
 5. files are put on lanes per device (folder + file-name pattern + picture size), so each camera
    and each recorder track gets its own timeline track
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from scipy import signal

from .media import FFMPEG, NOWIN, extract_audio, probe

RATE = 4000          # Hz, audio used for the envelope
ENV_RATE = 100       # Hz, envelope rate (10 ms)
HOP = RATE // ENV_RATE
NFFT = HOP * 4
ACCEPT = 0.8         # confidence needed to accept a match
FINE_RATE = 16000

_TAKE_RE = re.compile(r"^(?P<take>.+?)[_-](?P<trk>Tr\d+|TrLR|TrMS|LR|MS|Tr[A-Za-z]+)(?P<part>[_-]\d+)?$", re.I)


# ---------- envelopes ----------
def _env_chunk(x: np.ndarray) -> np.ndarray:
    if len(x) < NFFT:
        return np.zeros(0, np.float32)
    _, _, Z = signal.stft(x, fs=RATE, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None, padded=False)
    mag = np.log1p(30 * np.abs(Z))
    flux = np.maximum(0, np.diff(mag, axis=1)).sum(axis=0)
    return np.concatenate([[0.0], flux]).astype(np.float32)


def _cache_key(path: str) -> str:
    st = os.stat(path)
    return hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|v1".encode()).hexdigest()[:20]


def envelope(path: str, cache_dir: str | None = None) -> np.ndarray:
    """Onset envelope of a whole file (any length), z-scored. Streams the audio in 60 s pieces."""
    cp = os.path.join(cache_dir, _cache_key(path) + ".npy") if cache_dir else None
    if cp and os.path.exists(cp):
        try:
            return np.load(cp)
        except Exception:  # noqa: BLE001
            pass
    import subprocess
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(RATE),
           "-f", "f32le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **NOWIN)
    parts, carry = [], np.zeros(0, np.float32)
    block = RATE * 60 * 4
    while True:
        raw = proc.stdout.read(block)
        if not raw:
            break
        x = np.concatenate([carry, np.frombuffer(raw[: len(raw) // 4 * 4], np.float32)])
        n = (len(x) - NFFT) // HOP * HOP          # whole hops; keep the tail for the next piece
        if n <= 0:
            carry = x
            continue
        parts.append(_env_chunk(x[: n + NFFT])[: n // HOP])
        carry = x[n:]
    proc.wait()
    if len(carry) >= NFFT:
        parts.append(_env_chunk(carry))
    e = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    if len(e) > 41:
        # remove slow level changes (music beds, crowd noise) so only onsets are left
        k = 41
        base = np.convolve(e, np.ones(k, np.float32) / k, mode="same")
        e = np.maximum(e - base, 0)
    sd = float(e.std()) or 1.0
    e = np.clip((e - float(e.mean())) / sd, -3, 8).astype(np.float32)
    if cp:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            np.save(cp, e)
        except OSError:
            pass
    return e


# ---------- grouping ----------
def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def take_of(path: str) -> str | None:
    """'.../ZOOM0001_Tr1.WAV' -> '.../ZOOM0001' (+ split-part), None for files that are not a track of a take."""
    m = _TAKE_RE.match(_stem(path))
    if not m or not path.lower().endswith((".wav", ".bwf", ".w64", ".rf64")):
        return None
    return os.path.join(os.path.dirname(path), m.group("take") + (m.group("part") or "")).lower()


def device_of(f: dict) -> str:
    """Files of the same device share folder, file-name pattern and picture size."""
    p = f["path"]
    stem = _stem(p)
    m = _TAKE_RE.match(stem) if not f.get("has_video") else None
    if m:  # a recorder track: one device lane per track name (Tr1, Tr2, LR ...)
        return f"rec|{os.path.dirname(p).lower()}|{m.group('trk').lower()}"
    pattern = re.sub(r"\d+", "#", stem).lower()
    size = f"{f.get('width', 0)}x{f.get('height', 0)}" if f.get("has_video") else "audio"
    return f"{'cam' if f.get('has_video') else 'aud'}|{os.path.dirname(p).lower()}|{pattern}|{size}"


# ---------- matching ----------
def _corr_place(comp: np.ndarray, e: np.ndarray):
    """Best lag (envelope samples) of e inside comp, and a robust confidence."""
    c = signal.correlate(comp, e, mode="full", method="fft")
    lags = signal.correlation_lags(len(comp), len(e), mode="full")
    # need a real overlap (at least 20 s or half of the shorter one)
    ov = np.minimum(len(comp), lags + len(e)) - np.maximum(0, lags)
    need = min(20 * ENV_RATE, len(e) // 2, len(comp) // 2)
    c = np.where(ov >= max(need, 1), c / np.sqrt(np.maximum(ov, 1)), -np.inf)
    i = int(np.argmax(c))
    good = np.isfinite(c)
    med = float(np.median(c[good]))
    mad = float(np.median(np.abs(c[good] - med))) * 1.4826 + 1e-9
    return int(lags[i]), float((c[i] - med) / mad) / 10.0


def gcc_phat(a: np.ndarray, b: np.ndarray, rate: int, max_shift: float) -> tuple[float, float]:
    """Delay of b relative to a (seconds, + means b is later in a), and peak sharpness."""
    n = len(a) + len(b)
    A, B = np.fft.rfft(a, n), np.fft.rfft(b, n)
    R = A * np.conj(B)
    R /= np.abs(R) + 1e-12
    cc = np.fft.irfft(R, n)
    m = int(max_shift * rate)
    cc = np.concatenate([cc[-m:], cc[: m + 1]])
    i = int(np.argmax(np.abs(cc)))
    sharp = float(np.abs(cc[i]) / (np.abs(cc).mean() + 1e-12))
    return (i - m) / rate, sharp


def _refine(pa: str, a_start: float, pb: str, b_start: float, at: float, dur: float = 20.0):
    """Fine-tune b's start using the overlap at timeline time `at`. Returns corrected b_start or None."""
    try:
        xa = extract_audio(pa, FINE_RATE, start=max(0.0, at - a_start), duration=dur)
        xb = extract_audio(pb, FINE_RATE, start=max(0.0, at - b_start), duration=dur)
        if len(xa) < FINE_RATE * 5 or len(xb) < FINE_RATE * 5:
            return None
        n = min(len(xa), len(xb))
        d, sharp = gcc_phat(xa[:n], xb[:n], FINE_RATE, 0.06)
        if sharp < 6:
            return None
        return b_start + d
    except Exception:  # noqa: BLE001
        return None


def place_files(files: list[dict], cache_dir: str | None = None, workers: int = 4,
                log=print, progress=None) -> dict:
    """files: [{"path", "has_video", "width", "height", "name"?}] -> placements on lanes."""
    files = [dict(f) for f in files if f.get("path") and os.path.exists(f["path"])]
    if len(files) < 2:
        raise ValueError("sync කරන්න files 2ක් වත් ඕන")
    for f in files:
        if "has_video" not in f:
            try:
                info = probe(f["path"])
                f["has_video"], f["width"], f["height"] = info.width > 0, info.width, info.height
            except Exception:  # noqa: BLE001
                f["has_video"] = False

    # 1) envelopes
    envs: dict[str, np.ndarray] = {}
    done = [0]

    def one(f):
        e = envelope(f["path"], cache_dir)
        done[0] += 1
        if progress:
            progress(0.75 * done[0] / len(files), f"audio කියවනවා {done[0]}/{len(files)}")
        return f["path"], e

    with ThreadPoolExecutor(max(1, workers)) as ex:
        for p, e in ex.map(one, files):
            envs[p] = e
    silent = [f for f in files if len(envs[f["path"]]) < ENV_RATE * 3 or float(np.abs(envs[f["path"]]).sum()) == 0]
    files = [f for f in files if f not in silent]

    # 2) units: a recorder take's tracks move together
    units: dict[str, list[dict]] = {}
    for f in files:
        key = take_of(f["path"]) if not f.get("has_video") else None
        units.setdefault(key or f["path"], []).append(f)
    def unit_env(u):
        es = [envs[f["path"]] for f in u]
        n = max(len(e) for e in es)
        out = np.zeros(n, np.float32)
        for e in es:
            out[: len(e)] += e
        return out / len(es)
    order = sorted(units, key=lambda k: -max(len(envs[f["path"]]) for f in units[k]))

    # 3) composite matching, longest first
    anchor = order[0]
    starts: dict[str, float] = {anchor: 0.0}       # unit -> start on timeline (s), anchor at 0
    conf: dict[str, float] = {anchor: 99.0}
    origin = 0                                      # composite index of timeline 0
    e0 = unit_env(units[anchor])
    S, C = e0.astype(np.float64).copy(), np.ones(len(e0))
    pending = order[1:]
    for _ in range(3):
        left = []
        for k in pending:
            e = unit_env(units[k])
            comp = (S / np.maximum(C, 1)).astype(np.float32)
            lag, cf = _corr_place(comp, e)
            if cf < ACCEPT:
                left.append(k)
                continue
            starts[k] = (lag - origin) / ENV_RATE
            conf[k] = cf
            # grow the composite
            lo, hi = lag, lag + len(e)
            if lo < 0:
                S = np.concatenate([np.zeros(-lo), S]); C = np.concatenate([np.zeros(-lo), C])
                origin += -lo; hi += -lo; lo = 0
            if hi > len(S):
                S = np.concatenate([S, np.zeros(hi - len(S))]); C = np.concatenate([C, np.zeros(hi - len(C))])
            S[lo:hi] += e; C[lo:hi] += 1
        if len(left) == len(pending):
            break
        pending = left
    unsynced = [f for k in pending for f in units[k]] + silent
    if progress:
        progress(0.8, "හරියටම ගලපනවා")

    # 4) sample-accurate refine against the placed unit with the largest overlap (+ drift check)
    report = []
    span = {k: (starts[k], starts[k] + max(len(envs[f["path"]]) for f in units[k]) / ENV_RATE) for k in starts}
    ref_file = {k: max(units[k], key=lambda f: len(envs[f["path"]]))["path"] for k in starts}
    fixed = {anchor}
    for k in sorted(starts, key=lambda k: -(span[k][1] - span[k][0])):
        if k == anchor:
            continue
        best, bov = None, 0.0
        for o in fixed:
            ov = min(span[k][1], span[o][1]) - max(span[k][0], span[o][0])
            if ov > bov:
                best, bov = o, ov
        if best is not None and bov > 8:
            a0, a1 = max(span[k][0], span[best][0]), min(span[k][1], span[best][1])
            at = a0 + min(30.0, (a1 - a0) / 3)
            nb = _refine(ref_file[best], starts[best], ref_file[k], starts[k], at)
            if nb is not None and abs(nb - starts[k]) < 0.06:
                d = nb - starts[k]
                starts[k] = nb
                span[k] = (span[k][0] + d, span[k][1] + d)
            if a1 - a0 > 20 * 60:  # clock drift over a long overlap
                late = _refine(ref_file[best], starts[best], ref_file[k], starts[k], a1 - 60)
                if late is not None:
                    drift = late - starts[k]
                    if abs(drift) > 0.02:
                        report.append({"unit": os.path.basename(ref_file[k]), "drift_ms": round(drift * 1000, 1),
                                       "over_min": round((a1 - a0) / 60, 1)})
        fixed.add(k)

    # 5) lanes per device
    t0 = min(starts.values())
    placements = []
    for k, st in starts.items():
        for f in units[k]:
            dur = len(envs[f["path"]]) / ENV_RATE
            try:
                dur = probe(f["path"]).duration or dur
            except Exception:  # noqa: BLE001
                pass
            placements.append({"path": f["path"], "name": f.get("name") or os.path.basename(f["path"]),
                               "has_video": bool(f.get("has_video")), "start": round(st - t0, 4),
                               "end": round(st - t0 + dur, 4), "confidence": round(conf[k], 2),
                               "device": device_of(f), "take": take_of(f["path"])})
    # files that found no match: put after the end on their own camera/recorder lane to place by hand
    tail = max((p["end"] for p in placements), default=0.0) + 10.0
    for f in unsynced:
        try:
            dur = probe(f["path"]).duration
        except Exception:  # noqa: BLE001
            dur = len(envs.get(f["path"], [])) / ENV_RATE or 10.0
        placements.append({"path": f["path"], "name": f.get("name") or os.path.basename(f["path"]),
                           "has_video": bool(f.get("has_video")), "start": round(tail, 4),
                           "end": round(tail + dur, 4), "confidence": 0.0, "unsynced": True,
                           "device": device_of(f), "take": take_of(f["path"])})
        tail += dur + 5.0
    _assign_lanes(placements)
    if progress:
        progress(1.0, "ඉවරයි")
    return {"placements": placements,
            "unsynced": [{"path": f["path"], "name": f.get("name") or os.path.basename(f["path"])} for f in unsynced],
            "drift": report, "anchor": os.path.basename(ref_file[anchor])}


def _assign_lanes(pl: list[dict]):
    """Each device gets its own lane(s); overlapping files of one device spill onto an extra lane.
    Video lanes -> V tracks. Audio lanes: recorder/mic tracks first, then each camera's own audio."""
    def lanes(items):
        out = []  # list of end times per lane
        for p in sorted(items, key=lambda p: p["start"]):
            for i, end in enumerate(out):
                if p["start"] >= end - 0.01:
                    out[i] = p["end"]; p["_lane"] = i; break
            else:
                out.append(p["end"]); p["_lane"] = len(out) - 1
        return len(out)

    devs: dict[str, list[dict]] = {}
    for p in pl:
        devs.setdefault(p["device"], []).append(p)
    # cameras ordered by when they first appear, then by name; recorders by track name
    cams = sorted([d for d in devs if d.startswith("cam|")], key=lambda d: (min(p["start"] for p in devs[d]), d))
    auds = sorted([d for d in devs if not d.startswith("cam|")])
    v = 0
    for d in cams:
        n = lanes(devs[d])
        for p in devs[d]:
            p["video_track"] = v + p.pop("_lane")
        v += n
    a = 0
    for d in auds:
        n = lanes(devs[d])
        for p in devs[d]:
            p["audio_track"] = a + p.pop("_lane")
        a += n
    for d in cams:      # camera audio after the recorder tracks, one lane per camera lane
        for p in devs[d]:
            p["audio_track"] = a + p["video_track"]
    return pl


def save_report(path: str, result: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
