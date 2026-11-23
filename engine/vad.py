"""Voice activity (Silero VAD v5, MIT, ONNX): where people are talking, 32 ms steps.

Much better than a loudness threshold under music, crowd noise and fans: cuts stay out of the
middle of words and land just after a sentence. Works for Sinhala too (it hears voice, not words).
Missing model / onnxruntime = None, and the old loudness method is used.
"""
from __future__ import annotations

import os
import threading

import numpy as np

from .paths import MODELS_DIR

VAD_PATH = os.path.join(MODELS_DIR, "silero_vad.onnx")
SR = 16000
HOP = 512                       # 32 ms
STEP = HOP / SR
_LOCK = threading.Lock()
_S = {"sess": None, "tried": False}


def _session():
    with _LOCK:
        if not _S["tried"]:
            _S["tried"] = True
            try:
                import onnxruntime as ort  # noqa: PLC0415
                if os.path.exists(VAD_PATH):
                    so = ort.SessionOptions()
                    so.inter_op_num_threads = 1
                    so.intra_op_num_threads = 1
                    so.log_severity_level = 3
                    _S["sess"] = ort.InferenceSession(VAD_PATH, so, providers=["CPUExecutionProvider"])
            except Exception:  # noqa: BLE001
                _S["sess"] = None
    return _S["sess"]


def available() -> bool:
    return _session() is not None


def speech_prob(x16: np.ndarray, progress=None) -> np.ndarray | None:
    """Speech probability per 32 ms frame of 16 kHz mono audio (frame i = [i*STEP, (i+1)*STEP))."""
    s = _session()
    if s is None or len(x16) < HOP:
        return None
    x = np.asarray(x16, np.float32)
    pk = float(np.percentile(np.abs(x), 99.5)) if len(x) else 0.0
    if pk > 1e-4:
        x = x * min(50.0, 0.5 / pk)           # quiet far-away mics: bring to a normal level
    n = len(x) // HOP
    out = np.empty(n, np.float32)
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros((1, 64), np.float32)
    sr = np.array(SR, np.int64)
    names = [i.name for i in s.get_inputs()]
    for i in range(n):
        inp = np.concatenate([ctx, x[None, i * HOP:(i + 1) * HOP]], axis=1)
        feed = {"input": inp, "state": state, "sr": sr} if "state" in names else {names[0]: inp}
        o, state = s.run(None, feed)
        out[i] = float(o.reshape(-1)[0])
        ctx = inp[:, -64:]
        if progress and i % 4000 == 0:
            progress(i / max(1, n))
    return out


def speech_segments(p: np.ndarray, on: float = 0.5, off: float = 0.35, min_speech: float = 0.25,
                    min_silence: float = 0.12) -> list[tuple[float, float]]:
    """[(start, end)] seconds of talking (Silero's own hysteresis rules)."""
    segs, active, a, quiet_from = [], False, 0, None
    for i, v in enumerate(p):
        if not active:
            if v >= on:
                active, a, quiet_from = True, i, None
        else:
            if v < off:
                if quiet_from is None:
                    quiet_from = i
                elif (i - quiet_from) * STEP >= min_silence:
                    if (quiet_from - a) * STEP >= min_speech:
                        segs.append((a * STEP, quiet_from * STEP))
                    active, quiet_from = False, None
            else:
                quiet_from = None
    if active and (len(p) - a) * STEP >= min_speech:
        segs.append((a * STEP, (quiet_from if quiet_from is not None else len(p)) * STEP))
    return segs


def phrase_points(segs, min_pause: float = 0.18, min_gap: float = 1.2):
    """(times, strength) just after a sentence ends - same meaning as audio_events.phrase_points."""
    times, strength = [], []
    last = -1e9
    for k, (a, b) in enumerate(segs):
        nxt = segs[k + 1][0] if k + 1 < len(segs) else b + 2.0
        pause = nxt - b
        if pause < min_pause:
            continue
        t = b + min(0.12, pause / 2)
        s = float(np.clip(0.5 * min(pause / 0.8, 1) + 0.5 * min((b - a) / 2.5, 1), 0, 1))
        if t - last >= min_gap:
            times.append(t)
            strength.append(s)
            last = t
        elif strength and s > strength[-1]:
            times[-1], strength[-1] = t, s
            last = t
    return np.array(times), np.array(strength)
