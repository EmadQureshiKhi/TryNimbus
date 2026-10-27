"""Retry, backoff and circuit breaking for every Meta API call.

Follows the Model API error guidance: retry 429, 5xx, timeouts and dropped connections with
exponential backoff plus jitter; never retry 400/401/403/404, because a malformed request or a
bad key fails the same way every time. A circuit breaker per model stops hammering a model
that keeps failing, which is what lets the caller fall through to the next model in its chain.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx
import openai

log = logging.getLogger(__name__)

T = TypeVar("T")


class MetaAPIError(RuntimeError):
    """A Meta API call failed permanently (after retries, or on a non-retryable status)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class CircuitOpenError(MetaAPIError):
    pass


def status_of(exc: BaseException) -> int | None:
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return getattr(exc, "status", None)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (CircuitOpenError,)):
        return False
    if isinstance(exc, MetaAPIError):
        return exc.retryable
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError, asyncio.TimeoutError,
                        httpx.TimeoutException, httpx.TransportError, ConnectionError)):
        return True
    status = status_of(exc)
    if status is None:
        return False
    return status == 429 or status == 408 or status >= 500


def describe(exc: BaseException) -> str:
    status = status_of(exc)
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("error", {}).get("message", "") or "")
    message = message or str(exc)
    return f"[{status}] {message}" if status else f"{type(exc).__name__}: {message}"


@dataclass
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.6
    max_delay: float = 12.0

    def delay(self, attempt: int, exc: BaseException | None = None) -> float:
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                retry_after = float(response.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                retry_after = None
        if retry_after is not None:
            return min(self.max_delay, max(0.0, retry_after))
        return min(self.max_delay, self.base_delay * (2 ** attempt)) + random.uniform(0, 0.4)


class CircuitBreaker:
    """Opens after `threshold` consecutive retryable failures; half-opens after `cooldown`."""

    def __init__(self, threshold: int = 4, cooldown: float = 45.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            self.opened_at = None
            return True
        return False

    def record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
            self.opened_at = None
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


_breakers: dict[str, CircuitBreaker] = {}


def breaker_for(key: str) -> CircuitBreaker:
    if key not in _breakers:
        _breakers[key] = CircuitBreaker()
    return _breakers[key]


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    key: str,
    policy: RetryPolicy | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run `fn` with retries for transient failures, guarded by the breaker for `key`."""
    policy = policy or RetryPolicy()
    breaker = breaker_for(key)
    last: BaseException | None = None
    for attempt in range(policy.attempts):
        if not breaker.allow():
            raise CircuitOpenError(f"{key} is cooling down after repeated failures")
        try:
            result = await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # classified below
            last = exc
            retryable = is_retryable(exc)
            if retryable:
                breaker.record(ok=False)
            if isinstance(exc, MetaAPIError) and not retryable:
                raise  # already classified (e.g. MissingKeyError): keep its type
            if not retryable or attempt == policy.attempts - 1:
                raise MetaAPIError(f"{key}: {describe(exc)}", status=status_of(exc),
                                   retryable=retryable) from exc
            wait = policy.delay(attempt, exc)
            log.warning("%s attempt %d failed (%s); retrying in %.1fs", key, attempt + 1,
                        describe(exc), wait)
            if on_retry:
                on_retry(attempt, exc, wait)
            await asyncio.sleep(wait)
        else:
            breaker.record(ok=True)
            return result
    raise MetaAPIError(f"{key}: exhausted retries ({last})")  # pragma: no cover


async def first_success(
    models: list[str],
    call: Callable[[str], Awaitable[T]],
    *,
    what: str,
) -> T:
    """Try each model in order; move on only when a model fails for a reason a different model
    could plausibly avoid (unavailable model, server trouble, open circuit). A 400 caused by the
    request itself would fail on every model, so it is raised immediately."""
    errors: list[str] = []
    for model in models:
        try:
            return await call(model)
        except MetaAPIError as exc:
            errors.append(f"{model}: {exc}")
            if exc.status in (401, 403):
                raise  # a key problem fails the same on every model
            if exc.status == 400 and "model" not in str(exc).lower():
                raise
            log.warning("%s: %s failed, trying next model", what, model)
    raise MetaAPIError(f"{what} failed on every model: " + " | ".join(errors))
