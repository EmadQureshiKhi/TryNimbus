"""The Repro report: what was reported, what Nimbus saw, what it changed and how it proved it.

Written as Markdown (for the PR body and the repo) and as standalone HTML (for the UI and for
people), both in the run directory with images linked relatively so the folder can be zipped and
shared as is. Every claim in the report is backed by an artifact next to it: the verifier's cited
evidence with the screenshot it judged, the diff, and the raw test output.
"""
from __future__ import annotations

import html
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from nimbus import config

if TYPE_CHECKING:
    from nimbus.repro.pipeline import RunResult
    from nimbus.repro.reproduce import ReproResult

STATUS_TEXT = {
    "fixed": "Fixed and verified",
    "not_reproduced": "Not reproduced",
    "fix_failed": "Fix not verified",
    "not_verified": "Fix did not pass verification",
    "cancelled": "Cancelled",
    "error": "Error",
}
MAX_DIFF_CHARS = 60_000


def _rel(path: str | None, base: Path) -> str | None:
    if not path:
        return None
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _step_rows(result: "RunResult") -> list[tuple[str, ...]]:
    plan = result.plan
    if plan is None:
        return []
    before = {s.index: s for s in (result.before.steps if result.before else [])}
    after = {s.index: s for s in (result.after.steps if result.after else [])}
    rows = []
    for step in plan.steps:
        b, a = before.get(step.index), after.get(step.index)
        rows.append((str(step.index + 1), step.action, step.target, step.text,
                     f"{'ok' if b.ok else 'failed'} ({b.method})" if b else "-",
                     f"{'ok' if a.ok else 'failed'} ({a.method})" if a else "-",
                     step.instruction))
    return rows


def _verdict_md(title: str, repro: "ReproResult | None", base: Path) -> list[str]:
    if repro is None:
        return []
    lines = [f"## {title}", ""]
    verdict = {"yes": "bug visible", "no": "bug not visible", "uncertain": "uncertain"}[repro.reproduced]
    lines.append(f"- Verdict: **{verdict}** (verifier status `{repro.verdict_status or 'n/a'}`, confidence "
                 f"{repro.confidence:.2f}{', ' + repro.verifier_model if repro.verifier_model else ''})")
    lines.append(f"- Evidence: {repro.evidence or '-'}")
    if repro.problem:
        lines.append(f"- Problem: {repro.problem}")
    lines.append(f"- All steps completed: {'yes' if repro.completed else 'no'}")
    if repro.video:
        lines.append(f"- Browser video: [{Path(repro.video).name}]({_rel(repro.video, base)})")
    if repro.console_errors:
        lines.append("- Browser errors: " + "; ".join(f"`{e[:160]}`" for e in repro.console_errors[:6]))
    lines.append("")
    if repro.final_screenshot:
        lines.append(f"![{title} final screen]({_rel(repro.final_screenshot, base)})")
    if repro.reference_frame:
        lines.append(f"![Frame of the recording where the bug shows]({_rel(repro.reference_frame, base)})")
    lines.append("")
    return lines


