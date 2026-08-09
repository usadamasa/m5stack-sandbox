"""MicroPython constraints on everything under `device/`.

That tree does not run on the interpreter the rest of this repository is
checked with. It runs on MicroPython 1.27 on an ESP32-S3, where the gap
is not a matter of style: `typing` and `__future__` do not exist, and a
`bytearray` cannot be sliced-deleted. Both mistakes parse cleanly on
CPython, pass ruff, pass the type checker, and then fail on the device —
one as an ImportError at launch, the other as a TypeError in the rx
path that only shows up once a host starts talking.

So the rules are enforced here, mechanically, against the AST.
"""

import ast
import unittest
from pathlib import Path

DEVICE_ROOT = Path(__file__).resolve().parents[1]

# Available on MicroPython without an import, and safe to name in an
# annotation. Anything richer belongs in a `# type:` comment, which the
# device's parser never sees.
ALLOWED_ANNOTATIONS = frozenset(
    {
        "bool",
        "bytearray",
        "bytes",
        "dict",
        "float",
        "int",
        "list",
        "memoryview",
        "None",
        "str",
        "tuple",
    }
)

BANNED_IMPORTS = frozenset({"__future__", "typing", "typing_extensions"})


def device_sources() -> list[Path]:
    # tests/ は除く。デバイスへは載らず CPython で走るテストコードで、
    # ここで禁じている `typing` などをむしろ使う。
    return sorted(p for p in DEVICE_ROOT.rglob("*.py") if "tests" not in p.parts)


class DeviceSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = device_sources()
        # A glob that silently matches nothing would make every test
        # below vacuously true.
        self.assertTrue(self.sources, f"no sources under {DEVICE_ROOT}")

    def test_no_host_only_imports(self) -> None:
        for path in self.sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    self.assertNotIn(
                        name,
                        BANNED_IMPORTS,
                        f"{path.name}:{node.lineno} imports {name!r}, "
                        "which MicroPython does not ship",
                    )

    def test_no_bytearray_slice_deletion(self) -> None:
        # `del buf[:n]` raises TypeError on MicroPython. The idiom that
        # works is rebinding to the tail: `buf = buf[n:]`.
        for path in self.sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Delete):
                    continue
                for target in node.targets:
                    is_slice_delete = isinstance(target, ast.Subscript) and isinstance(
                        target.slice, ast.Slice
                    )
                    self.assertFalse(
                        is_slice_delete,
                        f"{path.name}:{node.lineno} deletes a slice; MicroPython's "
                        "bytearray has no item deletion — rebind to the tail instead",
                    )

    def test_no_exception_chaining(self) -> None:
        # MicroPython has no `__cause__`, so `raise X from e` is not the
        # cheap extra context it is on CPython. Ruff's B904 asks for it
        # by default and is switched off for this tree in pyproject;
        # this is the other half of that decision, so the rule is
        # enforced rather than merely unenforced. Fold the cause into
        # the message instead: `raise FetchError("...: " + str(e))`.
        for path in self.sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and node.cause is not None:
                    self.fail(
                        f"{path.name}:{node.lineno} chains an exception with `from`, "
                        "which MicroPython does not support"
                    )

    def test_annotations_name_only_builtins(self) -> None:
        # Annotations are parsed by MicroPython and then discarded, so
        # they are free as long as every name in them is a builtin. A
        # bare `list[str]` would be fine to parse but is a subscript we
        # would rather not have to reason about on two interpreters.
        for path in self.sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                annotations: list[ast.expr] = []
                if isinstance(node, ast.FunctionDef):
                    args = node.args
                    annotations = [
                        a.annotation
                        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                        if a.annotation is not None
                    ]
                    if node.returns is not None:
                        annotations.append(node.returns)
                elif isinstance(node, ast.AnnAssign):
                    annotations = [node.annotation]

                for annotation in annotations:
                    rendered = ast.unparse(annotation)
                    self.assertIsInstance(
                        annotation,
                        ast.Constant | ast.Name,
                        f"{path.name}:{annotation.lineno} annotation {rendered!r} "
                        "is not a plain builtin name",
                    )
                    self.assertIn(
                        rendered,
                        ALLOWED_ANNOTATIONS,
                        f"{path.name}:{annotation.lineno} annotation {rendered!r} "
                        "names something MicroPython does not have as a builtin",
                    )


if __name__ == "__main__":
    unittest.main()
