"""Choose which camera is on screen when.

1. build_cut_grid : every moment where a cut is allowed, each with a "preference" weight
   (music beat / bar start, end of a spoken phrase, or a plain time slot that is discouraged).
2. choose        : semi-Markov dynamic programming over that grid (shot length limits in seconds,
   cost per camera change scaled by the cut point's weight). Can continue from a previous camera,
   which is how the live (window by window) edit stays seamless.
"""
from __future__ import annotations

import numpy as np


def build_cut_grid(t0: float, t1: float, preset, beats=None, downbeats=None, phrases=None,
                   music=None, filler_step: float = 0.5, rolls=None, speech=None):
    """Return (bounds[T+1], weight[T+1]).  weight multiplies the switch cost at that boundary."""
    pts: dict[float, float] = {}

    def add(t, w):
        if t0 + 0.05 < t < t1 - 0.05:
            k = round(float(t), 3)
            pts[k] = min(w, pts.get(k, 9.9))

    grid = preset.grid
    beats = np.asarray(beats if beats is not None else [], float)
    downs = np.asarray(downbeats if downbeats is not None else [], float)
    ph_t, ph_s = phrases if phrases is not None else (np.zeros(0), np.zeros(0))

    def is_music(t):
        if grid == "beats":
            return True
        if grid == "phrases":
            return False
        if music is None or len(music) == 0:
            return len(beats) > 8
        i = int(min(max(t - t0, 0), len(music) - 1))
        return music[i] > 0.55

    for b in beats:
        if is_music(b):
            add(b, 1.0)
    for i, d in enumerate(downs):
        if is_music(d):
            # every 4th bar starts a musical phrase (verse / chorus lines): the most natural cut
            add(d, 0.35 if i % 4 == 0 else 0.55)
    for t, s in zip(ph_t, ph_s):
        if not is_music(t):
            add(t, 0.55 - 0.3 * float(s))
    # drum rolls: cut on the roll's hits (every other hit when they are very fast), and the
    # landing hit right after the roll is the most wanted cut of all
    for r in rolls or []:
        hits = r["hits"]
        step = 2 if len(hits) > 1 and (hits[-1] - hits[0]) / max(1, len(hits) - 1) < 0.16 else 1
        for h in hits[::step]:
            add(h, 0.3)
        add(r["land"], 0.12)
    # filler slots: allowed but discouraged (so a bad shot can still be left mid-phrase / off-beat)
    filler_w = 3.0 if grid in ("beats", "phrases") else 2.2

    def talking(t):
        # speech = (start time, step, per-step probability) from the AI voice detection
        if speech is None:
            return False
        s0, st, pr = speech
        i = int((t - s0) / st)
        return 0 <= i < len(pr) and pr[max(0, i - 2):i + 3].min() > 0.6   # in the middle of words
    for t in np.arange(t0 + filler_step, t1, filler_step):
        # a slot in the middle of someone talking is a much worse place to cut than a pause
        add(t, filler_w * (2.5 if (not is_music(t) and talking(t)) else 1.0))
    ks = sorted(pts)
    # merge points closer than 0.12 s (keep the preferred one)
    merged = []
    for k in ks:
        if merged and k - merged[-1][0] < 0.12:
            if pts[k] < merged[-1][1]:
                merged[-1] = (k, pts[k])
        else:
            merged.append((k, pts[k]))
    bounds = np.array([t0] + [m[0] for m in merged] + [t1])
    weight = np.array([1.0] + [m[1] for m in merged] + [1.0])
    return bounds, weight


def block_scores(scores: np.ndarray, times: np.ndarray, bounds: np.ndarray) -> np.ndarray:
    """Score-seconds per (camera, block): mean frame score x block duration."""
    C = scores.shape[0]
    T = len(bounds) - 1
    idx = np.searchsorted(times, bounds)
    out = np.empty((C, T))
    cs = np.concatenate([np.zeros((C, 1)), np.cumsum(scores, axis=1)], axis=1)
    nmax = scores.shape[1]
    for k in range(T):
        a = min(idx[k], nmax - 1)
        b = min(max(idx[k + 1], a + 1), nmax)
        out[:, k] = (cs[:, b] - cs[:, a]) / (b - a) * (bounds[k + 1] - bounds[k])
    return out


def _roll_mask(bounds, rolls):
    m = np.zeros(len(bounds), bool)
    for r in rolls or []:
        m |= (bounds >= r["start"] - 0.05) & (bounds <= r["land"] + 0.05)
    return m


