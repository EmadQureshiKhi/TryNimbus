"""The recorder's command line and lifecycle. A stand-in for screencapture is used throughout, so
these tests never record the real screen or microphone."""
from __future__ import annotations

import asyncio
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from nimbus.repro import recorder
from nimbus.repro.recorder import (
    PermissionDenied,
    RecorderError,
    ScreenRecorder,
    build_command,
    display_number,
    import_video,
)


def test_command_for_an_open_ended_narrated_recording() -> None:
    argv = build_command(Path("/tmp/bug.mov"), display=2, max_duration=None)
    assert argv == ["/usr/sbin/screencapture", "-x", "-v", "-g", "-k", "-D", "2", "/tmp/bug.mov"]


def test_command_with_a_cap_and_no_audio() -> None:
    argv = build_command(Path("/tmp/bug.mov"), display=None, audio=False, show_clicks=False, max_duration=90.4)
    assert argv == ["/usr/sbin/screencapture", "-x", "-V", "90", "/tmp/bug.mov"]


def test_command_rejects_display_zero() -> None:
    with pytest.raises(ValueError):
        build_command(Path("/tmp/bug.mov"), display=0)


def test_display_numbers_put_the_main_display_first() -> None:
    assert display_number(7, [3, 7, 9], main_id=3) == 2
    assert display_number(3, [9, 3], main_id=3) == 1
    assert display_number(9, [9, 3], main_id=3) == 2
    assert display_number(5, [9, 3], main_id=3) is None


def fake_binary(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-screencapture"
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


RECORD_UNTIL_SIGINT = """
out="${@: -1}"
echo "$@" > "$(dirname "$out")/argv.txt"
trap 'printf "fake movie" > "$out"; exit 0' INT
while true; do sleep 0.05; done
"""


async def test_stop_on_demand_finalises_the_file(tmp_path: Path) -> None:
    rec = ScreenRecorder(tmp_path / "out", binary=fake_binary(tmp_path, RECORD_UNTIL_SIGINT), display=1,
                         check_permissions=False, max_duration=120)
    path = await rec.start()
    assert rec.is_recording and path.suffix == ".mov"
    await asyncio.sleep(0.3)
    recording = await rec.stop()
    assert not rec.is_recording
    assert recording.path == path and path.read_text() == "fake movie"
    assert recording.source == "screen" and recording.display == 1 and recording.duration_s >= 1.0
    argv = (tmp_path / "out" / "argv.txt").read_text().split()
    assert argv[:6] == ["-x", "-V", "120", "-g", "-k", "-D"]
    assert await rec.stop() is recording  # stopping twice is harmless


async def test_the_duration_cap_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = ScreenRecorder(tmp_path / "out", binary=fake_binary(tmp_path, RECORD_UNTIL_SIGINT), display=1,
                         check_permissions=False, max_duration=0.2)
    monkeypatch.setattr(recorder, "CAP_GRACE_S", 0.1)
    await rec.start()
    recording = await asyncio.wait_for(rec.wait(), timeout=10)
    assert recording.path.exists() and not rec.is_recording


async def test_a_permission_failure_says_where_to_grant_it(tmp_path: Path) -> None:
    binary = fake_binary(tmp_path, 'echo "screencapture: not authorized to capture the screen" >&2\nexit 1\n')
    rec = ScreenRecorder(tmp_path / "out", binary=binary, display=1, check_permissions=False,
                         startup_grace=10.0)
    with pytest.raises(PermissionDenied, match="Privacy & Security"):
        await rec.start()
    assert not rec.is_recording


async def test_an_immediate_generic_failure_is_reported(tmp_path: Path) -> None:
    rec = ScreenRecorder(tmp_path / "out", binary=fake_binary(tmp_path, "echo 'bad option' >&2\nexit 2\n"),
                         display=1, check_permissions=False,
                         startup_grace=10.0)
    with pytest.raises(RecorderError, match="exit code 2") as info:
        await rec.start()
    assert not isinstance(info.value, PermissionDenied)


async def test_a_cancelled_recording_without_a_file_is_an_error(tmp_path: Path) -> None:
    binary = fake_binary(tmp_path, "trap 'exit 0' INT\nwhile true; do sleep 0.05; done\n")
    rec = ScreenRecorder(tmp_path / "out", binary=binary, display=1, check_permissions=False)
    await rec.start()
    with pytest.raises(RecorderError, match="without a recording"):
        await rec.stop()


async def test_a_missing_screen_permission_fails_before_starting(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "screen_recording_allowed", lambda: False)
    marker = tmp_path / "ran"
    rec = ScreenRecorder(tmp_path / "out", binary=fake_binary(tmp_path, f"touch {marker}\n"), display=1)
    with pytest.raises(PermissionDenied):
        await rec.start()
    assert not marker.exists()


async def test_a_denied_microphone_records_without_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "screen_recording_allowed", lambda: True)
    monkeypatch.setattr(recorder, "microphone_status", lambda: "denied")
    rec = ScreenRecorder(tmp_path / "out", binary=fake_binary(tmp_path, RECORD_UNTIL_SIGINT), display=1)
    await rec.start()
    recording = await rec.stop()
    argv = (tmp_path / "out" / "argv.txt").read_text().split()
    assert "-g" not in argv and not recording.has_audio
    assert any("Microphone" in w for w in recording.warnings)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg to make a sample video")
def test_import_accepts_a_real_video_and_copies_it(tmp_path: Path) -> None:
    src = tmp_path / "sample.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "1",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    recording = import_video(src, tmp_path / "run")
    assert recording.source == "import" and recording.path == tmp_path / "run" / "sample.mp4"
    assert 0.8 < recording.duration_s < 1.5 and not recording.has_audio


def test_import_rejects_missing_and_non_video_files(tmp_path: Path) -> None:
    with pytest.raises(RecorderError, match="does not exist"):
        import_video(tmp_path / "nope.mov")
    notes = tmp_path / "notes.txt"
    notes.write_text("x")
    with pytest.raises(RecorderError, match="not a supported video"):
        import_video(notes)
    broken = tmp_path / "broken.mov"
    broken.write_text("not a movie")
    with pytest.raises(RecorderError):
        import_video(broken)
