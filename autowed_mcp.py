"""AutoWed MCP server: lets Claude read the Premiere Pro timeline and run AutoWed tests.

Built as AutoWedMCP.exe (a small console program, Python standard library only).
Claude desktop starts it over stdio. It talks to the running AutoWed engine on 127.0.0.1:7788,
and the engine hands each request to the AutoWed panel inside Premiere Pro (footer button
"Claude: ON"). Nothing here edits a project on its own: every action goes through the panel.

    AutoWedMCP.exe              -> MCP server on stdio
    AutoWedMCP.exe --register   -> add "autowed-premiere" to Claude desktop's config
    AutoWedMCP.exe --unregister -> remove it again
"""
import json
import os
import sys
import urllib.error
import urllib.request

ENGINE = "http://127.0.0.1:7788"
NAME = "autowed-premiere"
try:
    from engine import __version__ as VERSION  # light: engine/__init__ only reads the version
except Exception:  # noqa: BLE001
    VERSION = "0"


def http(method, path, body=None, timeout=10):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(ENGINE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def panel(cmd, args=None, timeout=60):
    return http("POST", "/api/bridge/call", {"cmd": cmd, "args": args or {}, "timeout": timeout},
                timeout=timeout + 10)


TOOLS = [
    {"name": "autowed_status",
     "description": "AutoWed engine version, whether the Premiere panel is connected (footer 'Claude: ON'), "
                    "whether an edit is running and its progress.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "premiere_get_sequence",
     "description": "Read the active Premiere sequence: name, fps, In/Out, every video track with its clips "
                    "(name, file path, timeline start/end, source in/out, speed, disabled). "
                    "range=true keeps only clips that touch the In/Out range.",
     "inputSchema": {"type": "object", "properties": {
         "range": {"type": "boolean", "default": True},
         "audio": {"type": "boolean", "default": False, "description": "also list audio tracks"},
         "max_clips": {"type": "integer", "default": 400, "description": "per track"}}}},
    {"name": "autowed_read_sequence",
     "description": "Press 'Sequence කියවන්න' in the panel: which cameras/tracks AutoWed will use.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "autowed_get_log",
     "description": "The last lines of the AutoWed panel log (what the user sees in the panel).",
     "inputSchema": {"type": "object", "properties": {"lines": {"type": "integer", "default": 120}}}},
    {"name": "autowed_razor_test",
     "description": "Run the panel's Razor test (works on a copy of the active sequence) and return its log.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "autowed_auto_edit",
     "description": "Start Auto Edit on the active sequence between its In and Out (edits the user's "
                    "timeline in place). Returns at once; follow with autowed_status / autowed_get_log. "
                    "Only when the user asked for a test run.",
     "inputSchema": {"type": "object", "properties": {
         "event": {"type": "string", "description": "event id, e.g. auto, poruwa, dance_floor, speeches"},
         "edit_type": {"type": "string", "enum": ["multicam", "stacked"]}}}},
    {"name": "premiere_run_script",
     "description": "Debugging: run an async JavaScript body inside the AutoWed panel with (ppro, project, h) "
                    "and return its result as JSON. h has itemsOf, readTI, readItem, freshSeq, locate, log, "
                    "fmt, sleep. Use for reading state; avoid edits unless the user asked.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "engine_log",
     "description": "The last 300 lines of the AutoWed engine log file.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def call_tool(name, a):
    if name == "autowed_status":
        out = {"engine": http("GET", "/api/ping"), "bridge": http("GET", "/api/bridge/status")}
        if out["bridge"].get("panel_connected"):
            out["panel"] = panel("status", timeout=15)
        return out
    if name == "premiere_get_sequence":
        return panel("sequence", {"range": a.get("range", True), "audio": a.get("audio", False),
                                  "max_clips": a.get("max_clips", 400)}, timeout=120)
    if name == "autowed_read_sequence":
        return panel("read_sequence", timeout=60)
    if name == "autowed_get_log":
        return panel("log", {"lines": a.get("lines", 120)}, timeout=15)
    if name == "autowed_razor_test":
        return panel("razor_test", timeout=240)
    if name == "autowed_auto_edit":
        return panel("auto_edit", {k: a[k] for k in ("event", "edit_type") if a.get(k)}, timeout=30)
    if name == "premiere_run_script":
        return panel("eval", {"code": a.get("code", "")}, timeout=120)
    if name == "engine_log":
        return http("GET", "/api/log/tail")
    raise ValueError("unknown tool " + name)


def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # notification
    if method == "initialize":
        res = {"protocolVersion": params.get("protocolVersion") or "2024-11-05",
               "capabilities": {"tools": {}},
               "serverInfo": {"name": NAME, "version": VERSION},
               "instructions": "AutoWed (Powered by Endora) Premiere Pro bridge. The user must have the AutoWed "
                               "panel open with the footer button set to 'Claude: ON'."}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            out = call_tool(params.get("name"), params.get("arguments") or {})
            err = isinstance(out, dict) and out.get("ok") is False
            text = json.dumps(out, ensure_ascii=False, indent=1)
            res = {"content": [{"type": "text", "text": text[:200000]}], "isError": bool(err)}
        except urllib.error.URLError as e:
            res = {"content": [{"type": "text", "text": f"AutoWed engine is not running ({e}). Start "
                                                        "Start menu > AutoWed > AutoWed Engine."}], "isError": True}
        except Exception as e:  # noqa: BLE001
            res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    elif method == "ping":
        res = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def serve():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            out = handle(m)
            if out is not None:
                stdout.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                stdout.flush()


# ---------- Claude desktop config ----------
def _config_paths():
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(os.path.join(appdata, "Claude", "claude_desktop_config.json"))
    local = os.environ.get("LOCALAPPDATA")
    if local:  # Microsoft Store / MSIX install keeps its config in the package folder
        pk = os.path.join(local, "Packages")
        if os.path.isdir(pk):
            for d in os.listdir(pk):
                if d.lower().startswith("claude"):
                    paths.append(os.path.join(pk, d, "LocalCache", "Roaming", "Claude", "claude_desktop_config.json"))
    return [p for p in paths if os.path.isdir(os.path.dirname(p))]


def register(remove=False):
    exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
    for p in _config_paths():
        cfg = {}
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                print("skip (unreadable):", p)
                continue
            with open(p + ".autowed-bak", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        servers = cfg.setdefault("mcpServers", {})
        if remove:
            servers.pop(NAME, None)
        else:
            servers[NAME] = {"command": exe, "args": []}
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, p)
        print(("removed from " if remove else "registered in ") + p)


if __name__ == "__main__":
    if "--register" in sys.argv:
        register()
    elif "--unregister" in sys.argv:
        register(remove=True)
    else:
        serve()
