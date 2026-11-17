"""Package AutoWedMCP.exe as a Claude Desktop Extension (dist/AutoWed_<ver>.mcpb).

Install: double-click the .mcpb (or Claude desktop > Settings > Extensions > Install extension).
"""
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ver = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
exe = os.path.join(ROOT, "dist", "AutoWedMCP.exe")
if not os.path.exists(exe):
    sys.exit("dist/AutoWedMCP.exe missing")
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
        "entry_point": "AutoWedMCP.exe",
        "mcp_config": {"command": "${__dirname}/AutoWedMCP.exe", "args": []},
    },
    "tools": [{"name": t["name"], "description": t["description"]} for t in TOOLS],
    "compatibility": {"platforms": ["win32"]},
    "license": "Proprietary",
}
out = os.path.join(ROOT, "dist", f"AutoWed_{ver}.mcpb")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("manifest.json", json.dumps(manifest, indent=2))
    z.write(exe, "AutoWedMCP.exe")
import shutil  # noqa: E402
shutil.copy2(out, os.path.join(ROOT, "dist", "AutoWed.mcpb"))
print("wrote", out)
