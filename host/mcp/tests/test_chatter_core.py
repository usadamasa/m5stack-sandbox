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
