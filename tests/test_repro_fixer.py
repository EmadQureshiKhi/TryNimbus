"""The fixer's own acceptance rules, on a real git copy of the demo shop with scripted engines.

The engines here are fakes that edit files the way a coding agent would; everything after that
(test run, diff, regression check on the old code, secret scan, commit, rollback) is the real code.
"""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nimbus import config
from nimbus.engine.steps import PlanStep, StepPlan
from nimbus.meta.client import Reply
from nimbus.repro import fixer as fixer_mod
from nimbus.repro.fixer import EngineRun, FixContext, Fixer, SparkAgentEngine, find_secrets, is_test_file


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

BUGGY = "return cart.lines.reduce((sum, line) => sum + line.priceCents, 0);"
FIXED = "return cart.lines.reduce((sum, line) => sum + lineTotal(line), 0);"
REGRESSION_TEST = """
test("adding the same product twice charges for both", () => {
  const cart = addItem(addItem(createCart(), headphones), headphones);
  assert.equal(subtotal(cart), 15800);
  assert.equal(total(cart), 15800);
});
"""
USELESS_TEST = """
test("a mug costs money", () => {
  assert.ok(mug.priceCents > 0);
});
"""


def plan() -> StepPlan:
    return StepPlan(id="abc123", kind="bug", title="Cart total ignores quantity",
                    expected="$158.00 for two headphones", actual="$79.00",
                    steps=[PlanStep(index=0, action="click", target="Add to cart", instruction="Add headphones twice")])


def settings(repo: Path, **kw: Any) -> config.ReproSettings:
    return config.ReproSettings(repo_path=str(repo), test_command="node --test", use_muse_code=False, **kw)


class ScriptedEngine:
    def __init__(self, name: str, edit: Callable[[Path], None], ok: bool = True) -> None:
        self.name, self.edit, self.ok = name, edit, ok
        self.prompts: list[str] = []

    async def run(self, ctx: FixContext) -> EngineRun:
        self.prompts.append(ctx.prompt)
        self.edit(ctx.repo)
        return EngineRun(self.name, self.ok, "scripted", root_cause="subtotal ignores quantity",
                         summary="use lineTotal", models=["fake-model"])


def real_fix(repo: Path) -> None:
    cart = repo / "cart.js"
    cart.write_text(cart.read_text().replace(BUGGY, FIXED))
    tests = repo / "cart.test.js"
    tests.write_text(tests.read_text() + REGRESSION_TEST)


def nothing(repo: Path) -> None:
    pass


async def run_fixer(repo: Path, tmp_path: Path, *engines: Any, **kw: Any):
    fixer = Fixer(None, settings(repo), engines=list(engines), **kw)
    return await fixer.fix(plan(), None, tmp_path / "fix")


async def test_a_real_fix_is_verified_and_committed(shop_repo: Path, tmp_path: Path) -> None:
    engine = ScriptedEngine("scripted", real_fix)
    result = await run_fixer(shop_repo, tmp_path, engine)
    assert result.status == "fixed", result.message
    assert result.ok and result.tests_passed
    assert result.branch == "nimbus/repro-abc123"
    assert git(shop_repo, "rev-parse", "--abbrev-ref", "HEAD") == "nimbus/repro-abc123"
    assert result.commit == git(shop_repo, "rev-parse", "HEAD")
    assert git(shop_repo, "status", "--porcelain") == ""
    assert sorted(result.changed_files) == ["cart.js", "cart.test.js"]
    assert result.regression_test_added and result.regression_test_fails_before_fix is True
    assert result.baseline_tests_passed is True
    assert "+  return cart.lines.reduce((sum, line) => sum + lineTotal(line), 0);" in result.diff
    message = git(shop_repo, "log", "-1", "--format=%B")
    assert message.startswith("fix: Cart total ignores quantity") and "Co-Authored-By" not in message
    # The prompt carries every rule the user story requires.
    prompt = engine.prompts[0]
    for rule in ("root cause", "regression test", "FAILS on the current code", "minimal", "Never touch files",
                 "Never write secrets", "node --test"):
        assert rule in prompt


