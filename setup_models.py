"""Download the YuNet face-detection model (MIT licence, OpenCV Zoo) into ./models.

Without it the engine falls back to OpenCV's older Haar face detector.
"""
import os
import urllib.request

URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
       "face_detection_yunet_2023mar.onnx")
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "face_detection_yunet_2023mar.onnx")

os.makedirs(os.path.dirname(DEST), exist_ok=True)
if os.path.exists(DEST) and os.path.getsize(DEST) > 100_000:
    print("YuNet model already present:", DEST)
else:
    print("Downloading YuNet face model...")
    urllib.request.urlretrieve(URL, DEST)
    size = os.path.getsize(DEST)
    if size < 100_000:
        os.remove(DEST)
        raise SystemExit("Download failed (file too small). Download manually from:\n" + URL + "\ninto: " + DEST)
    print(f"OK ({size // 1024} KB) -> {DEST}")

import cv2  # noqa: E402
cv2.FaceDetectorYN.create(DEST, "", (320, 240))
print("Model loads fine in OpenCV", cv2.__version__)
