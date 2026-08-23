"""行数のラチェットが、緩む方向へ動かないことを見るテスト。

この仕組みの値打ちは「今より悪くならない」の一点にある。しきい値を超えた
ファイルは baseline に現在値で載り、そこから増やせない。減らしたぶんは
`--update` が取り込んで、二度と戻れなくなる。

だから守るべき性質は 3 つ。`--update` が baseline を上げないこと、縮んだ
ファイルを放置すると stale として落ちること、そして baseline に無い新顔が
しきい値を超えたらその場で落ちること。逆に言えば、行数を数える部分の
細かい挙動はここでは大して重要ではない。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from check_file_length import (
    Finding,
    classify,
    count_lines,
    discover,
    load_baseline,
    main,
    next_baseline,
    save_baseline,
)

THRESHOLD = 400


def _kinds(findings: list[Finding]) -> list[tuple[str, str]]:
    return [(f.kind, f.path) for f in findings]


class ClassifyTest(unittest.TestCase):
    def test_a_small_file_outside_the_baseline_is_silent(self) -> None:
        findings = classify({"a.py": 10}, {}, THRESHOLD)
        self.assertEqual(findings, [])

    def test_a_new_file_over_the_threshold_fails(self) -> None:
        findings = classify({"a.py": 401}, {}, THRESHOLD)
        self.assertEqual(_kinds(findings), [("new", "a.py")])

    def test_a_file_at_the_threshold_is_not_over_it(self) -> None:
        findings = classify({"a.py": THRESHOLD}, {}, THRESHOLD)
        self.assertEqual(findings, [])

    def test_growing_past_the_baseline_fails(self) -> None:
        findings = classify({"a.py": 501}, {"a.py": 500}, THRESHOLD)
        self.assertEqual(_kinds(findings), [("regression", "a.py")])

    def test_staying_at_the_baseline_passes(self) -> None:
        findings = classify({"a.py": 500}, {"a.py": 500}, THRESHOLD)
        self.assertEqual(findings, [])

    def test_shrinking_below_the_baseline_is_stale(self) -> None:
        # 落とすのは意地悪ではなく、ここで取り込まないとラチェットが
        # 前の値のまま残り、いつでもそこまで戻れてしまうため。
        findings = classify({"a.py": 450}, {"a.py": 500}, THRESHOLD)
        self.assertEqual(_kinds(findings), [("stale", "a.py")])

    def test_shrinking_under_the_threshold_is_stale(self) -> None:
        findings = classify({"a.py": 300}, {"a.py": 500}, THRESHOLD)
        self.assertEqual(_kinds(findings), [("stale", "a.py")])

    def test_a_baseline_entry_for_a_deleted_file_is_stale(self) -> None:
        findings = classify({}, {"a.py": 500}, THRESHOLD)
        self.assertEqual(_kinds(findings), [("stale", "a.py")])

    def test_a_baseline_entry_under_the_threshold_is_stale(self) -> None:
        # 手で書き足したときにしか生まれない形。しきい値以下の値を
        # 持たせても効果は無いので、残っていることを知らせる。
        findings = classify({"a.py": 300}, {"a.py": 350}, THRESHOLD)
        self.assertEqual(_kinds(findings), [("stale", "a.py")])

    def test_findings_come_back_sorted_by_path(self) -> None:
        findings = classify(
            {"z.py": 401, "a.py": 402},
            {},
            THRESHOLD,
        )
        self.assertEqual([f.path for f in findings], ["a.py", "z.py"])

    def test_a_finding_says_both_numbers(self) -> None:
        (finding,) = classify({"a.py": 501}, {"a.py": 500}, THRESHOLD)
        self.assertIn("501", finding.message())
        self.assertIn("500", finding.message())


class NextBaselineTest(unittest.TestCase):
    def test_an_entry_is_lowered_to_the_current_count(self) -> None:
        self.assertEqual(
            next_baseline({"a.py": 450}, {"a.py": 500}, THRESHOLD),
            {"a.py": 450},
        )

    def test_an_entry_is_never_raised(self) -> None:
        # ここが緩みの入口。回帰を `--update` で黙らせられるなら、
        # ラチェットは無いのと同じになる。
        self.assertEqual(
            next_baseline({"a.py": 600}, {"a.py": 500}, THRESHOLD),
            {"a.py": 500},
        )

    def test_an_entry_that_fell_under_the_threshold_is_dropped(self) -> None:
        self.assertEqual(next_baseline({"a.py": 300}, {"a.py": 500}, THRESHOLD), {})

    def test_an_entry_for_a_deleted_file_is_dropped(self) -> None:
        self.assertEqual(next_baseline({}, {"a.py": 500}, THRESHOLD), {})

    def test_a_new_violator_is_not_adopted_by_default(self) -> None:
        self.assertEqual(next_baseline({"a.py": 500}, {}, THRESHOLD), {})

    def test_adopt_takes_in_a_new_violator(self) -> None:
        self.assertEqual(
            next_baseline({"a.py": 500}, {}, THRESHOLD, adopt=True),
            {"a.py": 500},
        )

    def test_adopt_still_does_not_raise_an_existing_entry(self) -> None:
        self.assertEqual(
            next_baseline({"a.py": 600}, {"a.py": 500}, THRESHOLD, adopt=True),
            {"a.py": 500},
        )


class BaselineFileTest(unittest.TestCase):
    def test_a_missing_baseline_reads_as_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(load_baseline(Path(tmp) / "nope.json"), {})

    def test_what_is_saved_comes_back(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            save_baseline(path, {"z.py": 500, "a.py": 600})
            self.assertEqual(load_baseline(path), {"z.py": 500, "a.py": 600})

    def test_keys_are_written_sorted(self) -> None:
        # diff を読める形に保つため。dict の並び順がそのまま出ると、
        # 順番だけが変わった差分が PR に混ざる。
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            save_baseline(path, {"z.py": 500, "a.py": 600})
            self.assertEqual(list(json.loads(path.read_text())), ["a.py", "z.py"])

    def test_the_file_ends_with_a_newline(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            save_baseline(path, {"a.py": 600})
            self.assertTrue(path.read_text().endswith("\n"))


class CountLinesTest(unittest.TestCase):
    def test_counts_the_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            path.write_text("one\ntwo\nthree\n")
            self.assertEqual(count_lines(path), 3)

    def test_a_last_line_without_a_newline_still_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            path.write_text("one\ntwo")
            self.assertEqual(count_lines(path), 2)

    def test_an_empty_file_is_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            path.write_text("")
            self.assertEqual(count_lines(path), 0)


class DiscoverTest(unittest.TestCase):
    def test_paths_come_back_relative_to_the_root_with_their_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text("x\n" * 3)
            self.assertEqual(
                discover(root, list_files=lambda r: [r / "pkg" / "a.py"]),
                {"pkg/a.py": 3},
            )


class MainTest(unittest.TestCase):
    """CLI としての口。列挙だけ差し替えて、git の下でなくても走らせる。"""

    def _tree(self, tmp: str, files: dict[str, int], baseline: dict[str, int]) -> Path:
        root = Path(tmp)
        for name, lines in files.items():
            (root / name).write_text("x\n" * lines)
        save_baseline(root / "file-length-baseline.json", baseline)
        return root

    def _main(self, root: Path, *args: str) -> int:
        return main(
            ["--root", str(root), *args],
            list_files=lambda r: sorted(r.glob("*.py")),
        )

    def test_a_clean_tree_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"a.py": 10}, {})
            self.assertEqual(self._main(root), 0)

    def test_a_violation_exits_nonzero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"a.py": 500}, {})
            self.assertEqual(self._main(root), 1)

    def test_update_writes_the_lowered_baseline_and_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"a.py": 450}, {"a.py": 500})
            self.assertEqual(self._main(root, "--update"), 0)
            self.assertEqual(load_baseline(root / "file-length-baseline.json"), {"a.py": 450})

    def test_update_leaves_a_regression_failing(self) -> None:
        # `--update` を回帰の逃げ道にしない。書き戻しても baseline は
        # 据え置かれ、終了コードは 1 のまま。
        with TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"a.py": 600}, {"a.py": 500})
            self.assertEqual(self._main(root, "--update"), 1)
            self.assertEqual(load_baseline(root / "file-length-baseline.json"), {"a.py": 500})

    def test_adopt_takes_in_the_new_violator_and_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"a.py": 500}, {})
            self.assertEqual(self._main(root, "--adopt"), 0)
            self.assertEqual(load_baseline(root / "file-length-baseline.json"), {"a.py": 500})

    def test_the_threshold_is_overridable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"a.py": 500}, {})
            self.assertEqual(self._main(root, "--threshold", "600"), 0)


if __name__ == "__main__":
    unittest.main()
