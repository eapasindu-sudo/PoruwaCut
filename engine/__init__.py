"""PoruwaCut - Sri Lankan wedding multicam auto-edit engine."""
import os as _os

try:  # stamped by the release build
    from ._version import __version__  # type: ignore
except Exception:  # source checkout: read ../VERSION
    try:
        with open(_os.path.join(_os.path.dirname(__file__), "..", "VERSION"), encoding="utf-8") as _f:
            __version__ = _f.read().strip()
    except Exception:
        __version__ = "0.0.0-dev"

# GitHub repository whose Releases carry PoruwaCut_Setup_<ver>.exe (used for auto-update)
UPDATE_REPO = "eapasindu-sudo/PoruwaCut"
