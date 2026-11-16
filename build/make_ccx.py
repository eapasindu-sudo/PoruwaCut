"""Package premiere-uxp/ as a .ccx (a ZIP with manifest.json at its root).

    python build/make_ccx.py            -> dist/PoruwaCut.ccx and dist/PoruwaCut_<ver>.ccx

Install the .ccx by double-click (Creative Cloud), with the aescripts ZXP/UXP Installer,
or silently with UnifiedPluginInstallerAgent.exe /install (what PoruwaCut_Setup.exe does).
"""
import json
import os
import shutil
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "premiere-uxp")
DIST = os.path.join(ROOT, "dist")
ver = open(os.path.join(ROOT, "VERSION")).read().strip()

os.makedirs(DIST, exist_ok=True)
out = os.path.join(DIST, "PoruwaCut.ccx")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for base, _, files in os.walk(SRC):
        for fn in sorted(files):
            if fn.startswith(".") or fn.endswith(".md"):
                continue
            p = os.path.join(base, fn)
            arc = os.path.relpath(p, SRC).replace(os.sep, "/")
            data = open(p, "rb").read()
            if fn == "manifest.json":
                man = json.loads(data)
                man["version"] = ver
                data = json.dumps(man, indent=2, ensure_ascii=False).encode("utf-8")
            elif fn == "main.js":
                data = data.replace(b"__PORUWACUT_VERSION__", ver.encode())
            z.writestr(arc, data)
shutil.copyfile(out, os.path.join(DIST, f"PoruwaCut_{ver}.ccx"))
print("ccx:", out, f"(v{ver})")
