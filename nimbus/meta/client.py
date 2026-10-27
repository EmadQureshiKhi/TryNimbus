"""Meta Model API client: one place for auth, transport, structured output and file uploads.

Everything goes through the Responses API on `https://api.meta.ai/v1` with the plain OpenAI SDK
(the Model API cookbook says the official client package is out of date). The SDK's own retries
are disabled; `resilience.with_retries` owns retry policy so it is uniform across Spark, SAM,
the Files API and the open-weights partner endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import mimetypes
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai
from PIL import Image

from nimbus import config
from nimbus.meta.resilience import MetaAPIError, RetryPolicy, first_success, with_retries

log = logging.getLogger(__name__)

META_BASE_URL = "https://api.meta.ai/v1"


class MissingKeyError(MetaAPIError):
    pass


# ---------------------------------------------------------------- content helpers


def image_part(png_or_jpeg: bytes, *, detail_max: int | None = None) -> dict[str, Any]:
    """An `input_image` block from raw image bytes (base64 data URL)."""
    data = png_or_jpeg
    if detail_max:
        data = downscale_jpeg(data, detail_max)
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return {"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}


def text_part(text: str) -> dict[str, Any]:
    return {"type": "input_text", "text": text}


def user(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": list(parts)}


def downscale_jpeg(data: bytes, max_side: int, quality: int = 88) -> bytes:
    img = Image.open(io.BytesIO(data))
    img.load()
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_loose(text: str) -> Any:
    """Parse JSON that may be wrapped in a code fence or surrounded by prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fenced = _JSON_FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except ValueError:
            pass
    pairs = sorted((("{", "}"), ("[", "]")), key=lambda p: (text.find(p[0]) if p[0] in text else len(text)))
    for opener, closer in pairs:  # the outermost value is whichever bracket opens first
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise ValueError(f"no JSON object in model output: {text[:200]!r}")


@dataclass
class Reply:
    text: str
    model: str
    response_id: str | None
    raw: Any
    usage: dict[str, Any] | None = None


# ---------------------------------------------------------------- client


