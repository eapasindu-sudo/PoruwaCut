"""Face detection: YuNet (accurate) with Haar fallback.

GPU: when onnxruntime with CUDA or DirectML is present, YuNet and SFace run on the graphics card.
The GPU path checks itself against OpenCV on the first frames with faces and turns itself off if
the two disagree, so a driver / runtime problem can only cost speed, never wrong results.
"""
from __future__ import annotations

import os
import threading

import cv2
import numpy as np

from .paths import MODELS_DIR

YUNET_PATH = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_PATH = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")


# ---------------------------------------------------------------- GPU (onnxruntime) ----------
_GPU = {"tried": False, "det": None, "rec": None, "kind": "cpu", "state": None, "agree": 0, "lock": threading.Lock(),
        "why": ""}
_GPU_CHECK_FACES = 6        # frames with faces that must match OpenCV before the GPU is trusted alone


def _ort_sessions(use_gpu: bool):
    """(yunet session, sface session, provider name) or Nones. Made once, shared by all threads."""
    g = _GPU
    with g["lock"]:
        if g["tried"]:
            return g["det"], g["rec"], g["kind"]
        g["tried"] = True
        if not use_gpu or os.environ.get("AUTOWED_FACE_GPU", "1") == "0":
            g["why"] = "off"
            return None, None, "cpu"
        try:
            import onnxruntime as ort  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            g["why"] = f"onnxruntime නැහැ ({type(e).__name__})"
            return None, None, "cpu"
        names = {"CUDAExecutionProvider": "cuda", "DmlExecutionProvider": "directml"}
        avail = ort.get_available_providers()
        for prov in ("CUDAExecutionProvider", "DmlExecutionProvider"):
            if prov not in avail:
                continue
            try:
                so = ort.SessionOptions()
                so.log_severity_level = 3
                so.enable_mem_pattern = False                      # required by DirectML
                so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                det = ort.InferenceSession(YUNET_PATH, so, providers=[prov, "CPUExecutionProvider"]) \
                    if os.path.exists(YUNET_PATH) else None
                rec = ort.InferenceSession(SFACE_PATH, so, providers=[prov, "CPUExecutionProvider"]) \
                    if os.path.exists(SFACE_PATH) else None
                if det is None:
                    continue
                if prov not in det.get_providers():
                    continue                                       # silently fell back to CPU
                g["det"], g["rec"], g["kind"] = det, rec, names[prov]
                g["state"] = "checking"
                return det, rec, g["kind"]
            except Exception as e:  # noqa: BLE001
                g["why"] = f"{names[prov]}: {e}"[:160]
        if not g["why"]:
            g["why"] = "GPU provider නැහැ (" + ", ".join(avail) + ")"
        return None, None, "cpu"


def gpu_status() -> str:
    """For the log: which device runs the face networks."""
    g = _GPU
    if g["kind"] != "cpu" and g["state"] in ("checking", "ok"):
        return f"GPU ({g['kind']})" + (" ✔" if g["state"] == "ok" else "")
    if g["state"] == "bad":
        return "CPU (GPU එක OpenCV එක්ක එකඟ වුණේ නැහැ: " + g["why"] + ")"
    return "CPU" + (f" ({g['why']})" if g["why"] and g["why"] != "off" else "")


