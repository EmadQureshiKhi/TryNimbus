"""The repository sandbox the coding agent works inside.

The fallback fixer lets Muse Spark read and write files and run tests through function tools. A
model that can name any path or run any command is a model that can read `~/.ssh` or run
`curl | sh`, so every tool call goes through this class:

- Paths are resolved (symlinks included) and must stay inside the repo root. `.git` and files
  that typically hold secrets are off limits, so the agent can neither rewrite history nor read
  keys it would then echo into a diff.
- The only command it can run is the configured test command, exactly. No shell is involved, so
  `;`, `&&` and redirections are just arguments that fail to match the allowlist.
- Every command has a wall-clock timeout and runs in its own process group, so a test that hangs
  or forks a server is killed as a whole. Output is capped before it goes back to the model.
- Commands run with an environment stripped of API keys, so a test cannot leak them either.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import shlex
import shutil
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 500_000
MAX_OUTPUT_CHARS = 12_000
MAX_LIST_ENTRIES = 400
MAX_SEARCH_HITS = 80

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", "dist", "build",
                       ".next", ".cache"})
# Files an agent has no business reading or writing: they hold credentials, not code.
SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*", ".npmrc", ".netrc",
                   ".pypirc", "credentials*", "*.keychain*")
# Environment variables that must never reach a test process.
SECRET_ENV = re.compile(r"(API_KEY|_TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)


class SandboxError(PermissionError):
    """A tool call tried to leave the sandbox (path escape, forbidden file, disallowed command)."""


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int | None
    output: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self, limit: int = MAX_OUTPUT_CHARS) -> str:
        head = f"$ {shlex.join(self.argv)}\nexit={self.exit_code}{' (timed out)' if self.timed_out else ''} " \
               f"in {self.duration_s:.1f}s\n"
        return head + truncate(self.output, limit)


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and the tail: test runners print the failures last and the setup first."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters omitted] ...\n{text[-half:]}"


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env.setdefault("CI", "1")  # test runners stop prompting and watching
    env.update(extra or {})
    return env


async def run_command(argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str] | None = None,
                      should_stop: Callable[[], bool] | None = None, stdin_data: bytes | None = None,
                      on_output: Callable[[bytes], None] | None = None) -> CommandResult:
    """Run `argv` (no shell) in its own process group; kill the whole group on timeout or stop.
    `stdin_data` is written once and stdin closed (used to hand a secret over without argv or env).
    `on_output` sees the combined output as it arrives (a live log); its errors are ignored."""
    started = time.monotonic()
    exe = shutil.which(argv[0]) or argv[0]
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, *argv[1:], cwd=str(cwd), env=env if env is not None else scrubbed_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return CommandResult(argv, None, f"could not start {argv[0]}: {exc}", 0.0)
    if stdin_data is not None and proc.stdin is not None:
        proc.stdin.write(stdin_data)
        await proc.stdin.drain()
        proc.stdin.close()

    chunks: list[bytes] = []

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            block = await proc.stdout.read(65536)
            if not block:
                return
            chunks.append(block)
            if on_output:
                try:
                    on_output(block)
                except Exception as exc:  # a broken log must not stop the command
                    log.debug("output callback failed: %s", exc)

    reader = asyncio.ensure_future(pump())
    timed_out = False
    deadline = started + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=min(0.5, remaining))
                break
            except asyncio.TimeoutError:
                if should_stop and should_stop():
                    log.info("stop requested; killing %s", argv[0])
                    timed_out = True
                    break
        if timed_out:
            await _kill_group(proc)
    except asyncio.CancelledError:
        await asyncio.shield(_kill_group(proc))
        raise
    finally:
        try:
            await asyncio.wait_for(reader, timeout=5)
        except asyncio.TimeoutError:
            reader.cancel()
    output = b"".join(chunks).decode(errors="replace")
    return CommandResult(argv, proc.returncode, output, time.monotonic() - started, timed_out)


async def _kill_group(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    """SIGTERM the process group, then SIGKILL whatever is still alive after `grace` seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            break
        except asyncio.TimeoutError:
            continue
    if proc.returncode is None:
        await proc.wait()


