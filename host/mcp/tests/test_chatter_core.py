"""chatter の共有物のテスト — データグラムのパースと、設定の解決。

どちらも実機もネットワークも要らない。入力は hook が送ってくるバイト列と、
環境変数だけ。
"""

import json
import unittest

from chatter_core import ChatterConfig, Event, parse_event


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

    def test_a_field_this_version_does_not_know_is_ignored(self) -> None:
        # まだ `agent` を送ってくる古い hook がパースエラーになってはいけない:
        # socket はスクリプトのどの版よりも長く生きる。
        self.assertEqual(parse_event(b'{"kind": "stop", "agent": "codex"}'), Event("stop", ""))


class SessionIdTests(unittest.TestCase):
    """`session` は transcript を引く鍵になるので、ここが唯一の検問になる。

    socket はこのマシンの誰にでも開いている。この値は後で
    `~/.claude/projects/*/<session>.jsonl` の glob に埋まるので、UUID 以外の
    ものが通ればそこがパスの組み立てになる。
    """

    VALID = "747883a7-180d-453a-9f99-b06b38767561"

    def test_accepts_a_uuid(self) -> None:
        ev = parse_event(json.dumps({"kind": "tool", "session": self.VALID}).encode())
        self.assertEqual(ev, Event("tool", "", self.VALID))

    def test_absent_session_is_empty(self) -> None:
        ev = parse_event(b'{"kind": "stop"}')
        assert ev is not None
        self.assertEqual(ev.session, "")

    def test_uppercase_is_normalised(self) -> None:
        # 送り主がどう書いても、引く先のファイル名は小文字。
        ev = parse_event(json.dumps({"kind": "tool", "session": self.VALID.upper()}).encode())
        assert ev is not None
        self.assertEqual(ev.session, self.VALID)

    def test_rejects_anything_that_is_not_a_uuid(self) -> None:
        # 弾いたものは黙って空になる。イベント自体は捨てない — 出来事は
        # 起きたのだし、送り主が古い hook である可能性の方が高い。
        for bad in (
            "../../../etc/passwd",
            "747883a7-180d-453a-9f99-b06b38767561/../secret",
            "not-a-uuid",
            "",
            "*",
            "747883a7180d453a9f99b06b38767561",
            "747883a7-180d-453a-9f99-b06b38767561 ",
            "g47883a7-180d-453a-9f99-b06b38767561",
        ):
            with self.subTest(session=bad):
                ev = parse_event(json.dumps({"kind": "tool", "session": bad}).encode())
                assert ev is not None
                self.assertEqual(ev.session, "")

    def test_a_non_string_session_is_dropped_not_fatal(self) -> None:
        ev = parse_event(b'{"kind": "tool", "session": 42}')
        assert ev is not None
        self.assertEqual(ev.session, "")


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = ChatterConfig.from_env({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.gap_min, 40.0)
        self.assertEqual(cfg.voice_every, 1)
        self.assertTrue(str(cfg.socket_path).endswith("buddy/chatter.sock"))

    def test_disabled(self) -> None:
        self.assertFalse(ChatterConfig.from_env({"BUDDY_CHATTER": "0"}).enabled)
        self.assertFalse(ChatterConfig.from_env({"BUDDY_CHATTER": "false"}).enabled)

    def test_unparseable_values_fall_back(self) -> None:
        cfg = ChatterConfig.from_env({"BUDDY_CHATTER_GAP_MIN": "soon", "BUDDY_CHATTER_BATCH": ""})
        self.assertEqual(cfg.gap_min, 40.0)
        self.assertEqual(cfg.batch, 6)

    def test_the_saturation_rate_is_tunable(self) -> None:
        self.assertEqual(ChatterConfig.from_env({}).busy_rate, 12.0)
        self.assertEqual(ChatterConfig.from_env({"BUDDY_CHATTER_BUSY_RATE": "30"}).busy_rate, 30.0)

    def test_voice_every_cannot_be_zero(self) -> None:
        # 0 だと _transmit の剰余が毎行で例外になる。
        self.assertEqual(ChatterConfig.from_env({"BUDDY_CHATTER_VOICE_EVERY": "0"}).voice_every, 1)

    def test_sessions_are_read_by_default(self) -> None:
        cfg = ChatterConfig.from_env({"HOME": "/home/u"})
        self.assertTrue(cfg.sessions)
        self.assertEqual(cfg.session_limit, 3)
        self.assertEqual(cfg.session_ttl, 900.0)
        self.assertEqual(str(cfg.projects_path), "/home/u/.claude/projects")

    def test_sessions_can_be_turned_off(self) -> None:
        # デバイスが他所の作業を口にするのが嫌なときに、chatter ごと
        # 止めずに済ませられるように。
        for off in ("0", "false", "no"):
            with self.subTest(value=off):
                self.assertFalse(ChatterConfig.from_env({"BUDDY_CHATTER_SESSIONS": off}).sessions)

    def test_session_limits_are_tunable(self) -> None:
        cfg = ChatterConfig.from_env(
            {"BUDDY_CHATTER_SESSION_LIMIT": "5", "BUDDY_CHATTER_SESSION_TTL": "60"}
        )
        self.assertEqual(cfg.session_limit, 5)
        self.assertEqual(cfg.session_ttl, 60.0)

    def test_the_model_defaults_to_sonnet(self) -> None:
        # 独り言を 1 行書くのは最大のモデルを要する仕事ではないし、これは
        # セッション中ずっと 1 時間に何度も走る。
        self.assertEqual(ChatterConfig.from_env({}).model, "sonnet")

    def test_claude_cli_settings(self) -> None:
        cfg = ChatterConfig.from_env({})
        self.assertEqual(cfg.claude_bin, "claude")
        self.assertEqual(cfg.effort, "low")
        self.assertEqual(cfg.claude_timeout, 120.0)
        cfg = ChatterConfig.from_env(
            {
                "BUDDY_CHATTER_CLAUDE_BIN": "/opt/homebrew/bin/claude",
                "BUDDY_CHATTER_MODEL": "claude-haiku-4-5-20251001",
                "BUDDY_CHATTER_EFFORT": "medium",
                "BUDDY_CHATTER_CLAUDE_TIMEOUT": "45",
            }
        )
        self.assertEqual(cfg.claude_bin, "/opt/homebrew/bin/claude")
        self.assertEqual(cfg.model, "claude-haiku-4-5-20251001")
        self.assertEqual(cfg.effort, "medium")
        self.assertEqual(cfg.claude_timeout, 45.0)


if __name__ == "__main__":
    unittest.main()
