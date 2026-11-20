"""AutoWed licence: yearly key (1 PC) or a 7-day trial, from the Endora licence server.

The server (endoraweddingfilms.com, WordPress plugin "AutoWed License Server") signs a short token
with its Ed25519 key; the engine checks the signature with the public key below, so a token cannot be
made or changed by hand. A token is good offline for up to 14 days (or until the licence / trial ends);
the engine renews it in the background when it can reach the server. No valid token -> no edits.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import threading
import time
import urllib.request

from .ed25519v import verify

SERVER = "https://www.endoraweddingfilms.com/wp-json/autowed/v1"
PUBLIC_KEY = bytes.fromhex("257429dcd7b2bda8f87aa98ead187a063e1f4c2b838a3c3f172841197ee5ed80")
PRICE = "LKR 10,000 / year (updates included)"
RENEW_BEFORE = 4 * 86400          # renew when less than 4 days of offline time are left

_lock = threading.RLock()
_state: dict = {}


def _data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "PoruwaCut")
    os.makedirs(d, exist_ok=True)
    return d


LIC_FILE = os.path.join(_data_dir(), "license.json")


def machine_id() -> str:
    """Stable id of this PC (Windows MachineGuid, hashed; elsewhere the MAC address)."""
    raw = ""
    try:
        import winreg  # type: ignore
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                           winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0))
        raw = winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                raw = open(f).read().strip()
                break
            except Exception:
                pass
        if not raw:
            raw = platform.node()
    return hashlib.sha256(("autowed|" + raw).encode()).hexdigest()[:40]


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_token(tok: str) -> dict | None:
    """Payload of a token signed by the server for this PC, or None."""
    try:
        p, s = tok.split(".")
        payload, sig = _b64d(p), _b64d(s)
        if not verify(PUBLIC_KEY, payload, sig):
            return None
        d = json.loads(payload)
        if d.get("m") != machine_id():
            return None
        return d
    except Exception:
        return None


def _load() -> dict:
    try:
        with open(LIC_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        with open(LIC_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def _post(route: str, body: dict, timeout: float = 15) -> tuple[int, dict]:
    req = urllib.request.Request(f"{SERVER}/{route}", data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "AutoWed"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:   # type: ignore[attr-defined]
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {"ok": False, "message": str(e)}
    except Exception as e:
        return 0, {"ok": False, "error": "offline", "message": f"License server එකට යන්න බැරි වුණා ({e})."}


def _version() -> str:
    try:
        from . import __version__  # type: ignore
        return str(__version__)
    except Exception:
        return ""


def status() -> dict:
    """{ok, plan, key (masked), expires, offline_until, days_left, message}"""
    with _lock:
        d = _load()
        tok = parse_token(d.get("token", "")) if d.get("token") else None
        now = time.time()
        if not tok:
            msg = _state.get("message") or "License එකක් නැහැ. License key එක දාන්න, නැත්නම් free trial එක පටන් ගන්න."
            return {"ok": False, "plan": None, "message": msg, "price": PRICE, "machine": machine_id()[:8]}
        lic_end, tok_end = float(tok["e"]), float(tok["x"])
        ok = now < tok_end and now < lic_end
        key = tok.get("k", "")
        out = {"ok": ok, "plan": tok.get("p"), "key": key if key == "TRIAL" else key[:3] + "****-****-" + key[-4:],
               "expires": time.strftime("%Y-%m-%d", time.localtime(lic_end)),
               "offline_until": time.strftime("%Y-%m-%d", time.localtime(tok_end)),
               "days_left": max(0, int((lic_end - now) // 86400)), "price": PRICE, "machine": machine_id()[:8],
               "name": d.get("name", "")}
        if not ok:
            if now >= lic_end:
                out["message"] = ("Free trial එක ඉවරයි." if tok.get("p") == "trial" else "License එක expire වුණා.") + \
                    f" License key එකක් ගන්න / renew කරන්න: {PRICE}"
            else:
                out["message"] = "License එක check කරන්න internet ඕන (offline කාලය ඉවරයි). Internet connect කරලා ආයෙ try කරන්න."
        return out


def is_valid() -> bool:
    return bool(status().get("ok"))


def activate(key: str) -> dict:
    key = (key or "").strip().upper()
    code, r = _post("activate", {"key": key, "machine": machine_id(), "pc_name": platform.node(), "version": _version()})
    if r.get("ok") and parse_token(r.get("token", "")):
        with _lock:
            _save({"token": r["token"], "key": key, "name": r.get("name", "")})
            _state.pop("message", None)
        return {"ok": True, **status()}
    return {"ok": False, "message": r.get("message") or f"Activate කරන්න බැරි වුණා ({code})."}


def start_trial() -> dict:
    code, r = _post("trial", {"machine": machine_id(), "pc_name": platform.node()})
    if r.get("ok") and parse_token(r.get("token", "")):
        with _lock:
            _save({"token": r["token"], "key": "TRIAL"})
        return {"ok": True, **status()}
    return {"ok": False, "message": r.get("message") or f"Trial එක පටන් ගන්න බැරි වුණා ({code})."}


def refresh(force: bool = False) -> dict:
    """Renew the token from the server when it gets close to its offline limit."""
    with _lock:
        d = _load()
    tok = parse_token(d.get("token", "")) if d.get("token") else None
    if not tok:
        return status()
    if not force and float(tok["x"]) - time.time() > RENEW_BEFORE:
        return status()
    if tok.get("p") == "trial":
        code, r = _post("trial", {"machine": machine_id(), "pc_name": platform.node()})
    else:
        code, r = _post("check", {"key": d.get("key", tok.get("k")), "machine": machine_id(),
                                  "pc_name": platform.node(), "version": _version()})
    if r.get("ok") and parse_token(r.get("token", "")):
        with _lock:
            d["token"] = r["token"]
            if r.get("name"):
                d["name"] = r["name"]
            _save(d)
            _state.pop("message", None)
    elif code in (402, 403, 404, 409):     # expired / blocked / unknown / other PC: stop now
        with _lock:
            _save({})
            _state["message"] = r.get("message") or "License එක වලංගු නැහැ."
    return status()


def remove() -> dict:
    with _lock:
        _save({})
    return status()


def start_background_refresh() -> None:
    def loop():
        while True:
            try:
                refresh()
            except Exception:
                pass
            time.sleep(6 * 3600)
    threading.Thread(target=loop, daemon=True).start()
