"""Claude Code の transcript から、セッションの要約を読む側のテスト。

実機も Claude Code も要らない。入力は tmp に書いた jsonl だけ。fixture を
その場で組み立てるのは、末尾読みの境界 — 先頭で切れた行、壊れた行、同じ
種別が何度も出てくる — が、この読み取りで唯一きわどいところだから。
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import chatter_sessions
from chatter_sessions import SessionNote, SessionRegistry, read_note

SESSION = "747883a7-180d-453a-9f99-b06b38767561"
OTHER = "fe5b9947-4564-406e-b8bf-67c386683f6f"


def _lines(*entries: dict[str, Any]) -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)


def _transcript(
    root: Path,
    session: str = SESSION,
    project: str = "-Users-u-src-twigpui",
    body: str | None = None,
) -> Path:
    """1 セッション分の transcript を、本物と同じ階層に置く。"""
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text(
        _lines(
            {"type": "user", "cwd": "/Users/u/src/twigpui", "gitBranch": "main"},
            {"type": "ai-title", "aiTitle": "APIクォータ超過の特定と修正"},
            {"type": "last-prompt", "lastPrompt": "マージして"},
        )
        if body is None
        else body,
        encoding="utf-8",
    )
    return path


class ReadNoteTests(unittest.TestCase):
    def test_reads_the_four_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            note = read_note(_transcript(Path(tmp)), SESSION)
            assert note is not None
            self.assertEqual(note.title, "APIクォータ超過の特定と修正")
            self.assertEqual(note.prompt, "マージして")
            self.assertEqual(note.project, "twigpui")
            self.assertEqual(note.branch, "main")

    def test_the_last_of_each_kind_wins(self) -> None:
        # transcript は追記されるだけなので、そのセッションの今を知りたければ
        # 後ろから読む。タイトルはセッション中に何度も書き直される。
        with TemporaryDirectory() as tmp:
            body = _lines(
                {"type": "ai-title", "aiTitle": "古いほう"},
                {"type": "user", "cwd": "/Users/u/src/twigpui", "gitBranch": "main"},
                {"type": "ai-title", "aiTitle": "新しいほう"},
                {"type": "last-prompt", "lastPrompt": "あとの指示"},
            )
            note = read_note(_transcript(Path(tmp), body=body), SESSION)
            assert note is not None
            self.assertEqual(note.title, "新しいほう")
            self.assertEqual(note.prompt, "あとの指示")

    def test_a_worktree_reports_the_repository_it_belongs_to(self) -> None:
        # worktree の cwd の末尾はブランチ名なので、そのままではプロジェクトを
        # 名乗らない。同じリポジトリの worktree が 3 つ動いていても、どれが
        # どれか分かる形にする。
        with TemporaryDirectory() as tmp:
            body = _lines(
                {
                    "type": "user",
                    "cwd": "/Users/u/src/twigpui/.claude/worktrees/poll-denial",
                    "gitBranch": "worktree-poll-denial",
                }
            )
            note = read_note(_transcript(Path(tmp), body=body), SESSION)
            assert note is not None
            self.assertEqual(note.project, "twigpui")
            self.assertEqual(note.branch, "worktree-poll-denial")

    def test_broken_lines_are_stepped_over(self) -> None:
        with TemporaryDirectory() as tmp:
            body = "{not json\n" + _lines({"type": "ai-title", "aiTitle": "読めたほう"}) + "[]\n"
            note = read_note(_transcript(Path(tmp), body=body), SESSION)
            assert note is not None
            self.assertEqual(note.title, "読めたほう")

    def test_only_the_tail_is_read(self) -> None:
        # 本物は数 MB になる。毎バッチ全部読むのは、独り言 1 行のためには
        # 高すぎる。
        with TemporaryDirectory() as tmp:
            filler = _lines(*({"type": "assistant", "pad": "x" * 500},) * 400)
            body = filler + _lines(
                {"type": "ai-title", "aiTitle": "末尾にある"},
                {"type": "user", "cwd": "/Users/u/src/twigpui", "gitBranch": "main"},
            )
            path = _transcript(Path(tmp), body=body)
            self.assertGreater(path.stat().st_size, chatter_sessions.TAIL_BYTES)
            note = read_note(path, SESSION)
            assert note is not None
            self.assertEqual(note.title, "末尾にある")

    def test_a_line_cut_in_half_by_the_tail_is_dropped(self) -> None:
        # seek した先はほぼ確実に行の途中。その断片は JSON として壊れている
        # だけでなく、運が悪ければ壊れていない別の何かに見えうる。
        with TemporaryDirectory() as tmp:
            body = _lines(
                {"type": "ai-title", "aiTitle": "x" * 200},
                {"type": "ai-title", "aiTitle": "残るほう"},
            )
            path = _transcript(Path(tmp), body=body)
            note = read_note(path, SESSION, limit=120)
            assert note is not None
            self.assertEqual(note.title, "残るほう")

    def test_a_missing_file_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(read_note(Path(tmp) / "nope.jsonl", SESSION))

    def test_an_empty_transcript_reads_as_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(read_note(_transcript(Path(tmp), body=""), SESSION))

    def test_long_values_are_clamped(self) -> None:
        # パネルに出て読み上げられる。向こう側の `clean` も切るが、切られる
        # 前に 1 行 4000 字がプロンプトへ貼られる理由が無い。
        with TemporaryDirectory() as tmp:
            body = _lines(
                {"type": "ai-title", "aiTitle": "あ" * 400},
                {"type": "last-prompt", "lastPrompt": "い" * 400},
            )
            note = read_note(_transcript(Path(tmp), body=body), SESSION)
            assert note is not None
            self.assertLessEqual(len(note.title), 60)
            self.assertLessEqual(len(note.prompt), 80)

    def test_newlines_in_a_prompt_are_flattened(self) -> None:
        with TemporaryDirectory() as tmp:
            body = _lines({"type": "last-prompt", "lastPrompt": "まず\n  これを\nやって"})
            note = read_note(_transcript(Path(tmp), body=body), SESSION)
            assert note is not None
            self.assertEqual(note.prompt, "まず これを やって")

    def test_a_non_string_value_is_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            body = _lines({"type": "ai-title", "aiTitle": 42}, {"type": "last-prompt"})
            note = read_note(_transcript(Path(tmp), body=body), SESSION)
            self.assertIsNone(note)


class DescribeTests(unittest.TestCase):
    def test_a_note_reads_as_one_line(self) -> None:
        note = SessionNote(SESSION, title="deploy を直す", prompt="やって", project="p", branch="b")
        line = note.describe()
        self.assertNotIn("\n", line)
        self.assertIn("deploy を直す", line)
        self.assertIn("p", line)

    def test_the_parts_that_are_missing_are_left_out(self) -> None:
        note = SessionNote(SESSION, title="なにか")
        self.assertEqual(note.describe(), "なにか")


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.now = 0.0
        self.addCleanup(self._tmp.cleanup)

    def _clock(self) -> float:
        return self.now

    def _registry(self, ttl: float = 900.0, limit: int = 3) -> SessionRegistry:
        return SessionRegistry(self.root, ttl=ttl, limit=limit, clock=self._clock)

    def test_finds_a_transcript_under_any_project(self) -> None:
        _transcript(self.root)
        registry = self._registry()
        registry.note_activity(SESSION)
        notes = registry.recent()
        self.assertEqual([n.session for n in notes], [SESSION])

    def test_a_session_with_no_transcript_is_skipped(self) -> None:
        registry = self._registry()
        registry.note_activity(SESSION)
        self.assertEqual(registry.recent(), [])

    def test_the_most_recently_active_comes_first(self) -> None:
        _transcript(self.root, SESSION, project="-a")
        _transcript(self.root, OTHER, project="-b")
        registry = self._registry()
        registry.note_activity(SESSION)
        self.now = 1.0
        registry.note_activity(OTHER)
        self.assertEqual([n.session for n in registry.recent()], [OTHER, SESSION])

    def test_a_session_that_went_quiet_drops_out(self) -> None:
        # 昨日の作業を今の話として喋られても困る。
        _transcript(self.root)
        registry = self._registry(ttl=100.0)
        registry.note_activity(SESSION)
        self.now = 101.0
        self.assertEqual(registry.recent(), [])

    def test_only_so_many_come_back(self) -> None:
        for i, session in enumerate((SESSION, OTHER)):
            _transcript(self.root, session, project=f"-p{i}")
        registry = self._registry(limit=1)
        registry.note_activity(SESSION)
        self.now = 1.0
        registry.note_activity(OTHER)
        self.assertEqual([n.session for n in registry.recent()], [OTHER])

    def test_a_transcript_that_moved_on_is_read_again(self) -> None:
        path = _transcript(self.root)
        registry = self._registry()
        registry.note_activity(SESSION)
        self.assertEqual(registry.recent()[0].title, "APIクォータ超過の特定と修正")
        path.write_text(_lines({"type": "ai-title", "aiTitle": "次の話"}), encoding="utf-8")
        # mtime の分解能より速く書き換わりうるので、大きさが動かなくても
        # 気づけることまでは求めない。ここで見たいのは、キャッシュが
        # 永久に固まらないこと。
        self.now = 1000.0
        registry.note_activity(SESSION)
        self.assertEqual(registry.recent()[0].title, "次の話")

    def test_a_session_id_that_is_not_a_uuid_never_reaches_the_glob(self) -> None:
        # `parse_event` が先に弾くので、ここは二重の防御。パスを組み立てる
        # 側にも同じ検問を置いておく — この registry を呼ぶ経路が今後
        # 増えても、そこで検め直さずに済むように。
        registry = self._registry()
        for bad in ("../../etc", "*", "", "not-a-uuid", f"{SESSION}/x"):
            with self.subTest(session=bad):
                registry.note_activity(bad)
        self.assertEqual(registry.recent(), [])

    def test_it_forgets_sessions_that_stopped_firing(self) -> None:
        # daemon はセッションより長く生きる。時間で落ちない台帳は、
        # そのまま増え続ける。
        _transcript(self.root)
        registry = self._registry(ttl=10.0)
        registry.note_activity(SESSION)
        self.now = 100.0
        registry.recent()
        self.assertEqual(registry.tracked, 0)


if __name__ == "__main__":
    unittest.main()
