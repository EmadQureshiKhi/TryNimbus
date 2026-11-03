"""Run the real Repro pipeline on the Tally bug recording, headless, and check every stage.

    .venv/bin/python tools/tally_repro_check.py [--video artifacts/fixtures/bug_tally_cent.mp4] [--runs N]

For each run: copies demo/tally (the seeded rounding bug) into a throwaway temp git
repo, serves it on a free port, and runs `ReproPipeline` with the real Meta API (MODEL_API_KEY from
.env) and Muse Code as the fixer: Muse Spark plans from the narrated video, Chromium reproduces the
bug headless, Muse Code fixes it on a branch, the tests and the same steps run again, and the
report is written. Nothing opens a window and demo/tally is never touched.

Checks per run: the plan has the type, Add expense and Settle up steps; the bug is reproduced with
evidence; the fix changes ledger.js and adds a regression test that fails on the old code; the tests
pass; the same steps afterwards show the expected state; the fixed ledger splits every amount so the
shares add up exactly (checked independently with node); main in the clone still has the bug.
Results go to artifacts/tmp/tally_repro/run-<n>.json and a summary is printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "artifacts" / "tmp" / "tally_repro"
os.environ.setdefault("NIMBUS_HOME", str(OUT / "home"))  # default settings, no state from the real app
sys.path.insert(0, str(ROOT))

from nimbus import config  # noqa: E402
from nimbus.meta.client import MetaClient  # noqa: E402
from nimbus.repro.pipeline import ReproPipeline  # noqa: E402
from tools.demo_repo import digest, seed  # noqa: E402

log = logging.getLogger("tally_repro_check")

TALLY = ROOT / "demo" / "tally"
VIDEO = ROOT / "artifacts" / "fixtures" / "bug_tally_cent.mp4"

# Independent check of the fixed ledger: shares always add up, and the recorded bug is gone.
LEDGER_CHECK = """
const L = await import(process.argv[1]);
const ids = ["alex", "sam", "jo", "kim", "lee", "max", "ana"];
let bad = [];
for (let n = 1; n <= ids.length; n++)
  for (let cents = 1; cents <= 2000; cents++) {
    const shares = Object.values(L.splitEvenly(cents, ids.slice(0, n)));
    const sum = shares.reduce((a, b) => a + b, 0);
    const spread = Math.max(...shares) - Math.min(...shares);
    if (sum !== cents || spread > 1) bad.push(`${cents}/${n}`);
  }
let g = L.createGroup("Lisbon trip", ["Alex", "Sam", "Jo"]);
g = L.settleUp(L.addExpense(g, { description: "Dinner", amountCents: 10000, paidBy: "alex" }));
console.log(JSON.stringify({ bad: bad.slice(0, 10), badCount: bad.length, settled: L.isSettled(g), balances: L.balances(g) }));
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def clone_main(dest: Path) -> Path:
    """Tally (seeded bug and all) in a throwaway repo with no remote."""
    return seed(TALLY, dest, author="Nimbus Repro Check")