def choose(bs: np.ndarray, bounds: np.ndarray, weight: np.ndarray, preset, prev_cam: int | None = None,
           final: bool = True, rolls=None, beat_period=None, pair_extra=None) -> np.ndarray:
    """Semi-Markov DP with shot lengths in seconds. Returns camera index per block."""
    C, T = bs.shape
    cs = np.concatenate([np.zeros((C, 1)), np.cumsum(bs, axis=1)], axis=1).T  # [T+1, C]
    best = np.full((T + 1, C), -np.inf)
    arg_s = np.zeros((T + 1, C), np.int64)
    arg_c = np.zeros((T + 1, C), np.int64)
    best[0] = 0.0
    trans = np.full((C, C), preset.switch_cost)
    # re-starting the same camera = an over-long shot in disguise; only worth it when every other
    # camera is unusable (not recording / far worse)
    if pair_extra is not None:
        trans = trans + pair_extra      # e.g. two cameras with the same shot size: cutting between them looks like a jump
    np.fill_diagonal(trans, preset.switch_cost + 4.0 * preset.stay_cost)
    hard_max = preset.max_shot * 2.0
    # inside a drum roll shots may be as short as the roll's hits, and should not stay long
    in_roll = _roll_mask(bounds, rolls)
    roll_min = max(0.25, min(preset.min_shot, 0.5 * (beat_period or 0.5)))
    roll_max = max(roll_min * 2, beat_period or 0.5)
    min_at = np.where(in_roll, roll_min, preset.min_shot)
    max_at = np.where(in_roll, roll_max, preset.max_shot)
    # seconds of roll inside each block, cumulated: a shot that holds through a roll pays for it
    cov = np.zeros(T)
    for r in rolls or []:
        a, b = r["start"], r["land"]
        cov += np.clip(np.minimum(bounds[1:], b) - np.maximum(bounds[:-1], a), 0, None)
    ccov = np.concatenate([[0.0], np.cumsum(cov)])
    first_cost = np.zeros(C)
    if prev_cam is not None:
        first_cost = np.full(C, preset.switch_cost * weight[0])
        first_cost[prev_cam] = 0.0
    for t in range(1, T + 1):
        dur_all = bounds[t] - bounds[:t]                      # durations for starts 0..t-1
        ok = dur_all <= hard_max
        if not (final and t == T):
            ok &= (dur_all >= min_at[:t]) | (np.arange(t) == 0)
        starts = np.nonzero(ok)[0]
        if len(starts) == 0:
            starts = np.array([t - 1])
        dur = dur_all[starts]
        seg = cs[t][None, :] - cs[starts]                     # [L, C]
        mx = max_at[starts]
        over = 2.0 * preset.stay_cost * np.maximum(0.0, dur - mx) / mx
        if rolls:
            ro = ccov[t] - ccov[starts]
            over = over + 3.0 * preset.stay_cost * np.maximum(0.0, ro - roll_max) / roll_max
        prev = best[starts]                                   # [L, C']
        cand = prev[:, :, None] - trans[None] * weight[starts][:, None, None]  # [L, C', C]
        z = starts == 0
        if z.any():
            cand[z] = -np.inf
            cand[z, 0, :] = -first_cost[None, :]
        pc = cand.argmax(axis=1)                              # [L, C]
        pv = np.take_along_axis(cand, pc[:, None, :], 1)[:, 0, :]
        tot = seg + pv - over[:, None]
        li = tot.argmax(axis=0)
        best[t] = tot[li, np.arange(C)]
        arg_s[t] = starts[li]
        arg_c[t] = pc[li, np.arange(C)]
    choice = np.zeros(T, np.int64)
    t, c = T, int(best[T].argmax())
    while t > 0:
        s, pcam = arg_s[t, c], arg_c[t, c]
        choice[s:t] = c
        t, c = s, int(pcam)
    return choice


def to_segments(choice: np.ndarray, bounds: np.ndarray, bs: np.ndarray, preset):
    """Merge equal neighbours; flag 'dead' stretches where even the best camera is bad."""
    segs = []
    T = len(choice)
    for k in range(T):
        c = int(choice[k])
        dur = bounds[k + 1] - bounds[k]
        q = bs[c, k] / max(dur, 1e-6)
        if segs and segs[-1]["cam"] == c:
            segs[-1]["end"] = float(bounds[k + 1])
            segs[-1]["_q"].append((q, dur))
        else:
            segs.append({"cam": c, "start": float(bounds[k]), "end": float(bounds[k + 1]), "_q": [(q, dur)]})
    best_q = bs.max(axis=0) / np.maximum(np.diff(bounds), 1e-6)
    dead = []
    k = 0
    while k < T:
        if best_q[k] < preset.dead_threshold:
            j = k
            while j < T and best_q[j] < preset.dead_threshold:
                j += 1
            if bounds[j] - bounds[k] >= preset.dead_min_sec:
                dead.append({"start": float(bounds[k]), "end": float(bounds[j])})
            k = j
        else:
            k += 1
    merged = []
    for d in dead:
        if merged and d["start"] - merged[-1]["end"] < max(1.0, preset.min_shot * 0.6):
            merged[-1]["end"] = d["end"]
        else:
            merged.append(d)
    for s in segs:
        qs = s.pop("_q")
        s["quality"] = float(sum(q * d for q, d in qs) / max(1e-6, sum(d for _, d in qs)))
    return segs, merged


def apply_dead_cuts(segs: list[dict], dead: list[dict]) -> list[dict]:
    """Remove dead time from the segment list (keeps master-time positions; gaps = removed)."""
    out = []
    for s in segs:
        pieces = [(s["start"], s["end"])]
        for d in dead:
            nxt = []
            for a, b in pieces:
                if d["end"] <= a or d["start"] >= b:
                    nxt.append((a, b))
                    continue
                if d["start"] > a:
                    nxt.append((a, d["start"]))
                if d["end"] < b:
                    nxt.append((d["end"], b))
            pieces = nxt
        for a, b in pieces:
            if b - a > 0.2:
                out.append({**s, "start": a, "end": b})
    return out