def render_markdown(result: "RunResult", run_dir: Path, settings: config.ReproSettings) -> str:
    plan, fix = result.plan, result.fix
    title = plan.title if plan and plan.title else "Bug from a screen recording"
    out = [f"# Repro: {title}", "",
           f"**{STATUS_TEXT.get(result.status, result.status)}.** {result.message}", "",
           f"- Run: `{result.id}`, {time.strftime('%Y-%m-%d %H:%M', time.localtime(result.started_at))}",
           f"- Recording: `{result.video}`",
           f"- Repository: `{settings.repo_path}`, app at {settings.app_url}"]
    if fix and fix.branch and fix.ok:
        out.append(f"- Branch: `{fix.branch}` (base `{fix.base_branch}` at `{fix.base_commit[:10]}`), "
                   f"commit `{(fix.commit or '')[:10]}`")
    if result.pr_url:
        out.append(f"- Draft pull request: {result.pr_url}")
    if result.error:
        out.append(f"- Error: {result.error}")
    out.append("")
    if plan:
        out += ["## The report", "", plan.summary or "", "",
                f"- Expected: {plan.expected or '-'}", f"- Actual: {plan.actual or '-'}"]
        if plan.bug_time is not None:
            out.append(f"- Bug visible in the recording at {plan.bug_time:.1f}s")
        out += ["", "## Steps", "", "| # | Action | Target | Text | Before fix | After fix | Instruction |",
                "|---|---|---|---|---|---|---|"]
        out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in _step_rows(result)]
        out.append("")
    out += _verdict_md("Reproduction before the fix", result.before, run_dir)
    if fix:
        out += ["## Fix", "", f"- Result: **{fix.status}**: {fix.message}"]
        if fix.engine:
            out.append(f"- Engine: {fix.engine}")
        if fix.root_cause:
            out.append(f"- Root cause: {fix.root_cause}")
        if fix.summary:
            out.append(f"- Change: {fix.summary}")
        out.append(f"- Regression test added: {'yes' if fix.regression_test_added else 'no'}; fails on the old code: "
                   f"{ {True: 'yes', False: 'no', None: 'not checked'}[fix.regression_test_fails_before_fix] }")
        out.append(f"- Tests before the fix: {_yn(fix.baseline_tests_passed)}; after: {_yn(fix.tests_passed)}")
        out.append(f"- Muse Code CLI available: {'yes' if fix.muse_available else 'no'}")
        if fix.attempts:
            out += ["", "| Engine | Accepted | Detail |", "|---|---|---|"]
            out += [f"| {a.engine} | {'yes' if a.ok else 'no'} | {_cell(a.detail)[:300]} |" for a in fix.attempts]
        if fix.diff_stat:
            out += ["", "```", fix.diff_stat, "```"]
        if fix.diff:
            diff = fix.diff if len(fix.diff) <= MAX_DIFF_CHARS else fix.diff[:MAX_DIFF_CHARS] + "\n... (truncated)"
            out += ["", "```diff", diff.rstrip(), "```"]
        out.append("")
    out += _verdict_md("Reproduction after the fix", result.after, run_dir)
    if result.test_output:
        out += ["## Tests after the fix", "", f"Passed: {_yn(result.tests_passed)}", "", "```",
                result.test_output.rstrip()[-6000:], "```", ""]
    if result.models:
        out += ["## Meta models used", "", "| Role | Model |", "|---|---|"]
        out += [f"| {_cell(role)} | `{model}` |" for role, model in result.models]
        out.append("")
    out.append("Nimbus never merges. Review the branch and the evidence above before merging.")
    return "\n".join(out) + "\n"


def _yn(value: bool | None) -> str:
    return {True: "pass", False: "fail", None: "not run"}[value]


# ---------------------------------------------------------------- HTML

_CSS = """
:root{--bg:#f7f6f3;--card:#fff;--ink:#1d1f21;--muted:#5d6166;--line:#e3e1dc;--ok:#1f7a4d;--bad:#b3261e;--warn:#8a5a00;
--code:#f3f2ee;--add:#e6f4ea;--del:#fce8e6}
@media (prefers-color-scheme:dark){:root{--bg:#141516;--card:#1d1f21;--ink:#ecebe8;--muted:#a3a7ad;--line:#2f3236;
--code:#232528;--add:#173a26;--del:#43201d;--ok:#5fd394;--bad:#ff8a80;--warn:#f0c060}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 20px 64px}h1{font-size:26px;margin:0 0 8px;letter-spacing:-.01em}
h2{font-size:18px;margin:0 0 12px}section{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:20px;margin:16px 0}.status{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:600;
font-size:13px;border:1px solid currentColor}.fixed{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.meta{color:var(--muted);font-size:13px;margin:8px 0 0}dl{display:grid;grid-template-columns:max-content 1fr;
gap:6px 16px;margin:0}dt{color:var(--muted)}dd{margin:0}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);
font-weight:600}.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
figure{margin:0}figure video{width:100%;border:1px solid var(--line);border-radius:10px;display:block;background:#000}
figure img{width:100%;border:1px solid var(--line);border-radius:10px;display:block}
figcaption{color:var(--muted);font-size:13px;margin-top:6px}pre{background:var(--code);border-radius:10px;
padding:12px;overflow:auto;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}
.diff .a{background:var(--add);display:block}.diff .d{background:var(--del);display:block}
.diff .h{color:var(--muted);display:block}.diff .c{display:block}pre.log{max-height:420px}code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace}
a{color:inherit}
"""


