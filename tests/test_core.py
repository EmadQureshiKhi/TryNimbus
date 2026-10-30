"""Unit tests for the engine, with every model call faked. Live tests are marked `live`."""
from __future__ import annotations

import asyncio
import io
import time
import wave
from pathlib import Path

import openai
import pytest
from PIL import Image

from nimbus.ask import AskService
from nimbus.engine.steps import PlanStep, StepPlan, plan_from_model
from nimbus.engine.verify import Verdict
from nimbus.engine.video import CursorPath, _best_cursor_track
from nimbus.mac.control import AppContext
from nimbus.mac.screen import Capture, Display
from nimbus.meta import resilience
from nimbus.meta.client import parse_json_loose
from nimbus.meta.computer import AgentResult
from nimbus.meta.resilience import CircuitBreaker, MetaAPIError, RetryPolicy, first_success, with_retries
from nimbus.meta.sam import Box, SegmentResult, Track
from nimbus.meta.voice import pcm_to_wav
from nimbus.shadow.session import ShadowSession

FAST = RetryPolicy(attempts=3, base_delay=0.001, max_delay=0.002)


def _status_error(code: int) -> openai.APIStatusError:
    import httpx

    req = httpx.Request("POST", "https://api.meta.ai/v1/responses")
    return openai.APIStatusError(f"status {code}", response=httpx.Response(code, request=req), body={"message": "x"})


# ---------------------------------------------------------------- resilience


async def test_retries_transient_then_succeeds():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _status_error(503)
        return "ok"

    assert await with_retries(fn, key="t1", policy=FAST) == "ok"
    assert calls == 3


async def test_never_retries_a_400():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise _status_error(400)

    with pytest.raises(MetaAPIError) as err:
        await with_retries(fn, key="t2", policy=FAST)
    assert calls == 1 and err.value.status == 400 and not err.value.retryable


def test_circuit_breaker_opens_and_half_opens():
    b = CircuitBreaker(threshold=2, cooldown=0.05)
    b.record(False)
    assert b.allow()
    b.record(False)
    assert not b.allow()
    time.sleep(0.06)
    assert b.allow()
    b.record(True)
    assert b.failures == 0


async def test_first_success_falls_through_models():
    async def call(model):
        if model == "a":
            raise MetaAPIError("model a unavailable", status=503, retryable=True)
        return model

    assert await first_success(["a", "b"], call, what="x") == "b"


async def test_first_success_does_not_hide_a_bad_request():
    async def call(model):
        raise MetaAPIError("schema invalid", status=400)

    with pytest.raises(MetaAPIError):
        await first_success(["a", "b"], call, what="x")


