"""周期処理のテスト — 落ちたリンクを拾い直し、health を書き直す。

これが無かった日 (2026-08-28) の実測: デバイスは 09:03 に reboot して以降
ずっと正常だったのに、daemon は死んだ fd を 4 時間握ったままだった。開き直す
経路が tool 呼び出しの中にしか無く、その 4 時間は誰も tool を呼ばなかった。

`mcp_state` そのものは触らない。supervisor は状態を持つモジュールを引数で
受け取るので、ここでは同じ面を持つ偽物を渡す — patch 先が無ければ、モジュールを
割ったときにテストが黙って本物のシリアルポートを開きに行くこともない。
"""

import threading
import unittest

import mcp_supervisor
from buddy_wire import Message
from mcp_health import Check


class FakeLink:
    def __init__(self, port: str, *, answers: bool = True) -> None:
        self.port = port
        self.connected = True
        self.dropped = False
        self.answers = answers
        self.requests: list[str] = []
        self.lock_held: list[bool] = []

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        self.requests.append(expect)
        self.lock_held.append(FakeState.current_lock_held())
        if not self.answers:
            raise TimeoutError(f"no '{expect}' ack within {timeout}s")
        return {"ack": expect, "ok": True, "version": "m5buddy-0.1", "sys": {"heap": 41056}}


class FakeState:
    """`mcp_state` のうち supervisor が見る面だけ。"""

    live: "FakeState | None" = None

    def __init__(self, *, wanted: str | None = "/dev/fake", link: FakeLink | None = None) -> None:
        self.wanted = wanted
        self.device_lock = threading.Lock()
        self.link = link
        self.opens: list[str] = []
        self.open_error: Exception | None = None
        FakeState.live = self

    @classmethod
    def current_lock_held(cls) -> bool:
        state = cls.live
        return state is not None and state.device_lock.locked()

    def live_link(self) -> FakeLink | None:
        link = self.link
        if link is None or not link.connected or link.dropped:
            return None
        return link

    def get_link(self, port: str | None = None) -> FakeLink:
        target = port or "/dev/fake"
        self.opens.append(target)
        if self.open_error is not None:
            raise self.open_error
        self.link = FakeLink(target)
        return self.link


class SupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.checks: list[Check] = []

    def _sup(self, state: FakeState) -> mcp_supervisor.Supervisor:
        return mcp_supervisor.Supervisor(state, self.checks.append)

    def test_a_dropped_link_is_reopened(self) -> None:
        # 本題。デバイスが下で reboot すると reader が `dropped` を立てて降り、
        # `connected` は開いたつもりのまま True になる。tool 呼び出しを待って
        # いると、誰も呼ばない日はその日ずっと黙ることになる。
        dead = FakeLink("/dev/fake")
        dead.dropped = True
        state = FakeState(link=dead)
        self.assertEqual(self._sup(state).tick(), "reopened")
        self.assertEqual(state.opens, ["/dev/fake"])

    def test_a_live_link_is_left_alone(self) -> None:
        state = FakeState(link=FakeLink("/dev/fake"))
        self.assertEqual(self._sup(state).tick(), "ok")
        self.assertEqual(state.opens, [], "a working link must not be torn down")

    def test_a_port_let_go_on_purpose_is_not_taken_back(self) -> None:
        # `buddy_disconnect` が最後の言葉であり続けること。deploy と esptool は
        # これを頼りにポートを受け取る。
        state = FakeState(wanted=None, link=None)
        self.assertEqual(self._sup(state).tick(), "released")
        self.assertEqual(state.opens, [])

    def test_it_stands_aside_while_a_tool_owns_the_device(self) -> None:
        # ロックを待つと tool 呼び出しがこの tick のぶん遅れる。飛ばして
        # 次の tick で拾えばよい。
        dead = FakeLink("/dev/fake")
        dead.dropped = True
        state = FakeState(link=dead)
        with state.device_lock:
            self.assertEqual(self._sup(state).tick(), "busy")
        self.assertEqual(state.opens, [])

    def test_it_holds_the_lock_while_it_talks_to_the_device(self) -> None:
        # 握らずに request を出すと ack が入れ違う。
        link = FakeLink("/dev/fake")
        state = FakeState(link=link)
        self._sup(state).tick()
        self.assertEqual(link.lock_held, [True])
        self.assertFalse(state.device_lock.locked(), "the lock must be given back")

    def test_a_port_that_will_not_open_is_reported_not_raised(self) -> None:
        # ボードが挿さっていない間は毎 tick ここを通る。投げるとスレッドが
        # 死に、二度と拾い直せなくなる。
        state = FakeState(link=None)
        state.open_error = OSError("no such port")
        self.assertEqual(self._sup(state).tick(), "failed")
        self.assertFalse(self.checks[-1].ok)
        self.assertIn("no such port", self.checks[-1].detail)

    def test_a_link_that_is_open_but_mute_is_not_called_healthy(self) -> None:
        # アプリが走っておらず REPL で止まっているときの姿。ポートは開くが
        # ack は来ない。chatter から見れば喋れないのと同じ。
        state = FakeState(link=FakeLink("/dev/fake", answers=False))
        self.assertEqual(self._sup(state).tick(), "mute")
        self.assertFalse(self.checks[-1].ok)

    def test_the_check_it_reports_says_what_the_device_answered(self) -> None:
        # `health.json` の `serial` がこれで書き変わる。周期 health の要点は
        # 「今デバイスが答えるか」で、起動時の遺言ではない。
        state = FakeState(link=FakeLink("/dev/fake"))
        self._sup(state).tick()
        self.assertEqual(self.checks[-1].name, "serial")
        self.assertTrue(self.checks[-1].ok)
        self.assertIn("heap=41056", self.checks[-1].detail)

    def test_nothing_is_written_while_the_device_is_someone_elses(self) -> None:
        # 飛ばした tick で health を書くと、`checked_at` だけが進んで中身は
        # 前の tick のもの、という嘘になる。
        state = FakeState(link=FakeLink("/dev/fake"))
        with state.device_lock:
            self._sup(state).tick()
        self.assertEqual(self.checks, [])

    def test_it_only_speaks_up_when_something_changed(self) -> None:
        # ボードを抜いている間ずっと出続ける行なので、毎 tick 出すと log が
        # これで埋まる。
        state = FakeState(link=None)
        state.open_error = OSError("no such port")
        sup = self._sup(state)
        with self.assertLogs("buddy.supervisor", level="WARNING") as first:
            sup.tick()
        self.assertEqual(len(first.output), 1)
        sup.tick()
        sup.tick()
        self.assertEqual(sup.outcome, "failed")
        # 直ったときは、また 1 度だけ言う。
        state.open_error = None
        with self.assertLogs("buddy.supervisor", level="INFO") as healed:
            sup.tick()
        self.assertEqual(len(healed.output), 1)


class HealthWriterTest(unittest.TestCase):
    """起動時に集めた Check を持ち回り、`serial` の 1 項目だけ差し替える。"""

    def test_it_replaces_the_serial_line_and_keeps_the_rest(self) -> None:
        # VOICEVOX の HTTP と `claude --version` を 60 秒ごとに叩き直すのは
        # 無駄。起動時に見た項目はそのまま持ち回る。
        written: list[list[Check]] = []
        baseline = [
            Check("config", True, "port=/dev/fake"),
            Check("serial", True, "/dev/fake: heap=1"),
            Check("voicevox", True, "version 0.25.2"),
        ]
        writer = mcp_supervisor.health_writer(baseline, written.append)
        writer(Check("serial", False, "/dev/fake: no status ack"))
        names = [check.name for check in written[0]]
        self.assertEqual(names, ["config", "serial", "voicevox"])
        self.assertFalse(written[0][1].ok)
        self.assertEqual(written[0][2].detail, "version 0.25.2")

    def test_a_startup_that_never_reached_serial_still_gets_one(self) -> None:
        # 起動時にポートを開けなかった run では `serial` が失敗として載る。
        # 後から開けたなら、その run の health もそう書き変わってほしい。
        written: list[list[Check]] = []
        writer = mcp_supervisor.health_writer([Check("config", True, "")], written.append)
        writer(Check("serial", True, "/dev/fake: heap=1"))
        self.assertEqual([check.name for check in written[0]], ["config", "serial"])


if __name__ == "__main__":
    unittest.main()
