"""Tests for the idle chatter.

Two things here are worth more than the rest, because they are what
makes the feature acceptable rather than merely working: the worker must
never make a real tool call wait, and it must never raise into a thread
nobody is watching. Both are exercised directly.

No hardware and no network. The device is a stub, the clock is a
variable, and the line source is a list.
"""

import json
import random
import socket
import threading
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

import buddy_chatter
from buddy_agent import CLAUDE_CODE, CODEX, AgentIdentity
from buddy_chatter import (
    DEFAULT_PROMPT_PATH,
    ChatterConfig,
    ChatterService,
    CodexLineSource,
    Event,
    RoutingLineSource,
    VertexLineSource,
    describe,
    line_source_for,
    parse_event,
)
from buddy_wire import Message


class StubLink:
    """A device that answers everything, and records what it was told."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.said: list[str] = []
        self.spoke: list[str] = []
        self.fail_with: Exception | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        if self.fail_with is not None:
            raise self.fail_with
        if expect == "chat.say":
            self.said.append(str(obj["text"]))
            return {"ack": "chat.say", "ok": True}
        if expect == "speak.say":
            self.spoke.append(str(obj["text"]))
            # `bytes` and `rate` are what speak() turns into its
            # playback timeout; keep them small so nothing waits.
            return {"ack": "speak.say", "ok": True, "bytes": 32, "rate": 16000}
        return {"ack": expect, "ok": True}

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message:
        return {"ack": expect, "ok": True, "stalls": 0}


class ListSource:
    """Hands out canned lines and counts how many were taken."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines if lines is not None else [f"line {i} なのだ" for i in range(50)]
        self.calls = 0
        self.contexts: list[list[Event]] = []

    def next_line(self, context: Sequence[Event]) -> str | None:
        self.calls += 1
        self.contexts.append(list(context))
        return self.lines.pop(0) if self.lines else None


class FakeVertex:
    """The three attributes `VertexLineSource._fill` reaches through.

    Shaped like the SDK's reply rather than mocked at the call boundary,
    so the JSON-schema round trip and the text-block extraction are the
    things actually under test.
    """

    class _Block:
        def __init__(self, text: str) -> None:
            self.type = "text"
            self.text = text

    class _Reply:
        def __init__(self, text: str) -> None:
            self.content = [FakeVertex._Block(text)]

    def __init__(self, calls: list[dict[str, Any]], payload: dict[str, Any]) -> None:
        self._calls = calls
        self._payload = payload
        self.messages = self

    def create(self, **kwargs: object) -> "FakeVertex._Reply":
        self._calls.append(kwargs)
        return FakeVertex._Reply(json.dumps(self._payload, ensure_ascii=False))


class Clock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def build(
    link: StubLink | None,
    source: ListSource | None = None,
    lock: threading.Lock | None = None,
    real_clock: bool = False,
    identity: AgentIdentity | None = None,
    **overrides: Any,  # noqa: ANN401 — mirrors ChatterConfig's own field types
) -> tuple[ChatterService, Clock, ListSource]:
    """A service wired to stubs, with the jitter pinned to fixed values.

    `real_clock` is for the tests that run the actual threads, which
    cannot be driven by a clock the test has to advance by hand.
    """
    settings: dict[str, Any] = {
        # Equal bounds make uniform() deterministic without stubbing it,
        # so the pacing tests read as arithmetic.
        "gap_min": 30.0,
        "gap_max": 30.0,
        "idle_min": 100.0,
        "idle_max": 100.0,
        "engine": "http://192.0.2.1:50021",
    }
    settings.update(overrides)
    cfg = ChatterConfig(**settings)
    clock = Clock()
    src = source if source is not None else ListSource()
    service = ChatterService(
        cfg,
        lambda: link,
        lock if lock is not None else threading.Lock(),
        source=src,
        rng=random.Random(1),
        clock=time.monotonic if real_clock else clock,
        identity=identity,
    )
    return service, clock, src


