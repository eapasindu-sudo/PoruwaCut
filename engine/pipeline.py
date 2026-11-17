"""End-to-end, window by window so cuts can be applied to the timeline while analysis runs.

audio (beats, phrase ends, music) -> for each 60 s window: analyse all cameras -> score ->
dynamic programming from the last committed cut -> commit cuts older than the look-ahead ->
on_segments callback (the panel razors the timeline live) -> final result.json (+ XML).
"""
from __future__ import annotations

import csv
import json
import os
import re
import time

import numpy as np

from . import beats as beatmod
from . import presets
from .analyze import METRICS, analyze_cameras
from .audio_events import music_activity, phrase_points, sequence_audio
from .scoring import score_cameras
from .switcher import apply_dead_cuts, block_scores, build_cut_grid, choose, to_segments
from .xml_export import build_xml


COUPLE_EVENTS = {"auto", "groom_welcome", "bride_welcome", "poruwa", "pahana", "first_dance",
                 "table_visit", "surprise_dance", "speeches", "church_mass"}


def _moments(mix16, t0, times, parts, segs, max_n: int = 25):
    """Candidate highlight moments: applause / cheering, smiles, hugs on the chosen shots."""
    from .events_detect import KINDS, classify
    cand = []
    try:
        lab = classify(mix16, 16000) if len(mix16) > 16000 * 5 else np.zeros(0, int)
        ap = KINDS.index("applause")
        for s_ in range(len(lab)):
            if lab[s_] == ap and (s_ == 0 or lab[s_ - 1] != ap):
                cand.append({"time": round(t0 + s_, 2), "kind": "applause", "score": 1.0})
    except Exception:  # noqa: BLE001
        pass
    for sg in segs:
        c = sg["cam"]
        if c < 0 or c >= len(parts) or not parts[c]:
            continue
        m = (times >= sg["start"]) & (times < sg["end"])
        if not m.any():
            continue
        for key, kind, thr in (("smile", "smile", 0.6), ("hug", "hug", 0.6)):
            v = parts[c].get(key)
            if v is None:
                continue
            vv = np.where(m, v, 0)
            i = int(np.argmax(vv))
            if vv[i] >= thr:
                cand.append({"time": round(float(times[i]), 2), "kind": kind, "score": round(float(vv[i]), 2), "cam": c})
    cand.sort(key=lambda d: -d["score"])
    out = []
    for d in cand:
        if all(abs(d["time"] - o["time"]) > 15 for o in out):
            out.append(d)
        if len(out) >= max_n:
            break
    return sorted(out, key=lambda d: d["time"])


def _weak_takes(times, parts, project, min_len: float = 3.0):
    """Per camera stretches that are blurred, too dark / blown out or very shaky (QC markers)."""
    out = []
    for c, p in enumerate(parts):
        if not p:
            continue
        for key, why, test in (("sharp", "blur / focus", lambda v: v < -1.6), ("exposure", "exposure", lambda v: v > 1.5),
                               ("shake", "shake", lambda v: v > 2.5)):
            v = p.get(key)
            if v is None:
                continue
            bad = test(np.asarray(v))
            i = 0
            while i < len(bad):
                if bad[i]:
                    j = i
                    while j < len(bad) and bad[j]:
                        j += 1
                    if j - i >= 2 and times[min(j, len(times) - 1)] - times[i] >= min_len:
                        out.append({"cam": c, "name": project["cameras"][c]["name"], "why": why,
                                    "start": round(float(times[i]), 2), "end": round(float(times[min(j, len(times) - 1)]), 2)})
                    i = j
                else:
                    i += 1
    return out


def _merge_into(committed: list, new: list):
    for s in new:
        if committed and committed[-1]["cam"] == s["cam"] and abs(committed[-1]["end"] - s["start"]) < 1e-3:
            committed[-1]["end"] = s["end"]
        else:
            committed.append(dict(s))