def _e(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def _status_class(status: str) -> str:
    return {"fixed": "fixed", "not_reproduced": "warn", "cancelled": "warn"}.get(status, "bad")


def _diff_html(diff: str) -> str:
    rows = []
    for line in diff.splitlines():
        cls = "h" if line.startswith(("diff --git", "index ", "@@", "---", "+++")) else \
            "a" if line.startswith("+") else "d" if line.startswith("-") else "c"
        rows.append(f'<span class="{cls}">{_e(line) or " "}</span>')
    return '<pre class="diff">' + "".join(rows) + "</pre>"


def _figure(path: str | None, caption: str, base: Path) -> str:
    rel = _rel(path, base)
    if not rel:
        return ""
    return f'<figure><img src="{_e(rel)}" alt="{_e(caption)}" loading="lazy"><figcaption>{_e(caption)}</figcaption></figure>'


def _clip(repro: "ReproResult", caption: str, base: Path) -> str:
    """The run's browser recording as a player (its final screen as the poster), else that screenshot."""
    video, poster = _rel(repro.video, base), _rel(repro.final_screenshot, base)
    if not video:
        return _figure(repro.final_screenshot, caption, base)
    return (f'<figure><video src="{_e(video)}" poster="{_e(poster or "")}" controls muted playsinline preload="metadata">'
            f'</video><figcaption>{_e(caption)}</figcaption></figure>')


def _repro_html(title: str, repro: "ReproResult | None", base: Path) -> str:
    if repro is None:
        return ""
    verdict = {"yes": "bug visible", "no": "bug not visible", "uncertain": "uncertain"}[repro.reproduced]
    rows = [("Verdict", f"{verdict} (status {repro.verdict_status or 'n/a'}, confidence {repro.confidence:.2f})"),
            ("Evidence", repro.evidence or "-")]
    if repro.problem:
        rows.append(("Problem", repro.problem))
    rows.append(("Steps completed", "yes" if repro.completed else "no"))
    if repro.verifier_model:
        rows.append(("Verifier", repro.verifier_model))
    if repro.console_errors:
        rows.append(("Browser errors", "; ".join(repro.console_errors[:6])))
    video = f'<p class="meta"><a href="{_e(_rel(repro.video, base))}">Browser video</a></p>' if repro.video else ""
    figures = _figure(repro.final_screenshot, "Final screen of this run", base) + \
        _figure(repro.reference_frame, "The recording at the moment the bug shows", base)
    return (f"<section><h2>{_e(title)}</h2><dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in rows)
            + f"</dl>{video}<div class='shots' style='margin-top:14px'>{figures}</div></section>")


def render_html(result: "RunResult", run_dir: Path, settings: config.ReproSettings) -> str:
    plan, fix = result.plan, result.fix
    title = plan.title if plan and plan.title else "Bug from a screen recording"
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
             f"content='width=device-width,initial-scale=1'><title>Repro report</title><style>{_CSS}</style></head>"
             f"<body><main>"]
    meta = [f"Run {result.id}", time.strftime("%Y-%m-%d %H:%M", time.localtime(result.started_at)),
            f"repo {settings.repo_path}", f"app {settings.app_url}"]
    if fix and fix.ok:
        meta.append(f"branch {fix.branch} @ {(fix.commit or '')[:10]}")
    parts.append(f"<h1>{_e(title)}</h1><span class='status {_status_class(result.status)}'>"
                 f"{_e(STATUS_TEXT.get(result.status, result.status))}</span><p>{_e(result.message)}</p>"
                 f"<p class='meta'>{_e(' · '.join(meta))}</p>")
    if result.pr_url:
        parts.append(f"<p>Draft pull request: <a href='{_e(result.pr_url)}'>{_e(result.pr_url)}</a></p>")
    if result.error:
        parts.append(f"<p class='bad'>{_e(result.error)}</p>")
    if plan:
        parts.append("<section><h2>The report</h2>"
                     + (f"<p>{_e(plan.summary)}</p>" if plan.summary else "")
                     + f"<dl><dt>Expected</dt><dd>{_e(plan.expected or '-')}</dd><dt>Actual</dt>"
                       f"<dd>{_e(plan.actual or '-')}</dd>"
                     + (f"<dt>Bug in recording</dt><dd>{plan.bug_time:.1f}s</dd>" if plan.bug_time is not None else "")
                     + "</dl></section>")
        head = "".join(f"<th>{h}</th>" for h in ("#", "Action", "Target", "Text", "Before fix", "After fix",
                                                  "Instruction"))
        body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in _step_rows(result))
        parts.append(f"<section><h2>Steps</h2><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
                     "</section>")
    if result.before and result.after and result.before.final_screenshot and result.after.final_screenshot:
        parts.append("<section><h2>Before and after</h2><div class='shots'>"
                     + _clip(result.before, "Before the fix: Nimbus reproducing the bug", run_dir)
                     + _clip(result.after, "After the fix: the same steps again", run_dir) + "</div></section>")
    parts.append(_repro_html("Reproduction before the fix", result.before, run_dir))
    if fix:
        rows = [("Result", f"{fix.status}: {fix.message}")]
        if fix.engine:
            rows.append(("Engine", fix.engine))
        if fix.root_cause:
            rows.append(("Root cause", fix.root_cause))
        if fix.summary:
            rows.append(("Change", fix.summary))
        rows.append(("Regression test", f"added: {'yes' if fix.regression_test_added else 'no'}; fails on the old "
                     f"code: { {True: 'yes', False: 'no', None: 'not checked'}[fix.regression_test_fails_before_fix] }"))
        rows.append(("Tests", f"before: {_yn(fix.baseline_tests_passed)}, after: {_yn(fix.tests_passed)}"))
        rows.append(("Muse Code CLI", "available" if fix.muse_available else "not installed"))
        attempts = "".join(f"<tr><td>{_e(a.engine)}</td><td>{'yes' if a.ok else 'no'}</td><td>{_e(a.detail)}</td></tr>"
                           for a in fix.attempts)
        diff = fix.diff if len(fix.diff) <= MAX_DIFF_CHARS else fix.diff[:MAX_DIFF_CHARS] + "\n... (truncated)"
        parts.append("<section><h2>Fix</h2><dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in rows)
                     + "</dl>"
                     + (f"<table style='margin-top:14px'><thead><tr><th>Engine</th><th>Accepted</th><th>Detail</th>"
                        f"</tr></thead><tbody>{attempts}</tbody></table>" if attempts else "")
                     + (f"<h2 style='margin-top:18px'>Diff</h2>{_diff_html(diff)}" if diff else "")
                     + "</section>")
    parts.append(_repro_html("Reproduction after the fix", result.after, run_dir))
    if result.test_output:
        parts.append(f"<section><h2>Tests after the fix: {_e(_yn(result.tests_passed))}</h2>"
                     f"<pre class='log'>{_e(result.test_output.rstrip()[-6000:])}</pre></section>")
    if result.models:
        rows_html = "".join(f"<tr><td>{_e(r)}</td><td><code>{_e(m)}</code></td></tr>" for r, m in result.models)
        parts.append(f"<section><h2>Meta models used</h2><table><thead><tr><th>Role</th><th>Model</th></tr></thead>"
                     f"<tbody>{rows_html}</tbody></table></section>")
    parts.append("<p class='meta'>Nimbus never merges. Review the branch and the evidence above before merging.</p>"
                 "</main></body></html>")
    return "".join(parts)