def _yunet_decode(outs, out_names, pw: int, ph: int, score_thr=0.6, nms_thr=0.3, top_k=50):
    """Same post-processing as cv::FaceDetectorYN (YuNet 2023mar): rows [x,y,w,h, 10 landmarks, score]."""
    o = dict(zip(out_names, outs))
    rows = []
    for s in (8, 16, 32):
        cols, rws = pw // s, ph // s
        cls = np.clip(o[f"cls_{s}"].reshape(-1), 0, 1)
        obj = np.clip(o[f"obj_{s}"].reshape(-1), 0, 1)
        score = np.sqrt(cls * obj)
        keep = np.nonzero(score >= score_thr)[0]
        if not len(keep):
            continue
        bb = o[f"bbox_{s}"].reshape(-1, 4)[keep]
        kp = o[f"kps_{s}"].reshape(-1, 10)[keep]
        c = (keep % cols).astype(np.float32)
        r = (keep // cols).astype(np.float32)
        cx, cy = (c + bb[:, 0]) * s, (r + bb[:, 1]) * s
        w, h = np.exp(bb[:, 2]) * s, np.exp(bb[:, 3]) * s
        f = np.zeros((len(keep), 15), np.float32)
        f[:, 0], f[:, 1], f[:, 2], f[:, 3] = cx - w / 2, cy - h / 2, w, h
        for n in range(5):
            f[:, 4 + 2 * n] = (kp[:, 2 * n] + c) * s
            f[:, 5 + 2 * n] = (kp[:, 2 * n + 1] + r) * s
        f[:, 14] = score[keep]
        rows.append(f)
    if not rows:
        return np.zeros((0, 15), np.float32)
    f = np.concatenate(rows)
    idx = cv2.dnn.NMSBoxes(f[:, :4].tolist(), f[:, 14].tolist(), score_thr, nms_thr, top_k=top_k)
    idx = np.array(idx).reshape(-1)
    return f[idx] if len(idx) else np.zeros((0, 15), np.float32)


def _ort_yunet(sess, bgr: np.ndarray) -> np.ndarray:
    h, w = bgr.shape[:2]
    pw, ph = ((w - 1) // 32 + 1) * 32, ((h - 1) // 32 + 1) * 32
    pad = np.zeros((ph, pw, 3), np.float32)
    pad[:h, :w] = bgr
    blob = pad.transpose(2, 0, 1)[None]
    names = [o.name for o in sess.get_outputs()]
    with _GPU["lock"]:
        outs = sess.run(names, {sess.get_inputs()[0].name: blob})
    return _yunet_decode(outs, names, pw, ph)


def _iou(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    i = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return i / max(1e-6, a[2] * a[3] + b[2] * b[3] - i)


def _same_faces(f1: np.ndarray, f2: np.ndarray) -> bool:
    """Every clear face (score >= 0.75) of f1 has a face in f2 at the same place (both are >= 0.6)."""
    for a in f1[f1[:, 14] >= 0.75]:
        if not any(_iou(a, b) > 0.75 for b in f2):
            return False
    return True


class FaceDetector:
    def __init__(self, use_gpu: bool = True):
        self.kind = "none"
        self.det = None
        self._size = None
        self.use_gpu = use_gpu
        if os.path.exists(YUNET_PATH) and hasattr(cv2, "FaceDetectorYN"):
            self.kind = "yunet"
        else:
            casc = os.path.join(getattr(getattr(cv2, "data", None), "haarcascades", ""),
                                "haarcascade_frontalface_default.xml")
            if os.path.exists(casc):
                self.det = cv2.CascadeClassifier(casc)
                self.kind = "haar" if not self.det.empty() else "none"

    def _yunet(self, w, h):
        if self._size != (w, h):
            backend, target = cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU
            if self.use_gpu and cv2.cuda.getCudaEnabledDeviceCount() > 0:
                backend, target = cv2.dnn.DNN_BACKEND_CUDA, cv2.dnn.DNN_TARGET_CUDA
            self.det = cv2.FaceDetectorYN.create(YUNET_PATH, "", (w, h), 0.6, 0.3, 50, backend, target)
            self._size = (w, h)
        return self.det

    def _cv_full(self, bgr: np.ndarray) -> np.ndarray:
        h, w = bgr.shape[:2]
        _, faces = self._yunet(w, h).detect(bgr)
        return np.zeros((0, 15), np.float32) if faces is None else faces.astype(np.float32)

    def detect_full(self, bgr: np.ndarray) -> np.ndarray:
        """YuNet rows [x, y, w, h, 5 landmark pairs, score] (15 values); empty for other detectors."""
        if self.kind != "yunet":
            return np.zeros((0, 15), np.float32)
        g = _GPU
        sess = _ort_sessions(self.use_gpu)[0] if g["state"] != "bad" else None
        if sess is None or g["state"] == "bad":
            return self._cv_full(bgr)
        try:
            fg = _ort_yunet(sess, bgr)
        except Exception as e:  # noqa: BLE001
            g["state"], g["why"] = "bad", f"run: {e}"[:160]
            return self._cv_full(bgr)
        if g["state"] == "ok":
            return fg
        # still checking: OpenCV is the answer, the GPU result must match it
        fc = self._cv_full(bgr)
        with g["lock"]:
            if g["state"] == "checking":
                if not _same_faces(fc, fg) or not _same_faces(fg, fc):
                    g["state"], g["why"] = "bad", "faces වෙනස්"
                elif len(fc[fc[:, 14] >= 0.75]):
                    g["agree"] += 1
                    if g["agree"] >= _GPU_CHECK_FACES:
                        g["state"] = "ok"
        return fc

    def detect(self, bgr: np.ndarray, full: np.ndarray | None = None) -> np.ndarray:
        """Return array of faces: [x, y, w, h, score] in pixel coords."""
        h, w = bgr.shape[:2]
        if self.kind == "yunet":
            faces = full if full is not None else self.detect_full(bgr)
            if len(faces) == 0:
                return np.zeros((0, 5), np.float32)
            return np.concatenate([faces[:, :4], faces[:, -1:]], axis=1).astype(np.float32)
        if self.kind == "haar":
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            rects = self.det.detectMultiScale(gray, 1.15, 5, minSize=(max(12, w // 40),) * 2)
            if len(rects) == 0:
                return np.zeros((0, 5), np.float32)
            return np.concatenate([np.asarray(rects, np.float32), np.full((len(rects), 1), 0.8, np.float32)], 1)
        return np.zeros((0, 5), np.float32)


def face_features(faces: np.ndarray, w: int, h: int):
    """(count, largest_area_ratio, centrality_of_largest 0..1, mean_score, pair_central 0..1).

    pair_central: for the two biggest faces (the couple), how close the middle point between them
    sits to the frame centre, also requiring the two faces to be of similar size (both in focus/near).
    0 when fewer than two faces.
    """
    if len(faces) == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    areas = faces[:, 2] * faces[:, 3] / float(w * h)
    order = np.argsort(-areas)
    i = int(order[0])
    cx = (faces[i, 0] + faces[i, 2] / 2) / w
    cy = (faces[i, 1] + faces[i, 3] / 2) / h
    # rule-of-thirds friendly: centre band is good, edges are bad
    central = float(np.clip(1 - (abs(cx - 0.5) / 0.5) ** 2 - 0.5 * max(0, abs(cy - 0.4) - 0.2), 0, 1))
    pair = 0.0
    if len(faces) >= 2:
        j = int(order[1])
        if areas[j] > 0.35 * areas[i]:  # second face comparable -> likely the couple, not a guest behind
            px = ((faces[i, 0] + faces[i, 2] / 2) + (faces[j, 0] + faces[j, 2] / 2)) / 2 / w
            py = ((faces[i, 1] + faces[i, 3] / 2) + (faces[j, 1] + faces[j, 3] / 2)) / 2 / h
            pair = float(np.clip(1 - (abs(px - 0.5) / 0.35) ** 2 - 0.6 * max(0, abs(py - 0.42) - 0.18), 0, 1))
    return int(len(faces)), float(areas[i]), central, float(faces[:, 4].mean()), pair


def smile_and_hug(full: np.ndarray, w: int, h: int) -> tuple[float, float]:
    """From YuNet landmarks: smile 0..1 of the biggest face (mouth width against eye distance),
    hug 0..1 when the two biggest faces are large and very close together (embrace, cheek to cheek)."""
    if len(full) == 0:
        return 0.0, 0.0
    areas = full[:, 2] * full[:, 3]
    order = np.argsort(-areas)
    f = full[order[0]]
    eyes = np.hypot(f[4] - f[6], f[5] - f[7]) + 1e-6
    mouth = np.hypot(f[10] - f[12], f[11] - f[13])
    smile = float(np.clip((mouth / eyes - 0.82) / 0.22, 0, 1)) if f[2] * f[3] > 0.004 * w * h else 0.0
    hug = 0.0
    if len(full) >= 2:
        g = full[order[1]]
        big = min(f[2] * f[3], g[2] * g[3]) / float(w * h)
        cx1, cy1 = f[0] + f[2] / 2, f[1] + f[3] / 2
        cx2, cy2 = g[0] + g[2] / 2, g[1] + g[3] / 2
        d = np.hypot(cx1 - cx2, cy1 - cy2) / ((f[2] + g[2]) / 2 + 1e-6)
        if big > 0.006:
            hug = float(np.clip((1.6 - d) / 0.8, 0, 1))
    return smile, hug


class FaceRecognizer:
    """SFace embeddings (OpenCV Zoo, Apache-2.0). Optional: without the model, recognition is off."""

    def __init__(self):
        self.rec = None
        if os.path.exists(SFACE_PATH) and hasattr(cv2, "FaceRecognizerSF"):
            try:
                self.rec = cv2.FaceRecognizerSF.create(SFACE_PATH, "")
            except Exception:  # noqa: BLE001
                self.rec = None

    @property
    def ok(self) -> bool:
        return self.rec is not None

    def embed(self, bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray | None:
        if self.rec is None:
            return None
        try:
            aligned = self.rec.alignCrop(bgr, face_row)
            e = None
            g = _GPU
            sess = g["rec"] if g["state"] in ("checking", "ok") else None
            if sess is not None:
                try:
                    blob = cv2.dnn.blobFromImage(aligned, 1.0, (112, 112), (0, 0, 0), True, False)
                    with g["lock"]:
                        e = sess.run(None, {sess.get_inputs()[0].name: blob})[0]
                    e = e.reshape(-1).astype(np.float32)
                    if g["state"] == "checking":     # compare with OpenCV until the detector is trusted
                        ec = self.rec.feature(aligned).reshape(-1).astype(np.float32)
                        cos = float(ec @ e / (np.linalg.norm(ec) * np.linalg.norm(e) + 1e-9))
                        if cos < 0.98:
                            g["rec"], g["why"] = None, f"sface cos {cos:.2f}"
                        e = ec
                except Exception as ex:  # noqa: BLE001
                    g["rec"], g["why"] = None, f"sface: {ex}"[:160]
                    e = None
            if e is None:
                e = self.rec.feature(aligned).reshape(-1).astype(np.float32)
            return e / (np.linalg.norm(e) + 1e-9)
        except Exception:  # noqa: BLE001
            return None
