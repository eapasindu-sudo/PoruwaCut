"""Turn razored nest pieces into real multicam cuts inside a saved Premiere project (.prproj).

A .prproj is gzip-compressed XML. In an edit sequence whose V1 holds pieces of a nested
sequence (the multicam source), every piece is VideoClipTrackItem -> SubClip -> VideoClip, and
the VideoClip carries <IsMulticam>true</IsMulticam><SelectedTrackIndex>n</SelectedTrackIndex>
(n = video track of the nest = camera angle). We set those two values on every piece according
to the auto edit, and write the result as a NEW project file (the original is never modified).
"""
from __future__ import annotations

import copy
import gzip
import uuid
import xml.etree.ElementTree as ET

TICKS = 254016000000  # Premiere ticks per second


def _load(path: str) -> ET.Element:
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw)


def _save(root: ET.Element, path: str):
    body = ET.tostring(root, encoding="unicode")
    data = ('<?xml version="1.0" encoding="UTF-8" ?>\n' + body).encode("utf-8")
    with open(path, "wb") as f:
        f.write(gzip.compress(data))


def _index(root):
    objs = {}
    for el in root:
        k = el.get("ObjectID") or el.get("ObjectUID")
        if k:
            objs[k] = el
    return objs


def _ref(el, tag):
    e = el.find(".//" + tag)
    if e is None:
        return None
    return e.get("ObjectRef") or e.get("ObjectURef")


def _sequences(objs, name):
    return [k for k, e in objs.items() if e.tag == "Sequence" and (e.findtext("Name") or "") == name]


def _video_tracks(objs, seq_uid):
    seq = objs[seq_uid]
    out = []
    for tg in seq.iter("TrackGroup"):
        sec = tg.find("Second")
        grp = objs.get(sec.get("ObjectRef")) if sec is not None else None
        if grp is None or grp.tag != "VideoTrackGroup":
            continue
        for t in grp.iter("Track"):
            r = t.get("ObjectURef") or t.get("ObjectRef")
            if r and r in objs and objs[r].tag == "VideoClipTrack":
                out.append(r)
    return out


def _track_items(objs, track_uid):
    trk = objs[track_uid]
    items = []
    for ti in trk.iter("TrackItem"):
        r = ti.get("ObjectRef")
        if r and r in objs and objs[r].tag == "VideoClipTrackItem":
            items.append(r)
    return items


def _num(el, path, default=0):
    v = el.findtext(path)
    return int(v) if v not in (None, "") else default


def find_sequence_names(path: str) -> list[str]:
    objs = _index(_load(path))
    return sorted({e.findtext("Name") or "" for e in objs.values() if e.tag == "Sequence"})


def patch_multicam(src: str, dst: str, edit_seq: str, nest_seq: str, segments: list[dict],
                   cam_tracks: list[int] | None = None, nest_offset: float = 0.0) -> dict:
    """segments: [{start, end, cam}] in nest time (seconds). cam_tracks[cam] = nest video track index.

    nest_offset: nest time = edit-piece source time + nest_offset (0 when the nest starts at 0).
    Returns a small report.
    """
    root = _load(src)
    objs = _index(root)
    e_uids = _sequences(objs, edit_seq)
    n_uids = _sequences(objs, nest_seq)
    if not e_uids:
        raise ValueError(f"edit sequence '{edit_seq}' not found in project")
    if not n_uids:
        raise ValueError(f"multicam source sequence '{nest_seq}' not found in project")
    nest_uid = n_uids[0]
    max_id = max([int(k) for k in objs if k.isdigit()] + [0])
    used_clips: dict[str, int] = {}
    segs = sorted(segments, key=lambda s: s["start"])

    def angle_at(t):
        for s in segs:
            if s["start"] - 1e-3 <= t < s["end"] + 1e-3:
                c = s["cam"]
                return cam_tracks[c] if cam_tracks and c < len(cam_tracks) else c
        return None

    report = {"pieces": 0, "set": 0, "cloned": 0, "skipped": 0, "angles": {}}
    for e_uid in e_uids:
        tracks = _video_tracks(objs, e_uid)
        for tr in tracks:
            for it in _track_items(objs, tr):
                item = objs[it]
                sub = objs.get(_ref(item, "SubClip"))
                if sub is None:
                    continue
                clip_ref = sub.find("Clip")
                vclip = objs.get(clip_ref.get("ObjectRef")) if clip_ref is not None else None
                if vclip is None or vclip.tag != "VideoClip":
                    continue
                src_ref = _ref(vclip, "Source")
                source = objs.get(src_ref)
                if source is None or source.tag != "VideoSequenceSource":
                    continue
                if _ref(source, "Sequence") != nest_uid:
                    continue
                report["pieces"] += 1
                start = _num(item, ".//TrackItem/Start")
                end = _num(item, ".//TrackItem/End")
                inp = _num(vclip, "Clip/InPoint")
                mid_nest = (inp + (end - start) / 2) / TICKS + nest_offset
                ang = angle_at(mid_nest)
                if ang is None:
                    report["skipped"] += 1
                    continue
                vid = clip_ref.get("ObjectRef")
                if used_clips.get(vid):  # shared clip object -> give this piece its own copy
                    max_id += 1
                    new = copy.deepcopy(vclip)
                    new.set("ObjectID", str(max_id))
                    cid = new.find("Clip/ClipID")
                    if cid is not None:
                        cid.text = str(uuid.uuid4())
                    root.append(new)
                    objs[str(max_id)] = new
                    clip_ref.set("ObjectRef", str(max_id))
                    vclip = new
                    report["cloned"] += 1
                used_clips[vid] = 1
                cl = vclip.find("Clip")
                mc = cl.find("IsMulticam")
                if mc is None:
                    mc = ET.SubElement(cl, "IsMulticam")
                mc.text = "true"
                st = cl.find("SelectedTrackIndex")
                if st is None:
                    st = ET.SubElement(cl, "SelectedTrackIndex")
                st.text = str(int(ang))
                report["set"] += 1
                report["angles"][str(ang)] = report["angles"].get(str(ang), 0) + 1
    # the copy is a new project: give it its own GUID so it can be open next to the original
    for g in root.iter("MZ.Project.GUID"):
        g.text = str(uuid.uuid4())
    _save(root, dst)
    return report
