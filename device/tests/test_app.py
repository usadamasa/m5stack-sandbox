"""起動口と、アプリの 1 周。

`apps/claude_buddy.py` は import しても起動しない。起動するのは `run()` を
呼んだときで、呼ぶのは `/flash/main.py` と `buddy_link.LAUNCH_SOURCE` の
2 つ (その 2 つが揃っていることは `test_boot.py`)。

組み立てと main loop は `buddy/app.py` にあり、ファームウェアのモジュールを
import するのは `run()` の中だけなので、`sys.modules` へ fake を置けば
CPython の上でも 1 周させられる。ここで見るのは呼び出しの順と回数で、LCD に
何が出るかではない — 描画そのものは実機でしか確かめられない。それでも
footer と chat の重なりが依存している「transcript が出ているあいだ footer を
描かない」という規律は、ここで押さえられる。
"""

import ast
import contextlib
import importlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from buddy import app as buddy_app
from device_fakes import (
    FakeChat,
    FakeGc,
    FakeLcd,
    FakeNetModule,
    FakeSerialModule,
    FakeSpeech,
    FakeState,
    FakeTime,
    FakeTransport,
    FakeUi,
    Poll,
    Recorder,
    firmware_modules,
)

DEVICE_ROOT = Path(__file__).resolve().parents[1]
APP_SHIM = DEVICE_ROOT / "apps" / "claude_buddy.py"


class ShimTest(unittest.TestCase):
    """`apps/claude_buddy.py` は起動口であって、アプリではない。"""

    def setUp(self) -> None:
        apps = str(APP_SHIM.parent)
        if apps not in sys.path:
            sys.path.insert(0, apps)
            self.addCleanup(sys.path.remove, apps)
        # デバイスの階層はホストには無い。import が挿すぶんを戻す。
        for flash in ("/flash", "/flash/apps"):
            self.addCleanup(self._drop_from_path, flash)
        self.addCleanup(sys.modules.pop, "claude_buddy", None)
        _ = sys.modules.pop("claude_buddy", None)

    @staticmethod
    def _drop_from_path(entry: str) -> None:
        while entry in sys.path:
            sys.path.remove(entry)

    def test_importing_the_shim_does_not_start_the_app(self) -> None:
        # 以前はモジュールの末尾が run() を呼んでいたので、覗くだけで
        # アプリが走り出した。テストが書けなかったのはそれが理由。
        with mock.patch.object(buddy_app, "run") as run:
            _ = importlib.import_module("claude_buddy")
        run.assert_not_called()

    def test_the_shim_hands_over_the_app_s_run(self) -> None:
        shim = importlib.import_module("claude_buddy")
        self.assertIs(shim.run, buddy_app.run)

    def test_the_shim_puts_flash_on_the_path(self) -> None:
        # `/flash` は既定の sys.path に無く、upstream のピアはそこに居る。
        _ = importlib.import_module("claude_buddy")
        self.assertIn("/flash", sys.path)
        self.assertIn("/flash/apps", sys.path)

    def test_nothing_at_the_shim_s_module_level_calls_anything(self) -> None:
        # 振る舞いの側は上で見ているが、AST でも押さえておく。ここへ
        # 呼び出しが 1 つ戻るだけで、import が起動に戻ってしまう。
        tree = ast.parse(APP_SHIM.read_text(encoding="utf-8"), filename=str(APP_SHIM))
        calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]
        self.assertEqual(calls, [])


