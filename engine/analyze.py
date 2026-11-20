"""Per-camera visual analysis, done segment by segment (chunks) and cached on disk.

Metrics are computed on the camera's own file-time grid (j / fps) so the cache stays
valid if the sync offset or analysed range changes later.
"""
from __future__ import annotations

import hashlib
import math
import os
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from .faces import FaceDetector, FaceRecognizer, face_features, smile_and_hug
from .media import iter_frames, probe

METRICS = ["sharp", "bright", "clip_hi", "clip_lo", "dx", "dy", "phase_resp",
           "local_change", "edges", "faces", "face_area", "face_central", "face_score", "pair_central",
           "smile", "hug", "couple", "walk", "talk"]

ANALYSIS_WIDTH = 480
SMALL_WIDTH = 160
VERSION = "6"
# the couple's face embeddings (found by find_couple before the analysis), used for the "couple" metric
COUPLE = {"centroids": None, "key": ""}


def set_couple(centroids):
    COUPLE["centroids"] = None if centroids is None or len(centroids) == 0 else np.asarray(centroids, np.float32)
    COUPLE["key"] = "" if COUPLE["centroids"] is None else hashlib.sha1(COUPLE["centroids"].tobytes()).hexdigest()[:8]
CHUNK_LEN = 60.0  # seconds per analysed segment (smaller = smoother live progress)


def _cache_path(cache_dir: str, path: str, chunk_start: float, chunk_len: float, fps: float) -> str:
    st = os.stat(path)
    key = f"{VERSION}|{COUPLE['key']}|{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{chunk_start}|{chunk_len}|{fps}"
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    base = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(cache_dir, f"{base}_{int(chunk_start):06d}_{h}.npz")


