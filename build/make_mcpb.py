"""Package AutoWedMCP.exe as a Claude Desktop Extension (dist/AutoWed_<ver>.mcpb).

Install: double-click the .mcpb (or Claude desktop > Settings > Extensions > Install extension).
"""
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ver = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
MAC = sys.platform == "darwin"
EXE = "AutoWedMCP" if MAC else "AutoWedMCP.exe"
exe = os.path.join(ROOT, "dist", EXE)
if not os.path.exists(exe):
    sys.exit(f"dist/{EXE} missing")
sys.path.insert(0, ROOT)
from autowed_mcp import TOOLS  # noqa: E402

manifest = {
    "manifest_version": "0.2",
    "name": "autowed-premiere",
    "display_name": "AutoWed for Premiere Pro",
    "version": ver,
    "description": "Lets Claude read your Premiere Pro timeline and run AutoWed tests (AutoWed panel > Claude: ON).",
    "long_description": "AutoWed (Powered by Endora) auto-edits Sri Lankan wedding multicam footage in Premiere Pro. "
                        "This extension connects Claude to the AutoWed panel so it can read the active sequence, "
                        "the panel log and the engine log, and run the Razor test or an Auto Edit when you ask.",
    "author": {"name": "Endora"},
    "server": {
        "type": "binary",
        "entry_point": EXE,
        "mcp_config": {"command": "${__dirname}/" + EXE, "args": []},
    },
    "tools": [{"name": t["name"], "description": t["description"]} for t in TOOLS],
    "compatibility": {"platforms": ["darwin" if MAC else "win32"]},
    "license": "Proprietary",
}
out = os.path.join(ROOT, "dist", f"AutoWed_{ver}_mac.mcpb" if MAC else f"AutoWed_{ver}.mcpb")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("manifest.json", json.dumps(manifest, indent=2))
    zi = zipfile.ZipInfo(EXE)
    zi.external_attr = 0o100755 << 16        # keep the executable bit (Mac / unzip)
    zi.compress_type = zipfile.ZIP_DEFLATED
    with open(exe, "rb") as f:
        z.writestr(zi, f.read())
import shutil  # noqa: E402
if not MAC:
    shutil.copy2(out, os.path.join(ROOT, "dist", "AutoWed.mcpb"))
print("wrote", out)
