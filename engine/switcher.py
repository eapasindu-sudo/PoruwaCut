"""Choose which camera is on screen when: semi-Markov dynamic programming over blocks.

Blocks are either a uniform time grid (ceremony) or the intervals between music beats
(dance) so that every cut lands exactly on a beat.
"""
from __future__ import annotations

import numpy as np

from .scoring import ModeConfig


def make_blocks(t0: float, t1: float, cfg: ModeConfig, beats: np.ndarray | None = None,
                downbeats: np.ndarray | None = None, block_sec: float = 0.4):
    """Return (boundaries[T+1], is_downbeat[T+1], (Lmin, Lmax) in blocks)."""
    if cfg.use_beats and beats is not None and len(beats) > 8:
        b = beats[(beats > t0 + 0.05) & (beats < t1 - 0.05)]
        bounds = np.concatenate([[t0], b, [t1]])
        db = np.zeros(len(bounds), bool)
        if downbeats is not None and len(downbeats):
            near = np.abs(bounds[:, None] - downbeats[None, :]).min(axis=1) < 0.02
            db[near] = True
        # keep within min/max seconds too (very slow / very fast songs)
        period = float(np.median(np.diff(b))) if len(b) > 2 else 0.5
        lmin = max(1, min(cfg.min_beats, int(np.ceil(cfg.min_shot / period))))
        lmax = max(lmin + 1, min(cfg.max_beats, int(cfg.max_shot * 1.5 / period)))
        return bounds, db, (lmin, lmax)
    n = max(1, int(round((t1 - t0) / block_sec)))
    bounds = np.linspace(t0, t1, n + 1)
    lmin = max(1, int(round(cfg.min_shot / block_sec)))
    lmax = max(lmin + 1, int(round(cfg.max_shot / block_sec)))
    return bounds, np.zeros(n + 1, bool), (lmin, lmax)


def block_scores(scores: np.ndarray, times: np.ndarray, bounds: np.ndarray) -> np.ndarray:
    """Score-seconds per (camera, block): mean frame score x block duration."""
    C = scores.shape[0]
    T = len(bounds) - 1
    idx = np.searchsorted(times, bounds)
    out = np.empty((C, T))
    cs = np.concatenate([np.zeros((C, 1)), np.cumsum(scores, axis=1)], axis=1)
    for k in range(T):
        a, b = idx[k], max(idx[k + 1], idx[k] + 1)
        b = min(b, scores.shape[1])
        a = min(a, b - 1)
        out[:, k] = (cs[:, b] - cs[:, a]) / (b - a) * (bounds[k + 1] - bounds[k])
    return out


def choose(bs: np.ndarray, lmin: int, lmax: int, cfg: ModeConfig, is_down: np.ndarray) -> np.ndarray:
    """Semi-Markov DP. Returns camera index per block."""
    C, T = bs.shape
    cs = np.concatenate([np.zeros((C, 1)), np.cumsum(bs, axis=1)], axis=1).T  # [T+1, C]
    best = np.full((T + 1, C), -np.inf)
    arg_l = np.zeros((T + 1, C), np.int64)
    arg_c = np.zeros((T + 1, C), np.int64)
    best[0] = 0.0
    # transition cost matrix (prev -> next)
    trans = np.full((C, C), cfg.switch_cost)
    np.fill_diagonal(trans, cfg.stay_cost)
    for t in range(1, T + 1):
        lo = 1 if t == T else lmin          # the very last shot may be short
        ls = np.arange(lo, min(lmax, t) + 1)
        if len(ls) == 0:
            ls = np.array([t])
        starts = t - ls                      # [L]
        seg = cs[t][None, :] - cs[starts]    # [L, C]
        prev = best[starts]                  # [L, C']
        disc = np.where(is_down[starts], cfg.downbeat_discount, 1.0)[:, None, None]
        cand = prev[:, :, None] - trans[None, :, :] * disc   # [L, C', C]
        cand[starts == 0] = 0.0              # first shot: no transition cost
        pc = cand.argmax(axis=1)             # [L, C]
        pv = np.take_along_axis(cand, pc[:, None, :], 1)[:, 0, :]
        tot = seg + pv
        li = tot.argmax(axis=0)              # [C]
        best[t] = tot[li, np.arange(C)]
        arg_l[t] = ls[li]
        arg_c[t] = pc[li, np.arange(C)]
    # backtrack
    choice = np.zeros(T, np.int64)
    t, c = T, int(best[T].argmax())
    while t > 0:
        l, pc = arg_l[t, c], arg_c[t, c]
        choice[t - l:t] = c
        t, c = t - l, int(pc)
    return choice


def to_segments(choice: np.ndarray, bounds: np.ndarray, bs: np.ndarray, cfg: ModeConfig):
    """Merge equal neighbours; flag 'dead' blocks where even the best camera is bad."""
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
    # dead regions
    best_q = bs.max(axis=0) / np.maximum(np.diff(bounds), 1e-6)
    dead = []
    k = 0
    while k < T:
        if best_q[k] < cfg.dead_threshold:
            j = k
            while j < T and best_q[j] < cfg.dead_threshold:
                j += 1
            if bounds[j] - bounds[k] >= cfg.dead_min_sec:
                dead.append({"start": float(bounds[k]), "end": float(bounds[j])})
            k = j
        else:
            k += 1
    # merge dead regions separated by tiny gaps (avoid 0.4 s orphan clips)
    merged = []
    for d in dead:
        if merged and d["start"] - merged[-1]["end"] < max(1.0, cfg.min_shot * 0.6):
            merged[-1]["end"] = d["end"]
        else:
            merged.append(d)
    dead = merged
    for s in segs:
        qs = s.pop("_q")
        s["quality"] = float(sum(q * d for q, d in qs) / max(1e-6, sum(d for _, d in qs)))
    return segs, dead


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
