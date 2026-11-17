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
        xml_name: str | None = None) -> dict:
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
    fps = float(project["fps"])
    stages = {"audio": (0.0, 0.08), "analysis": (0.08, 0.97), "cuts": (0.97, 0.99), "xml": (0.99, 1.0)}

    def prog(stage, f):
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
    del audio

    # ---- window by window ---------------------------------------------------------------------
    log("කැමරා visuals analyze කරමින් (window එකෙන් window එක, cuts live)...")
    C = len(project["cameras"])
    acc_times: list = []
    acc = [{m: [] for m in METRICS} for _ in range(C)]
    committed: list[dict] = []
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
    # how shaky each chosen piece is (same measure the camera choice used; dance events forgive
    # movement that follows the music) -> the panel can stabilize the shaky ones
    for sg in final_segs:
        try:
            sh = _parts[sg["cam"]].get("shake") if sg["cam"] < len(_parts) else None
            m = (times >= sg["start"]) & (times < sg["end"])
            sg["shake"] = round(float(np.nanmean(sh[m])), 3) if sh is not None and m.any() else 0.0
        except Exception:  # noqa: BLE001
            sg["shake"] = 0.0

    total = sum(s["end"] - s["start"] for s in final_segs) or 1
    share = {project["cameras"][c]["name"]: round(100 * sum(s["end"] - s["start"] for s in final_segs
                                                          if s["cam"] == c) / total, 1) for c in range(C)}
    lens = [s["end"] - s["start"] for s in final_segs]
    result = {
        "event": preset.id, "mode": preset.id, "range": [t0, t1], "fps": fps, "config": preset.to_dict(),
        "segments": final_segs, "dead": dead, "remove_dead": remove_dead,
        "markers": [{"time": d["start"], "name": "CHECK - all cams weak"} for d in dead],
        "stats": {"cuts": max(0, len(final_segs) - 1), "avg_shot": round(float(np.mean(lens)), 2) if lens else 0,
                  "camera_share_pct": share, "dead_regions": len(dead),
                  "dead_seconds": round(sum(d["end"] - d["start"] for d in dead), 1),
                  "beats": int(len(beats)), "phrase_points": int(len(ph[0])),
                  "seconds": round(time.time() - t_start, 1)},
    }
    with open(os.path.join(out_dir, "cuts.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start_sec", "end_sec", "camera", "quality"])
        for s in final_segs:
            w.writerow([f"{s['start']:.3f}", f"{s['end']:.3f}", project["cameras"][s["cam"]]["name"],
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
