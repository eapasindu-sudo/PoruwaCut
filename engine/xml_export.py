"""Write the auto edit as Final Cut Pro 7 XML (File > Import in Premiere Pro).

Layout ("stacked", default): V1..Vn = camera 1..n, every track razored at every cut and only the
chosen camera's piece is enabled. In Premiere select a piece and press Shift+E to switch angle,
just like a multicam edit. "flat" layout puts the whole edit on V1.

Audio: the master camera's audio channels (DJI mic 1, DJI mic 2) go to A1, A2... continuously.
"""
from __future__ import annotations

import os
from urllib.parse import quote
from xml.sax.saxutils import escape

from .media import probe


def _rate(fps: float) -> str:
    tb = round(fps)
    ntsc = "TRUE" if abs(fps - tb) > 0.005 else "FALSE"
    return f"<rate><timebase>{tb}</timebase><ntsc>{ntsc}</ntsc></rate>"


def _pathurl(p: str) -> str:
    p = os.path.abspath(p).replace("\\", "/") if not (len(p) > 1 and p[1] == ":") else p.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://localhost" + quote(p, safe="/:")


class _Files:
    def __init__(self):
        self.ids, self.info = {}, {}

    def ref(self, path: str) -> str:
        if path in self.ids:
            return f'<file id="{self.ids[path]}"/>'
        fid = f"file-{len(self.ids) + 1}"
        self.ids[path] = fid
        mi = probe(path)
        self.info[path] = mi
        fr = float(mi.fps)
        dur = int(round(mi.duration * fr))
        video = ""
        if mi.width:
            video = (f"<video><samplecharacteristics>{_rate(fr)}<width>{mi.width}</width>"
                     f"<height>{mi.height}</height></samplecharacteristics></video>")
        audio = ""
        if mi.audio_channels:
            audio = (f"<audio><samplecharacteristics><depth>16</depth><samplerate>{mi.audio_rate}</samplerate>"
                     f"</samplecharacteristics><channelcount>{mi.audio_channels}</channelcount></audio>")
        return (f'<file id="{fid}"><name>{escape(os.path.basename(path))}</name>'
                f"<pathurl>{_pathurl(path)}</pathurl>{_rate(fr)}<duration>{dur}</duration>"
                f"<media>{video}{audio}</media></file>")


def _keep_intervals(t0, t1, dead, remove_dead):
    if not remove_dead or not dead:
        return [(t0, t1)]
    out, a = [], t0
    for d in sorted(dead, key=lambda d: d["start"]):
        if d["start"] > a:
            out.append((a, min(d["start"], t1)))
        a = max(a, d["end"])
    if a < t1:
        out.append((a, t1))
    return [(x, y) for x, y in out if y - x > 0.05]


