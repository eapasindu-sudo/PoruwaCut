"""Local engine server used by the Premiere UXP panel and the browser UI.

    python -m engine serve            -> http://127.0.0.1:7788

POST /api/edit  {"project": {...}, "mode": "ceremony", "options": {...}, "out_dir": "..."}
POST /api/sync  {"cams": ["D:/w/A", "D:/w/B"], "master": 1, "name": "..."}
GET  /api/job/<id>                    -> {"status", "progress", "log", "result"}
GET  /api/ping
"""
from __future__ import annotations

import json
import os
import re
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import license, __version__
from . import project as proj
from .media import nvdec_available, gpu_decode_name, FFMPEG
from .faces import FaceDetector
from .pipeline import run
from . import presets
from .paths import WEB_INDEX as WEB, LOG_FILE, DATA_DIR

IPC_DIR = os.path.join(DATA_DIR, "ipc")
from . import updater
JOBS: dict[str, dict] = {}
LOCK = threading.RLock()  # one heavy job at a time (GPU / disk friendly)


def _new_job(kind):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "kind": kind, "status": "queued", "progress": 0.0, "stage": "queued",
                 "stage_progress": 0.0, "eta": None, "elapsed": 0.0, "log": [], "result": None,
                 "segments": [], "committed_until": None}
    return JOBS[jid]


def _logger(job):
    def log(msg):
        if msg.startswith("PROGRESS"):
            return
        job["log"].append(msg)
        print(f"[{job['id']}] {msg}", flush=True)
    return log


def _progress(job):
    """Track stage + percentage + ETA (ETA from the analysis rate, the slow part)."""
    import time
    t_start = time.time()
    st = {"a_t0": None, "a_f0": 0.0}

    def progress(stage, overall, stage_frac):
        now = time.time()
        job["stage"], job["progress"] = stage, round(float(overall), 4)
        job["stage_progress"] = round(float(stage_frac), 4)
        job["elapsed"] = round(now - t_start, 1)
        if stage == "analysis":
            if st["a_t0"] is None:
                st["a_t0"], st["a_f0"] = now, stage_frac
            done = stage_frac - st["a_f0"]
            spent = now - st["a_t0"]
            if done > 0.01 and spent > 3:
                job["eta"] = round(spent / done * (1 - stage_frac) + 3, 0)
        elif stage in ("cuts", "xml"):
            job["eta"] = 2
    return progress


def _on_segments(job):
    def cb(segments, until):
        job["segments"] = segments          # committed cuts so far (panel razors them live)
        job["committed_until"] = until
    return cb


def _default_out(project, mode):
    first = project["cameras"][project["master_cam"]]["clips"][0]["path"]
    return os.path.join(os.path.dirname(first), "PoruwaCut_out", mode)


def _guarded(job, body, impl):
    """Run a job; Stop ends it cleanly ("cancelled") instead of as an error."""
    from .media import STOP, Cancelled
    job["cancel"] = False
    try:
        with LOCK:
            STOP.clear()
            if job.get("cancel"):
                raise Cancelled()
            impl(job, body)
    except Cancelled:
        job["status"] = "cancelled"
        job["log"].append("නැවැත්තුවා (Stop).")
    finally:
        STOP.clear()
    if job.get("status") == "error" and any("Cancelled" in l for l in job["log"][-3:]):
        job["status"] = "cancelled"


def _json_file(name):
    return os.path.join(DATA_DIR, name)


