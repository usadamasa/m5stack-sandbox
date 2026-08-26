"""ペーシングのテスト — いつ喋るかだけを見る。

間隔と idle のしきい値が刻まないこと、そしてそのどちらもセッションの
忙しさに追従することを確かめる。`ChatterService` 越しに駆動するのは、
`Pacer` を単体で叩くと「service がそれを本当にそう使っているか」が
抜け落ちるため。デバイスは stub、クロックは変数。
"""

import unittest
from typing import Any

import chatter_pace
from buddy_chatter import ChatterService
from chatter_core import Event
from chatter_stubs import Clock, ListSource, PinnedRandom, StubLink, build


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
        clock.advance(chatter_pace.ACTIVITY_WINDOW + 1.0)
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


if __name__ == "__main__":
    unittest.main()
