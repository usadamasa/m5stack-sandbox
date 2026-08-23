"""数値のラチェットが、緩む方向へ動かないことを見るテスト。

行数にも複雑度にも同じ仕掛けを使う。守るべきは 3 つ。`--update` が baseline
を上げないこと、縮んだ値を baseline に残したままにすると落ちること、
baseline に無い新顔がしきい値を超えたらその場で落ちること。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from ratchet import Finding, Ratchet, classify, load_baseline, next_baseline, save_baseline

THRESHOLD = 400


def _kinds(findings: list[Finding]) -> list[tuple[str, str]]:
    return [(f.kind, f.key) for f in findings]


class ClassifyTest(unittest.TestCase):
    def test_a_small_value_outside_the_baseline_is_silent(self) -> None:
        self.assertEqual(classify({"a": 10}, {}, THRESHOLD), [])

    def test_a_new_key_over_the_threshold_fails(self) -> None:
        self.assertEqual(_kinds(classify({"a": 401}, {}, THRESHOLD)), [("new", "a")])

    def test_a_value_at_the_threshold_is_not_over_it(self) -> None:
        self.assertEqual(classify({"a": THRESHOLD}, {}, THRESHOLD), [])

    def test_growing_past_the_baseline_fails(self) -> None:
        self.assertEqual(_kinds(classify({"a": 501}, {"a": 500}, THRESHOLD)), [("regression", "a")])

    def test_staying_at_the_baseline_passes(self) -> None:
        self.assertEqual(classify({"a": 500}, {"a": 500}, THRESHOLD), [])

    def test_shrinking_below_the_baseline_is_stale(self) -> None:
        # 取り込まないと baseline が前の値のまま残り、いつでもそこまで戻れる。
        self.assertEqual(_kinds(classify({"a": 450}, {"a": 500}, THRESHOLD)), [("stale", "a")])

    def test_shrinking_under_the_threshold_is_stale(self) -> None:
        self.assertEqual(_kinds(classify({"a": 300}, {"a": 500}, THRESHOLD)), [("stale", "a")])

    def test_a_baseline_entry_for_a_vanished_key_is_stale(self) -> None:
        self.assertEqual(_kinds(classify({}, {"a": 500}, THRESHOLD)), [("stale", "a")])

    def test_a_baseline_entry_under_the_threshold_is_stale(self) -> None:
        # 手で書き足したときにしか生まれない形。効果が無いので知らせる。
        self.assertEqual(_kinds(classify({"a": 300}, {"a": 350}, THRESHOLD)), [("stale", "a")])

    def test_findings_come_back_sorted_by_key(self) -> None:
        findings = classify({"z": 401, "a": 402}, {}, THRESHOLD)
        self.assertEqual([f.key for f in findings], ["a", "z"])

    def test_a_finding_says_both_numbers(self) -> None:
        (finding,) = classify({"a": 501}, {"a": 500}, THRESHOLD)
        self.assertIn("501", finding.message())
        self.assertIn("500", finding.message())

    def test_the_unit_rides_along(self) -> None:
        (finding,) = classify({"a": 401}, {}, THRESHOLD)
        self.assertIn("401 行", finding.message("行"))


class NextBaselineTest(unittest.TestCase):
    def test_an_entry_is_lowered_to_the_current_value(self) -> None:
        self.assertEqual(next_baseline({"a": 450}, {"a": 500}, THRESHOLD), {"a": 450})

    def test_an_entry_is_never_raised(self) -> None:
        # 回帰を `--update` で黙らせられるなら、ラチェットは無いのと同じ。
        self.assertEqual(next_baseline({"a": 600}, {"a": 500}, THRESHOLD), {"a": 500})

    def test_an_entry_that_fell_under_the_threshold_is_dropped(self) -> None:
        self.assertEqual(next_baseline({"a": 300}, {"a": 500}, THRESHOLD), {})

    def test_an_entry_for_a_vanished_key_is_dropped(self) -> None:
        self.assertEqual(next_baseline({}, {"a": 500}, THRESHOLD), {})

    def test_a_new_violator_is_not_adopted_by_default(self) -> None:
        self.assertEqual(next_baseline({"a": 500}, {}, THRESHOLD), {})

    def test_adopt_takes_in_a_new_violator(self) -> None:
        self.assertEqual(next_baseline({"a": 500}, {}, THRESHOLD, adopt=True), {"a": 500})

    def test_adopt_still_does_not_raise_an_existing_entry(self) -> None:
        self.assertEqual(next_baseline({"a": 600}, {"a": 500}, THRESHOLD, adopt=True), {"a": 500})


class BaselineFileTest(unittest.TestCase):
    def test_a_missing_baseline_reads_as_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(load_baseline(Path(tmp) / "nope.json"), {})

    def test_what_is_saved_comes_back(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            save_baseline(path, {"z": 500, "a": 600})
            self.assertEqual(load_baseline(path), {"z": 500, "a": 600})

    def test_keys_are_written_sorted(self) -> None:
        # 順番だけが変わった差分を PR に混ぜないため。
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            save_baseline(path, {"z": 500, "a": 600})
            self.assertEqual(list(json.loads(path.read_text())), ["a", "z"])

    def test_the_file_ends_with_a_newline(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            save_baseline(path, {"a": 600})
            self.assertTrue(path.read_text().endswith("\n"))


class RunTest(unittest.TestCase):
    def _run(
        self,
        values: dict[str, int],
        baseline: dict[str, int],
        **kwargs: bool,
    ) -> tuple[int, str, Path]:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            save_baseline(path, baseline)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = Ratchet(path, THRESHOLD, fixer="poe lines").run(values, **kwargs)
            return code, buffer.getvalue(), path

    def _persisted(
        self,
        values: dict[str, int],
        baseline: dict[str, int],
        **kwargs: bool,
    ) -> tuple[int, dict[str, int]]:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            save_baseline(path, baseline)
            with redirect_stdout(io.StringIO()):
                code = Ratchet(path, THRESHOLD, fixer="poe lines").run(values, **kwargs)
            return code, load_baseline(path)

    def test_a_clean_set_exits_zero(self) -> None:
        code, _, _ = self._run({"a": 10}, {})
        self.assertEqual(code, 0)

    def test_a_violation_exits_nonzero(self) -> None:
        code, _, _ = self._run({"a": 500}, {})
        self.assertEqual(code, 1)

    def test_update_writes_the_lowered_baseline_and_exits_zero(self) -> None:
        self.assertEqual(self._persisted({"a": 450}, {"a": 500}, update=True), (0, {"a": 450}))

    def test_update_leaves_a_regression_failing(self) -> None:
        # `--update` を回帰の逃げ道にしない。baseline は据え置かれる。
        self.assertEqual(self._persisted({"a": 600}, {"a": 500}, update=True), (1, {"a": 500}))

    def test_adopt_takes_in_the_new_violator_and_exits_zero(self) -> None:
        self.assertEqual(self._persisted({"a": 500}, {}, adopt=True), (0, {"a": 500}))

    def test_a_new_violator_is_pointed_at_adopt(self) -> None:
        _, out, _ = self._run({"a": 500}, {})
        self.assertIn("--adopt", out)

    def test_a_regression_is_not_pointed_at_adopt(self) -> None:
        # `--adopt` は既存エントリを上げないので、勧めても直らない。
        _, out, _ = self._run({"a": 600}, {"a": 500})
        self.assertNotIn("--adopt", out)

    def test_a_stale_entry_is_pointed_at_update(self) -> None:
        _, out, _ = self._run({"a": 450}, {"a": 500})
        self.assertIn("--update", out)

    def test_the_fixer_names_the_command(self) -> None:
        _, out, _ = self._run({"a": 450}, {"a": 500})
        self.assertIn("poe lines", out)


if __name__ == "__main__":
    unittest.main()
