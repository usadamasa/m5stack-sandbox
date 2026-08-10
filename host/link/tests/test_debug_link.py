"""Host side of the device debug channel.

Two things live here. `debug()` is a thin wrapper over `request` — thin
enough that the only interesting parts are the verb whitelist and the
fact that the ack name equals the command name, which is what lets one
helper serve seven verbs.

`interrupt()` is the odd one: a single raw byte sent *outside* the
sentinel framing, because MicroPython's console reader takes 0x03 before
any Python on the device sees it. Nothing acks it. Getting that byte
right is the difference between dropping a running app to the REPL and
sending it a character it will ignore.
"""

import contextlib
import io
import unittest
from unittest import mock

import buddy_bridge
from buddy_bridge import DEBUG_OPS, SENTINEL, BuddyLink, Message, debug, encode


class FakeIO:
    """The `SerialPort` surface, recording writes and replaying one ack."""

    def __init__(self, replies: bytes = b"") -> None:
        self.written = bytearray()
        self._inbound = bytearray(replies)

    @property
    def in_waiting(self) -> int:
        return len(self._inbound)

    def read(self, size: int = 1, /) -> bytes:
        data = bytes(self._inbound[:size])
        del self._inbound[:size]
        return data

    def write(self, data: bytes, /) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeRequester:
    """Satisfies both `Requester` and `SpeechLink`.

    `await_ack` is there for the second: `speak` sends and then waits
    separately, because the device answers a `speak.say` twice.
    """

    def __init__(self, ack: Message | None = None) -> None:
        self.sent: list[tuple[Message, str]] = []
        self.ack: Message = ack if ack is not None else {"ok": True}

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        self.sent.append((obj, expect))
        return dict(self.ack, ack=expect)

    def send(self, obj: Message) -> None:
        self.sent.append((obj, ""))

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message:
        return dict(self.ack, ack=expect)


class DebugHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.link = FakeRequester()

    def test_verb_is_prefixed_and_acked_under_the_same_name(self) -> None:
        # device/buddy_debug.py sets ack == cmd, which is what keeps this
        # from needing a per-verb table.
        ack = debug(self.link, "mem")
        self.assertEqual(self.link.sent, [({"cmd": "dbg.mem"}, "dbg.mem")])
        self.assertEqual(ack["ack"], "dbg.mem")

    def test_every_advertised_op_is_accepted(self) -> None:
        for op in DEBUG_OPS:
            with self.subTest(op=op):
                debug(self.link, op, src="1")

    def test_unknown_op_is_refused_before_the_wire(self) -> None:
        # A typo would otherwise reach the device, match no verb, fall
        # through to buddy_protocol and come back as nothing at all —
        # a timeout five seconds later with no explanation.
        with self.assertRaises(ValueError):
            debug(self.link, "memory")
        self.assertEqual(self.link.sent, [])

    def test_eval_carries_its_source(self) -> None:
        debug(self.link, "eval", src="gc.mem_free()")
        obj, _expect = self.link.sent[0]
        self.assertEqual(obj["src"], "gc.mem_free()")

    def test_eval_without_source_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            debug(self.link, "eval")

    def test_non_eval_ops_carry_no_source(self) -> None:
        debug(self.link, "gc", src="ignored")
        obj, _expect = self.link.sent[0]
        self.assertNotIn("src", obj)


class InterruptTest(unittest.TestCase):
    def test_buddylink_writes_a_bare_ctrl_c(self) -> None:
        io = FakeIO()
        link = BuddyLink("/dev/fake").open(adopt=io)
        link.interrupt()
        # No sentinel, no newline, no JSON. The console reader consumes
        # this byte itself; framing it would only make it invisible.
        self.assertEqual(bytes(io.written), b"\x03")
        self.assertNotIn(SENTINEL, bytes(io.written))

    def test_interrupt_does_not_disturb_the_framing(self) -> None:
        io = FakeIO()
        link = BuddyLink("/dev/fake").open(adopt=io)
        link.interrupt()
        link.send({"cmd": "status"})
        self.assertEqual(bytes(io.written), b"\x03" + encode({"cmd": "status"}))

    def test_module_exports_the_helpers(self) -> None:
        # They are part of the surface buddy_mcp and buddy_deploy import.
        for name in ("debug", "DEBUG_OPS"):
            self.assertTrue(hasattr(buddy_bridge, name), name)