def test_parse_json_loose():
    assert parse_json_loose('{"a": 1}') == {"a": 1}
    assert parse_json_loose('Sure!\n```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert parse_json_loose('prefix [{"x": 1}] suffix') == [{"x": 1}]
    with pytest.raises(ValueError):
        parse_json_loose("no json here")


def setup_function(_):
    resilience._breakers.clear()


# ---------------------------------------------------------------- step plans


def test_plan_from_model_sorts_clamps_and_reindexes():
    data = {"title": "T", "app": "Figma", "summary": "", "start_state": "",
            "steps": [{"t_start": 9, "t_end": 7, "action": "click", "target": "B", "instruction": "b"},
                      {"t_start": 2, "t_end": 50, "action": "type", "text": "hi", "instruction": "a"}]}
    plan = plan_from_model(data, kind="tutorial", source_video="v.mp4", duration_s=20, model="m")
    assert [s.instruction for s in plan.steps] == ["a", "b"]
    assert [s.index for s in plan.steps] == [0, 1]
    assert plan.steps[0].t_end == 20  # clamped to the video
    assert plan.steps[1].t_end >= plan.steps[1].t_start


def test_plan_roundtrip(tmp_path: Path):
    plan = StepPlan(title="x", steps=[PlanStep(index=0, instruction="click it", target="OK")])
    plan.save(tmp_path)
    again = StepPlan.load(tmp_path)
    assert again.steps[0].target == "OK" and again.id == plan.id


# ---------------------------------------------------------------- coordinates


def _capture(w=2880, h=1800, dx=0.0, dy=0.0) -> Capture:
    img = Image.new("RGB", (w, h), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Capture(Display(0, 1, dx, dy, w / 2, h / 2, 2.0, True), buf.getvalue(), w, h, time.time())


def test_retina_mapping_roundtrip():
    cap = _capture(dx=-1440, dy=120)  # a secondary display left of the main one
    gx, gy = cap.pixel_to_global(2000, 900)
    assert (gx, gy) == (-1440 + 1000, 120 + 450)
    assert cap.global_to_pixel(gx, gy) == (2000, 900)
    assert cap.norm_to_global(500, 500) == (-1440 + 720, 120 + 450)
    assert cap.norm_to_global(1003, -2) == (-1440 + 1440, 120)  # clamped to the grid


class _FakeClient:
    def __init__(self, data):
        self.data = data
        from nimbus.config import Settings

        self.settings = Settings()
        self.settings.refine_pointing = False

    async def respond_json(self, *a, **k):
        class R:
            model = "muse-spark-1.3"
        return self.data, R()


async def test_ask_maps_steps_to_global_points():
    data = {"reply": "Here.", "intent": "explain", "operate_task": "", "steps": [
        {"say": "Click New chat", "kind": "point", "target": "New chat", "target_description": "", "note": "",
         "image": 0, "x": 100, "y": 200, "x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0},
        {"say": "2x = 8", "kind": "note", "target": "", "target_description": "", "note": "2x = 8", "image": 0,
         "x": 500, "y": 500, "x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0},
        {"say": "That's it", "kind": "say", "target": "", "target_description": "", "note": "", "image": 0,
         "x": 0, "y": 0, "x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0},
    ]}
    svc = AskService(_FakeClient(data))
    walk = await svc.ask("how?", [_capture()], AppContext("App", "", "", 1), allow_operate=False)
    assert walk.reply == "Here."
    assert (walk.steps[0].gx, walk.steps[0].gy) == (144.0, 180.0)
    assert walk.steps[1].kind == "note" and walk.steps[1].note == "2x = 8"
    assert walk.steps[2].gx is None
    assert svc.history[-1].reply == "Here."


# ---------------------------------------------------------------- cursor tracking


def test_cursor_track_picks_small_persistent_object_and_dwell():
    cursor = Track("1", {f: Box(100 + (0 if f >= 6 else f * 40), 100, 112, 118) for f in range(10)})
    window = Track("2", {f: Box(0, 0, 900, 600) for f in range(10)})
    blip = Track("3", {2: Box(5, 5, 9, 9)})
    best = _best_cursor_track(SegmentResult(1280, 720, {"1": cursor, "2": window, "3": blip}), 1280 * 720)
    assert best is cursor
    path = CursorPath(best, fps=4.0, sx=1.5, sy=1.5)
    rest = path.dwell(0.0, 2.5)
    assert rest is not None and rest[0] == 1.5  # the cursor comes to rest at frame 6 (1.5 s)
    t = rest[0]
    x, y = path.position(t)
    assert abs(x - (100 + 0.15 * 12) * 1.5) < 2


def test_pcm_to_wav_header():
    wav = pcm_to_wav(b"\x00\x00" * 2400)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 2400)


# ---------------------------------------------------------------- Shadow state machine


class _Host:
    def __init__(self):
        self.events: list[tuple] = []

    async def capture(self):
        return _capture(400, 300)

    async def show_step(self, plan, step, plan_dir):
        self.events.append(("step", step.index))

    async def point(self, gx, gy, box, label, step_no):
        self.events.append(("point", label, step_no))

    async def clear_marks(self):
        pass

    async def say(self, text, *, speak=True):
        self.events.append(("say", text))

    async def verdict(self, text, ok):
        self.events.append(("verdict", ok))

    async def finished(self, text):
        self.events.append(("finished", text))

    async def run_agent(self, task):
        self.events.append(("agent", task))
        return AgentResult("done", "ok", 1)


class _Grounder:
    async def locate(self, *a, **k):
        from nimbus.engine.grounding import Located
        return Located(100, 80, (90, 70, 110, 90), 0.9)


class _Verifier:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    async def check(self, **k):
        status = self.outcomes.pop(0)
        return Verdict(status, "the dialog opened" if status == "done" else "", "nothing changed", "click it", 0.9)


def _session(tmp_path, outcomes, n_steps=2):
    plan = StepPlan(title="Demo", app="App", steps=[
        PlanStep(index=i, action="click", target=f"Button {i}", instruction=f"Click button {i}") for i in range(n_steps)])
    host = _Host()
    s = ShadowSession(_FakeClient({}), host, plan, tmp_path)
    s.grounder = _Grounder()
    s.verifier = _Verifier(outcomes)
    return s, host


async def test_shadow_advances_only_on_evidence_and_finishes(tmp_path):
    s, host = _session(tmp_path, ["not_done", "done", "done"])
    await s.start()
    assert ("point", "Button 0", 1) in host.events
    await s.check()  # not done -> stays on step 0
    assert s.index == 0 and s.failed_checks == 1
    await s.check()  # done -> step 1
    assert s.index == 1 and 0 in s.completed
    await s.check()  # done -> finished
    assert not s.active
    assert any(e[0] == "finished" for e in host.events)


async def test_shadow_escalates_after_three_failed_checks(tmp_path):
    s, host = _session(tmp_path, ["not_done"] * 3)
    await s.start()
    for _ in range(3):
        await s.check()
    await asyncio.sleep(0)  # speech runs alongside the guide
    assert s.index == 0
    assert any(e[0] == "say" and "can't confirm" in e[1] for e in host.events)


async def test_shadow_progress_resumes(tmp_path):
    s, _ = _session(tmp_path, ["done"], n_steps=3)
    await s.start()
    await s.check()
    again, _ = _session(tmp_path, [], n_steps=3)
    assert again.index == 1 and 0 in again.completed


async def test_shadow_do_for_me_runs_agent_then_verifies(tmp_path):
    s, host = _session(tmp_path, ["done"])
    await s.start()
    await s.handle("do")
    assert any(e[0] == "agent" and "Button 0" in e[1] for e in host.events)
    assert s.index == 1


async def test_clicks_ignored_on_typing_steps(tmp_path):
    s, _ = _session(tmp_path, [])
    s.plan.steps[0].action = "type"
    await s.start()
    s.on_click(10, 10)
    await asyncio.sleep(0)
    assert s._timer is None


async def test_click_while_nimbus_is_busy_is_checked_not_dropped(tmp_path, monkeypatch):
    import nimbus.shadow.session as sess

    monkeypatch.setattr(sess, "CLICK_SETTLE_S", 0.01)
    s, _ = _session(tmp_path, ["done"])
    await s.start()
    async with s._lock:  # Nimbus is mid-check or still pointing when the person clicks
        s.on_click(10, 10)
        await asyncio.sleep(0.05)
        assert s.index == 0
    await asyncio.sleep(0.05)
    assert s.index == 1 and 0 in s.completed


async def test_a_late_click_for_a_finished_step_is_skipped(tmp_path, monkeypatch):
    import nimbus.shadow.session as sess

    monkeypatch.setattr(sess, "CLICK_SETTLE_S", 0.01)
    s, _ = _session(tmp_path, ["done"], n_steps=3)  # one verdict only: a second check would fail the pop
    await s.start()
    s.on_click(10, 10)
    await asyncio.sleep(0.05)
    assert s.index == 1
    assert await s.check(trigger="click", index=0) is None


async def test_typing_steps_are_checked_when_typing_pauses(tmp_path, monkeypatch):
    import nimbus.shadow.session as sess

    monkeypatch.setattr(sess, "TYPING_PAUSE_S", 0.02)
    s, _ = _session(tmp_path, ["done"])
    s.plan.steps[0].action = "type"
    await s.start()
    for _ in range(3):
        s.on_key(False)
        await asyncio.sleep(0.005)
    assert s.index == 0
    await asyncio.sleep(0.06)
    assert s.index == 1


async def test_missing_key_keeps_its_type_and_stops_the_chain():
    from nimbus.meta.client import MissingKeyError

    calls = []

    async def fn():
        raise MissingKeyError("MODEL_API_KEY is not set", status=401)

    async def call(model):
        calls.append(model)
        return await with_retries(fn, key=f"k-{model}", policy=FAST)

    with pytest.raises(MissingKeyError):
        await first_success(["a", "b"], call, what="x")
    assert calls == ["a"]


def test_unknown_action_is_coerced_not_fatal():
    data = {"title": "", "app": "", "summary": "", "start_state": "",
            "steps": [{"action": "tap", "instruction": "x"}, {"action": "wiggle", "instruction": "y"}]}
    plan = plan_from_model(data, kind="tutorial", source_video="", duration_s=10, model="m")
    assert [s.action for s in plan.steps] == ["click", "observe"]


async def test_focus_only_step_advances_on_click_but_not_on_probe(tmp_path):
    s, host = _session(tmp_path, ["uncertain", "uncertain"])
    s.plan.steps[0].expected_after = "a blinking text cursor appears in the field"
    await s.start()
    await s.check(trigger="probe")
    assert s.index == 0  # no action behind it: nothing is assumed
    await s.check(trigger="click")
    assert s.index == 1 and 0 in s.assumed and 0 not in s.completed


def test_expected_after_timestamp_prefix_is_stripped():
    data = {"title": "", "app": "", "summary": "", "start_state": "",
            "steps": [{"action": "click", "expected_after": "seconds = 3.14, the menu opens"}]}
    plan = plan_from_model(data, kind="tutorial", source_video="", duration_s=10, model="m")
    assert plan.steps[0].expected_after == "the menu opens"


def test_agent_final_message_decides_status():
    from nimbus.meta.computer import _final_status, _strip_status

    assert _final_status("DONE: opened the Color page") == "done"
    assert _final_status("INFEASIBLE: the app is not installed") == "infeasible"
    assert _final_status("I could not click Add to cart because the page shows a 404") == "infeasible"
    assert _strip_status("DONE: opened it") == "opened it"


def test_bug_time_clamped_to_video():
    data = {"title": "", "app": "", "summary": "", "start_state": "", "steps": [], "url": "", "expected": "",
            "actual": "", "bug_time": 28.0}
    plan = plan_from_model(data, kind="bug", source_video="", duration_s=24.56, model="m")
    assert plan.bug_time == pytest.approx(24.36)
    data["bug_time"] = 0.24  # earlier than the steps that cause it
    data["steps"] = [{"action": "click", "t_start": 7, "t_end": 8}, {"action": "click", "t_start": 10, "t_end": 11}]
    plan = plan_from_model(data, kind="bug", source_video="", duration_s=24.56, model="m")
    assert plan.bug_time == pytest.approx(12.0)


def test_identifier_style_text_is_humanized():
    data = {"title": "", "app": "", "summary": "", "start_state": "",
            "steps": [{"action": "click", "target": "left_sidebar_top_new_chat", "expected_after": "composer_focused",
                       "instruction": "Click the message box."}]}
    step = plan_from_model(data, kind="tutorial", source_video="", duration_s=10, model="m").steps[0]
    assert step.target == "Left sidebar top new chat"
    assert step.expected_after == "Composer focused"
    assert step.instruction == "Click the message box."


async def test_click_into_a_field_before_typing_is_focus_only(tmp_path):
    s, host = _session(tmp_path, ["not_done"])
    s.plan.steps[0].target = "Message input field"
    s.plan.steps[0].expected_after = "The field is ready"  # no focus words at all
    s.plan.steps[1].action = "type"
    await s.start()
    await s.check(trigger="click")
    assert s.index == 1 and 0 in s.assumed


async def test_a_click_on_the_control_the_next_step_types_into_is_focus_only(tmp_path):
    s, host = _session(tmp_path, ["not_done"])
    s.plan.steps[0].target = "Reply to Chatbook"  # named by its placeholder: no field words
    s.plan.steps[0].expected_after = "Nothing much changes"
    s.plan.steps[1].action = "type"
    s.plan.steps[1].target = "Reply to Chatbook."
    await s.start()
    await s.check(trigger="click")
    assert s.index == 1 and 0 in s.assumed


class _ScriptedMeta:
    """A MetaClient whose `respond` plays back canned replies per model, recording every call."""

    def __new__(cls, script):
        from nimbus.meta.client import MetaClient, Reply

        client = MetaClient.__new__(MetaClient)
        client.calls = []

        async def respond(model, input, **kwargs):
            client.calls.append((model, input))
            return Reply(text=script[model].pop(0), model=model, response_id=None, raw=None)

        client.respond = respond
        client.open_weights_for = lambda role: False
        return client


async def test_unparseable_json_is_re_requested_with_the_full_input():
    client = _ScriptedMeta({"m1": ["{broken", '{"steps": [1]}']})
    original = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "video here"}]}]
    data, reply = await client.respond_json(["m1"], original, schema={}, schema_name="s", what="t")
    assert data == {"steps": [1]}
    assert client.calls[1][1] is original  # the retry carried the original input, video and all


