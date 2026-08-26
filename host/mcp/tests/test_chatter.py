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
from typing import Any

import buddy_chatter
import chatter_core
from buddy_chatter import ChatterService
from buddy_wire import Message
from chatter_core import SAID, ChatterConfig, Event, parse_event
from chatter_lines import ClaudeCliLineSource


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


def claude_source(cfg: ChatterConfig, payload: dict[str, Any]) -> ClaudeCliLineSource:
    """A source whose CLI is replaced by a canned answer."""
    return ClaudeCliLineSource(
        cfg, run=lambda _system, _prompt: json.dumps({"structured_output": payload})
    )


class Clock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class PinnedRandom(random.Random):
    """Jitter pinned to the middle of whatever range it is asked for.

    The activity tests reason about where one draw lands relative to a
    threshold, which a real generator would turn into a coin toss.
    """

    def random(self) -> float:
        return 0.5

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


def build(
    link: StubLink | None,
    source: ListSource | None = None,
    lock: threading.Lock | None = None,
    real_clock: bool = False,
    rng: random.Random | None = None,
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
        rng=rng if rng is not None else random.Random(1),
        clock=time.monotonic if real_clock else clock,
    )
    return service, clock, src


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


class ActivityPacingTests(unittest.TestCase):
    """The gap tracks how busy the session is, not just the dice.

    The jitter is pinned to the middle of its range throughout, so every
    assertion here is about where the range itself sat.
    """

    def busy(self, **overrides: Any) -> tuple[ChatterService, Clock, ListSource]:  # noqa: ANN401
        settings: dict[str, Any] = {"gap_min": 40.0, "gap_max": 150.0, "busy_rate": 12.0}
        settings.update(overrides)
        return build(StubLink(), rng=PinnedRandom(), **settings)

    def test_a_quiet_session_waits_near_the_top_of_the_range(self) -> None:
        service, _, _ = self.busy()
        service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 0, "one event inside the gap says nothing yet")
        self.assertGreater(service.status()["next_gap_s"], 95.0)

    def test_a_busy_session_waits_near_the_bottom_of_the_range(self) -> None:
        service, _, _ = self.busy()
        for _ in range(24):
            service.step(Event("tool", "Read"))
        self.assertEqual(service.status()["tempo"], 1.0)
        self.assertLess(service.status()["next_gap_s"], 95.0)

    def test_the_gap_never_leaves_the_configured_range(self) -> None:
        service, _, _ = self.busy()
        for _ in range(64):
            service.step(Event("tool", "Read"))
            gap = service.status()["next_gap_s"]
            self.assertGreaterEqual(gap, 40.0)
            self.assertLessEqual(gap, 150.0)

    def test_a_burst_shortens_the_wait_already_under_way(self) -> None:
        service, clock, _ = self.busy()
        clock.advance(80.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 0, "80s is still inside a quiet session's gap")
        for _ in range(24):
            service.step(Event("tool", "Read"))
        self.assertEqual(service.spoken, 1, "the burst pulled the threshold under the 80s spent")

    def test_activity_ages_out_of_the_window(self) -> None:
        service, clock, _ = self.busy()
        for _ in range(24):
            service.step(Event("tool", "Read"))
        self.assertEqual(service.status()["tempo"], 1.0)
        clock.advance(buddy_chatter._ACTIVITY_WINDOW + 1.0)
        self.assertEqual(service.status()["tempo"], 0.0)

    def test_the_devices_own_idle_event_is_not_activity(self) -> None:
        service, clock, _ = self.busy()
        clock.advance(200.0)
        service.step(None)
        self.assertEqual(service.spoken, 1, "silence alone spoke")
        self.assertEqual(service.status()["tempo"], 0.0, "talking to itself is not a busy session")

    def test_a_single_valued_range_stays_where_it_was_configured(self) -> None:
        service, _, _ = self.busy(gap_min=30.0, gap_max=30.0)
        for _ in range(24):
            service.step(Event("tool", "Read"))
        self.assertEqual(service.status()["next_gap_s"], 30.0)

    def test_a_zero_saturation_rate_does_not_divide_by_zero(self) -> None:
        service, _, _ = self.busy(busy_rate=0.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.status()["tempo"], 1.0, "any event at all counts as busy")


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


