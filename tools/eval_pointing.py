"""Pointing accuracy eval: does Nimbus land on the right control?

Each case loads a fixture UI in headless Chromium at Retina scale, asks a real question through the
same `AskService` the app uses (screenshot + question -> walkthrough -> zoom refine), and checks
whether the first pointed step lands inside the true element's bounding box from the DOM.

    .venv/bin/python tools/eval_pointing.py            # all cases
    .venv/bin/python tools/eval_pointing.py --no-refine

Writes artifacts/evals/pointing.json and pointing.md, plus an annotated screenshot per case.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.async_api import async_playwright

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # runnable as tools/<name>.py

from nimbus.ask import AskService  # noqa: E402
from nimbus.mac.control import AppContext  # noqa: E402
from nimbus.mac.screen import Capture, Display  # noqa: E402
from nimbus.meta.client import MetaClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "ui"
OUT = ROOT / "artifacts" / "evals"
EFFORT = "low"

CASES = [
    ("chat_app.html", "", "How do I start a new chat?", "#new-chat"),
    ("chat_app.html", "?theme=dark", "how do i open a new conversation here", "#new-chat"),
    ("chat_app.html", "", "Where do I switch which model I'm using?", "#model-picker"),
    ("chat_app.html", "", "Where are the settings?", "#settings"),
    ("chat_app.html", "", "How do I attach a file?", "#attach"),
    ("chat_app.html", "", "How can I share this conversation?", "#share"),
    ("video_editor.html", "", "Where's the blade tool to cut a clip?", "#blade"),
    ("video_editor.html", "", "How do I get to the color grading page?", "#tab-color"),
    ("video_editor.html", "", "Where do I go to export my video?", "#tab-deliver"),
    ("video_editor.html", "", "How do I add a marker on the timeline?", "#marker"),
    ("video_editor.html", "", "Where can I see the render queue?", "#render-queue"),
    ("video_editor.html", "", "Where do I change a clip's opacity?", "#inspector"),
]
# Live public web apps (no sign-in). Ground truth comes from each page's DOM at run time.
WEB_CASES = [
    ("https://github.com/meta-models/meta-model-cookbook", "How do I download this code?",
     lambda p: p.get_by_role("button", name=re.compile(r"^\s*Code\s*$", re.I)).first),
    ("https://github.com/meta-models/meta-model-cookbook", "Where can I see the open issues?",
     lambda p: p.locator("#issues-tab, a[href$='/issues']").first),
    ("https://en.wikipedia.org/wiki/Python_(programming_language)", "How do I read this article in another language?",
     lambda p: p.locator("#p-lang-btn").first),
    ("https://en.wikipedia.org/wiki/Python_(programming_language)", "Where can I see the edit history?",
     lambda p: p.locator("#ca-history").first),
    ("https://excalidraw.com", "Which tool draws a rectangle?", lambda p: p.locator("[data-testid='toolbar-rectangle']").first),
    ("https://excalidraw.com", "How do I add text to the canvas?", lambda p: p.locator("[data-testid='toolbar-text']").first),
    ("https://excalidraw.com", "Where's the main menu to export my drawing?",
     lambda p: p.locator("[data-testid='main-menu-trigger']").first),
    ("https://vscode.dev", "How do I open the extensions marketplace?",
     lambda p: p.locator("a[aria-label^='Extensions']").first),
    ("https://vscode.dev", "Where do I search across files?", lambda p: p.locator("a[aria-label^='Search']").first),
    ("https://vscode.dev", "Where is the file explorer?", lambda p: p.locator("a[aria-label^='Explorer']").first),
]
MATH_CASES = [
    ("whiteboard.html", "", "Solve problem 1 step by step", "#eq1"),
    ("whiteboard.html", "", "Walk me through solving problem 2", "#eq2"),
]


@dataclass
class Result:
    page: str
    question: str
    target: str
    hit: bool
    distance_px: float
    refined: bool
    latency_s: float
    reply: str
    steps: int
    error: str = ""


async def run_web(refine: bool) -> list[Result]:
    """The same scoring on live public web apps."""
    client = MetaClient()
    client.settings.refine_pointing = refine
    client.settings.ask_effort = EFFORT
    ask = AskService(client)
    OUT.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        current = None
        for url, question, locate in WEB_CASES:
            if url != current:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(5000)
                current = url
            box = await locate(page).bounding_box()
            png = await page.screenshot()
            img = Image.open(io.BytesIO(png))
            cap = Capture(Display(0, 1, 0.0, 0.0, 1440.0, 900.0, 2.0, True), png, img.width, img.height, time.time())
            ask.history.clear()
            started = time.perf_counter()
            try:
                walk = await ask.ask(question, [cap], AppContext("Google Chrome", "", url, 0), allow_operate=False)
            except Exception as exc:
                results.append(Result(url, question, "dom", False, -1, False, 0, "", 0, str(exc)))
                continue
            latency = time.perf_counter() - started
            shown = [s for s in walk.steps if s.gx is not None]
            first = next((s for s in shown if s.kind == "point"), shown[0] if shown else None)
            hit = bool(first and box) and _inside(first.gx, first.gy, box, pad=4)
            dist = _distance(first.gx, first.gy, box) if first and box else -1.0
            results.append(Result(url, question, "dom", hit, round(dist, 1), bool(first and first.refined),
                                  round(latency, 2), walk.reply, len(walk.steps)))
            if box:
                _annotate(img, box, shown, OUT / f"web_{len(results):02d}.png")
            print(f"{'HIT ' if hit else 'MISS'} {latency:5.1f}s  {url.split('//')[1][:28]:28}  {question}")
        await browser.close()
    return results


async def run(refine: bool) -> list[Result]:
    client = MetaClient()
    client.settings.refine_pointing = refine
    client.settings.ask_effort = EFFORT
    ask = AskService(client)
    OUT.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        for name, query, question, selector in CASES + MATH_CASES:
            await page.goto((FIXTURES / name).as_uri() + query)
            await page.wait_for_timeout(150)
            png = await page.screenshot()
            box = await page.locator(selector).bounding_box()
            display = Display(0, 1, 0.0, 0.0, 1440.0, 900.0, 2.0, True)
            img = Image.open(io.BytesIO(png))
            cap = Capture(display, png, img.width, img.height, time.time())
            ask.history.clear()
            started = time.perf_counter()
            try:
                walk = await ask.ask(question, [cap], AppContext("Chromium", "", name, 0), allow_operate=False)
            except Exception as exc:  # recorded, the eval keeps going
                results.append(Result(name + query, question, selector, False, -1, False, 0, "", 0, str(exc)))
                continue
            latency = time.perf_counter() - started
            shown = [s for s in walk.steps if s.gx is not None]
            is_math = (name, query, question, selector) in MATH_CASES
            if is_math:  # working is anchored on the equation, or written in lines directly beneath it
                inside = [_on_or_below(s.gx, s.gy, box) for s in shown]
                hit = bool(shown) and all(inside)
                dist = 0.0 if hit else -1.0
                refined = False
            else:
                first = next((s for s in shown if s.kind == "point"), shown[0] if shown else None)
                hit = bool(first) and _inside(first.gx, first.gy, box, pad=4)
                dist = _distance(first.gx, first.gy, box) if first else -1.0
                refined = bool(first and first.refined)
            results.append(Result(name + query, question, selector, hit, round(dist, 1), refined, round(latency, 2),
                                  walk.reply, len(walk.steps)))
            _annotate(img, box, shown, OUT / f"pointing_{len(results):02d}.png")
            print(f"{'HIT ' if hit else 'MISS'} {latency:5.1f}s  {question}  ->  {[ (round(s.gx), round(s.gy)) for s in shown][:3]}")
        await browser.close()
    return results


def _inside(x, y, box, pad=0) -> bool:
    return box["x"] - pad <= x <= box["x"] + box["width"] + pad and box["y"] - pad <= y <= box["y"] + box["height"] + pad


def _on_or_below(x, y, box) -> bool:
    return (box["x"] - 40 <= x <= box["x"] + box["width"] + 40
            and box["y"] - 40 <= y <= box["y"] + box["height"] + 260)


def _distance(x, y, box) -> float:
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    return ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5


def _annotate(img: Image.Image, box, steps, path: Path) -> None:
    img = img.convert("RGB")
    d = ImageDraw.Draw(img)
    s = 2.0
    d.rectangle([box["x"] * s, box["y"] * s, (box["x"] + box["width"]) * s, (box["y"] + box["height"]) * s],
                outline=(34, 197, 94), width=4)
    for i, st in enumerate(steps):
        x, y = st.gx * s, st.gy * s
        d.ellipse([x - 14, y - 14, x + 14, y + 14], outline=(255, 122, 26), width=5)
        d.text((x + 18, y - 10), str(i + 1), fill=(255, 122, 26))
    img.save(path)


def report_web(results: list[Result]) -> None:
    hits = sum(r.hit for r in results)
    lat = [r.latency_s for r in results if r.latency_s]
    summary = {"web_pointing_hit_rate": f"{hits}/{len(results)}",
               "median_latency_s": round(statistics.median(lat), 2) if lat else None}
    (OUT / "pointing_web.json").write_text(json.dumps({"summary": summary, "results": [asdict(r) for r in results]},
                                                      indent=2))
    lines = ["# Pointing eval on live web apps", "", f"Hit rate: **{summary['web_pointing_hit_rate']}**", "",
             "| Result | App | Question | Latency (s) |", "|---|---|---|---|"]
    lines += [f"| {'hit' if r.hit else 'miss'} | {r.page.split('//')[1].split('/')[0]} | {r.question} | {r.latency_s} |"
              for r in results]
    (OUT / "pointing_web.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, indent=2))


def report(results: list[Result], refine: bool) -> None:
    point = [r for r in results if "whiteboard" not in r.page]
    hits = sum(r.hit for r in point)
    lat = [r.latency_s for r in results if r.latency_s]
    dists = [r.distance_px for r in point if r.distance_px >= 0]
    summary = {
        "refine": refine,
        "pointing_hit_rate": f"{hits}/{len(point)}",
        "math_anchored": f"{sum(r.hit for r in results if 'whiteboard' in r.page)}/{len(results) - len(point)}",
        "median_distance_to_center_px": round(statistics.median(dists), 1) if dists else None,
        "median_latency_s": round(statistics.median(lat), 2) if lat else None,
    }
    (OUT / "pointing.json").write_text(json.dumps({"summary": summary, "results": [asdict(r) for r in results]}, indent=2))
    lines = ["# Pointing eval", "", "| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in summary.items()] + [
        "", "| Result | Question | Target | Distance to centre (px) | Latency (s) |", "|---|---|---|---|---|"]
    lines += [f"| {'hit' if r.hit else 'miss'} | {r.question} | `{r.target}` | {r.distance_px} | {r.latency_s} |"
              for r in results]
    (OUT / "pointing.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-refine", action="store_true")
    parser.add_argument("--web", action="store_true", help="live public web apps instead of the fixtures")
    parser.add_argument("--effort", default="low", choices=["minimal", "low", "medium"])
    args = parser.parse_args()
    global EFFORT
    EFFORT = args.effort
    if args.web:
        report_web(asyncio.run(run_web(refine=not args.no_refine)))
        return
    results = asyncio.run(run(refine=not args.no_refine))
    report(results, refine=not args.no_refine)


if __name__ == "__main__":
    main()