def ledger_check(repo: Path) -> dict:
    out = subprocess.run(["node", "--input-type=module", "-e", LEDGER_CHECK, str(repo / "ledger.js")],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return {"error": out.stderr.strip()[-600:]}
    return json.loads(out.stdout.strip().splitlines()[-1])


async def one_run(n: int, video: Path, keep: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"tally-repro-{n}-"))
    repo = clone_main(tmp / "tally")
    port = free_port()
    client = MetaClient()
    settings = config.ReproSettings(repo_path=str(repo), app_url=f"http://127.0.0.1:{port}",
                                    start_command=f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
                                    test_command="node --test", use_muse_code=True, open_pull_request=False)
    pipeline = ReproPipeline(client, settings, data_root=OUT / "runs")
    stages: dict[str, float] = {}
    started = time.monotonic()

    def progress(stage: str, message: str, fraction: float) -> None:
        stages.setdefault(stage, round(time.monotonic() - started, 1))
        print(f"  [{time.monotonic() - started:6.1f}s {stage:9s}] {message[:150]}", flush=True)

    result = await pipeline.run(video, progress=progress)
    total = round(time.monotonic() - started, 1)

    plan, before, fix, after = result.plan, result.before, result.fix, result.after
    steps = [{"action": s.action, "target": s.target, "text": s.text, "instruction": s.instruction}
             for s in (plan.steps if plan else [])]
    actions = " ".join(f"{s['action']} {s['target']} {s['text']}".lower() for s in steps)
    fixed_ledger = ledger_check(repo) if fix and fix.ok else {}
    checks = {
        "plan: types the dinner and clicks Add expense and Settle up": bool(
            plan and "100" in actions and "add expense" in actions and "settle up" in actions),
        "before: bug reproduced with evidence": bool(before and before.reproduced == "yes" and before.evidence),
        "fix: by Muse Code": bool(fix and fix.engine == "muse-code"),
        "fix: changes ledger.js": bool(fix and "ledger.js" in fix.changed_files),
        "fix: regression test added": bool(fix and fix.regression_test_added),
        "fix: regression test fails on the old code": bool(fix and fix.regression_test_fails_before_fix is True),
        "tests pass after the fix": result.tests_passed is True,
        "after: expected behaviour visible, bug gone": bool(after and after.expected_visible is True
                                                           and after.reproduced == "no"),
        "fixed ledger: shares add up for every amount": fixed_ledger.get("badCount") == 0,
        "fixed ledger: $100 three ways settles to zero": fixed_ledger.get("settled") is True,
        "status fixed": result.status == "fixed",
        "clone: main still has the seeded bug": git(repo, "show", "main:ledger.js") == (TALLY / "ledger.js").read_text().strip(),
    }
    report = {
        "run": n, "status": result.status, "message": result.message, "total_s": total,
        "stage_start_s": stages, "timings": result.timings, "checks": checks,
        "plan": {"title": plan.title if plan else "", "url": plan.url if plan else "",
                 "expected": plan.expected if plan else "", "actual": plan.actual if plan else "",
                 "bug_time": plan.bug_time if plan else None, "model": plan.model if plan else "", "steps": steps},
        "before": {"reproduced": before.reproduced if before else None, "evidence": before.evidence if before else "",
                   "methods": [s.method for s in before.steps] if before else []},
        "fix": {"engine": fix.engine if fix else None, "branch": fix.branch if fix else "",
                "root_cause": fix.root_cause if fix else "", "summary": fix.summary if fix else "",
                "changed_files": fix.changed_files if fix else [], "diff": fix.diff if fix else "",
                "attempts": [{"engine": a.engine, "ok": a.ok, "detail": a.detail[:400], "duration_s": round(a.duration_s, 1)}
                             for a in (fix.attempts if fix else [])],
                "message": fix.message if fix else ""},
        "fixed_ledger": fixed_ledger,
        "after": {"reproduced": after.reproduced if after else None, "evidence": after.evidence if after else "",
                  "methods": [s.method for s in after.steps] if after else []},
        "tests": result.test_output[-1500:], "report_html": result.report_html, "run_dir": result.run_dir,
        "error": result.error, "clone": str(repo) if keep else "",
    }
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return report


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--video", type=Path, default=VIDEO)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--keep", action="store_true", help="keep the throwaway clone for inspection")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not args.video.exists():
        print(f"no bug recording at {args.video}: record a narrated bug video of Tally there")
        return 2
    if not config.get_secret("MODEL_API_KEY"):
        print("MODEL_API_KEY is not set (.env or Keychain)")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    demo_before = digest(TALLY)
    ok_all = True
    for n in range(1, args.runs + 1):
        print(f"run {n}: {args.video.name}", flush=True)
        report = await one_run(n, args.video, args.keep)
        report["checks"]["demo/tally untouched"] = digest(TALLY) == demo_before
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = OUT / f"run-{stamp}-{n}.json"
        path.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nplan ({report['plan']['model']}): {report['plan']['title']}")
        for i, s in enumerate(report["plan"]["steps"], 1):
            print(f"  {i}. {s['action']} {s['target']!r} {s['text']!r}")
        print(f"before: {report['before']['reproduced']} via {report['before']['methods']}: {report['before']['evidence'][:300]}")
        print(f"fix: {report['fix']['engine']} on {report['fix']['branch']}, files {report['fix']['changed_files']}")
        print(f"  root cause: {report['fix']['root_cause'][:300]}")
        print(report["fix"]["diff"])
        print(f"fixed ledger: {report['fixed_ledger']}")
        print(f"after: {report['after']['reproduced']}: {report['after']['evidence'][:300]}")
        for name, ok in report["checks"].items():
            print(f"{'PASS' if ok else 'FAIL'} {name}")
        print(f"timings (s): {report['timings']}  total {report['total_s']}s")
        print(f"{report['status']}: {report['message']}")
        print(f"report: {report['report_html']}\nresult: {path}\n", flush=True)
        ok_all = ok_all and all(report["checks"].values())
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
