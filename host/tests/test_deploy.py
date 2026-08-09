"""The bytecode deploy, minus the board.

Two halves. The first is the compile, and it is the one CI cares about:
MicroPython's parser is not CPython's, so `ruff` and `basedpyright`
agreeing about device/ says nothing about whether the board can import
it. Every module is put through the real mpy-cross here.

The second is the flash rewriting, against `FakeRepl`. What is worth
asserting there is not that files move — it is the two invariants that
were learnt the hard way: bytecode with its source left beside it is
bytecode that never runs, and a file deleted off flash whose only other
copy was on flash is gone.
"""

from __future__ import annotations

import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from buddy_deploy import (
    DEST_ROOT,
    LAUNCHER,
    MPY_CROSS_ABI,
    OVERLAY,
    REMOVE,
    REPO,
    SERIAL_READ_TIMEOUT_S,
    UPSTREAM,
    Deadline,
    DeployError,
    DeployTimeout,
    Job,
    build_overlay,
    check_launcher,
    compile_source,
    device_mpy_abi,
    find_shadows,
    install_launcher,
    main,
    mpy_abi_of,
    mpy_cross_abi,
    prune,
    push_file,
    push_jobs,
    stage_upstream,
)
from device_repl import ReplError
from fake_repl import FakeRepl


def _forever() -> Deadline:
    """A budget nothing in these tests can exhaust."""
    return Deadline(3600.0)


def _silent(message: str) -> None:
    pass


class CompileTest(unittest.TestCase):
    """The check that runs with no device attached."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)

    def test_the_pinned_abi_is_what_mpy_cross_emits(self) -> None:
        # The pin in pyproject.toml and MPY_CROSS_ABI have to move
        # together. Bytecode does not cross ABI versions, and the device
        # reports the mismatch as a bare ImportError.
        self.assertEqual(mpy_cross_abi(), MPY_CROSS_ABI)

    def test_every_overlay_module_compiles(self) -> None:
        for rel in OVERLAY:
            with self.subTest(module=rel):
                built = self.out / f"{Path(rel).stem}.mpy"
                size = compile_source(REPO / "device" / rel, built)
                self.assertEqual(size, built.stat().st_size)
                self.assertEqual(mpy_abi_of(built.read_bytes()), int(MPY_CROSS_ABI.split(".")[0]))

    def test_the_launcher_compiles_even_though_it_ships_as_source(self) -> None:
        # /flash/main.py is executed as source and never looked up as
        # main.mpy, so this compile exists only to put the launcher in
        # front of MicroPython's parser.
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


class PushFileTest(unittest.TestCase):
    """The transfer primitive, once mpremote owns the mechanism.

    The paste blocks, the chunking and the REPL handshake are gone —
    those were a reimplementation of `mpremote fs cp` and are now the
    real thing, tested upstream. What is left is specific to this
    overlay: where files land, the one directory level the install
    layout needs, and the guard that exists because its absence once
    looked like success.
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

    def test_an_existing_directory_is_left_alone(self) -> None:
        # mkdir on a directory that is already there is an EEXIST, and
        # every push after the first one would hit it.
        repl = FakeRepl(dirs={f"{DEST_ROOT}/apps"})
        push_file(repl, self.src, "apps/claude_buddy.py", quiet=True)
        self.assertEqual(repl.made_dirs, [])

    def test_short_write_is_an_error(self) -> None:
        # The failure this guard exists for: the transfer looks fine and
        # the file on flash is truncated. mpremote does not stat what it
        # wrote, so this is ours to check.
        repl = FakeRepl()
        repl.report_size = 3
        with self.assertRaises(ReplError) as caught:
            push_file(repl, self.src, "buddy_serial.py", quiet=True)
        self.assertIn("3 bytes on flash", str(caught.exception))

    def test_a_device_side_failure_names_the_file(self) -> None:
        # mpremote raises TransportError for a link problem and OSError
        # for a filesystem one, and neither message says which file was
        # in flight.
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


class DeadlineTest(unittest.TestCase):
    def test_a_budget_with_time_left_lets_the_step_run(self) -> None:
        now = [0.0]
        deadline = Deadline(10.0, clock=lambda: now[0])
        now[0] = 9.0
        deadline.check("pushing buddy_tts.mpy")
        self.assertAlmostEqual(deadline.remaining(), 1.0)

    def test_an_exhausted_budget_names_the_step(self) -> None:
        now = [0.0]
        deadline = Deadline(10.0, clock=lambda: now[0])
        now[0] = 10.0
        with self.assertRaises(DeployTimeout) as caught:
            deadline.check("pushing buddy_tts.mpy")
        self.assertIn("buddy_tts.mpy", str(caught.exception))


class DeviceAbiTest(unittest.TestCase):
    def test_only_the_low_byte_of_the_device_word_is_the_version(self) -> None:
        # sys.implementation._mpy packs sub-version and native arch above
        # the version. 0x2806 is what this board reports: v6, sub 3,
        # arch 10 (xtensawin), and only the 6 has to match what we emit.
        repl = FakeRepl({"sys.implementation._mpy": 0x2806})
        self.assertEqual(device_mpy_abi(repl), 6)


class _Bench:
    """A FakeRepl seeded with a device, plus the directories around it."""

    def __init__(self, cleanup: unittest.TestCase) -> None:
        tmp = TemporaryDirectory()
        cleanup.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vendor = root / "vendor"
        self.build = root / "build"
        self.src = root / "device"
        self.src.mkdir()
        (self.src / LAUNCHER).write_text("print('our launcher')\n")
        self.repl = FakeRepl(dirs={DEST_ROOT, f"{DEST_ROOT}/apps"})

    def put(self, path: str, text: str) -> None:
        self.repl.files[f"{DEST_ROOT}/{path}"] = text.encode()


class StageUpstreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = _Bench(self)

    def test_source_on_flash_is_archived_before_it_is_replaced(self) -> None:
        # The peers are not in this repository. Reading them off the
        # device is the only way to get them, and the deploy then
        # deletes that copy — so the archive has to happen first.
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
        # Flash holds neither the source nor the bytecode — the state a
        # half-finished earlier run leaves behind.
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
        self.bench = _Bench(self)
        built = self.bench.build / "buddy_tts.mpy"
        built.parent.mkdir(parents=True)
        built.write_bytes(b"M\x06\x00\x1fbytecode")
        self.job = Job(built, "buddy_tts.mpy", "device/buddy_tts.py", built.stat().st_size)

    def test_the_source_that_would_shadow_the_bytecode_is_removed(self) -> None:
        # `foo.py` is found before `foo.mpy` on every sys.path entry, so
        # leaving it behind means the push changed nothing at all.
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
        self.bench = _Bench(self)

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
        # After the first deploy the file on flash is ours. Archiving it
        # would overwrite the only copy of upstream's with a copy of
        # device/main.py.
        self.bench.vendor.mkdir(parents=True)
        (self.bench.vendor / LAUNCHER).write_text("print('upstream menu + NimBLE')\n")
        self.bench.put(LAUNCHER, "print('our launcher')\n")
        install_launcher(
            self.bench.repl, self.bench.vendor, _forever(), _silent, src_dir=self.bench.src
        )
        self.assertEqual(
            (self.bench.vendor / LAUNCHER).read_text(), "print('upstream menu + NimBLE')\n"
        )


class CliTest(unittest.TestCase):
    def test_a_deploy_without_a_port_is_refused_before_anything_is_built(self) -> None:
        self.assertEqual(main([]), 2)

    def test_compile_only_needs_no_port(self) -> None:
        with TemporaryDirectory() as tmp:
            rc = main(["--compile-only", "--build", tmp, "--vendor", f"{tmp}/vendor"])
        self.assertEqual(rc, 0)


class WholeRunTest(unittest.TestCase):
    """One pass over a device that has never been converted.

    The parts are covered above; what this adds is the order they run
    in. Getting that wrong is how a file gets deleted before it has been
    archived, and no unit test of either half would notice.
    """

    def setUp(self) -> None:
        self.bench = _Bench(self)
        self.repl = self.bench.repl
        self.repl.answers["sys.implementation._mpy"] = 0x2806
        for name in UPSTREAM:
            self.bench.put(f"{name}.py", f"print('{name}')\n")
        for rel in OVERLAY:
            self.bench.put(rel, "print('the source this replaces')\n")
        for rel in REMOVE:
            self.bench.put(rel, f"print('{rel}')\n")
        self.bench.put(LAUNCHER, "print('upstream menu + NimBLE')\n")

        patch = unittest.mock.patch("buddy_deploy.connect_repl", return_value=self.repl)
        patch.start()
        self.addCleanup(patch.stop)
        self.rc = main(
            [
                "--port",
                "/dev/null",
                "--build",
                str(self.bench.build),
                "--vendor",
                str(self.bench.vendor),
            ]
        )

    def test_it_succeeds(self) -> None:
        self.assertEqual(self.rc, 0)

    def test_every_module_is_bytecode_and_no_source_is_left_beside_it(self) -> None:
        for rel in OVERLAY:
            stem = rel[: -len(".py")]
            self.assertIn(f"{DEST_ROOT}/{stem}.mpy", self.repl.files)
            self.assertNotIn(f"{DEST_ROOT}/{stem}.py", self.repl.files)
        for name in UPSTREAM:
            self.assertIn(f"{DEST_ROOT}/{name}.mpy", self.repl.files)
            self.assertNotIn(f"{DEST_ROOT}/{name}.py", self.repl.files)

    def test_the_launcher_is_ours_and_still_source(self) -> None:
        # main() takes the launcher from device/, not from the bench:
        # /flash/main.py is executed as source, so what lands has to be
        # the real file byte for byte.
        self.assertEqual(
            self.repl.files[f"{DEST_ROOT}/{LAUNCHER}"], (REPO / "device" / LAUNCHER).read_bytes()
        )
        self.assertNotIn(f"{DEST_ROOT}/main.mpy", self.repl.files)
        self.assertEqual(
            (self.bench.vendor / LAUNCHER).read_text(), "print('upstream menu + NimBLE')\n"
        )

    def test_nothing_was_deleted_that_was_not_archived_first(self) -> None:
        for path in self.repl.removed:
            rel = path[len(f"{DEST_ROOT}/") :]
            if rel.endswith(".py") and rel[: -len(".py")] in {
                job[: -len(".mpy")] for job in (f"{r[:-3]}.mpy" for r in OVERLAY)
            }:
                # Overlay sources are in device/ — the archive is git.
                continue
            self.assertTrue(
                (self.bench.vendor / rel).is_file(), f"{rel} was removed without being archived"
            )

    def test_the_port_gets_a_read_timeout(self) -> None:
        # mpremote leaves it at None, and every read in a transfer goes
        # through it. Without this the run can only be ended from outside.
        self.assertEqual(self.repl.serial.timeout, SERIAL_READ_TIMEOUT_S)


class PruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = _Bench(self)

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
