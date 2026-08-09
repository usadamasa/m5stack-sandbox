"""What is left of the pusher once mpremote owns the transfer.

The paste blocks, the chunking and the REPL handshake are gone — those
were a reimplementation of `mpremote fs cp` and are now the real thing,
tested upstream. What remains is specific to this overlay: where files
land, the one directory level the install layout needs, and the guard
that exists because its absence once looked like success — refusing to
call a transfer done when the device reports a different size than we
sent.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from buddy_push import DEST_ROOT, main, push_file
from device_repl import ReplError
from fake_repl import FakeRepl


class PushFileTest(unittest.TestCase):
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
        seen: list[tuple[int, int]] = []

        class _Watching(FakeRepl):
            def fs_writefile(
                self,
                dest: str,
                data: bytes,
                chunk_size: int = 256,
                progress_callback: object = None,
            ) -> None:
                seen.append((chunk_size, progress_callback is not None))  # type: ignore[arg-type]
                super().fs_writefile(dest, data)

        push_file(_Watching(), self.src, "buddy_serial.py", quiet=True)
        self.assertEqual(seen[0][1], False)


class CliTest(unittest.TestCase):
    def test_missing_source_exits_before_touching_the_port(self) -> None:
        # No serial port is opened, so a wrong --src cannot leave the
        # device half-written.
        rc = main(["--port", "/dev/null", "--src", "/nonexistent", "--files", "nope.py"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
