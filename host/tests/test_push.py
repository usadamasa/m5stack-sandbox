"""Transfer tests for the overlay pusher.

Two things here are worth a test without hardware attached. First, the
paste blocks: they are source code assembled by string formatting and
executed on the device, so a quoting slip is a runtime error on the
board rather than anything CPython would notice. Second, the two guards
that exist because their absence once looked like success — refusing to
push when no REPL answers, and refusing to call a transfer done when
the device reports a different size than we sent.
"""

import base64
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import buddy_push
from buddy_push import (
    PROMPT,
    PushError,
    ReplSession,
    chunk_script,
    close_script,
    iter_chunks,
    main,
    open_script,
    push_file,
)


class _NoSleep:
    """Stand in for `time`, so the settle delays do not cost real seconds."""

    @staticmethod
    def sleep(_seconds: float) -> None:
        return None


class FakeDevice:
    """A serial port that answers paste blocks from a canned script.

    Everything written is recorded; every Ctrl-D (0x04) pops the next
    canned response and queues it for the next read.
    """

    def __init__(self, responses: list[bytes], *, prompt: bool = True) -> None:
        self._responses = list(responses)
        self._prompt = prompt
        self._outbound = bytearray()
        self.written = bytearray()
        self._lock = threading.Lock()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._outbound)

    def read(self, size: int = 1, /) -> bytes:
        with self._lock:
            data = bytes(self._outbound[:size])
            del self._outbound[:size]
        return data

    def write(self, data: bytes, /) -> int:
        with self._lock:
            self.written += data
            if data == b"\r\n" and self._prompt:
                self._outbound += b">>> "
            if b"\x04" in data:
                nxt = self._responses.pop(0) if self._responses else b""
                self._outbound += nxt
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._outbound.clear()


class ScriptTest(unittest.TestCase):
    def test_root_file_opens_without_mkdir(self) -> None:
        script = open_script("buddy_serial.py")
        self.assertIn("fp = open('/flash/buddy_serial.py', 'wb')", script)
        self.assertNotIn("uos.mkdir", script)

    def test_nested_file_creates_its_directory(self) -> None:
        script = open_script("apps/claude_buddy.py")
        self.assertIn("uos.mkdir('/flash/apps')", script)
        self.assertIn("fp = open('/flash/apps/claude_buddy.py', 'wb')", script)

    def test_every_block_is_a_single_line(self) -> None:
        # Paste mode is line-oriented and the try/except pair below has
        # to survive as two one-liners; a block that grew an indented
        # continuation would be mangled on the way in.
        for line in open_script("apps/claude_buddy.py").splitlines():
            self.assertFalse(line.startswith(" "), line)

    def test_chunk_script_carries_the_bytes(self) -> None:
        payload = b"\x00\xff'\"\n binary"
        script = chunk_script(payload)
        encoded = script.split("'")[1]
        self.assertEqual(base64.b64decode(encoded), payload)

    def test_chunk_script_is_quote_safe(self) -> None:
        # base64 never emits a quote, which is what makes the single
        # quoting around the payload safe. Assert it rather than assume
        # it: the only quotes in the block must be the two delimiters.
        script = chunk_script(bytes(range(256)))
        self.assertEqual(script.count("'"), 2)

    def test_close_script_reports_both_sizes(self) -> None:
        script = close_script("buddy_serial.py", 1234)
        self.assertIn("uos.stat('/flash/buddy_serial.py')[6]", script)
        self.assertIn("1234", script)

    def test_iter_chunks_partitions_exactly(self) -> None:
        data = bytes(range(256)) * 4
        chunks = list(iter_chunks(data, 100))
        self.assertEqual(b"".join(chunks), data)
        self.assertEqual([len(c) for c in chunks[:-1]], [100] * (len(chunks) - 1))


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        real_time = buddy_push.time
        buddy_push.time = _NoSleep()
        self.addCleanup(setattr, buddy_push, "time", real_time)

    def test_ensure_prompt_accepts_a_live_repl(self) -> None:
        device = FakeDevice([], prompt=True)
        ReplSession(device).ensure_prompt()
        self.assertIn(b"\x03", device.written)

    def test_ensure_prompt_refuses_a_silent_device(self) -> None:
        # What a running Buddy app looks like: Ctrl-C is disabled, so
        # nothing answers. Pushing anyway would write into a void.
        device = FakeDevice([], prompt=False)
        with self.assertRaises(PushError) as caught:
            ReplSession(device).ensure_prompt()
        self.assertIn("BtnRST", str(caught.exception))

    def test_prompt_bytes_match_micropython(self) -> None:
        self.assertEqual(PROMPT, b">>>")


class PushFileTest(unittest.TestCase):
    def setUp(self) -> None:
        real_time = buddy_push.time
        buddy_push.time = _NoSleep()
        self.addCleanup(setattr, buddy_push, "time", real_time)

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = Path(self._tmp.name) / "buddy_serial.py"
        self.src.write_bytes(b"print('hi')\n")

    def _responses(self, confirmed: int) -> list[bytes]:
        size = self.src.stat().st_size
        return [
            b"",  # open block
            b"",  # single chunk
            f"PUSHED buddy_serial.py {confirmed} {size}\n".encode(),
        ]

    def test_reports_the_size_the_device_confirmed(self) -> None:
        size = self.src.stat().st_size
        device = FakeDevice(self._responses(confirmed=size))
        written = push_file(ReplSession(device), self.src, "buddy_serial.py", quiet=True)
        self.assertEqual(written, size)

    def test_short_write_is_an_error(self) -> None:
        # The failure this guard exists for: the transfer looks fine and
        # the file on flash is truncated.
        device = FakeDevice(self._responses(confirmed=3))
        with self.assertRaises(PushError) as caught:
            push_file(ReplSession(device), self.src, "buddy_serial.py", quiet=True)
        self.assertIn("did not confirm", str(caught.exception))

    def test_traceback_while_opening_stops_the_transfer(self) -> None:
        device = FakeDevice([b"Traceback (most recent call last):\nOSError: 28\n"])
        with self.assertRaises(PushError) as caught:
            push_file(ReplSession(device), self.src, "buddy_serial.py", quiet=True)
        self.assertIn("could not open", str(caught.exception))


class CliTest(unittest.TestCase):
    def test_missing_source_exits_before_touching_the_port(self) -> None:
        # No serial port is opened, so a wrong --src cannot leave the
        # device half-written.
        rc = main(["--port", "/dev/null", "--src", "/nonexistent", "--files", "nope.py"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
