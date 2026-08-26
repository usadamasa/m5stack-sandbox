"""flash の書き換え。相手は `FakeRepl`。

ここで assert する価値があるのはファイルが動くことではなく、痛い目を見て
学んだ 2 つの不変条件 — ソースを隣に残したままのバイトコードは決して走らない、
そして flash の他に控えの無いファイルを flash から消したらそれきり、ということ。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from deploy_device import (
    device_mpy_abi,
    find_shadows,
    install_launcher,
    prune,
    prune_stale,
    push_file,
    push_jobs,
    stage_upstream,
)
from deploy_spec import (
    DEST_ROOT,
    LAUNCHER,
    OVERLAY,
    REMOVE,
    STALE,
    UPSTREAM,
    Deadline,
    DeployError,
    DeployTimeout,
    Job,
)
from deploy_stubs import Bench
from device_repl import ReplError
from fake_repl import FakeRepl


def _forever() -> Deadline:
    """このテストのどれもが使い切れない予算。"""
    return Deadline(3600.0)


def _silent(message: str) -> None:
    pass


class PushFileTest(unittest.TestCase):
    """転送の原始的な部分。仕掛けそのものは mpremote が持っている。

    paste ブロックもチャンク分割も REPL のハンドシェイクも無くなった —
    あれは `mpremote fs cp` の焼き直しで、いまは本物を呼び、上流でテスト
    されている。残っているのはこの overlay に固有のこと: ファイルがどこへ
    落ちるか、インストールの配置が要求する 1 段だけのディレクトリ、そして
    かつて無いことが成功に見えた、そのための guard。
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = Path(self._tmp.name) / "buddy_serial.py"
        self.src.write_bytes(b"print('hi')\n")

    def test_lands_under_flash(self) -> None:
        repl = FakeRepl()
        written = push_file(repl, self.src, "buddy_serial.py", quiet=True)
        self.assertEqual(repl.files[f"{DEST_ROOT}/buddy_serial.py"], self.src.read_bytes())
        self.assertEqual(written, self.src.stat().st_size)

    def test_a_root_file_needs_no_directory(self) -> None:
        repl = FakeRepl()
        push_file(repl, self.src, "buddy_serial.py", quiet=True)
        self.assertEqual(repl.made_dirs, [])

    def test_a_nested_file_gets_its_directory(self) -> None:
        repl = FakeRepl()
        push_file(repl, self.src, "apps/claude_buddy.py", quiet=True)
        self.assertEqual(repl.made_dirs, [f"{DEST_ROOT}/apps"])
        self.assertIn(f"{DEST_ROOT}/apps/claude_buddy.py", repl.files)

    def test_the_buddy_package_gets_its_directory_too(self) -> None:
        # /flash/buddy はデプロイが作るもう 1 つのディレクトリで、apps/ と
        # 違って、既にそこにあるものを見つけるのではなく本リポジトリが作る。
        repl = FakeRepl()
        push_file(repl, self.src, "buddy/serial.py", quiet=True)
        self.assertEqual(repl.made_dirs, [f"{DEST_ROOT}/buddy"])
        self.assertIn(f"{DEST_ROOT}/buddy/serial.py", repl.files)

    def test_an_existing_directory_is_left_alone(self) -> None:
        # 既にあるディレクトリへの mkdir は EEXIST で、最初の 1 回を除く
        # 全ての push がそれを踏むことになる。
        repl = FakeRepl(dirs={f"{DEST_ROOT}/apps"})
        push_file(repl, self.src, "apps/claude_buddy.py", quiet=True)
        self.assertEqual(repl.made_dirs, [])

    def test_short_write_is_an_error(self) -> None:
        # この guard がそのためにある失敗: 転送は問題なく見えて、flash の上の
        # ファイルは切り詰められている。mpremote は書いたものを stat しないので、
        # 確かめるのはこちらの仕事。
        repl = FakeRepl()
        repl.report_size = 3
        with self.assertRaises(ReplError) as caught:
            push_file(repl, self.src, "buddy_serial.py", quiet=True)
        self.assertIn("3 bytes on flash", str(caught.exception))

    def test_a_device_side_failure_names_the_file(self) -> None:
        # mpremote はリンクの問題を TransportError、ファイルシステムの問題を
        # OSError で上げるが、どちらのメッセージも飛んでいたファイルの名前を
        # 言わない。
        class _Full(FakeRepl):
            def fs_writefile(
                self,
                dest: str,
                data: bytes,
                chunk_size: int = 256,
                progress_callback: object = None,
            ) -> None:
                raise OSError(28, "No space left on device")

        with self.assertRaises(ReplError) as caught:
            push_file(_Full(), self.src, "buddy_serial.py", quiet=True)
        self.assertIn("buddy_serial.py", str(caught.exception))
        self.assertIn("No space left", str(caught.exception))

    def test_progress_is_suppressed_when_quiet(self) -> None:
        seen: list[tuple[int, bool]] = []

        class _Watching(FakeRepl):
            def fs_writefile(
                self,
                dest: str,
                data: bytes,
                chunk_size: int = 256,
                progress_callback: object = None,
            ) -> None:
                seen.append((chunk_size, progress_callback is not None))
                super().fs_writefile(dest, data)

        push_file(_Watching(), self.src, "buddy_serial.py", quiet=True)
        self.assertEqual(seen[0][1], False)


