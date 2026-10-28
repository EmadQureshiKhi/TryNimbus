"""Video plumbing: probe, normalise to H.264 MP4, grab frames, build proxies.

ffmpeg is used when it is installed (it is faster and handles every container). Every operation
has a fallback on macOS's own AVFoundation / `avconvert`, so Nimbus works on a Mac with nothing
extra installed.
"""
from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class MediaError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    codec: str = ""


def _run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise MediaError(f"{Path(cmd[0]).name} failed: {proc.stderr.decode(errors='replace')[-600:]}")
    return proc


def probe(path: Path) -> VideoInfo:
    if FFPROBE:
        try:
            out = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
            data = json.loads(out.stdout)
            video = next(s for s in data["streams"] if s.get("codec_type") == "video")
            num, _, den = (video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1").partition("/")
            fps = float(num) / float(den or 1) if float(den or 1) else 30.0
            duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
            return VideoInfo(duration, int(video["width"]), int(video["height"]), fps or 30.0,
                             any(s.get("codec_type") == "audio" for s in data["streams"]), video.get("codec_name", ""))
        except (MediaError, StopIteration, KeyError, ValueError) as exc:
            log.warning("ffprobe failed (%s); using AVFoundation", exc)
    return _probe_avf(path)


def _probe_avf(path: Path) -> VideoInfo:
    import AVFoundation
    from Foundation import NSURL

    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    duration = asset.duration()
    seconds = duration.value / duration.timescale if duration.timescale else 0.0
    videos = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeVideo)
    if not videos:
        raise MediaError(f"{path.name} has no video track")
    track = videos[0]
    size = track.naturalSize()
    audio = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeAudio)
    return VideoInfo(seconds, int(abs(size.width)), int(abs(size.height)), float(track.nominalFrameRate() or 30.0),
                     bool(audio))


def normalize(path: Path, out_dir: Path) -> Path:
    """Return an H.264/AAC MP4 copy (or the original if it already is one)."""
    info = probe(path)
    if path.suffix.lower() == ".mp4" and info.codec in ("h264", "hevc"):
        return path
    out = out_dir / (path.stem + ".mp4")
    if FFMPEG:
        try:
            _run([FFMPEG, "-y", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                  "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(out)])
            return out
        except MediaError as exc:
            log.warning("ffmpeg transcode failed (%s); trying avconvert", exc)
    _run(["/usr/bin/avconvert", "--preset", "PresetHighestQuality", "--source", str(path), "--output", str(out),
          "--replace"])
    return out


def proxy(path: Path, out_dir: Path, *, fps: float = 4.0, max_width: int = 1280) -> Path:
    """Low-fps, downscaled, silent copy for SAM tracking (fewer frames = faster and cheaper)."""
    out = out_dir / f"{path.stem}.proxy{int(fps)}fps.mp4"
    if out.exists():
        return out
    if not FFMPEG:
        log.warning("ffmpeg not installed; SAM will track the full-rate video")
        return path
    _run([FFMPEG, "-y", "-i", str(path), "-an", "-vf", f"fps={fps},scale='min({max_width},iw)':-2",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", str(out)])
    return out


def frame_at(path: Path, t: float) -> bytes:
    """PNG bytes of the frame at `t` seconds."""
    t = max(0.0, t)
    if FFMPEG:
        # Seeking to the very last frame can yield nothing; step back a little and try again.
        for attempt_t in (t, max(0.0, t - 0.3), max(0.0, t - 1.0)):
            try:
                out = _run([FFMPEG, "-v", "error", "-ss", f"{attempt_t:.3f}", "-i", str(path), "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], timeout=60)
                if out.stdout:
                    return out.stdout
            except MediaError as exc:
                log.warning("ffmpeg frame grab at %.2fs failed (%s)", attempt_t, exc)
                break
    return _frame_avf(path, t)


def _frame_avf(path: Path, t: float) -> bytes:
    import AVFoundation
    import Quartz
    from CoreMedia import CMTimeMakeWithSeconds
    from Foundation import NSURL

    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    gen = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
    gen.setAppliesPreferredTrackTransform_(True)
    gen.setRequestedTimeToleranceBefore_(CMTimeMakeWithSeconds(0.05, 600))
    gen.setRequestedTimeToleranceAfter_(CMTimeMakeWithSeconds(0.05, 600))
    cg, err = None, None
    for attempt_t in (t, max(0.0, t - 0.3), max(0.0, t - 1.0)):
        result = gen.copyCGImageAtTime_actualTime_error_(CMTimeMakeWithSeconds(attempt_t, 600), None, None)
        # PyObjC returns (image, actualTime, error) or (image, error) depending on the bridge metadata.
        parts = result if isinstance(result, tuple) else (result,)
        cg, err = parts[0], parts[-1] if len(parts) > 1 else None
        if cg is not None:
            break
    if cg is None:
        raise MediaError(f"could not read frame at {t:.2f}s: {err}")
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        url = NSURL.fileURLWithPath_(tmp.name)
        dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        Quartz.CGImageDestinationAddImage(dest, cg, None)
        Quartz.CGImageDestinationFinalize(dest)
        return Path(tmp.name).read_bytes()


def crop_png(png: bytes, box: tuple[int, int, int, int], pad: int = 12,
             min_size: tuple[int, int] = (0, 0)) -> bytes:
    """`box` plus `pad` on every side, grown around its centre to at least `min_size`, inside the image."""
    img = Image.open(io.BytesIO(png))
    w, h = img.size
    l, t, r, b = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
    grow_x, grow_y = max(0, min_size[0] - (r - l)) / 2, max(0, min_size[1] - (b - t)) / 2
    l, r, t, b = l - grow_x, r + grow_x, t - grow_y, b + grow_y
    dx = max(0, -l) - max(0, r - w)  # slide back inside rather than lose the context
    dy = max(0, -t) - max(0, b - h)
    region = tuple(int(round(v)) for v in (max(0, l + dx), max(0, t + dy), min(w, r + dx), min(h, b + dy)))
    out = io.BytesIO()
    img.crop(region).save(out, format="PNG")
    return out.getvalue()


def png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as img:
        return img.size
