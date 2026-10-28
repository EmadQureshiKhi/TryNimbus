"""Microphone, global hotkey, global click listener and spoken output.

- Microphone: 24 kHz mono PCM16 in 80 ms chunks, the native format of Muse Voice Transcribe.
- Hotkey: hold-to-talk chord (default Control+Option+Space) through a global key listener; needs
  Input Monitoring permission for the app that launched Nimbus.
- Clicks: a global mouse listener so Shadow knows when the person acted and can verify the step.
- Speech: the macOS system voice (`say`), cancellable mid-sentence. Meta has no text-to-speech
  model, so spoken output is a local OS feature; captions always show the same text.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import shutil
import subprocess
import threading
from collections.abc import Callable

from nimbus.speakable import speakable

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000
CHUNK_FRAMES = SAMPLE_RATE * 80 // 1000


class Microphone:
    """Push-to-talk capture: `start(on_chunk, on_level)` ... `stop()`.

    Safe to call from any thread and in any order: `start` closes a stream that is still open, and
    a stream is never dropped while PortAudio may still call into it (a collected callback segfaults).
    """

    def __init__(self) -> None:
        self._stream = None
        self._lock = threading.Lock()
        self._unclosed: list = []  # streams that refused to close; kept alive so their callback stays valid

    def start(self, on_chunk: Callable[[bytes], None], on_level: Callable[[float], None] | None = None) -> None:
        import numpy as np
        import sounddevice as sd

        def callback(indata, frames, _time, status) -> None:
            if status:
                log.debug("mic status: %s", status)
            try:
                data = bytes(indata)
                on_chunk(data)
                if on_level:
                    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
                    on_level(min(1.0, rms * 4))
            except Exception as exc:  # never let a consumer error abort the audio thread
                log.debug("mic consumer failed: %s", exc)

        with self._lock:
            self._close_locked()
            stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                       blocksize=CHUNK_FRAMES, callback=callback)
            try:
                stream.start()
            except Exception:
                self._retire(stream)
                raise
            self._stream = stream

    def stop(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            self._retire(stream)

    def _retire(self, stream) -> None:
        try:
            stream.abort()
            stream.close()
        except Exception as exc:
            log.warning("microphone stream did not close cleanly: %s", exc)
            self._unclosed.append(stream)

    @staticmethod
    def available() -> bool:
        try:
            import sounddevice as sd

            return any(d.get("max_input_channels", 0) > 0 for d in sd.query_devices())
        except Exception:
            return False


class HoldHotkey:
    """Calls `on_press` when the whole chord goes down and `on_release` when any key of it lifts."""

    def __init__(self, chord: str, on_press: Callable[[], None], on_release: Callable[[], None],
                 on_escape: Callable[[], None] | None = None,
                 on_key: Callable[[bool], None] | None = None) -> None:
        from pynput import keyboard

        self._kb = keyboard
        self.chord = {self._parse(k) for k in chord.split("+")}
        self.on_press, self.on_release, self.on_escape = on_press, on_release, on_escape
        self.on_key = on_key  # every other key press (True for Return): Shadow's typing steps
        self._down: set = set()
        self._active = False
        self._listener = None

    def _parse(self, key: str):
        kb = self._kb
        key = key.strip().lower()
        named = {"<ctrl>": kb.Key.ctrl, "<alt>": kb.Key.alt, "<cmd>": kb.Key.cmd, "<shift>": kb.Key.shift,
                 "<space>": kb.Key.space}
        return named.get(key) or kb.KeyCode.from_char(key)

    def _norm(self, key):
        kb = self._kb
        aliases = {kb.Key.ctrl_l: kb.Key.ctrl, kb.Key.ctrl_r: kb.Key.ctrl, kb.Key.alt_l: kb.Key.alt,
                   kb.Key.alt_r: kb.Key.alt, kb.Key.cmd_l: kb.Key.cmd, kb.Key.cmd_r: kb.Key.cmd,
                   kb.Key.shift_l: kb.Key.shift, kb.Key.shift_r: kb.Key.shift}
        return aliases.get(key, key)

    def start(self) -> None:
        def press(key) -> None:
            k = self._norm(key)
            if k == self._kb.Key.esc and self.on_escape:
                self.on_escape()
            self._down.add(k)
            if not self._active and self.chord <= self._down:
                self._active = True
                self.on_press()
            elif self.on_key and not self._active and k != self._kb.Key.esc:
                self.on_key(k == self._kb.Key.enter)

        def release(key) -> None:
            k = self._norm(key)
            self._down.discard(k)
            if self._active and k in self.chord:
                self._active = False
                self.on_release()

        self._listener = self._kb.Listener(on_press=press, on_release=release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()


class ClickListener:
    def __init__(self, on_click: Callable[[float, float], None]) -> None:
        self.on_click = on_click
        self.suppressed = threading.Event()
        self._listener = None

    def start(self) -> None:
        from pynput import mouse

        def click(x, y, _button, pressed) -> None:
            if not pressed and not self.suppressed.is_set():
                self.on_click(float(x), float(y))

        self._listener = mouse.Listener(on_click=click)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()


# Apple's free neural voices, best first. They ship as optional downloads (System Settings >
# Accessibility > Spoken Content > System Voice > Manage Voices); the first one installed wins.
PREFERRED_VOICES = [
    "Ava (Premium)", "Zoe (Premium)", "Evan (Premium)", "Nathan (Premium)", "Noelle (Premium)",
    "Ava (Enhanced)", "Zoe (Enhanced)", "Evan (Enhanced)", "Nathan (Enhanced)", "Allison (Enhanced)",
    "Samantha (Enhanced)", "Serena (Premium)", "Daniel (Enhanced)", "Samantha",
]


def installed_voices() -> list[str]:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    return [re.split(r"\s{2,}", line.strip())[0] for line in out.splitlines() if line.strip()]


def best_voice(installed: list[str] | None = None) -> str:
    names = installed if installed is not None else installed_voices()
    return next((v for v in PREFERRED_VOICES if v in names), "")


class Speaker:
    """macOS system voice. `await speak(text)` returns when finished or interrupted. Text is
    normalised for speech first (symbols, maths, URLs, file names, acronyms); captions keep the
    original."""

    def __init__(self, voice: str = "", rate: int = 0) -> None:
        self.enabled = shutil.which("say") is not None
        self.voice = voice or (best_voice() if self.enabled else "")
        # Neural voices sound rushed at the classic voices' rate.
        self.rate = rate or (185 if "(" in self.voice else 195)
        self._proc: subprocess.Popen | None = None

    async def speak(self, text: str) -> None:
        text = speakable(text)
        if not text:
            return
        if not self.enabled:
            await asyncio.sleep(min(6.0, 0.9 + len(text) / 16))
            return
        self.stop()
        cmd = ["say", "-r", str(self.rate)]
        if self.voice:
            cmd += ["-v", self.voice]
        self._proc = subprocess.Popen(cmd + [text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = self._proc
        await asyncio.to_thread(proc.wait)

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None


def reading_time(text: str) -> float:
    """Seconds a caption should stay up when it is not spoken."""
    return max(1.6, min(7.0, 0.8 + len(text.split()) * 0.32))


def level_curve(rms: float) -> float:
    return max(0.0, min(1.0, math.sqrt(rms)))
