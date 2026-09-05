"""Tests for the idle chatter.

Two things here are worth more than the rest, because they are what
makes the feature acceptable rather than merely working: the worker must
never make a real tool call wait, and it must never raise into a thread
nobody is watching. Both are exercised directly.

No hardware and no network. The device is a stub, the clock is a
variable, and the line source is a list.

いつ喋るかの方は `test_chatter_pace`。stub と `build()` は `chatter_stubs`。
"""

import json
import logging
import random
import socket
import threading
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

import buddy_chatter
import chatter_core
from buddy_chatter import ChatterService
from chatter_core import SAID, ChatterConfig, Event, parse_event
from chatter_lines import ClaudeCliLineSource
from chatter_stubs import ListSource, StubLink, build


def claude_source(cfg: ChatterConfig, payload: dict[str, Any]) -> ClaudeCliLineSource:
    """A source whose CLI is replaced by a canned answer."""
    return ClaudeCliLineSource(
        cfg, run=lambda _system, _prompt: json.dumps({"structured_output": payload})
    )


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

    def test_what_was_said_is_written_to_the_log(self) -> None:
        # daemon の log にはこれが出ない時期があり、喋ったかどうかを知るには
        # `buddy_chatter_status` を HTTP で叩くしかなかった。log に残って
        # いれば、後から「いつ何を言ったか」を追える。
        link = StubLink()
        service, clock, _ = build(link)
        clock.advance(31.0)
        with self.assertLogs("buddy.chatter", level=logging.INFO) as caught:
            service.step(Event("tool", "Read"))
        self.assertIn('said "line 0 なのだ"', caught.output[0])
        self.assertIn("voice=yes", caught.output[0])
        self.assertIn("next 30s", caught.output[0])

    def test_a_line_shown_but_not_voiced_says_so(self) -> None:
        link = StubLink()
        service, clock, _ = build(link, voice_every=3)
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        clock.advance(31.0)
        with self.assertLogs("buddy.chatter", level=logging.INFO) as caught:
            service.step(Event("tool", "Read"))
        self.assertIn("voice=no", caught.output[0])

    def test_a_device_that_would_not_take_the_line_says_why(self) -> None:
        link = StubLink()
        link.fail_with = TimeoutError("no chat.say ack within 5.0s")
        service, clock, _ = build(link)
        clock.advance(31.0)
        with self.assertLogs("buddy.chatter", level=logging.WARNING) as caught:
            service.step(Event("tool", "Read"))
        self.assertIn("not said", caught.output[0])
        self.assertIn("TimeoutError", caught.output[0])

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


class SessionRegistryTests(unittest.TestCase):
    """どのセッションが動いているかを覚えるのは、喋る側の仕事。

    イベントを見ているのはここだけで、台詞を書く側はバッチを作る瞬間に
    その台帳を覗くだけ。
    """

    SESSION = "747883a7-180d-453a-9f99-b06b38767561"

    def _service(self, **overrides: Any) -> ChatterService:  # noqa: ANN401 — ChatterConfig のそれ
        with TemporaryDirectory() as tmp:
            cfg = ChatterConfig(projects_path=Path(tmp), **overrides)
        return ChatterService(cfg, lambda: None, threading.Lock())

    def test_an_event_records_the_session_it_came_from(self) -> None:
        service, clock, _ = build(StubLink(), sessions=True, projects_path=Path("/nonexistent"))
        clock.advance(31.0)
        service.step(Event("tool", "Read", self.SESSION))
        self.assertEqual(service.status()["sessions"], 1)

    def test_an_event_without_a_session_records_nothing(self) -> None:
        # 古い hook からのイベントも、chatter が自分で作る idle も、
        # 送り主を名乗らない。
        service, clock, _ = build(StubLink(), sessions=True, projects_path=Path("/nonexistent"))
        clock.advance(31.0)
        service.step(Event("tool", "Read"))
        self.assertEqual(service.status()["sessions"], 0)

    def test_turning_it_off_leaves_no_registry(self) -> None:
        service, clock, _ = build(StubLink(), sessions=False)
        clock.advance(31.0)
        service.step(Event("tool", "Read", self.SESSION))
        self.assertIsNone(service.status()["sessions"])

    def test_the_generator_is_handed_the_same_registry(self) -> None:
        # 台帳が 2 つあると、片方は更新されるのに、もう片方が読まれる。
        with mock.patch.object(buddy_chatter, "ClaudeCliLineSource") as source:
            service = self._service()
        self.assertIsNotNone(source.call_args.kwargs["sessions"])
        self.assertIs(source.call_args.kwargs["sessions"], service.sessions)

    def test_nothing_is_handed_over_when_it_is_off(self) -> None:
        with mock.patch.object(buddy_chatter, "ClaudeCliLineSource") as source:
            self._service(sessions=False)
        self.assertIsNone(source.call_args.kwargs["sessions"])


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
