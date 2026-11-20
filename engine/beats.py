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


def split_songs(audio: np.ndarray, sr: int = SR, min_song: float = 15.0) -> list[tuple[float, float]]:
    """One audio track often holds several songs (a dance medley, songs one after another). Song
    boundaries: a quiet gap (>= 0.4 s well below the music level) or a lasting change of tempo."""
    n = len(audio)
    dur = n / sr
    if dur < min_song * 2:
        return [(0.0, dur)]
    hop = int(0.1 * sr)
    rms = np.sqrt(np.convolve(audio.astype(np.float64) ** 2, np.ones(hop) / hop, mode="valid")[::hop] + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    level = np.percentile(db, 70)
    quiet = db < level - 22
    cuts = []
    i = 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * 0.1 >= 0.4:
                cuts.append((i + j) / 2 * 0.1)
            i = j
        else:
            i += 1
    # tempo changes: local tempo every 4 s over 10 s windows
    env = onset_strength(audio)
    win, step = int(10 * FRAME_RATE), int(4 * FRAME_RATE)
    loc = []
    for s0 in range(0, max(1, len(env) - win), step):
        e = env[s0:s0 + win]
        if e.std() < 1e-6:
            loc.append((s0 / FRAME_RATE + 5, np.nan))
            continue
        loc.append((s0 / FRAME_RATE + 5, estimate_tempo(e)))
    for k in range(2, len(loc) - 1):
        a = np.nanmedian([loc[k - 2][1], loc[k - 1][1]])
        b = np.nanmedian([loc[k][1], loc[k + 1][1]])
        if np.isfinite(a) and np.isfinite(b) and abs(np.log2(b / a)) > 0.09 and abs(np.log2(b / a) - 1) > 0.05 \
                and abs(np.log2(b / a) + 1) > 0.05:      # not a half / double tempo flip
            t0c = (loc[k - 1][0] + loc[k][0]) / 2
            i0, i1 = int(max(0, t0c - 16) * FRAME_RATE), int((t0c - 6) * FRAME_RATE)
            j0, j1 = int((t0c + 6) * FRAME_RATE), int(min(len(env) / FRAME_RATE, t0c + 16) * FRAME_RATE)
            ta = estimate_tempo(env[i0:i1]) if i1 - i0 > FRAME_RATE * 4 else a
            tb = estimate_tempo(env[j0:j1]) if j1 - j0 > FRAME_RATE * 4 else b
            t = _refine_change(t0c, ta, tb, env)
            if not any(abs(t - c) < 10 for c in cuts):
                cuts.append(t)
    edges = [0.0] + sorted(c for c in cuts if 1.0 < c < dur - 1.0) + [dur]
    songs = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    merged = []      # short bits join their neighbour
    for a, b in songs:
        if merged and (b - a < min_song or merged[-1][1] - merged[-1][0] < min_song):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _period_fit(e: np.ndarray, bpm: float) -> float:
    """How well an onset envelope repeats at this tempo (normalised autocorrelation at the period)."""
    lag = int(round(60.0 / bpm * FRAME_RATE))
    if len(e) <= lag * 2 or e.std() < 1e-9:
        return 0.0
    e = e - e.mean()
    return float(np.dot(e[:-lag], e[lag:]) / (np.dot(e, e) + 1e-9))


def _refine_change(t: float, bpm_a: float, bpm_b: float, env: np.ndarray, span: float = 7.0) -> float:
    """Move a tempo-change boundary to where tempo A fits before it and tempo B after it."""
    best, best_t = -1e9, t
    w = int(6 * FRAME_RATE)
    for c in np.arange(t - span, t + span + 0.01, 0.25):
        i = int(c * FRAME_RATE)
        if i - w < 0 or i + w > len(env):
            continue
        sc = _period_fit(env[i - w:i], bpm_a) + _period_fit(env[i:i + w], bpm_b)
        if sc > best:
            best, best_t = sc, float(c)
    return best_t


def detect_beats_songs(audio: np.ndarray, sr: int = SR, progress=None):
    """Beats song by song: each song gets its own tempo. Returns beats, strengths, songs
    [{start, end, bpm}] (seconds of the audio)."""
    songs = split_songs(audio, sr)
    all_b, all_s, info = [], [], []
    for k, (a, b) in enumerate(songs):
        if progress:
            progress(k / max(1, len(songs)))
        seg = audio[int(a * sr):int(b * sr)]
        if len(seg) < sr * 4:
            continue
        bb, tempos, st = detect_beats(seg, sr, segment_sec=max(60.0, b - a + 1))
        if len(bb):
            all_b.append(bb + a)
            all_s.append(st)
        bpm = float(60.0 / np.median(np.diff(bb))) if len(bb) > 2 else 0.0
        info.append({"start": a, "end": b, "bpm": round(bpm, 1)})
    if not all_b:
        return np.zeros(0), np.zeros(0), info
    return np.concatenate(all_b), np.concatenate(all_s), info


def downbeats(beats: np.ndarray, strength: np.ndarray, per_bar: int = 4) -> np.ndarray:
    """Rough bar starts: choose the beat phase with the strongest average onset."""
    if len(beats) < per_bar * 2:
        return beats
    best = max(range(per_bar), key=lambda p: strength[p::per_bar].mean())
    return beats[best::per_bar]


def detect_rolls(audio: np.ndarray, beats: np.ndarray, sr: int = SR, min_len: float = 0.7):
    """Drum rolls / fills: stretches where hits come much faster than the beat (>= ~2.2x the beat
    rate and >= 5 per second). Returns [(start, end, hits[])] in seconds of the audio, and the
    "landing" after each roll (the first beat at or after its end: the big hit to cut on)."""
    env = onset_strength(audio)
    if len(env) < FRAME_RATE * 2:
        return []
    pk, _ = signal.find_peaks(env, height=1.2, distance=max(1, int(0.07 * FRAME_RATE)))
    hits = pk / FRAME_RATE + LATENCY
    if len(hits) < 6:
        return []
    period = float(np.median(np.diff(beats))) if len(beats) > 2 else 0.5
    fast = min(1.0 / 5.0, period / 2.2)          # gap between hits inside a roll
    rolls = []
    i = 0
    while i < len(hits) - 1:
        j = i
        while j + 1 < len(hits) and hits[j + 1] - hits[j] <= fast * 1.15:
            j += 1
        if j - i >= 4 and hits[j] - hits[i] >= min_len:
            a, b = float(hits[i]), float(hits[j])
            nxt = beats[beats >= b - 0.05] if len(beats) else np.zeros(0)
            land = float(nxt[0]) if len(nxt) and nxt[0] - b < period * 1.5 else b + fast
            rolls.append({"start": a, "end": b, "hits": [float(h) for h in hits[i:j + 1]], "land": land})
        i = j + 1
    return rolls
