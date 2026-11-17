"""What the audio says about *where* to cut.

* sequence_audio : mix of the sequence's own audio (all tracks the editor laid out, e.g. DJI mics)
* phrase_points  : ends of spoken / chanted phrases (Jayamangala Gatha, vows, speeches) -> cut there
* music_activity : per-second 0..1 "is music playing" (decides beat cuts vs phrase cuts in Auto)
"""
from __future__ import annotations

import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d

from .media import extract_audio

ENV_RATE = 100  # Hz


def sequence_audio(project: dict, t0: float, t1: float, rate: int, progress=None) -> np.ndarray:
    """Sum of every audio clip in the sequence (or the cameras' own audio when none given)."""
    buf = np.zeros(int((t1 - t0) * rate) + 1, np.float32)
    tracks = project.get("audio_tracks") or []
    if not tracks:
        tracks = [c["clips"] for c in project["cameras"]]
    used = 0
    todo = [cl for trk in tracks for cl in trk if min(t1, cl["tl_end"]) - max(t0, cl["tl_start"]) >= 0.5]
    done = 0
    for trk in tracks:
        for cl in trk:
            a, b = max(t0, cl["tl_start"]), min(t1, cl["tl_end"])
            if b - a < 0.5:
                continue
            done += 1
            if progress:
                progress(min(1.0, (done - 0.5) / max(1, len(todo))))
            x = extract_audio(cl["path"], rate, start=a - cl["offset"], duration=b - a)
            if len(x) == 0:
                continue
            i = int((a - t0) * rate)
            n = min(len(x), len(buf) - i)
            buf[i:i + n] += x[:n]
            used += 1
        if not project.get("audio_tracks") and used:
            break  # camera fallback: one camera's audio is enough
    peak = np.percentile(np.abs(buf), 99.9) if len(buf) else 0
    return buf / peak if peak > 0 else buf


def _envelope_db(x: np.ndarray, rate: int) -> np.ndarray:
    hop = rate // ENV_RATE
    n = len(x) // hop
    if n == 0:
        return np.zeros(0)
    fr = x[:n * hop].reshape(n, hop)
    rms = np.sqrt((fr.astype(np.float64) ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(uniform_filter1d(rms, 5) + 1e-9)


def phrase_points(x: np.ndarray, rate: int, min_pause: float = 0.18, min_gap: float = 1.2):
    """Return (times, strength) of phrase ends: the moment a voice stops for a short pause.

    A cut placed just after a phrase ends feels 'on the words', not in the middle of them.
    """
    env = _envelope_db(x, rate)
    if len(env) < ENV_RATE * 2:
        return np.zeros(0), np.zeros(0)
    # local floor / speech level over ~6 s windows (handles music under speech, crowd noise)
    w = ENV_RATE * 6
    floor = np.array([np.percentile(env[max(0, i - w // 2):i + w // 2], 10) for i in range(0, len(env), ENV_RATE)])
    level = np.array([np.percentile(env[max(0, i - w // 2):i + w // 2], 85) for i in range(0, len(env), ENV_RATE)])
    floor = np.repeat(floor, ENV_RATE)[:len(env)]
    level = np.repeat(level, ENV_RATE)[:len(env)]
    thr = floor + 0.35 * np.maximum(level - floor, 6.0)
    quiet = env < thr
    times, strength = [], []
    i, n = 0, len(env)
    last = -1e9
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            dur = (j - i) / ENV_RATE
            if dur >= min_pause and i > 0:
                t = i / ENV_RATE + min(0.12, dur / 2)   # just after the last syllable
                depth = float(np.clip((level[i] - env[i:j].mean()) / 20.0, 0, 1))
                s = float(np.clip(0.4 * min(dur / 0.8, 1) + 0.6 * depth, 0, 1))
                if t - last >= min_gap:
                    times.append(t)
                    strength.append(s)
                    last = t
                elif strength and s > strength[-1]:
                    times[-1], strength[-1] = t, s
                    last = t
            i = j
        else:
            i += 1
    return np.array(times), np.array(strength)


def music_activity(x: np.ndarray, rate: int, win: float = 6.0, progress=None) -> np.ndarray:
    """Per-second 0..1: strong regular pulse (music / drums) vs speech, chanting or silence."""
    from .beats import onset_strength, SR, FRAME_RATE
    if rate != SR:
        x = signal.resample_poly(x, SR, rate)
    env = onset_strength(x)
    secs = int(len(env) / FRAME_RATE)
    out = np.zeros(max(1, secs))
    w = int(win * FRAME_RATE)
    lag_lo, lag_hi = int(0.3 * FRAME_RATE), int(1.0 * FRAME_RATE)
    for s in range(secs):
        if progress and s % 30 == 0:
            progress(s / max(1, secs))
        c = int(s * FRAME_RATE)
        seg = env[max(0, c - w // 2):c + w // 2]
        if len(seg) < lag_hi * 2:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        out[s] = float(np.clip((ac[lag_lo:lag_hi].max() - 0.15) / 0.35, 0, 1))
    return uniform_filter1d(out, 5)
