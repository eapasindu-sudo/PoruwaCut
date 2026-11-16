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


# ---------------------------------------------------------------------------------------------
# Cut the nest clip(s) into pieces inside the project file (works without Premiere's razor API)
# ---------------------------------------------------------------------------------------------
_SHARED = {"VideoSequenceSource", "AudioSequenceSource", "VideoMediaSource", "AudioMediaSource",
           "MasterClip", "Sequence", "Media", "Markers", "Link", "VideoStream", "AudioStream"}
_UUID_RE = None


def _closure(objs, k, acc):
    """Objects owned by one track item (ObjectRef only; shared sources are not followed)."""
    if k in acc or k not in objs:
        return
    el = objs[k]
    if acc and el.tag in _SHARED:
        return
    acc.append(k)
    for x in el.iter():
        r = x.get("ObjectRef")
        if r:
            _closure(objs, r, acc)


def _clone_group(root, objs, keys, next_id):
    import re
    global _UUID_RE
    if _UUID_RE is None:
        _UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    mapping = {}
    for k in keys:
        mapping[k] = str(next_id)
        next_id += 1
    for k in keys:
        new = copy.deepcopy(objs[k])
        new.set("ObjectID", mapping[k])
        for x in new.iter():
            r = x.get("ObjectRef")
            if r in mapping:
                x.set("ObjectRef", mapping[r])
            if x.tag in ("ClipID", "ID") and x.text and _UUID_RE.match(x.text.strip()):
                x.text = str(uuid.uuid4())
        root.append(new)
        objs[mapping[k]] = new
    return mapping, next_id


def _set_text(parent, tag, value, before=None):
    e = parent.find(tag)
    if e is None:
        e = ET.Element(tag)
        kids = list(parent)
        idx = len(kids)
        if before is not None:
            for i, kid in enumerate(kids):
                if kid.tag == before:
                    idx = i
                    break
        parent.insert(idx, e)
    e.text = str(value)
    return e


def _frame_ticks(objs, e_uid):
    seq = objs[e_uid]
    for tg in seq.iter("TrackGroup"):
        sec = tg.find("Second")
        grp = objs.get(sec.get("ObjectRef")) if sec is not None else None
        if grp is not None and grp.tag == "VideoTrackGroup":
            fr = grp.findtext(".//FrameRate")
            if fr:
                return int(fr)
    return TICKS // 25