async def test_an_unusable_answer_moves_to_the_next_model():
    client = _ScriptedMeta({"m1": ['{"steps": []}'], "m2": ['{"steps": [{"a": 1}]}']})
    data, reply = await client.respond_json(["m1", "m2"], [], schema={}, schema_name="s", what="t",
                                            validate=lambda d: bool(d.get("steps")))
    assert reply.model == "m2" and data["steps"]


def test_microphone_start_closes_a_stream_left_open(monkeypatch):
    """A second start (release lost behind the first-run permission prompt) must close the first
    stream rather than drop it while PortAudio still calls its callback."""
    import sys
    import types

    from nimbus.mac.io import Microphone

    events = []

    class FakeStream:
        def __init__(self, **kw):
            self.id = len([e for e in events if e[0] == "open"])
            events.append(("open", self.id))

        def start(self):
            events.append(("start", self.id))

        def abort(self):
            events.append(("abort", self.id))

        def close(self):
            events.append(("close", self.id))

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(RawInputStream=FakeStream))
    mic = Microphone()
    mic.stop()
    mic.start(lambda c: None)
    mic.start(lambda c: None)
    mic.stop()
    assert events == [("open", 0), ("start", 0), ("abort", 0), ("close", 0), ("open", 1), ("start", 1),
                      ("abort", 1), ("close", 1)]