def run(project: dict, event: str = "auto", out_dir: str = "out", overrides: dict | None = None,
        analysis_fps: float | None = None, use_gpu: bool = True, workers: int = 2, layout: str = "stacked",
        remove_dead: bool = False, cache_dir: str | None = None, write_xml: bool = True, log=print,
        progress=None, on_segments=None, window: float = 60.0, lookahead: float = 12.0,
        xml_name: str | None = None, extras: dict | None = None) -> dict:
    """progress(stage, overall, stage_frac); on_segments(committed_segments, committed_until)."""
    t_start = time.time()
    if event in ("ceremony",):
        event = "poruwa"
    elif event in ("dance",):
        event = "dance_floor"
    os.makedirs(out_dir, exist_ok=True)
    cache_dir = cache_dir or os.path.join(os.path.dirname(os.path.abspath(out_dir)), ".poruwacut_cache")
    preset = presets.get(event, overrides)
    analysis_fps = float(analysis_fps or preset.analysis_fps)
    workers = max(int(workers or 1), min(6, len(project["cameras"])))  # every camera in parallel
    t0, t1 = project["range"]
    r0, r1 = t0, t1                       # the editor's In/Out (t0/t1 may shrink with smart trim)
    fps = float(project["fps"])
    ex = dict(extras or {})
    want_couple = ex.get("couple", True) and preset.id in COUPLE_EVENTS
    stages = {"audio": (0.0, 0.08), "faces": (0.08, 0.14 if want_couple else 0.08),
              "analysis": (0.14 if want_couple else 0.08, 0.96), "qc": (0.96, 0.98),
              "cuts": (0.98, 0.99), "xml": (0.99, 1.0)}

    from .media import STOP, Cancelled

    def prog(stage, f):
        if STOP.is_set():
            raise Cancelled()
        if progress:
            a, b = stages[stage]
            progress(stage, a + (b - a) * f, f)

    log(f"Event: {preset.en} | {t0:.1f}s - {t1:.1f}s ({(t1 - t0) / 60:.1f} min) | cams {len(project['cameras'])}")

    # ---- audio: beats, phrase ends, music activity -------------------------------------------
    prog("audio", 0.0)
    log("Audio එක අහමින් (beats, වචන ඉවර වෙන තැන්, music)...")
    audio = sequence_audio(project, t0, t1, beatmod.SR, progress=lambda f: prog("audio", 0.5 * f))
    beats = dbeats = np.zeros(0)
    beat_period = None
    music = np.zeros(int(t1 - t0) + 1)
    ph = (np.zeros(0), np.zeros(0))
    if len(audio) > beatmod.SR * 2 and np.abs(audio).max() > 0:
        if preset.grid in ("beats", "auto"):
            b, _tempos, strength = beatmod.detect_beats(audio, progress=lambda f: prog("audio", 0.5 + 0.3 * f))
            beats = b + t0
            dbeats = beatmod.downbeats(beats, strength)
            if len(b) > 2:
                beat_period = float(np.median(np.diff(b)))
        prog("audio", 0.8)
        if preset.grid in ("phrases", "auto"):
            pt, ps = phrase_points(audio, beatmod.SR)
            ph = (pt + t0, ps)
        if preset.grid == "auto":
            prog("audio", 0.85)
            music = music_activity(audio, beatmod.SR, progress=lambda f: prog("audio", 0.85 + 0.15 * f))
        log(f"  beats {len(beats)}" + (f" (~{60 / beat_period:.0f} BPM)" if beat_period else "")
            + f", වචන ඉවර වෙන තැන් {len(ph[0])}"
            + (f", music {100 * float((music > 0.55).mean()):.0f}% of time" if preset.grid == "auto" else ""))
    else:
        log("  Audio නැහැ - visuals විතරක් බලලා cut කරනවා")
    prog("audio", 1.0)
    from scipy.signal import resample_poly
    mix16 = resample_poly(audio, 16000, beatmod.SR).astype(np.float32) if len(audio) else np.zeros(0, np.float32)
    del audio

    # ---- smart trim: empty head / tail of the range (decided from the audio) ------------------
    trim = None
    if ex.get("trim"):
        from .audio_qc import smart_trim
        trim = smart_trim(mix16, r0, r1, [])
        if trim:
            t0, t1 = trim
            log(f"  Smart trim: {trim[0] - r0:.1f}s මුලින්, {r1 - trim[1]:.1f}s අගින් හිස් කොටස් අයින් කරනවා")
            beats = beats[(beats >= t0) & (beats <= t1)]
            dbeats = dbeats[(dbeats >= t0) & (dbeats <= t1)]
            keep = (ph[0] >= t0) & (ph[0] <= t1)
            ph = (ph[0][keep], ph[1][keep])
            music = music[int(t0 - r0):] if len(music) else music

    # ---- the couple's faces (bride + groom), recognised across the cameras -----------------------
    from .analyze import find_couple, set_couple
    set_couple(None)
    if want_couple:
        try:
            log("මනාලයා / මනාලිය අඳුරගන්නවා (face recognition)...")
            cents = find_couple(project["cameras"], t0, t1, use_gpu, log=log, progress=lambda f: prog("faces", f))
            set_couple(cents)
            if cents is None:
                log("  face recognition model නැති නිසා හෝ මූණු මදි නිසා මඟ හැරියා")
        except Exception as e:  # noqa: BLE001
            log(f"  face recognition මඟ හැරියා: {e}")
    prog("faces", 1.0)

    # ---- window by window ---------------------------------------------------------------------
    log("කැමරා visuals analyze කරමින් (window එකෙන් window එක, cuts live)...")
    C = len(project["cameras"])
    acc_times: list = []
    acc = [{m: [] for m in METRICS} for _ in range(C)]
    committed: list[dict] = []
    if trim and t0 > r0:   # trimmed head: every camera off (-1)
        committed.append({"cam": -1, "start": r0, "end": t0})
    commit_t, prev_cam = t0, None
    wins = list(np.arange(t0, t1, window))
    detectors = set()
    times = np.zeros(0)
    scores = np.zeros((C, 0))
    for wi, w0 in enumerate(wins):
        w1 = min(t1, w0 + window)
        final = wi == len(wins) - 1
        an = analyze_cameras(project["cameras"], w0, w1, analysis_fps, cache_dir, use_gpu, workers,
                             log=lambda m: None,
                             progress=lambda f, wi=wi: prog("analysis", (wi + f) / len(wins)))
        acc_times.append(an["times"])
        for i in range(C):
            detectors.add(an["cams"][i].get("detector", "?"))
            for m in METRICS:
                acc[i][m].append(an["cams"][i][m])
        times = np.concatenate(acc_times)
        analysis = {"times": times, "fps": analysis_fps,
                    "cams": [{m: np.concatenate(acc[i][m]) for m in METRICS} for i in range(C)]}
        scores, _parts = score_cameras(analysis, preset, beat_period)
        if w1 - commit_t < preset.min_shot + lookahead and not final:
            continue
        bounds, weight = build_cut_grid(commit_t, w1, preset, beats, dbeats, ph,
                                        music[int(commit_t - t0):] if len(music) else None)
        bs = block_scores(scores, times, bounds)
        choice = choose(bs, bounds, weight, preset, prev_cam, final=final)
        segs, _ = to_segments(choice, bounds, bs, preset)
        limit = w1 if final else w1 - lookahead
        new = [s for s in segs if s["end"] <= limit + 1e-6]
        if new:
            for s in new:  # snap to sequence frames
                s["start"] = round(s["start"] * fps) / fps
                s["end"] = round(s["end"] * fps) / fps
            new = [s for s in new if s["end"] > s["start"]]
        if new:
            _merge_into(committed, new)
            commit_t, prev_cam = committed[-1]["end"], committed[-1]["cam"]
            if prev_cam < 0:
                prev_cam = None
            if on_segments:
                on_segments([dict(s) for s in committed], commit_t)
        log(f"  {(w1 - t0) / 60:.1f}/{(t1 - t0) / 60:.1f} min: cuts {max(0, len(committed) - 1)}")
    from .media import decode_mode
    log(f"  face detector: {', '.join(sorted(detectors))} · decode: {decode_mode()} · fps {analysis_fps:g} · parallel {workers}")

    # ---- weak stretches (all cameras poor) -------------------------------------------------
    prog("cuts", 0.0)
    bounds = np.arange(t0, t1 + 0.5, 0.5)
    bounds[-1] = t1
    bounds = np.unique(bounds)
    bs = block_scores(scores, times, bounds)
    _, dead = to_segments(np.argmax(bs, axis=0), bounds, bs, preset)
    final_segs = apply_dead_cuts(committed, dead) if remove_dead else committed
    if trim and t1 < r1:
        final_segs.append({"cam": -1, "start": t1, "end": r1})
        if on_segments:
            on_segments([dict(s) for s in final_segs], r1)
    # how shaky each chosen piece is (same measure the camera choice used; dance events forgive
    # movement that follows the music) -> the panel can stabilize the shaky ones
    for sg in final_segs:
        try:
            sh = _parts[sg["cam"]].get("shake") if 0 <= sg["cam"] < len(_parts) else None
            m = (times >= sg["start"]) & (times < sg["end"])
            sg["shake"] = round(float(np.nanmean(sh[m])), 3) if sh is not None and m.any() else 0.0
        except Exception:  # noqa: BLE001
            sg["shake"] = 0.0

    total = sum(s["end"] - s["start"] for s in final_segs) or 1
    share = {project["cameras"][c]["name"]: round(100.0 * float(sum(s["end"] - s["start"] for s in final_segs
                                                                if s["cam"] == c)) / float(total), 1) for c in range(C)}
    # ---- checks: loudness, best mic, drop-outs, clipping, silences, moments, weak takes ------------
    prog("qc", 0.0)
    qc = {}
    try:
        from .audio_qc import run_qc
        qc = run_qc(project, t0, t1, preset.id, mix16, dead, bool(ex.get("silence")), False)
        qc["moments"] = _moments(mix16, t0, times, _parts, final_segs)
        qc["bad"] = _weak_takes(times, _parts, project)
    except Exception as e:  # noqa: BLE001
        log(f"  checks මඟ හැරියා: {e}")
    prog("qc", 1.0)
    lens = [s["end"] - s["start"] for s in final_segs]
    result = {
        "event": preset.id, "mode": preset.id, "range": [r0, r1], "fps": fps, "config": preset.to_dict(),
        "segments": final_segs, "dead": dead, "remove_dead": remove_dead, "trim": trim, **qc,
        "copy_edit": {k: ex.get(k) for k in ("silence", "flash_frames", "trim", "best_mic", "loudness")},
        "markers": [{"time": d["start"], "name": "CHECK - all cams weak"} for d in dead],
        "stats": {"cuts": max(0, len(final_segs) - 1), "avg_shot": round(float(np.mean(lens)), 2) if lens else 0,
                  "camera_share_pct": share, "dead_regions": len(dead),
                  "dead_seconds": round(sum(d["end"] - d["start"] for d in dead), 1),
                  "beats": int(len(beats)), "phrase_points": int(len(ph[0])),
                  "seconds": round(time.time() - t_start, 1)},
    }
    if ex.get("qc_markers"):
        # the new timeline carries the checks as markers (its times are mapped past trims / silences)
        for m in result.get("moments") or []:
            result["markers"].append({"time": m["time"], "name": f"Moment: {m.get('kind', '')}"})
        for b in result.get("bad") or []:
            result["markers"].append({"time": b["start"], "name": f"Weak take: {b.get('name', '')} ({b.get('why', '')})"})
        for c in (result.get("clipping") or [])[:40]:
            result["markers"].append({"time": c["start"], "name": f"Audio clipping: {c.get('name', '')}"})
        for g in (result.get("mic_gaps") or [])[:40]:
            result["markers"].append({"time": g["start"], "name": f"Mic drop-out: {g.get('name', '')}"})
    with open(os.path.join(out_dir, "cuts.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start_sec", "end_sec", "camera", "quality"])
        for s in final_segs:
            w.writerow([f"{s['start']:.3f}", f"{s['end']:.3f}",
                        project["cameras"][s["cam"]]["name"] if s["cam"] >= 0 else "-",
                        f"{s.get('quality', 0):.2f}"])
    np.savez_compressed(os.path.join(out_dir, "scores.npz"), times=times, scores=scores)
    prog("xml", 0.0)
    if write_xml:
        fname = re.sub(r'[\\/:*?"<>|]+', "_", xml_name).strip() if xml_name else f"AutoEdit_{preset.id}"
        xp = os.path.join(out_dir, f"{fname}.xml")
        build_xml(project, result, xp, layout=layout, remove_dead=remove_dead, seq_name=xml_name)
        result["xml"] = os.path.abspath(xp)
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    prog("xml", 1.0)
    st = result["stats"]
    log(f"ඉවරයි: cuts {st['cuts']}, සාමාන්‍ය shot {st['avg_shot']}s, cams {share}, "
        f"දුර්වල කොටස් {st['dead_regions']}, කාලය {st['seconds']}s")
    return result
