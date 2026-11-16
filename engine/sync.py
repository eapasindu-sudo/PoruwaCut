"""Audio-based multicam sync.

Every camera is aligned to a *master* camera (the one carrying the DJI mics).
Result: offset[c] = master_time - camera_time  (seconds). Master offset = 0.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from .media import extract_audio

ENV_RATE = 200  # Hz, onset envelope rate


def _onset_envelope(x: np.ndarray, rate: int) -> np.ndarray:
    """Spectral-flux style envelope: robust between a lav mic and a camera scratch mic."""
    if len(x) == 0:
        return np.zeros(1, np.float32)
    hop = rate // ENV_RATE
    n_fft = hop * 4
    _, _, Z = signal.stft(x, fs=rate, nperseg=n_fft, noverlap=n_fft - hop, boundary=None, padded=False)
    mag = np.log1p(30 * np.abs(Z))
    flux = np.maximum(0, np.diff(mag, axis=1)).sum(axis=0)
    flux = flux - signal.medfilt(flux, 21) if len(flux) > 21 else flux
    flux = np.maximum(flux, 0)
    sd = flux.std() or 1.0
    return ((flux - flux.mean()) / sd).astype(np.float32)


def find_offset(master: np.ndarray, other: np.ndarray, rate: int, max_offset: float | None = None):
    """Return (offset_seconds, confidence)."""
    em, eo = _onset_envelope(master, rate), _onset_envelope(other, rate)
    corr = signal.correlate(em, eo, mode="full", method="fft")
    lags = signal.correlation_lags(len(em), len(eo), mode="full")
    if max_offset is not None:
        keep = np.abs(lags) <= max_offset * ENV_RATE
        corr, lags = corr[keep], lags[keep]
    i = int(np.argmax(corr))
    coarse = lags[i] / ENV_RATE
    # confidence: how far the peak stands above the correlation "noise floor" (robust z-score).
    # (a ratio against the 2nd-best peak is misleading for music, which repeats every beat)
    med = float(np.median(corr))
    mad = float(np.median(np.abs(corr - med))) * 1.4826 + 1e-9
    conf = float((corr[i] - med) / mad) / 10.0  # ~1.0+ is a solid match

    # fine refinement on raw audio around the coarse lag (+-60 ms)
    fine = coarse
    try:
        win = int(min(60, len(other) / rate - 1) * rate)
        o_start = int(max(0, -coarse) * rate) + rate  # a region of `other` that overlaps master
        m_start = int(o_start + coarse * rate)
        pad = int(0.06 * rate)
        seg_o = other[o_start:o_start + win]
        seg_m = master[max(0, m_start - pad):m_start + win + pad]
        if len(seg_o) > rate and len(seg_m) > len(seg_o):
            c2 = signal.correlate(np.abs(seg_m), np.abs(seg_o), mode="valid", method="fft")
            j = int(np.argmax(c2))
            fine = (max(0, m_start - pad) + j - o_start) / rate
            if abs(fine - coarse) > 0.06:
                fine = coarse
    except Exception:
        pass
    return float(fine), conf


def sync_cameras(paths: list[str], master_index: int = 0, rate: int = 8000,
                 max_offset: float | None = None, log=print) -> list[dict]:
    master = extract_audio(paths[master_index], rate)
    results = []
    for i, p in enumerate(paths):
        if i == master_index:
            results.append({"path": p, "offset": 0.0, "confidence": float("inf")})
            continue
        other = extract_audio(p, rate)
        if len(other) == 0:
            log(f"  ! {p}: audio නැත - offset 0 ලෙස තබයි (Premiere එකේ manual sync කරන්න)")
            results.append({"path": p, "offset": 0.0, "confidence": 0.0})
            continue
        off, conf = find_offset(master, other, rate, max_offset)
        flag = "" if conf > 1.0 else "  (විශ්වාසය අඩුයි - පරීක්ෂා කරන්න)"
        log(f"  cam{i + 1}: offset {off:+.3f}s  confidence {conf:.2f}{flag}")
        results.append({"path": p, "offset": off, "confidence": conf})
    return results
