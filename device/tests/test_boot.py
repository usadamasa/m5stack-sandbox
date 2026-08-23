"""What `/flash/main.py` has to do at power-on, checked against the host.

`main.py` never runs here — it imports M5 and the firmware's WiFi helper
— so this reads it as an AST instead. What is worth pinning is not the
file's shape but the three properties the rest of the system leans on:

* the app is launched at all (issue #28: power-on has to be enough);
* it is launched the same way `buddy_bridge.LAUNCH_SOURCE` launches it,
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

import buddy_bridge

MAIN_PY = Path(__file__).resolve().parents[1] / "main.py"

APP_MODULE = "claude_buddy"


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every module name the source imports, however it spells it.

    `__import__(...)` counts. At the bottom of `main.py` a plain `import`
    would be an unused binding, and the module body is what runs either
    way, so the call form is the one in use there.
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
        self.launch = ast.parse(buddy_bridge.LAUNCH_SOURCE, filename="LAUNCH_SOURCE")

    def test_boot_launches_the_app(self) -> None:
        self.assertIn(APP_MODULE, _imported_modules(self.main))

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
