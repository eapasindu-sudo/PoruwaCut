"""Endora Highlights - phase 1: the best moments of a whole wedding + the structure of the song.

Input (from the DaVinci Resolve panel):
  clips: [{"id", "path", "name", "bin"}]          every video clip of the wedding (media pool)
  music: [{"path", "start", "in", "dur"}]         the song(s) the editor laid out on the highlight
                                                  timeline (timeline seconds / file seconds)
  fps:   timeline frame rate (for slow motion: a 50p clip in a 25p timeline = 2x slow)
  exclude_groups: group ids to leave out (e.g. the evening reception)

Everything runs offline. Video is skimmed on key frames only (GPU decode), 2 pictures a second,
with the same face / smile / hug / couple / people / shot size / sharpness checks as AutoWed.
Sound of every clip is sorted per second (speech, chanting, drums, music, applause).
"""
from __future__ import annotations

import base64
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import beats as beatmod
from .analyze import analyze_file
from .media import extract_audio, frame_jpeg, probe

SKIM_FPS = 2.0
KIND_NAMES = {"poruwa": "පෝරුව", "speech": "Speeches", "party": "Dance / party", "prep": "සූදානම / details",
              "outdoor": "Outdoor / couple shoot", "other": "වෙනත්"}


# ---------------------------------------------------------------- clips ---------------------------
def _creation_time(path: str) -> float | None:
    """Recording time (epoch seconds) from the file's tags, else the file's modified time."""
    import json
    import subprocess

    from .media import FFPROBE, NOWIN
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_entries",
                              "format_tags=creation_time:stream_tags=creation_time", path],
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, **NOWIN).stdout
        d = json.loads(out or "{}")
        tags = [(d.get("format") or {}).get("tags") or {}] + [s.get("tags") or {} for s in d.get("streams") or []]
        for tg in tags:
            v = tg.get("creation_time")
            if v:
                from datetime import datetime
                return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _clip_info(c: dict, tl_fps: float) -> dict | None:
    try:
        info = probe(c["path"])
    except Exception:  # noqa: BLE001
        return None
    if info.width <= 0 or info.duration < 1.0:
        return None
    fps = info.fps_float
    slow = fps / tl_fps if tl_fps and fps >= 1.9 * tl_fps else 1.0
    name = c.get("name") or os.path.basename(c["path"])
    return {**c, "dur": float(info.duration), "fps": round(fps, 3), "w": info.width, "h": info.height,
            "slowmo": round(slow, 2), "drone": bool(re.match(r"(?i)^dji_\d+", os.path.basename(c["path"]))),
            "time": _creation_time(c["path"]), "name": name}


# ---------------------------------------------------------------- per-frame scores ---------------
def _robust(x: np.ndarray):
    x = x[np.isfinite(x)]
    if x.size < 10:
        return 0.0, 1.0
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med))) * 1.4826
    return med, max(mad, 1e-6)


def _frame_scores(an: list[dict]) -> list[dict]:
    """Per frame: quality (picture) + emotion (people) -> score, compared across the whole wedding."""
    allv = lambda k: np.concatenate([a[k] for a in an if len(a["times"])]) if an else np.zeros(0)  # noqa: E731
    sh_m, sh_s = _robust(allv("sharp"))
    out = []
    for a in an:
        n = len(a["times"])
        if not n:
            out.append({"score": np.zeros(0), "tags": {}})
            continue
        f = lambda k, d=0.0: np.nan_to_num(np.asarray(a[k], np.float64), nan=d)  # noqa: E731
        sharp = np.clip((f("sharp", sh_m) - sh_m) / sh_s, -3, 2)
        expo = f("clip_hi") * 3 + f("clip_lo") * 2 + np.clip(np.abs(f("bright", 0.45) - 0.45) - 0.25, 0, 1) * 3
        move = np.abs(f("dx")) + np.abs(f("dy"))                    # camera movement per frame (picture widths)
        shake = np.clip((move - 0.04) / 0.06, 0, 2)                 # fast / jerky
        glide = np.clip(1 - np.abs(move - 0.012) / 0.012, 0, 1)    # a slow, smooth move looks cinematic
        faces = f("faces")
        smile, hug, couple = f("smile"), f("hug"), f("couple")
        scale = f("shot_scale", np.nan) if "shot_scale" in a else np.full(n, np.nan)
        away = f("away")
        walk = f("walk")
        people = f("people", 0.0) if "people" in a else (faces > 0).astype(float)
        detail = ((faces <= 0) & (people <= 0) & (sharp > 0.5) & (move < 0.02)).astype(float)
        q = 0.8 * sharp - 1.2 * expo - 1.0 * shake + 0.4 * glide - 1.5 * np.clip((walk - 0.15) / 0.15, 0, 1.5) - 0.5 * away
        emo = 1.6 * smile + 1.6 * hug + 1.2 * couple + 0.4 * np.clip(faces, 0, 2) + 0.3 * detail
        score = q + emo
        out.append({"score": score, "tags": {"smile": smile, "hug": hug, "couple": couple, "detail": detail,
                                            "glide": glide, "scale": scale, "faces": faces, "sharp": sharp}})
    return out


