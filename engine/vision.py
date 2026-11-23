"""People in the picture (NanoDet-Plus, Apache-2.0, OpenCV Zoo): who is in the shot and how big.

From the person boxes and the faces we get, per frame:
  people      - how many people are clearly in the shot
  person_h    - height of the biggest person / picture height (0..1+)
  shot_scale  - 0 = wide (small people / a crowd) .. 1 = close-up (one face fills the frame)
  away        - the main person is turned away (a body without a face)
Runs on a few frames per second only; missing model = no values (NaN), nothing else changes.
"""
from __future__ import annotations

import os
import threading

import cv2
import numpy as np

from .paths import MODELS_DIR

NANODET_PATH = os.path.join(MODELS_DIR, "nanodet_2022nov.onnx")
PERSON = 0                      # COCO class 0
_SIZE = 416
_STRIDES = (8, 16, 32, 64)
_REG_MAX = 7
_MEAN = np.array([103.53, 116.28, 123.675], np.float32).reshape(1, 1, 3)
_STD = np.array([57.375, 57.12, 58.395], np.float32).reshape(1, 1, 3)
_LOCK = threading.Lock()
_NET = {"net": None, "tried": False}


def _anchors():
    out = []
    for s in _STRIDES:
        n = _SIZE // s
        xv, yv = np.meshgrid(np.arange(n) * s, np.arange(n) * s)
        out.append(np.column_stack((xv.flatten() + 0.5 * (s - 1), yv.flatten() + 0.5 * (s - 1))))
    return out


_ANCH = _anchors()
_PROJ = np.arange(_REG_MAX + 1, dtype=np.float32)


def available() -> bool:
    return os.path.exists(NANODET_PATH)


class PersonDetector:
    """One cv2.dnn net per thread (cv2.dnn nets are not thread safe)."""

    def __init__(self):
        self.net = None
        if available():
            try:
                self.net = cv2.dnn.readNet(NANODET_PATH)
                self.names = self.net.getUnconnectedOutLayersNames()
            except Exception:  # noqa: BLE001
                self.net = None

    def detect(self, bgr: np.ndarray, thr: float = 0.4) -> np.ndarray:
        """Person boxes [x0, y0, x1, y1, score] in pixels of `bgr`."""
        if self.net is None:
            return np.zeros((0, 5), np.float32)
        h, w = bgr.shape[:2]
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        # letterbox to 416x416 (same as the OpenCV Zoo demo)
        top = left = 0
        if h > w:
            nh, nw = _SIZE, int(_SIZE * w / h)
            img = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
            left = (_SIZE - nw) // 2
            img = cv2.copyMakeBorder(img, 0, 0, left, _SIZE - nw - left, cv2.BORDER_CONSTANT, value=0)
        elif h < w:
            nh, nw = int(_SIZE * h / w), _SIZE
            img = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
            top = (_SIZE - nh) // 2
            img = cv2.copyMakeBorder(img, top, _SIZE - nh - top, 0, 0, cv2.BORDER_CONSTANT, value=0)
        else:
            nh = nw = _SIZE
            img = cv2.resize(rgb, (_SIZE, _SIZE), interpolation=cv2.INTER_AREA)
        blob = cv2.dnn.blobFromImage((img.astype(np.float32) - _MEAN) / _STD)
        self.net.setInput(blob)
        outs = self.net.forward(self.names)
        # match outputs by shape, not by order (the order of the names differs between OpenCV versions):
        # class scores have 80 columns, box distributions 4*(reg_max+1); rows = anchors of one stride
        by = {}
        for o in outs:
            o2 = o.reshape(-1, o.shape[-1])
            kind = "reg" if o2.shape[1] == 4 * (_REG_MAX + 1) else "cls"
            by[(kind, o2.shape[0])] = o2
        boxes, scores = [], []
        for stride, anc in zip(_STRIDES, _ANCH):
            cls, reg = by.get(("cls", len(anc))), by.get(("reg", len(anc)))
            if cls is None or reg is None:
                continue
            sc = cls[:, PERSON]
            keep = sc >= thr
            if not keep.any():
                continue
            r = reg[keep].reshape(-1, _REG_MAX + 1)
            e = np.exp(r - r.max(axis=1, keepdims=True))
            d = ((e / e.sum(axis=1, keepdims=True)) @ _PROJ).reshape(-1, 4) * stride
            a = anc[keep]
            bb = np.column_stack([a[:, 0] - d[:, 0], a[:, 1] - d[:, 1], a[:, 0] + d[:, 2], a[:, 1] + d[:, 3]])
            boxes.append(np.clip(bb, 0, _SIZE))
            scores.append(sc[keep])
        if not boxes:
            return np.zeros((0, 5), np.float32)
        bb = np.concatenate(boxes)
        sc = np.concatenate(scores)
        wh = bb.copy()
        wh[:, 2:] -= wh[:, :2]
        idx = np.array(cv2.dnn.NMSBoxes(wh.tolist(), sc.tolist(), thr, 0.6)).reshape(-1)
        if not len(idx):
            return np.zeros((0, 5), np.float32)
        bb, sc = bb[idx], sc[idx]
        # back to the frame's pixels
        rx, ry = w / nw, h / nh
        bb[:, [0, 2]] = np.clip((bb[:, [0, 2]] - left) * rx, 0, w)
        bb[:, [1, 3]] = np.clip((bb[:, [1, 3]] - top) * ry, 0, h)
        return np.column_stack([bb, sc]).astype(np.float32)


def shot_features(persons: np.ndarray, faces, w: int, h: int) -> tuple[float, float, float, float]:
    """(people, person_h, shot_scale, away) for one frame. faces: YuNet rows or None."""
    big = persons[(persons[:, 3] - persons[:, 1]) > 0.12 * h] if len(persons) else persons
    people = float(len(big))
    ph = float(((big[:, 3] - big[:, 1]) / h).max()) if len(big) else 0.0
    fh = 0.0
    if faces is not None and len(faces):
        fh = float((faces[:, 3] / h).max())
    # shot size: a face ~ 1/2 of the height = close-up; a person ~ 1/3 of the height or a crowd = wide
    s_face = np.clip((fh - 0.06) / 0.30, 0, 1) if fh > 0 else 0.0
    s_body = np.clip((ph - 0.35) / 0.65, 0, 1) if ph > 0 else 0.0
    scale = float(max(s_face, 0.8 * s_body))
    if people >= 4:
        scale = min(scale, 0.35)
    if people == 0 and fh == 0:
        scale = np.nan
    away = 0.0
    if len(big):
        m = big[np.argmax(big[:, 3] - big[:, 1])]
        has_face = False
        if faces is not None and len(faces):
            cx = faces[:, 0] + faces[:, 2] / 2
            cy = faces[:, 1] + faces[:, 3] / 2
            has_face = bool(((cx > m[0]) & (cx < m[2]) & (cy > m[1]) & (cy < m[1] + 0.45 * (m[3] - m[1]))).any())
        # the head of a big person is in the picture (top not cut off) but no face: turned away
        if not has_face and (m[3] - m[1]) > 0.3 * h and m[1] > 0.02 * h:
            away = 1.0
    return people, ph, scale, away
