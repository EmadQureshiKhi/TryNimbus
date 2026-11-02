"""Repro: a narrated bug recording in, a reproduced, fixed and re-verified change out.

UI API
------
    from nimbus.repro.pipeline import ReproPipeline

    pipeline = ReproPipeline(client, settings)        # MetaClient; Settings or ReproSettings (default:
                                                      # client.settings.repro)
    result = await pipeline.run(
        video_path,                                   # .mov/.mp4 from recorder.ScreenRecorder or an import
        progress=cb,                                  # cb(stage: str, message: str, fraction: float)
        should_stop=fn,                               # fn() -> bool, polled between and inside stages
    )

`stage` is one of analyze, launch, reproduce, fix, verify, report. `fraction` is the overall
progress of the whole run in [0, 1] (it only moves forward). The callback is called on the event
loop's thread; a Qt UI should forward it with a queued signal. Exceptions it raises are logged
and ignored.

`run()` never raises for an expected failure; it returns a `RunResult`:
    result.status   fixed | not_reproduced | fix_failed | not_verified | cancelled | error
    result.success  True only for "fixed"
    result.message  one human sentence for the UI
    result.plan     the StepPlan (steps, expected, actual)
    result.before / result.after   ReproResult (verdict, evidence, screenshots, video)
    result.fix      FixResult (branch, diff, tests, engine, attempts)
    result.report_md / result.report_html / result.run_dir   files for the UI to open
    result.pr_url   set only when a draft PR was opened

Rules the pipeline enforces (see `decide`):
- It fixes nothing it has not reproduced: the verifier must confirm the bug with cited evidence.
- A fix counts only if Nimbus's own test run passes, the diff is non-empty, and the same
  reproduction run again shows the expected behaviour (the verifier says so, with evidence).
- It never merges. A draft PR is opened only when `ReproSettings.open_pull_request` is on and the
  repo has a GitHub remote; otherwise the fix stays on its local branch with the report.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from nimbus import config
from nimbus.engine.steps import StepPlan
from nimbus.engine.video import VideoAnalyzer
from nimbus.meta.client import MetaClient, text_part, user
from nimbus.repro.appserver import AppLaunchError, AppServer
from nimbus.repro.fixer import FixResult, Fixer, git
from nimbus.repro.report import pr_body, write_report
from nimbus.repro.reproduce import ReproResult, Reproducer, StepOutcome
from nimbus.repro.sandbox import CommandResult, RepoSandbox, run_command, scrubbed_env

log = logging.getLogger(__name__)

Stage = Literal["analyze", "launch", "reproduce", "fix", "verify", "report"]
Outcome = Literal["fixed", "not_reproduced", "fix_failed", "not_verified", "cancelled", "error"]
ProgressCallback = Callable[[str, str, float], None]
PullRequestOpener = Callable[[Path, "RunResult"], Awaitable[str | None]]

STAGES: dict[str, tuple[float, float]] = {
    "analyze": (0.00, 0.25),
    "launch": (0.25, 0.30),
    "reproduce": (0.30, 0.45),
    "fix": (0.45, 0.78),
    "verify": (0.78, 0.94),
    "report": (0.94, 1.00),
}


class PipelineCancelled(RuntimeError):
    pass


@dataclass
class RunResult:
    id: str
    status: Outcome
    message: str
    run_dir: str
    video: str = ""
    plan: StepPlan | None = None
    before: ReproResult | None = None
    fix: FixResult | None = None
    after: ReproResult | None = None
    tests_passed: bool | None = None
    test_output: str = ""
    branch: str = ""
    pr_url: str | None = None
    report_md: str = ""
    report_html: str = ""
    models: list[tuple[str, str]] = field(default_factory=list)  # (role, model id)
    timings: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    error: str = ""

    @property
    def success(self) -> bool:
        return self.status == "fixed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "status": self.status, "message": self.message, "run_dir": self.run_dir,
            "video": self.video, "plan": self.plan.model_dump() if self.plan else None,
            "before": self.before.to_dict() if self.before else None,
            "fix": self.fix.to_dict() if self.fix else None,
            "after": self.after.to_dict() if self.after else None,
            "tests_passed": self.tests_passed, "test_output": self.test_output, "branch": self.branch,
            "pr_url": self.pr_url, "report_md": self.report_md, "report_html": self.report_html,
            "models": self.models, "timings": self.timings, "started_at": self.started_at, "error": self.error,
        }


def decide(before: ReproResult | None, fix: FixResult | None, after: ReproResult | None,
           tests_passed: bool | None, expected: str = "") -> tuple[Outcome, str]:
    """The success rules, in one pure function so they are tested on their own.

    Anything short of positive proof at every stage is not a success: an uncertain verdict, a
    skipped step, an empty diff or a test run that did not happen all count as failures.
    """
    if before is None or before.reproduced != "yes":
        why = before.evidence if before else "the reproduction did not run"
        state = "could not be confirmed" if before and before.reproduced == "uncertain" else "did not reproduce"
        return "not_reproduced", f"The bug {state}, so nothing was changed. {why}".strip()
    if fix is None or not fix.ok:
        return "fix_failed", f"No verified fix: {fix.message if fix else 'the fixer did not run'}"
    if not fix.diff.strip():
        return "fix_failed", "The fix produced no change."
    if tests_passed is not True:
        return "not_verified", "The tests do not pass after the fix."
    if after is None or not after.completed:
        why = after.problem if after else "the reproduction was not re-run"
        return "not_verified", f"The reproduction could not be re-run on the fixed code: {why}"
    if after.expected_visible is not True or after.reproduced != "no":
        return "not_verified", f"The bug is not confirmed gone after the fix: {after.evidence or after.problem}"
    # One sentence a person reads or hears; the verifier's evidence is in the report.
    expected = re.sub(r"\(\s*([^()]*?)\s*\)", r"(\1)", re.sub(r"[`*]", "", expected))  # model formatting
    expected = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s{2,}", " ", expected)).strip().rstrip(".")
    return "fixed", f"Fixed and verified. {expected}." if expected else f"Fixed and verified: {after.evidence}"


HEADLINE_PROMPT = """A bug was reproduced, fixed and verified. Write the one sentence a person reads and hears
when it is done: what works now, in at most 14 plain words, with the key value. No quotes, code,
symbols, formulas or field labels. Example: "Two headphones now add up to $158.00 in the cart."
The bug: {actual}
What should happen: {expected}
What the screen shows after the fix: {after}"""
HEADLINE_SCHEMA = {"type": "object", "properties": {"sentence": {"type": "string"}}, "required": ["sentence"]}


class ReproPipeline:
    """Analyze -> launch -> reproduce -> fix -> verify -> report. All collaborators are injectable."""

    def __init__(self, client: MetaClient | None, settings: config.Settings | config.ReproSettings | None = None, *,
                 analyzer: Any | None = None, reproducer: Any | None = None, fixer: Any | None = None,
                 app_server: AppServer | None = None, pr_opener: PullRequestOpener | None = None,
                 data_root: Path | None = None) -> None:
        if isinstance(settings, config.Settings):
            self.settings = settings.repro
        elif isinstance(settings, config.ReproSettings):
            self.settings = settings
        elif client is not None:
            self.settings = client.settings.repro
        else:
            self.settings = config.ReproSettings()
        self.client = client
        self.repo = Path(self.settings.repo_path).expanduser().resolve()
        self.analyzer = analyzer if analyzer is not None else (VideoAnalyzer(client) if client else None)
        self.reproducer = reproducer if reproducer is not None else Reproducer(client, app_url=self.settings.app_url)
        self.fixer = fixer if fixer is not None else Fixer(client, self.settings)
        self._app_server = app_server
        self.pr_opener = pr_opener or open_draft_pull_request
        self.data_root = data_root

    async def run(self, video_path: Path | str, *, progress: ProgressCallback | None = None,
                  should_stop: Callable[[], bool] | None = None, plan: StepPlan | None = None) -> RunResult:
        """Run the whole flow. Pass `plan` to skip analysis (re-running a saved plan)."""
        run_id = uuid.uuid4().hex[:10]
        run_dir = (self.data_root / run_id) if self.data_root else config.data_dir("repro", run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        result = RunResult(run_id, "error", "", str(run_dir), video=str(video_path))
        events = _Progress(progress)
        stop = should_stop or (lambda: False)
        server = self._app_server or AppServer(self.settings.app_url, self.settings.start_command, self.repo,
                                               log_path=run_dir / "app.log")

        def checkpoint() -> None:
            if stop():
                raise PipelineCancelled("Stopped by the user.")

        def timed(stage: str, started: float) -> None:
            result.timings[stage] = round(time.monotonic() - started, 2)

        try:
            # -- analyze
            t = time.monotonic()
            if plan is None:
                if self.analyzer is None:
                    raise RuntimeError("MODEL_API_KEY is needed to analyze a recording")
                events("analyze", "Watching the recording with Muse Spark", 0.0)
                plan = await self.analyzer.analyze(Path(video_path), run_dir / "plan", kind="bug",
                                                   progress=lambda msg, frac: events("analyze", msg, frac))
            else:
                plan.save(run_dir / "plan")
            result.plan = plan
            events("analyze", f"{len(plan.steps)} steps: {plan.title}", 1.0)
            timed("analyze", t)
            checkpoint()

            # -- launch
            t = time.monotonic()
            events("launch", f"Checking {self.settings.app_url}", 0.0)
            started = await server.ensure()
            events("launch", "Started the app" if started else "The app is already running", 1.0)
            timed("launch", t)
            checkpoint()

            # -- reproduce
            t = time.monotonic()
            events("reproduce", "Reproducing the bug in Chromium", 0.0)
            result.before = await self.reproducer.reproduce(
                plan, run_dir / "before", plan_dir=run_dir / "plan", mode="bug", should_stop=stop,
                on_step=lambda o, n: events("reproduce", _step_message(o, n), (o.index + 1) / max(1, n) * 0.8))
            timed("reproduce", t)
            checkpoint()
            if result.before.reproduced != "yes":
                result.status, result.message = decide(result.before, None, None, None)
                events("reproduce", result.message, 1.0)
                return await self._finish(result, run_dir, events)
            events("reproduce", f"Reproduced: {result.before.evidence}", 1.0)

            # -- fix
            t = time.monotonic()
            events("fix", "Fixing the code", 0.0)
            result.fix = await self.fixer.fix(plan, result.before, run_dir / "fix", should_stop=stop,
                                              progress=lambda msg, frac: events("fix", msg, frac))
            result.branch = result.fix.branch if result.fix.ok else ""
            timed("fix", t)
            checkpoint()
            if not result.fix.ok:
                result.status, result.message = decide(result.before, result.fix, None, None)
                events("fix", result.message, 1.0)
                return await self._finish(result, run_dir, events)
            events("fix", f"Committed on {result.fix.branch}", 1.0)

            # -- verify
            t = time.monotonic()
            events("verify", "Reloading the app with the fix", 0.0)
            await server.restart()
            events("verify", "Running the tests", 0.1)
            tests = await self._run_tests(stop)
            result.tests_passed, result.test_output = tests.ok, tests.summary()
            (run_dir / "tests-after.txt").write_text(tests.summary(100_000))
            checkpoint()
            events("verify", "Re-running the reproduction on the fixed code", 0.3)
            result.after = await self.reproducer.reproduce(
                plan, run_dir / "after", plan_dir=run_dir / "plan", mode="fixed", compare_to=result.before,
                should_stop=stop,
                on_step=lambda o, n: events("verify", _step_message(o, n), 0.3 + 0.6 * (o.index + 1) / max(1, n)))
            timed("verify", t)
            result.status, result.message = decide(result.before, result.fix, result.after, result.tests_passed)
            if result.status == "fixed":
                headline = await self._headline(plan, result.after) or "The report shows the before and after"
                result.status, result.message = decide(result.before, result.fix, result.after, result.tests_passed,
                                                       expected=headline)
            events("verify", result.message, 1.0)

            if result.success and self.settings.open_pull_request:
                result.pr_url = await self._maybe_open_pr(result, run_dir)
            return await self._finish(result, run_dir, events)
        except PipelineCancelled as exc:
            result.status, result.message = "cancelled", str(exc)
            return await self._finish(result, run_dir, events)
        except AppLaunchError as exc:
            result.status, result.message, result.error = "error", f"The app could not be started: {exc}", str(exc)
            return await self._finish(result, run_dir, events)
        except Exception as exc:  # the UI needs a result and a report, not a traceback
            log.exception("repro run %s failed", run_id)
            result.status, result.message, result.error = "error", f"Repro failed: {exc}", repr(exc)
            return await self._finish(result, run_dir, events)
        finally:
            if self._app_server is None:
                await server.stop()  # only stops a server this run started

    async def _run_tests(self, should_stop: Callable[[], bool]) -> CommandResult:
        return await RepoSandbox(self.repo, self.settings.test_command).run_tests(should_stop=should_stop)

    async def _maybe_open_pr(self, result: RunResult, run_dir: Path) -> str | None:
        try:
            return await self.pr_opener(self.repo, result)
        except Exception as exc:  # a PR is a convenience; the verified local branch is the deliverable
            log.warning("could not open a draft pull request: %s", exc)
            result.error = f"Draft PR not opened: {exc}"
            return None

    async def _headline(self, plan: StepPlan, after: ReproResult | None) -> str:
        """The one sentence a person reads and hears when the fix is verified (the evidence stays in the
        report). The plan's own wording is detailed for the verifier and too long to say out loud."""
        if self.client is None:
            return ""
        prompt = HEADLINE_PROMPT.format(actual=plan.actual, expected=plan.expected,
                                        after=after.evidence if after else "")

        def plain(data) -> bool:
            sentence = str(data.get("sentence", "")) if isinstance(data, dict) else ""
            return 3 <= len(sentence.split()) <= 16 and not re.search(r"[`*=<>{}\[\]|\\]", sentence)

        try:
            data, _ = await self.client.respond_json(
                self.client.settings.models.verify, [user(text_part(prompt))], schema=HEADLINE_SCHEMA,
                schema_name="outcome_sentence", what="outcome sentence", reasoning_effort="low",
                max_output_tokens=2000, validate=plain)  # reasoning counts toward the cap
        except Exception as exc:  # a nicer sentence is not worth failing a verified fix over
            log.info("no outcome sentence (%s)", exc)
            return ""
        return str(data["sentence"]).strip()

    async def _finish(self, result: RunResult, run_dir: Path, events: "_Progress") -> RunResult:
        events("report", "Writing the report", 0.0)
        result.models = self._models_used(result)
        try:
            md, html = await asyncio.to_thread(write_report, result, run_dir, self.settings)
            result.report_md, result.report_html = str(md), str(html)
        except Exception as exc:  # a broken report must not hide the outcome
            log.exception("report failed")
            result.error = (result.error + "; " if result.error else "") + f"report failed: {exc}"
        (run_dir / "run.json").write_text(json.dumps(result.to_dict(), indent=2, default=str))
        written = "Report written" if result.report_html else "The report could not be written"
        events("report", written + (" and a draft PR opened" if result.pr_url else ""), 1.0)
        return result

    def _models_used(self, result: RunResult) -> list[tuple[str, str]]:
        models: list[tuple[str, str]] = []
        cfg = self.client.settings.models if self.client else config.Models()
        if result.plan and result.plan.model:
            models.append(("Video analysis (narrated recording to steps)", result.plan.model))
            if any(s.evidence.cursor_source == "sam" for s in result.plan.steps):
                models.append(("Cursor tracking and control segmentation", cfg.sam))
            if any(s.evidence.cursor_source == "grounding" for s in result.plan.steps):
                models.append(("Click grounding in the recording", " / ".join(cfg.grounding)))
        for label, repro in (("before", result.before), ("after", result.after)):
            if repro is None:
                continue
            if "vision" in repro.methods:
                models.append((f"Visual element grounding ({label})", " / ".join(cfg.grounding)))
            if "agent" in repro.methods:
                models.append((f"Computer use fallback ({label})", " / ".join(cfg.computer_use)))
            if repro.verifier_model:
                models.append((f"Verifier ({label} the fix)", repro.verifier_model))
        if result.fix:
            for run in result.fix.attempts:
                for model in run.models:
                    models.append((f"Coding agent ({run.engine})", model))
        seen: set[tuple[str, str]] = set()
        return [m for m in models if not (m in seen or seen.add(m))]