class DeviceAbiTest(unittest.TestCase):
    def test_only_the_low_byte_of_the_device_word_is_the_version(self) -> None:
        # sys.implementation._mpy はバージョンの上に sub-version と native の
        # アーキテクチャを詰めている。0x2806 がこのボードの申告で、v6・sub 3・
        # arch 10 (xtensawin)。こちらが出すものと合っている必要があるのは 6 だけ。
        repl = FakeRepl({"sys.implementation._mpy": 0x2806})
        self.assertEqual(device_mpy_abi(repl), 6)


class StageUpstreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = Bench(self)

    def test_source_on_flash_is_archived_before_it_is_replaced(self) -> None:
        # 相手のモジュールは本リポジトリには無い。デバイスから読み出すのが
        # 手に入れる唯一の手段で、デプロイはその後でその控えを消す — だから
        # 退避が先に来る必要がある。
        for name in UPSTREAM:
            self.bench.put(f"{name}.py", f"print('{name}')\n")

        jobs = stage_upstream(
            self.bench.repl, self.bench.vendor, self.bench.build, _forever(), _silent
        )

        self.assertEqual([job.dest for job in jobs], [f"{name}.mpy" for name in UPSTREAM])
        for name in UPSTREAM:
            archived = self.bench.vendor / f"{name}.py"
            self.assertEqual(archived.read_text(), f"print('{name}')\n")

    def test_a_module_already_converted_is_left_alone(self) -> None:
        for name in UPSTREAM:
            self.bench.put(f"{name}.mpy", "bytecode")
        jobs = stage_upstream(
            self.bench.repl, self.bench.vendor, self.bench.build, _forever(), _silent
        )
        self.assertEqual(jobs, [])

    def test_the_archive_covers_a_module_the_device_has_lost(self) -> None:
        # flash にソースもバイトコードも無い — 前の run が途中で終わったときに
        # 残る状態。
        self.bench.vendor.mkdir(parents=True)
        for name in UPSTREAM:
            (self.bench.vendor / f"{name}.py").write_text(f"print('{name}')\n")
        jobs = stage_upstream(
            self.bench.repl, self.bench.vendor, self.bench.build, _forever(), _silent
        )
        self.assertEqual(len(jobs), len(UPSTREAM))

    def test_a_module_nobody_has_stops_the_deploy(self) -> None:
        with self.assertRaises(DeployError) as caught:
            stage_upstream(
                self.bench.repl, self.bench.vendor, self.bench.build, _forever(), _silent
            )
        self.assertIn(UPSTREAM[0], str(caught.exception))
        self.assertIn("m5-onboard", str(caught.exception))


class PushTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = Bench(self)
        built = self.bench.build / "buddy_tts.mpy"
        built.parent.mkdir(parents=True)
        built.write_bytes(b"M\x06\x00\x1fbytecode")
        self.job = Job(built, "buddy_tts.mpy", "device/buddy_tts.py", built.stat().st_size)

    def test_the_source_that_would_shadow_the_bytecode_is_removed(self) -> None:
        # sys.path のどのエントリでも `foo.py` は `foo.mpy` より先に見つかる。
        # 残したままにするのは、その push が何も変えなかったのと同じこと。
        self.bench.put("buddy_tts.py", "print('old')\n")
        push_jobs(self.bench.repl, [self.job], _forever(), _silent)
        self.assertIn(f"{DEST_ROOT}/buddy_tts.mpy", self.bench.repl.files)
        self.assertNotIn(f"{DEST_ROOT}/buddy_tts.py", self.bench.repl.files)
        self.assertEqual(self.bench.repl.removed, [f"{DEST_ROOT}/buddy_tts.py"])

    def test_no_source_to_remove_is_not_an_error(self) -> None:
        push_jobs(self.bench.repl, [self.job], _forever(), _silent)
        self.assertEqual(self.bench.repl.removed, [])

    def test_an_expired_budget_stops_before_the_transfer(self) -> None:
        now = [0.0]
        spent = Deadline(1.0, clock=lambda: now[0])
        now[0] = 2.0
        with self.assertRaises(DeployTimeout):
            push_jobs(self.bench.repl, [self.job], spent, _silent)
        self.assertEqual(self.bench.repl.files, {})

    def test_find_shadows_reports_what_is_still_hiding_the_bytecode(self) -> None:
        self.bench.put("buddy_tts.py", "print('old')\n")
        self.assertEqual(find_shadows(self.bench.repl, [self.job]), [f"{DEST_ROOT}/buddy_tts.py"])


class LauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = Bench(self)

    def test_upstreams_launcher_is_kept_before_it_is_replaced(self) -> None:
        self.bench.put(LAUNCHER, "print('upstream menu + NimBLE')\n")
        install_launcher(
            self.bench.repl, self.bench.vendor, _forever(), _silent, src_dir=self.bench.src
        )
        self.assertEqual(
            (self.bench.vendor / LAUNCHER).read_text(), "print('upstream menu + NimBLE')\n"
        )
        self.assertEqual(
            self.bench.repl.files[f"{DEST_ROOT}/{LAUNCHER}"], b"print('our launcher')\n"
        )

    def test_a_second_run_does_not_archive_our_own_launcher(self) -> None:
        # 最初のデプロイの後、flash の上のファイルはこちらのもの。それを退避
        # すると、upstream のものの唯一の控えを device/main.py の写しで
        # 上書きしてしまう。
        self.bench.vendor.mkdir(parents=True)
        (self.bench.vendor / LAUNCHER).write_text("print('upstream menu + NimBLE')\n")
        self.bench.put(LAUNCHER, "print('our launcher')\n")
        install_launcher(
            self.bench.repl, self.bench.vendor, _forever(), _silent, src_dir=self.bench.src
        )
        self.assertEqual(
            (self.bench.vendor / LAUNCHER).read_text(), "print('upstream menu + NimBLE')\n"
        )


class PruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = Bench(self)

    def test_nothing_is_deleted_without_being_archived(self) -> None:
        for rel in REMOVE:
            self.bench.put(rel, f"print('{rel}')\n")
        prune(self.bench.repl, self.bench.vendor, _forever(), _silent)
        for rel in REMOVE:
            self.assertEqual((self.bench.vendor / rel).read_text(), f"print('{rel}')\n")
            self.assertNotIn(f"{DEST_ROOT}/{rel}", self.bench.repl.files)

    def test_an_absent_file_is_not_an_error(self) -> None:
        prune(self.bench.repl, self.bench.vendor, _forever(), _silent)
        self.assertEqual(self.bench.repl.removed, [])

    def test_the_old_flat_layout_is_removed(self) -> None:
        # REMOVE と違って退避しない。これらは本リポジトリ自身のモジュールで、
        # ソースは git にある。
        for rel in STALE:
            self.bench.put(rel, f"print('{rel}')\n")
        prune_stale(self.bench.repl, _forever(), _silent)
        for rel in STALE:
            self.assertNotIn(f"{DEST_ROOT}/{rel}", self.bench.repl.files)
            self.assertFalse((self.bench.vendor / rel).exists())

    def test_stale_names_no_longer_collide_with_what_is_pushed(self) -> None:
        # 両方のリストに載る名前は、デプロイが書いたばかりのものを自分で消す
        # ということ — prune_stale は push_jobs の後に走るので、黙って消える。
        pushed = {rel[: -len(".py")] + ".mpy" for rel in OVERLAY}
        self.assertEqual(pushed & set(STALE), set())

    def test_an_absent_stale_file_is_not_an_error(self) -> None:
        prune_stale(self.bench.repl, _forever(), _silent)
        self.assertEqual(self.bench.repl.removed, [])

    def test_an_existing_archive_is_not_overwritten(self) -> None:
        (self.bench.vendor / "apps").mkdir(parents=True)
        for rel in REMOVE:
            (self.bench.vendor / rel).write_text("the copy from the first run\n")
            self.bench.put(rel, "whatever is on flash now\n")
        prune(self.bench.repl, self.bench.vendor, _forever(), _silent)
        for rel in REMOVE:
            self.assertEqual((self.bench.vendor / rel).read_text(), "the copy from the first run\n")


if __name__ == "__main__":
    unittest.main()
