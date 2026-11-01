"""Record a bug report: the screen plus the microphone, to a .mov, or take an existing video.

Uses the built-in `/usr/sbin/screencapture` in video mode (`-v`, or `-V <seconds>` for a hard cap)
with `-g` for the default audio input, so the narration lands in the same file and nothing extra
has to be installed or granted to a helper binary. `-k` draws click rings, which helps SAM and
the reader find the clicks. The display that contains the mouse is recorded, because that is where
the person is about to reproduce the bug.

Stopping on demand sends SIGINT (what ctrl-c does in a terminal); screencapture then finalises the
movie. A watchdog enforces the maximum duration even if the cap flag is ignored.

Permissions: macOS attributes Screen Recording and Microphone access to the app that launched
this process (Nimbus, or the terminal during development). Both are checked up front without
prompting; a missing grant becomes a `PermissionDenied` that tells the person where to grant it.

No Qt here: the UI drives this through `start()` / `stop()` from its event loop.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nimbus import media

log = logging.getLogger(__name__)

SCREENCAPTURE = "/usr/sbin/screencapture"
VIDEO_SUFFIXES = frozenset({".mov", ".mp4", ".m4v", ".webm", ".mkv"})
STARTUP_GRACE_S = 1.2
FINALIZE_TIMEOUT_S = 20.0
CAP_GRACE_S = 3.0  # the -V flag should end the recording first; the watchdog is the backstop

SCREEN_HELP = ("Nimbus cannot record the screen. Open System Settings > Privacy & Security > Screen & System Audio "
               "Recording, turn on the app you launched Nimbus from, then quit and reopen it.")
MIC_HELP = ("Nimbus cannot use the microphone, so the recording will have no narration. To narrate, open System "
            "Settings > Privacy & Security > Microphone and turn on the app you launched Nimbus from.")


class RecorderError(RuntimeError):
    pass


class PermissionDenied(RecorderError):
    pass


@dataclass
class Recording:
    path: Path
    duration_s: float
    has_audio: bool
    source: Literal["screen", "import"]
    display: int | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- pure helpers


def build_command(out_path: Path, *, display: int | None, audio: bool = True, show_clicks: bool = True,
                  max_duration: float | None = None, binary: str = SCREENCAPTURE) -> list[str]:
    """The screencapture argv. `display` is screencapture's 1-based number (1 = main)."""
    argv = [binary, "-x"]  # -x: no camera shutter sound
    if max_duration and max_duration > 0:
        argv += ["-V", str(max(1, int(round(max_duration))))]
    else:
        argv.append("-v")
    if audio:
        argv.append("-g")
    if show_clicks:
        argv.append("-k")
    if display is not None:
        if display < 1:
            raise ValueError("display numbers start at 1")
        argv += ["-D", str(display)]
    argv.append(str(out_path))
    return argv


def display_number(target_id: int, active_ids: list[int], main_id: int) -> int | None:
    """screencapture numbers displays from 1 with the main display first, then the active list order."""
    ordered = [main_id] + [d for d in active_ids if d != main_id]
    try:
        return ordered.index(target_id) + 1
    except ValueError:
        return None


def display_under_mouse() -> int | None:
    """screencapture's number for the display that contains the mouse pointer (reads, never moves it)."""
    try:
        import Quartz
    except ImportError:  # pragma: no cover - Quartz ships with the app's dependencies
        return None
    try:
        point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        err, hits, count = Quartz.CGGetDisplaysWithPoint(point, 1, None, None)
        if err or not count:
            return None
        err, active, n = Quartz.CGGetActiveDisplayList(16, None, None)
        if err:
            return None
        return display_number(int(hits[0]), [int(d) for d in active[:n]], int(Quartz.CGMainDisplayID()))
    except Exception as exc:  # pyobjc surfaces CoreGraphics failures as assorted types
        log.warning("could not find the display under the mouse: %s", exc)
        return None


def screen_recording_allowed() -> bool | None:
    """True/False from CoreGraphics' preflight (never prompts); None if it cannot be asked."""
    try:
        import Quartz

        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except (ImportError, AttributeError):
        return None


def microphone_status() -> Literal["authorized", "denied", "restricted", "not_determined", "unknown"]:
    try:
        import AVFoundation

        status = int(AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio))
    except (ImportError, AttributeError):
        return "unknown"
    return {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}.get(status, "unknown")  # type: ignore[return-value]


