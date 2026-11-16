"""Generate a synthetic 3-camera test shoot with known problems, to test the engine.

cam1 (master, 2-ch "DJI" audio, wide/steady): over-exposed 20-30 s
cam2 (handheld close): heavy shake 0-10 s, out of focus 40-46 s, starts 3.2 s late
cam3 (handheld): someone walks in front 12-20 s, starts 1.7 s early
Music at 120 BPM throughout.
"""
import os
import subprocess
import sys
import wave

import cv2
import numpy as np

OUT = sys.argv[1] if len(sys.argv) > 1 else "test_footage"
DUR, FPS, W, H, SR = 60.0, 25, 640, 360, 48000
rng = np.random.default_rng(1)
os.makedirs(OUT, exist_ok=True)

# big textured "venue"
world = np.zeros((1200, 2000, 3), np.uint8)
world[:] = (40, 60, 90)
for _ in range(900):
    c = tuple(int(v) for v in rng.integers(30, 255, 3))
    cv2.circle(world, (int(rng.integers(0, 2000)), int(rng.integers(0, 1200))), int(rng.integers(4, 40)), c, -1)
for _ in range(300):
    p = rng.integers(0, [2000, 1200], 2)
    cv2.rectangle(world, tuple(int(v) for v in p), tuple(int(v) for v in p + rng.integers(10, 80, 2)),
                  tuple(int(v) for v in rng.integers(0, 255, 3)), 2)
world = cv2.GaussianBlur(world, (3, 3), 0)

# audio: 120 bpm kick + hats + "voice" noise, absolute timeline -10 .. 70 s
T0 = -10.0
n = int((DUR + 20) * SR)
t = np.arange(n) / SR + T0
audio = 0.02 * rng.standard_normal(n)
for b in np.arange(T0, T0 + DUR + 20, 0.5):
    i = int((b - T0) * SR)
    k = np.arange(min(4000, n - i))
    audio[i:i + len(k)] += 0.8 * np.sin(2 * np.pi * 60 * k / SR) * np.exp(-k / 900)
    j = i + int(0.25 * SR)
    if j + 800 < n:
        audio[j:j + 800] += 0.2 * rng.standard_normal(800) * np.exp(-np.arange(800) / 150)
audio += 0.1 * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 0.3 * t) > 0.6)


def write_wav(path, chans):
    x = np.clip(np.stack(chans, 1), -1, 1)
    with wave.open(path, "wb") as w:
        w.setnchannels(x.shape[1]); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((x * 32767).astype(np.int16).tobytes())


def render(name, cam_start, dur, cx, cy, zoom, problems, chans):
    path = os.path.join(OUT, name + ".mp4")
    wav = os.path.join(OUT, name + ".wav")
    i0 = int((cam_start - T0) * SR)
    write_wav(wav, [c[i0:i0 + int(dur * SR)] for c in chans])
    cmd = [os.environ.get("FFMPEG", "ffmpeg"), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-i", wav, "-c:v", "mpeg4", "-q:v", "4",  # mpeg4: present in every ffmpeg build (LGPL too)
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    jitter = np.zeros(2)
    for f in range(int(dur * FPS)):
        tt = cam_start + f / FPS  # absolute time
        x = cx + 60 * np.sin(tt * 0.15)
        y = cy + 20 * np.sin(tt * 0.1)
        if problems.get("shake") and problems["shake"][0] <= tt < problems["shake"][1]:
            jitter = rng.normal(0, 22, 2)
        else:
            jitter *= 0.3
        x, y = x + jitter[0], y + jitter[1]
        cw, ch = int(W * zoom), int(H * zoom)
        x0, y0 = int(np.clip(x - cw / 2, 0, 2000 - cw)), int(np.clip(y - ch / 2, 0, 1200 - ch))
        frame = cv2.resize(world[y0:y0 + ch, x0:x0 + cw], (W, H), interpolation=cv2.INTER_AREA)
        if problems.get("over") and problems["over"][0] <= tt < problems["over"][1]:
            frame = cv2.convertScaleAbs(frame, alpha=2.6, beta=110)
        if problems.get("blur") and problems["blur"][0] <= tt < problems["blur"][1]:
            frame = cv2.GaussianBlur(frame, (0, 0), 7)
        if problems.get("block") and problems["block"][0] <= tt < problems["block"][1]:
            a, b = problems["block"]
            px = int(-300 + (tt - a) / (b - a) * (W + 300))
            cv2.ellipse(frame, (px + 150, H // 2 + 40), (230, 300), 0, 0, 360, (25, 22, 30), -1)
            frame = cv2.GaussianBlur(frame, (0, 0), 1.2)
        p.stdin.write(frame.tobytes())
    p.stdin.close(); p.wait()
    os.remove(wav)
    return path


mic1 = audio + 0.01 * rng.standard_normal(n)
mic2 = 0.7 * audio + 0.03 * rng.standard_normal(n)
camaud = np.convolve(audio, np.ones(8) / 8, mode="same") * 0.6 + 0.05 * rng.standard_normal(n)
render("cam1_master", 0.0, DUR, 1000, 600, 2.2, {"over": (20, 30)}, [mic1, mic2])
render("cam2_close", 3.2, DUR - 3.2, 900, 560, 1.0, {"shake": (0, 10), "blur": (40, 46)}, [camaud, camaud])
render("cam3_side", -1.7, DUR + 1.7, 1150, 640, 1.3, {"block": (12, 20)}, [camaud * 0.8, camaud * 0.8])
print("done ->", OUT)
