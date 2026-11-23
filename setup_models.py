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

# Optional: SFace face recognition (Apache-2.0, OpenCV Zoo) -> bride / groom recognition.
SFACE_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
             "face_recognition_sface_2021dec.onnx")
SFACE = os.path.join(os.path.dirname(DEST), "face_recognition_sface_2021dec.onnx")
try:
    if not (os.path.exists(SFACE) and os.path.getsize(SFACE) > 1_000_000):
        print("Downloading SFace recognition model...")
        urllib.request.urlretrieve(SFACE_URL, SFACE)
    if os.path.getsize(SFACE) < 1_000_000:
        os.remove(SFACE)
        print("SFace download failed (too small) - couple recognition will be off")
    else:
        cv2.FaceRecognizerSF.create(SFACE, "")
        print(f"SFace OK ({os.path.getsize(SFACE) // 1024} KB)")
except Exception as e:  # noqa: BLE001
    print("SFace skipped:", e)

# AI people detector: NanoDet-Plus (Apache-2.0, OpenCV Zoo) -> people, shot size, turned away.
NANO_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/object_detection_nanodet/"
            "object_detection_nanodet_2022nov.onnx")
NANO = os.path.join(os.path.dirname(DEST), "nanodet_2022nov.onnx")
try:
    if not (os.path.exists(NANO) and os.path.getsize(NANO) > 1_000_000):
        print("Downloading NanoDet people model...")
        urllib.request.urlretrieve(NANO_URL, NANO)
    if os.path.getsize(NANO) < 1_000_000:
        os.remove(NANO)
        print("NanoDet download failed (too small) - shot size AI will be off")
    else:
        cv2.dnn.readNet(NANO)
        print(f"NanoDet OK ({os.path.getsize(NANO) // 1024} KB)")
except Exception as e:  # noqa: BLE001
    print("NanoDet skipped:", e)

# AI voice detection: Silero VAD v5 (MIT) -> cuts after sentences, not in the middle of words.
VAD_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
VADF = os.path.join(os.path.dirname(DEST), "silero_vad.onnx")
try:
    if not (os.path.exists(VADF) and os.path.getsize(VADF) > 1_000_000):
        print("Downloading Silero VAD model...")
        urllib.request.urlretrieve(VAD_URL, VADF)
    if os.path.getsize(VADF) < 1_000_000:
        os.remove(VADF)
        print("Silero VAD download failed (too small) - voice detection falls back to loudness")
    else:
        print(f"Silero VAD OK ({os.path.getsize(VADF) // 1024} KB)")
except Exception as e:  # noqa: BLE001
    print("Silero VAD skipped:", e)