def build_multicam_cuts(src: str, dst: str, edit_seq: str, nest_seq: str, segments: list[dict],
                        cam_tracks: list[int] | None = None) -> dict:
    """Split every nest clip on the edit sequence's video tracks at the cut points and make each
    piece a multicam clip showing the chosen angle. segments: [{start, end, cam}] in nest time."""
    root = _load(src)
    objs = _index(root)
    e_uids = _sequences(objs, edit_seq)
    n_uids = _sequences(objs, nest_seq)
    if not e_uids:
        raise ValueError(f"edit sequence '{edit_seq}' not found in project")
    if not n_uids:
        raise ValueError(f"multicam source sequence '{nest_seq}' not found in project")
    nest_uid = n_uids[0]
    next_id = max([int(k) for k in objs if k.isdigit()] + [0]) + 1
    segs = sorted(segments, key=lambda s: s["start"])
    report = {"items": 0, "pieces": 0, "multicam": 0, "angles": {}}

    def angle_of(cam):
        return cam_tracks[cam] if cam_tracks and cam < len(cam_tracks) else cam

    for e_uid in e_uids:
        fr = _frame_ticks(objs, e_uid)
        snap = lambda ticks: int(round(ticks / fr)) * fr
        for tr_uid in _video_tracks(objs, e_uid):
            trk = objs[tr_uid]
            items_el = trk.find(".//ClipItems/TrackItems")
            if items_el is None:
                continue
            new_refs = []
            for ti in list(items_el):
                k = ti.get("ObjectRef")
                item = objs.get(k)
                keep = [(k, None)]
                if item is None or item.tag != "VideoClipTrackItem":
                    new_refs.extend(keep)
                    continue
                sub = objs.get(_ref(item, "SubClip"))
                cref = sub.find("Clip") if sub is not None else None
                vclip = objs.get(cref.get("ObjectRef")) if cref is not None else None
                source = objs.get(_ref(vclip, "Source")) if vclip is not None else None
                if source is None or source.tag != "VideoSequenceSource" or _ref(source, "Sequence") != nest_uid:
                    new_refs.extend(keep)
                    continue
                report["items"] += 1
                a = _num(item, ".//TrackItem/Start")
                b = _num(item, ".//TrackItem/End")
                inp = _num(vclip, "Clip/InPoint")
                # piece boundaries in edit ticks: item edges + every segment edge inside the item
                to_edit = lambda nest_sec: snap(a + (nest_sec * TICKS - inp))
                pts = {a, b}
                for s in segs:
                    for t in (s["start"], s["end"]):
                        e_t = to_edit(t)
                        if a < e_t < b:
                            pts.add(e_t)
                pts = sorted(pts)
                group = []
                _closure(objs, k, group)
                orig_cl = vclip.find("Clip")
                orig_flags = {t: (orig_cl.findtext(t)) for t in ("IsMulticam", "SelectedTrackIndex")}
                pieces = []
                for i, (pa, pb) in enumerate(zip(pts[:-1], pts[1:])):
                    if pb - pa < fr:
                        continue
                    mid_nest = (inp + (pa - a) + (pb - pa) / 2) / TICKS
                    cam = next((s["cam"] for s in segs if s["start"] - 1e-3 <= mid_nest < s["end"] + 1e-3), None)
                    pieces.append((pa, pb, cam))
                for i, (pa, pb, cam) in enumerate(pieces):
                    if i == 0:
                        p_item = item  # the original keeps its id (links to audio stay valid)
                        p_k = k
                    else:
                        mapping, next_id = _clone_group(root, objs, group, next_id)
                        p_k = mapping[k]
                        p_item = objs[p_k]
                    p_sub = objs[_ref(p_item, "SubClip")]
                    p_clip = objs[p_sub.find("Clip").get("ObjectRef")]
                    ti_el = p_item.find(".//ClipTrackItem/TrackItem")
                    if pa:
                        _set_text(ti_el, "Start", pa, before="End")
                    elif ti_el.find("Start") is not None:
                        ti_el.remove(ti_el.find("Start"))
                    _set_text(ti_el, "End", pb)
                    cl = p_clip.find("Clip")
                    _set_text(cl, "InPoint", inp + (pa - a))
                    _set_text(cl, "OutPoint", inp + (pb - a))
                    if cam is not None:
                        ang = angle_of(cam)
                        _set_text(cl, "IsMulticam", "true")
                        _set_text(cl, "SelectedTrackIndex", int(ang))
                        report["multicam"] += 1
                        report["angles"][str(ang)] = report["angles"].get(str(ang), 0) + 1
                    else:  # outside the edited range: leave the piece as it was
                        for t, v in orig_flags.items():
                            x = cl.find(t)
                            if v is None and x is not None:
                                cl.remove(x)
                            elif v is not None:
                                _set_text(cl, t, v)
                    new_refs.append((p_k, pa))
                    report["pieces"] += 1
            # rebuild the track's item list in time order
            def start_of(ref):
                it = objs.get(ref[0])
                return _num(it, ".//TrackItem/Start") if it is not None else 0
            new_refs.sort(key=start_of)
            for old in list(items_el):
                items_el.remove(old)
            for i, (ref, _) in enumerate(new_refs):
                el = ET.SubElement(items_el, "TrackItem")
                el.set("Index", str(i))
                el.set("ObjectRef", ref)
    for g in root.iter("MZ.Project.GUID"):
        g.text = str(uuid.uuid4())
    _save(root, dst)
    return report
