"""PoruwaCut command line.

  python -m engine sync  --cams D:/w/camA D:/w/camB D:/w/camC --master 1 -o project.json
  python -m engine edit  -p project.json --mode ceremony --start 12:30 --end 48:00 -o out_poruwa
  python -m engine edit  -p project.json --mode dance --start 1:55:00 --end 2:20:00 -o out_dance
  python -m engine auto  --cams D:/w/camA D:/w/camB --master 1 --mode ceremony -o out
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import project as proj
from .pipeline import run


def log(msg):
    print(msg, flush=True)


def parse_time(s: str | None):
    if s is None:
        return None
    parts = [float(x) for x in str(s).split(":")]
    v = 0.0
    for p in parts:
        v = v * 60 + p
    return v


def _edit_args(ap):
    ap.add_argument("--mode", choices=["ceremony", "dance"], default="ceremony",
                    help="ceremony = පෝරුව/කතා, dance = dance floor (beats මත cut)")
    ap.add_argument("--start", help="master timeline එකේ ආරම්භය (ss / mm:ss / hh:mm:ss)")
    ap.add_argument("--end", help="අවසානය")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--layout", choices=["stacked", "flat"], default="stacked",
                    help="stacked = කැමරා track වෙන වෙනම (Shift+E මාරු කරන්න), flat = V1 එකේ විතරක්")
    ap.add_argument("--remove-dead", action="store_true", help="කැමරා සියල්ල නරක කොටස් සම්පූර්ණයෙන්ම කපන්න")
    ap.add_argument("--cpu", action="store_true", help="GPU (NVDEC) භාවිතා නොකරන්න")
    ap.add_argument("--fps", type=float, default=5.0, help="analysis frames per second (default 5)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--min-shot", type=float)
    ap.add_argument("--max-shot", type=float)
    ap.add_argument("--min-beats", type=int)
    ap.add_argument("--max-beats", type=int)
    ap.add_argument("--switch-cost", type=float)
    ap.add_argument("--stay-cost", type=float, help="එකම කැමරාවේ උපරිම දිග පැනීමේ දඬුවම (වැඩි = variety වැඩි)")
    ap.add_argument("--bias", help="කැමරාවකට කැමැත්ත: උදා '0,0.3,0' (cam2 ට වැඩි ප්‍රමුඛත්වය)")
    ap.add_argument("--no-xml", action="store_true")


def _do_edit(p, a):
    if a.start or a.end:
        r = list(p["range"])
        if a.start:
            r[0] = parse_time(a.start)
        if a.end:
            r[1] = parse_time(a.end)
        p["range"] = r
    ov = {"min_shot": a.min_shot, "max_shot": a.max_shot, "min_beats": a.min_beats,
          "max_beats": a.max_beats, "switch_cost": a.switch_cost, "stay_cost": a.stay_cost}
    if a.bias:
        ov["cam_bias"] = [float(x) for x in a.bias.split(",")]
    last = [-1]

    def progress(stage, overall, _f):
        pct = int(overall * 100)
        if pct != last[0]:
            last[0] = pct
            print(f"\r  {stage:9s} {pct:3d}%", end="\n" if pct >= 100 else "", flush=True)

    res = run(p, a.mode, a.out, ov, a.fps, not a.cpu, a.workers, a.layout, a.remove_dead,
              write_xml=not a.no_xml, log=log, progress=progress)
    log("RESULT " + json.dumps({"xml": res.get("xml"), "result": os.path.abspath(os.path.join(a.out, "result.json"))}))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="poruwacut", description="Sri Lankan wedding multicam auto-edit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="කැමරා folders audio මගින් sync කර project.json හදන්න")
    s.add_argument("--cams", nargs="+", required=True, help="එක් කැමරාවකට එක folder එකක් (හෝ file)")
    s.add_argument("--master", type=int, default=1, help="DJI mic audio තියෙන කැමරාව (1 සිට)")
    s.add_argument("--name", default="Wedding")
    s.add_argument("-o", "--out", default="project.json")

    e = sub.add_parser("edit", help="project.json එකෙන් auto edit එක හදන්න")
    e.add_argument("-p", "--project", required=True)
    _edit_args(e)

    au = sub.add_parser("auto", help="sync + edit එකවර")
    au.add_argument("--cams", nargs="+", required=True)
    au.add_argument("--master", type=int, default=1)
    au.add_argument("--name", default="Wedding")
    _edit_args(au)

    sv = sub.add_parser("serve", help="Premiere panel / browser UI සඳහා engine server එක")
    sv.add_argument("--port", type=int, default=7788)

    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from .server import serve
        serve(port=a.port)
    elif a.cmd == "sync":
        p = proj.from_folders(a.cams, a.master - 1, a.name, log)
        proj.save(p, a.out)
        log(f"project saved: {a.out}")
    elif a.cmd == "edit":
        _do_edit(proj.load(a.project), a)
    elif a.cmd == "auto":
        p = proj.from_folders(a.cams, a.master - 1, a.name, log)
        os.makedirs(a.out, exist_ok=True)
        proj.save(p, os.path.join(a.out, "project.json"))
        _do_edit(p, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
