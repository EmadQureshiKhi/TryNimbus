"""Muse Voice Transcribe: push-to-talk speech to text.

Primary path is the realtime WebSocket (`wss://api.meta.ai/v1/asr/realtime`): audio streams
while the key is held, so the transcript is ready almost as soon as it is released. Every PCM
chunk is also kept locally, so if the socket fails at any point the same audio is re-sent to the
one-shot HTTP endpoint, and if that fails too, to Muse Spark's audio input. The user never has
to repeat themselves because a transport broke.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import wave
from typing import Any

import httpx
import websockets

from nimbus import config
from nimbus.meta.client import MetaClient, text_part, user
from nimbus.meta.resilience import MetaAPIError, RetryPolicy, with_retries

log = logging.getLogger(__name__)

STREAM_URL = "wss://api.meta.ai/v1/asr/realtime"
TRANSCRIBE_URL = "https://api.meta.ai/v1/asr/transcribe"
SAMPLE_RATE = 24000
ENCODING = "PCM_24KHZ"


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class PushToTalkSession:
    """One utterance. `feed()` from the audio thread (via the loop), `finish()` on key release."""

    def __init__(self, client: MetaClient, *, keywords: list[str] | None = None) -> None:
        self.client = client
        self.keywords = keywords or []
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pcm = bytearray()
        self._task: asyncio.Task[str] | None = None
        self._partial = ""
        self._cancelled = False
        self.on_partial = None  # callable(str) for live captions

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._stream())

    def feed(self, chunk: bytes) -> None:
        if self._cancelled:
            return
        self._pcm.extend(chunk)
        self._queue.put_nowait(chunk)

    async def finish(self) -> str:
        self._queue.put_nowait(None)
        if len(self._pcm) < SAMPLE_RATE * 2 * 0.25:  # under 250 ms: nothing was said
            if self._task:
                self._task.cancel()
            return ""
        try:
            assert self._task is not None
            text = await asyncio.wait_for(self._task, timeout=20)
            if text.strip():
                return tidy(text)
            log.warning("realtime transcript empty; retrying with the one-shot endpoint")
        except Exception as exc:
            log.warning("realtime transcription failed (%s); falling back", exc)
        return tidy(await transcribe_pcm(self.client, bytes(self._pcm), keywords=self.keywords))

    def cancel(self) -> None:
        """Drop the utterance: the stream is closed, buffered audio discarded, nothing is transcribed."""
        self._cancelled = True
        self._pcm.clear()
        if self._task:
            self._task.cancel()

    async def _stream(self) -> str:
        key = config.get_secret("MODEL_API_KEY")
        if not key:
            raise MetaAPIError("MODEL_API_KEY missing", status=401)
        handshake: dict[str, Any] = {
            "mode": "PUSH_TO_TALK",
            "authorization": {"accessToken": key},
            "audioEncoding": ENCODING,
            "model": self.client.settings.models.transcribe,
            "partialMode": "CUMULATIVE",
            "emitAudioProgress": False,
        }
        if self.keywords:
            handshake["keywords"] = self.keywords[:50]
        async with websockets.connect(STREAM_URL, max_size=None, open_timeout=10) as ws:
            await ws.send(json.dumps(handshake))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if ack.get("type") == "error":
                raise MetaAPIError(f"voice handshake rejected: {ack.get('message')}", status=400)

            async def pump() -> None:
                while True:
                    chunk = await self._queue.get()
                    if chunk is None:
                        await ws.send(json.dumps({"type": "endStream"}))
                        return
                    await ws.send(chunk)

            sender = asyncio.ensure_future(pump())
            finals: list[str] = []
            turns: list[str] = []
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        continue
                    event = json.loads(message)
                    kind = event.get("type")
                    if kind == "error":
                        raise MetaAPIError(f"voice stream error: {event.get('message')}", retryable=True)
                    if kind == "transcript":
                        if event.get("final"):
                            finals.append(event.get("transcript", ""))
                        else:
                            self._partial = event.get("transcript", "")
                            if self.on_partial:
                                self.on_partial(tidy(" ".join(finals + [self._partial])))
                    elif kind == "speechComplete":
                        turns.append(event.get("transcript", ""))
            except websockets.exceptions.ConnectionClosedOK:
                pass
            finally:
                sender.cancel()
            text = " ".join(t.strip() for t in turns if t.strip()) or " ".join(f.strip() for f in finals if f.strip())
            return text or self._partial


# English doubles that are real words in a row, not a stutter of the transcriber.
_REAL_DOUBLES = {"had", "that", "is", "do", "very", "really", "so", "no", "bye", "ha", "go"}
_REPEAT = re.compile(r"\b((?:[\w']+\s+){0,2}[\w']+)(\s+\1\b)+", re.IGNORECASE)


def tidy(text: str) -> str:
    """Muse Voice Transcribe sometimes stutters ("I want want to", "walk me walk me through"): an
    immediately repeated word or 2-3 word phrase is said once. Real doubles ("had had") are kept."""
    def once(m: re.Match) -> str:
        phrase = m.group(1)
        if " " not in phrase and phrase.lower() in _REAL_DOUBLES:
            return m.group(0)
        return phrase

    return _REPEAT.sub(once, text.strip())


async def transcribe_pcm(client: MetaClient, pcm: bytes, *, keywords: list[str] | None = None) -> str:
    """One-shot HTTP transcription of a whole utterance, with Spark audio as the last resort."""
    wav = pcm_to_wav(pcm)
    try:
        return await transcribe_wav(client, wav, keywords=keywords)
    except MetaAPIError as exc:
        log.warning("one-shot transcription failed (%s); asking Muse Spark to transcribe", exc)
    reply = await client.respond(
        "muse-spark-1.2",
        [user(text_part("Transcribe this audio. Return only the transcript."),
              {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}})],
        max_output_tokens=4000,
        reasoning_effort="low",
    )
    return reply.text.strip()


async def transcribe_wav(client: MetaClient, wav: bytes, *, keywords: list[str] | None = None,
                         mode: str = "PUSH_TO_TALK") -> str:
    key = config.get_secret("MODEL_API_KEY")
    if not key:
        raise MetaAPIError("MODEL_API_KEY missing", status=401)
    request: dict[str, Any] = {"mode": mode, "model": client.settings.models.transcribe, "audioEncoding": "WAV"}
    if keywords:
        request["keywords"] = keywords[:50]

    async def call() -> str:
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(
                TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"request": (None, json.dumps(request), "application/json"),
                       "audio": ("speech.wav", wav, "audio/wav")},
            )
            resp.raise_for_status()
            data = resp.json()
        return (data.get("transcript") or " ".join(t.get("transcript", "") for t in data.get("turns", []))).strip()

    return await with_retries(call, key="asr:transcribe", policy=RetryPolicy(attempts=3))
