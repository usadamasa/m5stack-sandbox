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

The third is the confirmation the run ends with, which is a launch and
an utterance. No speaker here either: the acks a device would send are
fed to a fake port, and what is asserted is that the app is started the
one-way way, that a refusal is reported as a failed deploy rather than
a successful one, and that the port the link took over is not closed
twice.
"""

from __future__ import annotations

import json
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from buddy_bridge import (
    DEFAULT_READ_TIMEOUT,
    LAUNCH_SOURCE,
    SENTINEL,
    Message,
    encode,
)
from buddy_deploy import (
    DEST_ROOT,
    LAUNCHER,
    MPY_CROSS_ABI,
    OVERLAY,
    REMOVE,
    REPO,
    SERIAL_READ_TIMEOUT_S,
    STALE,
    UPSTREAM,
    VERIFY_TEXT,
    Deadline,
    DeployError,
    DeployTimeout,
    Job,
    build_overlay,
    check_launcher,
    compile_source,
    device_mpy_abi,
    engine_url,
    find_shadows,
    install_launcher,
    main,
    mpy_abi_of,
    mpy_cross_abi,
    prune,
    prune_stale,
    push_file,
    push_jobs,
    stage_upstream,
    verify_by_speech,
)
from device_repl import ReplError
from fake_repl import FakePort, FakeRepl

ENGINE = "http://192.168.0.156:50021"

# What the device answers with, keyed by the cmd it is answering.
Replies = dict[str, list[Message]]

# A device that is working. `speak.say` is answered twice: once when
# playback starts and once when the last block has been played, which
# is what the confirmation actually waits for.
_HAPPY: Replies = {
    "chat.say": [{"ack": "chat.say", "ok": True}],
    "speak.say": [
        {"ack": "speak.say", "ok": True, "bytes": 81920, "rate": 16000},
        {"ack": "speak.end", "ok": True, "blocks": 40, "stalls": 0},
    ],
}


class _TalkingPort(FakePort):
    """A port that answers, rather than one with answers waiting on it.

    Queueing the acks up front does not work and the reason is worth
    keeping: the launch is followed by a settle window that reads
    whatever the app says while starting, and anything already sitting
    in the buffer is consumed there and gone.
    """

    def __init__(self, replies: Replies | None = None) -> None:
        super().__init__()
        self.replies = _HAPPY if replies is None else replies

    def write(self, data: bytes, /) -> int:
        written = super().write(data)
        for frame in _frames(data):
            for ack in self.replies.get(frame["cmd"], []):
                self.feed(encode(ack))
        return written

    @property
    def frames(self) -> list[Message]:
        """Everything the host sent, parsed. `encode` escapes non-ASCII,
        so matching on the raw bytes would miss the text every time."""
        return _frames(self.written)


def _frames(data: bytes | bytearray) -> list[Message]:
    return [
        json.loads(line[len(SENTINEL) :])
        for line in bytes(data).split(b"\n")
        if line.startswith(SENTINEL)
    ]


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

    def test_the_debug_module_ships(self) -> None:
        # It is never imported unless a dbg.* frame arrives, so a bundle
        # missing it looks perfectly healthy right up to the moment
        # somebody needs to inspect a device that is misbehaving. Flash
        # is not the scarce resource here; heap is, and lazily importing
        # already costs it nothing.
        self.assertIn("buddy/debug.py", OVERLAY)

    def test_the_package_init_ships(self) -> None:
        # MicroPython has no namespace packages: without an `__init__` on
        # flash, `/flash/buddy` is a directory and every `from buddy
        # import ...` in the app raises ImportError.
        self.assertIn("buddy/__init__.py", OVERLAY)

    def test_the_package_init_is_first(self) -> None:
        # push_jobs walks OVERLAY in order and push_file creates the one
        # directory level it needs, so the order does not decide whether
        # /flash/buddy exists. It decides what a half-finished transfer
        # leaves behind: a package with no __init__ imports as neither a
        # module nor a directory, which is the confusing failure.
        self.assertEqual(OVERLAY[0], "buddy/__init__.py")

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

    def test_the_buddy_package_gets_its_directory_too(self) -> None:
        # /flash/buddy is the other directory a deploy has to create, and
        # unlike apps/ it is created by this repository rather than found
        # already there.
        repl = FakeRepl()
        push_file(repl, self.src, "buddy/serial.py", quiet=True)
        self.assertEqual(repl.made_dirs, [f"{DEST_ROOT}/buddy"])
        self.assertIn(f"{DEST_ROOT}/buddy/serial.py", repl.files)

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
        self.case = cleanup
        root = Path(tmp.name)
        self.vendor = root / "vendor"
        self.build = root / "build"
        self.src = root / "device"
        self.src.mkdir()
        (self.src / LAUNCHER).write_text("print('our launcher')\n")
        self.repl = FakeRepl(dirs={DEST_ROOT, f"{DEST_ROOT}/apps"})
        # Kept as its own reference, not read back off the transport:
        # `FakeRepl.serial` is a plain port and this is the one that
        # answers.
        self.port = _TalkingPort()

    def put(self, path: str, text: str) -> None:
        self.repl.files[f"{DEST_ROOT}/{path}"] = text.encode()

    def seed_unconverted(self) -> None:
        """Flash as it looks before the first deploy: all source."""
        self.repl.answers["sys.implementation._mpy"] = 0x2806
        self.repl.serial = self.port
        for name in UPSTREAM:
            self.put(f"{name}.py", f"print('{name}')\n")
        for rel in OVERLAY:
            self.put(rel, "print('the source this replaces')\n")
        for rel in REMOVE:
            self.put(rel, f"print('{rel}')\n")
        self.put(LAUNCHER, "print('upstream menu + NimBLE')\n")

    def deploy(self, *extra: str) -> int:
        """One pass of main() over this device. Returns its exit code."""
        patch = unittest.mock.patch("buddy_deploy.connect_repl", return_value=self.repl)
        patch.start()
        self.case.addCleanup(patch.stop)
        return main(
            [
                "--port",
                "/dev/null",
                "--build",
                str(self.build),
                "--vendor",
                str(self.vendor),
                *extra,
            ]
        )


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


class SpeechConfirmationTest(unittest.TestCase):
    """The last step: launch what was installed and listen to it.

    A directory listing cannot tell a bundle that imports from one that
    does not, and neither can a successful transfer. This is the step
    that can, so what matters is that a device which will not speak ends
    the run as a failure — and that whatever it printed on the way is
    said out loud in the output rather than being flattened into a
    timeout.
    """

    def setUp(self) -> None:
        self.repl = FakeRepl()
        self.port = _TalkingPort()
        self.repl.serial = self.port
        self.said: list[str] = []

    def answers(self, replies: Replies) -> None:
        self.port.replies = replies

    def run_it(self, text: str = VERIFY_TEXT) -> Message:
        return verify_by_speech(self.repl, "/dev/null", text, ENGINE, self.said.append, settle=0.0)

    @property
    def sent(self) -> list[Message]:
        return self.port.frames

    def test_the_app_is_started_the_one_way_way(self) -> None:
        # exec would wait for a return the app never makes: it takes the
        # console over and speaks its own protocol on the same wire.
        self.run_it()
        self.assertEqual(self.repl.launched, [LAUNCH_SOURCE])

    def test_it_asks_for_the_words_on_the_panel_and_then_out_loud(self) -> None:
        ack = self.run_it("デプロイ完了なのだ")
        self.assertEqual([frame["cmd"] for frame in self.sent], ["chat.say", "speak.say"])
        self.assertEqual([frame["text"] for frame in self.sent], ["デプロイ完了なのだ"] * 2)
        self.assertEqual(self.sent[1]["url"], ENGINE)
        self.assertEqual(ack["blocks"], 40)

    def test_the_port_the_link_took_over_is_closed_once(self) -> None:
        # The transport is not closed by the caller after this, so the
        # link is the only thing that can release the port.
        self.run_it()
        self.assertTrue(self.repl.serial.closed)
        self.assertFalse(self.repl.closed)

    def test_a_device_that_refuses_fails_the_deploy(self) -> None:
        self.answers(
            {
                "chat.say": [{"ack": "chat.say", "ok": True}],
                "speak.say": [{"ack": "speak.say", "ok": False, "err": "ECONNREFUSED"}],
            }
        )
        with self.assertRaises(DeployError) as caught:
            self.run_it()
        self.assertIn("ECONNREFUSED", str(caught.exception))

    def test_playback_that_ends_early_is_not_a_confirmation(self) -> None:
        self.answers(
            {
                "chat.say": [{"ack": "chat.say", "ok": True}],
                "speak.say": [
                    {"ack": "speak.say", "ok": True, "bytes": 81920, "rate": 16000},
                    {"ack": "speak.end", "ok": False, "blocks": 3, "stalls": 0},
                ],
            }
        )
        with self.assertRaises(DeployError) as caught:
            self.run_it()
        self.assertIn("ended early", str(caught.exception))

    def test_what_the_app_printed_while_failing_is_reported(self) -> None:
        # The case this exists for: the bundle does not import. Without
        # the traceback the only symptom is an ack that never came.
        self.answers({})
        self.port.feed(b"Traceback (most recent call last):\r\n")
        self.port.feed(b"ImportError: no module named 'buddy_tts'\r\n")
        with (
            unittest.mock.patch("buddy_deploy.VERIFY_TIMEOUT_S", 0.05),
            self.assertRaises(DeployError),
        ):
            self.run_it()
        self.assertTrue(
            any("ImportError" in line for line in self.said),
            f"the device's own output never made it into the log: {self.said}",
        )

    def test_a_loopback_engine_is_refused_before_the_repl_is_spent(self) -> None:
        # Reachable from this Mac and from nowhere else. Discovered
        # after the launch it costs another round trip to try again.
        with self.assertRaises(DeployError):
            engine_url("http://127.0.0.1:50021")


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

    `--no-speak`, so this stays about the flash rewriting; the launch
    and the utterance have their own tests.
    """

    extra_args: tuple[str, ...] = ("--no-speak",)

    def setUp(self) -> None:
        self.bench = _Bench(self)
        self.bench.seed_unconverted()
        self.repl = self.bench.repl
        self.rc = self.bench.deploy(*self.extra_args)

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


