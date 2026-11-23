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


COUPLE_EVENTS = {"auto", "groom_welcome", "bride_welcome", "poruwa", "pahana", "kiri_kalaya", "registration", "cake_cutting", "first_dance",
                 "table_visit", "surprise_dance", "speeches", "church_mass"}


def _jump_cuts(times, analysis, segs):
    """Cuts between two cameras that frame the subject almost the same way (same face size and place):
    they look like a jump cut. Returned as warnings for markers."""
    out = []
    cams = analysis["cams"]
    tt = np.asarray(times)
    for a, b in zip(segs, segs[1:]):
        if a["cam"] == b["cam"] or a["cam"] < 0 or b["cam"] < 0:
            continue
        t = b["start"]
        i = int(np.clip(np.searchsorted(tt, t), 0, len(tt) - 1))
        lo, hi = max(0, i - 3), min(len(tt), i + 4)

        def look(c):
            fa = np.asarray(cams[c]["face_area"][lo:hi], np.float64)
            fc = np.asarray(cams[c]["face_central"][lo:hi], np.float64)
            fa, fc = fa[np.isfinite(fa)], fc[np.isfinite(fc)]
            return (float(np.sqrt(np.median(fa))) if fa.size else 0.0, float(np.median(fc)) if fc.size else 0.0)

        (fa, ca), (fb, cb) = look(a["cam"]), look(b["cam"])
        if fa > 0.05 and fb > 0.05 and abs(fa - fb) < 0.025 and abs(ca - cb) < 0.15:
            out.append({"time": float(t), "why": "framing එක සමානයි"})
            continue

        def shot(c):   # AI shot size + people count (also when nobody faces the camera)
            if "shot_scale" not in cams[c]:
                return None
            s_ = np.asarray(cams[c]["shot_scale"][lo:hi], np.float64)
            p_ = np.asarray(cams[c]["people"][lo:hi], np.float64)
            s_, p_ = s_[np.isfinite(s_)], p_[np.isfinite(p_)]
            return (float(np.median(s_)), float(np.median(p_))) if s_.size and p_.size else None
        sa, sb = shot(a["cam"]), shot(b["cam"])
        if sa and sb and abs(sa[0] - sb[0]) < 0.08 and sa[1] == sb[1] and 0 < sa[1] <= 3 and fa <= 0.05 and fb <= 0.05:
            out.append({"time": float(t), "why": "shot size එක සමානයි"})
    return out


RITUAL_MIN = {"chant": 8.0, "drums": 4.0, "applause": 2.0, "quiet": 5.0}
RITUAL_LABEL = {"chant": "ගාථා / පිරිත්", "drums": "බෙර / හේවිසි", "applause": "අත්පොළසන්",
                "quiet": "නිහඬ මොහොත"}


def _color_match(analysis, segs, project):
    """Per-camera RGB gains that bring each camera to the main camera's colour.

    Skin tones of the biggest face are the reference both cameras share (the couple, the speaker);
    without enough faces the picture centre is used, white balance only and gentler. Gains are
    damped and limited so a wrong guess can never ruin a shot. Applied in Premiere as Color Balance (RGB).
    """
    cams = analysis["cams"]
    C = len(cams)
    used = {c: float(sum(s["end"] - s["start"] for s in segs if s["cam"] == c)) for c in range(C)}
    if C < 2 or not any(used.values()):
        return None
    ref = max(used, key=used.get)

    def med(c, src):
        a = cams[c]
        ok = np.isfinite(a[src + "_b"]) & np.isfinite(a[src + "_g"]) & np.isfinite(a[src + "_r"])
        if "clip_hi" in a:
            ok &= ~(np.nan_to_num(a["clip_hi"], nan=1.0) > 0.08)
        n = int(ok.sum())
        if n == 0:
            return None, 0
        return np.array([np.median(a[src + "_r"][ok]), np.median(a[src + "_g"][ok]), np.median(a[src + "_b"][ok])]), n

    out = []
    for c in range(C):
        if c == ref or used[c] < 5.0:
            continue
        src = None
        for cand, need in (("skin", 25), ("img", 60)):
            mr, nr = med(ref, cand)
            mc, nc = med(c, cand)
            if mr is not None and mc is not None and min(nr, nc) >= need and mc.min() > 8:
                src = cand
                break
        if src is None:
            continue
        g = mr / mc
        if src == "img":                       # different framing: white balance only, half strength
            g = g / g.mean()
            g = 1.0 + 0.5 * (g - 1.0)
        else:
            g = 1.0 + 0.75 * (g - 1.0)
        g = np.clip(g, 0.8, 1.25)
        if np.abs(g - 1.0).max() < 0.03:
            continue
        out.append({"cam": c, "name": project["cameras"][c]["name"], "track": project["cameras"][c].get("track"),
                    "rgb": [round(float(v) * 100, 1) for v in g], "source": src, "samples": int(min(nr, nc))})
    return {"ref": ref, "ref_name": project["cameras"][ref]["name"], "cams": out}