def _analyze_chunk(path: str, start: float, length: float, fps: float, use_gpu: bool, tick=None) -> dict:
    n = int(round(length * fps))
    out = {m: np.full(n, np.nan, np.float32) for m in METRICS}
    det = FaceDetector(use_gpu)
    cents = COUPLE["centroids"]
    rec = FaceRecognizer() if cents is not None else None
    prev_small = None
    win = None
    last_face = None
    mouth_box = None      # mouth area of the biggest face (from the last detection)
    prev_mouth = None
    bg = None          # slow background of a still camera, to see someone walking in front of it
    bg_busy = 0
    bg_age = 0
    for t, frame in iter_frames(path, start, length, fps, ANALYSIS_WIDTH, use_gpu):
        k = int(round((t - start) * fps))
        if k >= n:
            break
        if tick:
            tick(1.0 / fps)
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # sharpness: Laplacian variance in the centre 60% (where the subject usually is)
        y0, y1, x0, x1 = int(h * .2), int(h * .8), int(w * .2), int(w * .8)
        lap = cv2.Laplacian(gray[y0:y1, x0:x1], cv2.CV_32F)
        out["sharp"][k] = math.log10(float(lap.var()) + 1.0)

        out["bright"][k] = gray.mean() / 255.0
        out["clip_hi"][k] = float((gray > 245).mean())
        out["clip_lo"][k] = float((gray < 10).mean())

        sh = int(round(h * SMALL_WIDTH / w))
        small = cv2.resize(gray, (SMALL_WIDTH, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
        out["edges"][k] = float((cv2.Canny(small.astype(np.uint8), 60, 140) > 0).mean())
        if prev_small is not None:
            if win is None:
                win = cv2.createHanningWindow((SMALL_WIDTH, sh), cv2.CV_32F)
            (dx, dy), resp = cv2.phaseCorrelate(prev_small, small, win)
            out["dx"][k] = dx / SMALL_WIDTH
            out["dy"][k] = dy / SMALL_WIDTH
            out["phase_resp"][k] = resp
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            warped = cv2.warpAffine(prev_small, M, (SMALL_WIDTH, sh), borderMode=cv2.BORDER_REPLICATE)
            out["local_change"][k] = float(np.abs(warped[4:-4, 4:-4] - small[4:-4, 4:-4]).mean() / 255.0)
        # walk-by: a big blob that differs from this camera's background while the camera holds still
        # (photographer / videographer / guest passing right in front of the lens)
        moving = prev_small is not None and abs(out["dx"][k] * SMALL_WIDTH) + abs(out["dy"][k] * SMALL_WIDTH) > 1.5
        if bg is None or moving or bg_age < 3:
            bg = small.copy() if (bg is None or moving) else 0.5 * bg + 0.5 * small
            bg_busy = 0
            bg_age = 0 if moving or bg_age is None else bg_age + 1
            out["walk"][k] = 0.0
        else:
            mask = (np.abs(small - bg) > 35).astype(np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            nlab, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
            big = float(stats[1:, cv2.CC_STAT_AREA].max()) / mask.size if nlab > 1 else 0.0
            out["walk"][k] = big
            if big < 0.12:
                bg = 0.9 * bg + 0.1 * small
                bg_busy = 0
            else:
                bg_busy += 1
                bg = 0.99 * bg + 0.01 * small
                if bg_busy > fps * 6 or (big > 0.6 and bg_busy > fps * 1.5):   # it stayed: a new scene / light change
                    bg, bg_busy = small.copy(), 0
        prev_small = small

        if k % 2 == 0 or last_face is None:  # faces change slowly: detect on every 2nd frame
            full = det.detect_full(frame) if det.kind == "yunet" else None
            ff = face_features(det.detect(frame, full), w, h)
            sm, hg = smile_and_hug(full, w, h) if full is not None else (0.0, 0.0)
            cp = 0.0
            if rec is not None and rec.ok and full is not None and len(full):
                big = full[np.argsort(-(full[:, 2] * full[:, 3]))[:3]]
                hits = set()
                for row in big:
                    if row[2] * row[3] < 0.003 * w * h:
                        continue
                    e = rec.embed(frame, row)
                    if e is None:
                        continue
                    sim = cents @ e
                    if sim.max() > 0.40:
                        hits.add(int(sim.argmax()))
                cp = float(len(hits))
            last_face = (*ff, sm, hg, cp)
            mouth_box = None
            if full is not None and len(full):
                f = full[np.argsort(-(full[:, 2] * full[:, 3]))[0]]
                if f[2] * f[3] > 0.003 * w * h:
                    mx0, mx1 = min(f[10], f[12]), max(f[10], f[12])
                    mw = max(4.0, mx1 - mx0)
                    ny, my = f[9], (f[11] + f[13]) / 2
                    x0, x1 = int(max(0, mx0 - 0.3 * mw)), int(min(w, mx1 + 0.3 * mw))
                    y0, y1 = int(max(0, ny + 0.3 * (my - ny))), int(min(h, my + 0.8 * max(4.0, my - ny)))
                    if x1 - x0 >= 6 and y1 - y0 >= 6:
                        mouth_box = (x0, y0, x1, y1)
        # talking: the biggest face's mouth area changes more than the frame as a whole
        if mouth_box is not None:
            x0, y0, x1, y1 = mouth_box
            crop = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
            crop = cv2.resize(crop, (24, 16))
            if prev_mouth is not None:
                out["talk"][k] = max(0.0, float(np.abs(crop - prev_mouth).mean() / 255.0) - out["local_change"][k])
            prev_mouth = crop
        else:
            prev_mouth = None
        c, area, central, score, pair, sm, hg, cp = last_face
        out["smile"][k], out["hug"][k], out["couple"][k] = sm, hg, cp
        out["faces"][k], out["face_area"][k] = c, area
        out["face_central"][k], out["face_score"][k] = central, score
        out["pair_central"][k] = pair
    out["_detector"] = np.array(det.kind)
    return out


def analyze_file(path: str, f_start: float, f_end: float, fps: float, cache_dir: str,
                 chunk_len: float = CHUNK_LEN, use_gpu: bool = True, progress=None, tick=None) -> dict:
    """Metrics on grid times j/fps for f_start <= j/fps < f_end (file time)."""
    os.makedirs(cache_dir, exist_ok=True)
    info = probe(path)
    f_start, f_end = max(0.0, f_start), min(info.duration, f_end)
    j0, j1 = int(math.ceil(f_start * fps)), int(math.floor(f_end * fps))
    if j1 <= j0:
        return {"times": np.zeros(0, np.float32), **{m: np.zeros(0, np.float32) for m in METRICS}}
    c0 = int(f_start // chunk_len)
    c1 = int(math.ceil(f_end / chunk_len))
    parts = []
    for ci in range(c0, c1):
        cs = ci * chunk_len
        cl = min(chunk_len, info.duration - cs)
        if cl <= 0:
            break
        cp = _cache_path(cache_dir, path, cs, chunk_len, fps)
        d = None
        if os.path.exists(cp):
            try:
                d = dict(np.load(cp))
                if tick:
                    tick(cl)  # cached segment counts as done
            except Exception:  # noqa: BLE001  half-written cache (crash / power cut) -> analyze again
                d = None
                try:
                    os.remove(cp)
                except OSError:
                    pass
        if d is None:
            d = _analyze_chunk(path, cs, cl, fps, use_gpu, tick)
            tmp = cp + ".part.npz"
            try:
                np.savez_compressed(tmp, **d)
                os.replace(tmp, cp)      # atomic: a crash never leaves a broken cache file
            except OSError:
                pass
        parts.append((ci, d))
        if progress:
            progress(path, ci - c0 + 1, c1 - c0)
    # stitch
    total = {m: [] for m in METRICS}
    for ci, d in parts:
        for m in METRICS:
            total[m].append(d[m] if m in d else np.full(len(d["sharp"]), np.nan, np.float32))
    full = {m: np.concatenate(total[m]) for m in METRICS}
    base_j = int(round(c0 * chunk_len * fps))
    s, e = j0 - base_j, j1 - base_j
    res = {m: full[m][s:e] for m in METRICS}
    res["times"] = (np.arange(j0, j0 + len(res["sharp"])) / fps).astype(np.float64)
    res["detector"] = str(parts[0][1].get("_detector", "?")) if parts else "?"
    return res


def analyze_cameras(cams: list[dict], t0: float, t1: float, fps: float, cache_dir: str,
                    use_gpu: bool = True, workers: int = 2, log=print, progress=None) -> dict:
    """Analyse all cameras over master-timeline range [t0, t1).

    cams: [{"clips": [{"path", "offset", "tl_start", "tl_end"}]}], offset = master_time - file_time.
    Returns {"times": master grid, "cams": [metrics aligned to grid (NaN where cam not recording)]}.
    """
    n = int(math.floor((t1 - t0) * fps))
    grid = t0 + np.arange(n) / fps
    aligned = [{m: np.full(n, np.nan, np.float32) for m in METRICS} for _ in cams]
    jobs = []
    for i, cam in enumerate(cams):
        for cl in cam["clips"]:
            a, b = max(t0, cl["tl_start"]), min(t1, cl["tl_end"])
            if b - a > 0.5:
                jobs.append((i, cl, a, b))
    done = [0]
    # progress in "seconds of footage analysed" (whole 5-min chunks are decoded, so count chunk spans)
    import threading as _th
    _lk = _th.Lock()
    total = 0.0
    for i, cl, a, b in jobs:
        fa, fb = a - cl["offset"], b - cl["offset"]
        cs = math.floor(max(0.0, fa) / CHUNK_LEN) * CHUNK_LEN
        ce = min(math.ceil(fb / CHUNK_LEN) * CHUNK_LEN, cl.get("duration", fb) or fb)
        total += max(1e-3, ce - cs)
    acc = [0.0]

    def tick(sec):
        with _lk:
            acc[0] += sec
            frac = min(1.0, acc[0] / total)
        if progress:
            progress(frac)

    def work(job):
        i, cl, a, b = job
        src = cl.get("proxy") if cl.get("proxy") and os.path.exists(cl["proxy"]) else cl["path"]
        r = analyze_file(src, a - cl["offset"], b - cl["offset"], fps, cache_dir,
                         use_gpu=use_gpu, tick=tick)
        return i, cl, r

    detectors = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for i, cl, r in ex.map(work, jobs):
            done[0] += 1
            log(f"  clip {done[0]}/{len(jobs)} ඉවරයි: cam{i + 1} {os.path.basename(cl['path'])}")
            detectors[i] = r.get("detector", "?")
            if not len(r["times"]):
                continue
            # for every master-grid sample, take the nearest analysed frame of this clip
            j = np.floor((grid - cl["offset"]) * fps + 0.5).astype(np.int64)
            j -= int(round(r["times"][0] * fps))
            ok = (j >= 0) & (j < len(r["times"]))
            ok &= (grid >= cl["tl_start"]) & (grid < cl["tl_end"])
            for m in METRICS:
                aligned[i][m][ok] = r[m][j[ok]]
    for i in range(len(cams)):
        aligned[i]["detector"] = detectors.get(i, "?")
    return {"times": grid, "fps": fps, "cams": aligned}


def find_couple(cams: list[dict], t0: float, t1: float, use_gpu: bool = True, per_cam: int = 60,
                log=print, progress=None):
    """Sample frames across all cameras, embed the faces, and return the two face identities that are
    seen most and most centrally (the bride and groom). None when the recognition model is missing."""
    rec = FaceRecognizer()
    if not rec.ok:
        return None
    det = FaceDetector(use_gpu)
    if det.kind != "yunet":
        return None
    embs, weights = [], []
    from .media import grab_frame
    # a few dozen single frames per camera, spread over the range (fast seeks, no full decode)
    samples = []
    for cam in cams:
        span = [(cl, max(t0, cl["tl_start"]), min(t1, cl["tl_end"])) for cl in cam["clips"]]
        span = [s_ for s_ in span if s_[2] - s_[1] > 2]
        tot = sum(b - a for _, a, b in span)
        if tot <= 0:
            continue
        step = max(3.0, tot / per_cam)
        for cl, a, b in span:
            t = a + step / 2
            while t < b:
                samples.append((cl, t))
                t += step
    jobs = samples
    for n, (cl, tt_) in enumerate(jobs):
        try:
            src = cl.get("proxy") if cl.get("proxy") and os.path.exists(cl["proxy"]) else cl["path"]
            frame = grab_frame(src, tt_ - cl["offset"], ANALYSIS_WIDTH)
            if frame is not None:
                h, w = frame.shape[:2]
                full = det.detect_full(frame)
                for row in full[np.argsort(-(full[:, 2] * full[:, 3]))[:3]] if len(full) else []:
                    if row[2] * row[3] < 0.004 * w * h:
                        continue
                    e = rec.embed(frame, row)
                    if e is None:
                        continue
                    cx = (row[0] + row[2] / 2) / w
                    embs.append(e)
                    weights.append((1.2 - abs(cx - 0.5)) * min(1.0, row[2] * row[3] / (0.02 * w * h)))
        except Exception:  # noqa: BLE001
            continue
        if progress:
            progress((n + 1) / len(jobs))
    if len(embs) < 12:
        return None
    E = np.stack(embs)
    W = np.asarray(weights, np.float32)
    # greedy clustering by cosine similarity
    centers, mass = [], []
    for e, wt in sorted(zip(E, W), key=lambda x: -x[1]):
        if centers:
            sims = np.array([c @ e / (np.linalg.norm(c) + 1e-9) for c in centers])
            i = int(sims.argmax())
            if sims[i] > 0.42:
                centers[i] = centers[i] + wt * e
                mass[i] += wt
                continue
        centers.append(wt * e)
        mass.append(wt)
    order = np.argsort(-np.asarray(mass))[:2]
    if len(order) < 2 or mass[order[1]] < 0.25 * mass[order[0]]:
        order = order[:1]
    cents = [centers[i] / (np.linalg.norm(centers[i]) + 1e-9) for i in order]
    log(f"  couple recognition: faces {len(embs)} · identities {len(centers)} · couple {len(cents)}")
    return np.stack(cents)