class RepoSandbox:
    """File and command access confined to one repository directory."""

    def __init__(self, root: Path, test_command: str, *, timeout: float = 180.0) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise SandboxError(f"{root} is not a directory")
        self.test_argv = shlex.split(test_command)
        if not self.test_argv:
            raise SandboxError("the test command is empty")
        self.timeout = timeout

    # -- paths

    def resolve(self, path: str, *, must_exist: bool = False) -> Path:
        """Map a model-supplied path to an absolute path inside the repo, or raise SandboxError."""
        if not isinstance(path, str) or "\x00" in path:
            raise SandboxError("invalid path")
        raw = Path(path.strip() or ".")
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)  # follows symlinks that exist
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise SandboxError(f"{path!r} is outside the repository")
        rel = resolved.relative_to(self.root)
        if ".git" in rel.parts:
            raise SandboxError("the .git directory is off limits")
        if any(fnmatch.fnmatch(resolved.name, pattern) for pattern in SECRET_PATTERNS):
            raise SandboxError(f"{rel} may hold secrets and is off limits")
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"{rel} does not exist")
        return resolved

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    # -- file tools

    def list_files(self, path: str = ".", pattern: str = "*") -> list[str]:
        base = self.resolve(path, must_exist=True)
        if base.is_file():
            return [self.rel(base)]
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                full = Path(dirpath) / name
                if not fnmatch.fnmatch(name, pattern or "*"):
                    continue
                try:
                    self.resolve(str(full))
                except SandboxError:
                    continue
                out.append(self.rel(full))
                if len(out) >= MAX_LIST_ENTRIES:
                    return out + [f"... (truncated at {MAX_LIST_ENTRIES} entries)"]
        return out

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        target = self.resolve(path, must_exist=True)
        if not target.is_file():
            raise SandboxError(f"{path} is not a file")
        if target.stat().st_size > MAX_READ_BYTES and start_line is None:
            raise SandboxError(f"{path} is larger than {MAX_READ_BYTES} bytes; read a line range")
        lines = target.read_text(errors="replace").splitlines()
        lo = max(1, start_line or 1)
        hi = min(len(lines), end_line or len(lines))
        width = len(str(hi))
        return "\n".join(f"{n:>{width}}| {lines[n - 1]}" for n in range(lo, hi + 1))

    def search(self, query: str, path: str = ".", *, regex: bool = False) -> list[str]:
        try:
            pattern = re.compile(query if regex else re.escape(query))
        except re.error as exc:
            raise SandboxError(f"bad regex: {exc}") from exc
        hits: list[str] = []
        for rel in self.list_files(path):
            if rel.startswith("..."):
                break
            full = self.root / rel
            try:
                if full.stat().st_size > MAX_READ_BYTES:
                    continue
                text = full.read_text(errors="strict")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable
            for n, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        return hits + ["... (more hits omitted)"]
        return hits

    def write_file(self, path: str, content: str) -> str:
        if len(content.encode()) > MAX_WRITE_BYTES:
            raise SandboxError(f"refusing to write more than {MAX_WRITE_BYTES} bytes")
        target = self.resolve(path)
        if target == self.root or target.is_dir():
            raise SandboxError(f"{path} is a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        tmp = target.with_name(f".{target.name}.nimbus-tmp")
        tmp.write_text(content)
        tmp.replace(target)
        return f"{'updated' if existed else 'created'} {self.rel(target)} ({len(content.splitlines())} lines)"

    def edit_file(self, path: str, old: str, new: str) -> str:
        """Replace exactly one occurrence of `old`; ambiguity is an error, not a guess."""
        target = self.resolve(path, must_exist=True)
        text = target.read_text()
        count = text.count(old) if old else 0
        if count == 0:
            raise SandboxError(f"the text to replace was not found in {path}; read the file again")
        if count > 1:
            raise SandboxError(f"the text to replace occurs {count} times in {path}; include more context")
        return self.write_file(path, text.replace(old, new, 1))

    # -- commands

    def check_command(self, command: str | list[str]) -> list[str]:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if argv != self.test_argv:
            raise SandboxError(f"only the test command is allowed: {shlex.join(self.test_argv)}")
        return argv

    async def run(self, command: str | list[str], *,
                  should_stop: Callable[[], bool] | None = None) -> CommandResult:
        argv = self.check_command(command)
        log.info("sandbox: running %s in %s", shlex.join(argv), self.root)
        return await run_command(argv, cwd=self.root, timeout=self.timeout, should_stop=should_stop)

    async def run_tests(self, *, should_stop: Callable[[], bool] | None = None) -> CommandResult:
        return await self.run(self.test_argv, should_stop=should_stop)