def test_only_screen_steering_warnings_go_back_to_the_person():
    from nimbus.app import _is_injection_warning

    routine = {"code": "computer_action_requires_confirmation",
               "message": "Review and confirm before executing this computer action."}
    steering = {"code": "malicious_instructions", "message": "The page contains instructions to the agent."}
    assert not _is_injection_warning(routine)
    assert _is_injection_warning(steering)


async def test_agent_resumes_when_meta_refuses_a_stored_step():
    """Reproduces '[400] This native computer input is too large to store for a later
    previous_response_id continuation': the agent restarts from the current screen and finishes."""
    from types import SimpleNamespace

    from nimbus.meta import computer
    from nimbus.meta.resilience import MetaAPIError

    def call(cid, action):
        return SimpleNamespace(type="computer_call", call_id=cid, pending_safety_checks=[], actions=[action])

    replies = iter([
        SimpleNamespace(id="r1", output=[call("c1", {"type": "click", "x": 5, "y": 5})], output_text=""),
        MetaAPIError("responses:muse-spark-1.3: [400] This native computer input is too large to store for a "
                     "later `previous_response_id` continuation.", status=400),
        SimpleNamespace(id="r2", output=[], output_text="DONE: searched and read the first result."),
    ])
    sent = []

    class Client:
        settings = SimpleNamespace(models=SimpleNamespace(computer_use=["muse-spark-1.3"]))

        async def respond(self, model, input, **kw):
            sent.append((input, kw.get("previous_response_id")))
            r = next(replies)
            if isinstance(r, Exception):
                raise r
            return SimpleNamespace(model=model, raw=r)

    class Driver:
        async def screenshot(self):
            return b"\xff\xd8jpeg"

        async def execute(self, action):
            pass

    result = await computer.run_agent(Client(), Driver(), "Search ChatGPT", approve=None)
    assert result.status == "done" and len(result.actions) == 1
    resumed_input, prev = sent[-1]
    assert prev is None and "Actions taken so far: click" in resumed_input[0]["content"][0]["text"]