# ---------------------------------------------------------------- moments ------------------------
def _mine(ci: dict, times: np.ndarray, fs: dict, audio: np.ndarray | None, max_n: int):
    """Best non-overlapping windows (1.5..5 s) of one clip."""
    score = fs["score"]
    n = len(score)
    if n < 3:
        return []
    k = max(3, int(round(2.5 * SKIM_FPS)))                       # 2.5 s windows
    sm = np.convolve(score, np.ones(k) / k, mode="same")
    used = np.zeros(n, bool)
    out = []
    for _ in range(max_n):
        cand = np.where(used, -1e9, sm)
        i = int(np.argmax(cand))
        if cand[i] <= -1e8:
            break
        # grow while the picture stays good (at most 5 s), at least 1.5 s
        a = b = i
        floor = sm[i] - 0.8
        while a > 0 and not used[a - 1] and sm[a - 1] >= floor and (b - a + 1) < 5 * SKIM_FPS:
            a -= 1
        while b < n - 1 and not used[b + 1] and sm[b + 1] >= floor and (b - a + 1) < 5 * SKIM_FPS:
            b += 1
        if (b - a + 1) < 1.5 * SKIM_FPS:
            a, b = max(0, i - 2), min(n - 1, i + 2)
        gap = int(3 * SKIM_FPS)
        used[max(0, a - gap):b + gap + 1] = True
        if sm[i] < -0.3 and out:
            break
        t0, t1 = float(times[a]), float(times[b]) + 1.0 / SKIM_FPS
        tg = fs["tags"]
        tags = []
        for key, thr in (("smile", 0.5), ("hug", 0.5), ("couple", 0.5), ("detail", 0.5)):
            if tg[key][a:b + 1].max() >= thr:
                tags.append(key)
        sc = tg["scale"][a:b + 1]
        sc = sc[np.isfinite(sc)]
        size = None
        if sc.size:
            m = float(np.median(sc))
            size = "wide" if m < 0.3 else ("close" if m > 0.7 else "medium")
            tags.append(size)
        if tg["glide"][a:b + 1].mean() > 0.5:
            tags.append("glide")
        if ci["slowmo"] > 1:
            tags.append("slowmo")
        if ci["drone"]:
            tags.append("drone")
        sound = None
        if audio is not None and len(audio):
            s0, s1 = int(t0), int(math.ceil(t1))
            lab = audio[max(0, s0):max(s0 + 1, s1)]
            if len(lab):
                from .events_detect import KINDS
                vals, cnt = np.unique(lab, return_counts=True)
                sound = KINDS[int(vals[np.argmax(cnt)])]
                if sound in ("applause", "chant", "drums", "speech"):
                    tags.append(sound)
        bonus = 0.6 * ("slowmo" in tags) + 0.5 * ("applause" in tags) + 0.3 * ("drone" in tags) + 0.2 * ("glide" in tags)
        out.append({"clip": ci["id"], "path": ci["path"], "name": ci["name"], "start": round(t0, 2),
                    "end": round(min(t1, ci["dur"]), 2), "score": round(float(sm[i]) + bonus, 3),
                    "tags": tags, "size": size, "sound": sound})
    return out


