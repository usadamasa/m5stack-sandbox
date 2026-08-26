"""台詞を書く側のテスト。

CLI を実際に起こすことは無い。`subprocess.run` を差し替えるか、`run` を注入
する。ネットワークも実機も出てこない。
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

import chatter_core
import chatter_lines
from chatter_core import DEFAULT_PROMPT_PATH, ChatterConfig, Event, describe
from chatter_lines import ClaudeCliLineSource


class FakeClaude:
    """Claude CLI を起こす `subprocess.run` の代役。

    これを書いた時点で CLI が実際に出していた形で stdout に答える: ストリーム
    イベントのリストで、最後の `result` エントリが `structured_output` を
    持つもの。その形をパースする部分こそ固定する価値があるので、モックで
    消してしまわずにここで再現している。
    """

    def __init__(
        self,
        payload: dict[str, Any] | None,
        returncode: int = 0,
        stdout: str | None = None,
    ) -> None:
        self.payload = payload
        self.returncode = returncode
        self._stdout = stdout
        self.argv: list[str] = []
        self.stdin = ""
        self.kwargs: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401 — subprocess のそれ
        self.argv = argv
        self.stdin = str(kwargs.get("input", ""))
        self.kwargs = kwargs
        if self._stdout is not None:
            out = self._stdout
        elif self.payload is None:
            out = ""
        else:
            out = json.dumps(
                [
                    {"type": "system", "subtype": "init"},
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": json.dumps(self.payload, ensure_ascii=False),
                        "structured_output": self.payload,
                    },
                ],
                ensure_ascii=False,
            )
        return SimpleNamespace(returncode=self.returncode, stdout=out, stderr="not logged in")


def claude_source(cfg: ChatterConfig, payload: dict[str, Any]) -> ClaudeCliLineSource:
    """CLI を決め打ちの答えに差し替えた source。"""
    return ClaudeCliLineSource(
        cfg, run=lambda _system, _prompt: json.dumps({"structured_output": payload})
    )


class PromptFileTests(unittest.TestCase):
    """ペルソナはファイルの中の散文なので、編集に Python が要ってはいけない。"""

    def test_the_prompt_is_read_from_the_configured_path(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "persona.md"
            path.write_text("きみは猫である。\n", encoding="utf-8")
            seen: list[str] = []

            def run(system: str, _prompt: str) -> str:
                seen.append(system)
                return json.dumps({"structured_output": {"lines": ["にゃあ"]}})

            source = ClaudeCliLineSource(ChatterConfig(prompt_path=path), run=run)
            source.next_line([])
            self.assertEqual(seen[0], "きみは猫である。\n")

    def test_the_shipped_prompt_exists(self) -> None:
        # 既定のものは package data。これを取りこぼすリネームは、全セッション
        # が決め打ちの台詞へフォールバックするという形でしか表に出ない。
        self.assertTrue(DEFAULT_PROMPT_PATH.is_file(), DEFAULT_PROMPT_PATH)
        self.assertTrue(DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip())

    def test_a_missing_prompt_is_a_counted_failure_not_a_crash(self) -> None:
        source = claude_source(
            ChatterConfig(prompt_path=Path("/nope/persona.md")), {"lines": ["x"]}
        )
        line = source.next_line([])
        assert line is not None
        self.assertTrue(line, "it should still say something")
        self.assertEqual(source.failures, 1)
        self.assertIn("persona.md", source.last_error)


class LineSourceTests(unittest.TestCase):
    def test_a_generation_failure_falls_back_rather_than_going_silent(self) -> None:
        source = ClaudeCliLineSource(ChatterConfig(claude_bin="/nope/claude"))
        line = source.next_line([Event("tool", "Read")])
        assert line is not None
        self.assertTrue(line)
        self.assertEqual(source.failures, 1)
        self.assertTrue(source.last_error)

    def test_a_batch_is_parsed_and_handed_out_one_line_at_a_time(self) -> None:
        calls: list[str] = []

        def run(_system: str, prompt: str) -> str:
            calls.append(prompt)
            return json.dumps({"structured_output": {"lines": ["いち なのだ", "に なのだ"]}})

        source = ClaudeCliLineSource(ChatterConfig(), run=run)
        self.assertEqual(source.next_line([Event("tool", "Read")]), "いち なのだ")
        self.assertEqual(source.next_line([]), "に なのだ")
        self.assertEqual(len(calls), 1, "the second line should come from the same batch")
        self.assertEqual(source.generated, 2)

    def test_overlong_and_multiline_output_is_cut_to_one_panel(self) -> None:
        cfg = ChatterConfig(max_chars=10)
        source = claude_source(cfg, {"lines": ["あ" * 40, "改行\nを\n含む", "", 12345]})
        self.assertEqual(source.next_line([]), "あ" * 10)
        self.assertEqual(source.next_line([]), "改行 を 含む")
        self.assertEqual(source.next_line([]), "12345", "a non-string must not raise")

    def test_describe_renders_events_for_the_prompt(self) -> None:
        self.assertEqual(describe([]), "まだ何も起きていない。")
        rendered = describe([Event("tool", "Bash"), Event("idle")])
        self.assertEqual(rendered, "- tool: Bash\n- idle")


class ClaudeCliLineSourceTests(unittest.TestCase):
    """Claude Code のバックエンド。CLI を実際に走らせることは無い。"""

    def test_the_cli_is_invoked_without_a_session_or_a_workspace(self) -> None:
        fake = FakeClaude({"lines": ["うむ なのだ"]})
        cfg = ChatterConfig(claude_bin="claude-x", model="haiku", effort="medium")
        source = ClaudeCliLineSource(cfg)
        with mock.patch.object(chatter_lines.subprocess, "run", fake):
            self.assertEqual(source.next_line([Event("stop")]), "うむ なのだ")
        self.assertEqual(fake.argv[0], "claude-x")
        self.assertIn("-p", fake.argv)
        self.assertEqual(fake.argv[fake.argv.index("--model") + 1], "haiku")
        self.assertEqual(fake.argv[fake.argv.index("--effort") + 1], "medium")
        # このリポジトリ自身の hook を読み込めるターンは、生成の相手である
        # chatter へデータグラムを送ってしまう。セッションを残すターンは、
        # 独り言 1 回ごとにトランスクリプトを残す。
        self.assertIn("--safe-mode", fake.argv)
        self.assertIn("--no-session-persistence", fake.argv)
        self.assertEqual(fake.argv[fake.argv.index("--tools") + 1], "")
        self.assertEqual(fake.argv[fake.argv.index("--output-format") + 1], "json")
        schema = json.loads(fake.argv[fake.argv.index("--json-schema") + 1])
        self.assertEqual(schema, chatter_core.LINES_SCHEMA)
        self.assertEqual(fake.kwargs["timeout"], 120.0)

    def test_the_persona_is_the_system_prompt_and_the_events_are_stdin(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "persona.md"
            path.write_text("きみは猫である。\n", encoding="utf-8")
            fake = FakeClaude({"lines": ["にゃあ"]})
            with mock.patch.object(chatter_lines.subprocess, "run", fake):
                ClaudeCliLineSource(ChatterConfig(prompt_path=path)).next_line(
                    [Event("tool", "Bash")]
                )
        self.assertEqual(fake.argv[fake.argv.index("--system-prompt") + 1], "きみは猫である。\n")
        # 引数の列ではなく stdin。出来事も過去の台詞もセッションとともに
        # 伸びるが、argv は伸びない。
        self.assertIn("- tool: Bash", fake.stdin)
        self.assertIn("独り言", fake.stdin)

    def test_the_scratch_directory_does_not_outlive_the_turn(self) -> None:
        fake = FakeClaude({"lines": ["ほい"]})
        with mock.patch.object(chatter_lines.subprocess, "run", fake):
            ClaudeCliLineSource(ChatterConfig()).next_line([])
        self.assertFalse(Path(fake.kwargs["cwd"]).exists())

    def test_a_single_result_object_parses_too(self) -> None:
        # `--output-format json` はオブジェクト 1 つと文書化されているが、
        # ストリーム全体をリストで出すのも観測されている。両方を読む。
        payload = {"type": "result", "structured_output": {"lines": ["ひとつ なのだ"]}}
        fake = FakeClaude(None, stdout=json.dumps(payload, ensure_ascii=False))
        with mock.patch.object(chatter_lines.subprocess, "run", fake):
            self.assertEqual(ClaudeCliLineSource(ChatterConfig()).next_line([]), "ひとつ なのだ")

    def test_the_result_text_is_read_when_no_structured_output_came_back(self) -> None:
        payload = {"type": "result", "result": json.dumps({"lines": ["もじれつ なのだ"]})}
        fake = FakeClaude(None, stdout=json.dumps(payload, ensure_ascii=False))
        with mock.patch.object(chatter_lines.subprocess, "run", fake):
            self.assertEqual(ClaudeCliLineSource(ChatterConfig()).next_line([]), "もじれつ なのだ")

    def test_a_turn_that_reports_an_error_is_a_counted_failure(self) -> None:
        payload = {"type": "result", "is_error": True, "result": "Credit balance is too low"}
        fake = FakeClaude(None, stdout=json.dumps(payload))
        source = ClaudeCliLineSource(ChatterConfig())
        with mock.patch.object(chatter_lines.subprocess, "run", fake):
            line = source.next_line([])
        assert line is not None
        self.assertTrue(line, "it should still say something")
        self.assertEqual(source.failures, 1)
        self.assertIn("Credit balance", source.last_error)

    def test_a_turn_that_writes_nothing_is_a_counted_failure(self) -> None:
        fake = FakeClaude(None, returncode=1)
        source = ClaudeCliLineSource(ChatterConfig())
        with mock.patch.object(chatter_lines.subprocess, "run", fake):
            line = source.next_line([])
        assert line is not None
        self.assertTrue(line)
        self.assertEqual(source.failures, 1)
        # 理由は status のフィールドまで残らないといけない。黙って決め打ちの
        # 台詞へ落ちた chatter は、単に口数が少ない chatter に見えるため。
        self.assertIn("not logged in", source.last_error)

    def test_no_effort_flag_when_the_session_default_should_stand(self) -> None:
        fake = FakeClaude({"lines": ["ほい"]})
        with mock.patch.object(chatter_lines.subprocess, "run", fake):
            ClaudeCliLineSource(ChatterConfig(effort="")).next_line([])
        self.assertNotIn("--effort", fake.argv)

    def test_the_model_it_will_ask_is_reportable(self) -> None:
        self.assertEqual(ClaudeCliLineSource(ChatterConfig(model="sonnet")).model, "sonnet")


if __name__ == "__main__":
    unittest.main()
