"""The reproduction executor against the real demo shop in headless Chromium. No model calls:
vision, computer use and the verifier are fakes where a test needs them."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from nimbus.engine.grounding import Located
from nimbus.engine.steps import Evidence, PlanStep, StepPlan
from nimbus.engine.verify import Verdict
from nimbus.meta.computer import AgentResult
from nimbus.repro.reproduce import (
    PlaywrightDriver,
    Reproducer,
    StepExecutor,
    key_chords,
    launch_chromium,
    resolve_url,
    target_phrases,
)


def cart_bug_plan() -> StepPlan:
    """What the video analyzer should produce for the 'same item twice' recording."""
    return StepPlan(
        id="cartbug01", kind="bug", title="Cart total ignores quantity",
        url="http://localhost:5173/",  # the recording's URL; must be rebased onto the test server
        expected="Total is $158.00 for two Aurora Headphones", actual="Total stays at $79.00 while the line shows Qty 2",
        steps=[
            PlanStep(index=0, action="navigate", text="localhost:5173", instruction="Open the shop"),
            PlanStep(index=1, action="click", target="Add to cart button",
                     target_description="green button on the Aurora Headphones card, top left of the grid",
                     instruction="Add the Aurora Headphones to the cart"),
            PlanStep(index=2, action="click", target="Add to cart (Aurora Headphones)",
                     target_description="the same Add to cart button on the Aurora Headphones card",
                     instruction="Add the same headphones again"),
            PlanStep(index=3, action="observe", instruction="Look at the cart total"),
        ],
    )


class FakeVerifier:
    def __init__(self, status: str = "done", evidence: str = "Total shows $79.00 next to Qty 2") -> None:
        self.status, self.evidence = status, evidence
        self.calls: list[dict[str, Any]] = []

    async def check(self, **kwargs: Any) -> Verdict:
        self.calls.append(kwargs)
        return Verdict(self.status, self.evidence, "", "", 0.9, model="fake-verifier")


async def button_center(url: str, name: str) -> tuple[float, float]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(url)
        box = await page.get_by_role("button", name=name).bounding_box()
        await browser.close()
    assert box
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


# ---------------------------------------------------------------- pure helpers


def test_recorded_local_urls_are_rebased_onto_the_app() -> None:
    app = "http://127.0.0.1:5199"
    assert resolve_url("localhost:5173/cart?x=1#top", app) == "http://127.0.0.1:5199/cart?x=1#top"
    assert resolve_url("http://127.0.0.1:5173", app) == "http://127.0.0.1:5199/"
    assert resolve_url("/checkout", app) == "http://127.0.0.1:5199/checkout"
    assert resolve_url("", app) == app
    assert resolve_url("https://example.com/a", app) == "https://example.com/a"
    with pytest.raises(Exception):
        resolve_url("javascript:alert(1)", app)
    with pytest.raises(Exception):
        resolve_url("file:///etc/passwd", app)


def test_target_phrases_strip_role_words_and_qualifiers() -> None:
    assert target_phrases("Add to cart button for Aurora Headphones")[-1] == "Add to cart"
    assert target_phrases("'Apply' button (discount)")[0] == "Apply"
    assert "Discount code" in target_phrases("Discount code input field")


def test_key_chords() -> None:
    assert key_chords("cmd+shift+k") == ["Meta+Shift+k"]
    assert key_chords("Tab, Tab, Return") == ["Tab", "Tab", "Enter"]
    assert key_chords("ctrl++") == ["Control++"]


# ---------------------------------------------------------------- the deterministic executor


async def test_executor_reproduces_the_cart_total_bug_with_locators(shop_url: str, tmp_path: Path) -> None:
    result = await Reproducer(None, app_url=shop_url).reproduce(cart_bug_plan(), tmp_path / "run")
    assert result.completed, result.problem
    assert result.methods == ["navigate", "locator", "locator", "skipped"]
    assert all(s.ok and s.attempts == 1 for s in result.steps)
    # Without a verifier the verdict is never "yes", however obvious the screen looks.
    assert result.reproduced == "uncertain"
    # The DOM shows the bug: the line says $158.00, the total says $79.00.
    text = " ".join(result.page_text.split())
    assert "Qty 2 × $79.00 $158.00" in text
    assert "Total $79.00" in text
    assert result.url.startswith(shop_url)
    assert result.video and Path(result.video).stat().st_size > 0
    assert len(result.screenshots) == 4 and all(Path(p).exists() for p in result.screenshots)
    assert result.final_screenshot and Path(result.final_screenshot).exists()
    assert (tmp_path / "run" / "result.json").exists()
    assert result.console_errors == []


async def test_verifier_verdicts_map_to_reproduced(shop_url: str, tmp_path: Path) -> None:
    verifier = FakeVerifier("done")
    before = await Reproducer(None, app_url=shop_url, verifier=verifier).reproduce(cart_bug_plan(), tmp_path / "b")
    assert before.reproduced == "yes" and before.evidence.startswith("Total shows")
    call = verifier.calls[0]
    assert "Total stays at $79.00" in call["expected"] and call["before"] is None
    assert "Total $79.00" in call["context"]  # the page text goes to the verifier as evidence

    fixed_verifier = FakeVerifier("done", "Total now shows $158.00")
    after = await Reproducer(None, app_url=shop_url, verifier=fixed_verifier).reproduce(
        cart_bug_plan(), tmp_path / "a", mode="fixed", compare_to=before)
    assert after.reproduced == "no" and after.expected_visible is True
    assert fixed_verifier.calls[0]["before"] == Path(before.final_screenshot).read_bytes()

    still_broken = await Reproducer(None, app_url=shop_url, verifier=FakeVerifier("not_done", "")).reproduce(
        cart_bug_plan(), tmp_path / "c", mode="fixed", compare_to=before)
    assert still_broken.reproduced == "yes" and still_broken.expected_visible is False


async def test_typing_into_labelled_inputs_reproduces_the_discount_bug(shop_url: str, tmp_path: Path) -> None:
    plan = StepPlan(kind="bug", title="Lowercase discount code rejected", url="127.0.0.1:5173", steps=[
        PlanStep(index=0, action="click", target="Add Terra Mug to cart", instruction="Add the mug"),
        PlanStep(index=1, action="type", target="Discount code field", text="save10", instruction="Type save10"),
        PlanStep(index=2, action="keypress", text="Enter", instruction="Press Enter to apply"),
    ])
    result = await Reproducer(None, app_url=shop_url).reproduce(plan, tmp_path / "run")
    assert result.completed, result.problem
    assert result.methods == ["locator", "locator", "keyboard"]
    assert '"save10" is not a valid code' in result.page_text


async def test_vision_fallback_clicks_where_the_grounder_points(shop_url: str, tmp_path: Path) -> None:
    x, y = await button_center(shop_url, "Add Lumen Desk Lamp to cart")
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    crop = io.BytesIO()
    Image.new("RGB", (90, 30), (35, 75, 58)).save(crop, format="PNG")
    (plan_dir / "step00_target.png").write_bytes(crop.getvalue())

    class FakeGrounder:
        calls: list[dict[str, Any]] = []

        async def locate(self, image: bytes, target: str, **kwargs: Any) -> Located:
            self.calls.append({"size": Image.open(io.BytesIO(image)).size, "target": target, **kwargs})
            return Located(x, y, None, 0.9, "the lamp's add button")

    grounder = FakeGrounder()
    plan = StepPlan(kind="bug", steps=[PlanStep(
        index=0, action="click", target="the shiny thing", target_description="looks like a lamp button",
        evidence=Evidence(target_crop="step00_target.png"))])
    result = await Reproducer(None, app_url=shop_url, grounder=grounder).reproduce(
        plan, tmp_path / "run", plan_dir=plan_dir)
    assert result.completed, result.problem
    assert result.methods == ["vision"]
    assert "Lumen Desk Lamp" in result.page_text.split("Your cart")[1]
    call = grounder.calls[0]
    assert call["size"] == (1280, 800)
    assert call["reference"] == crop.getvalue()  # the control as it looked in the recording
    assert call["description"] == "looks like a lamp button"


async def test_computer_use_fallback_runs_after_a_retry(shop_url: str, tmp_path: Path) -> None:
    x, y = await button_center(shop_url, "Add Drift Backpack to cart")
    tasks: list[str] = []

    async def agent(page: Any, step: PlanStep, task: str) -> AgentResult:
        tasks.append(task)
        driver = PlaywrightDriver(page)
        await driver.screenshot()
        await driver.execute({"type": "click", "x": x, "y": y, "button": "left"})
        return AgentResult("done", "Clicked the backpack's Add to cart button.", 1)

    plan = StepPlan(kind="bug", title="t", steps=[PlanStep(index=0, action="click", target="zzz qqq",
                                                           instruction="Add the backpack to the cart")])
    result = await Reproducer(None, app_url=shop_url, grounder=None, agent_runner=agent).reproduce(
        plan, tmp_path / "run")
    assert result.completed, result.problem
    assert result.methods == ["agent"]
    assert result.steps[0].attempts == 3  # two deterministic attempts, then the agent
    assert "Add the backpack to the cart" in tasks[0]
    assert "Drift Backpack" in result.page_text.split("Your cart")[1]


async def test_a_step_that_cannot_run_means_not_reproduced(shop_url: str, tmp_path: Path) -> None:
    verifier = FakeVerifier("done")
    plan = StepPlan(kind="bug", steps=[
        PlanStep(index=0, action="click", target="Add Terra Mug to cart"),
        PlanStep(index=1, action="click", target="Gift wrap toggle"),
        PlanStep(index=2, action="click", target="Checkout"),
    ])
    result = await Reproducer(None, app_url=shop_url, grounder=None, verifier=verifier, agent_runner=None).reproduce(
        plan, tmp_path / "run")
    assert not result.completed
    assert result.reproduced == "no"
    assert "step 2" in result.problem and "Gift wrap toggle" in result.problem
    assert len(result.steps) == 2  # it stops at the failed step
    assert verifier.calls == []  # an incomplete run is never sent to the verifier


async def test_ambiguous_target_is_not_guessed(shop_url: str, tmp_path: Path) -> None:
    """Four identical 'Add to cart' buttons and no hint: the ladder must not pick one at random."""
    plan = StepPlan(kind="bug", steps=[PlanStep(index=0, action="click", target="Add to cart")])
    result = await Reproducer(None, app_url=shop_url, grounder=None, agent_runner=None).reproduce(
        plan, tmp_path / "run")
    assert not result.completed
    assert "Your cart is empty." in result.page_text


async def test_playwright_driver_actions(shop_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(shop_url)
        driver = PlaywrightDriver(page)
        shot = await driver.screenshot()
        assert Image.open(io.BytesIO(shot)).size == (1280, 800)
        box = await page.get_by_label("Discount code").bounding_box()
        assert box
        await driver.execute({"type": "click", "x": box["x"] + 10, "y": box["y"] + 10})
        await driver.execute({"type": "type", "text": "SAVE10"})
        await driver.execute({"type": "keypress", "keys": ["ENTER"]})
        await driver.execute({"type": "scroll", "x": 400, "y": 400, "scroll_x": 0, "scroll_y": 200})
        await driver.execute({"type": "wait", "ms": 50})
        assert await page.locator("#discount-msg").inner_text() == "Code SAVE10 applied."
        # Browser shortcuts act on the browser: a reload clears the page's own state.
        await page.evaluate("window.__marker = 1")
        await driver.execute({"type": "keypress", "keys": ["CMD", "R"]})
        assert await page.evaluate("window.__marker === undefined")
        with pytest.raises(ValueError):
            await driver.execute({"type": "teleport"})
        await browser.close()


def test_resolve_url_extracts_a_url_from_prose():
    from nimbus.repro.reproduce import resolve_url

    app = "http://127.0.0.1:53985"
    assert resolve_url("Navigate to http://127.0.0.1:5173/ - fresh load", app) == "http://127.0.0.1:53985/"
    assert resolve_url("http://127.0.0.1:5173/ - visible in address bar as `127.0.0.1:5173`", app) == "http://127.0.0.1:53985/"
    assert resolve_url("open the shop home page", app) == app
    assert resolve_url("localhost:3000/cart?x=1", app) == "http://127.0.0.1:53985/cart?x=1"


def test_split_target_takes_the_control_and_keeps_context():
    from nimbus.repro.reproduce import split_target, target_phrases

    control, context = split_target("AURORA card > Aurora Headphones $79.00 > Add to cart button [top left tile, purple]")
    assert control == "Add to cart button"
    assert "Aurora Headphones" in context
    assert target_phrases("AURORA card > Add to cart button [top left]")[0].lower().startswith("add to cart")


async def test_path_style_targets_hit_the_right_card_by_dom(shop_url: str, tmp_path: Path) -> None:
    """The exact target a live run produced: four buttons share "Add to cart"; context must pick Aurora."""
    step = dict(action="click", target="AURORA card > Aurora Headphones $79.00 > Add to cart button [top left tile, purple]",
                target_description="dark green button under the price", text="Add to cart - Aurora Headphones")
    plan = StepPlan(kind="bug", steps=[PlanStep(index=0, **step), PlanStep(index=1, **step)])
    result = await Reproducer(None, app_url=shop_url).reproduce(plan, tmp_path / "run")
    assert result.completed, result.problem
    assert result.methods == ["locator", "locator"] or set(result.methods) <= {"locator", "dom"}
    assert "Qty 2" in " ".join(result.page_text.split())


async def test_a_vision_click_that_changes_nothing_is_a_failure(shop_url: str, tmp_path: Path) -> None:
    class BlankSpot:
        async def locate(self, image: bytes, target: str, **kwargs: Any) -> Located:
            return Located(1260, 790, None, 0.9, "empty corner")

    plan = StepPlan(kind="bug", steps=[PlanStep(index=0, action="click", target="the shiny thing")])
    result = await Reproducer(None, app_url=shop_url, grounder=BlankSpot()).reproduce(plan, tmp_path / "run")
    assert not result.completed
    assert "changed nothing" in (result.problem or "") or "changed nothing" in result.steps[0].detail


async def test_typing_into_the_address_bar_navigates(shop_url: str, tmp_path: Path) -> None:
    plan = StepPlan(kind="bug", steps=[PlanStep(index=0, action="type", target="Address bar - 127.0.0.1:5173",
                                                text="http://127.0.0.1:5173/")])
    result = await Reproducer(None, app_url=shop_url).reproduce(plan, tmp_path / "run")
    assert result.completed, result.problem
    assert result.methods == ["navigate"] and result.url.startswith(shop_url)


async def test_safety_checks_approved_only_on_the_local_app(shop_url: str) -> None:
    from nimbus.repro.reproduce import local_app_safety_policy

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.goto(shop_url)
        approve = local_app_safety_policy(page, shop_url)
        assert await approve([{"id": "c1", "code": "review", "message": "Review and confirm"}])
        assert not await approve([{"id": "c2", "code": "credential", "message": "enter a password"}])
        await page.goto("data:text/html,<p>elsewhere</p>")
        assert not await approve([{"id": "c3", "code": "review", "message": "Review and confirm"}])
        await browser.close()


async def test_a_control_that_hides_itself_when_clicked_does_not_stall_the_step() -> None:
    """Tally's Settle up button disappears once everyone is square. Describing it after the click used
    to wait out Playwright's 30 s default because the role locator no longer matched anything."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.set_content("<p id=state>Alex is owed $0.01</p><button id=settle>Settle up</button><script>"
                               "settle.onclick = () => { settle.hidden = true; state.textContent = 'All settled up'; }"
                               "</script>")
        executor = StepExecutor(page, app_url="http://127.0.0.1:9")
        outcome = await executor.run_step(PlanStep(index=0, action="click", target="Settle up button"))
        state = await page.inner_text("#state")
        await browser.close()
    assert outcome.ok and outcome.method == "locator" and outcome.attempts == 1
    assert state == "All settled up"
    assert outcome.duration_s < 5, f"the step took {outcome.duration_s:.1f}s"
    assert "Settle up" in outcome.detail


