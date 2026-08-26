"""起動時の疏通確認 — 何を見て、どう log に出し、どこへ書き置くか。

実機もネットワークも要らない。engine と CLI は注入した関数、デバイスは
stub、state ディレクトリは一時ディレクトリへ逃がしてある。
"""

import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

import mcp_health
import mcp_state
from buddy_wire import Message
from chatter_core import ChatterConfig
from chatter_stubs import StubLink as ChatStub
from chatter_stubs import build
from mcp_stubs import McpTestCase, StubLink

# デバイスが実際に返す status ack の形。`sys` の下に heap が居るところまで
# 含めてあるのは、そこを平らだと思い込んだ整形が黙って空行を出すため。
STATUS_ACK: Message = {
    "ack": "status",
    "version": "m5buddy-0.1",
    "name": "Buddy",
    "owner": "",
    "sys": {"up": 1796, "heap": 68208},
}


class OriginTests(unittest.TestCase):
    """設定 1 つがどこから来たか。env > config > 既定。"""

    def test_the_environment_wins(self) -> None:
        got = mcp_health.origin("BUDDY_PORT", {"BUDDY_PORT": "/dev/env"}, {"BUDDY_PORT": "/dev/f"})
        self.assertEqual(got, "env")

    def test_an_empty_environment_value_is_not_a_setting(self) -> None:
        # `FOO=` はシェルで変数を消し損ねた形。`merge_env` がそれを未設定と
        # 見なす以上、出どころの表示も同じ規則で読めなければ嘘になる。
        got = mcp_health.origin("BUDDY_PORT", {"BUDDY_PORT": ""}, {"BUDDY_PORT": "/dev/f"})
        self.assertEqual(got, "config")

    def test_nothing_anywhere_is_the_default(self) -> None:
        self.assertEqual(mcp_health.origin("BUDDY_PORT", {}, {}), "default")


class ConfigCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.toml"
        self.values = mcp_health.effective(ChatterConfig(), "/dev/cu.usbmodem101", connect=True)

    def test_a_missing_file_says_so_rather_than_naming_nothing(self) -> None:
        checks = mcp_health.config_checks({}, {}, self.values, self.path)
        self.assertIn("no file at", checks[0].detail)
        self.assertIn(str(self.path), checks[0].detail)

    def test_a_file_that_exists_is_named(self) -> None:
        self.path.write_text('port = "/dev/x"\n', encoding="utf-8")
        checks = mcp_health.config_checks({}, {}, self.values, self.path)
        self.assertEqual(checks[0].detail, f"file={self.path}")

    def test_every_setting_carries_where_it_came_from(self) -> None:
        checks = mcp_health.config_checks(
            {"BUDDY_CHATTER_MODEL": "opus"}, {"BUDDY_PORT": "/dev/x"}, self.values, self.path
        )
        shown = checks[1].detail
        for label, _var in mcp_health.SETTING_NAMES:
            self.assertIn(f"{label}=", shown)
        self.assertIn("port=/dev/cu.usbmodem101(config)", shown)
        self.assertIn("chatter.model=sonnet(env)", shown)
        self.assertIn("chatter.gap_min=40(default)", shown)


class DescribeStatusTests(unittest.TestCase):
    def test_the_ack_becomes_one_line(self) -> None:
        self.assertEqual(
            mcp_health.describe_status(STATUS_ACK),
            "version=m5buddy-0.1 name=Buddy heap=68208",
        )

    def test_an_unrecognisable_ack_still_reads_as_an_answer(self) -> None:
        # ファームウェアが何を載せるかはこのリポジトリの外で決まる。形が
        # 変わったときに出るべきなのは KeyError ではなく短い一行。
        self.assertIn("answered", mcp_health.describe_status({"ack": "status"}))


