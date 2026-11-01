"""Fix a reproduced bug on a new branch, and refuse to call it fixed without proof.

Engines, in order:

1. Muse Code, Meta's coding agent, headless: `muse exec` scoped to the repo, the key handed over on
   stdin (`--api-key-stdin`, from `MODEL_API_KEY`), Muse Code's own sandbox left on, approval prompts
   off (nobody is there to answer them; Nimbus's gates below judge the result instead). Nimbus only
   detects the CLI (PATH, then ~/.local/bin, or `MUSE_BIN`); it never installs it.
2. A built-in coding agent on Muse Spark: Responses API function tools (list, read, search, write,
   edit, run tests) that all go through `RepoSandbox`, so the model cannot leave the repo or run
   anything except the configured test command. Iterations are capped.

Both get the same rules: confirm the root cause first, add a regression test that fails before
the fix, make the minimal change, run the tests, touch nothing unrelated, never write secrets.

Then Nimbus checks the work itself instead of trusting the agent's summary. An attempt only
counts when the test command passes, the diff is non-empty, a test file changed, the new tests
fail on the original code (checked in a separate worktree), and the diff holds no secrets. A
failed attempt is saved for the report, the tree is reset and the next engine gets a clean start.
On success the change is committed on `nimbus/repro-<plan id>`; on failure the repo goes back to
the branch it was on and the attempt branch is deleted. Nothing is ever pushed or merged here.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shlex
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from nimbus import config
from nimbus.engine.steps import StepPlan
from nimbus.meta.client import MetaClient, image_part, text_part, user
from nimbus.meta.resilience import MetaAPIError, first_success
from nimbus.repro.reproduce import ReproResult
from nimbus.repro.sandbox import SECRET_ENV, SECRET_PATTERNS, CommandResult, RepoSandbox, SandboxError, \
    run_command, scrubbed_env, truncate

log = logging.getLogger(__name__)

FixStatus = Literal["fixed", "failed", "refused"]
FixProgress = Callable[[str, float], None]

TEST_FILE_PATTERNS = ("*.test.*", "*.spec.*", "*_test.*", "*_spec.*", "test_*", "test.*", "tests.*", "*Test.*",
                      "*Tests.*", "*Spec.*")
TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "e2e"})
SECRET_VALUE_PATTERNS = (
    re.compile(r"LLM\|[A-Za-z0-9_-]{6,}\|[A-Za-z0-9_-]{6,}"),           # Meta Model API key format
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][A-Za-z0-9_\-|]{16,}[\"']"),
)

RULES = """Rules, all mandatory, in this order:
1. Confirm the root cause before changing anything: read the relevant code and state in one or two
   sentences exactly why the actual behaviour happens.
2. First add a regression test, next to the existing tests and in the same framework and style, that
   reproduces this bug and FAILS on the current code. Run the test command and confirm it fails for the
   right reason.