# ---------------------------------------------------------------- groups (parts of the day) ------
def _groups(clips: list[dict], sounds: dict, gap_min: float = 25.0) -> list[dict]:
    """Bins first (the editor's own sorting); otherwise recording time, split at gaps > 25 min.
    Each group gets a guessed kind from its sound (chanting/drums = poruwa, talk = speeches ...)."""
    from .events_detect import KINDS
    by_bin = {}
    if sum(1 for c in clips if c.get("bin")) >= 0.8 * len(clips) and len({c.get("bin") for c in clips}) > 1:
        for c in clips:
            by_bin.setdefault(c.get("bin") or "?", []).append(c)
        parts = list(by_bin.values())
    else:
        cs = sorted(clips, key=lambda c: c.get("time") or 0)
        parts, cur, last = [], [], None
        for c in cs:
            t = c.get("time") or 0
            if cur and last is not None and t - last > gap_min * 60:
                parts.append(cur)
                cur = []
            cur.append(c)
            last = t + c["dur"]
        if cur:
            parts.append(cur)
    out = []
    for gi, p in enumerate(sorted(parts, key=lambda p: min((c.get("time") or 0) for c in p))):
        cnt = np.zeros(len(KINDS))
        for c in p:
            lab = sounds.get(c["id"])
            if lab is not None and len(lab):
                cnt += np.bincount(lab, minlength=len(KINDS))
        tot = max(1.0, cnt.sum())
        share = {KINDS[i]: cnt[i] / tot for i in range(len(KINDS))}
        if share["chant"] + share["drums"] > 0.12:
            kind = "poruwa"
        elif share["speech"] > 0.45:
            kind = "speech"
        elif share["music"] > 0.45:
            kind = "party"
        elif share["quiet"] + share["ambient"] > 0.7:
            kind = "prep"
        else:
            kind = "other"
        label = p[0].get("bin") if by_bin else None
        out.append({"id": f"g{gi + 1}", "label": label or f"{KIND_NAMES[kind]} {gi + 1}", "kind": kind,
                    "kind_name": KIND_NAMES[kind], "clips": [c["id"] for c in p],
                    "minutes": round(sum(c["dur"] for c in p) / 60, 1),
                    "start": min((c.get("time") or 0) for c in p)})
    return out


# ---------------------------------------------------------------- song ---------------------------
def song_structure(music: list[dict], log=print) -> dict | None:
    """Beats, bars, energy sections, hits and quiet spots of the song(s) on the highlight timeline."""
    if not music:
        return None
    sr = beatmod.SR
    end = max(m["start"] + m["dur"] for m in music)
    bed = np.zeros(int(end * sr) + sr, np.float32)
    for m in music:
        x = extract_audio(m["path"], rate=sr, start=float(m.get("in", 0.0)), duration=float(m["dur"]))
        a = int(m["start"] * sr)
        bed[a:a + len(x)] += x[:max(0, len(bed) - a)]
    b, strength, songs = beatmod.detect_beats_songs(bed)
    if len(b) < 8:
        return {"bpm": 0, "beats": [], "downbeats": [], "sections": [], "hits": [], "quiet": [], "end": end}
    db = beatmod.downbeats(b, strength)
    bars = np.concatenate([db, [end]]) if len(db) else np.array([0.0, end])
    # energy per bar (dB) and onset density
    e, o = [], []
    env = beatmod.onset_strength(bed)
    hop = len(bed) / max(1, len(env))
    for i in range(len(bars) - 1):
        a, z = int(bars[i] * sr), int(bars[i + 1] * sr)
        seg = bed[a:z]
        e.append(10 * np.log10(float(np.mean(seg ** 2)) + 1e-9) if len(seg) else -90)
        o.append(float(np.mean(env[int(a / hop):max(int(a / hop) + 1, int(z / hop))])) if len(env) else 0)
    e, o = np.array(e), np.array(o)
    feat = (e - e.mean()) / (e.std() + 1e-6) + 0.5 * (o - o.mean()) / (o.std() + 1e-6)
    sm = np.convolve(feat, np.ones(3) / 3, mode="same")
    # sections: cut where the smoothed energy jumps, on 4-bar phrases, at least 4 bars long
    cuts = [0]
    for i in range(4, len(sm) - 3):
        if i - cuts[-1] >= 4 and abs(sm[i:i + 2].mean() - sm[i - 2:i].mean()) > 0.6 and i % 2 == 0:
            cuts.append(i)
    cuts.append(len(sm))
    lv = []
    for a, z in zip(cuts[:-1], cuts[1:]):
        lv.append(float(feat[a:z].mean()))
    lo, hi = np.percentile(lv, [33, 66]) if len(lv) >= 3 else (min(lv), max(lv))
    sections = []
    for (a, z), v in zip(zip(cuts[:-1], cuts[1:]), lv):
        lab = "high" if v >= hi and v > lo else ("low" if v <= lo else "mid")
        sections.append({"start": round(float(bars[a]), 3), "end": round(float(bars[min(z, len(bars) - 1)]), 3),
                         "energy": lab, "level": round(v, 2), "bars": int(z - a)})
    if sections:
        top = max(range(len(sections)), key=lambda i: sections[i]["level"])
        sections[top]["energy"] = "peak"
    # hits: the first downbeat of a section that is much louder than the one before (drop / chorus in)
    hits = [s["start"] for i, s in enumerate(sections) if i and s["level"] - sections[i - 1]["level"] > 0.8]
    # quiet bars: room for a speech line over the music
    qthr = np.percentile(e, 30)
    quiet = [[round(float(bars[i]), 3), round(float(bars[i + 1]), 3)] for i in range(len(e)) if e[i] <= qthr]
    bpm = float(60.0 / np.median(np.diff(b))) if len(b) > 2 else 0.0
    log(f"  සින්දුව: ~{bpm:.0f} BPM · bars {len(bars) - 1} · කොටස් {len(sections)} "
        f"({', '.join(s['energy'] for s in sections)}) · hits {len(hits)}")
    return {"bpm": round(bpm, 1), "beats": [round(float(x), 3) for x in b], "downbeats": [round(float(x), 3) for x in db],
            "sections": sections, "hits": [round(h, 3) for h in hits], "quiet": quiet, "end": round(end, 3),
            "songs": [{"start": round(s_["start"], 2), "end": round(s_["end"], 2), "bpm": s_["bpm"]} for s_ in songs]}