class StatusTests(unittest.TestCase):
    def test_status_reports_the_counters(self) -> None:
        service, clock, _ = build(StubLink())
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        status = service.status()
        self.assertEqual(status["spoken"], 1)
        self.assertEqual(status["last_line"], "line 0 なのだ")
        self.assertFalse(status["running"])

    def test_status_names_the_backend_that_writes_the_lines(self) -> None:
        service = ChatterService(ChatterConfig(), lambda: None, threading.Lock())
        status = service.status()
        self.assertEqual(status["backend"], "claude-cli")
        self.assertEqual(status["model"], "sonnet")


class VarietyTests(unittest.TestCase):
    """What keeps the muttering from converging on the same few lines.

    The persona file has always asked for variety, but a generator that
    never sees its own output cannot honour that: every batch is built
    from the same shape of event log and comes back the same. These are
    the two things that give it something to differ from — a memory of
    what was said, and a fresh angle per batch — plus the net that
    catches a repeat when both fail.
    """

    def test_a_spoken_line_comes_back_as_context(self) -> None:
        link = StubLink()
        service, clock, src = build(link)
        clock.advance(100)
        service.step(Event("tool", "Read"))
        clock.advance(100)
        service.step(Event("tool", "Bash"))
        said = [ev for ev in src.contexts[-1] if ev.kind == SAID]
        self.assertEqual([ev.detail for ev in said], ["line 0 なのだ"])

    def test_the_memory_of_what_was_said_is_bounded(self) -> None:
        link = StubLink()
        service, clock, src = build(link)
        for _ in range(20):
            clock.advance(100)
            service.step(Event("tool", "Read"))
        said = [ev for ev in src.contexts[-1] if ev.kind == SAID]
        self.assertGreater(len(said), 1)
        self.assertLessEqual(len(said), buddy_chatter._SAID_DEPTH)
        self.assertEqual(said[-1].detail, "line 18 なのだ", "the newest is kept")

    def test_a_line_the_device_never_took_is_not_remembered(self) -> None:
        link = StubLink()
        link.fail_with = RuntimeError("no device")
        service, clock, src = build(link)
        clock.advance(100)
        service.step(Event("tool", "Read"))
        link.fail_with = None
        clock.advance(100)
        service.step(Event("tool", "Bash"))
        self.assertEqual([ev for ev in src.contexts[-1] if ev.kind == SAID], [])

    def test_the_spoken_lines_are_not_events_the_socket_can_forge(self) -> None:
        self.assertNotIn(SAID, chatter_core.KINDS)
        forged = json.dumps({"kind": SAID, "detail": "ぼくの偽物なのだ"}).encode()
        self.assertIsNone(parse_event(forged))

    def test_the_prompt_separates_what_was_said_from_what_happened(self) -> None:
        source = ClaudeCliLineSource(ChatterConfig())
        prompt = source._user_prompt([Event("tool", "Bash"), Event(SAID, "ずんだ餅が食べたいのだ")])
        self.assertIn("- tool: Bash", prompt)
        self.assertIn("ずんだ餅が食べたいのだ", prompt)
        self.assertNotIn(f"- {SAID}:", prompt, "a said line is not one of the events")

    def test_the_angle_is_drawn_fresh_for_every_batch(self) -> None:
        cfg = ChatterConfig()
        drawn = {
            ClaudeCliLineSource(cfg, random.Random(seed))._user_prompt([]) for seed in range(12)
        }
        self.assertGreater(len(drawn), 1, "identical context must not mean an identical prompt")

    def test_a_line_already_said_is_dropped_from_the_batch(self) -> None:
        source = claude_source(
            ChatterConfig(), {"lines": ["また同じことを言うのだ", "こっちは新しいのだ"]}
        )
        line = source.next_line([Event(SAID, "また同じことを言うのだ")])
        self.assertEqual(line, "こっちは新しいのだ")

    def test_a_batch_that_repeats_itself_is_thinned(self) -> None:
        source = claude_source(
            ChatterConfig(), {"lines": ["ふたつあるのだ", "ふたつあるのだ", "ひとつだけなのだ"]}
        )
        self.assertEqual(source.next_line([]), "ふたつあるのだ")
        self.assertEqual(source.next_line([]), "ひとつだけなのだ")
        self.assertEqual(source.generated, 2)


if __name__ == "__main__":
    unittest.main()