def _load_json(name, default):
    try:
        with open(_json_file(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(name, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _json_file(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _json_file(name))


def _learned():
    return _load_json("learned.json", {})


def _record_stats(res, body):
    """Usage stats: what AutoWed edited and roughly how much editing time that saved."""
    try:
        import time as _t
        st = _load_json("stats.json", {"runs": []})
        a, b = res.get("range", [0, 0])
        st["runs"].append({"t": int(_t.time()), "event": res.get("event"), "kind": (body.get("options") or {}).get("kind"),
                           "seconds": round(b - a, 1), "cuts": res["stats"]["cuts"],
                           "cams": len(res["stats"].get("camera_share_pct", {})), "took": res["stats"].get("seconds")})
        st["runs"] = st["runs"][-5000:]
        _save_json("stats.json", st)
    except Exception:  # noqa: BLE001
        pass


def _stats_summary():
    import time as _t
    runs = _load_json("stats.json", {"runs": []})["runs"]
    def summ(rs):
        foot = sum(r["seconds"] for r in rs)
        took = sum(r.get("took") or 0 for r in rs)
        # a manual multicam pass is roughly 1.5x the footage length (watch + choose + cut)
        saved = max(0.0, 1.5 * foot - took)
        return {"runs": len(rs), "footage_min": round(foot / 60, 1), "cuts": sum(r["cuts"] for r in rs),
                "processing_min": round(took / 60, 1), "saved_hours": round(saved / 3600, 1)}
    now = _t.time()
    by_event = {}
    for r in runs:
        by_event[r["event"]] = by_event.get(r["event"], 0) + 1
    return {"all": summ(runs), "last_30_days": summ([r for r in runs if now - r["t"] < 30 * 86400]),
            "by_event": by_event}


def _run_sync(job, body):
    return _guarded(job, body, __run_sync_impl)


def __run_sync_impl(job, body):
    """Sync tab: line up any set of camera / recorder / mic files by audio."""
    import time
    from . import msync
    log = _logger(job)
    t0 = time.time()
    try:
        with LOCK:
            job["status"] = "running"

            def prog(frac, msg):
                job["stage"], job["progress"], job["stage_progress"] = "sync", round(frac, 4), round(frac, 4)
                job["elapsed"] = round(time.time() - t0, 1)
                job["stage_text"] = msg
            res = msync.place_files(body["files"], cache_dir=os.path.join(DATA_DIR, "cache", "sync"),
                                    workers=int(body.get("workers", 4)), log=log, progress=prog)
            job["result"] = res
            job["progress"] = 1.0
            job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["log"].append(f"ERROR: {e}")
        job["log"].append(traceback.format_exc())
        print(traceback.format_exc(), flush=True)


def _run_events(job, body):
    return _guarded(job, body, __run_events_impl)


def __run_events_impl(job, body):
    """Find speech / chanting / music / drums blocks in a timeline range (event markers)."""
    import time
    from . import events_detect
    log = _logger(job)
    t0 = time.time()
    try:
        with LOCK:
            job["status"] = "running"
            p = proj.normalise(body["project"])
            a, b = body.get("range") or p.get("range") or [0, 0]

            def prog(frac, msg):
                job["stage"], job["progress"], job["stage_progress"] = "events", round(frac, 4), round(frac, 4)
                job["elapsed"] = round(time.time() - t0, 1)
                job["stage_text"] = msg
            job["result"] = {"events": events_detect.detect_events(p, float(a), float(b), progress=prog, log=log)}
            job["progress"] = 1.0
            job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["log"].append(f"ERROR: {e}")
        job["log"].append(traceback.format_exc())
        print(traceback.format_exc(), flush=True)


def _hl_out_dir(paths) -> str:
    """Rendered highlight audio goes next to the footage (.endora_highlights), else the app folder."""
    try:
        base = os.path.commonpath([os.path.dirname(os.path.abspath(p_)) for p_ in paths]) if paths else ""
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            return os.path.join(base, ".endora_highlights")
    except ValueError:
        pass
    return os.path.join(DATA_DIR, "highlights")


def __run_hl_plan_impl(job, body):
    import time
    from . import highlight_build
    log = _logger(job)
    t0 = time.time()
    try:
        job["status"] = "running"

        def prog(stage, overall, frac):
            job["stage"], job["progress"], job["stage_progress"] = stage, round(float(overall), 4), round(float(frac), 4)
        out = _hl_out_dir([c.get("path") for c in (body.get("analysis") or {}).get("clips") or [] if c.get("path")])
        job["result"] = highlight_build.plan(body, out, log=log, progress=prog)
        job["progress"] = 1.0
        job["status"] = "done"
        log(f"plan ඉවරයි ({time.time() - t0:.0f}s)")
    except ValueError as e:
        job["status"] = "error"
        job["log"].append(f"ERROR: {e}")
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["log"].append(f"ERROR: {e}")
        job["log"].append(traceback.format_exc())


def __run_highlights_impl(job, body):
    """Endora Highlights (DaVinci Resolve panel): best moments of the whole wedding + song structure."""
    import time
    from . import highlights
    log = _logger(job)
    t0 = time.time()
    try:
        job["status"] = "running"
        paths = [c.get("path") for c in body.get("clips") or [] if c.get("path")]
        cache_dir = os.path.join(DATA_DIR, "cache", "highlights")
        try:
            base = os.path.commonpath([os.path.dirname(os.path.abspath(p_)) for p_ in paths]) if paths else ""
            if base and os.path.isdir(base) and os.access(base, os.W_OK):
                cache_dir = os.path.join(base, ".poruwacut_cache")
        except ValueError:      # clips on different drives
            pass

        def prog(stage, overall, frac):
            job["stage"], job["progress"], job["stage_progress"] = stage, round(float(overall), 4), round(float(frac), 4)
            job["elapsed"] = round(time.time() - t0, 1)
        job["result"] = highlights.analyze(body, cache_dir, use_gpu=not (body.get("options") or {}).get("cpu"),
                                           progress=prog, log=log)
        job["progress"] = 1.0
        job["status"] = "done"
        log(f"✔ Highlights analysis ඉවරයි ({time.time() - t0:.0f}s)")
    except Exception as e:  # noqa: BLE001
        from .media import Cancelled
        if isinstance(e, Cancelled):
            raise
        job["status"] = "error"
        job["log"].append(f"ERROR: {e}")
        job["log"].append(traceback.format_exc())
        print(traceback.format_exc(), flush=True)


def _run_edit(job, body):
    return _guarded(job, body, __run_edit_impl)


def __run_edit_impl(job, body):
    log = _logger(job)
    try:
        with LOCK:
            job["status"] = "running"
            p = body["project"]
            if "cams" in p:  # folder-based request from the browser UI: sync first
                job["stage"] = "sync"
                log("Audio sync...")
                p = proj.from_folders(p["cams"], int(p.get("master", 1)) - 1, p.get("name", "Wedding"), log)
            else:
                job["stage"] = "read"
                n = sum(len(c.get("clips", [])) for c in p["cameras"]) + sum(len(t) for t in p.get("audio_tracks") or [])
                log(f"Timeline clips කියවනවා (In/Out එක ඇතුළේ ඒවා විතරයි, මුළු clips {n})...")
                p = proj.normalise(p)
            o = body.get("options", {})
            if o.get("start") is not None or o.get("end") is not None:
                r = list(p["range"])
                if o.get("start") not in (None, ""):
                    r[0] = float(o["start"])
                if o.get("end") not in (None, ""):
                    r[1] = float(o["end"])
                p["range"] = r
            mode = body.get("event") or body.get("mode") or "auto"
            out_dir = body.get("out_dir") or _default_out(p, mode)
            os.makedirs(out_dir, exist_ok=True)
            _remember_cache(os.path.join(os.path.dirname(os.path.abspath(out_dir)), ".poruwacut_cache"))
            proj.save(p, os.path.join(out_dir, "project.json"))
            ov = {k: o.get(k) for k in ("min_shot", "max_shot", "switch_cost", "stay_cost")}
            # learned from the editor's own changes (Learn button): shot length scale for this event
            lr = _learned().get(body.get("event") or "auto")
            if lr and not o.get("min_shot") and not o.get("max_shot"):
                from . import presets as _pr
                base = _pr.get(body.get("event") or "auto", None)
                sc = float(lr.get("shot_scale", 1.0))
                ov["min_shot"], ov["max_shot"] = round(base.min_shot * sc, 2), round(base.max_shot * sc, 2)
                log(f"ඔබේ edit style එකෙන් ඉගෙනගත්ත shot දිග: x{sc:.2f}")
            if o.get("cam_bias"):
                ov["cam_bias"] = [float(x) for x in o["cam_bias"]]
            res = run(p, mode, out_dir, ov, (float(o["fps"]) if o.get("fps") else None), not o.get("cpu", False),
                      int(o.get("workers", 2)), o.get("layout", "stacked"), bool(o.get("remove_dead", False)),
                      log=log, progress=_progress(job), on_segments=_on_segments(job),
                      xml_name=(o.get("xml_name") or None), extras=o.get("extras") or {})
            res.pop("beats", None)
            _record_stats(res, body)
            job["result"] = res
            job["progress"] = 1.0
            job["status"] = "done"
    except Exception as e:  # report to the panel instead of dying
        job["status"] = "error"
        job["log"].append(f"ERROR: {e}")
        job["log"].append(traceback.format_exc())
        print(traceback.format_exc(), flush=True)


# ---------- Claude debug bridge: MCP server -> engine -> Premiere panel -> back ----------
import collections as _collections
import uuid as _uuid

BRIDGE = {"queue": _collections.deque(), "results": {}, "cond": threading.Condition(),
          "last_poll": 0.0, "panel": ""}


def _bridge_call(cmd: str, args: dict, timeout: float) -> dict:
    import time
    if time.time() - BRIDGE["last_poll"] > 6:
        return {"ok": False, "error": "AutoWed panel is not connected. Open Premiere Pro, open the AutoWed "
                                      "panel and switch the footer button to 'Claude: ON'."}
    jid = _uuid.uuid4().hex[:12]
    with BRIDGE["cond"]:
        BRIDGE["queue"].append({"id": jid, "cmd": cmd, "args": args or {}})
        end = time.time() + timeout
        while jid not in BRIDGE["results"]:
            left = end - time.time()
            if left <= 0:
                BRIDGE["queue"] = _collections.deque(j for j in BRIDGE["queue"] if j["id"] != jid)
                return {"ok": False, "error": f"panel did not answer within {timeout:.0f}s (it may still be working)"}
            BRIDGE["cond"].wait(min(left, 1.0))
        return BRIDGE["results"].pop(jid)


def _bridge_api(method, path, body):
    import time
    if method == "GET" and path == "/api/bridge/next":
        BRIDGE["last_poll"] = time.time()
        with BRIDGE["cond"]:
            return 200, (BRIDGE["queue"].popleft() if BRIDGE["queue"] else {})
    if method == "GET" and path == "/api/bridge/status":
        age = time.time() - BRIDGE["last_poll"] if BRIDGE["last_poll"] else None
        return 200, {"engine": __version__, "panel_connected": age is not None and age < 6,
                     "last_poll_age": age, "panel": BRIDGE["panel"]}
    if method == "GET" and path == "/api/log/tail":
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                return 200, {"lines": f.read().splitlines()[-300:]}
        except OSError as e:
            return 200, {"lines": [], "error": str(e)}
    if method == "POST" and path == "/api/bridge/result":
        with BRIDGE["cond"]:
            BRIDGE["results"][body.get("id", "")] = {k: v for k, v in body.items() if k != "id"}
            BRIDGE["cond"].notify_all()
        return 200, {"ok": True}
    if method == "POST" and path == "/api/bridge/call":
        return 200, _bridge_call(body.get("cmd", ""), body.get("args") or {}, float(body.get("timeout", 60)))
    return None


def api(method: str, path: str, body: dict | None = None):
    """One router for both the HTTP server and the file-based IPC used by the Premiere panel.
    Returns (status_code, json_obj)."""
    body = body or {}
    bpath, _, q = path.partition("?")
    if bpath.startswith("/api/bridge/") or bpath == "/api/log/tail":
        if bpath == "/api/bridge/next" and q.startswith("panel="):
            from urllib.parse import unquote
            BRIDGE["panel"] = unquote(q[6:])
        r = _bridge_api(method, bpath, body)
        if r is not None:
            return r
    if method == "GET":
        if path == "/api/license":
            return 200, license.status()
        if path == "/api/presets":
            return 200, {"events": presets.listing()}
        if path == "/api/ping":
            return 200, {"ok": True, "version": __version__, "nvdec": nvdec_available(),
                         "face_detector": FaceDetector().kind, "ffmpeg": FFMPEG}
        if path.startswith("/api/update/status"):
            return 200, dict(updater.STATE)
        if path.startswith("/api/update"):
            return 200, updater.check(force="force" in path)
        if path == "/api/jobs":
            return 200, {"jobs": [{k: j.get(k) for k in ("id", "kind", "status", "progress", "stage")} for j in JOBS.values()]}
        if path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return 404, {"error": "no such job"}
            return 200, {**job, "log": list(job["log"])}
        if path == "/api/stats":
            return 200, _stats_summary()
        if path == "/api/cache":
            return 200, cache_info()
        if path == "/api/studio_presets":
            folder = os.path.join(os.path.expanduser("~"), "Documents", "AutoWed Presets")
            out = []
            if os.path.isdir(folder):
                for fn in sorted(os.listdir(folder)):
                    if fn.lower().endswith(".json"):
                        try:
                            with open(os.path.join(folder, fn), encoding="utf-8") as f:
                                d = json.load(f)
                            out.append({"name": d.get("name") or fn[:-5], "settings": d.get("settings") or {}})
                        except (OSError, ValueError):
                            pass
            return 200, {"folder": folder, "presets": out}
        if path == "/api/learned":
            return 200, _learned()
    elif method == "POST":
        if path == "/api/update/install" and not license.is_valid():
            return 402, {"error": "Updates ලැබෙන්නේ valid license එකක් තියෙද්දී විතරයි. " + license.PRICE}
        if path == "/api/update/install":
            if any(j.get("status") in ("queued", "running") for j in JOBS.values()):
                return 409, {"status": "error", "message": "Edit එකක් run වෙනවා. ඉවර වුණාම update කරන්න."}
            return 200, dict(updater.start_install(close_premiere=bool(body.get("close_premiere")),
                                                   reopen=body.get("reopen")))
        if path == "/api/prproj/multicam":
            import time as _t
            from . import prproj
            src = body["project_path"]
            base, _ = os.path.splitext(src)
            backup = None
            if body.get("in_place"):
                import shutil
                backup = f"{base}.autowed-backup-{_t.strftime('%Y%m%d-%H%M%S')}.prproj"
                shutil.copy2(src, backup)   # untouched copy of the project as saved by Premiere
                dst = src
            else:
                dst = body.get("out_path") or f"{base} - AutoWed {_t.strftime('%H%M%S')}.prproj"
            try:
                rep = prproj.build_multicam_cuts(src, dst, body["edit_seq"], body["nest_seq"], body["segments"],
                                                 body.get("cam_tracks"))
            except Exception as e:  # nothing was written; drop the unneeded backup
                if backup and os.path.exists(backup):
                    os.remove(backup)
                return 200, {"error": str(e)}
            if body.get("in_place") and rep.get("multicam", 0) == 0:
                import shutil
                shutil.copy2(backup, src)   # nothing matched: leave the project exactly as it was
                return 200, {"error": "nest pieces not found in the saved project", "report": rep}
            return 200, {"path": dst, "report": rep, "backup": backup}
        if path == "/api/prproj/labels":
            import shutil
            import time as _t
            from . import prproj
            src = body["project_path"]
            base, _ = os.path.splitext(src)
            backup = f"{base}.autowed-backup-{_t.strftime('%Y%m%d-%H%M%S')}.prproj"
            shutil.copy2(src, backup)
            try:
                rep = prproj.label_camera_tracks(src, src, body["edit_seq"], body["cam_tracks"],
                                                 float(body["start"]), float(body["end"]))
            except Exception as e:  # noqa: BLE001
                shutil.copy2(backup, src)
                os.remove(backup)
                return 200, {"error": str(e)}
            return 200, {"report": rep, "backup": backup}
        if path == "/api/quit":
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return 200, {"ok": True}
        if path.startswith("/api/job/") and path.endswith("/cancel"):
            from .media import STOP
            job = JOBS.get(path.split("/")[3])
            if not job:
                return 404, {"error": "no such job"}
            job["cancel"] = True
            if job.get("status") in ("running",):
                STOP.set()
            elif job.get("status") == "queued":
                job["status"] = "cancelled"
            return 200, {"ok": True}
        if path == "/api/license/activate":
            return 200, license.activate(str(body.get("key") or ""))
        if path == "/api/license/trial":
            return 200, license.start_trial()
        if path == "/api/license/refresh":
            return 200, license.refresh(force=True)
        if path == "/api/license/remove":
            return 200, license.remove()
        # no licence: nothing that edits, syncs or analyses runs
        if path in ("/api/edit", "/api/sync", "/api/events", "/api/learn", "/api/hl/analyze", "/api/hl/plan", "/api/hl/learn") and not license.is_valid():
            st = license.status()
            return 402, {"error": st.get("message") or "License එකක් නැහැ.", "license": st}
        if path == "/api/notify":
            # phone notification through ntfy.sh (free app; subscribe to the same topic on the phone)
            topic = "".join(ch for ch in str(body.get("topic") or "") if ch.isalnum() or ch in "-_")[:64]
            if not topic:
                return 400, {"error": "topic නැහැ"}
            import urllib.request
            title = str(body.get("title") or "AutoWed")[:120]
            msg = str(body.get("message") or "")[:2000]
            pr = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}.get(str(body.get("priority") or "default"), 3)
            payload = {"topic": topic, "title": title, "message": msg, "priority": pr,
                       "tags": [str(body.get("tags") or "clapper")]}
            req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return 200, {"ok": r.status == 200}
            except Exception as e:  # noqa: BLE001
                return 502, {"error": str(e)}
        if path == "/api/cache/clear":
            if any(j.get("status") in ("queued", "running") for j in JOBS.values()):
                return 409, {"error": "edit එකක් run වෙනවා: ඉවර වුණාම cache clear කරන්න"}
            r = cache_clear(body.get("media_paths") or [])
            print(f"Cache clear: {r['files']} files, {r['freed_mb']} MB", flush=True)
            return 200, r
        if path == "/api/reset":
            # Stop everything that runs or waits
            from .media import STOP
            n = 0
            for j in JOBS.values():
                if j.get("status") in ("running", "queued"):
                    j["cancel"] = True
                    n += 1
                    if j["status"] == "running":
                        STOP.set()
                    else:
                        j["status"] = "cancelled"
            return 200, {"ok": True, "stopped": n}
        if path == "/api/learn":
            # {"event", "user_avg_shot", "auto_avg_shot"}: move the shot length toward the editor's taste
            ev = body.get("event") or "auto"
            u, a = float(body.get("user_avg_shot") or 0), float(body.get("auto_avg_shot") or 0)
            if u <= 0 or a <= 0:
                return 400, {"error": "need both averages"}
            lr = _learned()
            old = float(lr.get(ev, {}).get("shot_scale", 1.0))
            new = max(0.5, min(2.0, 0.6 * old + 0.4 * old * (u / a)))
            lr[ev] = {"shot_scale": round(new, 3), "samples": int(lr.get(ev, {}).get("samples", 0)) + 1}
            _save_json("learned.json", lr)
            return 200, {"event": ev, "shot_scale": lr[ev]["shot_scale"], "samples": lr[ev]["samples"]}
        if path == "/api/learn/reset":
            _save_json("learned.json", {})
            return 200, {"ok": True}
        if path == "/api/studio_presets":
            # save one named preset {"name", "settings"} ; shared as a file in Documents\AutoWed Presets
            name = re.sub(r'[\\/:*?"<>|]+', "_", str(body.get("name") or "preset")).strip()[:60]
            folder = os.path.join(os.path.expanduser("~"), "Documents", "AutoWed Presets")
            os.makedirs(folder, exist_ok=True)
            p_ = os.path.join(folder, name + ".json")
            with open(p_, "w", encoding="utf-8") as f:
                json.dump({"name": name, "settings": body.get("settings") or {}}, f, ensure_ascii=False, indent=1)
            return 200, {"path": p_}
        if path == "/api/events":
            job = _new_job("events")
            threading.Thread(target=_run_events, args=(job, body), daemon=True).start()
            return 200, {"job": job["id"]}
        if path == "/api/sync":
            job = _new_job("sync")
            threading.Thread(target=_run_sync, args=(job, body), daemon=True).start()
            return 200, {"job": job["id"]}
        if path == "/api/hl/plan":
            job = _new_job("highlight-plan")
            threading.Thread(target=_guarded, args=(job, body, __run_hl_plan_impl), daemon=True).start()
            return 200, {"job": job["id"]}
        if path == "/api/hl/learn":
            from . import highlight_build
            try:
                lines = []
                st = highlight_build.learn(body, log=lines.append)
                return 200, {"ok": True, "style": st, "log": lines}
            except ValueError as e:
                return 400, {"error": str(e)}
        if path == "/api/hl/analyze":
            job = _new_job("highlights")
            threading.Thread(target=_guarded, args=(job, body, __run_highlights_impl), daemon=True).start()
            return 200, {"job": job["id"]}
        if path == "/api/edit":
            job = _new_job("edit")
            threading.Thread(target=_run_edit, args=(job, body), daemon=True).start()
            return 200, {"job": job["id"]}
    return 404, {"error": "not found"}



# ---------- cache (analysis / audio / sync) ----------
CACHE_LIST = os.path.join(DATA_DIR, "caches.json")


def _remember_cache(d: str) -> None:
    try:
        lst = json.load(open(CACHE_LIST, encoding="utf-8")) if os.path.exists(CACHE_LIST) else []
        if d not in lst:
            lst.append(d)
            json.dump(lst, open(CACHE_LIST, "w", encoding="utf-8"))
    except Exception:
        pass


def _cache_dirs(media_paths=None) -> list[str]:
    out = [os.path.join(DATA_DIR, "cache")]
    for mp in media_paths or []:         # the panel's clips: <media folder>/PoruwaCut_out/.poruwacut_cache
        out.append(os.path.join(os.path.dirname(mp), "PoruwaCut_out", ".poruwacut_cache"))
    try:
        out += json.load(open(CACHE_LIST, encoding="utf-8"))
    except Exception:
        pass
    # only folders that are AutoWed's own caches
    return [d for d in dict.fromkeys(out) if os.path.basename(os.path.normpath(d)) in ("cache", ".poruwacut_cache")]


def _dir_size(d: str) -> tuple[int, int]:
    n = size = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return n, size


def cache_info(media_paths=None) -> dict:
    tot_n = tot_s = 0
    dirs = []
    for d in _cache_dirs(media_paths):
        if os.path.isdir(d):
            n, sz = _dir_size(d)
            tot_n += n
            tot_s += sz
            dirs.append({"path": d, "files": n, "mb": round(sz / 1e6, 1)})
    return {"files": tot_n, "mb": round(tot_s / 1e6, 1), "dirs": dirs}


def cache_clear(media_paths=None) -> dict:
    import shutil
    before = cache_info(media_paths)
    errors = []
    for d in _cache_dirs(media_paths):
        if os.path.isdir(d):
            try:
                shutil.rmtree(d)
            except Exception as e:      # a file in use: remove what can be removed
                errors.append(f"{d}: {e}")
                for root, _, files in os.walk(d):
                    for f in files:
                        try:
                            os.remove(os.path.join(root, f))
                        except OSError:
                            pass
    try:
        from . import media
        media.probe.cache_clear()
    except Exception:
        pass
    return {"ok": True, "freed_mb": before["mb"], "files": before["files"], "errors": errors}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, body=None, ctype="application/json"):
        data = body if body is not None else json.dumps(obj, ensure_ascii=False, default=float).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, body=b"")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(WEB, "rb") as f:
                return self._send(200, body=f.read(), ctype="text/html")
        self._send(*api("GET", self.path))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        try:
            self._send(*api("POST", self.path, body))
        except Exception as e:
            self._send(500, {"error": str(e)})