class ParseEventTests(unittest.TestCase):
    def test_accepts_a_known_kind(self) -> None:
        ev = parse_event(b'{"kind": "tool", "detail": "Bash: uv run pytest"}')
        self.assertEqual(ev, Event("tool", "Bash: uv run pytest"))

    def test_detail_is_optional(self) -> None:
        self.assertEqual(parse_event(b'{"kind": "stop"}'), Event("stop", ""))

    def test_rejects_an_unknown_kind(self) -> None:
        self.assertIsNone(parse_event(b'{"kind": "shutdown"}'))

    def test_rejects_malformed_json(self) -> None:
        self.assertIsNone(parse_event(b"{not json"))
        self.assertIsNone(parse_event(b"\xff\xfe"))

    def test_rejects_a_non_object(self) -> None:
        self.assertIsNone(parse_event(b'["tool"]'))

    def test_detail_is_clamped_and_flattened(self) -> None:
        ev = parse_event(json.dumps({"kind": "tool", "detail": "a\n  b " + "x" * 500}).encode())
        assert ev is not None
        self.assertLessEqual(len(ev.detail), 120)
        self.assertTrue(ev.detail.startswith("a b "))

    def test_a_non_string_detail_is_dropped_not_fatal(self) -> None:
        ev = parse_event(b'{"kind": "tool", "detail": 42}')
        self.assertEqual(ev, Event("tool", ""))

    def test_the_sender_may_name_itself(self) -> None:
        ev = parse_event(b'{"kind": "stop", "agent": "codex"}')
        self.assertEqual(ev, Event("stop", "", "codex"))

    def test_an_unnamed_sender_is_normal(self) -> None:
        self.assertEqual(parse_event(b'{"kind": "stop"}').agent, "")  # type: ignore[union-attr]

    def test_a_junk_agent_is_dropped_and_clamped(self) -> None:
        self.assertEqual(parse_event(b'{"kind": "stop", "agent": 7}').agent, "")  # type: ignore[union-attr]
        ev = parse_event(json.dumps({"kind": "stop", "agent": "z" * 200}).encode())
        assert ev is not None
        self.assertLessEqual(len(ev.agent), 40)


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = ChatterConfig.from_env({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.gap_min, 40.0)
        self.assertEqual(cfg.voice_every, 1)
        self.assertTrue(str(cfg.socket_path).endswith("tmp/buddy-chatter.sock"))

    def test_disabled(self) -> None:
        self.assertFalse(ChatterConfig.from_env({"BUDDY_CHATTER": "0"}).enabled)
        self.assertFalse(ChatterConfig.from_env({"BUDDY_CHATTER": "false"}).enabled)

    def test_unparseable_values_fall_back(self) -> None:
        cfg = ChatterConfig.from_env({"BUDDY_CHATTER_GAP_MIN": "soon", "BUDDY_CHATTER_BATCH": ""})
        self.assertEqual(cfg.gap_min, 40.0)
        self.assertEqual(cfg.batch, 6)

    def test_voice_every_cannot_be_zero(self) -> None:
        # A zero would make the modulo in _transmit raise on every line.
        self.assertEqual(ChatterConfig.from_env({"BUDDY_CHATTER_VOICE_EVERY": "0"}).voice_every, 1)

    def test_vertex_settings_come_from_claude_codes_own(self) -> None:
        cfg = ChatterConfig.from_env(
            {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-1", "CLOUD_ML_REGION": "us-east5"}
        )
        self.assertEqual(cfg.project_id, "proj-1")
        self.assertEqual(cfg.region, "us-east5")

    def test_the_agent_defaults_to_claude_code(self) -> None:
        self.assertEqual(ChatterConfig.from_env({}).agent, CLAUDE_CODE)
        self.assertEqual(ChatterConfig.from_env({"BUDDY_CHATTER_AGENT": CODEX}).agent, CODEX)

    def test_codex_settings(self) -> None:
        cfg = ChatterConfig.from_env({})
        self.assertEqual(cfg.codex_bin, "codex")
        self.assertEqual(cfg.codex_model, "", "empty means whatever Codex is configured for")
        cfg = ChatterConfig.from_env(
            {
                "BUDDY_CHATTER_CODEX_BIN": "/opt/homebrew/bin/codex",
                "BUDDY_CHATTER_CODEX_MODEL": "gpt-5.6-sol",
                "BUDDY_CHATTER_CODEX_TIMEOUT": "45",
            }
        )
        self.assertEqual(cfg.codex_bin, "/opt/homebrew/bin/codex")
        self.assertEqual(cfg.codex_model, "gpt-5.6-sol")
        self.assertEqual(cfg.codex_timeout, 45.0)


class PacingTests(unittest.TestCase):
    def test_an_event_inside_the_gap_stays_quiet(self) -> None:
        service, clock, _ = build(StubLink())
        clock.advance(5.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 0)

    def test_an_event_after_the_gap_speaks(self) -> None:
        service, clock, _ = build(StubLink())
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 1)

    def test_silence_alone_speaks_once_idle_elapses(self) -> None:
        service, clock, _ = build(StubLink())
        clock.advance(31.0)
        service.step(None)
        self.assertEqual(service.spoken, 0, "the gap passed but the idle threshold did not")
        clock.advance(70.0)
        service.step(None)
        self.assertEqual(service.spoken, 1)

    def test_the_idle_event_reaches_the_prompt(self) -> None:
        service, clock, source = build(StubLink())
        clock.advance(101.0)
        service.step(None)
        self.assertEqual(source.contexts[-1][-1], Event("idle", ""))

    def test_speaking_rearms_both_timers(self) -> None:
        service, clock, _ = build(StubLink())
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        clock.advance(10.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 1, "the second event was inside the fresh gap")

    def test_the_gap_is_redrawn_and_varies(self) -> None:
        service, clock, _ = build(StubLink(), gap_min=10.0, gap_max=200.0)
        seen: set[float] = set()
        for _ in range(8):
            clock.advance(400.0)
            service.step(Event("tool", "Read"))
            seen.add(service.status()["next_gap_s"])
        self.assertGreater(len(seen), 1, "a fixed interval is the thing this must not be")
        self.assertTrue(all(10.0 <= gap <= 200.0 for gap in seen))

    def test_an_inverted_range_does_not_produce_a_negative_gap(self) -> None:
        service, clock, _ = build(StubLink(), gap_min=90.0, gap_max=10.0)
        clock.advance(1000.0)
        service.step(Event("tool", "Read"))
        self.assertGreaterEqual(service.status()["next_gap_s"], 90.0)


class DeviceTests(unittest.TestCase):
    def test_the_line_reaches_the_panel_and_the_speaker(self) -> None:
        link = StubLink()
        service, clock, _ = build(link)
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(link.said, ["line 0 なのだ"])
        self.assertEqual(link.spoke, ["line 0 なのだ"])

    def test_voice_every_thins_out_the_audio_but_not_the_panel(self) -> None:
        link = StubLink()
        service, clock, _ = build(link, voice_every=3)
        for _ in range(6):
            clock.advance(31.0)
            service.step(Event("tool", "Read"))
        self.assertEqual(len(link.said), 6)
        self.assertEqual(len(link.spoke), 2)

    def test_a_disconnected_device_is_not_spoken_to(self) -> None:
        service, clock, source = build(StubLink(connected=False))
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 0)
        self.assertEqual(service.skipped_offline, 1)
        self.assertEqual(source.calls, 0, "no line should be generated for a device that is gone")

    def test_no_link_at_all_is_not_an_error(self) -> None:
        service, clock, _ = build(None)
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.skipped_offline, 1)
        self.assertEqual(service.last_error, "")

    def test_a_device_failure_is_recorded_and_survived(self) -> None:
        link = StubLink()
        link.fail_with = TimeoutError("no chat.say ack within 5.0s")
        service, clock, _ = build(link)
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 0)
        self.assertIn("TimeoutError", service.last_error)

    def test_an_exhausted_source_does_not_wedge_the_worker(self) -> None:
        service, clock, _ = build(StubLink(), source=ListSource([]))
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 0)


