"""Media helpers: ffprobe, fast frame reading (NVIDIA NVDEC when available), audio extraction."""
from __future__ import annotations

import functools
import json
import os
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from .paths import find_tool

FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
# never flash console windows from the (windowless) engine on Windows
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}

_NVDEC_OK: bool | None = None


@dataclass
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: Fraction
    audio_channels: int
    audio_rate: int
    audio_streams: int = 0
    timecode: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def fps_float(self) -> float:
        return float(self.fps)


@functools.lru_cache(maxsize=512)
def probe(path: str) -> MediaInfo:
    """ffprobe a media file (cached). Audio-only files (e.g. DJI WAV) get width=height=0."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=60, **NOWIN,
    ).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a_streams = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if v is None:
        if not a_streams:
            raise ValueError(f"No video or audio stream in {path}")
        v = {"width": 0, "height": 0, "avg_frame_rate": "25/1"}
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "25/1"
    if rate in ("0/0", "0/1"):
        rate = v.get("r_frame_rate", "25/1")
    fps = Fraction(rate)
    dur = float(data["format"].get("duration") or v.get("duration") or 0)
    tc = (v.get("tags") or {}).get("timecode") or (data["format"].get("tags") or {}).get("timecode")
    a0 = a_streams[0] if a_streams else {}
    return MediaInfo(
        path=path, duration=dur, width=int(v["width"]), height=int(v["height"]), fps=fps,
        audio_channels=int(a0.get("channels", 0)), audio_rate=int(a0.get("sample_rate", 48000) or 48000),
        audio_streams=len(a_streams), timecode=tc,
    )


def nvdec_available() -> bool:
    """True when ffmpeg can decode with CUDA (NVIDIA GPU)."""
    global _NVDEC_OK
    if _NVDEC_OK is None:
        try:
            r = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=10, **NOWIN)
            _NVDEC_OK = "cuda" in r.stdout
        except Exception:
            _NVDEC_OK = False
    return _NVDEC_OK


_DECODE_MODE: str | None = None  # remembered after the first success: "cuda_scale" | "cuda" | "cpu"


def iter_frames(path: str, start: float, duration: float, fps: float, width: int = 320,
                use_gpu: bool = True, gray: bool = False):
    """Yield (time_in_file, frame) sampled at `fps`, scaled to `width` px wide.

    Fastest first: NVIDIA decode + resize on the GPU (only the small frame is copied back), then
    NVIDIA decode + CPU resize, then plain CPU. The first mode that works is remembered.
    """
    global _DECODE_MODE
    info = probe(path)
    h = int(round(info.height * width / info.width / 2)) * 2
    pix = "gray" if gray else "bgr24"
    ch = 1 if gray else 3
    frame_bytes = width * h * ch

    def _cmd(mode: str):
        c = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if mode == "cuda_scale":
            c += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            vf = f"fps={fps},scale_cuda={width}:{h}:format=nv12,hwdownload,format=nv12,format={pix}"
        elif mode == "cuda":
            c += ["-hwaccel", "cuda"]
            vf = f"fps={fps},scale={width}:{h}:flags=area"
        else:
            vf = f"fps={fps},scale={width}:{h}:flags=area"
            # CPU decode (e.g. 10-bit 4:2:2 HEVC the GPU can't decode): skip the deblocking filter,
            # invisible at analysis size and a good deal faster
            c += ["-skip_loop_filter", "all", "-threads", "0"]
        c += ["-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}", "-i", path,
              "-an", "-sn", "-dn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", pix, "-"]
        return c

    if _DECODE_MODE:
        attempts = [_DECODE_MODE] + ([] if _DECODE_MODE == "cpu" else ["cpu"])
    elif use_gpu and nvdec_available():
        attempts = ["cuda_scale", "cuda", "cpu"]
    else:
        attempts = ["cpu"]
    for mode in attempts:
        proc = subprocess.Popen(_cmd(mode), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, bufsize=frame_bytes * 4, **NOWIN)
        n = 0
        try:
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                arr = np.frombuffer(buf, np.uint8).reshape((h, width) if gray else (h, width, 3))
                yield start + n / fps, arr
                n += 1
        finally:
            proc.stdout.close()
            err = proc.stderr.read().decode(errors="ignore")
            proc.wait()
        if n > 0:
            if _DECODE_MODE is None:
                _DECODE_MODE = mode
            return
        if mode == "cpu" and err.strip():
            raise RuntimeError(f"ffmpeg failed on {path}: {err.strip()[:500]}")
        # this mode produced nothing -> try the next one


def decode_mode() -> str:
    return _DECODE_MODE or "?"


def extract_audio(path: str, rate: int = 8000, start: float = 0.0, duration: float | None = None,
                  channel: int | None = None) -> np.ndarray:
    """Return mono float32 audio (or one specific channel) at `rate` Hz."""
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", path, "-vn"]
    if channel is not None:
        cmd += ["-af", f"pan=mono|c0=c{channel}"]
    else:
        cmd += ["-ac", "1"]
    cmd += ["-ar", str(rate), "-f", "f32le", "-"]
    # never wait forever on one file (a busy / sleeping drive, a stuck decoder): one retry, then skip
    limit = 60.0 + 0.5 * (duration if duration is not None else 1200.0)
    for attempt in range(2):
        try:
            out = subprocess.run(cmd, capture_output=True, check=False, stdin=subprocess.DEVNULL,
                                 timeout=limit, **NOWIN).stdout
            return np.frombuffer(out[: len(out) // 4 * 4], np.float32).copy()
        except subprocess.TimeoutExpired:
            print(f"audio read timed out ({limit:.0f}s, try {attempt + 1}/2): {path}", flush=True)
    return np.zeros(0, np.float32)


def grab_frame(path: str, t: float, width: int = 480):
    """One BGR frame at file time t (fast keyframe seek), or None."""
    import numpy as _np
    try:
        info = probe(path)
        h = int(round(info.height * width / info.width / 2)) * 2
        cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path,
               "-frames:v", "1", "-an", "-vf", f"scale={width}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        out = subprocess.run(cmd, capture_output=True, timeout=20, stdin=subprocess.DEVNULL, **NOWIN).stdout
        if len(out) < width * h * 3:
            return None
        return _np.frombuffer(out[: width * h * 3], _np.uint8).reshape(h, width, 3)
    except Exception:  # noqa: BLE001
        return None