class MetaClient:
    """Async client for the Meta Model API (and the open-weights partner endpoint)."""

    def __init__(self, store: config.SettingsStore | None = None) -> None:
        self.store = store or config.SettingsStore()
        self._clients: dict[str, openai.AsyncOpenAI] = {}
        self._file_cache_path = config.data_dir("cache") / "files.json"
        self._file_cache_lock = threading.Lock()

    @property
    def settings(self) -> config.Settings:
        return self.store.settings

    # -- transport

    def _api(self, *, open_weights: bool = False) -> openai.AsyncOpenAI:
        key_name = self.settings.open_weights.api_key_env if open_weights else "MODEL_API_KEY"
        key = config.get_secret(key_name)
        if not key:
            raise MissingKeyError(
                f"{key_name} is not set. Add it to {config.REPO_ROOT / '.env'} or the Nimbus panel.",
                status=401,
            )
        base = self.settings.open_weights.base_url if open_weights else META_BASE_URL
        cache_key = f"{base}|{hashlib.sha256(key.encode()).hexdigest()[:12]}"
        if cache_key not in self._clients:
            self._clients[cache_key] = openai.AsyncOpenAI(
                base_url=base, api_key=key, max_retries=0, timeout=openai.Timeout(180.0, connect=15.0)
            )
        return self._clients[cache_key]

    def has_key(self) -> bool:
        return bool(config.get_secret("MODEL_API_KEY"))

    def open_weights_for(self, role: str) -> bool:
        ow = self.settings.open_weights
        return ow.enabled and role in ow.roles and bool(config.get_secret(ow.api_key_env))

    # -- Responses API

    async def respond(
        self,
        model: str,
        input: list[dict[str, Any]] | str,
        *,
        instructions: str | None = None,
        schema: dict[str, Any] | None = None,
        schema_name: str = "result",
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        previous_response_id: str | None = None,
        stream: bool = False,
        timeout: float | None = None,
        policy: RetryPolicy | None = None,
        open_weights: bool = False,
    ) -> Reply:
        kwargs: dict[str, Any] = {"model": model, "input": input}
        if instructions:
            kwargs["instructions"] = instructions
        if schema is not None:
            kwargs["text"] = {"format": {"type": "json_schema", "name": schema_name, "schema": schema}}
        if reasoning_effort:
            kwargs["reasoning"] = {"effort": reasoning_effort}
        if max_output_tokens:
            kwargs["max_output_tokens"] = max_output_tokens
        if tools:
            kwargs["tools"] = tools
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        if timeout:
            kwargs["timeout"] = timeout

        async def call() -> Reply:
            api = self._api(open_weights=open_weights)
            if stream:
                return await self._respond_streaming(api, kwargs)
            response = await api.responses.create(**kwargs)
            return self._reply(response, model)

        return await with_retries(call, key=f"responses:{model}", policy=policy)

    async def _respond_streaming(self, api: openai.AsyncOpenAI, kwargs: dict[str, Any]) -> Reply:
        """Stream to keep long video/reasoning calls from hitting idle timeouts."""
        final = None
        chunks: list[str] = []
        stream = await api.responses.create(stream=True, **kwargs)
        async with stream:
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "response.output_text.delta":
                    chunks.append(getattr(event, "delta", ""))
                elif etype in ("response.completed", "response.incomplete"):
                    final = getattr(event, "response", None)
                elif etype in ("response.failed", "error"):
                    err = getattr(event, "error", None) or getattr(getattr(event, "response", None), "error", None)
                    raise MetaAPIError(f"stream failed: {err}", retryable=True)
        if final is not None:
            reply = self._reply(final, kwargs["model"])
            if not reply.text and chunks:
                reply.text = "".join(chunks)
            return reply
        return Reply(text="".join(chunks), model=kwargs["model"], response_id=None, raw=None)

    @staticmethod
    def _reply(response: Any, model: str) -> Reply:
        text = getattr(response, "output_text", None) or ""
        if not text:
            for item in getattr(response, "output", []) or []:
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", "") == "output_text":
                        text += getattr(part, "text", "")
        status = getattr(response, "status", None)
        if status == "incomplete" and not text:
            details = getattr(response, "incomplete_details", None)
            raise MetaAPIError(f"{model} returned incomplete output: {details}", retryable=False)
        usage = getattr(response, "usage", None)
        return Reply(
            text=text,
            model=model,
            response_id=getattr(response, "id", None),
            raw=response,
            usage=usage.model_dump() if hasattr(usage, "model_dump") else None,
        )

    async def respond_json(
        self,
        models: list[str],
        input: list[dict[str, Any]],
        *,
        schema: dict[str, Any],
        schema_name: str,
        what: str,
        open_weights_role: str | None = None,
        **kwargs: Any,
    ) -> tuple[Any, Reply]:
        """Structured output with model fallback, a retry and a repair path.

        Structured output is decoded against the schema, so the loose parser is only a safety net for
        partners that ignore `text.format` (Spark Open on Baseten has no JSON mode). A reply that does
        not parse is first re-requested with the full original input (a repair from the broken text
        alone loses the images or video it was about), then repaired as text. `validate` lets the caller
        reject a well-formed but useless answer (an empty plan, say); a rejected answer moves on to the
        next model in the chain.
        """
        validate = kwargs.pop("validate", None)
        if open_weights_role and self.open_weights_for(open_weights_role):
            try:
                data, reply = await self._json_open_weights(input, schema=schema, what=what, **kwargs)
                if validate is None or validate(data):
                    return data, reply
            except MetaAPIError as exc:
                log.warning("%s on open weights failed (%s); using the Meta Model API", what, exc)

        def accept(data: Any, reply: Reply) -> tuple[Any, Reply]:
            if validate is not None and not validate(data):
                raise MetaAPIError(f"{what}: {reply.model} returned an unusable answer", status=503, retryable=True)
            return data, reply

        async def attempt(model: str) -> tuple[Any, Reply]:
            reply = await self.respond(model, input, schema=schema, schema_name=schema_name, **kwargs)
            try:
                return accept(parse_json_loose(reply.text), reply)
            except ValueError:
                log.warning("%s: %s returned unparseable JSON; asking again with the full input", what, model)
            retry = await self.respond(model, input, schema=schema, schema_name=schema_name, **kwargs)
            try:
                return accept(parse_json_loose(retry.text), retry)
            except ValueError:
                log.warning("%s: %s still unparseable; repairing the text", what, model)
            repair = await self.respond(
                model,
                [user(text_part(
                    "Rewrite the following as a single valid JSON value that matches the "
                    f"schema. Output only JSON.\nSchema: {json.dumps(schema)}\n\n{retry.text or reply.text}"
                ))],
                schema=schema,
                schema_name=schema_name,
                reasoning_effort="low",
            )
            try:
                return accept(parse_json_loose(repair.text), repair)
            except ValueError as exc:
                raise MetaAPIError(f"{what}: model output was not valid JSON", status=503, retryable=True) from exc

        return await first_success(models, attempt, what=what)

    async def _json_open_weights(self, input: list[dict[str, Any]], *, schema: dict[str, Any], what: str,
                                 **kwargs: Any) -> tuple[Any, Reply]:
        ow = self.settings.open_weights
        guided = list(input) + [user(text_part(
            "Respond with only a JSON value matching this JSON Schema, no prose:\n" + json.dumps(schema)
        ))]
        kwargs.pop("reasoning_effort", None)
        reply = await self.respond(ow.model, guided, open_weights=True, reasoning_effort="low", **kwargs)
        try:
            return parse_json_loose(reply.text), reply
        except ValueError as exc:
            raise MetaAPIError(f"{what}: open-weights output was not JSON", retryable=True) from exc

    # -- Files API

    def _file_cache(self) -> dict[str, str]:
        try:
            return json.loads(self._file_cache_path.read_text())
        except (OSError, ValueError):
            return {}

    async def upload_file(self, path: Path, *, force: bool = False) -> str:
        """Upload once per content hash; return the `file-...` id."""
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._file_cache_lock:
            cached = self._file_cache().get(digest)
        if cached and not force:
            return cached
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        async def call() -> str:
            uploaded = await self._api().files.create(
                file=(path.name, path.read_bytes(), mime), purpose="user_data"
            )
            file_id = getattr(uploaded, "id", None)
            if not isinstance(file_id, str) or not file_id.startswith("file-"):
                raise MetaAPIError(f"Files API returned an invalid id: {file_id!r}")
            return file_id

        file_id = await with_retries(call, key="files", policy=RetryPolicy(attempts=5))
        with self._file_cache_lock:
            cache = self._file_cache()
            cache[digest] = file_id
            self._file_cache_path.write_text(json.dumps(cache, indent=1))
        return file_id

    async def respond_with_file(self, path: Path, build: Any, **kwargs: Any) -> Any:
        """Call `build(file_id)` -> awaitable; if the cached file id is gone, re-upload once."""
        file_id = await self.upload_file(path)
        try:
            return await build(file_id)
        except MetaAPIError as exc:
            if exc.status in (404, 400) and "file" in str(exc).lower():
                log.info("file id %s rejected; re-uploading %s", file_id, path.name)
                return await build(await self.upload_file(path, force=True))
            raise


def image_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size