class NoSpeakTest(unittest.TestCase):
    """The escape hatch, and the reason anyone would want it.

    Its own run rather than an assertion on `WholeRunTest`, which the
    confirmation subclass inherits from: what is being asserted here is
    exactly what that subclass inverts.
    """

    def test_it_leaves_the_device_at_the_repl(self) -> None:
        # The REPL is what the next deploy needs. Getting it back once
        # the app has the console costs an interrupt and a teardown.
        bench = _Bench(self)
        bench.seed_unconverted()
        self.assertEqual(bench.deploy("--no-speak"), 0)
        self.assertEqual(bench.repl.launched, [])
        self.assertTrue(bench.repl.closed)


class WholeRunWithConfirmationTest(WholeRunTest):
    """The same pass, ending the way a real deploy ends: out loud.

    Everything `WholeRunTest` asserts still has to hold — the ordering
    is inherited rather than restated — and what is added is that the
    run does not stop at a successful transfer.
    """

    extra_args = ("--engine", ENGINE, "--settle", "0")

    def test_it_launched_the_app_and_got_its_utterance_played(self) -> None:
        self.assertEqual(self.repl.launched, [LAUNCH_SOURCE])
        frames = self.bench.port.frames
        self.assertEqual([frame["cmd"] for frame in frames], ["chat.say", "speak.say"])
        self.assertEqual(frames[-1]["text"], VERIFY_TEXT)

    def test_the_transport_is_not_closed_over_the_top_of_the_link(self) -> None:
        # mpremote's close() drops RTS/DTR, which on a port the link has
        # already closed raises out of the finally block.
        self.assertFalse(self.repl.closed)
        self.assertTrue(self.repl.serial.closed)

    def test_the_port_gets_a_read_timeout(self) -> None:
        # Superseded: the launch hands the port to the link, which polls
        # `in_waiting` and wants the short timeout instead.
        self.assertEqual(self.repl.serial.timeout, DEFAULT_READ_TIMEOUT)


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

    def test_the_old_flat_layout_is_removed(self) -> None:
        # Not archived, unlike REMOVE: these are this repository's own
        # modules and the source is in git.
        for rel in STALE:
            self.bench.put(rel, f"print('{rel}')\n")
        prune_stale(self.bench.repl, _forever(), _silent)
        for rel in STALE:
            self.assertNotIn(f"{DEST_ROOT}/{rel}", self.bench.repl.files)
            self.assertFalse((self.bench.vendor / rel).exists())

    def test_stale_names_no_longer_collide_with_what_is_pushed(self) -> None:
        # A name in both lists would mean the deploy deletes what it just
        # wrote — silently, since prune_stale runs after push_jobs.
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