class RunTest(unittest.TestCase):
    """`run()` を回す。差し替えるのは `run()` の中の import の境界だけ。"""

    def setUp(self) -> None:
        self.ui = FakeUi()
        self.state = FakeState()
        self.lcd = FakeLcd()
        self.machine = Recorder()
        self.chat = FakeChat()
        self.speech = FakeSpeech()
        self.gc = FakeGc()
        self.time = FakeTime()
        self.net: FakeTransport | None = None

        for name, fake in (("gc", self.gc), ("time", self.time)):
            self.addCleanup(setattr, buddy_app, name, getattr(buddy_app, name))
            setattr(buddy_app, name, fake)

        firmware = mock.patch.dict(
            sys.modules, firmware_modules(self.ui, self.state, self.lcd, self.machine)
        )
        firmware.start()
        self.addCleanup(firmware.stop)

        def chat_panel() -> FakeChat:
            return self.chat

        def speech_player(_ble: object) -> FakeSpeech:
            return self.speech

        # こちらは本物が import できるので、モジュールの属性だけ差し替える。
        for module_name, attr, fake in (
            ("buddy.chat", "ChatPanel", chat_panel),
            ("buddy.speak", "SpeechPlayer", speech_player),
        ):
            patch = mock.patch.object(importlib.import_module(module_name), attr, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, poll: Poll, net_error: OSError | None = None) -> FakeTransport:
        """USB の leg を返す。net の leg は `self.net` に残る (bind に失敗させたなら None)。"""
        serial = FakeSerialModule(poll)
        net = FakeNetModule(net_error)
        with (
            mock.patch.object(
                importlib.import_module("buddy.serial"), "BuddySerial", serial.BuddySerial
            ),
            mock.patch.object(importlib.import_module("buddy.netlink"), "BuddyNet", net.BuddyNet),
        ):
            buddy_app.run()
        assert serial.made is not None
        self.net = net.made
        return serial.made

    def test_a_pass_through_the_loop_and_out_at_the_interrupt(self) -> None:
        def poll(transport: FakeTransport, count: int) -> None:
            if count == 1:
                # トランスポートが配るのはこの 2 つだけ。あとは main loop。
                transport.on_state("connected")
                transport.on_line(b'{"cmd":"chat.say","text":"hi"}')
            elif count >= 3:
                raise KeyboardInterrupt

        transport = self._run(poll)

        # 状態変化も chat の再描画も、コールバックではなくループが描いた。
        self.assertIn("set_connection", self.ui.names())
        self.assertEqual(self.chat.names().count("render"), 2)
        self.assertEqual(transport.acks(), [{"ack": "chat.say", "ok": True}])
        # transcript が出ているあいだ footer は打ち抜かない。描いたのは
        # 起動時の 1 回だけ。
        self.assertEqual(self.ui.names().count("update_footer"), 1)
        self.assertGreaterEqual(self.state.names().count("tick_nap"), 1)
        # 音声の pump はトランスポートを汲んだ直後、描くものより前。
        self.assertEqual(self.speech.names().count("pump"), transport.polls - 1)
        # Ctrl-C は REPL へ戻る出口。reboot しない。
        self.assertEqual(transport.deinits, 1)
        self.assertEqual(self.speech.names().count("stop"), 1)
        self.assertIn("drawString", self.lcd.names())
        self.assertEqual(self.machine.names(), [])
        # WiFi の leg も同じループで汲まれ、同じ去り際で畳まれる。ack は
        # 繋がっている相手にだけ届く — こちらは繋がっていない。
        assert self.net is not None
        # USB が先で、その最後の poll が KeyboardInterrupt を投げるので 1 回少ない。
        self.assertEqual(self.net.polls, transport.polls - 1)
        self.assertEqual(self.net.deinits, 1)
        self.assertEqual(self.net.lines, [])

    def test_the_boot_note_carries_the_reset_cause_and_the_uptime(self) -> None:
        # 例外を伴わない reboot (RST 線、brownout) には traceback が無い。
        # host が繋いだ瞬間に reset_cause と稼働時間を言えば、daemon の log
        # 1 行で「いつ何で reboot したか」が読める (issue #92)。
        def poll(transport: FakeTransport, count: int) -> None:
            if count == 2:
                transport.on_state("connected")
            elif count >= 3:
                raise KeyboardInterrupt

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._run(poll)

        # FakeTime は sleep_ms のたびに 4 秒進む。2 周目なら 1 回寝ている。
        self.assertIn("claude_buddy: boot reset_cause=2 up=4s\n", out.getvalue())

    def test_the_app_runs_on_usb_alone_when_the_listener_cannot_bind(self) -> None:
        def poll(transport: FakeTransport, count: int) -> None:
            if count == 1:
                transport.on_state("connected")
                transport.on_line(b'{"cmd":"chat.say","text":"hi"}')
            elif count >= 2:
                raise KeyboardInterrupt

        transport = self._run(poll, net_error=OSError(98, "EADDRINUSE"))
        self.assertIsNone(self.net)
        self.assertEqual(transport.acks(), [{"ack": "chat.say", "ok": True}])
        self.assertEqual(self.machine.names(), [])

    def test_the_chrome_is_repainted_when_the_panel_comes_back(self) -> None:
        # chat.clear は panel を返す。chat は header まで覆っているので、
        # main panel だけでなく chrome の全面描き直しが要る。
        def poll(transport: FakeTransport, count: int) -> None:
            if count == 1:
                transport.on_line(b'{"cmd":"chat.clear"}')
            elif count >= 2:
                raise KeyboardInterrupt

        _ = self._run(poll)
        self.assertFalse(self.chat.active)
        self.assertEqual(self.chat.names().count("render"), 0)
        # `_redraw_chrome` を持たない UI での落とし先。
        self.assertIn("update_heartbeat", self.ui.names())
        self.assertIn("restore_button_hints", self.ui.names())
        self.assertEqual(self.ui.names().count("update_footer"), 2)

    def test_upstream_s_own_helper_wins_when_it_is_there(self) -> None:
        redraw = Recorder()
        self.ui._redraw_chrome = lambda: redraw.record("_redraw_chrome")

        def poll(transport: FakeTransport, count: int) -> None:
            if count == 1:
                transport.on_line(b'{"cmd":"chat.clear"}')
            elif count >= 2:
                raise KeyboardInterrupt

        _ = self._run(poll)
        self.assertEqual(redraw.names(), ["_redraw_chrome"])
        self.assertNotIn("update_heartbeat", self.ui.names())

    def test_an_unhandled_exception_reboots(self) -> None:
        # launcher へ戻る API が無いので、reset が App List へ帰る唯一の道。
        printed: list[BaseException] = []

        def poll(_transport: FakeTransport, _count: int) -> None:
            raise RuntimeError("boom")

        # `sys.print_exception` は MicroPython にしか無い。
        fake_sys = types.SimpleNamespace(print_exception=printed.append)
        with mock.patch.object(buddy_app, "sys", fake_sys):
            transport = self._run(poll)

        self.assertEqual([type(e).__name__ for e in printed], ["RuntimeError"])
        self.assertEqual(self.machine.names(), ["reset"])
        self.assertEqual(transport.deinits, 1)
        # 画面は黒く塗るが、REPL の札は出さない。
        self.assertIn("fillScreen", self.lcd.names())
        self.assertNotIn("drawString", self.lcd.names())

    def test_the_collector_is_told_to_run_early(self) -> None:
        # 既定は確保に失敗してから集める。これだけ断片化したヒープでは、
        # その時点でもう手遅れになる。
        def poll(_transport: FakeTransport, _count: int) -> None:
            raise KeyboardInterrupt

        _ = self._run(poll)
        self.assertEqual(self.gc.thresholds, [100_000 + 60_000 // 4])


if __name__ == "__main__":
    unittest.main()
