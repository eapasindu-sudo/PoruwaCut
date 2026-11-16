"""Set the version everywhere from a tag like v0.2.0 (or from VERSION when no argument).

    python build/stamp_version.py v0.2.0
"""
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
arg = sys.argv[1] if len(sys.argv) > 1 else open(os.path.join(ROOT, "VERSION")).read()
m = re.search(r"(\d+)\.(\d+)\.(\d+)", arg)
if not m:
    raise SystemExit(f"bad version: {arg!r}")
ver = ".".join(m.groups())

with open(os.path.join(ROOT, "VERSION"), "w") as f:
    f.write(ver + "\n")
with open(os.path.join(ROOT, "engine", "_version.py"), "w") as f:
    f.write(f'__version__ = "{ver}"\n')
mp = os.path.join(ROOT, "premiere-uxp", "manifest.json")
man = json.load(open(mp, encoding="utf-8"))
man["version"] = ver
json.dump(man, open(mp, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
gh = os.environ.get("GITHUB_OUTPUT")
if gh:
    with open(gh, "a") as f:
        f.write(f"version={ver}\n")
print(ver)