3. Make the minimal code change that fixes the root cause. Do not refactor, reformat or rename anything else.
4. Run the test command again: every test must pass.
5. Never touch files unrelated to this bug. Never edit lockfiles, CI config or generated files.
6. Never write secrets, API keys, tokens or credentials into any file, and never print them.
7. Do not commit, push, or switch or create branches: Nimbus handles git."""


# ---------------------------------------------------------------- results


@dataclass
class EngineRun:
    engine: str  # muse-code | spark-agent
    ok: bool
    detail: str
    root_cause: str = ""
    summary: str = ""
    models: list[str] = field(default_factory=list)
    log_path: str | None = None
    diff_path: str | None = None
    duration_s: float = 0.0
    iterations: int = 0


@dataclass
class FixResult:
    status: FixStatus
    message: str
    repo: str = ""
    branch: str = ""
    base_branch: str = ""
    base_commit: str = ""
    commit: str | None = None
    engine: str | None = None
    root_cause: str = ""
    summary: str = ""
    diff: str = ""
    diff_stat: str = ""
    changed_files: list[str] = field(default_factory=list)
    tests_passed: bool = False
    test_output: str = ""
    baseline_tests_passed: bool | None = None
    regression_test_added: bool = False
    regression_test_fails_before_fix: bool | None = None
    muse_available: bool = False
    models: list[str] = field(default_factory=list)
    attempts: list[EngineRun] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "fixed" and self.tests_passed and bool(self.diff.strip())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FixContext:
    repo: Path
    plan: StepPlan
    repro: ReproResult | None
    prompt: str
    sandbox: RepoSandbox
    run_dir: Path
    test_command: str
    should_stop: Callable[[], bool] | None = None
    progress: FixProgress | None = None


class FixEngine(Protocol):
    name: str

    async def run(self, ctx: FixContext) -> EngineRun: ...


class Cancelled(RuntimeError):
    pass


# ---------------------------------------------------------------- prompt


def build_prompt(plan: StepPlan, repro: ReproResult | None, test_command: str, *, finish_hint: str) -> str:
    steps = "\n".join(f"  {s.index + 1}. {s.instruction or (s.action + ' ' + s.target + ' ' + s.text).strip()}"
                      for s in plan.steps)
    lines: list[str | None] = [
        "You are fixing a bug in the repository in the current directory. The bug was reported as a narrated "
        "screen recording and Nimbus has already reproduced it in a real browser.",
        "",
        f"Bug: {plan.title}",
        f"Summary: {plan.summary}" if plan.summary else None,
        f"Page: {plan.url}" if plan.url else None,
        f"Steps to reproduce:\n{steps}",
        f"Expected: {plan.expected}",
        f"Actual (the bug): {plan.actual}",
    ]
    if repro is not None:
        lines.append(f"Evidence from the browser reproduction: {repro.evidence}")
        if repro.console_errors:
            lines.append("Browser errors: " + "; ".join(repro.console_errors[:8]))
    lines += ["", f"Test command: {test_command}", "", RULES, "", finish_hint]
    return "\n".join(line for line in lines if line is not None).strip()


# ---------------------------------------------------------------- git helpers


class GitError(RuntimeError):
    pass


async def git(repo: Path, *args: str, check: bool = True, timeout: float = 60.0) -> CommandResult:
    result = await run_command(["git", *args], cwd=repo, timeout=timeout, env=scrubbed_env({"GIT_TERMINAL_PROMPT": "0"}))
    if check and not result.ok:
        raise GitError(f"git {' '.join(args)} failed: {result.output.strip()[-400:]}")
    return result


async def git_out(repo: Path, *args: str) -> str:
    return (await git(repo, *args)).output.strip()


def is_test_file(path: str) -> bool:
    p = Path(path)
    return any(fnmatch.fnmatchcase(p.name, pat) for pat in TEST_FILE_PATTERNS) or \
        any(part.lower() in TEST_DIRS for part in p.parts[:-1])


def find_secrets(diff: str) -> list[str]:
    """Secrets in the added lines of a diff: known key formats, and the live values of secret env vars."""
    added = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    hits = [f"pattern {p.pattern[:40]!r}" for p in SECRET_VALUE_PATTERNS if p.search(added)]
    for name, value in os.environ.items():
        if SECRET_ENV.search(name) and len(value) >= 8 and value in added:
            hits.append(f"the value of ${name}")
    return hits


# ---------------------------------------------------------------- engines


class MuseCodeEngine:
    """Muse Code's headless mode. `extra_args` are appended after Nimbus's own flags."""

    name = "muse-code"

    def __init__(self, binary: str, *, extra_args: tuple[str, ...] = (), timeout: float = 900.0,
                 model: str | None = None, max_steps: int = 80, effort: str = "medium") -> None:
        self.binary = binary
        self.extra_args = tuple(extra_args)
        self.timeout = timeout
        self.model = model
        self.max_steps = max_steps
        self.effort = effort

    def argv(self, repo: Path, prompt_file: Path) -> list[str]:
        argv = [self.binary, "exec",
                "--api-key-stdin",
                "--workspace", str(repo),
                "--worktree", "off",                 # Nimbus already made the branch
                "--approval-mode", "never",          # headless; Muse Code's sandbox stays on
                "--disable-web-tools",
                "--disable-request-user-input",
                "--no-foreign-personal-context",
                "--max-model-steps", str(self.max_steps),
                "--reasoning-effort", self.effort,
                "--prompt-file", str(prompt_file)]
        if self.model:
            argv += ["--model", self.model]
        return argv + list(self.extra_args)

    async def run(self, ctx: FixContext) -> EngineRun:
        started = time.monotonic()
        key = config.get_secret("MODEL_API_KEY")
        if not key:
            return EngineRun(self.name, False, "MODEL_API_KEY is not set, so Muse Code cannot authenticate")
        prompt_file = ctx.run_dir / "muse-code-prompt.md"
        prompt_file.write_text(ctx.prompt)
        argv = self.argv(ctx.repo, prompt_file)
        env = scrubbed_env()
        env.setdefault("CI", "1")
        if ctx.progress:
            ctx.progress("Muse Code is working on the fix", 0.2)
        log.info("running Muse Code in %s", ctx.repo)
        log_path = ctx.run_dir / "muse-code.log"
        with log_path.open("wb") as live:  # written as Muse Code works, so a stuck run still shows where it got
            live.write(("$ " + " ".join(a if a != str(prompt_file) else "<prompt>" for a in argv[1:]) + "\n\n").encode())
            live.flush()

            def tee(block: bytes) -> None:
                live.write(block)
                live.flush()

            result = await run_command(argv, cwd=ctx.repo, timeout=self.timeout, env=env, should_stop=ctx.should_stop,
                                       stdin_data=(key + "\n").encode(), on_output=tee)
        root_cause, summary = _parse_summary(result.output)
        if result.timed_out:
            detail = f"Muse Code was stopped after {result.duration_s:.0f}s"
        elif result.exit_code != 0:
            detail = f"Muse Code exited with code {result.exit_code}: {result.output.strip()[-300:]}"
        else:
            detail = "Muse Code finished"
        return EngineRun(self.name, result.ok, detail, root_cause, summary, ["muse-code"], str(log_path),
                         duration_s=time.monotonic() - started)


def find_muse() -> str | None:
    """`muse` on PATH, else Meta's default install location, else $MUSE_BIN. Apps launched from Finder do
    not inherit a shell PATH, so the default location matters."""
    for candidate in (os.environ.get("MUSE_BIN"), shutil.which("muse"), str(Path.home() / ".local" / "bin" / "muse")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _parse_summary(text: str) -> tuple[str, str]:
    root = re.findall(r"ROOT CAUSE:\s*(.+)", text)
    fix = re.findall(r"FIX:\s*(.+)", text)
    return (root[-1].strip() if root else ""), (fix[-1].strip() if fix else truncate(text.strip(), 600))


CODER_INSTRUCTIONS = """You are a careful senior engineer fixing one bug in a repository through the tools
you are given. You can only see and change files through these tools, and the only command you can
run is the project's test command (run_tests). Work in small verified steps. Read before you write.
Prefer edit_file for small changes; use write_file only for new files. When everything is done and
the tests pass, call finish. If you cannot fix the bug, call finish and explain why in fix_summary."""


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}


CODER_TOOLS = [
    _tool("list_files", "List files under a directory of the repository (recursive, skips .git and node_modules).",
          {"path": {"type": "string", "description": "directory relative to the repo root, '.' for the root"},
           "pattern": {"type": "string", "description": "glob on the file name, e.g. '*.js'; '*' for all"}},
          ["path", "pattern"]),
    _tool("read_file", "Read a text file with line numbers. Give a line range for big files.",
          {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
          ["path"]),
    _tool("search", "Search file contents for a string (or a regex when regex=true). Returns path:line: text.",
          {"query": {"type": "string"}, "path": {"type": "string"}, "regex": {"type": "boolean"}}, ["query"]),
    _tool("edit_file", "Replace exactly one occurrence of old_text with new_text in a file. old_text must be unique.",
          {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
          ["path", "old_text", "new_text"]),
    _tool("write_file", "Create a file or overwrite it completely with content.",
          {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _tool("run_tests", "Run the project's test command and return its exit code and output.", {}, []),
    _tool("finish", "Report the result once the regression test was added, the fix made, and the tests pass.",
          {"root_cause": {"type": "string", "description": "one or two sentences: why the bug happened"},
           "fix_summary": {"type": "string", "description": "one or two sentences: what changed"},
           "regression_test": {"type": "string", "description": "file and name of the regression test"},
           "tests_passed": {"type": "boolean"}},
          ["root_cause", "fix_summary", "regression_test", "tests_passed"]),
]


class SparkAgentEngine:
    """A function-calling coding loop on Muse Spark, confined to `RepoSandbox`."""

    name = "spark-agent"

    def __init__(self, client: MetaClient, *, models: list[str] | None = None, max_iterations: int = 40,
                 max_nudges: int = 2) -> None:
        self.client = client
        self.models = models or list(client.settings.models.coder)
        self.max_iterations = max_iterations
        self.max_nudges = max_nudges

    async def run(self, ctx: FixContext) -> EngineRun:
        started = time.monotonic()
        transcript: list[dict[str, Any]] = []
        log_path = ctx.run_dir / "spark-agent.jsonl"
        parts = [text_part(ctx.prompt)]
        if ctx.repro and ctx.repro.final_screenshot and Path(ctx.repro.final_screenshot).exists():
            parts.append(text_part("Screenshot of the page at the end of the reproduction, showing the bug:"))
            parts.append(image_part(Path(ctx.repro.final_screenshot).read_bytes(), detail_max=1400))

        async def start(model: str):
            return await self.client.respond(model, [user(*parts)], instructions=CODER_INSTRUCTIONS, tools=CODER_TOOLS,
                                             reasoning_effort="medium", max_output_tokens=16000)

        try:
            reply = await first_success(self.models, start, what="coding agent")
        except MetaAPIError as exc:
            return EngineRun(self.name, False, f"Muse Spark could not start: {exc}", duration_s=time.monotonic() - started)
        model = reply.model
        finish: dict[str, Any] | None = None
        nudges = 0
        iteration = 0
        try:
            for iteration in range(1, self.max_iterations + 1):
                if ctx.should_stop and ctx.should_stop():
                    raise Cancelled("stopped by the user")
                calls = [item for item in (getattr(reply.raw, "output", None) or [])
                         if getattr(item, "type", "") == "function_call"]
                if not calls:
                    text = (reply.text or "").strip()
                    transcript.append({"iteration": iteration, "message": text[:4000]})
                    if nudges >= self.max_nudges:
                        break
                    nudges += 1
                    outputs: list[dict[str, Any]] = [user(text_part(
                        "Continue with the tools. Follow the rules: regression test first, minimal fix, run_tests, "
                        "then call finish."))]
                else:
                    outputs = []
                    for call in calls:
                        name = getattr(call, "name", "")
                        try:
                            args = json.loads(getattr(call, "arguments", "") or "{}")
                        except ValueError:
                            args = {}
                        if name == "finish":
                            finish = args
                            output = "Recorded. Nimbus will now verify the change independently."
                        else:
                            output = await self._execute(ctx, name, args)
                        transcript.append({"iteration": iteration, "tool": name,
                                           "args": {k: (v if len(str(v)) < 400 else str(v)[:400] + "...")
                                                    for k, v in args.items()},
                                           "output": output[:2000]})
                        outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
                    if ctx.progress:
                        ctx.progress(f"Muse Spark coding agent: step {iteration}", min(0.9, 0.1 + iteration / self.max_iterations))
                    if finish is not None:
                        break
                reply = await self.client.respond(model, outputs, previous_response_id=reply.response_id,
                                                  instructions=CODER_INSTRUCTIONS, tools=CODER_TOOLS,
                                                  reasoning_effort="medium", max_output_tokens=16000)
        except MetaAPIError as exc:
            _write_jsonl(log_path, transcript)
            return EngineRun(self.name, False, f"the coding agent's model call failed: {exc}", models=[model],
                             log_path=str(log_path), duration_s=time.monotonic() - started, iterations=iteration)
        _write_jsonl(log_path, transcript)
        if finish is None:
            return EngineRun(self.name, False, f"the coding agent did not finish within {iteration} iterations",
                             models=[model], log_path=str(log_path), duration_s=time.monotonic() - started,
                             iterations=iteration)
        return EngineRun(self.name, True, "the coding agent called finish", str(finish.get("root_cause", "")),
                         str(finish.get("fix_summary", "")), [model], str(log_path),
                         duration_s=time.monotonic() - started, iterations=iteration)

    async def _execute(self, ctx: FixContext, name: str, args: dict[str, Any]) -> str:
        box = ctx.sandbox
        try:
            if name == "list_files":
                return "\n".join(box.list_files(str(args.get("path") or "."), str(args.get("pattern") or "*"))) or "(empty)"
            if name == "read_file":
                return box.read_file(str(args.get("path", "")), _int(args.get("start_line")), _int(args.get("end_line")))
            if name == "search":
                return "\n".join(box.search(str(args.get("query", "")), str(args.get("path") or "."),
                                            regex=bool(args.get("regex")))) or "(no matches)"
            if name == "edit_file":
                return box.edit_file(str(args.get("path", "")), str(args.get("old_text", "")), str(args.get("new_text", "")))
            if name == "write_file":
                return box.write_file(str(args.get("path", "")), str(args.get("content", "")))
            if name == "run_tests":
                return (await box.run_tests(should_stop=ctx.should_stop)).summary()
            return f"error: unknown tool {name!r}"
        except (SandboxError, FileNotFoundError, IsADirectoryError, UnicodeDecodeError) as exc:
            return f"error: {exc}"


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r, default=str) for r in rows) + ("\n" if rows else ""))


# ---------------------------------------------------------------- the fixer


class Fixer:
    def __init__(self, client: MetaClient | None, settings: config.ReproSettings, *,
                 engines: list[FixEngine] | None = None, muse_args: tuple[str, ...] = (), muse_timeout: float = 900.0,
                 test_timeout: float = 180.0, max_iterations: int = 40, require_regression_test: bool = True) -> None:
        self.client = client
        self.settings = settings
        self.test_timeout = test_timeout
        self.require_regression_test = require_regression_test
        self.muse_path = find_muse()
        self.unavailable: list[EngineRun] = []
        if engines is not None:
            self.engines = list(engines)
        else:
            self.engines = []
            if settings.use_muse_code:
                if self.muse_path:
                    coder = client.settings.models.coder[0] if client is not None else None
                    self.engines.append(MuseCodeEngine(self.muse_path, extra_args=muse_args, timeout=muse_timeout,
                                                       model=coder))
                else:
                    self.unavailable.append(EngineRun("muse-code", False,
                                                      "Muse Code CLI (`muse`) is not installed; using the Muse Spark agent"))
            if client is not None:
                self.engines.append(SparkAgentEngine(client, max_iterations=max_iterations))

    async def fix(self, plan: StepPlan, repro: ReproResult | None, run_dir: Path, *,
                  should_stop: Callable[[], bool] | None = None, progress: FixProgress | None = None) -> FixResult:
        report = progress or (lambda msg, frac: None)
        run_dir.mkdir(parents=True, exist_ok=True)
        repo = Path(self.settings.repo_path).expanduser().resolve()
        result = FixResult(status="refused", message="", repo=str(repo), muse_available=bool(self.muse_path),
                           attempts=list(self.unavailable))
        try:
            await self._preflight(repo, result)
        except (GitError, SandboxError, OSError) as exc:
            result.message = str(exc)
            return result
        if not self.engines:
            result.status, result.message = "refused", "no fixing engine is available (Muse Code missing and no client)"
            return result

        sandbox = RepoSandbox(repo, self.settings.test_command, timeout=self.test_timeout)
        result.branch = await self._create_branch(repo, plan)
        report(f"On branch {result.branch}; running the tests first", 0.05)
        baseline = await sandbox.run_tests(should_stop=should_stop)
        result.baseline_tests_passed = baseline.ok
        (run_dir / "tests-baseline.txt").write_text(baseline.summary(100_000))

        try:
            for n, engine in enumerate(self.engines, 1):
                if should_stop and should_stop():
                    raise Cancelled("stopped by the user")
                if n > 1:
                    await self._reset(repo, result.base_commit)
                prompt = build_prompt(plan, repro, self.settings.test_command, finish_hint=(
                    "When done, call the finish tool." if engine.name == "spark-agent" else
                    "When done, end your output with a line `ROOT CAUSE: <one sentence>` and a line `FIX: <one sentence>`."))
                ctx = FixContext(repo, plan, repro, prompt, sandbox, run_dir, self.settings.test_command, should_stop,
                                 lambda msg, frac: report(msg, 0.1 + 0.7 * frac))
                report(f"Fixing with {engine.name}", 0.1)
                run = await engine.run(ctx)
                result.attempts.append(run)
                result.models = sorted(set(result.models) | set(run.models))
                if should_stop and should_stop():
                    raise Cancelled("stopped by the user")
                report("Checking the change independently", 0.82)
                problem = await self._check(repo, sandbox, result, run, run_dir, n, should_stop)
                if problem is None:
                    result.engine, result.root_cause, result.summary = engine.name, run.root_cause, run.summary
                    try:
                        result.commit = await self._commit(repo, plan, result)
                    except GitError as exc:
                        problem = f"the commit failed (a pre-commit hook?): {exc}"
                if problem is None:
                    result.status = "fixed"
                    result.message = f"Fixed by {engine.name}; tests pass and the regression test fails on the old code."
                    report("Fix committed on " + result.branch, 1.0)
                    return result
                run.ok = False
                run.detail = f"{run.detail}; rejected: {problem}"
                log.warning("%s attempt rejected: %s", engine.name, problem)
        except Cancelled as exc:
            result.status, result.message = "failed", str(exc)
            await self._abandon(repo, result)
            return result
        result.status = "failed"
        result.message = "No engine produced a verified fix: " + " | ".join(
            f"{a.engine}: {a.detail}" for a in result.attempts)
        await self._abandon(repo, result)
        return result

    # -- steps

    async def _preflight(self, repo: Path, result: FixResult) -> None:
        if not repo.is_dir():
            raise GitError(f"{repo} does not exist")
        inside = await git(repo, "rev-parse", "--is-inside-work-tree", check=False)
        if inside.output.strip() != "true":
            raise GitError(f"{repo} is not a git repository")
        status = await git_out(repo, "status", "--porcelain", "--untracked-files=all")
        if status:
            raise GitError(f"{repo} has uncommitted changes; commit or stash them first so the fix diff is clean:\n"
                           f"{truncate(status, 600)}")
        result.base_commit = await git_out(repo, "rev-parse", "HEAD")
        result.base_branch = await git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")

    async def _create_branch(self, repo: Path, plan: StepPlan) -> str:
        base = f"nimbus/repro-{re.sub(r'[^A-Za-z0-9._-]', '-', plan.id)}"
        name, n = base, 1
        while (await git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False)).ok:
            n += 1
            name = f"{base}-{n}"
        await git(repo, "switch", "-c", name)
        return name

    async def _reset(self, repo: Path, commit: str) -> None:
        await git(repo, "reset", "--hard", commit)
        await git(repo, "clean", "-fd")

    async def _abandon(self, repo: Path, result: FixResult) -> None:
        """Put the repo back exactly as it was: base branch checked out, attempt branch deleted."""
        try:
            await self._reset(repo, result.base_commit)
            if result.base_branch and result.base_branch != "HEAD":
                await git(repo, "switch", result.base_branch)
            else:
                await git(repo, "switch", "--detach", result.base_commit)
            if result.branch:
                await git(repo, "branch", "-D", result.branch)
        except GitError as exc:
            log.error("could not restore %s after a failed fix: %s", repo, exc)

    async def _collect_diff(self, repo: Path, base_commit: str) -> tuple[str, str, list[str]]:
        await git(repo, "add", "-A")
        diff = (await git(repo, "diff", "--cached", "--no-color", base_commit)).output
        stat = (await git(repo, "diff", "--cached", "--stat", "--no-color", base_commit)).output.strip()
        files = [f for f in (await git(repo, "diff", "--cached", "--name-only", base_commit)).output.splitlines() if f]
        return diff, stat, files

    async def _check(self, repo: Path, sandbox: RepoSandbox, result: FixResult, run: EngineRun, run_dir: Path,
                     attempt: int, should_stop: Callable[[], bool] | None) -> str | None:
        """Nimbus's own acceptance check. Returns why the attempt is rejected, or None."""
        tests = await sandbox.run_tests(should_stop=should_stop)
        result.tests_passed = tests.ok
        result.test_output = tests.summary()
        (run_dir / f"tests-attempt{attempt}.txt").write_text(tests.summary(100_000))
        result.diff, result.diff_stat, result.changed_files = await self._collect_diff(repo, result.base_commit)
        diff_path = run_dir / f"attempt{attempt}-{run.engine}.diff"
        diff_path.write_text(result.diff)
        run.diff_path = str(diff_path)
        result.regression_test_added = any(is_test_file(f) for f in result.changed_files)
        result.regression_test_fails_before_fix = None

        if not run.ok and not result.diff.strip():
            return run.detail
        if not result.diff.strip():
            return "the diff is empty"
        if not tests.ok:
            return f"the test command fails after the change (exit {tests.exit_code})"
        forbidden = [f for f in result.changed_files
                     if any(fnmatch.fnmatch(Path(f).name, p) for p in SECRET_PATTERNS)]
        if forbidden:
            return f"the change touches files that may hold secrets: {', '.join(forbidden)}"
        secrets = find_secrets(result.diff)
        if secrets:
            return "the diff contains what looks like a secret (" + ", ".join(secrets) + ")"
        if self.require_regression_test:
            if not result.regression_test_added:
                return "no regression test was added"
            # Only meaningful when the old code passed its own tests; otherwise "fails on base" proves nothing.
            fails = await self._regression_fails_on_base(repo, result, run_dir) if result.baseline_tests_passed else None
            result.regression_test_fails_before_fix = fails
            if fails is False:
                return "the new tests also pass on the original code, so they do not cover the bug"
        return None

    async def _regression_fails_on_base(self, repo: Path, result: FixResult, run_dir: Path) -> bool | None:
        """Run the changed test files against the original code in a throwaway worktree."""
        tree = run_dir / "regression-check"
        if tree.exists():
            shutil.rmtree(tree, ignore_errors=True)
        try:
            await git(repo, "worktree", "add", "--detach", str(tree), result.base_commit)
        except GitError as exc:
            log.warning("could not create the regression worktree: %s", exc)
            return None
        try:
            for rel in result.changed_files:
                if not is_test_file(rel):
                    continue
                src = repo / rel
                dest = tree / rel
                if src.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
            check = await run_command(shlex.split(self.settings.test_command), cwd=tree, timeout=self.test_timeout)
            (run_dir / "tests-regression-on-base.txt").write_text(check.summary(100_000))
            return not check.ok
        finally:
            await git(repo, "worktree", "remove", "--force", str(tree), check=False)
            shutil.rmtree(tree, ignore_errors=True)
            await git(repo, "worktree", "prune", check=False)

    async def _commit(self, repo: Path, plan: StepPlan, result: FixResult) -> str:
        title = re.sub(r"\s+", " ", plan.title or "bug from a screen recording").strip()[:60]
        body = [f"fix: {title}", ""]
        if result.root_cause:
            body += [f"Root cause: {result.root_cause}", ""]
        if result.summary:
            body += [f"Fix: {result.summary}", ""]
        body.append(f"Reproduced from a screen recording and fixed by Nimbus Repro ({result.attempts[-1].engine}).")
        identity: list[str] = []
        if not (await git(repo, "config", "user.email", check=False)).output.strip():
            identity = ["-c", "user.name=Nimbus Repro", "-c", "user.email=nimbus-repro@localhost"]
        await git(repo, *identity, "commit", "-q", "-m", "\n".join(body), timeout=300.0)
        return await git_out(repo, "rev-parse", "HEAD")
