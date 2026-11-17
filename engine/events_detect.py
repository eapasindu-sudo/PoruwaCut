"""Find the parts of a wedding timeline from its audio: speech, chanting, music, drums, applause.

    detect_events(project, t0, t1) -> [{"start", "end", "kind", "event", "confidence"}]

Each second gets a sound class from a few simple measures; the classes are smoothed over ~20 s and
joined into blocks of at least 30 s. A block's kind suggests an AutoWed event:
    speech -> speeches, chant -> poruwa, drums -> groom_welcome, music -> dance_floor.
These are suggestions: the panel shows them as markers the editor can check and change.
"""
from __future__ import annotations

import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d

from .audio_events import music_activity, sequence_audio

RATE = 16000
KINDS = ["quiet", "ambient", "speech", "chant", "music", "drums", "applause"]
EVENT_OF = {"speech": "speeches", "chant": "poruwa", "drums": "groom_welcome", "music": "dance_floor"}
LABEL_SI = {"speech": "කතා / Speeches", "chant": "ගාථා / Poruwa", "drums": "බෙර / Welcome",
            "music": "Music / Dance", "applause": "අත්පොළසන්", "ambient": "වෙනත්", "quiet": "නිහඬ"}


def _features(x: np.ndarray, rate: int):
    """Per-second measures."""
    secs = int(len(x) / rate)
    hop = rate // 100
    frames = x[: len(x) // hop * hop].reshape(-1, hop)
    env = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)            # 100 Hz loudness envelope
    f, t, Z = signal.stft(x, fs=rate, nperseg=1024, noverlap=1024 - rate // 50, boundary=None, padded=False)
    P = np.abs(Z) ** 2 + 1e-12                                   # 50 Hz spectral frames
    band = (f >= 100) & (f <= 4000)
    flat = np.exp(np.log(P[band]).mean(axis=0)) / P[band].mean(axis=0)
    low = (f < 200)
    lowflux = np.maximum(0, np.diff(np.log(P[low].sum(axis=0)), prepend=0))
    out = {k: np.zeros(secs) for k in ("db", "mod", "flat", "low")}
    for s in range(secs):
        e = env[s * 100:(s + 2) * 100]
        if len(e) < 50:
            continue
        out["db"][s] = 20 * np.log10(np.sqrt((e ** 2).mean()) + 1e-9)
        spec = np.abs(np.fft.rfft(e - e.mean())) ** 2
        fr = np.fft.rfftfreq(len(e), 0.01)
        tot = spec[(fr > 0.5) & (fr < 20)].sum() + 1e-12
        out["mod"][s] = spec[(fr >= 2) & (fr <= 8)].sum() / tot      # syllable rate -> speech
        out["flat"][s] = float(np.median(flat[s * 50:(s + 2) * 50])) if s * 50 < len(flat) else 0.0
        lf = lowflux[s * 50:(s + 2) * 50]
        out["low"][s] = float(np.percentile(lf, 95)) if len(lf) else 0.0
    return out


def classify(x: np.ndarray, rate: int) -> np.ndarray:
    ft = _features(x, rate)
    secs = len(ft["db"])
    mus = music_activity(x, rate)
    mus = np.pad(mus, (0, max(0, secs - len(mus))))[:secs]
    loud = ft["db"] - np.percentile(ft["db"], 95)                # relative to the loud parts
    lab = np.zeros(secs, int)
    for s in range(secs):
        m, mod, fl = mus[s], ft["mod"][s], ft["flat"][s]
        if loud[s] < -38:
            k = "quiet"
        elif fl < 0.05 and mod < 0.45:
            k = "chant"            # tonal and sustained (gatha, pirith, singing without a beat)
        elif m > 0.5 and fl > 0.2:
            k = "drums"            # strong regular pulse, noisy hits (magul bera, drummers)
        elif m > 0.5:
            k = "music"
        elif fl > 0.35:
            k = "applause"
        elif mod > 0.55:
            k = "speech"           # syllable-rate loudness changes
        else:
            k = "ambient"
        lab[s] = KINDS.index(k)
    return lab


def _smooth(lab: np.ndarray, win: int = 21) -> np.ndarray:
    onehot = np.eye(len(KINDS))[lab]
    votes = uniform_filter1d(onehot, win, axis=0, mode="nearest")
    return votes.argmax(axis=1), votes.max(axis=1)


def blocks(lab: np.ndarray, conf: np.ndarray, t0: float, min_len: float = 30.0):
    runs = []
    s = 0
    for i in range(1, len(lab) + 1):
        if i == len(lab) or lab[i] != lab[s]:
            runs.append([s, i, int(lab[s])])
            s = i
    # fold short runs into the longer neighbour
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, r in enumerate(runs):
            if r[1] - r[0] < min_len:
                left = runs[i - 1] if i > 0 else None
                right = runs[i + 1] if i + 1 < len(runs) else None
                tgt = max([n for n in (left, right) if n], key=lambda n: n[1] - n[0])
                tgt[0], tgt[1] = min(tgt[0], r[0]), max(tgt[1], r[1])
                runs.pop(i)
                changed = True
                break
        # join neighbours of the same kind
        j = 0
        while j < len(runs) - 1:
            if runs[j][2] == runs[j + 1][2]:
                runs[j][1] = runs[j + 1][1]
                runs.pop(j + 1)
                changed = True
            else:
                j += 1
    out = []
    for a, b, k in runs:
        kind = KINDS[k]
        out.append({"start": round(t0 + a, 2), "end": round(t0 + b, 2), "kind": kind,
                    "label": LABEL_SI[kind], "event": EVENT_OF.get(kind),
                    "confidence": round(float(np.mean(conf[a:b])), 2)})
    return out


def detect_events(project: dict, t0: float, t1: float, progress=None, log=print) -> list[dict]:
    x = sequence_audio(project, t0, t1, RATE, progress=(lambda f: progress(0.7 * f, "audio")) if progress else None)
    if progress:
        progress(0.75, "sound classes")
    lab = classify(x, RATE)
    lab, conf = _smooth(lab)
    ev = blocks(lab, conf, t0)
    if progress:
        progress(1.0, "done")
    log(f"Events {len(ev)}: " + ", ".join(f"{e['kind']} {int(e['end'] - e['start'])}s" for e in ev))
    return ev
