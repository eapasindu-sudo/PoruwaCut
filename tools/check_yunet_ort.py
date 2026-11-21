"""CI check: the onnxruntime YuNet path (used on the GPU) gives the same faces as OpenCV.
Prints the result; exit code 0 always (the engine checks itself again at run time)."""
import os
import sys
import urllib.request

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import faces as F  # noqa: E402

try:
    import onnxruntime as ort
    print("onnxruntime", ort.__version__, ort.get_available_providers())
except Exception as e:  # noqa: BLE001
    print("onnxruntime missing:", e)
    sys.exit(0)

imgs = []
for name in ("messi5.jpg", "lena.jpg", "basketball1.png"):
    try:
        data = urllib.request.urlopen("https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/" + name, timeout=30).read()
        im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if im is not None:
            imgs.append((name, im))
    except Exception as e:  # noqa: BLE001
        print("image", name, e)
sess = ort.InferenceSession(F.YUNET_PATH, providers=["CPUExecutionProvider"])
print("yunet inputs", [(i.name, i.shape) for i in sess.get_inputs()])
ok = True
for name, im in imgs:
    for wid in (480, 640):
        h = int(round(im.shape[0] * wid / im.shape[1] / 2)) * 2
        fr = cv2.resize(im, (wid, h))
        det = cv2.FaceDetectorYN.create(F.YUNET_PATH, "", (wid, h), 0.6, 0.3, 50)
        _, fc = det.detect(fr)
        fc = np.zeros((0, 15), np.float32) if fc is None else fc
        try:
            fg = F._ort_yunet(sess, fr)
        except Exception as e:  # noqa: BLE001
            print(f"{name} {wid}px: ort error {e}")
            ok = False
            continue
        same = F._same_faces(fc, fg) and F._same_faces(fg, fc) and len(fc) == len(fg)
        err = 0.0
        if same and len(fc):
            a, b = fc[np.argsort(fc[:, 0])], fg[np.argsort(fg[:, 0])]
            err = float(np.abs(a - b).max())
        print(f"{name} {wid}px: opencv {len(fc)} faces, ort {len(fg)} faces, same={same}, max diff {err:.3f}")
        ok = ok and same and err < 1.0
print("YUNET ORT CHECK:", "PASS" if ok else "FAIL")