class _Progress:
    """Maps stage-local fractions onto one forward-only overall fraction and shields the caller."""

    def __init__(self, callback: ProgressCallback | None) -> None:
        self.callback = callback
        self.last = 0.0

    def __call__(self, stage: str, message: str, fraction: float) -> None:
        lo, hi = STAGES.get(stage, (self.last, self.last))
        overall = max(self.last, lo + (hi - lo) * max(0.0, min(1.0, fraction)))
        self.last = overall
        log.info("[%s %3.0f%%] %s", stage, overall * 100, message)
        if self.callback is None:
            return
        try:
            self.callback(stage, message, round(overall, 4))
        except Exception:  # a UI bug must not abort a run
            log.exception("progress callback failed")


def _step_message(outcome: StepOutcome, total: int) -> str:
    state = "ok" if outcome.ok else "failed"
    what = f"{outcome.action} {outcome.target}".strip()
    return f"Step {outcome.index + 1}/{total} {state} via {outcome.method}: {what}"


# ---------------------------------------------------------------- pull requests


async def github_remote(repo: Path) -> str | None:
    """The name of a remote that points at GitHub, preferring `origin`."""
    out = (await git(repo, "remote", "-v", check=False)).output
    remotes = [line.split()[0] for line in out.splitlines() if "github.com" in line and line.split()]
    if not remotes:
        return None
    return "origin" if "origin" in remotes else remotes[0]


