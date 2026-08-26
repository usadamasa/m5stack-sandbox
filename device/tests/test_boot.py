"""What `/flash/main.py` has to do at power-on, checked against the host.

`main.py` never runs here — it imports M5 and the firmware's WiFi helper
— so this reads it as an AST instead. What is worth pinning is not the
file's shape but the properties the rest of the system leans on:

* the app is launched at all (issue #28: power-on has to be enough);
* 起動は `run()` の呼び出しであること。import は起動ではない —
  `/flash/apps/claude_buddy.py` は sys.path を整えて `/flash/buddy/app.py`
  へ橋を渡すだけの起動口で、呼ばなければ何も動かない;
* it is launched the same way `buddy_link.LAUNCH_SOURCE` launches it,
  because the two are the only ways into the app and a search path that
  works from the host but not at boot is a bug that only shows up on a
  board nobody has a cable in;
* WiFi comes up first. `claude_buddy` inherits the link and cannot make
  one — `connect()` from inside a running app is accepted and never
  associates.
"""

import ast
import unittest
from pathlib import Path

import buddy_link

MAIN_PY = Path(__file__).resolve().parents[1] / "main.py"

APP_MODULE = "claude_buddy"


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every module name the source imports, however it spells it.

    `__import__(...)` counts。`main.py` がその形を使うのは、呼ぶのが
    ファイルの末尾で、モジュールの import 文を先頭以外に置けないため。
    返り値はそのまま `run()` を呼ぶのに使う。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def _flash_paths(tree: ast.AST) -> set[str]:
    """The `/flash...` literals the source puts on the search path.

    Every such literal in either source is there for `sys.path` and
    nothing else, so matching on the prefix is enough and avoids caring
    which loop or comprehension holds them.
    """
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/flash")
    }


def _first_line_of_call(tree: ast.AST, attr: str) -> int:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
        ):
            return node.lineno
    raise AssertionError(f"main.py never calls {attr}()")


def _app_import_line(tree: ast.AST) -> int:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == APP_MODULE
        ):
            return node.lineno
        if isinstance(node, ast.Import) and any(a.name == APP_MODULE for a in node.names):
            return node.lineno
    raise AssertionError(f"main.py never imports {APP_MODULE}")


class BootTest(unittest.TestCase):
    def setUp(self) -> None:
        self.main = ast.parse(MAIN_PY.read_text(encoding="utf-8"), filename=str(MAIN_PY))
        self.launch = ast.parse(buddy_link.LAUNCH_SOURCE, filename="LAUNCH_SOURCE")

    def test_boot_launches_the_app(self) -> None:
        self.assertIn(APP_MODULE, _imported_modules(self.main))

    def test_both_ways_in_call_run(self) -> None:
        # import しただけでは起動しない。呼ぶのを忘れた側は、静かに何も
        # せずに終わる — 電源を入れても上がらない、あるいはホストからの
        # launch が黙って戻る、という形でしか出てこない。
        for name, tree in (("main.py", self.main), ("LAUNCH_SOURCE", self.launch)):
            with self.subTest(source=name):
                self.assertGreaterEqual(_first_line_of_call(tree, "run"), _app_import_line(tree))

    def test_search_path_matches_the_host_launcher(self) -> None:
        host = _flash_paths(self.launch)
        # A guard on the extraction itself: an empty set would make the
        # comparison below true no matter what main.py does.
        self.assertTrue(host, "LAUNCH_SOURCE puts nothing on sys.path")
        self.assertLessEqual(host, _flash_paths(self.main))

    def test_wifi_comes_up_before_the_app(self) -> None:
        self.assertLess(_first_line_of_call(self.main, "connect"), _app_import_line(self.main))

    def test_the_heap_is_collected_before_the_app_starts(self) -> None:
        # The app is the biggest import on the board and the WiFi splash
        # above has just churned the heap. LAUNCH_SOURCE collects for the
        # same reason.
        self.assertLess(_first_line_of_call(self.main, "collect"), _app_import_line(self.main))


if __name__ == "__main__":
    unittest.main()
