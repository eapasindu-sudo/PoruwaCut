"""Highlight cut: the best stretches of one event, cut down to a target length.

Built on top of a normal AutoWed edit of the In/Out range (camera choice, beats, best mic):
  * pieces ("chunks") of continuous sound from the clearest mic, each starting and ending on a
    downbeat, during music (dance / songs presets) - nothing from talk in between
  * pieces where the picture is weak, a camera has a crew member / someone's back in front of it,
    or nobody is filming are left out; small holes are covered with the best unused shot of the
    same length (cut on the beat) from elsewhere in the event
  * each join gets an equal-power crossfade of about two beats; every piece is brought to the same
    loudness
The panel builds the new timeline from this plan (all camera layers, the chosen one enabled).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.ndimage import maximum_filter1d


def _per_sec(T, t0, items, val=1.0, key=None):
    x = np.zeros(T, np.float32)
    for it in items:
        a, b = int(math.floor(it["start"] - t0)), int(math.ceil(it["end"] - t0))
        x[max(0, a):max(0, min(T, b))] = it[key] if key else val
    return x


def _crew(analysis, t0, T):
    """Per camera, per second: 1 where a big person stands turned away from the lens (a photographer
    / videographer / guest in front of the camera) or walks through the picture."""
    times = np.asarray(analysis["times"], np.float64)
    out = []
    for c in analysis["cams"]:
        f = lambda k: np.nan_to_num(np.asarray(c.get(k, np.zeros(len(times))), np.float64), nan=0.0)  # noqa: E731
        bad = ((f("away") > 0.5) & (f("person_h") > 0.55)) | (f("walk") > 0.3)
        sec = np.zeros(T, np.float32)
        idx = np.clip(np.floor(times - t0).astype(int), 0, T - 1)
        np.maximum.at(sec, idx, bad.astype(np.float32))
        out.append(maximum_filter1d(sec, 3))
    return out


def _loudness(clip, a, b):
    from .media import extract_audio
    try:
        st = clip.get("src_in", 0.0) + (a - clip["tl_start"])
        x = extract_audio(clip["path"], rate=8000, start=max(0.0, st), duration=max(0.5, b - a))
        if len(x) < 4000:
            return None
        # loud parts of the music, not the quiet gaps
        w = x[: len(x) // 400 * 400].reshape(-1, 400)
        r = np.sqrt((w ** 2).mean(axis=1) + 1e-12)
        return float(20 * np.log10(np.percentile(r, 80) + 1e-9))
    except Exception:  # noqa: BLE001
        return None


def plan(project: dict, segments: list, analysis: dict, scores: np.ndarray, beats: np.ndarray,
         dbeats: np.ndarray, songs: list, qc: dict, dead: list, target_sec: float, music_only: bool,
         log=print, style: str = "beats", phrases=None, speech=None, music_act=None) -> dict:
    """style: "beats" (dance / songs: cut on downbeats, music only), "phrases" (poruwa, speeches,
    rituals: cut where a sentence / chant ends, the whole event told in order), "auto" (welcomes,
    cake, table visits: downbeats where music plays, sentence ends elsewhere)."""
    r0, r1 = project["range"]
    t0 = float(r0)
    T = int(math.ceil(r1 - r0)) + 1
    cams = project["cameras"]
    C = len(cams)
    atracks = project.get("audio_tracks") or []
    segs = [s for s in segments if s["end"] > s["start"]]

    q = np.full(T, np.nan)
    for s in segs:
        if s["cam"] >= 0:
            a, b = int(math.floor(s["start"] - t0)), int(math.ceil(s["end"] - t0))
            q[max(0, a):max(0, min(T, b))] = max(-6.0, min(2.0, float(s.get("quality", 0.0))))
    dd = _per_sec(T, t0, dead)
    mic = np.full(T, -1)
    for m in qc.get("mic_choice") or []:
        a, b = int(math.floor(m["start"] - t0)), int(math.ceil(m["end"] - t0))
        mic[max(0, a):max(0, min(T, b))] = m["track"]
    clip = {}
    for c in qc.get("clipping") or []:
        clip.setdefault(c["track"], np.zeros(T, np.float32))
        a, b = int(math.floor(c["start"] - t0)), int(math.ceil(c["end"] - t0))
        clip[c["track"]][max(0, a):max(0, min(T, b))] = 1
    mom = np.zeros(T)
    for m in qc.get("moments") or []:
        k = int(m.get("time", 0) - t0)
        if 0 <= k < T:
            mom[k] += 1
    crew = _crew(analysis, t0, T)
    music = np.ones(T, np.float32)
    if music_only and songs:
        music = _per_sec(T, t0, [s for s in songs if s.get("bpm", 0) > 0])
    # where people talk (AI voice), per second: a piece never starts or ends inside a sentence
    talk = np.zeros(T, np.float32)
    gaps = []
    if speech is not None:
        try:
            s0, step, vp = speech
            vp = np.asarray(vp, np.float64)
            tt_ = s0 + np.arange(len(vp)) * step
            idx = np.clip(np.floor(tt_ - t0).astype(int), 0, T - 1)
            np.maximum.at(talk, idx, (vp > 0.6).astype(np.float32))
            quiet = vp < 0.3
            i = 0
            while i < len(quiet):          # pauses of >= 0.4 s: their middle is a clean place to cut
                if quiet[i]:
                    j = i
                    while j + 1 < len(quiet) and quiet[j + 1]:
                        j += 1
                    if (j - i + 1) * step >= 0.4:
                        gaps.append(s0 + (i + j + 1) / 2 * step)
                    i = j + 1
                else:
                    i += 1
        except Exception:  # noqa: BLE001
            pass
    mus = None
    if music_act is not None:
        m0, m_ = music_act if isinstance(music_act, tuple) else (t0, music_act)
        m_ = np.asarray(m_, np.float32)
        if len(m_):
            mus = np.zeros(T, np.float32)
            o = int(round(m0 - t0))
            for i in range(len(m_)):
                if 0 <= i + o < T:
                    mus[i + o] = m_[i]
    db_ = np.asarray(dbeats if len(dbeats) >= 8 else beats, np.float64)
    ph_ = np.concatenate([np.asarray(phrases if phrases is not None else [], np.float64), np.asarray(gaps, np.float64)])
    if style == "beats":
        grid = db_
    elif style == "phrases":
        grid = ph_
    else:
        on_music = np.array([mus is not None and 0 <= int(t - t0) < T and mus[int(t - t0)] > 0.55 for t in db_], bool) \
            if len(db_) else np.zeros(0, bool)
        off_music = np.array([not (mus is not None and 0 <= int(t - t0) < T and mus[int(t - t0)] > 0.55) for t in ph_], bool) \
            if len(ph_) else np.zeros(0, bool)
        grid = np.concatenate([db_[on_music], ph_[off_music]])
    if len(grid) < 4:
        grid = np.asarray([s["start"] for s in segs] + [s["end"] for s in segs], np.float64)
    grid = np.unique(grid)
    bp = float(np.median(np.diff(beats))) if len(beats) > 2 else 0.5
    calm = style != "beats"
    rit = [float(r["start"]) for r in (qc.get("rituals") or [])]

    def snap(t, lo, hi):
        g = grid[(grid >= lo) & (grid <= hi)]
        return float(g[np.argmin(np.abs(g - t))]) if len(g) else None

    seg_cam = np.full(T, -1)
    for s in segs:
        a, b = int(math.floor(s["start"] - t0)), int(math.ceil(s["end"] - t0))
        seg_cam[max(0, a):max(0, min(T, b))] = s["cam"]

    cands = []
    for ti, trk in enumerate(atracks):
        for cl in trk:
            a0, b0 = max(r0, cl["tl_start"] + 0.3), min(r1, cl["tl_end"] - 0.3)
            for L in ((15.0, 25.0, 40.0, 60.0, 90.0) if calm else (20.0, 30.0, 45.0, 60.0)):
                t = a0
                while t + L <= b0 + 0.01:
                    a, b = snap(t, a0, b0), snap(t + L, a0, b0)
                    t += 4.0
                    if a is None or b is None or b - a < 15:
                        continue
                    ia, ib = int(a - t0), int(b - t0)
                    n = ib - ia
                    if n <= 0:
                        continue
                    qq = q[ia:ib]
                    if not np.isfinite(qq).any():
                        continue
                    df, mf = float(dd[ia:ib].mean()), float(music[ia:ib].mean())
                    cf = float(clip[ti][ia:ib].mean()) if ti in clip else 0.0
                    if df > 0.65 or cf > 0.3 or mf < 0.8:
                        continue
                    # never in the middle of a sentence
                    if calm and (talk[min(T - 1, max(0, ia))] > 0 and talk[min(T - 1, max(0, ia - 1))] > 0
                                 or talk[min(T - 1, max(0, ib))] > 0 and talk[min(T - 1, max(0, ib - 1))] > 0):
                        continue
                    sc_ = seg_cam[ia:ib]
                    cr = float(np.mean([crew[c][ia + k] if 0 <= c < C else 0 for k, c in enumerate(sc_)]))
                    score = (float(np.nanmean(qq)) + (1.5 if calm else 1.0) * float((mic[ia:ib] == ti).mean())
                             - 4.0 * cf + (0.6 if calm else 0.3) * float(mom[ia:ib].sum()) - 2.0 * df - 1.5 * cr
                             + (1.0 * sum(a <= x < b for x in rit) if calm else 0.0))
                    cands.append({"a": a, "b": b, "atrack": ti, "score": score, "len": b - a})
    cands.sort(key=lambda c: -c["score"])
    chosen, total, block = [], 0.0, {}
    # the whole event, in order: a few pieces from every part (ceremonies tell a story; dance spreads)
    if calm:
        parts = max(3, int(round(target_sec / 35.0)))
        blk, per = max(60.0, (r1 - r0) / parts), 2
    else:
        blk, per = 240.0, 8
    for relaxed in (False, True):
        for c in cands:
            if total >= target_sec - 8:
                break
            if any(c["a"] < x["b"] + 0.5 and c["b"] > x["a"] - 0.5 for x in chosen):
                continue
            k = int((c["a"] - t0) // blk)
            if (not relaxed and block.get(k, 0) >= per) or total + c["len"] > target_sec + 12:
                continue
            chosen.append(c)
            block[k] = block.get(k, 0) + 1
            total += c["len"]
    chosen.sort(key=lambda c: c["a"])

    def in_clip(ti, a, b):
        return next((cl for cl in atracks[ti] if cl["tl_start"] <= a + 0.02 and cl["tl_end"] >= b - 0.02), None)

    merged = []
    for c in chosen:
        p = merged[-1] if merged else None
        if p and p["atrack"] == c["atrack"] and c["a"] - p["b"] < 9 and in_clip(c["atrack"], p["a"], c["b"]):
            p["b"] = c["b"]
            continue
        merged.append(dict(c))
    chunks = merged
    if not chunks:
        return {"target": target_sec, "chunks": [], "shots": [], "cutaways": [], "total": 0.0}

    def covers(ci, a, b):
        return any(cl["tl_start"] <= a + 0.02 and cl["tl_end"] >= b - 0.02 for cl in cams[ci]["clips"])

    times = np.asarray(analysis["times"], np.float64)

    def cam_score(ci, a, b):
        m = (times >= a) & (times < b)
        return float(np.nanmean(scores[ci][m])) if m.any() and np.isfinite(scores[ci][m]).any() else -99.0

    def crew_frac(ci, a, b):
        ia, ib = int(a - t0), max(int(a - t0) + 1, int(math.ceil(b - t0)))
        return float(crew[ci][ia:ib].mean()) if 0 <= ci < C else 1.0

    shots, holes = [], []
    for k, c in enumerate(chunks):
        for s in segs:
            a, b = max(s["start"], c["a"]), min(s["end"], c["b"])
            if b - a < 0.004:
                continue
            if b - a < 0.25 and shots and shots[-1]["chunk"] == k and abs(shots[-1]["b"] - a) < 0.02 \
                    and covers(shots[-1]["cam"], shots[-1]["a"], b):
                shots[-1]["b"] = round(b, 4)        # a sliver joins the shot before it
                continue
            cam = s["cam"]
            ok = cam >= 0 and covers(cam, a, b) and crew_frac(cam, a, b) < 0.4
            ia, ib = int(a - t0), max(int(a - t0) + 1, int(b - t0))
            weak = float(dd[ia:ib].mean()) > 0.7 and b - a >= bp
            if not ok or weak:
                alts = [(cam_score(ci, a, b), ci) for ci in range(C)
                        if ci != cam and covers(ci, a, b) and crew_frac(ci, a, b) < 0.4]
                alts.sort(reverse=True)
                if alts and not weak and alts[0][0] > -50:
                    cam = alts[0][1]
                    ok = True
            if ok and not weak:
                shots.append({"chunk": k, "cam": int(cam), "a": round(a, 4), "b": round(b, 4)})
            else:
                holes.append({"chunk": k, "a": a, "b": b,
                              "cam": int(cam) if cam >= 0 and covers(cam, a, b) else -1})
    # join a tiny shot to the one before it (same chunk, same camera can show it)
    sh2 = []
    for s in shots:
        p = sh2[-1] if sh2 else None
        if p and p["chunk"] == s["chunk"] and abs(p["b"] - s["a"]) < 0.02 and (s["b"] - s["a"] < 0.5 or p["cam"] == s["cam"]) \
                and covers(p["cam"], p["a"], s["b"]):
            p["b"] = s["b"]
            continue
        sh2.append(s)
    shots = sh2

    # holes -> best unused shot of the same length from elsewhere (not inside a chosen piece)
    used = [(c["a"] - 10, c["b"] + 10) for c in chunks]
    cutaways = []
    for h in holes:
        L = h["b"] - h["a"]
        best = None
        for ci in range(C):
            for cl in cams[ci]["clips"]:
                t = max(r0, cl["tl_start"] + 0.2)
                while t + L <= min(r1, cl["tl_end"] - 0.2):
                    if not any(t < u1 and t + L > u0 for u0, u1 in used):
                        ia, ib = int(t - t0), max(int(t - t0) + 1, int(math.ceil(t + L - t0)))
                        if float(crew[ci][ia:ib].mean()) < 0.2 and float(music[ia:ib].mean()) >= 0.5:
                            sc = cam_score(ci, t, t + L)
                            if best is None or sc > best[0]:
                                best = (sc, ci, t)
                    t += max(1.0, L / 2)
        if best and best[0] > -50:
            cutaways.append({"chunk": h["chunk"], "cam": best[1], "src": round(best[2], 4),
                             "a": round(h["a"], 4), "b": round(h["b"], 4)})
            used.append((best[2] - 3, best[2] + L + 3))
        elif h["cam"] >= 0:      # nothing better: keep the weak picture rather than black
            shots.append({"chunk": h["chunk"], "cam": h["cam"], "a": round(h["a"], 4), "b": round(h["b"], 4)})
    shots.sort(key=lambda s: (s["chunk"], s["a"]))

    # joins: equal-power crossfade of ~2 beats; handles only where the mic kept recording
    for k, c in enumerate(chunks):
        c["h_in"] = c["h_out"] = 0.0
    for k in range(len(chunks) - 1):
        A, B = chunks[k], chunks[k + 1]
        xf = min(1.2, max(0.5, 2 * bp)) if not calm else 1.0
        h = xf / 2
        ca, cb = in_clip(A["atrack"], A["a"], A["b"]), in_clip(B["atrack"], B["a"], B["b"])
        roomA = (ca["tl_end"] - A["b"]) if ca else 0
        roomB = (B["a"] - cb["tl_start"]) if cb else 0
        h = max(0.0, min(h, roomA - 0.05, roomB - 0.05))
        A["h_out"] = B["h_in"] = round(h, 3)
    # same loudness everywhere: each piece to the median level (music peaks), at most +-8 dB
    lv = []
    for c in chunks:
        cl = in_clip(c["atrack"], c["a"], c["b"])
        lv.append(_loudness(cl, c["a"], c["b"]) if cl else None)
    ok_lv = [x for x in lv if x is not None]
    ref = float(np.median(ok_lv)) if ok_lv else None
    for c, x in zip(chunks, lv):
        c["gain_db"] = round(max(-8.0, min(8.0, ref - x)), 2) if (ref is not None and x is not None) else 0.0
        c["name"] = (in_clip(c["atrack"], c["a"], c["b"]) or {}).get("path", "")
        c.pop("score", None)
        c.pop("len", None)
        c["a"], c["b"] = round(c["a"], 4), round(c["b"], 4)
    total = float(sum(c["b"] - c["a"] for c in chunks))
    log(f"  Highlight cut: කොටස් {len(chunks)} · {total / 60:.1f} min · shots {len(shots)} · "
        f"හිස් තැන් වසන්න shots {len(cutaways)} · "
        + (f"crossfade ~{min(1.2, max(0.5, 2 * bp)):.2f}s (beats)" if not calm else "crossfade 1.0s (වාක්‍ය / කොටස් ඉවර වෙන තැන්)"))
    return {"target": target_sec, "chunks": chunks, "shots": shots, "cutaways": cutaways,
            "total": round(total, 2), "beat": round(bp, 4), "fade_in": 1.0, "fade_out": 2.5}