async def test_agent_survives_a_model_that_keeps_failing_mid_task():
    """Seen live mid-drawing: Spark 1.3 answered 500 to every retry of one step, and the whole task died.
    The step is resumed from the current screen, on the next model in the chain first."""
    from types import SimpleNamespace

    from nimbus.meta import computer
    from nimbus.meta.resilience import MetaAPIError

    call = SimpleNamespace(type="computer_call", call_id="c1", pending_safety_checks=[],
                           actions=[{"type": "click", "x": 5, "y": 5}])
    sent = []

    class Client:
        settings = SimpleNamespace(models=SimpleNamespace(computer_use=["muse-spark-1.3", "muse-wonder-1.0"]))

        async def respond(self, model, input, **kw):
            sent.append((model, kw.get("previous_response_id")))
            if len(sent) == 1:
                return SimpleNamespace(model=model, raw=SimpleNamespace(id="r1", output=[call], output_text=""))
            if model == "muse-spark-1.3":
                raise MetaAPIError("responses:muse-spark-1.3: [500] internal server error", status=500)
            return SimpleNamespace(model=model, raw=SimpleNamespace(id="r2", output=[], output_text="DONE: drawn."))

    class Driver:
        async def screenshot(self):
            return b"\xff\xd8jpeg"

        async def execute(self, action):
            pass

    result = await computer.run_agent(Client(), Driver(), "Draw it", approve=None)
    assert result.status == "done"
    assert sent[1] == ("muse-spark-1.3", "r1") and sent[2] == ("muse-wonder-1.0", None)


