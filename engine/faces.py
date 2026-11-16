"""Face detection: YuNet (accurate, uses CUDA if OpenCV was built with it) with Haar fallback."""
from __future__ import annotations

import os

import cv2
import numpy as np

from .paths import MODELS_DIR

YUNET_PATH = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")


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

    def detect(self, bgr: np.ndarray) -> np.ndarray:
        """Return array of faces: [x, y, w, h, score] in pixel coords."""
        h, w = bgr.shape[:2]
        if self.kind == "yunet":
            _, faces = self._yunet(w, h).detect(bgr)
            if faces is None:
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
