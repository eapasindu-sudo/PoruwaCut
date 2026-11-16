"""End-to-end: project -> analysis -> scores -> camera choice -> result.json (+ Premiere XML)."""
from __future__ import annotations

import csv
import json
import os
import time

import numpy as np

from . import beats as beatmod
from .analyze import analyze_cameras
from .media import extract_audio
from .scoring import PRESETS, ModeConfig, score_cameras
from .switcher import make_blocks, block_scores, choose, to_segments, apply_dead_cuts
from .xml_export import build_xml


def master_audio(project: dict, t0: float, t1: float, rate: int) -> np.ndarray:
    buf = np.zeros(int((t1 - t0) * rate) + 1, np.float32)
    for cl in project["cameras"][project["master_cam"]]["clips"]:
        a, b = max(t0, cl["tl_start"]), min(t1, cl["tl_end"])
        if b - a < 0.5:
            continue
        x = extract_audio(cl["path"], rate, start=a - cl["offset"], duration=b - a)
        i = int((a - t0) * rate)
        n = min(len(x), len(buf) - i)
        buf[i:i + n] = x[:n]
    return buf


def make_config(mode: str, overrides: dict | None = None) -> ModeConfig:
    base = PRESETS[mode].to_dict()
    for k, v in (overrides or {}).items():
        if v is not None and k in base:
            base[k] = type(base[k])(v) if not isinstance(base[k], list) else v
    return ModeConfig(**base)


def run(project: dict, mode: str = "ceremony", out_dir: str = "out", overrides: dict | None = None,
        analysis_fps: float = 5.0, use_gpu: bool = True, workers: int = 2, layout: str = "stacked",
        remove_dead: bool = False, cache_dir: str | None = None, write_xml: bool = True, log=print) -> dict:
    t_start = time.time()
    os.makedirs(out_dir, exist_ok=True)
    # shared by every output folder next to it, so ceremony + dance runs reuse one analysis
    cache_dir = cache_dir or os.path.join(os.path.dirname(os.path.abspath(out_dir)), ".poruwacut_cache")
    cfg = make_config(mode, overrides)
    t0, t1 = project["range"]
    fps = float(project["fps"])
    log(f"Mode: {mode} | පරාසය {t0:.1f}s - {t1:.1f}s ({(t1 - t0) / 60:.1f} min) | cams {len(project['cameras'])}")

    beats = dbeats = np.zeros(0)
    tempos, beat_period = [], None
    if cfg.use_beats:
        log("Music beats හොයමින්...")
        audio = master_audio(project, t0, t1, beatmod.SR)
        b, tempos, strength = beatmod.detect_beats(audio)
        beats = b + t0
        dbeats = beatmod.downbeats(beats, strength)
        if len(b) > 2:
            beat_period = float(np.median(np.diff(b)))
            log(f"  beats {len(beats)}, tempo ~{60 / beat_period:.0f} BPM")
        else:
            log("  beats හමු නොවුණි - time grid එකට cut කරයි")

    log("Visual analysis (segment by segment)...")
    an = analyze_cameras(project["cameras"], t0, t1, analysis_fps, cache_dir, use_gpu, workers, log)
    dets = {c.get("detector", "?") for c in an["cams"]}
    log(f"  face detector: {', '.join(sorted(dets))}")

    log("Shots score කරමින් සහ cut තෝරමින්...")
    scores, parts = score_cameras(an, cfg, beat_period)
    bounds, is_down, (lmin, lmax) = make_blocks(t0, t1, cfg, beats, dbeats)
    bs = block_scores(scores, an["times"], bounds)
    choice = choose(bs, lmin, lmax, cfg, is_down)
    segs, dead = to_segments(choice, bounds, bs, cfg)

    # snap to sequence frames
    for s in segs:
        s["start"] = round(s["start"] * fps) / fps
        s["end"] = round(s["end"] * fps) / fps
    segs = [s for s in segs if s["end"] > s["start"]]
    final = apply_dead_cuts(segs, dead) if remove_dead else segs

    C = len(project["cameras"])
    total = sum(s["end"] - s["start"] for s in final) or 1
    share = {project["cameras"][c]["name"]: round(100 * sum(s["end"] - s["start"] for s in final if s["cam"] == c) / total, 1)
             for c in range(C)}
    lens = [s["end"] - s["start"] for s in final]
    result = {
        "mode": mode, "range": [t0, t1], "fps": fps, "config": cfg.to_dict(),
        "segments": final, "dead": dead, "remove_dead": remove_dead,
        "beats": [round(float(x), 3) for x in beats], "tempos": tempos,
        "markers": [],
        "stats": {"cuts": max(0, len(final) - 1), "avg_shot": round(float(np.mean(lens)), 2) if lens else 0,
                  "camera_share_pct": share, "dead_regions": len(dead),
                  "dead_seconds": round(sum(d["end"] - d["start"] for d in dead), 1),
                  "seconds": round(time.time() - t_start, 1)},
    }
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "cuts.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start_sec", "end_sec", "camera", "quality"])
        for s in final:
            w.writerow([f"{s['start']:.3f}", f"{s['end']:.3f}", project["cameras"][s["cam"]]["name"], f"{s['quality']:.2f}"])
    np.savez_compressed(os.path.join(out_dir, "scores.npz"), times=an["times"], scores=scores)

    if write_xml:
        xp = os.path.join(out_dir, f"AutoEdit_{mode}.xml")
        build_xml(project, result, xp, layout=layout, remove_dead=remove_dead)
        result["xml"] = os.path.abspath(xp)
        log(f"XML: {xp}")
    st = result["stats"]
    log(f"ඉවරයි: cuts {st['cuts']}, සාමාන්‍ය shot {st['avg_shot']}s, cams {share}, "
        f"නරක කොටස් {st['dead_regions']} ({st['dead_seconds']}s), කාලය {st['seconds']}s")
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result
