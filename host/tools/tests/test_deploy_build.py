"""コンパイル。ボードの無いところで走る側。

CI が気にするのはこちら。MicroPython のパーサは CPython のものではないので、
`ruff` と `basedpyright` が device/ について何も言わないことは、ボードが
それを import できることを何も意味しない。ここでは全モジュールを本物の
mpy-cross に通す。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from deploy_build import (
    build_overlay,
    check_launcher,
    compile_source,
    mpy_abi_of,
    mpy_cross_abi,
)
from deploy_spec import MPY_CROSS_ABI, OVERLAY, REPO, DeployError


class CompileTest(unittest.TestCase):
    """デバイスを繋がずに走るチェック。"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)

    def test_the_pinned_abi_is_what_mpy_cross_emits(self) -> None:
        # pyproject.toml のピンと MPY_CROSS_ABI は一緒に動かす。バイトコードは
        # ABI のバージョンをまたがず、デバイスはその食い違いを素の ImportError
        # としか言わない。
        self.assertEqual(mpy_cross_abi(), MPY_CROSS_ABI)

    def test_every_overlay_module_compiles(self) -> None:
        for rel in OVERLAY:
            with self.subTest(module=rel):
                built = self.out / f"{Path(rel).stem}.mpy"
                size = compile_source(REPO / "device" / rel, built)
                self.assertEqual(size, built.stat().st_size)
                self.assertEqual(mpy_abi_of(built.read_bytes()), int(MPY_CROSS_ABI.split(".")[0]))

    def test_the_debug_module_ships(self) -> None:
        # dbg.* のフレームが来るまで import されないので、これを欠いたバンドルは
        # 完全に健康そうに見える — 挙動のおかしいデバイスを誰かが覗きたくなる
        # その瞬間まで。ここで乏しい資源は flash ではなく heap で、遅延 import に
        # してある以上それも使わない。
        self.assertIn("buddy/debug.py", OVERLAY)

    def test_the_package_init_ships(self) -> None:
        # MicroPython に namespace package は無い。flash に `__init__` が無ければ
        # `/flash/buddy` はただのディレクトリで、アプリの `from buddy import ...`
        # は全て ImportError になる。
        self.assertIn("buddy/__init__.py", OVERLAY)

    def test_the_package_init_is_first(self) -> None:
        # push_jobs は OVERLAY を順に歩き、push_file は必要な 1 段だけ
        # ディレクトリを作るので、/flash/buddy ができるかどうかを順序が決める
        # わけではない。順序が決めるのは、転送が途中で終わったときに何が残るか
        # — __init__ の無い package はモジュールでもディレクトリでもない形で
        # import され、これが分かりにくい失敗になる。
        self.assertEqual(OVERLAY[0], "buddy/__init__.py")

    def test_the_launcher_compiles_even_though_it_ships_as_source(self) -> None:
        # /flash/main.py はソースのまま実行され、main.mpy として引かれることは
        # 無い。このコンパイルは launcher を MicroPython のパーサの前に置くため
        # だけにある。
        self.assertGreater(check_launcher(self.out), 0)
        self.assertFalse((self.out / "main.mpy").exists())

    def test_build_overlay_names_every_module_it_built(self) -> None:
        jobs = build_overlay(self.out)
        self.assertEqual([job.dest for job in jobs], [rel[:-3] + ".mpy" for rel in OVERLAY])
        for job in jobs:
            self.assertTrue(job.built.is_file())

    def test_a_missing_source_fails_before_anything_is_compiled(self) -> None:
        empty = self.out / "empty"
        empty.mkdir()
        with self.assertRaises(DeployError) as caught:
            build_overlay(self.out / "build", src_dir=empty)
        self.assertIn(OVERLAY[0], str(caught.exception))

    def test_a_syntax_error_names_the_file(self) -> None:
        bad = self.out / "bad.py"
        bad.write_text("def (\n")
        with self.assertRaises(DeployError) as caught:
            compile_source(bad, self.out / "bad.mpy")
        self.assertIn("bad.py", str(caught.exception))

    def test_something_that_is_not_bytecode_is_rejected(self) -> None:
        with self.assertRaises(DeployError):
            mpy_abi_of(b"print('hi')\n")


if __name__ == "__main__":
    unittest.main()