def test_agent_screenshots_are_compact_and_keep_coordinates():
    import io

    from PIL import Image

    from nimbus.mac.control import compact
    from nimbus.mac.screen import Capture, Display

    d = Display(0, 1, 0.0, 0.0, 1440.0, 900.0, 2.0, True)
    buf = io.BytesIO()
    Image.new("RGB", (2880, 1800), (200, 120, 40)).save(buf, "PNG")
    small = compact(Capture(d, buf.getvalue(), 2880, 1800, 0.0))
    assert (small.pixel_width, small.pixel_height) == (1440, 900)
    assert small.png[:2] == b"\xff\xd8"
    assert small.pixel_to_global(720, 450) == (720.0, 450.0)


async def test_target_crop_is_the_control_not_the_cursor_glyph_on_it():
    from nimbus.engine.video import VideoAnalyzer

    cursor = (500.0, 300.0, 522.0, 330.0)
    seg = SegmentResult(1440, 900, {
        "cursor": Track("cursor", {0: Box(498, 299, 523, 331)}),
        "item": Track("item", {0: Box(420, 290, 620, 320)}),  # the menu item under the cursor
        "menu": Track("menu", {0: Box(400, 200, 640, 400)}),
    })

    class FakeSam:
        async def segment_image_at(self, frame, size, point):
            return seg

    analyzer = VideoAnalyzer.__new__(VideoAnalyzer)
    analyzer.sam = FakeSam()
    assert await analyzer._control_box(b"", (503, 303), (1440, 900), cursor) == (420, 290, 620, 320)
    assert await analyzer._control_box(b"", (503, 303), (1440, 900)) == (498, 299, 523, 331)  # no track: smallest


def test_cursor_path_reports_the_glyph_box_in_full_resolution():
    path = CursorPath(Track("c", {8: Box(100, 50, 110, 64)}), fps=4, sx=2, sy=2)
    assert path.box(2.0) == (200, 100, 220, 128)


def test_review_quotes_the_text_to_type():
    from nimbus.engine.video import apply_review

    plan = StepPlan(title="t", app="a", summary="", start_state="", steps=[
        PlanStep(index=0, action="type", target="Reply", instruction="x", expected_after="", t_start=0, t_end=1)])
    apply_review(plan, {"steps": [{"index": 0, "keep": True, "target": "Reply to Chatbook", "text": "Hi there",
                                   "instruction": "Type Hi there in Reply to Chatbook", "expected_after": "shown"}],
                        "title": "Say hi", "start_state": "Chatbook is open."})
    assert plan.steps[0].instruction == 'Type "Hi there" in Reply to Chatbook'
    assert (plan.title, plan.start_state) == ("Say hi", "Chatbook is open")


def test_dwell_starts_before_the_click_not_at_the_rest_after_it():
    # Move in (frames 0-3), rest on the control through the click (4-15), move to the next one (16+).
    xs = {f: (40 * f if f <= 4 else 160 if f <= 15 else 160 + 40 * (f - 15)) for f in range(20)}
    path = CursorPath(Track("c", {f: Box(xs[f], 100, xs[f] + 12, 118) for f in xs}), fps=4.0, sx=1, sy=1)
    arrive, leave = path.dwell(0.5, 4.5)
    assert arrive == 1.0 and leave == 3.75


