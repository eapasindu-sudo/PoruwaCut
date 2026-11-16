"""Beat tracking for dance-floor edits (tempo estimate + Ellis dynamic-programming tracker)."""
from __future__ import annotations

import numpy as np
from scipy import signal

SR = 11025
HOP = 256
FRAME_RATE = SR / HOP  # ~43 Hz
LATENCY = 1024 / SR      # seconds


def onset_strength(x: np.ndarray) -> np.ndarray:
    _, _, Z = signal.stft(x, fs=SR, nperseg=1024, noverlap=1024 - HOP, boundary=None, padded=False)
    S = np.log1p(100 * np.abs(Z))
    # emphasise kick / bass + general flux
    flux = np.maximum(0, np.diff(S, axis=1))
    env = flux.sum(axis=0) + 2.0 * flux[:24].sum(axis=0)
    env = env - signal.medfilt(env, 31)
    env = np.maximum(env, 0)
    return env / (env.std() + 1e-9)


def estimate_tempo(env: np.ndarray, lo=70, hi=180) -> float:
    ac = signal.correlate(env, env, mode="full", method="fft")[len(env) - 1:]
    lags = np.arange(len(ac)) / FRAME_RATE
    bpm = np.zeros_like(lags)
    bpm[1:] = 60.0 / lags[1:]
    ok = (bpm >= lo) & (bpm <= hi)
    prior = np.exp(-0.5 * (np.log2(np.maximum(bpm, 1e-6) / 118.0) / 0.9) ** 2)
    score = np.where(ok, ac * prior, -np.inf)
    return float(bpm[int(np.argmax(score))])


def track_beats(env: np.ndarray, bpm: float, tightness: float = 100.0) -> np.ndarray:
    period = 60.0 / bpm * FRAME_RATE
    n = len(env)
    score = env.astype(np.float64).copy()
    back = np.full(n, -1, np.int64)
    lo, hi = int(round(period * 0.5)), int(round(period * 2))
    offsets = np.arange(-hi, -lo + 1)
    penalty = -tightness * np.log(-offsets / period) ** 2
    for i in range(hi, n):
        cand = score[i + offsets] + penalty
        j = int(np.argmax(cand))
        if cand[j] > 0:
            score[i] = env[i] + cand[j]
            back[i] = i + offsets[j]
    # pick last beat near the end with high score
    tail = max(1, int(period))
    i = n - tail + int(np.argmax(score[-tail:]))
    beats = []
    while i >= 0:
        beats.append(i)
        i = back[i]
    return np.array(beats[::-1]) / FRAME_RATE


def detect_beats(audio: np.ndarray, sr: int = SR, segment_sec: float = 60.0, progress=None):
    """Beats over a long recording; tempo is re-estimated per minute so song changes are handled."""
    if sr != SR:
        raise ValueError("audio must be resampled to 11025 Hz")
    env = onset_strength(audio)
    seg = int(segment_sec * FRAME_RATE)
    beats, tempos = [], []
    for s in range(0, len(env), seg):
        if progress:
            progress(s / max(1, len(env)))
        e = env[s:s + seg + int(2 * FRAME_RATE)]  # small overlap
        if len(e) < FRAME_RATE * 4:
            continue
        bpm = estimate_tempo(e)
        b = track_beats(e, bpm) + s / FRAME_RATE
        b = b[b < (s + seg) / FRAME_RATE]
        beats.append(b)
        tempos.append((s / FRAME_RATE, bpm))
    if not beats:
        return np.zeros(0), tempos, np.zeros(0)
    # STFT analysis latency: an onset shows up one window (1024 samples) before it is heard
    beats = np.unique(np.round(np.concatenate(beats) + LATENCY, 3))
    # remove beats closer than 40% of the local period
    keep = [beats[0]]
    for b in beats[1:]:
        if b - keep[-1] > 0.25:
            keep.append(b)
    beats = np.array(keep)
    strength = np.interp((beats - LATENCY) * FRAME_RATE, np.arange(len(env)), env)
    return beats, tempos, strength


def downbeats(beats: np.ndarray, strength: np.ndarray, per_bar: int = 4) -> np.ndarray:
    """Rough bar starts: choose the beat phase with the strongest average onset."""
    if len(beats) < per_bar * 2:
        return beats
    best = max(range(per_bar), key=lambda p: strength[p::per_bar].mean())
    return beats[best::per_bar]
