"""アーキテクチャのメトリクスが、意味のある数を返すことを見るテスト。

数そのものより「何を数えて何を数えないか」の方が壊れやすい。else が分岐を
増やさないこと、共有定数が凝集の辺になること、tests/ が結合度に混ざらない
こと。この 3 つがずれると、出てくる表は読めるのに嘘になる。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from arch_analysis import (
    ComponentMetric,
    ModuleMetric,
    analyze_module,
    component_of,
    components,
    find_cycles,
    is_test,
    module_name,
    resolve_import,
)
from arch_metrics import main


def _complexity(source: str) -> int:
    module = analyze_module("x.py", source)
    return module.max_complexity


class CyclomaticComplexityTest(unittest.TestCase):
    def test_a_straight_line_function_is_one(self) -> None:
        self.assertEqual(_complexity("def f():\n    return 1\n"), 1)

    def test_an_if_adds_one(self) -> None:
        self.assertEqual(_complexity("def f(x):\n    if x:\n        return 1\n    return 2\n"), 2)

    def test_an_else_adds_nothing(self) -> None:
        # else は新しい経路ではなく、if が既に数えた経路の裏側。
        source = "def f(x):\n    if x:\n        return 1\n    else:\n        return 2\n"
        self.assertEqual(_complexity(source), 2)

    def test_an_elif_adds_one(self) -> None:
        source = (
            "def f(x):\n"
            "    if x:\n"
            "        return 1\n"
            "    elif x > 2:\n"
            "        return 2\n"
            "    return 3\n"
        )
        self.assertEqual(_complexity(source), 3)

    def test_a_loop_adds_one(self) -> None:
        self.assertEqual(_complexity("def f(xs):\n    for x in xs:\n        print(x)\n"), 2)

    def test_a_boolean_operator_adds_one_per_extra_operand(self) -> None:
        self.assertEqual(_complexity("def f(a, b, c):\n    return a and b and c\n"), 3)

    def test_each_except_handler_adds_one(self) -> None:
        source = (
            "def f():\n"
            "    try:\n"
            "        g()\n"
            "    except ValueError:\n"
            "        pass\n"
            "    except KeyError:\n"
            "        pass\n"
        )
        self.assertEqual(_complexity(source), 3)

    def test_a_comprehension_filter_adds_one(self) -> None:
        self.assertEqual(_complexity("def f(xs):\n    return [x for x in xs if x]\n"), 3)

    def test_a_ternary_adds_one(self) -> None:
        self.assertEqual(_complexity("def f(x):\n    return 1 if x else 2\n"), 2)

    def test_a_nested_function_is_measured_on_its_own(self) -> None:
        # 内側を外側に足し込むと、外側が実際より複雑に見える。
        source = (
            "def outer(x):\n"
            "    def inner(y):\n"
            "        if y:\n"
            "            return 1\n"
            "        return 2\n"
            "    return inner\n"
        )
        module = analyze_module("x.py", source)
        self.assertEqual(
            {f.name: f.complexity for f in module.function_metrics}, {"outer": 1, "inner": 2}
        )

    def test_a_method_is_named_with_its_class(self) -> None:
        source = "class C:\n    def m(self):\n        return 1\n"
        module = analyze_module("x.py", source)
        self.assertEqual([f.name for f in module.function_metrics], ["C.m"])

    def test_a_module_without_functions_has_complexity_zero(self) -> None:
        self.assertEqual(_complexity("X = 1\n"), 0)


class CohesionTest(unittest.TestCase):
    """モジュール内の定義が、参照でいくつの塊に分かれるか。1 なら一枚岩。"""

    def _cohesion(self, source: str) -> int:
        return analyze_module("x.py", source).cohesion

    def test_two_unrelated_functions_are_two_pieces(self) -> None:
        self.assertEqual(self._cohesion("def a():\n    pass\n\n\ndef b():\n    pass\n"), 2)

    def test_a_call_joins_them(self) -> None:
        self.assertEqual(self._cohesion("def a():\n    return b()\n\n\ndef b():\n    pass\n"), 1)

    def test_a_shared_constant_joins_them(self) -> None:
        # 定数を辺に数えないと、設定値を囲む素直なモジュールが
        # ばらばらに見える。
        source = "N = 3\n\n\ndef a():\n    return N\n\n\ndef b():\n    return N\n"
        self.assertEqual(self._cohesion(source), 1)

    def test_a_class_counts_as_one_definition(self) -> None:
        source = "class C:\n    def m(self):\n        return 1\n\n\ndef a():\n    return C()\n"
        self.assertEqual(self._cohesion(source), 1)

    def test_an_empty_module_is_zero(self) -> None:
        self.assertEqual(self._cohesion("import os\n"), 0)


class ModuleNameTest(unittest.TestCase):
    def test_a_host_member_drops_its_src_root(self) -> None:
        self.assertEqual(module_name("host/link/src/buddy_link.py"), "buddy_link")

    def test_a_device_package_keeps_its_dotted_path(self) -> None:
        self.assertEqual(module_name("device/buddy/chat.py"), "buddy.chat")

    def test_a_device_app_keeps_its_directory(self) -> None:
        self.assertEqual(module_name("device/apps/claude_buddy.py"), "apps.claude_buddy")

    def test_a_package_init_is_the_package_itself(self) -> None:
        self.assertEqual(module_name("device/buddy/__init__.py"), "buddy")

    def test_a_loose_script_is_just_its_stem(self) -> None:
        self.assertEqual(module_name("scripts/buddy_chatter_notify.py"), "buddy_chatter_notify")


class ComponentTest(unittest.TestCase):
    def test_a_host_member_is_two_levels_deep(self) -> None:
        self.assertEqual(component_of("host/link/src/buddy_link.py"), "host/link")

    def test_the_device_is_one_level(self) -> None:
        self.assertEqual(component_of("device/buddy/chat.py"), "device")

    def test_loose_scripts_are_their_own_component(self) -> None:
        self.assertEqual(component_of("scripts/buddy_chatter_notify.py"), "scripts")

    def test_tests_belong_to_their_member(self) -> None:
        self.assertEqual(component_of("host/mcp/tests/test_mcp.py"), "host/mcp")


class IsTestTest(unittest.TestCase):
    def test_a_file_under_tests_is_a_test(self) -> None:
        self.assertTrue(is_test("host/mcp/tests/test_mcp.py"))

    def test_source_is_not(self) -> None:
        self.assertFalse(is_test("host/mcp/src/buddy_mcp.py"))


class ResolveImportTest(unittest.TestCase):
    KNOWN: ClassVar[dict[str, str]] = {
        "buddy_link": "host/link/src/buddy_link.py",
        "buddy.chat": "device/buddy/chat.py",
    }

    def test_an_exact_name_resolves(self) -> None:
        self.assertEqual(resolve_import("buddy_link", self.KNOWN), "buddy_link")

    def test_a_submodule_falls_back_to_its_package(self) -> None:
        self.assertEqual(resolve_import("buddy.chat.inner", self.KNOWN), "buddy.chat")

    def test_a_third_party_name_resolves_to_nothing(self) -> None:
        self.assertIsNone(resolve_import("serial", self.KNOWN))


def _module(path: str, name: str, imports: set[str]) -> ModuleMetric:
    return ModuleMetric(
        path=path,
        name=name,
        lines=1,
        functions=0,
        classes=0,
        max_complexity=0,
        cohesion=0,
        raw_imports=frozenset(imports),
        function_metrics=(),
    )


class ComponentsTest(unittest.TestCase):
    def _by_name(self, metrics: list[ComponentMetric]) -> dict[str, ComponentMetric]:
        return {m.name: m for m in metrics}

    def test_a_one_way_dependency_makes_the_caller_unstable(self) -> None:
        modules = [
            _module("host/mcp/src/a.py", "a", {"b"}),
            _module("host/link/src/b.py", "b", set()),
        ]
        by_name = self._by_name(components(modules))
        self.assertEqual((by_name["host/mcp"].efferent, by_name["host/mcp"].afferent), (1, 0))
        self.assertEqual((by_name["host/link"].efferent, by_name["host/link"].afferent), (0, 1))
        self.assertEqual(by_name["host/mcp"].instability, 1.0)
        self.assertEqual(by_name["host/link"].instability, 0.0)

    def test_an_isolated_component_is_stable_by_convention(self) -> None:
        by_name = self._by_name(components([_module("device/a.py", "a", set())]))
        self.assertEqual(by_name["device"].instability, 0.0)

    def test_imports_inside_one_component_are_not_coupling(self) -> None:
        modules = [
            _module("host/mcp/src/a.py", "a", {"b"}),
            _module("host/mcp/src/b.py", "b", set()),
        ]
        by_name = self._by_name(components(modules))
        self.assertEqual((by_name["host/mcp"].efferent, by_name["host/mcp"].afferent), (0, 0))

    def test_size_is_counted_per_component(self) -> None:
        modules = [
            _module("device/a.py", "a", set()),
            _module("device/b.py", "b", set()),
        ]
        by_name = self._by_name(components(modules))
        self.assertEqual((by_name["device"].files, by_name["device"].lines), (2, 2))


class FindCyclesTest(unittest.TestCase):
    def test_a_tree_has_no_cycle(self) -> None:
        self.assertEqual(find_cycles({"a": {"b"}, "b": {"c"}, "c": set()}), [])

    def test_a_two_node_loop_is_reported(self) -> None:
        self.assertEqual(find_cycles({"a": {"b"}, "b": {"a"}}), [["a", "b"]])

    def test_a_longer_loop_is_reported_once(self) -> None:
        graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
        self.assertEqual(find_cycles(graph), [["a", "b", "c"]])

    def test_a_self_import_is_not_a_cycle(self) -> None:
        # 自分を import するモジュールは書けるが、循環依存の話ではない。
        self.assertEqual(find_cycles({"a": {"a"}}), [])


class MainTest(unittest.TestCase):
    """レポートは出るだけ。落ちるのは循環依存があるときだけ。"""

    def _run(self, files: dict[str, str], *args: str) -> tuple[int, str]:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, source in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(source)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    ["--root", str(root), *args],
                    list_files=lambda r: sorted(r.rglob("*.py")),
                )
            return code, buffer.getvalue()

    def test_a_tree_without_cycles_exits_zero(self) -> None:
        code, out = self._run(
            {
                "host/mcp/src/a.py": "import b\n",
                "host/link/src/b.py": "X = 1\n",
            }
        )
        self.assertEqual(code, 0)
        self.assertIn("host/link", out)

    def test_a_module_cycle_exits_nonzero(self) -> None:
        code, out = self._run(
            {
                "host/mcp/src/a.py": "import b\n",
                "host/mcp/src/b.py": "import a\n",
            }
        )
        self.assertEqual(code, 1)
        self.assertIn("循環", out)

    def test_a_low_complexity_tree_reports_no_offenders(self) -> None:
        _, out = self._run({"host/mcp/src/a.py": "def f():\n    return 1\n"})
        self.assertNotIn("複雑", out)

    def test_json_carries_the_numbers(self) -> None:
        code, out = self._run({"host/mcp/src/a.py": "def f():\n    return 1\n"}, "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["components"][0]["name"], "host/mcp")
        self.assertEqual(report["modules"][0]["name"], "a")

    def test_tests_stay_out_of_the_coupling(self) -> None:
        # device/tests は host/link を import する契約テスト。数えると
        # device が host/link に依存しているように見えてしまう。
        _, out = self._run(
            {
                "device/buddy/a.py": "X = 1\n",
                "device/tests/test_a.py": "import b\n",
                "host/link/src/b.py": "Y = 1\n",
            },
            "--json",
        )
        report = json.loads(out)
        device = next(c for c in report["components"] if c["name"] == "device")
        self.assertEqual(device["efferent"], 0)


if __name__ == "__main__":
    unittest.main()
