"""The pipeline's rules: reproduce before fixing, and never report success without proof."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nimbus import config
from nimbus.engine.steps import PlanStep, StepPlan
from nimbus.repro.fixer import FixResult
from nimbus.repro.pipeline import STAGES, ReproPipeline, decide, github_remote, open_draft_pull_request
from nimbus.repro.reproduce import ReproResult, StepOutcome

PASS = f'{sys.executable} -c "pass"'
FAIL = f'{sys.executable} -c "raise SystemExit(1)"'


def make_plan() -> StepPlan:
    return StepPlan(id="plan42", kind="bug", title="Cart total ignores quantity", model="muse-spark-1.2",
                    expected="$158.00", actual="$79.00",
                    steps=[PlanStep(index=0, action="click", target="Add to cart"),
                           PlanStep(index=1, action="click", target="Add to cart")])


def repro(mode: str, reproduced: str, *, completed: bool = True, expected_visible: bool | None = None,
          evidence: str = "Total shows $79.00", tmp: Path | None = None) -> ReproResult:
    shot = None
    if tmp is not None:
        tmp.mkdir(parents=True, exist_ok=True)
        shot = tmp / f"{mode}.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    steps = [StepOutcome(0, "click", "Add to cart", True, "locator"), StepOutcome(1, "click", "Add to cart", True, "vision")]
    return ReproResult(mode=mode, reproduced=reproduced, evidence=evidence, completed=completed,  # type: ignore[arg-type]
                       expected_visible=expected_visible, verifier_model="muse-spark-1.3", steps=steps,
                       final_screenshot=str(shot) if shot else None, verdict_status="done")


def fix_result(status: str = "fixed", diff: str = "diff --git a/cart.js b/cart.js\n+fixed\n",
               tests_passed: bool = True) -> FixResult:
    return FixResult(status=status, message=f"fix {status}", branch="nimbus/repro-plan42", base_branch="main",  # type: ignore[arg-type]
                     base_commit="0" * 40, commit="1" * 40, engine="spark-agent", diff=diff, tests_passed=tests_passed,
                     regression_test_added=True, regression_test_fails_before_fix=True, models=["muse-spark-1.3"])


# ---------------------------------------------------------------- decide()


def test_success_needs_every_proof() -> None:
    before = repro("bug", "yes")
    after = repro("fixed", "no", expected_visible=True, evidence="Total shows $158.00")
    assert decide(before, fix_result(), after, True)[0] == "fixed"
    assert decide(before, fix_result(), after, True, expected="The total is $158.00.")[1] == \
        "Fixed and verified. The total is $158.00."
    assert decide(before, fix_result(), after, True, expected="Qty 2 ( $79.00 ) and `Total $158.00` .")[1] == \
        "Fixed and verified. Qty 2 ($79.00) and Total $158.00."


@pytest.mark.parametrize("before,fix,after,tests,expected", [
    (None, None, None, None, "not_reproduced"),
    ("no", None, None, None, "not_reproduced"),
    ("uncertain", None, None, None, "not_reproduced"),
    ("yes", None, None, None, "fix_failed"),
    ("yes", "failed", None, None, "fix_failed"),
    ("yes", "empty-diff", None, True, "fix_failed"),
    ("yes", "tests-failed", None, True, "fix_failed"),
    ("yes", "fixed", "gone", False, "not_verified"),
    ("yes", "fixed", "gone", None, "not_verified"),
    ("yes", "fixed", None, True, "not_verified"),
    ("yes", "fixed", "incomplete", True, "not_verified"),
    ("yes", "fixed", "still-there", True, "not_verified"),
    ("yes", "fixed", "uncertain", True, "not_verified"),
    ("yes", "fixed", "no-evidence-flag", True, "not_verified"),
])
def test_anything_short_of_proof_is_not_success(before: str | None, fix: str | None, after: str | None,
                                                tests: bool | None, expected: str) -> None:
    b = repro("bug", before) if before else None
    f = {None: None, "failed": fix_result("failed"), "empty-diff": fix_result(diff="  \n"),
         "tests-failed": fix_result(tests_passed=False), "fixed": fix_result()}[fix]
    a = {None: None,
         "gone": repro("fixed", "no", expected_visible=True),
         "incomplete": repro("fixed", "uncertain", completed=False),
         "still-there": repro("fixed", "yes", expected_visible=False),
         "uncertain": repro("fixed", "uncertain", expected_visible=False),
         "no-evidence-flag": repro("fixed", "no", expected_visible=None)}[after]
    status, message = decide(b, f, a, tests)
    assert status == expected and message


# ---------------------------------------------------------------- the pipeline with fakes


class FakeAnalyzer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def analyze(self, video: Path, workdir: Path, *, kind: str = "tutorial", progress: Any = None) -> StepPlan:
        self.calls += 1
        assert kind == "bug"
        if progress:
            progress("Watching", 0.5)
        if self.error:
            raise self.error
        plan = make_plan()
        plan.save(workdir)
        return plan


class FakeReproducer:
    def __init__(self, before: str, after: str = "gone") -> None:
        self.before, self.after = before, after
        self.calls: list[str] = []

    async def reproduce(self, plan: StepPlan, out_dir: Path, *, plan_dir: Path | None = None, mode: str = "bug",
                        compare_to: ReproResult | None = None, should_stop: Any = None, on_step: Any = None) -> ReproResult:
        self.calls.append(mode)
        if on_step:
            on_step(StepOutcome(0, "click", "Add to cart", True, "locator"), 2)
        if mode == "bug":
            return repro("bug", self.before, tmp=out_dir)
        assert compare_to is not None and compare_to.mode == "bug"
        return {"gone": repro("fixed", "no", expected_visible=True, evidence="Total now $158.00", tmp=out_dir),
                "still-there": repro("fixed", "yes", expected_visible=False, tmp=out_dir),
                "uncertain": repro("fixed", "uncertain", expected_visible=False, tmp=out_dir)}[self.after]


class FakeFixer:
    def __init__(self, result: FixResult) -> None:
        self.result = result
        self.calls = 0

    async def fix(self, plan: StepPlan, before: ReproResult, run_dir: Path, *, should_stop: Any = None,
                  progress: Any = None) -> FixResult:
        self.calls += 1
        assert before.reproduced == "yes"  # it is never asked to fix an unconfirmed bug
        if progress:
            progress("fixing", 0.5)
        return self.result


class FakeServer:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def ensure(self) -> bool:
        self.events.append("ensure")
        return True

    async def restart(self) -> None:
        self.events.append("restart")

    async def stop(self) -> None:
        self.events.append("stop")


def make_pipeline(tmp_path: Path, *, before: str = "yes", after: str = "gone", fix: FixResult | None = None,
                  test_command: str = PASS, open_pr: bool = False, analyzer: FakeAnalyzer | None = None,
                  pr_opener: Any = None) -> tuple[ReproPipeline, dict[str, Any]]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    settings = config.ReproSettings(repo_path=str(repo), test_command=test_command, open_pull_request=open_pr,
                                    app_url="http://127.0.0.1:9")
    parts = {"analyzer": analyzer or FakeAnalyzer(), "reproducer": FakeReproducer(before, after),
             "fixer": FakeFixer(fix or fix_result()), "server": FakeServer()}
    pipeline = ReproPipeline(None, settings, analyzer=parts["analyzer"], reproducer=parts["reproducer"],
                             fixer=parts["fixer"], app_server=parts["server"], pr_opener=pr_opener,
                             data_root=tmp_path / "runs")
    return pipeline, parts


async def test_a_bug_that_does_not_reproduce_is_never_fixed(tmp_path: Path) -> None:
    pipeline, parts = make_pipeline(tmp_path, before="no")
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.status == "not_reproduced" and not result.success
    assert parts["fixer"].calls == 0
    assert parts["reproducer"].calls == ["bug"]
    assert Path(result.report_md).exists() and Path(result.report_html).exists()
    assert "Not reproduced" in Path(result.report_md).read_text()


async def test_an_uncertain_reproduction_is_not_enough(tmp_path: Path) -> None:
    pipeline, parts = make_pipeline(tmp_path, before="uncertain")
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.status == "not_reproduced" and parts["fixer"].calls == 0


async def test_a_failed_fix_stops_before_verification(tmp_path: Path) -> None:
    pipeline, parts = make_pipeline(tmp_path, fix=fix_result("failed", diff=""))
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.status == "fix_failed"
    assert parts["reproducer"].calls == ["bug"]
    assert "restart" not in parts["server"].events


async def test_a_claimed_fix_with_an_empty_diff_is_refused(tmp_path: Path) -> None:
    pipeline, parts = make_pipeline(tmp_path, fix=fix_result("fixed", diff=""))
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.status == "fix_failed" and result.branch == ""


async def test_failing_tests_after_the_fix_block_success(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path, test_command=FAIL)
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.status == "not_verified" and result.tests_passed is False
    assert "tests do not pass" in result.message


@pytest.mark.parametrize("after", ["still-there", "uncertain"])
async def test_the_bug_must_be_gone_on_rerun(tmp_path: Path, after: str) -> None:
    pipeline, parts = make_pipeline(tmp_path, after=after)
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.status == "not_verified"
    assert parts["reproducer"].calls == ["bug", "fixed"]


async def test_a_verified_fix_reports_success_with_progress(tmp_path: Path) -> None:
    events: list[tuple[str, str, float]] = []
    pr_calls: list[Any] = []

    async def opener(repo: Path, result: Any) -> str:
        pr_calls.append(result)
        return "https://example.invalid/pr/1"

    pipeline, parts = make_pipeline(tmp_path, pr_opener=opener)
    result = await pipeline.run(tmp_path / "bug.mov", progress=lambda s, m, f: events.append((s, m, f)))
    assert result.status == "fixed" and result.success
    assert result.tests_passed is True and result.branch == "nimbus/repro-plan42"
    assert parts["server"].events == ["ensure", "restart"]  # an injected server is never stopped by the pipeline
    assert pr_calls == []  # open_pull_request is off
    assert result.pr_url is None
    stages = [s for s, _, _ in events]
    assert stages[0] == "analyze" and stages[-1] == "report"
    assert [s for s in dict.fromkeys(stages)] == list(STAGES)
    fractions = [f for _, _, f in events]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    md = Path(result.report_md).read_text()
    assert "Fixed and verified" in md and "muse-spark-1.2" in md and "spark-agent" in md
    assert "```diff" in md and "before/bug.png" in md
    html = Path(result.report_html).read_text()
    assert "Before and after" in html and "<script" not in html
    assert (Path(result.run_dir) / "run.json").exists()
    roles = dict(result.models)
    assert roles["Video analysis (narrated recording to steps)"] == "muse-spark-1.2"
    assert any("Visual element grounding" in role for role, _ in result.models)


async def test_pull_request_only_when_enabled_and_verified(tmp_path: Path) -> None:
    calls: list[Any] = []

    async def opener(repo: Path, result: Any) -> str:
        calls.append(result.status)
        return "https://example.invalid/pr/2"

    pipeline, _ = make_pipeline(tmp_path, open_pr=True, pr_opener=opener)
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.pr_url == "https://example.invalid/pr/2" and calls == ["fixed"]

    pipeline, _ = make_pipeline(tmp_path, open_pr=True, pr_opener=opener, after="still-there")
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.pr_url is None and calls == ["fixed"]


async def test_no_github_remote_means_no_pull_request(shop_repo: Path, tmp_path: Path) -> None:
    assert await github_remote(shop_repo) is None
    run = (await make_pipeline(tmp_path)[0].run(tmp_path / "bug.mov"))
    assert await open_draft_pull_request(shop_repo, run) is None
    subprocess.run(["git", "remote", "add", "upstream", "git@gitlab.example:me/shop.git"], cwd=shop_repo, check=True)
    assert await github_remote(shop_repo) is None
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/someone/shop.git"], cwd=shop_repo, check=True)
    assert await github_remote(shop_repo) == "origin"


async def test_cancellation_between_stages(tmp_path: Path) -> None:
    pipeline, parts = make_pipeline(tmp_path)
    result = await pipeline.run(tmp_path / "bug.mov", should_stop=lambda: parts["analyzer"].calls > 0)
    assert result.status == "cancelled"
    assert parts["reproducer"].calls == [] and parts["fixer"].calls == 0
    assert Path(result.report_md).exists()


async def test_an_analysis_failure_becomes_an_error_result(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path, analyzer=FakeAnalyzer(RuntimeError("MODEL_API_KEY is not set")))
    result = await pipeline.run(tmp_path / "bug.mov")
    assert result.status == "error" and "MODEL_API_KEY" in result.message
    assert Path(result.report_html).exists()


async def test_a_broken_progress_callback_does_not_abort(tmp_path: Path) -> None:
    def explode(stage: str, message: str, fraction: float) -> None:
        raise RuntimeError("UI bug")

    pipeline, _ = make_pipeline(tmp_path)
    assert (await pipeline.run(tmp_path / "bug.mov", progress=explode)).status == "fixed"


async def test_a_saved_plan_skips_analysis(tmp_path: Path) -> None:
    pipeline, parts = make_pipeline(tmp_path)
    result = await pipeline.run(tmp_path / "bug.mov", plan=make_plan())
    assert parts["analyzer"].calls == 0 and result.status == "fixed"


# ---------------------------------------------------------------- offline end to end


async def test_offline_end_to_end_on_the_demo_shop(shop_repo: Path, tmp_path: Path) -> None:
    """Every real component (app server, Chromium executor, fixer with git and node tests, report)
    with only the models replaced: a scripted coding engine and a verifier that reads the page text."""
    import socket

    from nimbus.engine.verify import Verdict
    from nimbus.repro.fixer import EngineRun, Fixer
    from nimbus.repro.reproduce import Reproducer
    from tests.test_repro_executor import cart_bug_plan
    from tests.test_repro_fixer import real_fix

    class PageTextVerifier:
        async def check(self, *, expected: str, context: str, **_: Any) -> Verdict:
            text = " ".join(context.split())
            if "bug is visible" in expected:
                seen = "Qty 2" in text and "Total $79.00" in text
                return Verdict("done" if seen else "not_done", "Qty 2 with Total $79.00" if seen else "", "", "", 0.9,
                               model="page-text-verifier")
            fixed = "Total $158.00" in text
            return Verdict("done" if fixed else "not_done", "Total $158.00" if fixed else "", "", "", 0.9,
                           model="page-text-verifier")

    class Engine:
        name = "scripted"

        async def run(self, ctx: Any) -> EngineRun:
            real_fix(ctx.repo)
            return EngineRun("scripted", True, "done", "subtotal ignores quantity", "sum lineTotal", ["scripted"])

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = config.ReproSettings(repo_path=str(shop_repo), app_url=f"http://127.0.0.1:{port}",
                                    start_command=f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
                                    test_command="node --test", use_muse_code=False)
    pipeline = ReproPipeline(
        None, settings, analyzer=FakeAnalyzer(),
        reproducer=Reproducer(None, app_url=settings.app_url, grounder=None, verifier=PageTextVerifier(),
                              agent_runner=None),
        fixer=Fixer(None, settings, engines=[Engine()]), data_root=tmp_path / "runs")
    result = await pipeline.run(tmp_path / "bug.mov", plan=cart_bug_plan())
    assert result.status == "fixed", result.message
    assert result.before and result.before.reproduced == "yes"
    assert result.after and result.after.expected_visible and "Total $158.00" in " ".join(result.after.page_text.split())
    assert result.fix and result.fix.regression_test_fails_before_fix is True
    assert result.branch == "nimbus/repro-cartbug01"
    html = Path(result.report_html).read_text()
    assert "Fixed and verified" in html and "lineTotal(line)" in html
    for shot in (result.before.final_screenshot, result.after.final_screenshot):
        assert shot and Path(shot).exists()


async def test_outcome_sentence_is_validated_and_never_blocks_a_fix() -> None:
    from types import SimpleNamespace

    from nimbus.engine.steps import StepPlan
    from nimbus.repro.pipeline import ReproPipeline

    seen = {}

    async def respond_json(models, input, *, validate, **kwargs):
        seen["long_ok"] = validate({"sentence": "Fix cart total calculation to multiply price by quantity for "
                                                "subtotal and total so that it is right"})
        seen["formula_ok"] = validate({"sentence": "Total = Quantity x Unit Price now"})
        return {"sentence": "Two headphones now add up to $158.00 in the cart."}, None

    client = SimpleNamespace(settings=config.Settings(), respond_json=respond_json)
    plan = StepPlan(kind="bug", expected="total $158.00", actual="total $79.00")
    pipeline = ReproPipeline(client, config.Settings())
    assert await pipeline._headline(plan, None) == "Two headphones now add up to $158.00 in the cart."
    assert seen == {"long_ok": False, "formula_ok": False}

    async def broken(*args, **kwargs):
        raise RuntimeError("every model failed")

    client.respond_json = broken
    assert await pipeline._headline(plan, None) == ""


def test_the_report_plays_the_reproduction_recordings(tmp_path: Path) -> None:
    from nimbus.repro.report import _clip
    from nimbus.repro.reproduce import ReproResult

    run = ReproResult(mode="bug", reproduced="yes", evidence="")
    run.video, run.final_screenshot = str(tmp_path / "before" / "reproduction.webm"), str(tmp_path / "before" / "final.png")
    html = _clip(run, "Before the fix", tmp_path)
    assert '<video src="before/reproduction.webm"' in html and 'poster="before/final.png"' in html
    run.video = None  # no recording kept: the screenshot instead
    assert "<img src=\"before/final.png\"" in _clip(run, "Before the fix", tmp_path)