class SerialCheckTests(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        mcp_state.startup_connect = None
        self.addCleanup(setattr, mcp_state, "startup_connect", None)

    def _live(self, link: Any) -> None:  # noqa: ANN401 — stub は 2 種類ある
        mcp_state.link = link
        mcp_state.startup_connect = {"ok": True, "port": "/dev/stub"}

    def test_a_server_that_was_never_asked_to_connect_is_not_a_failure(self) -> None:
        check = mcp_health.serial_check()
        self.assertTrue(check.ok)
        self.assertIn("connect_on_start", check.detail)

    def test_a_port_that_did_not_open_is_reported_but_not_logged_twice(self) -> None:
        # `connect_on_start` が既に WARNING を書いている。同じことを 2 行
        # 書くなら、この機能は log を読みやすくしていない。
        mcp_state.startup_connect = {"ok": False, "error": "SerialException: busy"}
        check = mcp_health.serial_check()
        self.assertFalse(check.ok)
        self.assertTrue(check.quiet)
        self.assertIn("busy", check.detail)

    def test_the_device_answer_lands_in_the_detail(self) -> None:
        link = StubLink("/dev/stub")
        link.connected = True
        link.ack_extra = STATUS_ACK
        self._live(link)
        check = mcp_health.serial_check()
        self.assertTrue(check.ok)
        self.assertIn("heap=68208", check.detail)
        self.assertIn("/dev/stub", check.detail)

    def test_the_device_is_asked_under_the_lock(self) -> None:
        # 握らずに request を出すと、走っている chatter と ack が入れ違う。
        link = StubLink("/dev/stub")
        link.connected = True
        self._live(link)
        mcp_health.serial_check()
        self.assertEqual(link.lock_held, [True])

    def test_an_open_port_with_no_answer_is_a_failure_worth_seeing(self) -> None:
        class Mute(StubLink):
            def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
                raise TimeoutError("no 'status' ack within 4.0s")

        link = Mute("/dev/stub")
        link.connected = True
        self._live(link)
        check = mcp_health.serial_check()
        self.assertFalse(check.ok)
        self.assertFalse(check.quiet)
        self.assertIn("no status ack", check.detail)


class VoicevoxCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        # LAN のアドレス探しをテストに持ち込まない。engine の場所は
        # 環境変数で決まる、というのが本番の経路でもある。
        patcher = mock.patch.dict("os.environ", {"VOICEVOX_URL": "http://192.0.2.10:50021"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_reachable_engine_reports_its_version(self) -> None:
        check = mcp_health.voicevox_check(lambda _url, _timeout: '"0.14.5"')
        self.assertTrue(check.ok)
        self.assertIn("0.14.5", check.detail)
        self.assertIn("192.0.2.10", check.detail)

    def test_an_engine_that_is_not_there_names_the_url_it_tried(self) -> None:
        def refuse(_url: str, _timeout: float) -> str:
            raise ConnectionRefusedError("Connection refused")

        check = mcp_health.voicevox_check(refuse)
        self.assertFalse(check.ok)
        self.assertIn("http://192.0.2.10:50021", check.detail)
        self.assertIn("ConnectionRefusedError", check.detail)

    def test_a_url_that_cannot_be_worked_out_is_the_engine_check_failing(self) -> None:
        with mock.patch.dict("os.environ", {"VOICEVOX_URL": "http://127.0.0.1:50021"}):
            check = mcp_health.voicevox_check(lambda _url, _timeout: "unused")
        self.assertFalse(check.ok)
        self.assertIn("loopback", check.detail)


class ClaudeCheckTests(unittest.TestCase):
    def test_a_missing_binary_says_what_the_chatter_falls_back_to(self) -> None:
        cfg = ChatterConfig(claude_bin="definitely-not-on-this-path")
        check = mcp_health.claude_check(cfg, lambda _b: (0, "unused"))
        self.assertFalse(check.ok)
        self.assertIn("canned lines", check.detail)

    def test_a_working_cli_reports_its_version(self) -> None:
        check = mcp_health.claude_check(ChatterConfig(claude_bin="sh"), lambda _b: (0, "1.2.3"))
        self.assertTrue(check.ok)
        self.assertIn("1.2.3", check.detail)

    def test_a_cli_that_exits_non_zero_is_a_failure(self) -> None:
        check = mcp_health.claude_check(
            ChatterConfig(claude_bin="sh"), lambda _b: (1, "not logged in")
        )
        self.assertFalse(check.ok)
        self.assertIn("not logged in", check.detail)


class ChatterCheckTests(unittest.TestCase):
    def test_a_chatter_turned_off_on_purpose_is_not_a_warning(self) -> None:
        service, _clock, _src = build(ChatStub(), enabled=False)
        checks = mcp_health.chatter_checks(service)
        self.assertEqual([c.ok for c in checks], [True])
        self.assertIn("disabled", checks[0].detail)

    def test_a_chatter_that_never_bound_its_socket_is_named(self) -> None:
        # worker だけ回っていても hook は届かない。外からは「独り言を
        # 言わない」としか見えないので、log で層を名指しできること。
        service, _clock, _src = build(ChatStub())
        socket, running = mcp_health.chatter_checks(service)
        self.assertEqual(socket.name, "socket")
        self.assertFalse(socket.ok)
        self.assertFalse(running.ok)


class SaveAndLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self._tmp.name, "HOME": self._tmp.name}
        self.path = Path(self._tmp.name) / "buddy" / "health.json"

    def test_what_was_written_is_what_comes_back(self) -> None:
        mcp_health.save([mcp_health.Check("voicevox", False, "unreachable")], self.env)
        loaded = mcp_health.load(self.env)
        assert loaded is not None
        self.assertEqual(
            loaded["checks"], [{"name": "voicevox", "ok": False, "detail": "unreachable"}]
        )
        self.assertIn("checked_at", loaded)

    def test_the_quiet_flag_stays_out_of_the_file(self) -> None:
        # あれは log に出すかどうかの都合で、status を読む側の関心ではない。
        mcp_health.save([mcp_health.Check("serial", False, "busy", quiet=True)], self.env)
        loaded = mcp_health.load(self.env)
        assert loaded is not None
        self.assertNotIn("quiet", loaded["checks"][0])

    def test_no_file_reads_as_no_health(self) -> None:
        self.assertIsNone(mcp_health.load(self.env))

    def test_a_half_written_file_reads_as_no_health(self) -> None:
        # supervisor は daemon の代わりに確かめ直せない。壊れた health で
        # `buddy-mcpd status` ごと落ちるより、health だけ空になる方がまし。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"checks": [', encoding="utf-8")
        self.assertIsNone(mcp_health.load(self.env))

    def test_the_file_is_swapped_in_rather_than_grown(self) -> None:
        mcp_health.save([mcp_health.Check("claude", True, "found")], self.env)
        mcp_health.save([mcp_health.Check("claude", True, "found again")], self.env)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["checks"][0]["detail"], "found again")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))