class LockTests(unittest.TestCase):
    """The chatter must never be the reason a tool call waits."""

    def test_a_busy_device_is_skipped_rather_than_waited_for(self) -> None:
        lock = threading.Lock()
        link = StubLink()
        service, clock, _ = build(link, lock=lock)
        lock.acquire()
        try:
            clock.advance(31.0)
            started = time.monotonic()
            service.step(Event("tool", "Read"))
            elapsed = time.monotonic() - started
        finally:
            lock.release()
        self.assertEqual(service.skipped_busy, 1)
        self.assertEqual(link.said, [])
        self.assertLess(elapsed, 0.5, "step() blocked on the device lock")

    def test_the_paid_for_line_is_kept_and_used_on_the_next_turn(self) -> None:
        lock = threading.Lock()
        link = StubLink()
        service, clock, source = build(link, lock=lock)
        lock.acquire()
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        lock.release()

        service.step(Event("tool", "Read"))
        self.assertEqual(link.said, ["line 0 なのだ"])
        self.assertEqual(source.calls, 1, "the line was generated twice")

    def test_the_lock_is_released_even_when_the_device_throws(self) -> None:
        lock = threading.Lock()
        link = StubLink()
        link.fail_with = RuntimeError("device refused speak.say")
        service, clock, _ = build(link, lock=lock)
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertTrue(lock.acquire(blocking=False), "the device lock was leaked")
        lock.release()

    def test_generation_happens_outside_the_lock(self) -> None:
        """A slow model must not turn into a slow tool call."""
        lock = threading.Lock()
        held_during_generation: list[bool] = []

        class WatchingSource(ListSource):
            def next_line(self, context: Sequence[Event]) -> str | None:
                held_during_generation.append(not lock.acquire(blocking=False))
                if not held_during_generation[-1]:
                    lock.release()
                return super().next_line(context)

        service, clock, _ = build(StubLink(), source=WatchingSource(), lock=lock)
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(held_during_generation, [False])


