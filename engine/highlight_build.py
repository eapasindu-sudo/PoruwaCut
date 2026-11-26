"""Endora Highlights - phases 2-4: build the highlight from the analysis.

  plan()   moments + song -> a shot list cut on the beat, told as a story
           (intro details -> poruwa -> speech lines -> celebration -> ending on the couple),
           speech lines from the speeches with the music ducked under them,
           rendered audio: music_mix.wav (ducked music) + speech.wav (levelled speech lines)
  learn()  a finished highlight timeline -> the editor's style (shot length per song energy,
           slow-motion share) saved and used by plan() next time
"""
from __future__ import annotations

import json
import os
import time

import numpy as np

from .media import extract_audio
from .paths import DATA_DIR

SR_OUT = 48000
STYLE_FILE = os.path.join(DATA_DIR, "highlight_style.json")
# shot length in beats for each song energy (changed by learn())
DEFAULT_STYLE = {"beats": {"low": 8, "mid": 4, "high": 2, "peak": 2}, "slowmo": 0.35, "n": 0}


def load_style() -> dict:
    try:
        with open(STYLE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        out = json.loads(json.dumps(DEFAULT_STYLE))
        out["beats"].update({k: float(v) for k, v in (s.get("beats") or {}).items()})
        out["slowmo"] = float(s.get("slowmo", out["slowmo"]))
        out["n"] = int(s.get("n", 0))
        return out
    except Exception:  # noqa: BLE001
        return json.loads(json.dumps(DEFAULT_STYLE))


# ---------------------------------------------------------------- speech lines -------------------
def speech_lines(clips: list[dict], groups: list[dict], an_faces: dict | None = None, max_n: int = 10, log=print):
    """Candidate lines from the speeches: one sentence (2.5-8 s) that the room answers (applause /
    laughter) or that is clear and complete. Sinhala or English - it listens to the voice, not the words."""
    from . import vad
    from .events_detect import KINDS, classify
    sp_ids = {cid for g in groups if g["kind"] == "speech" for cid in g["clips"]}
    cands = []
    for c in clips:
        if c["id"] not in sp_ids:
            continue
        try:
            x = extract_audio(c["path"], rate=16000)
        except Exception:  # noqa: BLE001
            continue
        if len(x) < 16000 * 4:
            continue
        p = vad.speech_prob(x)
        if p is None:
            continue
        segs = vad.speech_segments(p, min_silence=0.35)
        lab = classify(x, 16000)
        app = KINDS.index("applause")
        rms = lambda a, b: float(np.sqrt(np.mean(x[int(a * 16000):int(b * 16000)] ** 2) + 1e-12))  # noqa: E731
        # join pieces of one sentence (pauses < 0.5 s), keep 2.5..9 s
        i = 0
        while i < len(segs):
            a, b = segs[i]
            j = i
            while j + 1 < len(segs) and segs[j + 1][0] - b < 0.5 and segs[j + 1][1] - a <= 9.0:
                j += 1
                b = segs[j][1]
            i = j + 1
            d = b - a
            if d < 2.5 or d > 9.0:
                continue
            after = lab[int(b):int(b) + 4] if len(lab) else []
            cheer = float(np.mean(np.asarray(after) == app)) if len(after) else 0.0
            # clear: speech much louder than the room just before and after
            room = max(rms(max(0, a - 1.0), a), rms(b, b + 1.0), 1e-6)
            snr = 20 * np.log10(rms(a, b) / room)
            pause_after = (segs[j + 1][0] - b) if j + 1 < len(segs) else 2.0
            score = 2.0 * cheer + 0.04 * np.clip(snr, 0, 20) + 0.3 * min(pause_after, 1.5) - 0.15 * abs(d - 5.0)
            cands.append({"clip": c["id"], "path": c["path"], "name": c["name"], "start": round(a, 2),
                          "end": round(b, 2), "score": round(float(score), 3), "cheer": round(cheer, 2),
                          "has_video": c.get("w", 1) > 0})
    cands.sort(key=lambda m: -m["score"])
    # different moments: at most 2 per clip, 20 s apart
    out = []
    for m in cands:
        same = [o for o in out if o["clip"] == m["clip"]]
        if len(same) >= 2 or any(abs(o["start"] - m["start"]) < 20 for o in same):
            continue
        out.append(m)
        if len(out) >= max_n:
            break
    log(f"  speech lines: candidates {len(cands)} · තෝරපු {len(out)}")
    return out


# ---------------------------------------------------------------- slots on the song --------------
def _slots(song: dict, style: dict) -> list[dict]:
    beats = np.asarray(song["beats"], np.float64)
    end = float(song["end"])
    if len(beats) < 4:
        step = 2.5
        return [{"t0": t, "t1": min(end, t + step), "energy": "mid"} for t in np.arange(0, end - 0.5, step)]
    period = float(np.median(np.diff(beats)))
    slots = []
    secs = song["sections"] or [{"start": 0.0, "end": end, "energy": "mid"}]
    # before the first section (pickup): one slot
    if secs[0]["start"] > 0.6:
        slots.append({"t0": 0.0, "t1": secs[0]["start"], "energy": "low"})
    for s in secs:
        nb = float(style["beats"].get(s["energy"], 4))
        while nb * period < 1.0:         # never shorter than 1 s
            nb *= 2
        while nb * period > 6.0 and nb > 1:
            nb /= 2
        bs = beats[(beats >= s["start"] - 1e-3) & (beats < s["end"] - 1e-3)]
        if not len(bs):
            continue
        pts = list(bs[::max(1, int(round(nb)))]) + [s["end"]]
        if pts[0] > s["start"] + 0.05:
            pts = [s["start"]] + pts
        for a, b in zip(pts[:-1], pts[1:]):
            if b - a >= 0.5:
                slots.append({"t0": float(a), "t1": float(b), "energy": s["energy"]})
            elif slots:
                slots[-1]["t1"] = float(b)
    if slots and slots[-1]["t1"] < end - 0.05:
        slots[-1]["t1"] = end
    return slots


# ---------------------------------------------------------------- plan ---------------------------
CHAPTERS = ("intro", "ceremony", "celebration", "ending")


def plan(body: dict, out_dir: str, log=print, progress=None) -> dict:
    an = body["analysis"]
    song = an.get("song")
    if not song or not song.get("sections"):
        raise ValueError("Highlight timeline එකේ සින්දුවක් නැහැ: සින්දුව audio track එකට දාලා ආයෙ analyze කරන්න.")
    style = load_style()
    excl = set(body.get("exclude_groups") or [])
    clips = {c["id"]: c for c in an["clips"]}
    groups = [g for g in an["groups"]]
    gkind = {g["id"]: g["kind"] for g in groups}
    gorder = {g["id"]: i for i, g in enumerate(sorted(groups, key=lambda g: g.get("start") or 0))}
    moments = [m for m in an["moments"] if m.get("group") not in excl]
    if len(moments) < 5:
        raise ValueError("Highlight එකට moments මදි: තව clips / කොටස් තෝරන්න.")
    tl_fps = float(an.get("fps") or 25)
    T = float(song["end"])
    slots = _slots(song, style)
    log(f"Highlight: {T / 60:.1f} min · shots {len(slots)} · style " +
        ", ".join(f"{k} {v:g} beats" for k, v in style["beats"].items()) + (f" (ඔබේ edits {style['n']}කින්)" if style["n"] else ""))

    # chapters on the song: intro = first section (max 15%), ending = last section (max 15%),
    # ceremony then celebration in between, split where the section energy rises the most
    secs = song["sections"]
    intro_end = min(secs[0]["end"], 0.15 * T) if len(secs) > 1 else 0.12 * T
    end_start = max(secs[-1]["start"], 0.85 * T) if len(secs) > 1 else 0.88 * T
    has_party = any(gkind.get(m.get("group")) == "party" for m in moments)
    mid_split = intro_end + (end_start - intro_end) * (0.55 if has_party else 0.7)
    for s in secs:   # move the split to the start of the loudest part in the middle
        if s["energy"] == "peak" and intro_end < s["start"] < end_start:
            mid_split = s["start"]
            break

    def chapter(t):
        return "intro" if t < intro_end else ("ending" if t >= end_start else ("ceremony" if t < mid_split else "celebration"))

    def pool(ch):
        def ok(m):
            k = gkind.get(m.get("group"))
            tg = m["tags"]
            if ch == "intro":
                return k in ("prep", "outdoor", "other") or bool({"detail", "drone"} & set(tg))
            if ch == "ceremony":
                return k == "poruwa" or bool({"chant", "drums"} & set(tg))
            if ch == "celebration":
                return k in ("party", "speech", "other", "outdoor") or bool({"smile", "hug", "applause"} & set(tg))
            return bool({"couple", "smile", "hug", "slowmo", "glide"} & set(tg))
        p = [m for m in moments if ok(m)]
        return p if len(p) >= 3 else moments

    pools = {ch: pool(ch) for ch in CHAPTERS}
    in_ch = {ch: {id(m) for m in pools[ch]} for ch in CHAPTERS}
    used = {}            # clip -> [(a, b)] source ranges already in the highlight
    used_abs = []        # wall-clock time of every shot used (same instant, other camera = repeat)

    def cand_list(ch):
        # the chapter's own moments first; when they run out, the best of the rest of the day
        p = pools[ch]
        return p if p is moments else p + [m for m in moments if m not in p]

    def free(cid, a, b):
        for x, y in used.get(cid, []):
            if min(b, y) - max(a, x) > 0.3 * (b - a):
                return False
        return True

    # ---- speech lines (phase 3): placed in the quiet / low parts, the music ducks under them
    bites = []
    if body.get("speech", True):
        try:
            cand = speech_lines([c for c in an["clips"]], groups, log=log)
        except Exception as e:  # noqa: BLE001
            cand = []
            log(f"  speech lines මඟ හැරියා: {e}")
        want = max(0, min(4, int(T / 75)))
        lows = [s for s in secs if s["energy"] in ("low", "mid") and s["end"] - s["start"] >= 6]
        for m in sorted(cand[:want * 2], key=lambda m: (gorder.get(next((g["id"] for g in groups if m["clip"] in g["clips"]), ""), 0), m["start"])):
            if len(bites) >= want:
                break
            d = m["end"] - m["start"]
            for s in lows:
                at = next((b for b in song["downbeats"] if s["start"] - 1e-3 <= b <= s["end"] - d), None)
                if at is None:
                    continue
                at = max(at, intro_end * 0.5)
                if all(abs(at - b["tl"]) > 25 and not (at < b["tl"] + b["dur"] + 2 and b["tl"] < at + d + 2) for b in bites) \
                        and at + d < T - 3:
                    bites.append({**m, "tl": float(at), "dur": float(d)})
                    break
        bites.sort(key=lambda b: b["tl"])
        log(f"  speech lines timeline එකට: {len(bites)}")

    def in_bite(t0, t1):
        return next((b for b in bites if t0 < b["tl"] + b["dur"] and b["tl"] < t1), None)

    # ---- fill every slot with the best moment that fits (energy, story, variety, unused)
    shots = []
    prev = None
    slow_n = 0
    for i, sl in enumerate(slots):
        if progress and i % 10 == 0:
            progress("plan", 0.1 + 0.5 * i / max(1, len(slots)), i / max(1, len(slots)))
        d = sl["t1"] - sl["t0"]
        ch = chapter(sl["t0"])
        b = in_bite(sl["t0"], sl["t1"])
        if b is not None and b.get("has_video") and clips.get(b["clip"], {}).get("w", 1) > 0:
            # show the speaker while the line plays (the same moment of the same clip)
            off = sl["t0"] - b["tl"]
            a = max(0.0, b["start"] + off)          # in sync with the line (lips match the words)
            shots.append({"clip": b["clip"], "path": b["path"], "src_in": round(a, 3), "src_out": round(a + d, 3),
                          "tl_in": sl["t0"], "tl_out": sl["t1"], "slow": 1.0, "chapter": "speech",
                          "why": "speech line", "energy": sl["energy"]})
            prev = {"clip": b["clip"], "size": None}
            continue
        best, bs = None, -1e9
        for m in cand_list(ch):
            c = clips.get(m["clip"])
            if not c:
                continue
            slow = c.get("slowmo", 1.0) if (c.get("slowmo", 1.0) > 1 and style["slowmo"] > 0.05) else 1.0
            need = d / slow
            mid = (m["start"] + m["end"]) / 2
            a = max(0.0, min(c["dur"] - need, mid - need / 2))
            if need > c["dur"]:
                continue
            if not free(m["clip"], a, a + need):
                # the moment's middle is used: another free part of the same good stretch
                alt = [x for x in (m["start"], m["end"] - need, m["start"] - need / 2, m["end"] - need / 2)
                       if 0 <= x <= c["dur"] - need and free(m["clip"], x, x + need)]
                if not alt:
                    continue
                a = alt[0]
            tg = set(m["tags"])
            fit = m["score"] - (0.8 if id(m) not in in_ch[ch] else 0.0)
            if sl["energy"] == "low":
                fit += 0.8 * (slow > 1) + 0.4 * ("glide" in tg) + 0.4 * ("detail" in tg) + 0.3 * ("drone" in tg) + 0.2 * ("wide" in tg)
            elif sl["energy"] in ("high", "peak"):
                fit += 0.6 * ("hug" in tg) + 0.5 * ("smile" in tg) + 0.4 * ("applause" in tg) + 0.2 * ("close" in tg) - 0.3 * (slow > 1)
            else:
                fit += 0.3 * ("couple" in tg) + 0.2 * (slow > 1)
            # slow motion share near the editor's own (learn())
            if slow > 1 and slow_n > style["slowmo"] * (len(shots) + 1):
                fit -= 0.6
            if ch == "ending":
                fit += 0.5 * ("couple" in tg) + 0.3 * (slow > 1)
            if prev and prev["clip"] == m["clip"]:
                fit -= 2.0
            # the same instant from another camera was just used: a repeat, not a new moment
            if c.get("time"):
                at = c["time"] + a
                if any(abs(at - u) < 6 for u in used_abs[-12:]):
                    fit -= 1.2
            if prev and prev.get("size") and prev["size"] == m.get("size"):
                fit -= 0.3
            # keep the story in time order inside a chapter (soft)
            last_t = next((s_["order"] for s_ in reversed(shots) if s_.get("chapter") == ch and "order" in s_), None)
            order = gorder.get(m.get("group"), 0) * 1e6 + (clips[m["clip"]].get("time") or 0) % 1e6 + m["start"]
            if last_t is not None and order < last_t and ch in ("ceremony",):
                fit -= 0.4
            if fit > bs:
                best, bs = (m, a, need, slow, order), fit
        if best is None:
            continue
        m, a, need, slow, order = best
        used.setdefault(m["clip"], []).append((a, a + need))
        if clips[m["clip"]].get("time"):
            used_abs.append(clips[m["clip"]]["time"] + a)
        slow_n += slow > 1
        shots.append({"clip": m["clip"], "path": m["path"], "src_in": round(a, 3), "src_out": round(a + need, 3),
                      "tl_in": round(sl["t0"], 3), "tl_out": round(sl["t1"], 3), "slow": slow, "chapter": ch,
                      "why": f"{ch} · {', '.join(m['tags'][:4])} · {m['score']:.2f}", "energy": sl["energy"],
                      "moment": m.get("id"), "order": order})
        prev = {"clip": m["clip"], "size": m.get("size")}

    # ---- audio: ducked music bed + levelled speech lines (rendered, exact, offline)
    if progress:
        progress("audio", 0.7, 0.0)
    os.makedirs(out_dir, exist_ok=True)
    music = body.get("music") or []
    n = int(T * SR_OUT) + SR_OUT
    bed = np.zeros(n, np.float32)
    for mu in music:
        x = extract_audio(mu["path"], rate=SR_OUT, start=float(mu.get("in", 0.0)), duration=float(mu["dur"]))
        a = int(mu["start"] * SR_OUT)
        bed[a:a + len(x)] += x[:max(0, n - a)]
    gain = np.ones(n, np.float32)
    sp = np.zeros(n, np.float32)
    duck = 10 ** (-float(body.get("duck_db", 11)) / 20)
    ramp = int(0.45 * SR_OUT)
    for b in bites:
        x = extract_audio(b["path"], rate=SR_OUT, start=b["start"], duration=b["dur"])
        if not len(x):
            continue
        r = float(np.sqrt(np.mean(x ** 2)) + 1e-9)
        x = x * min(8.0, 0.1 / r)                    # ~ -20 dBFS RMS
        f = int(0.06 * SR_OUT)
        x[:f] *= np.linspace(0, 1, f)
        x[-f:] *= np.linspace(1, 0, f)
        a = int(b["tl"] * SR_OUT)
        sp[a:a + len(x)] += x[:max(0, n - a)]
        g0, g1 = max(0, a - ramp), min(n, a + len(x) + ramp)
        env = np.full(g1 - g0, duck, np.float32)
        env[:min(ramp, len(env))] = np.linspace(1, duck, min(ramp, len(env)))
        tail = min(ramp, len(env))
        env[-tail:] = np.minimum(env[-tail:], np.linspace(duck, 1, tail))
        gain[g0:g1] = np.minimum(gain[g0:g1], env)
    bed *= gain
    pk = float(np.max(np.abs(bed + sp))) if n else 0.0
    if pk > 0.98:
        bed *= 0.98 / pk
        sp *= 0.98 / pk
    from scipy.io import wavfile
    stamp = time.strftime("%Y%m%d-%H%M%S")
    mpath = os.path.join(out_dir, f"highlight_music_{stamp}.wav")
    spath = os.path.join(out_dir, f"highlight_speech_{stamp}.wav")
    wavfile.write(mpath, SR_OUT, (np.clip(bed, -1, 1) * 32767).astype(np.int16))
    if bites:
        wavfile.write(spath, SR_OUT, (np.clip(sp, -1, 1) * 32767).astype(np.int16))
    for s in shots:
        s.pop("order", None)
    log(f"✔ Highlight plan: shots {len(shots)} · slow-mo {sum(s['slow'] > 1 for s in shots)} · speech lines {len(bites)} · "
        f"{T / 60:.1f} min")
    return {"shots": shots, "bites": [{k: b[k] for k in ("clip", "name", "start", "end", "tl", "dur", "score", "cheer")} for b in bites],
            "music_wav": mpath, "speech_wav": spath if bites else None, "length": T, "fps": tl_fps,
            "slowmo_clips": sorted({s["clip"] for s in shots if s["slow"] > 1})}


# ---------------------------------------------------------------- learn (phase 4) ----------------
def learn(body: dict, log=print) -> dict:
    """A highlight the editor finished: shot lengths per song energy + slow-motion share -> style."""
    from .highlights import song_structure
    items = body.get("items") or []
    music = body.get("music") or []
    if len(items) < 8 or not music:
        raise ValueError("ඉගෙනගන්න ඉවර කරපු highlight timeline එකක් open කරන්න (video shots 8+ සහ සින්දුව).")
    song = song_structure(music, log=log)
    if not song or len(song["beats"]) < 8:
        raise ValueError("සින්දුවේ beat එක අල්ලගන්න බැරි වුණා.")
    period = float(np.median(np.diff(song["beats"])))
    by = {"low": [], "mid": [], "high": [], "peak": []}
    slow = 0
    for it in items:
        d = float(it["tl_out"]) - float(it["tl_in"])
        if d <= 0.2:
            continue
        t = (float(it["tl_in"]) + float(it["tl_out"])) / 2
        e = next((s["energy"] for s in song["sections"] if s["start"] <= t < s["end"]), "mid")
        by[e].append(d / period)
        tl_fps, cf = float(it.get("tl_fps") or 25), float(it.get("clip_fps") or 25)
        src = float(it.get("src_frames") or 0)
        if cf >= 1.9 * tl_fps or (src and src / max(1, d * tl_fps) < 0.75):
            slow += 1
    old = load_style()
    w_old = min(old["n"], 5)
    new = {"beats": {}, "slowmo": 0.0, "n": old["n"] + 1}
    for e, v in by.items():
        if v:
            med = float(np.median(v))
            new["beats"][e] = round((old["beats"][e] * w_old + med) / (w_old + 1), 2)
        else:
            new["beats"][e] = old["beats"][e]
    share = slow / max(1, len(items))
    new["slowmo"] = round((old["slowmo"] * w_old + share) / (w_old + 1), 3)
    os.makedirs(os.path.dirname(STYLE_FILE), exist_ok=True)
    with open(STYLE_FILE, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=1)
    log("✔ ඔබේ style එක ඉගෙනගත්තා: " + ", ".join(f"{k} {v:g} beats" for k, v in new["beats"].items())
        + f" · slow-mo {100 * new['slowmo']:.0f}% · highlights {new['n']}")
    return new
