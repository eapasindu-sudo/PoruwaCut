"""Project description shared by the CLI and the Premiere panel.

project.json
{
  "name": "Nimal & Kumari - Poruwa",
  "fps": 25,                      # sequence frame rate (optional, taken from master cam)
  "range": [0, 2700],             # optional master-time range to analyse (seconds)
  "master_cam": 0,                # camera that carries the DJI mic audio
  "cameras": [
    {"name": "Cam 1 (DJI audio)", "clips": [{"path": "D:/.../C0001.MP4", "offset": 0.0}]},
    {"name": "Cam 2",             "clips": [{"path": "D:/.../MVI_0001.MP4", "offset": -12.48}]}
  ]
}
offset = master_time - file_time.  A clip may also carry "tl_start"/"tl_end" (the part of the
master timeline it covers, e.g. when it comes from a Premiere sequence) and "src_in".
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess

from .media import probe, FFPROBE, NOWIN
from .sync import find_offset
from .media import extract_audio

VIDEO_EXT = (".mp4", ".mov", ".mts", ".m2ts", ".mxf", ".avi", ".mkv", ".m4v")


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    return normalise(p)


def save(p: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2, ensure_ascii=False)


def normalise(p: dict) -> dict:
    for cam in p["cameras"]:
        for cl in cam["clips"]:
            if "duration" not in cl:
                cl["duration"] = probe(cl["path"]).duration
            cl.setdefault("src_in", 0.0)
            cl.setdefault("tl_start", cl["offset"] + cl["src_in"])
            cl.setdefault("tl_end", cl["offset"] + cl["duration"])
    for trk in p.get("audio_tracks") or []:
        for cl in trk:
            if "duration" not in cl:
                cl["duration"] = probe(cl["path"]).duration
            cl.setdefault("src_in", 0.0)
            cl.setdefault("tl_start", cl["offset"] + cl["src_in"])
            cl.setdefault("tl_end", cl["offset"] + cl["duration"])
    p.setdefault("master_cam", 0)
    if not p.get("fps"):
        first = p["cameras"][p["master_cam"]]["clips"][0]["path"]
        p["fps"] = float(probe(first).fps)
    if not p.get("range"):
        starts = [cl["tl_start"] for c in p["cameras"] for cl in c["clips"]]
        ends = [cl["tl_end"] for c in p["cameras"] for cl in c["clips"]]
        mc = p["cameras"][p["master_cam"]]["clips"]
        # default: the span where the master (audio) camera is recording
        p["range"] = [min(cl["tl_start"] for cl in mc), max(cl["tl_end"] for cl in mc)]
        p["range"][0] = max(p["range"][0], min(starts))
        p["range"][1] = min(p["range"][1], max(ends))
    return p


def _creation_time(path: str) -> float | None:
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format_tags=creation_time",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, **NOWIN).stdout.strip()
        if not out:
            return None
        from datetime import datetime
        return datetime.fromisoformat(out.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _list_videos(folder_or_file: str) -> list[str]:
    if os.path.isfile(folder_or_file):
        return [folder_or_file]
    files = [f for f in glob.glob(os.path.join(folder_or_file, "*")) if f.lower().endswith(VIDEO_EXT)]
    return sorted(files, key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)])


def from_folders(cam_sources: list[str], master_cam: int = 0, name: str = "Wedding",
                 log=print) -> dict:
    """Build a project from one folder (or file) per camera and sync everything by audio.

    The master camera's files are laid out by their recording time (or back to back);
    every other file is matched against the master file it overlaps best.
    """
    cams = []
    master_files = _list_videos(cam_sources[master_cam])
    if not master_files:
        raise ValueError(f"master camera folder has no videos: {cam_sources[master_cam]}")
    log("Master (DJI audio) camera files ලබා ගනිමින්...")
    mclips, pos, t_first = [], 0.0, None
    for f in master_files:
        d = probe(f).duration
        ct = _creation_time(f)
        if ct is not None:
            if t_first is None:
                t_first = ct
            start = max(pos, ct - t_first) if ct - t_first < pos + 3600 else pos
        else:
            start = pos
        mclips.append({"path": f, "offset": start, "duration": d})
        pos = start + d
    rate = 8000
    m_audio = [extract_audio(c["path"], rate) for c in mclips]

    for ci, src in enumerate(cam_sources):
        if ci == master_cam:
            cams.append({"name": f"Cam {ci + 1} (DJI audio)", "clips": mclips})
            continue
        clips = []
        for f in _list_videos(src):
            other = extract_audio(f, rate)
            best = None
            for mc, ma in zip(mclips, m_audio):
                if len(other) == 0 or len(ma) == 0:
                    continue
                off, conf = find_offset(ma, other, rate)
                if best is None or conf > best[1]:
                    best = (mc["offset"] + off, conf)
            if best is None:
                log(f"  ! {os.path.basename(f)}: sync කළ නොහැක (audio නැත)")
                continue
            flag = "" if best[1] > 1.0 else "  <- විශ්වාසය අඩුයි, Premiere එකේ check කරන්න"
            log(f"  cam{ci + 1} {os.path.basename(f)}: offset {best[0]:+.2f}s conf {best[1]:.2f}{flag}")
            clips.append({"path": f, "offset": best[0], "sync_confidence": best[1]})
        cams.append({"name": f"Cam {ci + 1}", "clips": clips})
    return normalise({"name": name, "master_cam": master_cam, "cameras": cams})