class SocketTests(unittest.TestCase):
    """The hook side of the contract, end to end over a real socket."""

    def test_a_datagram_becomes_an_utterance(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "chatter.sock"
            link = StubLink()
            service, _, _ = build(
                link,
                lock=threading.Lock(),
                socket_path=path,
                real_clock=True,
                # The gap is zero so the datagram is acted on at once;
                # the idle threshold is an hour so nothing else can be
                # what spoke.
                gap_min=0.0,
                gap_max=0.0,
                idle_min=3600.0,
                idle_max=3600.0,
            )
            service.start()
            try:
                self.assertTrue(path.exists())
                sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                sender.sendto(b'{"kind": "tool", "detail": "Bash"}', str(path))
                sender.close()
                deadline = time.monotonic() + 5.0
                while not link.said and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                service.stop()
            self.assertEqual(link.said, ["line 0 なのだ"])
            self.assertFalse(path.exists(), "stop() left the socket behind")

    def test_sending_to_nobody_is_the_senders_problem_not_a_crash(self) -> None:
        """What the hook relies on: an absent server costs it nothing."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent.sock"
            sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            with self.assertRaises(OSError):
                sender.sendto(b'{"kind": "stop"}', str(path))
            sender.close()

    def test_a_stale_socket_file_does_not_block_a_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "chatter.sock"
            path.write_bytes(b"")
            service, _, _ = build(StubLink(), socket_path=path)
            service.start()
            try:
                self.assertTrue(service.running)
            finally:
                service.stop()

    def test_start_is_a_no_op_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "chatter.sock"
            service, _, _ = build(StubLink(), socket_path=path, enabled=False)
            service.start()
            self.assertFalse(service.running)
            self.assertFalse(path.exists())


class PromptFileTests(unittest.TestCase):
    """The persona is prose in a file, so editing it must not need Python."""

    def test_the_prompt_is_read_from_the_configured_path(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "persona.md"
            path.write_text("きみは猫である。\n", encoding="utf-8")
            source = VertexLineSource(ChatterConfig(prompt_path=path))
            calls: list[dict[str, Any]] = []
            source._ensure_client = lambda: FakeVertex(  # type: ignore[method-assign]
                calls, {"lines": ["にゃあ"]}
            )
            source.next_line([])
            self.assertEqual(calls[0]["system"], "きみは猫である。\n")

    def test_the_shipped_prompt_exists(self) -> None:
        # The default is package data; a rename that misses it would only
        # show up as every session falling back to canned lines.
        self.assertTrue(DEFAULT_PROMPT_PATH.is_file(), DEFAULT_PROMPT_PATH)
        self.assertTrue(DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip())

    def test_a_missing_prompt_is_a_counted_failure_not_a_crash(self) -> None:
        source = VertexLineSource(ChatterConfig(prompt_path=Path("/nope/persona.md")))
        source._ensure_client = lambda: FakeVertex([], {"lines": ["x"]})  # type: ignore[method-assign]
        line = source.next_line([])
        assert line is not None
        self.assertTrue(line, "it should still say something")
        self.assertEqual(source.failures, 1)
        self.assertIn("persona.md", source.last_error)


class LineSourceTests(unittest.TestCase):
    def test_a_generation_failure_falls_back_rather_than_going_silent(self) -> None:
        source = VertexLineSource(ChatterConfig(project_id="nope", region="nowhere"))
        line = source.next_line([Event("tool", "Read")])
        assert line is not None
        self.assertTrue(line)
        self.assertEqual(source.failures, 1)
        self.assertTrue(source.last_error)

    def test_a_batch_is_parsed_and_handed_out_one_line_at_a_time(self) -> None:
        source = VertexLineSource(ChatterConfig())
        calls: list[dict[str, Any]] = []
        source._ensure_client = lambda: FakeVertex(  # type: ignore[method-assign]
            calls, {"lines": ["いち なのだ", "に なのだ"]}
        )
        self.assertEqual(source.next_line([Event("tool", "Read")]), "いち なのだ")
        self.assertEqual(source.next_line([]), "に なのだ")
        self.assertEqual(len(calls), 1, "the second line should come from the same batch")
        self.assertEqual(source.generated, 2)

    def test_overlong_and_multiline_output_is_cut_to_one_panel(self) -> None:
        cfg = ChatterConfig(max_chars=10)
        source = VertexLineSource(cfg)
        source._ensure_client = lambda: FakeVertex(  # type: ignore[method-assign]
            [], {"lines": ["あ" * 40, "改行\nを\n含む", "", 12345]}
        )
        self.assertEqual(source.next_line([]), "あ" * 10)
        self.assertEqual(source.next_line([]), "改行 を 含む")
        self.assertEqual(source.next_line([]), "12345", "a non-string must not raise")

    def test_describe_renders_events_for_the_prompt(self) -> None:
        self.assertEqual(describe([]), "まだ何も起きていない。")
        rendered = describe([Event("tool", "Bash"), Event("idle")])
        self.assertEqual(rendered, "- tool: Bash\n- idle")


class FakeCodex:
    """Stands in for `subprocess.run` launching the Codex CLI.

    Writes to whatever path the argv named, which is the part worth
    pinning: the answer comes back through a file the caller chose, so a
    flag that stops being passed shows up as an empty read rather than
    as a crash.
    """

    def __init__(self, payload: dict[str, Any] | None, returncode: int = 0) -> None:
        self.payload = payload
        self.returncode = returncode
        self.argv: list[str] = []
        self.stdin = ""
        self.kwargs: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401 — subprocess's own
        self.argv = argv
        self.stdin = str(kwargs.get("input", ""))
        self.kwargs = kwargs
        if self.payload is not None:
            target = Path(argv[argv.index("--output-last-message") + 1])
            target.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="not logged in")


class CodexLineSourceTests(unittest.TestCase):
    """Codex's backend. Never actually runs the CLI."""

    def test_a_batch_comes_back_through_the_injected_runner(self) -> None:
        source = CodexLineSource(
            ChatterConfig(), run=lambda _: json.dumps({"lines": ["いち なのだ", "に なのだ"]})
        )
        self.assertEqual(source.next_line([]), "いち なのだ")
        self.assertEqual(source.next_line([]), "に なのだ")
        self.assertEqual(source.generated, 2)

    def test_the_persona_and_the_events_both_reach_the_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "persona.md"
            path.write_text("きみは猫である。\n", encoding="utf-8")
            seen: list[str] = []

            def run(prompt: str) -> str:
                seen.append(prompt)
                return json.dumps({"lines": ["にゃあ"]})

            source = CodexLineSource(ChatterConfig(prompt_path=path), run=run)
            source.next_line([Event("tool", "Bash")])
        # One blob, persona first: `codex exec` has no system slot.
        self.assertIn("きみは猫である。", seen[0])
        self.assertIn("- tool: Bash", seen[0])
        self.assertLess(seen[0].index("きみは猫である。"), seen[0].index("- tool: Bash"))

    def test_a_broken_runner_falls_back_rather_than_going_silent(self) -> None:
        def boom(_: str) -> str:
            raise FileNotFoundError("codex: command not found")

        source = CodexLineSource(ChatterConfig(), run=boom)
        line = source.next_line([])
        assert line is not None
        self.assertTrue(line)
        self.assertEqual(source.failures, 1)
        self.assertIn("command not found", source.last_error)

    def test_the_cli_is_invoked_read_only_and_ephemeral(self) -> None:
        fake = FakeCodex({"lines": ["うむ なのだ"]})
        source = CodexLineSource(ChatterConfig(codex_bin="codex-x", codex_model="gpt-5.6-sol"))
        with mock.patch.object(buddy_chatter.subprocess, "run", fake):
            self.assertEqual(source.next_line([Event("stop")]), "うむ なのだ")
        self.assertEqual(fake.argv[0], "codex-x")
        self.assertEqual(fake.argv[1], "exec")
        # A turn that could write files, keep a session or pick up a
        # project's AGENTS.md would be a side effect of muttering.
        self.assertIn("--ephemeral", fake.argv)
        self.assertIn("--skip-git-repo-check", fake.argv)
        self.assertEqual(fake.argv[fake.argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(fake.argv[fake.argv.index("--model") + 1], "gpt-5.6-sol")
        self.assertEqual(fake.argv[-1], "-", "the prompt goes on stdin, not the argument list")
        self.assertIn("独り言", fake.stdin)
        self.assertEqual(fake.kwargs["timeout"], 180.0)

    def test_no_model_flag_when_codex_should_use_its_own_default(self) -> None:
        fake = FakeCodex({"lines": ["ほい"]})
        with mock.patch.object(buddy_chatter.subprocess, "run", fake):
            CodexLineSource(ChatterConfig()).next_line([])
        self.assertNotIn("--model", fake.argv)

    def test_a_turn_that_writes_nothing_is_a_counted_failure(self) -> None:
        fake = FakeCodex(None, returncode=1)
        source = CodexLineSource(ChatterConfig())
        with mock.patch.object(buddy_chatter.subprocess, "run", fake):
            line = source.next_line([])
        assert line is not None
        self.assertTrue(line, "it should still say something")
        self.assertEqual(source.failures, 1)
        # The reason has to survive into the status field, or a chatter
        # that has quietly fallen back to canned lines looks like a
        # chatter that is simply not saying much.
        self.assertIn("not logged in", source.last_error)

    def test_the_scratch_directory_does_not_outlive_the_turn(self) -> None:
        fake = FakeCodex({"lines": ["ほい"]})
        with mock.patch.object(buddy_chatter.subprocess, "run", fake):
            CodexLineSource(ChatterConfig()).next_line([])
        self.assertFalse(Path(fake.kwargs["cwd"]).exists())


class RoutingTests(unittest.TestCase):
    """The pairing of agent to backend, and when it is decided."""

    def test_the_pairing_is_fixed(self) -> None:
        self.assertIsInstance(line_source_for(CLAUDE_CODE, ChatterConfig()), VertexLineSource)
        self.assertIsInstance(line_source_for(CODEX, ChatterConfig()), CodexLineSource)

    def test_an_unknown_agent_gets_the_default_backend(self) -> None:
        self.assertIsInstance(line_source_for("cursor", ChatterConfig()), VertexLineSource)

    def test_nothing_is_built_until_a_line_is_wanted(self) -> None:
        # Building a backend touches credentials — the Vertex client
        # resolves ADC, the Codex one needs an install. A server that
        # never speaks must never look for either.
        built: list[str] = []
        routing = RoutingLineSource(
            ChatterConfig(),
            AgentIdentity(),
            factory=lambda agent, _cfg, _rng: built.append(agent) or ListSource(),  # type: ignore[func-returns-value]
        )
        self.assertEqual(built, [])
        routing.next_line([])
        self.assertEqual(built, [CLAUDE_CODE])

    def test_a_backend_is_built_once_and_kept(self) -> None:
        built: list[str] = []
        routing = RoutingLineSource(
            ChatterConfig(),
            AgentIdentity(),
            factory=lambda agent, _cfg, _rng: built.append(agent) or ListSource(),  # type: ignore[func-returns-value]
        )
        routing.next_line([])
        routing.next_line([])
        self.assertEqual(built, [CLAUDE_CODE])

    def test_the_identity_changing_switches_backend(self) -> None:
        identity = AgentIdentity()
        sources = {CLAUDE_CODE: ListSource(["vertex なのだ"]), CODEX: ListSource(["codex なのだ"])}
        routing = RoutingLineSource(
            ChatterConfig(), identity, factory=lambda agent, _cfg, _rng: sources[agent]
        )
        self.assertEqual(routing.next_line([]), "vertex なのだ")
        identity.observe("codex-mcp-client")
        self.assertEqual(routing.next_line([]), "codex なのだ")

    def test_the_backend_is_named_before_anything_is_built(self) -> None:
        identity = AgentIdentity(default=CODEX)
        routing = RoutingLineSource(ChatterConfig(), identity)
        self.assertEqual(routing.backend, "codex")
        self.assertEqual(routing.agent, CODEX)

    def test_counters_are_summed_across_backends(self) -> None:
        identity = AgentIdentity()
        routing = RoutingLineSource(
            ChatterConfig(),
            identity,
            factory=lambda _agent, cfg, rng: CodexLineSource(
                cfg, rng, run=lambda _: json.dumps({"lines": ["ほい"]})
            ),
        )
        routing.next_line([])
        identity.observe("codex")
        routing.next_line([])
        self.assertEqual(routing.generated, 2)
        self.assertEqual(routing.failures, 0)


class AgentWitnessTests(unittest.TestCase):
    """How the running service learns who it is working for."""

    def test_an_event_that_names_its_sender_switches_the_backend(self) -> None:
        service, clock, _ = build(StubLink())
        clock.advance(31.0)
        service.step(Event("tool", "Read", "codex"))
        self.assertEqual(service.identity.current, CODEX)
        self.assertEqual(service.status()["agent"], CODEX)

    def test_an_event_inside_the_gap_still_counts_as_a_witness(self) -> None:
        # It says nothing worth speaking to, but it does say who is at
        # the keyboard, and that is worth knowing before the next line.
        service, _, _ = build(StubLink())
        service.step(Event("tool", "Read", "codex"))
        self.assertEqual(service.spoken, 0)
        self.assertEqual(service.identity.current, CODEX)

    def test_an_unsigned_event_leaves_the_identity_alone(self) -> None:
        service, clock, _ = build(StubLink())
        service.identity.observe("codex")
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.identity.current, CODEX)

    def test_the_server_can_hand_in_the_identity_it_learned_at_the_handshake(self) -> None:
        identity = AgentIdentity()
        identity.observe("codex-mcp-client")
        service, _, _ = build(StubLink(), identity=identity)
        self.assertIs(service.identity, identity)
        self.assertEqual(service.status()["agent"], CODEX)

    def test_the_configured_default_applies_with_no_witness(self) -> None:
        service, _, _ = build(StubLink(), agent=CODEX)
        self.assertEqual(service.status()["agent"], CODEX)


class StatusTests(unittest.TestCase):
    def test_status_reports_the_counters(self) -> None:
        service, clock, _ = build(StubLink())
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        status = service.status()
        self.assertEqual(status["spoken"], 1)
        self.assertEqual(status["last_line"], "line 0 なのだ")
        self.assertFalse(status["running"])

    def test_status_names_the_agent_and_the_backend_it_implies(self) -> None:
        service = ChatterService(ChatterConfig(), lambda: None, threading.Lock())
        status = service.status()
        self.assertEqual(status["agent"], CLAUDE_CODE)
        self.assertEqual(status["backend"], "vertex")
        self.assertEqual(status["client"], "")
        service.identity.observe("codex-mcp-client")
        status = service.status()
        self.assertEqual(status["agent"], CODEX)
        self.assertEqual(status["backend"], "codex")
        self.assertEqual(status["client"], "codex-mcp-client")


if __name__ == "__main__":
    unittest.main()
