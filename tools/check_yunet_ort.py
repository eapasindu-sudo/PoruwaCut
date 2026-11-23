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

# NanoDet people detector (AI shot size): count people on the test pictures
try:
    from engine import vision
    if vision.available():
        pd = vision.PersonDetector()
        total = 0
        for name, im in imgs:
            fr = cv2.resize(im, (480, int(round(im.shape[0] * 480 / im.shape[1] / 2)) * 2))
            ps = pd.detect(fr)
            total += len(ps)
            print(f"NANODET {name}: people {len(ps)}", [list(map(int, p[:4])) for p in ps[:4]],
                  "features", vision.shot_features(ps, None, fr.shape[1], fr.shape[0]))
        if imgs and total == 0:
            print("NANODET CHECK: FAIL (no people found on pictures that have people)")
            sys.exit(1)               # a broken people detector must not be released
        print("NANODET CHECK: PASS")
    else:
        print("NANODET model missing")
except SystemExit:
    raise
except Exception as e:  # noqa: BLE001
    print("NANODET error", e)
    sys.exit(1)

# Silero VAD (AI voice detection)
try:
    from engine import vad
    print("VAD available:", vad.available())
    if vad.available():
        sr = 16000
        t = np.arange(sr * 3) / sr
        p = vad.speech_prob((0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
        print("VAD on a tone (should be low):", float(p.max()))
except Exception as e:  # noqa: BLE001
    print("VAD error", e)
