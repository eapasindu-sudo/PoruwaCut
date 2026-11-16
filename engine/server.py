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
from .paths import WEB_INDEX as WEB, LOG_FILE
from . import updater
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()  # one heavy job at a time (GPU / disk friendly)


def _new_job(kind):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "kind": kind, "status": "queued", "progress": 0.0, "log": [], "result": None}
    return JOBS[jid]


def _logger(job):
    pat = re.compile(r"PROGRESS clips (\d+)/(\d+)")

    def log(msg):
        m = pat.search(msg)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            job["progress"] = 0.05 + 0.9 * a / max(1, b)
        if not msg.startswith("PROGRESS"):
            job["log"].append(msg)
            print(f"[{job['id']}] {msg}", flush=True)
    return log


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
            mode = body.get("mode", "ceremony")
            out_dir = body.get("out_dir") or _default_out(p, mode)
            os.makedirs(out_dir, exist_ok=True)
            proj.save(p, os.path.join(out_dir, "project.json"))
            ov = {k: o.get(k) for k in ("min_shot", "max_shot", "min_beats", "max_beats", "switch_cost", "stay_cost")}
            if o.get("cam_bias"):
                ov["cam_bias"] = [float(x) for x in o["cam_bias"]]
            res = run(p, mode, out_dir, ov, float(o.get("fps", 5)), not o.get("cpu", False),
                      int(o.get("workers", 2)), o.get("layout", "stacked"), bool(o.get("remove_dead", False)),
                      log=log)
            res.pop("beats", None)
            job["result"] = res
            job["progress"] = 1.0
            job["status"] = "done"
    except Exception as e:  # report to the panel instead of dying
        job["status"] = "error"
        job["log"].append(f"ERROR: {e}")
        job["log"].append(traceback.format_exc())
        print(traceback.format_exc(), flush=True)


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
        if self.path == "/api/ping":
            return self._send(200, {"ok": True, "version": __version__, "nvdec": nvdec_available(),
                                    "face_detector": FaceDetector().kind, "ffmpeg": FFMPEG})
        if self.path.startswith("/api/update/status"):
            return self._send(200, updater.STATE)
        if self.path.startswith("/api/update"):
            return self._send(200, updater.check(force="force" in self.path))
        if self.path.startswith("/api/job/"):
            job = JOBS.get(self.path.rsplit("/", 1)[-1])
            if not job:
                return self._send(404, {"error": "no such job"})
            since = 0
            return self._send(200, {**job, "log": job["log"][since:]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/update/install":
            if LOCK.locked():
                return self._send(409, {"status": "error", "message": "Edit එකක් run වෙනවා. ඉවර වුණාම update කරන්න."})
            return self._send(200, updater.start_install())
        if self.path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return
        if self.path == "/api/edit":
            job = _new_job("edit")
            threading.Thread(target=_run_edit, args=(job, body), daemon=True).start()
            return self._send(200, {"job": job["id"]})
        self._send(404, {"error": "not found"})


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
    print(f"PoruwaCut engine {__version__}  ->  http://{host}:{port}", flush=True)
    print(f"  GPU decode (NVDEC): {'ON' if nvdec_available() else 'OFF'}   face detector: {FaceDetector().kind}"
          f"   ffmpeg: {FFMPEG}", flush=True)
    print("  මේ window එක open කරලා තියන්න. Premiere panel එක හෝ browser එකෙන් භාවිතා කරන්න.", flush=True)
    threading.Thread(target=updater.check, daemon=True).start()  # warm the update cache
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