class CheckOnStartTests(McpTestCase):
    """log に何が立つか。daemon の起動 1 回ぶんの通し。"""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self._tmp.name, "HOME": self._tmp.name}
        mcp_state.startup_connect = None
        self.addCleanup(setattr, mcp_state, "startup_connect", None)

    def _run(self) -> list[str]:
        service, _clock, _src = build(ChatStub())
        stub = mcp_health.Check("stub", True, "stub")
        with (
            mock.patch.object(mcp_health, "voicevox_check", return_value=stub),
            mock.patch.object(mcp_health, "claude_check", return_value=stub),
            # 走らせている人のマシンの `config.toml` を覗きに行かせない。
            mock.patch.dict("os.environ", {"HOME": self._tmp.name}, clear=True),
            self.assertLogs("buddy.health", level=logging.INFO) as caught,
        ):
            mcp_health.check_on_start(self.env, service, connect=False)
        return caught.output

    def test_one_line_per_item(self) -> None:
        lines = self._run()
        for name in ("config", "serial", "socket", "chatter"):
            self.assertTrue(any(f"{name}:" in line for line in lines), (name, lines))

    def test_a_failure_is_a_warning(self) -> None:
        lines = self._run()
        # socket も chatter も上がっていない service を渡してあるので、
        # そこは WARNING で立つ。
        self.assertTrue(
            any(line.startswith("WARNING") and "socket" in line for line in lines), lines
        )

    def test_the_result_is_left_where_the_supervisor_reads_it(self) -> None:
        self._run()
        loaded = mcp_health.load(self.env)
        assert loaded is not None
        self.assertIn("serial", [check["name"] for check in loaded["checks"]])


if __name__ == "__main__":
    unittest.main()