class AnnounceTest(unittest.TestCase):
    """Saying "debug mode" out loud when the device enters it.

    The device is the only side that knows: it reports `entered` on the
    frame that pulled `buddy_debug` in, because a fresh CLI process
    cannot tell whether an earlier one already loaded it.
    """

    def setUp(self) -> None:
        self.spoken: list[str] = []
        patch = mock.patch.object(
            buddy_bridge,
            "speak",
            side_effect=lambda _link, text, **_kw: self.spoken.append(text) or {"ok": True},
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_announces_when_the_device_says_it_entered(self) -> None:
        link = FakeRequester({"ok": True, "entered": True})
        buddy_bridge.announce_debug_entry(link, debug(link, "mem"))
        self.assertEqual(self.spoken, [buddy_bridge.DEBUG_ENTER_TEXT])

    def test_stays_quiet_on_every_later_call(self) -> None:
        link = FakeRequester({"ok": True})
        buddy_bridge.announce_debug_entry(link, debug(link, "mem"))
        self.assertEqual(self.spoken, [])

    def test_a_silent_engine_does_not_fail_the_inspection(self) -> None:
        # VOICEVOX down, WiFi off, speaker unplugged — none of that is a
        # reason for `dbg.mem` to raise. The point of the call is the
        # numbers, and the announcement is a courtesy on top.
        with mock.patch.object(buddy_bridge, "speak", side_effect=OSError("engine unreachable")):
            link = FakeRequester({"ok": True, "entered": True})
            spoke = buddy_bridge.announce_debug_entry(link, debug(link, "mem"))
        self.assertFalse(spoke)


class CliOutputTest(unittest.TestCase):
    """That the CLI actually prints what the device said.

    `pump()` drains as it returns. Calling it and then calling `drain()`
    for the output throws the batch away — which is how `--start` came
    to swallow every startup line, traceback included, while looking
    like it had simply started quietly.
    """

    def _run(self, argv: list[str], inbound: bytes) -> str:
        io_port = FakeIO(inbound)
        out = io.StringIO()
        with (
            mock.patch.object(buddy_bridge.sys, "argv", ["buddy_bridge", *argv]),
            mock.patch.object(buddy_bridge, "launch_app", return_value=io_port),
            mock.patch.object(buddy_bridge.serial, "Serial", return_value=io_port),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(buddy_bridge.main(), 0)
        return out.getvalue()

    def test_start_prints_the_startup_log(self) -> None:
        printed = self._run(
            ["--port", "/dev/fake", "--start", "--settle", "0"],
            b"claude_buddy: run() start\nMemoryError: allocating 776 bytes\n",
        )
        self.assertIn("claude_buddy: run() start", printed)
        self.assertIn("MemoryError", printed)

    def test_watch_prints_what_arrived(self) -> None:
        printed = self._run(
            # Not 0: that is the "do not watch" value, not a zero-length
            # watch.
            ["--port", "/dev/fake", "--watch", "0.05"],
            b"buddy_serial: up as Claude_serial\n",
        )
        self.assertIn("up as Claude_serial", printed)

    def test_dbg_prints_the_ack_and_the_log_behind_it(self) -> None:
        # dbg.frag answers in one line and prints a heap map in twenty.
        printed = self._run(
            [
                "--port",
                "/dev/fake",
                "--dbg",
                "frag",
                "--timeout",
                "1",
            ],
            SENTINEL
            + b'{"ack":"dbg.frag","ok":true,"to":"log"}\n'
            + b"GC: total: 131072, used: 41328, free: 89744\n",
        )
        self.assertIn("dbg.frag:", printed)
        self.assertIn("free: 89744", printed)

    def test_interrupt_sends_the_byte_and_reports_the_reply(self) -> None:
        io_port = FakeIO(b"claude_buddy: at the REPL. machine.reset() to restart.\n")
        out = io.StringIO()
        with (
            mock.patch.object(
                buddy_bridge.sys, "argv", ["buddy_bridge", "--port", "/dev/fake", "--interrupt"]
            ),
            mock.patch.object(buddy_bridge.serial, "Serial", return_value=io_port),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(buddy_bridge.main(), 0)
        self.assertEqual(bytes(io_port.written), b"\x03")
        self.assertIn("at the REPL", out.getvalue())


if __name__ == "__main__":
    unittest.main()
