"""関数ごとの複雑度を集める部分のテスト。ラチェットそのものは test_ratchet.py。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from check_complexity import main, measure
from ratchet import load_baseline, save_baseline


def _tree(tmp: str, files: dict[str, str], baseline: dict[str, int]) -> Path:
    root = Path(tmp)
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(source)
    save_baseline(root / "complexity-baseline.json", baseline)
    return root


def _flat(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


class MeasureTest(unittest.TestCase):
    def _measure(self, files: dict[str, str]) -> dict[str, int]:
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, files, {})
            return measure(root, list_files=_flat)

    def test_a_function_is_keyed_by_path_and_name(self) -> None:
        values = self._measure({"a.py": "def f():\n    return 1\n"})
        self.assertEqual(values, {"a.py::f": 1})

    def test_a_method_carries_its_class(self) -> None:
        values = self._measure({"a.py": "class C:\n    def m(self):\n        return 1\n"})
        self.assertEqual(list(values), ["a.py::C.m"])

    def test_the_value_is_the_complexity(self) -> None:
        values = self._measure({"a.py": "def f(x):\n    if x:\n        return 1\n    return 2\n"})
        self.assertEqual(values["a.py::f"], 2)

    def test_tests_are_measured_too(self) -> None:
        # 行数のラチェットと同じで、テストコードも読めなくなる。
        values = self._measure({"tests/test_a.py": "def test_x():\n    pass\n"})
        self.assertEqual(list(values), ["tests/test_a.py::test_x"])

    def test_a_module_without_functions_contributes_nothing(self) -> None:
        self.assertEqual(self._measure({"a.py": "X = 1\n"}), {})


class MainTest(unittest.TestCase):
    def _main(self, root: Path, *args: str) -> int:
        return main(["--root", str(root), *args], list_files=_flat)

    def test_a_simple_tree_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, {"a.py": "def f():\n    return 1\n"}, {})
            self.assertEqual(self._main(root), 0)

    def test_a_complex_function_exits_nonzero(self) -> None:
        source = "def f(x):\n" + "".join(
            f"    if x == {i}:\n        return {i}\n" for i in range(12)
        )
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, {"a.py": source}, {})
            self.assertEqual(self._main(root), 1)

    def test_adopt_takes_the_complex_function_into_the_baseline(self) -> None:
        source = "def f(x):\n" + "".join(
            f"    if x == {i}:\n        return {i}\n" for i in range(12)
        )
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, {"a.py": source}, {})
            self.assertEqual(self._main(root, "--adopt"), 0)
            self.assertEqual(load_baseline(root / "complexity-baseline.json"), {"a.py::f": 13})

    def test_the_threshold_is_overridable(self) -> None:
        source = "def f(x):\n" + "".join(
            f"    if x == {i}:\n        return {i}\n" for i in range(12)
        )
        with TemporaryDirectory() as tmp:
            root = _tree(tmp, {"a.py": source}, {})
            self.assertEqual(self._main(root, "--threshold", "20"), 0)


if __name__ == "__main__":
    unittest.main()
