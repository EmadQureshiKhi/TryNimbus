"""Start, watch and stop the web app under test.

Repro needs the app answering at `ReproSettings.app_url` before it can reproduce anything, and it
needs fresh code served after the fix. If something already answers at the URL (the developer's
own dev server), Repro uses it and never stops it. Otherwise it runs `start_command` in the repo,
in its own process group so the whole server tree can be stopped, and owns it until `stop()`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import signal
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class AppLaunchError(RuntimeError):
    pass


class AppServer:
    def __init__(self, url: str, start_command: str, cwd: Path, *, startup_timeout: float = 45.0,
                 log_path: Path | None = None) -> None:
        self.url = url
        self.start_command = start_command
        self.cwd = Path(cwd)
        self.startup_timeout = startup_timeout
        self.log_path = log_path
        self._proc: asyncio.subprocess.Process | None = None
        self._log_file = None

    @property
    def owned(self) -> bool:
        """True when this object started the server (and so may restart or stop it)."""
        return self._proc is not None and self._proc.returncode is None

    async def is_reachable(self, timeout: float = 2.0) -> bool:
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as http:
                response = await http.get(self.url)
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    async def ensure(self) -> bool:
        """Make the app reachable. Returns True if this call started it."""
        if await self.is_reachable():
            log.info("app already reachable at %s", self.url)
            return False
        await self._start()
        return True

    async def restart(self) -> None:
        """Restart a server we own so it serves the fixed code; a foreign server is left alone."""
        if not self.owned:
            if not await self.is_reachable():
                await self._start()
            return
        await self.stop()
        await self._start()

    async def _start(self) -> None:
        argv = shlex.split(self.start_command)
        if not argv:
            raise AppLaunchError("ReproSettings.start_command is empty and the app is not running")
        exe = shutil.which(argv[0], path=os.environ.get("PATH")) or argv[0]
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self.log_path, "ab")  # noqa: SIM115 - closed in stop()
        out = self._log_file or asyncio.subprocess.DEVNULL
        log.info("starting app: %s (cwd=%s)", self.start_command, self.cwd)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *argv[1:], cwd=str(self.cwd), stdout=out, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            raise AppLaunchError(f"could not run {self.start_command!r}: {exc}") from exc

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                raise AppLaunchError(f"{self.start_command!r} exited with code {self._proc.returncode} before "
                                     f"{self.url} answered.{self._log_tail()}")
            if await self.is_reachable(timeout=1.0):
                log.info("app is up at %s (pid %s)", self.url, self._proc.pid)
                return
            await asyncio.sleep(0.3)
        await self.stop()
        raise AppLaunchError(f"{self.url} did not answer within {self.startup_timeout:.0f}s after running "
                             f"{self.start_command!r}.{self._log_tail()}")

    def _log_tail(self) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        if self._log_file:
            self._log_file.flush()
        tail = self.log_path.read_bytes()[-1500:].decode(errors="replace").strip()
        return f"\nServer output:\n{tail}" if tail else ""

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                    break
                except asyncio.TimeoutError:
                    continue
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    async def __aenter__(self) -> "AppServer":
        await self.ensure()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