class _Chromium:
    """Stands in for `pw.chromium`: fails the bundled launch with `first`, records every launch."""

    def __init__(self, first: str, chrome_ok: bool = True) -> None:
        self.first, self.chrome_ok, self.calls = first, chrome_ok, []

    async def launch(self, **kw: Any) -> str:
        self.calls.append(kw.get("channel"))
        if "channel" not in kw:
            raise PlaywrightError(self.first)
        if not self.chrome_ok:
            raise PlaywrightError("Chromium distribution 'chrome' is not found")
        return "chrome"


class _Pw:
    def __init__(self, chromium: _Chromium) -> None:
        self.chromium = chromium


async def test_without_playwrights_chromium_repro_uses_google_chrome() -> None:
    chromium = _Chromium("Executable doesn't exist at /ms-playwright/chromium")
    assert await launch_chromium(_Pw(chromium), headless=True) == "chrome"
    assert chromium.calls == [None, "chrome"]


async def test_other_launch_failures_are_not_hidden_and_no_browser_says_what_to_install() -> None:
    crashed = _Chromium("Browser closed unexpectedly")
    with pytest.raises(PlaywrightError, match="closed unexpectedly"):
        await launch_chromium(_Pw(crashed), headless=True)
    assert crashed.calls == [None]
    neither = _Chromium("Executable doesn't exist", chrome_ok=False)
    with pytest.raises(PlaywrightError, match="install Google Chrome"):
        await launch_chromium(_Pw(neither), headless=True)