def build_xml(project: dict, result: dict, out_path: str, layout: str = "stacked",
              remove_dead: bool = False, seq_name: str | None = None) -> str:
    fps = float(project["fps"])
    t0, t1 = result["range"]
    segs = result["segments"]
    dead = result.get("dead", [])
    keep = _keep_intervals(t0, t1, dead, remove_dead)

    # master-time -> sequence-frame mapping (ripple over removed parts)
    def seq_frame(t):
        acc = 0.0
        for a, b in keep:
            if t <= a:
                break
            acc += min(t, b) - a
        return int(round(acc * fps))

    def clip_at(cam, a, b):
        """Clip pieces of camera `cam` that cover master interval [a,b)."""
        res = []
        for cl in project["cameras"][cam]["clips"]:
            x, y = max(a, cl["tl_start"]), min(b, cl["tl_end"])
            if y - x > 0.5 / fps:
                res.append((cl, x, y))
        return res

    # all cut points inside kept intervals
    cuts = sorted({t0, t1} | {s["start"] for s in segs} | {s["end"] for s in segs}
                  | {a for a, _ in keep} | {b for _, b in keep})
    pieces = []  # (a, b, chosen_cam)
    for a, b in zip(cuts[:-1], cuts[1:]):
        if b - a < 0.5 / fps or not any(ka <= a and b <= kb + 1e-6 for ka, kb in keep):
            continue
        mid = (a + b) / 2
        chosen = next((s["cam"] for s in segs if s["start"] <= mid < s["end"]), None)
        pieces.append((a, b, chosen))

    files = _Files()
    cid = [0]

    def clipitem(cl, a, b, enabled=True, audio_track=None, cam=None):
        cid[0] += 1
        mi = files.info.get(cl["path"]) or probe(cl["path"])
        st, en = seq_frame(a), seq_frame(b)
        if en <= st:
            return ""
        src_in = int(round((a - cl["offset"]) * fps))
        src_out = src_in + (en - st)
        fdur = int(round(mi.duration * fps))
        name = escape(os.path.basename(cl["path"]))
        x = (f'<clipitem id="clipitem-{cid[0]}"><name>{name}</name>'
             f'<enabled>{"TRUE" if enabled else "FALSE"}</enabled><duration>{fdur}</duration>{_rate(fps)}'
             f"<start>{st}</start><end>{en}</end><in>{src_in}</in><out>{src_out}</out>{files.ref(cl['path'])}")
        if cam is not None:
            from .prproj import LABEL_NAMES, label_index_for_cam
            x += f"<labels><label2>{LABEL_NAMES[label_index_for_cam(cam)]}</label2></labels>"
        if audio_track is not None:
            x += f"<sourcetrack><mediatype>audio</mediatype><trackindex>{audio_track}</trackindex></sourcetrack>"
        return x + "</clipitem>"

    C = len(project["cameras"])
    vtracks = []
    if layout == "flat":
        items = []
        for a, b, c in pieces:
            if c is None:
                continue
            for cl, x, y in clip_at(c, a, b):
                items.append(clipitem(cl, x, y, cam=c))
        vtracks.append("".join(items))
    else:
        for c in range(C):
            items = []
            for a, b, ch in pieces:
                for cl, x, y in clip_at(c, a, b):
                    items.append(clipitem(cl, x, y, enabled=(ch == c), cam=c))
            vtracks.append("".join(items))

    def pieces_of(clips, a, b):
        res = []
        for cl in clips:
            x, y = max(a, cl["tl_start"]), min(b, cl["tl_end"])
            if y - x > 0.5 / fps:
                res.append((cl, x, y))
        return res

    m = project["master_cam"]
    mclips = project["cameras"][m]["clips"]
    atracks = []
    if project.get("audio_tracks"):
        # audio layout copied from the user's Premiere sequence (e.g. DJI mic 1 on A1, mic 2 on A2).
        # The n-th track that uses a file gets that file's n-th audio channel.
        seen = {}
        for trk in project["audio_tracks"]:
            items = []
            used_here = set()
            for cl in trk:
                if cl["path"] not in used_here:
                    used_here.add(cl["path"])
                    seen[cl["path"]] = seen.get(cl["path"], 0) + 1
                ch = cl.get("channel") or seen[cl["path"]]
                for a, b in keep:
                    for c2, x, y in pieces_of([cl], a, b):
                        items.append(clipitem(c2, x, y, audio_track=ch))
            atracks.append("".join(items))
    else:
        # default: master camera channels (DJI mic 1, DJI mic 2) on their own tracks
        nch = max([probe(cl["path"]).audio_channels for cl in mclips] + [0])
        for ch in range(1, max(nch, 0) + 1):
            items = []
            for a, b in keep:
                for cl, x, y in clip_at(m, a, b):
                    items.append(clipitem(cl, x, y, audio_track=ch))
            atracks.append("".join(items))

    total = seq_frame(t1)
    first = probe(mclips[0]["path"])
    markers = []
    for d in dead:
        label = "REMOVED - bad footage" if remove_dead else "CHECK - all cams weak here"
        if not remove_dead:
            markers.append(f"<marker><name>{label}</name><comment>PoruwaCut</comment>"
                           f"<in>{seq_frame(d['start'])}</in><out>{seq_frame(d['end'])}</out></marker>")
    for mk in result.get("markers", []):
        markers.append(f"<marker><name>{escape(mk['name'])}</name><comment>PoruwaCut</comment>"
                       f"<in>{seq_frame(mk['time'])}</in><out>-1</out></marker>")

    name = escape(seq_name or f"{project.get('name', 'Wedding')} - AutoEdit {result['mode']}")
    vt = "".join(f"<track>{t}</track>" for t in vtracks)
    at = "".join(f"<track>{t}</track>" for t in atracks)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
           f'<sequence id="sequence-1"><name>{name}</name><duration>{total}</duration>{_rate(fps)}'
           f"<timecode>{_rate(fps)}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat></timecode>"
           f"<media><video><format><samplecharacteristics>{_rate(fps)}<width>{first.width}</width>"
           f"<height>{first.height}</height><pixelaspectratio>square</pixelaspectratio>"
           f"<fielddominance>none</fielddominance></samplecharacteristics></format>{vt}</video>"
           f"<audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics><depth>16</depth>"
           f"<samplerate>48000</samplerate></samplecharacteristics></format>{at}</audio></media>"
           f"{''.join(markers)}</sequence></xmeml>\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    return out_path