# ---------------------------------------------------------------- main ---------------------------
def analyze(body: dict, cache_dir: str, use_gpu: bool = True, progress=None, log=print) -> dict:
    tl_fps = float(body.get("fps") or 25)
    raw = [c for c in body.get("clips") or [] if c.get("path") and os.path.exists(c["path"])]
    log(f"Highlights: clips {len(raw)} කියවනවා…")
    with ThreadPoolExecutor(max_workers=6) as ex:
        clips = [c for c in ex.map(lambda c: _clip_info(c, tl_fps), raw) if c]
    total = sum(c["dur"] for c in clips) or 1.0
    log(f"  video clips {len(clips)} · footage {total / 3600:.1f}h · slow-mo clips {sum(c['slowmo'] > 1 for c in clips)}")
    done = [0.0]

    def tick(sec):
        done[0] += sec
        if progress:
            progress("analyze", 0.05 + 0.85 * min(1.0, done[0] / total), done[0] / total)

    # sound of every clip, per second (cheap): speech / chanting / drums / music / applause
    from .events_detect import classify

    def sound(c):
        try:
            x = extract_audio(c["path"], rate=16000)
            return c["id"], (classify(x, 16000) if len(x) > 16000 * 2 else np.zeros(0, int))
        except Exception:  # noqa: BLE001
            return c["id"], np.zeros(0, int)
    with ThreadPoolExecutor(max_workers=4) as ex:
        sounds = dict(ex.map(sound, clips))
    if progress:
        progress("analyze", 0.05, 0.0)

    def pic(c):
        a = analyze_file(c["path"], 0.0, c["dur"], SKIM_FPS, cache_dir, use_gpu=use_gpu, skim=True,
                         tick=lambda s: tick(s))
        return a
    workers = 3 if (os.cpu_count() or 4) >= 8 else 2
    with ThreadPoolExecutor(max_workers=workers) as ex:
        an = list(ex.map(pic, clips))

    groups = _groups(clips, sounds)
    excl = set(body.get("exclude_groups") or [])
    skip = {cid for g in groups if g["id"] in excl for cid in g["clips"]}
    fsc = _frame_scores(an)
    moments = []
    for c, a, fs in zip(clips, an, fsc):
        if c["id"] in skip:
            continue
        per = max(1, min(12, int(c["dur"] / 20) + 1))
        moments += _mine(c, np.asarray(a["times"]), fs, sounds.get(c["id"]), per)
    gid = {cid: g["id"] for g in groups for cid in g["clips"]}
    for m in moments:
        m["group"] = gid.get(m["clip"])
    moments.sort(key=lambda m: -m["score"])
    for i, m in enumerate(moments):
        m["id"] = f"m{i + 1}"
    if progress:
        progress("song", 0.92, 0.0)
    song = None
    try:
        song = song_structure(body.get("music") or [], log=log)
    except Exception as e:  # noqa: BLE001
        log(f"  සින්දුව analyze කරන්න බැරි වුණා: {e}")
    # pictures for the best ones
    top = moments[:int(body.get("thumbs", 90))]

    def th(m):
        b = frame_jpeg(m["path"], (m["start"] + m["end"]) / 2, width=240)
        return m["id"], ("data:image/jpeg;base64," + base64.b64encode(b).decode()) if b else None
    with ThreadPoolExecutor(max_workers=4) as ex:
        thumbs = {k: v for k, v in ex.map(th, top) if v}
    from collections import Counter
    tc = Counter(t for m in moments for t in m["tags"])
    log(f"  moments {len(moments)} · " + ", ".join(f"{k} {v}" for k, v in tc.most_common(8)))
    log("  කොටස්: " + " · ".join(f"{g['label']} ({g['kind_name']}, {g['minutes']} min)" for g in groups))
    return {"clips": [{k: c[k] for k in ("id", "name", "path", "dur", "fps", "slowmo", "drone", "bin", "w", "h", "time") if k in c}
                      for c in clips],
            "groups": groups, "moments": moments, "song": song, "thumbs": thumbs, "fps": tl_fps}