def write_report(result: "RunResult", run_dir: Path, settings: config.ReproSettings) -> tuple[Path, Path]:
    md = run_dir / "report.md"
    page = run_dir / "report.html"
    md.write_text(render_markdown(result, run_dir, settings))
    page.write_text(render_html(result, run_dir, settings))
    return md, page


def pr_body(result: "RunResult") -> str:
    """A PR description without local image links (they would not resolve on GitHub)."""
    plan, fix, before, after = result.plan, result.fix, result.before, result.after
    lines = ["Reproduced from a narrated screen recording and fixed by Nimbus Repro.", ""]
    if plan:
        lines += [f"**Bug:** {plan.title}", "", f"- Expected: {plan.expected}", f"- Actual: {plan.actual}", "",
                  "**Steps**", ""]
        lines += [f"{s.index + 1}. {s.instruction or (s.action + ' ' + s.target)}" for s in plan.steps]
        lines.append("")
    if before:
        lines.append(f"**Reproduced before the fix:** {before.evidence}")
    if fix:
        if fix.root_cause:
            lines.append(f"**Root cause:** {fix.root_cause}")
        if fix.summary:
            lines.append(f"**Change:** {fix.summary}")
        lines.append(f"**Regression test:** added, and it fails on the original code: "
                     f"{ {True: 'yes', False: 'no', None: 'not checked'}[fix.regression_test_fails_before_fix] }")
    if after:
        lines.append(f"**Verified after the fix:** {after.evidence}")
    lines.append(f"**Tests:** {_yn(result.tests_passed)}")
    if result.models:
        lines += ["", "Meta models: " + ", ".join(sorted({m for _, m in result.models}))]
    lines += ["", "Opened as a draft. Nimbus never merges."]
    return "\n".join(lines) + "\n"