def _uxp_storage_roots():
    if os.name == "nt":
        return [os.path.join(os.environ.get("APPDATA", ""), "Adobe", "UXP", "PluginsStorage")]
    return [os.path.expanduser("~/Library/Application Support/Adobe/UXP/PluginsStorage")]


def _panel_dirs(extra_roots=()):
    """Plugin-data folders of the PoruwaCut UXP panel (one per Premiere version)."""
    found = []
    for base in list(_uxp_storage_roots()) + list(extra_roots):
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            if root[len(base):].count(os.sep) > 8:
                dirs[:] = []
                continue
            if "poruwacut_panel.json" in files or (
                    "poruwacut" in root.lower() and os.path.basename(root).lower() == "plugindata"):
                found.append(root)
    return found


def _serve_dir(d):
    import glob
    for req in glob.glob(os.path.join(d, "req_*.json")):
        rid = os.path.basename(req)[4:-5]
        try:
            with open(req, encoding="utf-8") as f:
                r = json.load(f)
            os.remove(req)
        except (OSError, ValueError):
            continue  # still being written
        try:
            code, obj = api(r.get("method", "GET"), r.get("path", ""), r.get("body"))
        except Exception as e:
            code, obj = 500, {"error": str(e)}
        tmp = os.path.join(d, f"res_{rid}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"status": code, "body": obj}, f, ensure_ascii=False, default=float)
        os.replace(tmp, os.path.join(d, f"res_{rid}.json"))


def _ipc_loop(extra_roots=()):
    """File-based IPC: Premiere's UXP sandbox sometimes blocks http:// fetches, so the panel
    drops req_<id>.json into its plugin-data folder and reads back res_<id>.json."""
    import glob
    import time
    os.makedirs(IPC_DIR, exist_ok=True)
    with open(os.path.join(IPC_DIR, "engine.json"), "w", encoding="utf-8") as f:
        json.dump({"version": __version__, "pid": os.getpid()}, f)
    dirs, last_scan = [IPC_DIR], 0.0
    while True:
        try:
            now = time.time()
            if now - last_scan > 3:
                new = [IPC_DIR] + _panel_dirs(extra_roots)
                if set(new) != set(dirs):
                    print("IPC folders:", new, flush=True)
                dirs, last_scan = new, now
            for d in dirs:
                _serve_dir(d)
                for old in glob.glob(os.path.join(d, "res_*.json")):
                    if now - os.path.getmtime(old) > 120:
                        os.remove(old)
        except Exception as e:
            print("IPC error:", e, flush=True)
        time.sleep(0.25)


def _ensure_streams():
    """The windowless exe has no console: send prints to %LOCALAPPDATA%\\PoruwaCut\\engine.log."""
    import sys
    if sys.stdout is None or sys.stderr is None:
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
                os.replace(LOG_FILE, LOG_FILE + ".old")
        except OSError:
            pass
        f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def serve(host="127.0.0.1", port=7788):
    _ensure_streams()
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        print(f"Port {port} දැනටමත් භාවිතයේ - PoruwaCut engine එකක් දැනටමත් run වෙනවා.", flush=True)
        return
    print(f"AutoWed engine (Powered by Endora) {__version__}  ->  http://{host}:{port}", flush=True)
    try:
        license.start_background_refresh()
        st = license.status()
        print(f"  License: {'OK' if st.get('ok') else 'නැහැ'}  {st.get('plan') or ''} {st.get('expires') or ''}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  License check: {e}", flush=True)
    print(f"  GPU decode ({gpu_decode_name()}): {'ON' if nvdec_available() else 'OFF'}   face detector: {FaceDetector().kind}"
          f"   ffmpeg: {FFMPEG}", flush=True)
    print("  මේ window එක open කරලා තියන්න. Premiere panel එක හෝ browser එකෙන් භාවිතා කරන්න.", flush=True)
    threading.Thread(target=updater.check, daemon=True).start()  # warm the update cache
    threading.Thread(target=_ipc_loop, daemon=True).start()
    print(f"  IPC folder: {IPC_DIR}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
