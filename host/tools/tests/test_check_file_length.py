"""行数を数えて集める部分のテスト。ラチェットそのものは test_ratchet.py。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from check_file_length import count_lines, discover, main
from ratchet import load_baseline, save_baseline


class CountLinesTest(unittest.TestCase):
    def test_counts_the_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            _ = path.write_text("one\ntwo\nthree\n")
            self.assertEqual(count_lines(path), 3)

    def test_a_last_line_without_a_newline_still_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            _ = path.write_text("one\ntwo")
            self.assertEqual(count_lines(path), 2)

    def test_an_empty_file_is_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            _ = path.write_text("")
            self.assertEqual(count_lines(path), 0)


class DiscoverTest(unittest.TestCase):
    def test_paths_come_back_relative_to_the_root_with_their_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            _ = (root / "pkg" / "a.py").write_text("x\n" * 3)
            self.assertEqual(
                discover(root, list_files=lambda r: [r / "pkg" / "a.py"]),
                {"pkg/a.py": 3},
            )


def _tree(tmp: str, files: dict[str, int], baseline: dict[str, int]) -> Path:
    root = Path(tmp)
    for name, lines in files.items():
        _ = (root / name).write_text("x\n" * lines)
    save_baseline(root / "file-length-baseline.json", baseline)
    return root


def _flat(root: Path) -> list[Path]:
    return sorted(root.glob("*.py"))


class MainTest(unittest.TestCase):
    """CLI としての口。列挙だけ差し替えて、git の下でなくても走らせる。"""

    def _main(self, root: Path, *args: str) -> int:
        return main(["--root", str(root), *args], list_files=_flat)

    def test_a_clean_tree_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, {"a.py": 10}, {})
            self.assertEqual(self._main(root), 0)

    def test_a_violation_exits_nonzero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, {"a.py": 500}, {})
            self.assertEqual(self._main(root), 1)

    def test_update_writes_the_lowered_baseline(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, {"a.py": 450}, {"a.py": 500})
            self.assertEqual(self._main(root, "--update"), 0)
            self.assertEqual(load_baseline(root / "file-length-baseline.json"), {"a.py": 450})

    def test_the_threshold_is_overridable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, {"a.py": 500}, {})
            self.assertEqual(self._main(root, "--threshold", "600"), 0)


if __name__ == "__main__":
    unittest.main()