def test_thumbnail_crop_keeps_a_short_label_readable_and_inside_the_frame():
    from nimbus import media

    buf = io.BytesIO()
    Image.new("RGB", (1440, 900), "white").save(buf, "PNG")
    assert Image.open(io.BytesIO(media.crop_png(buf.getvalue(), (1061, 829, 1113, 859), pad=14,
                                                min_size=(240, 84)))).size == (240, 84)
    corner = media.crop_png(buf.getvalue(), (1420, 880, 1436, 896), pad=14, min_size=(240, 84))
    assert Image.open(io.BytesIO(corner)).size == (240, 84)  # slid back in, not cut at the edge


def test_a_refined_point_outside_the_coarse_box_is_not_trusted():
    from nimbus.engine.grounding import _near

    box = (19, 64, 111, 87)
    assert _near(box, 64, 76) and _near(box, 64, 60)
    assert not _near(box, 64, 45)  # 25 px above a 23 px tall button: the heading above it


def test_ask_refine_cannot_move_a_point_off_the_first_pass_box():
    """On a soft GitHub screenshot the zoom pass moved a correctly boxed Issues-tab point 21 pt up."""
    from nimbus.ask import ShowStep, Walkthrough
    from nimbus.engine import grounding

    class Client:
        settings = type("S", (), {"models": type("M", (), {"grounding": ["m"]})()})()

        async def respond_json(self, *args, **kwargs):
            return {"found": True, "x": 500, "y": 434, "x_min": 470, "y_min": 420, "x_max": 530, "y_max": 450}, None

    service = AskService.__new__(AskService)
    service.grounder = grounding.Grounder(Client())
    buf = io.BytesIO()
    Image.new("RGB", (3200, 1800), "white").save(buf, "PNG")
    cap = Capture(Display(0, 1, 0.0, 0.0, 1600.0, 900.0, 2.0, True), buf.getvalue(), 3200, 1800, time.time())
    step = ShowStep(say="", kind="point", target="Issues tab")
    step.gx, step.gy = cap.norm_to_global(95, 160)
    step.box = (*cap.norm_to_global(67, 150), *cap.norm_to_global(128, 170))
    walk = Walkthrough(question="q", reply="r", model="m", intent="explain", operate_task="", show_steps=False)
    walk.steps.append(step)
    asyncio.run(service.refine(walk, {"steps": [{"target_description": "Issues tab"}]}, [cap]))
    assert (round(step.gx), round(step.gy)) == (152, 144)  # the first pass's point, kept


def test_a_bug_plan_must_describe_the_bug_not_the_recording():
    from nimbus.engine.steps import _untimed, describes_the_bug

    step = [{"action": "click"}]
    assert describes_the_bug({"steps": step, "actual": "Alex is still owed $0.01 after Settle up"})
    # Seen live: Muse Spark described the video instead of the wrong behaviour.
    assert not describes_the_bug({"steps": step, "actual": "32.3s video - Lisbon trip expense splitter, address bar visible"})
    assert not describes_the_bug({"steps": step, "actual": ""})
    assert not describes_the_bug({"steps": step, "actual": "The recording shows the cart and a total"})
    assert describes_the_bug({"steps": step, "actual": "The video does not play after pressing Play"})
    assert _untimed("00:23-00:32.3 Balances panel shows Alex is owed $0.01") == "Balances panel shows Alex is owed $0.01"
    assert _untimed("Total is $79.00, not $158.00") == "Total is $79.00, not $158.00"


def test_transcriber_stutters_are_said_once():
    from nimbus.meta.voice import tidy

    # Seen live from Muse Voice Transcribe.
    assert tidy("Is this project still actively maintained? I want want to use it in production.") == \
        "Is this project still actively maintained? I want to use it in production."
    assert tidy("can you walk me walk me through finding the eigenvalues") == "can you walk me through finding the eigenvalues"
    assert tidy("Where is the is the history button") == "Where is the history button"
    assert tidy("I I want it") == "I want it"
    assert tidy("He had had enough, and that that was it") == "He had had enough, and that that was it"
    assert tidy("Click Settings in the menu") == "Click Settings in the menu"


