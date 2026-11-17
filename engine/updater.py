"""Auto-update from GitHub Releases.

A release is tagged vX.Y.Z and carries an asset named PoruwaCut_Setup_X.Y.Z.exe.
The engine downloads it, starts it silently (/VERYSILENT) and exits; the installer replaces
the engine, re-installs the Premiere plugin (.ccx) and starts the new engine again.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from . import __version__, UPDATE_REPO
from .paths import FROZEN

API = os.environ.get("PORUWACUT_UPDATE_API") or f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
_cache = {"t": 0.0, "data": None}
STATE = {"status": "idle", "progress": 0.0, "message": "", "version": None}


def _ver(s: str) -> tuple:
    nums = re.findall(r"\d+", s or "")
    return tuple(int(x) for x in nums[:3]) + (0,) * (3 - len(nums[:3]))


def _get(url: str, timeout=15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"PoruwaCut/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check(force: bool = False) -> dict:
    """{'current', 'latest', 'available', 'notes', 'url', 'sha256'} (cached 30 min)."""
    if not force and _cache["data"] and time.time() - _cache["t"] < 1800:
        return _cache["data"]
    out = {"current": __version__, "latest": None, "available": False, "notes": "", "url": None,
           "sha256": None, "size": None, "error": None}
    try:
        rel = json.loads(_get(API))
        tag = rel.get("tag_name", "")
        asset = next((a for a in rel.get("assets", [])
                      if a["name"].lower().startswith(("autowed_setup", "poruwacut_setup")) and a["name"].lower().endswith(".exe")), None)
        out["latest"] = tag.lstrip("vV")
        out["notes"] = (rel.get("body") or "")[:2000]
        if asset:
            out["url"] = asset["browser_download_url"]
            out["size"] = asset.get("size")
            dg = asset.get("digest") or ""
            if dg.startswith("sha256:"):
                out["sha256"] = dg.split(":", 1)[1]
        out["available"] = bool(asset) and _ver(tag) > _ver(__version__)
    except Exception as e:
        out["error"] = str(e)
    _cache.update(t=time.time(), data=out)
    return out


_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$setup = $args[0]; $proj = $args[1]; $close = $args[2]
$exe = $null; $closed = $true
if ($close -eq '1') {
  $p = Get-Process | Where-Object { $_.ProcessName -match '^Adobe Premiere' -and $_.MainWindowHandle -ne 0 }
  if ($p) {
    $exe = ($p | Select-Object -First 1).Path
    foreach ($x in $p) { [void]$x.CloseMainWindow() }
    $p | Wait-Process -Timeout 120
    $closed = -not (Get-Process -Id ($p.Id) -ErrorAction SilentlyContinue)
  }
}
Start-Process -FilePath $setup -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS' -Wait
if ($exe -and $closed) {
  Start-Sleep -Seconds 2
  if ($proj) { Start-Process -FilePath $exe -ArgumentList ('"' + $proj + '"') } else { Start-Process -FilePath $exe }
}
"""


def _download_and_run(info: dict, exit_after: bool, close_premiere: bool = False, reopen: str | None = None):
    try:
        STATE.update(status="downloading", progress=0.0, message="Update download වෙනවා...", version=info["latest"])
        dest = os.path.join(tempfile.gettempdir(), f"PoruwaCut_Setup_{info['latest']}.exe")
        req = urllib.request.Request(info["url"], headers={"User-Agent": f"PoruwaCut/{__version__}"})
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
            total = int(r.headers.get("Content-Length") or info.get("size") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total:
                    STATE["progress"] = got / total
        if info.get("sha256") and h.hexdigest().lower() != info["sha256"].lower():
            os.remove(dest + ".part")
            raise RuntimeError("download checksum mismatch")
        os.replace(dest + ".part", dest)
        STATE.update(status="installing", progress=1.0,
                     message="Install වෙනවා... ඉවර වුණාම Premiere restart කරන්න.")
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
            if close_premiere:
                # save happened in the panel; close Premiere politely, install, open Premiere again
                ps = os.path.join(tempfile.gettempdir(), "poruwacut_update.ps1")
                with open(ps, "w", encoding="utf-8") as f:
                    f.write(_PS)
                STATE["message"] = "Premiere close කරලා update එක install කරනවා. ඉවර වුණාම Premiere ආයෙ open වෙනවා."
                subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                                  "-File", ps, dest, reopen or "", "1"], creationflags=flags, close_fds=True)
            else:
                subprocess.Popen([dest, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                                 creationflags=flags, close_fds=True)
        else:
            STATE.update(status="downloaded", message=f"Windows නොවන නිසා install කළේ නැහැ: {dest}")
            return
        if exit_after:
            time.sleep(1.5)
            os._exit(0)  # installer replaces our files and starts the new engine
    except Exception as e:
        STATE.update(status="error", message=f"Update අසාර්ථකයි: {e}")


def start_install(exit_after: bool | None = None, close_premiere: bool = False, reopen: str | None = None) -> dict:
    if STATE["status"] in ("downloading", "installing"):
        return STATE
    info = check(force=True)
    if not info.get("available"):
        STATE.update(status="error", message=info.get("error") or "අලුත් update එකක් නැහැ")
        return STATE
    if exit_after is None:
        exit_after = FROZEN
    threading.Thread(target=_download_and_run, args=(info, exit_after, close_premiere, reopen), daemon=True).start()
    STATE.update(status="downloading", progress=0.0, version=info["latest"])
    return STATE


if __name__ == "__main__":
    print(json.dumps(check(force=True), indent=2), file=sys.stdout)