async def test_empty_diff_is_refused_and_the_repo_restored(shop_repo: Path, tmp_path: Path) -> None:
    result = await run_fixer(shop_repo, tmp_path, ScriptedEngine("lazy", nothing))
    assert result.status == "failed" and not result.ok
    assert "the diff is empty" in result.message
    assert git(shop_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "nimbus/repro-abc123" not in git(shop_repo, "branch", "--list")


async def test_failing_tests_are_refused(shop_repo: Path, tmp_path: Path) -> None:
    def break_it(repo: Path) -> None:
        real_fix(repo)
        (repo / "cart.js").write_text((repo / "cart.js").read_text().replace("itemCount: 0", "itemCount: 1"))

    result = await run_fixer(shop_repo, tmp_path, ScriptedEngine("breaker", break_it))
    assert result.status == "failed" and not result.tests_passed
    assert "test command fails" in result.message
    assert git(shop_repo, "status", "--porcelain") == ""
    assert (shop_repo / "cart.js").read_text().count(BUGGY) == 1  # rolled back


async def test_a_fix_without_a_regression_test_is_refused(shop_repo: Path, tmp_path: Path) -> None:
    def code_only(repo: Path) -> None:
        (repo / "cart.js").write_text((repo / "cart.js").read_text().replace(BUGGY, FIXED))

    result = await run_fixer(shop_repo, tmp_path, ScriptedEngine("no-test", code_only))
    assert result.status == "failed" and "no regression test" in result.message


async def test_a_regression_test_that_passes_on_the_old_code_is_refused(shop_repo: Path, tmp_path: Path) -> None:
    def weak(repo: Path) -> None:
        (repo / "cart.js").write_text((repo / "cart.js").read_text().replace(BUGGY, FIXED))
        (repo / "cart.test.js").write_text((repo / "cart.test.js").read_text() + USELESS_TEST)

    result = await run_fixer(shop_repo, tmp_path, ScriptedEngine("weak", weak))
    assert result.status == "failed"
    assert "also pass on the original code" in result.message
    assert result.regression_test_fails_before_fix is False


async def test_secrets_in_the_diff_are_refused(shop_repo: Path, tmp_path: Path) -> None:
    def leaky(repo: Path) -> None:
        real_fix(repo)
        (repo / "app.js").write_text((repo / "app.js").read_text() + '\nconst KEY = "LLM|1234567890|abcdefghijkl";\n')

    result = await run_fixer(shop_repo, tmp_path, ScriptedEngine("leaky", leaky))
    assert result.status == "failed" and "secret" in result.message


async def test_a_dirty_repo_is_refused_untouched(shop_repo: Path, tmp_path: Path) -> None:
    (shop_repo / "notes.txt").write_text("work in progress\n")
    engine = ScriptedEngine("scripted", real_fix)
    result = await run_fixer(shop_repo, tmp_path, engine)
    assert result.status == "refused" and "uncommitted changes" in result.message
    assert engine.prompts == []
    assert git(shop_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


async def test_the_next_engine_gets_a_clean_tree(shop_repo: Path, tmp_path: Path) -> None:
    def half_done(repo: Path) -> None:
        (repo / "scratch.js").write_text("// abandoned attempt\n")
        (repo / "cart.js").write_text((repo / "cart.js").read_text() + "\n// broken\n" + "syntax error here(\n")

    seen: list[bool] = []

    def second(repo: Path) -> None:
        seen.append((repo / "scratch.js").exists())
        real_fix(repo)

    result = await run_fixer(shop_repo, tmp_path, ScriptedEngine("first", half_done, ok=False),
                             ScriptedEngine("second", second))
    assert result.status == "fixed" and result.engine == "second"
    assert seen == [False]
    assert [a.engine for a in result.attempts] == ["first", "second"]
    assert not result.attempts[0].ok and result.attempts[1].ok
    assert Path(result.attempts[0].diff_path or "").read_text()  # the failed attempt is kept for the report
    assert "scratch.js" not in result.changed_files


async def test_a_branch_name_collision_gets_a_suffix(shop_repo: Path, tmp_path: Path) -> None:
    git(shop_repo, "branch", "nimbus/repro-abc123")
    result = await run_fixer(shop_repo, tmp_path, ScriptedEngine("scripted", real_fix))
    assert result.branch == "nimbus/repro-abc123-2"


def test_muse_is_detected_not_installed(shop_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_which = shutil.which
    monkeypatch.setattr(fixer_mod.shutil, "which", lambda name, *a, **k: None if name == "muse" else real_which(name))
    monkeypatch.setattr(fixer_mod.Path, "home", lambda: shop_repo / "no-home")  # no ~/.local/bin/muse either
    monkeypatch.delenv("MUSE_BIN", raising=False)
    fixer = Fixer(None, config.ReproSettings(repo_path=str(shop_repo), use_muse_code=True))
    assert fixer.muse_path is None and fixer.engines == []
    assert "not installed" in fixer.unavailable[0].detail


async def test_muse_code_cli_is_driven_headless(shop_repo: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """A stand-in `muse` binary checks the contract: headless `muse exec` scoped to the repo, the prompt in a
    file, the key on stdin (never in argv or the environment), Muse Code's sandbox left on."""
    record = tmp_path / "muse-call.json"
    fake = tmp_path / "bin" / "muse"
    fake.parent.mkdir()
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
prompt = Path(argv[argv.index("--prompt-file") + 1]).read_text()
json.dump({{"argv": argv, "cwd": os.getcwd(), "stdin_key": sys.stdin.readline().strip(), "prompt": prompt,
           "env_key": os.environ.get("META_API_KEY") or os.environ.get("MODEL_API_KEY")}},
          open({str(record)!r}, "w"))
cart = Path("cart.js")
cart.write_text(cart.read_text().replace({BUGGY!r}, {FIXED!r}))
tests = Path("cart.test.js")
tests.write_text(tests.read_text() + {REGRESSION_TEST!r})
print("ROOT CAUSE: subtotal adds the unit price once per line, ignoring quantity.")
print("FIX: subtotal now sums lineTotal(line).")
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    real_which = shutil.which
    monkeypatch.setattr(fixer_mod.shutil, "which", lambda name, *a, **k: str(fake) if name == "muse" else real_which(name))
    monkeypatch.setenv("MODEL_API_KEY", "LLM|test-only|not-a-real-key")
    fixer = Fixer(None, config.ReproSettings(repo_path=str(shop_repo), use_muse_code=True))
    assert [e.name for e in fixer.engines] == ["muse-code"]
    result = await fixer.fix(plan(), None, tmp_path / "fix")
    assert result.status == "fixed", result.message
    assert result.engine == "muse-code" and result.muse_available
    assert result.root_cause.startswith("subtotal adds the unit price")
    call = json.loads(record.read_text())
    argv = call["argv"]
    assert argv[0] == "exec" and "Cart total ignores quantity" in call["prompt"]
    assert Path(call["cwd"]).resolve() == shop_repo.resolve()
    assert Path(argv[argv.index("--workspace") + 1]).resolve() == shop_repo.resolve()
    assert call["stdin_key"] == "LLM|test-only|not-a-real-key" and call["env_key"] is None
    assert "--api-key-stdin" in argv and "LLM|test-only|not-a-real-key" not in " ".join(argv)
    assert argv[argv.index("--approval-mode") + 1] == "never"
    assert "--yolo" not in argv and "--disable-sandbox" not in argv


# ---------------------------------------------------------------- the Muse Spark coding loop


def fn_call(name: str, call_id: str, **args: Any) -> SimpleNamespace:
    return SimpleNamespace(type="function_call", name=name, call_id=call_id, arguments=json.dumps(args))


class ScriptedClient:
    """Plays back a coding session turn by turn and records what the loop sent back."""

    def __init__(self, turns: list[list[SimpleNamespace]]) -> None:
        self.turns = turns
        self.sent: list[Any] = []
        self.settings = config.Settings()

    async def respond(self, model: str, input: Any, **kwargs: Any) -> Reply:
        self.sent.append({"input": input, "previous": kwargs.get("previous_response_id")})
        n = len(self.sent) - 1
        output = self.turns[n] if n < len(self.turns) else []
        raw = SimpleNamespace(id=f"resp-{n}", output=output, output_text="")
        return Reply(text="", model=model, response_id=raw.id, raw=raw)


async def test_spark_agent_fixes_through_the_sandbox(shop_repo: Path, tmp_path: Path) -> None:
    cart = (shop_repo / "cart.js").read_text()
    tests = (shop_repo / "cart.test.js").read_text()
    client = ScriptedClient([
        [fn_call("read_file", "c1", path="cart.js"), fn_call("read_file", "c2", path="../../../../etc/passwd")],
        [fn_call("write_file", "c3", path="cart.test.js", content=tests + REGRESSION_TEST)],
        [fn_call("run_tests", "c4")],
        [fn_call("edit_file", "c5", path="cart.js", old_text=BUGGY, new_text=FIXED)],
        [fn_call("run_tests", "c6")],
        [fn_call("finish", "c7", root_cause="subtotal ignores quantity", fix_summary="sum lineTotal",
                 regression_test="cart.test.js", tests_passed=True)],
    ])
    engine = SparkAgentEngine(client, models=["muse-spark-1.3"], max_iterations=10)  # type: ignore[arg-type]
    fixer = Fixer(None, settings(shop_repo), engines=[engine])
    result = await fixer.fix(plan(), None, tmp_path / "fix")
    assert result.status == "fixed", result.message
    assert result.engine == "spark-agent" and result.models == ["muse-spark-1.3"]
    outputs = {o["call_id"]: o["output"] for turn in client.sent[1:] for o in turn["input"]
               if isinstance(o, dict) and o.get("type") == "function_call_output"}
    assert "export function subtotal" in outputs["c1"]
    assert outputs["c2"].startswith("error:") and "outside the repository" in outputs["c2"]
    assert "exit=1" in outputs["c4"]  # the new regression test fails before the fix
    assert "exit=0" in outputs["c6"]
    assert client.sent[1]["previous"] == "resp-0"
    assert cart != (shop_repo / "cart.js").read_text()


async def test_spark_agent_that_never_finishes_is_capped(shop_repo: Path, tmp_path: Path) -> None:
    client = ScriptedClient([[fn_call("list_files", f"c{i}", path=".", pattern="*")] for i in range(50)])
    engine = SparkAgentEngine(client, models=["muse-spark-1.3"], max_iterations=4)  # type: ignore[arg-type]
    result = await Fixer(None, settings(shop_repo), engines=[engine]).fix(plan(), None, tmp_path / "fix")
    assert result.status == "failed"
    assert len(client.sent) <= 5
    assert git(shop_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_secret_scanner() -> None:
    diff = "+++ b/a.js\n+const k = 'LLM|abcdefgh|ijklmnop';\n-old\n context LLM|zzzzzzzz|yyyyyyyy\n"
    assert find_secrets(diff)
    assert find_secrets("+-----BEGIN RSA PRIVATE KEY-----\n")
    assert not find_secrets("+const total = subtotal(cart);\n")
    assert not find_secrets(" context only LLM|abcdefgh|ijklmnop\n")


def test_test_file_detection() -> None:
    assert all(is_test_file(p) for p in ("cart.test.js", "tests/test_cart.py", "src/__tests__/a.js", "a.spec.ts",
                                         "pkg/cart_test.go", "CartTests.swift"))
    assert not any(is_test_file(p) for p in ("cart.js", "src/latest.js", "contest.py", "app.js"))
