"""End to end on the real Meta Model API: generated bug video -> plan -> reproduction -> fix -> proof.

    .venv/bin/python tools/make_bug_video.py          # once, makes artifacts/fixtures/bug_cart_total.mp4
    .venv/bin/python -m pytest -m live tests/test_repro_live.py -s

Needs MODEL_API_KEY. Uses Muse Code when `muse` is installed, else the Muse Spark coding agent. Works
on a throwaway git copy of the demo shop, served on a free port, so the real demo repo is untouched.
"""
from __future__ import annotations

import json
import logging
import socket
import sys
from pathlib import Path

import pytest

from nimbus import config
from nimbus.meta.client import MetaClient
from nimbus.repro.pipeline import ReproPipeline

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "artifacts" / "fixtures" / "bug_cart_total.mp4"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.live
async def test_whole_pipeline_fixes_the_cart_total_bug(shop_repo: Path, tmp_path: Path) -> None:
    if not config.get_secret("MODEL_API_KEY"):
        pytest.skip("MODEL_API_KEY is not set")
    if not FIXTURE.exists():
        pytest.skip("run tools/make_bug_video.py first")
    logging.basicConfig(level=logging.INFO)
    truth = json.loads(FIXTURE.with_suffix(".json").read_text())
    port = free_port()
    settings = config.ReproSettings(
        repo_path=str(shop_repo), app_url=f"http://127.0.0.1:{port}",
        start_command=f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
        test_command="node --test", use_muse_code=True, open_pull_request=False,
    )
    events: list[tuple[str, str, float]] = []
    result = await ReproPipeline(MetaClient(), settings, data_root=tmp_path / "runs").run(
        FIXTURE, progress=lambda stage, message, fraction: events.append((stage, message, fraction)))
    print(f"\nreport: {result.report_html}\n{result.status}: {result.message}")

    assert result.status == "fixed", result.message
    plan = result.plan
    assert plan is not None and plan.kind == "bug"
    assert sum(1 for s in plan.steps if s.action == "click") >= 2
    # The bug is on screen from the click that causes it; it must not be placed before that click.
    last_click = max(c["t"] for c in truth["clicks"])
    assert plan.bug_time is not None and last_click - 1.0 <= plan.bug_time <= truth["duration_s"]
    assert result.before and result.before.reproduced == "yes" and result.before.evidence
    assert result.fix and result.fix.ok and result.fix.regression_test_fails_before_fix is True
    assert "cart.js" in result.fix.changed_files
    assert result.after and result.after.expected_visible is True and result.after.reproduced == "no"
    assert result.tests_passed is True
    assert [s for s in dict.fromkeys(stage for stage, _, _ in events)] == [
        "analyze", "launch", "reproduce", "fix", "verify", "report"]
    assert Path(result.report_html).exists()
