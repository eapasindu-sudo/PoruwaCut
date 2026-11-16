"""Where things live, both in a source checkout and inside the installed PoruwaCutEngine.exe."""
from __future__ import annotations

import os
import shutil
import sys

FROZEN = getattr(sys, "frozen", False)

if FROZEN:  # PyInstaller one-folder build: data files sit in sys._MEIPASS (the _internal folder)
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    APP_DIR = os.path.dirname(sys.executable)
else:
    RES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    APP_DIR = RES_DIR

WEB_INDEX = os.path.join(RES_DIR, "web", "index.html")
MODELS_DIR = os.path.join(RES_DIR, "models")

if os.name == "nt":
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PoruwaCut")
else:
    DATA_DIR = os.path.join(os.path.expanduser("~"), ".poruwacut")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(DATA_DIR, "engine.log")


def find_tool(name: str) -> str:
    """Bundled ffmpeg/ffprobe (installer ships them next to the exe) first, then PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    for d in (os.path.join(APP_DIR, "ffmpeg"), APP_DIR, os.path.join(RES_DIR, "ffmpeg")):
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return shutil.which(name) or name