def import_video(path: Path, dest_dir: Path | None = None) -> Recording:
    """Accept an existing recording (QuickTime, Loom export, phone video...). Copies it when `dest_dir`
    is given so a later cleanup of the original does not break the run."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise RecorderError(f"{path} does not exist")
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise RecorderError(f"{path.name} is not a supported video ({', '.join(sorted(VIDEO_SUFFIXES))})")
    try:
        info = media.probe(path)
    except Exception as exc:  # ffprobe/AVFoundation raise different types for a broken file
        raise RecorderError(f"{path.name} could not be read as a video: {exc}") from exc
    if info.duration <= 0:
        raise RecorderError(f"{path.name} has no duration")
    if dest_dir is not None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        if dest.resolve() != path.resolve():
            shutil.copy2(path, dest)
        path = dest
    return Recording(path, info.duration, info.has_audio, "import")


# ---------------------------------------------------------------- the recorder


class ScreenRecorder:
    """One recording at a time: `await start()`, then `await stop()` (or `await wait()` for the cap)."""

    def __init__(self, out_dir: Path, *, audio: bool = True, show_clicks: bool = True, max_duration: float = 600.0,
                 display: int | None = None, binary: str = SCREENCAPTURE, check_permissions: bool = True,
                 startup_grace: float = STARTUP_GRACE_S) -> None:
        self.out_dir = Path(out_dir)
        self.startup_grace = startup_grace
        self.audio = audio
        self.show_clicks = show_clicks
        self.max_duration = max_duration
        self.display = display
        self.binary = binary
        self.check_permissions = check_permissions
        self.warnings: list[str] = []
        self._proc: asyncio.subprocess.Process | None = None
        self._path: Path | None = None
        self._started = 0.0
        self._watchdog: asyncio.Task | None = None
        self._audio_on = audio
        self._display_used: int | None = None
        self._stopping: asyncio.Lock = asyncio.Lock()
        self._result: Recording | None = None

    @property
    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started if self.is_recording else 0.0

    def _preflight(self) -> None:
        if not Path(self.binary).exists():
            raise RecorderError(f"{self.binary} is missing; screen recording needs macOS")
        if not self.check_permissions:
            return
        if screen_recording_allowed() is False:
            raise PermissionDenied(SCREEN_HELP)
        if self.audio:
            status = microphone_status()
            if status in ("denied", "restricted"):
                log.warning("microphone access is %s; recording without narration", status)
                self.warnings.append(MIC_HELP)
                self._audio_on = False

    async def start(self) -> Path:
        if self.is_recording:
            raise RecorderError("a recording is already running")
        self._result = None
        self._audio_on = self.audio
        self._preflight()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.out_dir / f"bug-{time.strftime('%Y%m%d-%H%M%S')}.mov"
        self._display_used = self.display if self.display is not None else display_under_mouse()
        argv = build_command(self._path, display=self._display_used, audio=self._audio_on,
                             show_clicks=self.show_clicks, max_duration=self.max_duration, binary=self.binary)
        log.info("recording: %s", " ".join(argv))
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            raise RecorderError(f"could not start screencapture: {exc}") from exc
        self._started = time.monotonic()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=self.startup_grace)
        except asyncio.TimeoutError:
            pass  # still running: recording has started
        else:
            stderr = (await self._proc.stderr.read()).decode(errors="replace") if self._proc.stderr else ""
            code = self._proc.returncode
            self._proc = None
            raise self._failure(code, stderr)
        self._watchdog = asyncio.ensure_future(self._enforce_cap())
        return self._path

    async def _enforce_cap(self) -> None:
        await asyncio.sleep(self.max_duration + CAP_GRACE_S)
        if self.is_recording:
            log.info("recording reached the %.0fs cap; stopping", self.max_duration)
            await self.stop()

    async def wait(self) -> Recording:
        """Wait for the recording to end by itself (the duration cap) and return it."""
        if self._proc is not None:
            await self._proc.wait()
        return await self.stop()

    async def stop(self) -> Recording:
        async with self._stopping:
            if self._result is not None:
                return self._result
            proc = self._proc
            if proc is None or self._path is None:
                raise RecorderError("no recording is running")
            if self._watchdog and self._watchdog is not asyncio.current_task():
                self._watchdog.cancel()
            if proc.returncode is None:
                proc.send_signal(signal.SIGINT)
                for sig, wait in ((None, FINALIZE_TIMEOUT_S), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
                    if sig is not None:
                        log.warning("screencapture did not finish; sending %s", sig.name)
                        try:
                            proc.send_signal(sig)
                        except ProcessLookupError:
                            break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=wait)
                        break
                    except asyncio.TimeoutError:
                        continue
            stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
            elapsed = time.monotonic() - self._started
            self._proc = None
            path = self._path
            if not path.exists() or path.stat().st_size == 0:
                raise self._failure(proc.returncode, stderr)
            duration = elapsed
            has_audio = self._audio_on
            try:
                info = await asyncio.to_thread(media.probe, path)
                duration, has_audio = info.duration or elapsed, info.has_audio
            except Exception as exc:  # the file exists; a probe failure should not lose it
                log.warning("could not probe %s: %s", path.name, exc)
            if self._audio_on and not has_audio:
                self.warnings.append("The recording has no audio track; check the default input device.")
            self._result = Recording(path, duration, has_audio, "screen", self._display_used, list(self.warnings))
            log.info("recorded %.1fs to %s", duration, path)
            return self._result

    def _failure(self, code: int | None, stderr: str) -> RecorderError:
        text = stderr.strip()
        lowered = text.lower()
        if any(k in lowered for k in ("permission", "not authorized", "not permitted", "denied", "tcc")) or \
                (self.check_permissions and screen_recording_allowed() is False):
            return PermissionDenied(f"{SCREEN_HELP}\n(screencapture said: {text or f'exit code {code}'})")
        return RecorderError(f"screencapture stopped without a recording (exit code {code})"
                             + (f": {text[-400:]}" if text else ". Was the recording cancelled?"))
