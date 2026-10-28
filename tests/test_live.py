"""Live checks against the real Meta Model API. Run with: .venv/bin/python -m pytest -m live -q"""
from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from nimbus.engine.grounding import Grounder
from nimbus.meta.client import MetaClient, text_part, user
from nimbus.meta.voice import transcribe_wav

pytestmark = pytest.mark.live
FIXTURES = Path(__file__).parent / "fixtures" / "ui"


@pytest.fixture(scope="module")
def client() -> MetaClient:
    c = MetaClient()
    if not c.has_key():
        pytest.skip("MODEL_API_KEY not set")
    return c


async def test_ping_and_structured_output(client):
    data, reply = await client.respond_json(
        ["muse-spark-1.3"], [user(text_part("Return the capital of France and its population in millions."))],
        schema={"type": "object", "properties": {"city": {"type": "string"}, "millions": {"type": "number"}},
                "required": ["city", "millions"]},
        schema_name="city", what="ping", reasoning_effort="low",
    )
    assert data["city"].lower() == "paris"
    assert reply.model == "muse-spark-1.3"


async def test_grounding_finds_new_chat_button(client):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        await page.goto((FIXTURES / "chat_app.html").as_uri())
        png = await page.screenshot()
        box = await page.locator("#new-chat").bounding_box()
        await browser.close()
    hit = await Grounder(client).locate(png, "New chat button", description="top of the left sidebar")
    assert hit is not None
    w, _ = Image.open(io.BytesIO(png)).size
    scale = w / 1440
    x, y = hit.x / scale, hit.y / scale
    assert box["x"] - 4 <= x <= box["x"] + box["width"] + 4
    assert box["y"] - 4 <= y <= box["y"] + box["height"] + 4


async def test_voice_transcribe_on_generated_speech(client):
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = Path(tmp) / "q.aiff", Path(tmp) / "q.wav"
        subprocess.run(["say", "-o", str(aiff), "How do I open a new chat?"], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", "24000",
                        "-sample_fmt", "s16", str(wav)], check=True)
        text = await transcribe_wav(client, wav.read_bytes())
    assert "new chat" in text.lower()


async def test_realtime_push_to_talk_stream(client):
    import asyncio
    import wave

    from nimbus.meta.voice import PushToTalkSession

    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = Path(tmp) / "q.aiff", Path(tmp) / "q.wav"
        subprocess.run(["say", "-o", str(aiff), "Where is the blade tool?"], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", "24000",
                        "-sample_fmt", "s16", str(wav)], check=True)
        with wave.open(str(wav)) as w:
            pcm = w.readframes(w.getnframes())
    session = PushToTalkSession(client)
    session.start()
    chunk = 24000 * 2 * 80 // 1000
    for i in range(0, len(pcm), chunk):  # paced like a live microphone
        session.feed(pcm[i : i + chunk])
        await asyncio.sleep(0.08)
    text = await session.finish()
    assert "blade" in text.lower()
