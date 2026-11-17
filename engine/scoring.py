"""Turn raw per-frame metrics into one 'how good is this shot right now' score per camera.

Modes
-----
ceremony : Poruwa / speeches. Faces + steadiness matter most, calm cutting (2.5-10 s shots).
dance    : Dance floor. All cams handheld, so shake vs. *intentional rhythmic* movement is the key,
           cuts land on beats (2-8 beats).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
from scipy.ndimage import gaussian_filter1d, uniform_filter1d


@dataclass
class ModeConfig:
    name: str
    w_sharp: float = 1.0
    w_exposure: float = 0.8
    w_shake: float = 1.2
    w_pan: float = 0.6
    w_obstruct: float = 1.5
    w_face: float = 1.2
    pan_limit: float = 0.30        # frame-widths per second before a pan counts as a whip
    shake_ref: float = 0.010       # jitter level that costs 1 point
    rhythm_forgive: float = 0.0    # 0..1: how much beat-synced movement is forgiven
    min_shot: float = 2.5          # seconds (ceremony) ...
    max_shot: float = 10.0
    min_beats: int = 2             # ... or beats (dance)
    max_beats: int = 8
    switch_cost: float = 0.8       # score-seconds paid on every camera change
    stay_cost: float = 3.0         # paid each time a shot exceeds max length on the same cam
    downbeat_discount: float = 0.5 # dance: switching on a bar start is cheaper
    use_beats: bool = False
    dead_threshold: float = -1.2   # best available score below this = "dead" footage
    dead_min_sec: float = 2.0
    smooth_sec: float = 0.6
    good_level: float = 0.0        # above this a shot is "good enough"...
    good_slope: float = 0.3        # ...and extra quality counts only 30% (variety wins)
    cam_bias: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


PRESETS = {
    "ceremony": ModeConfig(name="ceremony"),
    "dance": ModeConfig(
        name="dance", w_sharp=1.0, w_exposure=0.6, w_shake=1.6, w_pan=0.3, w_obstruct=1.2,
        w_face=0.6, pan_limit=0.8, shake_ref=0.014, rhythm_forgive=0.65, min_shot=1.0, max_shot=6.0,
        min_beats=2, max_beats=8, switch_cost=0.35, stay_cost=1.2, use_beats=True,
        dead_threshold=-1.5, dead_min_sec=1.5, smooth_sec=0.4,
    ),
}


def _robust_z(x: np.ndarray, ref: np.ndarray) -> np.ndarray:
    ref = ref[np.isfinite(ref)]
    if len(ref) < 5:
        return np.zeros_like(x)
    med = np.median(ref)
    iqr = np.subtract(*np.percentile(ref, [75, 25])) or 1e-3
    return (x - med) / (iqr / 1.35)


def _nan_fill(x: np.ndarray) -> np.ndarray:
    x = x.astype(np.float64).copy()
    ok = np.isfinite(x)
    if ok.sum() == 0:
        return np.zeros_like(x)
    idx = np.arange(len(x))
    x[~ok] = np.interp(idx[~ok], idx[ok], x[ok])
    return x


def _rhythm(motion: np.ndarray, fps: float, beat_period: float | None, win_sec=4.0) -> np.ndarray:
    """0..1: how periodic camera movement is at the beat period (intentional dancing camera)."""
    if not beat_period or beat_period * fps < 1.5:
        return np.zeros_like(motion)
    lag = int(round(beat_period * fps))
    m = motion - uniform_filter1d(motion, int(win_sec * fps) or 1)
    a = m[lag:] * m[:-lag]
    w = int(win_sec * fps) or 1
    num = uniform_filter1d(a, w)
    den = uniform_filter1d(m[lag:] ** 2, w) + 1e-9
    r = np.clip(num / den, 0, 1)
    return np.concatenate([np.zeros(lag), r])


def score_cameras(analysis: dict, cfg: ModeConfig, beat_period: float | None = None) -> tuple[np.ndarray, list[dict]]:
    """Returns (scores[C, N], per-camera component dict for debugging/reporting)."""
    fps = analysis["fps"]
    cams = analysis["cams"]
    C, N = len(cams), len(analysis["times"])
    all_sharp = np.concatenate([c["sharp"] for c in cams])
    scores = np.full((C, N), -1e3, np.float64)
    parts = []
    sig = max(1.0, 0.5 * fps)

    for i, c in enumerate(cams):
        valid = np.isfinite(c["sharp"])
        if valid.sum() < 3:
            parts.append({})
            continue
        sharp = _nan_fill(c["sharp"])
        s_own = _robust_z(sharp, c["sharp"])
        s_all = _robust_z(sharp, all_sharp)
        sharp_s = np.clip(0.5 * s_own + 0.5 * s_all, -3, 2)

        bright, chi, clo = _nan_fill(c["bright"]), _nan_fill(c["clip_hi"]), _nan_fill(c["clip_lo"])
        exposure = (np.clip((chi - 0.06) / 0.1, 0, 2) + np.clip((clo - 0.35) / 0.2, 0, 2)
                    + np.clip((0.12 - bright) / 0.06, 0, 2) + np.clip((bright - 0.88) / 0.06, 0, 2))

        dx, dy = _nan_fill(c["dx"]), _nan_fill(c["dy"])
        sdx, sdy = gaussian_filter1d(dx, sig), gaussian_filter1d(dy, sig)
        jitter = uniform_filter1d(np.hypot(dx - sdx, dy - sdy), max(1, int(fps)))
        pan = np.hypot(sdx, sdy) * fps
        resp = _nan_fill(c["phase_resp"])
        rhythm = _rhythm(np.hypot(dx, dy), fps, beat_period) if cfg.rhythm_forgive > 0 else np.zeros(N)
        shake = np.clip(jitter / cfg.shake_ref, 0, 4) * (1 - cfg.rhythm_forgive * rhythm)
        shake += np.clip((0.05 - resp) / 0.05, 0, 1) * (np.hypot(dx, dy) > 0.02)  # smeared whip frames
        pan_pen = np.clip((pan - cfg.pan_limit) / cfg.pan_limit, 0, 3)

        # obstruction / focus hunting: sudden loss of detail vs the last few seconds,
        # while the camera itself isn't moving much (someone walked in front)
        w3 = max(3, int(3 * fps))
        edges = _nan_fill(c["edges"])
        med_e = uniform_filter1d(edges, w3)
        med_s = uniform_filter1d(sharp, w3)
        local = _nan_fill(c["local_change"])
        drop = (np.clip((med_e - edges) / (med_e + 1e-4) / 0.35, 0, 2)
                + np.clip((med_s - sharp) / 0.35, 0, 2))
        obstruct = drop * np.clip(local / 0.06, 0.3, 1.5) * (pan < cfg.pan_limit)

        nf, area = _nan_fill(c["faces"]), _nan_fill(c["face_area"])
        central = _nan_fill(c["face_central"])
        face = (nf > 0) * (0.35 + np.clip(np.sqrt(area) / 0.22, 0, 1) * (0.5 + 0.5 * central))
        face = gaussian_filter1d(face, sig)  # detector flicker
        pair = _nan_fill(c["pair_central"]) if "pair_central" in c else np.zeros(N)
        couple = gaussian_filter1d(pair, sig)   # couple framed in the centre of the shot
        cid = _nan_fill(c["couple"]) if "couple" in c and np.isfinite(c["couple"]).any() else np.zeros(N)
        cid = gaussian_filter1d(np.clip(cid, 0, 2) / 2, sig)       # bride / groom recognised
        smile = _nan_fill(c["smile"]) if "smile" in c and np.isfinite(c["smile"]).any() else np.zeros(N)
        smile = gaussian_filter1d(smile, sig)

        bias = cfg.cam_bias[i] if i < len(cfg.cam_bias) else 0.0
        total = (cfg.w_sharp * sharp_s - cfg.w_exposure * exposure - cfg.w_shake * shake
                 - cfg.w_pan * pan_pen - cfg.w_obstruct * obstruct + cfg.w_face * face
                 + getattr(cfg, "w_couple", 0.0) * couple + getattr(cfg, "w_couple_id", 0.0) * cid
                 + getattr(cfg, "w_smile", 0.0) * smile + bias)
        total = uniform_filter1d(total, max(1, int(cfg.smooth_sec * fps)))
        over = total > cfg.good_level
        total[over] = cfg.good_level + cfg.good_slope * (total[over] - cfg.good_level)
        total[~valid] = -1e3
        scores[i] = total
        parts.append({"sharp": sharp_s, "exposure": exposure, "shake": shake, "pan": pan_pen,
                      "obstruct": obstruct, "face": face, "couple": couple, "rhythm": rhythm,
                      "smile": smile, "couple_id": cid,
                      "hug": _nan_fill(c["hug"]) if "hug" in c and np.isfinite(c["hug"]).any() else np.zeros(N)})
    return scores, parts