def _page_png(lines: list[tuple[int, int]], size=(3200, 1800)) -> bytes:
    """A white page with lines of "text" (black and white stripes) at the given pixel rows (top, bottom)."""
    img = Image.new("RGB", size, "white")
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    for top, bottom in lines:
        for x in range(400, 2600, 14):
            d.rectangle((x, top, x + 6, bottom), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_a_note_card_goes_below_the_paragraph_its_anchor_sits_in():
    """Seen live: a note anchored mid-footnote covered the footnote's formula, the evidence it explained."""
    from nimbus.ask import block_end

    # Three lines of a paragraph (12 px gaps between lines at 2x), then a blank band.
    png = _page_png([(1000, 1030), (1042, 1072), (1084, 1114), (1300, 1330)])
    cap = Capture(Display(0, 1, 0.0, 0.0, 1600.0, 900.0, 2.0, True), png, 3200, 1800, time.time())
    end = block_end(cap, 800, 507)  # anchored on the first line
    assert end is not None and 557 <= end <= 560  # just under the third line, not between lines
    assert block_end(cap, 800, 880) is None or block_end(cap, 800, 880) >= 880  # nothing busy below: never above


def test_ask_sharpens_a_small_framed_area_but_not_a_whole_paragraph():
    """The first pass framed the Transformer paper's equation 45 pt too high; the zoomed look found it."""
    from nimbus.ask import ShowStep, Walkthrough
    from nimbus.engine import grounding

    calls = []

    class Client:
        settings = type("S", (), {"models": type("M", (), {"grounding": ["m"]})()})()

        async def respond_json(self, *args, **kwargs):
            calls.append(1)
            # Crop coordinates: the equation sits lower in the crop than the coarse box said.
            return {"found": True, "x": 500, "y": 600, "x_min": 300, "y_min": 520, "x_max": 700, "y_max": 680}, None

    service = AskService.__new__(AskService)
    service.grounder = grounding.Grounder(Client())
    buf = io.BytesIO()
    Image.new("RGB", (3200, 1800), "white").save(buf, "PNG")
    cap = Capture(Display(0, 1, 0.0, 0.0, 1600.0, 900.0, 2.0, True), buf.getvalue(), 3200, 1800, time.time())
    eq = ShowStep(say="", kind="box", target="Equation 1")
    eq.box = (640.0, 240.0, 960.0, 290.0)
    eq.gx, eq.gy = 800.0, 265.0
    para = ShowStep(say="", kind="box", target="the paragraph")
    para.box = (380.0, 500.0, 1200.0, 620.0)
    para.gx, para.gy = 790.0, 560.0
    walk = Walkthrough(question="q", reply="r", model="m", intent="explain", operate_task="", show_steps=False)
    walk.steps += [eq, para]
    asyncio.run(service.refine(walk, {"steps": [{}, {}]}, [cap]))
    assert len(calls) == 1  # the paragraph is too big to zoom on: left as framed
    assert eq.refined and eq.box[1] > 240 and eq.gy > 265  # moved down onto the equation
    assert para.box == (380.0, 500.0, 1200.0, 620.0)


def test_working_a_problem_out_gets_more_thought_than_pointing():
    from nimbus.ask import effort_for
    from nimbus.config import Settings

    s = Settings()
    assert effort_for("Can you finish this alignment table and give me the score?", s) == s.solve_effort
    assert effort_for("Walk me through finding the eigenvalues step by step", s) == s.solve_effort
    assert effort_for("Where do I turn on dark mode?", s) == s.ask_effort
    assert effort_for("Why can't I type in this search box?", s) == s.ask_effort


async def test_shadow_skips_the_starting_point_when_the_first_control_is_on_screen(tmp_path):
    s, host = _session(tmp_path, [])
    s.plan.start_state = "The app is open on its home screen"
    await s.start()
    await asyncio.sleep(0)
    said = [e[1] for e in host.events if e[0] == "say"]
    assert said == ["Demo, in 2 steps. Click button 0"]

    class Nowhere:
        async def locate(self, *a, **k):
            return None

    s, host = _session(tmp_path / "b", [])
    s.plan.start_state = "The app is open on its home screen"
    s.grounder = Nowhere()
    await s.start()
    await asyncio.sleep(0)
    assert [e[1] for e in host.events if e[0] == "say"] == [
        "Demo, in 2 steps. First, make sure the app is open on its home screen. Click button 0"]


def test_nimbus_never_shows_an_em_dash():
    from nimbus.ask import _plain

    assert _plain("2 — diag 1+1=2 beats −2, −3") == "2 - diag 1+1=2 beats −2, −3"
    assert _plain("AC–TCA") == "AC–TCA"  # an en dash (the alignment gap) stays
