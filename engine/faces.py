"""Face detection: YuNet (accurate, uses CUDA if OpenCV was built with it) with Haar fallback."""
from __future__ import annotations

import os

import cv2
import numpy as np

from .paths import MODELS_DIR

YUNET_PATH = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_PATH = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")


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

    def detect_full(self, bgr: np.ndarray) -> np.ndarray:
        """YuNet rows [x, y, w, h, 5 landmark pairs, score] (15 values); empty for other detectors."""
        if self.kind != "yunet":
            return np.zeros((0, 15), np.float32)
        h, w = bgr.shape[:2]
        _, faces = self._yunet(w, h).detect(bgr)
        return np.zeros((0, 15), np.float32) if faces is None else faces.astype(np.float32)

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
            e = self.rec.feature(aligned).reshape(-1).astype(np.float32)
            return e / (np.linalg.norm(e) + 1e-9)
        except Exception:  # noqa: BLE001
            return None
