"""Entry point for the PoruwaCutEngine.exe build (PyInstaller).

No arguments -> run the local engine server (what the installer starts at login).
Any arguments -> the normal command line (sync / edit / auto / serve).
"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from engine.server import _ensure_streams
    _ensure_streams()
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    if sys.argv[1] in ("--version", "-V"):
        from engine import __version__
        print(__version__)
        sys.exit(0)
    from engine.cli import main
    sys.exit(main())