async def open_draft_pull_request(repo: Path, result: RunResult) -> str | None:
    """Push the fix branch and open a draft PR with `gh`. Never merges. None if there is no GitHub remote."""
    remote = await github_remote(repo)
    if remote is None:
        log.info("no GitHub remote in %s; leaving the fix on its local branch", repo)
        return None
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("the GitHub CLI (gh) is not installed")
    if result.fix is None or not result.fix.branch:
        return None
    await git(repo, "push", "-u", remote, result.fix.branch, timeout=120.0)
    body = Path(result.run_dir) / "pr-body.md"
    body.write_text(pr_body(result))
    title = f"fix: {(result.plan.title if result.plan else 'bug from a screen recording')}"[:120]
    argv = [gh, "pr", "create", "--draft", "--head", result.fix.branch, "--title", title, "--body-file", str(body)]
    if result.fix.base_branch and result.fix.base_branch != "HEAD":
        argv += ["--base", result.fix.base_branch]
    env = scrubbed_env()
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):  # gh's own auth, if the user configured it that way
        if os.environ.get(name):
            env[name] = os.environ[name]
    created = await run_command(argv, cwd=repo, timeout=120.0, env=env)
    if not created.ok:
        raise RuntimeError(f"gh pr create failed: {created.output.strip()[-400:]}")
    url = created.output.strip().splitlines()[-1].strip()
    log.info("opened draft PR %s", url)
    return url