def _thumbs(project, segs, result, limit: int = 48):
    """Small pictures for the panel's review list: {key: data-URI}. Key = "<list>:<index>"."""
    import base64
    from concurrent.futures import ThreadPoolExecutor

    from .media import frame_jpeg

    def cam_at(t):
        for sg in segs:
            if sg["start"] <= t < sg["end"]:
                return sg["cam"]
        return -1
    want = []
    for i, b in enumerate(result.get("bad") or []):
        want.append((f"bad:{i}", b.get("cam", cam_at(b["start"])), (b["start"] + b["end"]) / 2))
    for i, j in enumerate(result.get("jumps") or []):
        want.append((f"jump:{i}", cam_at(j["time"] + 0.2), j["time"] + 0.2))
    for i, d in enumerate(result.get("dead") or []):
        want.append((f"dead:{i}", cam_at((d["start"] + d["end"]) / 2), (d["start"] + d["end"]) / 2))
    for i, m in enumerate(result.get("moments") or []):
        want.append((f"mom:{i}", m.get("cam", cam_at(m["time"])), m["time"]))
    want = [w for w in want if w[1] is not None and w[1] >= 0][:limit]

    def one(x):
        key, c, t = x
        for cl in project["cameras"][c]["clips"]:
            if cl["tl_start"] <= t < cl["tl_end"]:
                b = frame_jpeg(cl["path"], t - cl["offset"])
                return key, ("data:image/jpeg;base64," + base64.b64encode(b).decode()) if b else None
        return key, None
    if not want:
        return {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        return {k: v for k, v in ex.map(one, want) if v}


def _rituals(mix16, t0):
    """Poruwa: the parts of the ceremony from its sound (chanting, drums, applause, quiet moments)."""
    from .events_detect import KINDS, classify
    lab = classify(mix16, 16000)
    out, i = [], 0
    while i < len(lab):
        j = i
        while j + 1 < len(lab) and lab[j + 1] == lab[i]:
            j += 1
        k = KINDS[lab[i]]
        if k in RITUAL_MIN and (j - i + 1) >= RITUAL_MIN[k]:
            out.append({"start": t0 + i, "end": t0 + j + 1, "kind": k, "label": RITUAL_LABEL[k]})
        i = j + 1
    return out


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


def _weak_takes(times, parts, project, min_len: float = 3.0, segments=None, dance: bool = False):
    """Per camera stretches that are blurred, too dark / blown out or very shaky (QC markers).
    Only where that camera is actually in the edit (segments). Dance: movement, pans and people
    crossing are the event itself, so only blur / exposure, and only longer stretches."""
    out = []
    tests = [("sharp", "blur / focus", lambda v: v < -1.6), ("exposure", "exposure", lambda v: v > 1.5)]
    if dance:
        min_len = max(min_len, 5.0)
    else:
        tests += [("shake", "jerk / shake", lambda v: v > 2.5), ("pan", "jerk (ඉක්මන් pan)", lambda v: v > 1.0),
                  ("walk", "කෙනෙක් ඉස්සරහින් යනවා", lambda v: v > 0.6)]
    for c, p in enumerate(parts):
        if not p:
            continue
        for key, why, test in tests:
            ml = 0.8 if key == "walk" else min_len
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
                    a_t, b_t = float(times[i]), float(times[min(j, len(times) - 1)])
                    used = segments is None or any(sg["cam"] == c and min(sg["end"], b_t) - max(sg["start"], a_t) > 0.5
                                                   for sg in segments)
                    if j - i >= 2 and b_t - a_t >= ml and used:
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


def fmt_t(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


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
    rolls = []
    music = np.zeros(int(t1 - t0) + 1)
    ph = (np.zeros(0), np.zeros(0))
    if len(audio) > beatmod.SR * 2 and np.abs(audio).max() > 0:
        if preset.grid in ("beats", "auto"):
            # dance / songs: song by song (each song its own tempo, bars and phrases); other events
            # (speech, ambience with some music) keep one beat track - splitting them gives nonsense
            if preset.grid == "beats":
                b, strength, songs = beatmod.detect_beats_songs(audio, progress=lambda f: prog("audio", 0.5 + 0.3 * f))
            else:
                b, _tempos, strength = beatmod.detect_beats(audio, progress=lambda f: prog("audio", 0.5 + 0.3 * f))
                songs = []
            beats = b + t0
            dbeats = np.concatenate([beatmod.downbeats(beats[(beats >= s["start"] + t0) & (beats < s["end"] + t0)],
                                                       strength[(beats >= s["start"] + t0) & (beats < s["end"] + t0)])
                                     for s in songs]) if songs and len(beats) else beatmod.downbeats(beats, strength)
            if len(songs) > 1:
                shown = [s for s in songs if s["bpm"] > 0][:8]
                log("  සින්දු " + str(len(songs)) + ": " + ", ".join(
                    f"{fmt_t(s['start'] + t0)} ~{s['bpm']:.0f} BPM" for s in shown) + (" …" if len(songs) > 8 else ""))
            if len(b) > 2:
                beat_period = float(np.median(np.diff(b)))
            if getattr(preset, "roll_cuts", False):
                rolls = beatmod.detect_rolls(audio, b)
                for r in rolls:
                    r["start"] += t0; r["end"] += t0; r["land"] += t0
                    r["hits"] = [h + t0 for h in r["hits"]]
                if rolls:
                    log(f"  beat rolls {len(rolls)} (cuts roll එකට ගැලපෙන්න)")
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

    # ---- AI voice detection (Silero VAD): sentence ends + where people are talking -------------
    speech = None
    if preset.grid in ("phrases", "auto") and len(mix16) > 16000:
        try:
            from . import vad as vadmod
            vp = vadmod.speech_prob(mix16)
            if vp is not None:
                segs_v = vadmod.speech_segments(vp)
                vt, vs = vadmod.phrase_points(segs_v)
                if len(vt):
                    ph = (vt + t0, vs)
                speech = (t0, vadmod.STEP, vp)
                talk_pc = 100 * sum(b - a for a, b in segs_v) / max(1e-6, len(mix16) / 16000)
                log(f"  AI voice detection: කතා කරන වෙලාව {talk_pc:.0f}% · වාක්‍ය ඉවර වෙන තැන් {len(vt)}")
        except Exception as e:  # noqa: BLE001
            log(f"  AI voice detection මඟ හැරියා: {e}")

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

    # speeches: key moments (applause / laughter / cheering) from the sound, per second
    key_sec = None
    if getattr(preset, "speaker_lock", 0.0) > 0 and len(mix16) > 16000 * 5:
        try:
            from .events_detect import KINDS, classify
            lab = classify(mix16, 16000)
            k = (lab == KINDS.index("applause")).astype(np.float64)
            k = np.convolve(k, np.ones(4), mode="same") > 0          # a little before and after
            key_sec = k.astype(np.float64)
            if key_sec.any():
                log(f"  key moments (අත්පොළසන් / සිනහ) {int(key_sec.sum())}s: එතන විතරක් reactions")
        except Exception as e:  # noqa: BLE001
            log(f"  key moments මඟ හැරියා: {e}")

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
    from .analyze import timing_reset, timing_text
    from .faces import gpu_status
    timing_reset()
    times = np.zeros(0)
    scores = np.zeros((C, 0))
    # look-ahead: while one window is scored and cut, the next windows are already being analysed
    # (fills the chunk cache; analyze_file waits for a chunk another thread is on, never does it twice)
    from concurrent.futures import ThreadPoolExecutor as _TPE
    cores = os.cpu_count() or 4
    ahead = 2 if cores >= 8 else (1 if cores >= 4 else 0)
    try:
        ahead = int(os.environ.get("AUTOWED_LOOKAHEAD", ahead))
    except ValueError:
        pass
    pre = _TPE(max_workers=ahead) if ahead and len(wins) > 1 else None
    queued = set()

    def _prefetch(k):
        if pre is None or k >= len(wins) or k in queued:
            return
        queued.add(k)
        a = wins[k]
        pre.submit(lambda a=a: analyze_cameras(project["cameras"], a, min(t1, a + window), analysis_fps, cache_dir,
                                               use_gpu, workers, log=lambda m: None, progress=None))
    for wi, w0 in enumerate(wins):
        w1 = min(t1, w0 + window)
        final = wi == len(wins) - 1
        for k in range(wi + 1, wi + 1 + ahead):
            _prefetch(k)
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
        if key_sec is not None:
            idx = np.clip(np.floor(times - t0).astype(int), 0, len(key_sec) - 1)
            analysis["key"] = key_sec[idx]
        scores, _parts = score_cameras(analysis, preset, beat_period)
        if w1 - commit_t < preset.min_shot + lookahead and not final:
            continue
        bounds, weight = build_cut_grid(commit_t, w1, preset, beats, dbeats, ph,
                                        music[int(commit_t - t0):] if len(music) else None, rolls=rolls, speech=speech)
        bs = block_scores(scores, times, bounds)
        # cameras with the same shot size here (AI shot size): a cut between them looks like a jump
        pair_extra = None
        try:
            sel = (times >= commit_t) & (times < w1)
            med = [float(np.nanmedian(np.asarray(c["shot_scale"], np.float64)[sel]))
                   if np.isfinite(np.asarray(c["shot_scale"], np.float64)[sel]).sum() >= 5 else np.nan
                   for c in analysis["cams"]]
            pe = np.zeros((C, C))
            for a_ in range(C):
                for b_ in range(C):
                    if a_ != b_ and np.isfinite(med[a_]) and np.isfinite(med[b_]) and abs(med[a_] - med[b_]) < 0.1:
                        pe[a_, b_] = 0.5 * preset.switch_cost
            pair_extra = pe if pe.any() else None
        except Exception:  # noqa: BLE001
            pair_extra = None
        choice = choose(bs, bounds, weight, preset, prev_cam, final=final, rolls=rolls, beat_period=beat_period,
                        pair_extra=pair_extra)
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
    if pre is not None:
        pre.shutdown(wait=False, cancel_futures=True)
    log(f"  face detector: {', '.join(sorted(detectors))} · decode: {decode_mode()} · fps {analysis_fps:g} · parallel {workers}"
        + (f" · look-ahead {ahead}" if pre is not None else ""))
    if "yunet" in detectors:
        from . import vision as _vision
        log(f"  faces: {gpu_status()} · AI people / shot size: {'ON' if _vision.available() else 'OFF (model නැහැ)'}")
    tt = timing_text()
    if tt:
        log(f"  analysis වෙලාව: {tt}")

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
        qc = run_qc(project, t0, t1, preset.id, mix16, dead, bool(ex.get("silence")), False,
                     silence_min=float(ex.get("silence_min") or 0.9))
        qc["moments"] = _moments(mix16, t0, times, _parts, final_segs)
        qc["bad"] = _weak_takes(times, _parts, project, segments=final_segs, dance=preset.grid == "beats")
        qc["jumps"] = _jump_cuts(times, analysis, final_segs)
    except Exception as e:  # noqa: BLE001
        log(f"  checks මඟ හැරියා: {e}")
    try:
        cm = _color_match(analysis, final_segs, project)
        if cm is not None:
            qc["color"] = cm
            if cm["cams"]:
                for x in cm["cams"]:
                    log(f"  Color match ({'skin tones' if x['source'] == 'skin' else 'scene'}, {x['samples']}): {x['name']} → "
                        f"R {x['rgb'][0]:g}% · G {x['rgb'][1]:g}% · B {x['rgb'][2]:g}% ({cm['ref_name']} එකට)")
            else:
                log(f"  Color match: කැමරා දැනටමත් එක වගේ ({cm['ref_name']} එකට)")
    except Exception as e:  # noqa: BLE001
        log(f"  color match මඟ හැරියා: {e}")
    if getattr(preset, "ritual_marks", False) and len(mix16) > 16000 * 10:
        try:
            qc["rituals"] = _rituals(mix16, t0)
            if qc["rituals"]:
                log(f"  පෝරුවේ කොටස් {len(qc['rituals'])} (ගාථා / බෙර / අත්පොළසන් / නිහඬ)")
        except Exception as e:  # noqa: BLE001
            log(f"  පෝරුවේ කොටස් මඟ හැරියා: {e}")
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
    try:
        result["thumbs"] = _thumbs(project, final_segs, result)
    except Exception as e:  # noqa: BLE001
        log(f"  review pictures මඟ හැරියා: {e}")
    if ex.get("qc_markers"):
        # the new timeline carries the checks as markers (its times are mapped past trims / silences)
        for m in result.get("moments") or []:
            result["markers"].append({"time": m["time"], "name": f"Moment: {m.get('kind', '')}"})
        for b in result.get("bad") or []:
            result["markers"].append({"time": b["start"], "name": f"Weak take: {b.get('name', '')} ({b.get('why', '')})"})
        for j in result.get("jumps") or []:
            result["markers"].append({"time": j["time"], "name": "Jump cut?"})
        for r in result.get("rituals") or []:
            result["markers"].append({"time": r["start"], "name": r["label"]})
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
