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

from . import __version__
from . import project as proj
from .media import nvdec_available, FFMPEG
from .faces import FaceDetector
from .pipeline import run
from . import presets
from .paths import WEB_INDEX as WEB, LOG_FILE, DATA_DIR

IPC_DIR = os.path.join(DATA_DIR, "ipc")
from . import updater
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()  # one heavy job at a time (GPU / disk friendly)


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


def _run_edit(job, body):
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
            proj.save(p, os.path.join(out_dir, "project.json"))
            ov = {k: o.get(k) for k in ("min_shot", "max_shot", "switch_cost", "stay_cost")}
            if o.get("cam_bias"):
                ov["cam_bias"] = [float(x) for x in o["cam_bias"]]
            res = run(p, mode, out_dir, ov, (float(o["fps"]) if o.get("fps") else None), not o.get("cpu", False),
                      int(o.get("workers", 2)), o.get("layout", "stacked"), bool(o.get("remove_dead", False)),
                      log=log, progress=_progress(job), on_segments=_on_segments(job),
                      xml_name=(o.get("xml_name") or None))
            res.pop("beats", None)
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
        if path == "/api/presets":
            return 200, {"events": presets.listing()}
        if path == "/api/ping":
            return 200, {"ok": True, "version": __version__, "nvdec": nvdec_available(),
                         "face_detector": FaceDetector().kind, "ffmpeg": FFMPEG}
        if path.startswith("/api/update/status"):
            return 200, dict(updater.STATE)
        if path.startswith("/api/update"):
            return 200, updater.check(force="force" in path)
        if path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return 404, {"error": "no such job"}
            return 200, {**job, "log": list(job["log"])}
    elif method == "POST":
        if path == "/api/update/install":
            if LOCK.locked():
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
        if path == "/api/edit":
            job = _new_job("edit")
            threading.Thread(target=_run_edit, args=(job, body), daemon=True).start()
            return 200, {"job": job["id"]}
    return 404, {"error": "not found"}


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
    print(f"  GPU decode (NVDEC): {'ON' if nvdec_available() else 'OFF'}   face detector: {FaceDetector().kind}"
          f"   ffmpeg: {FFMPEG}", flush=True)
    print("  මේ window එක open කරලා තියන්න. Premiere panel එක හෝ browser එකෙන් භාවිතා කරන්න.", flush=True)
    threading.Thread(target=updater.check, daemon=True).start()  # warm the update cache
    threading.Thread(target=_ipc_loop, daemon=True).start()
    print(f"  IPC folder: {IPC_DIR}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
