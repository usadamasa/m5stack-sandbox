"""走っているアプリを覗く tool のテスト。

REPL を取り上げたアプリの代わりを務める 2 つ。ack に出ない出力が `logs` から
返ること、初回だけ声に出すこと、そしてどちらもデバイスとの往復の間ロックを
握り続けることを見る。
"""

import unittest
from collections.abc import Callable
from unittest import mock

import buddy_mcp
import buddy_verbs
import mcp_debug_tools
from buddy_wire import Message
from device_repl import ReplError
from mcp_stubs import McpTestCase, StubLink


def _recording_speak(store: list[str]) -> Callable[..., Message]:
    """A stand-in for `buddy_verbs.speak` that records the text and acks.

    A plain lambda cannot carry parameter annotations, so `mock.patch.object`
    would otherwise see `side_effect` as an untyped callable.
    """

    def fake(_link: object, text: str, **_kwargs: object) -> Message:
        store.append(text)
        return {"ok": True}

    return fake


def _silent_speak(store: list[str]) -> Callable[..., None]:
    """A stand-in for `buddy_verbs.speak` that records nothing was said."""

    def fake(*_args: object, **_kwargs: object) -> None:
        store.append("")

    return fake


class DebugToolTest(McpTestCase):
    """The tools that stand in for the REPL the running app took away."""

    def test_debug_prefixes_the_verb(self) -> None:
        result = mcp_debug_tools.buddy_debug("mem")
        self.assertEqual(StubLink.instances[0].requests, [({"cmd": "dbg.mem"}, "dbg.mem")])
        self.assertEqual(result["ack"]["ack"], "dbg.mem")

    def test_debug_carries_source_for_eval(self) -> None:
        mcp_debug_tools.buddy_debug("eval", src="gc.mem_free()")
        obj, _expect = StubLink.instances[0].requests[0]
        self.assertEqual(obj["src"], "gc.mem_free()")

    def test_debug_returns_the_log_lines_too(self) -> None:
        # dbg.frag's heap map and a failed dbg.eval's traceback never
        # appear in the ack — they are printed. A tool that returned only
        # the ack would be reporting "ok: true" and nothing else.
        buddy_mcp.buddy_connect()
        StubLink.instances[0].queued_logs = [b"GC: total: 131072, used: 41328, free: 89744"]
        result = mcp_debug_tools.buddy_debug("frag", settle=0.0)
        self.assertEqual(result["logs"], ["GC: total: 131072, used: 41328, free: 89744"])

    def test_debug_names_the_valid_ops_on_a_typo(self) -> None:
        result = mcp_debug_tools.buddy_debug("memory")
        self.assertFalse(result["ok"])
        self.assertIn("mem", result["error"])
        self.assertEqual(StubLink.instances, [])

    def test_entering_debug_mode_is_announced_out_loud(self) -> None:
        # The device sets `entered` on the frame that imported its debug
        # module. Only it knows which one that was.
        spoken: list[str] = []
        with mock.patch.object(buddy_verbs, "speak", side_effect=_recording_speak(spoken)):
            buddy_mcp.buddy_connect()
            StubLink.instances[0].ack_extra = {"entered": True}
            result = mcp_debug_tools.buddy_debug("mem", settle=0.0)
        self.assertEqual(spoken, [buddy_verbs.DEBUG_ENTER_TEXT])
        self.assertTrue(result["announced"])

    def test_later_calls_say_nothing(self) -> None:
        spoken: list[str] = []
        with mock.patch.object(buddy_verbs, "speak", side_effect=_silent_speak(spoken)):
            result = mcp_debug_tools.buddy_debug("mem", settle=0.0)
        self.assertEqual(spoken, [])
        self.assertFalse(result["announced"])

    def test_announce_false_keeps_it_quiet(self) -> None:
        spoken: list[str] = []
        with mock.patch.object(buddy_verbs, "speak", side_effect=_silent_speak(spoken)):
            buddy_mcp.buddy_connect()
            StubLink.instances[0].ack_extra = {"entered": True}
            mcp_debug_tools.buddy_debug("mem", announce=False, settle=0.0)
        self.assertEqual(spoken, [])

    def test_a_silent_engine_does_not_fail_the_inspection(self) -> None:
        with mock.patch.object(buddy_verbs, "speak", side_effect=OSError("engine unreachable")):
            buddy_mcp.buddy_connect()
            StubLink.instances[0].ack_extra = {"entered": True}
            result = mcp_debug_tools.buddy_debug("mem", settle=0.0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["announced"])

    def test_debug_holds_the_lock_while_it_talks(self) -> None:
        mcp_debug_tools.buddy_debug("mem")
        self.assertEqual(StubLink.instances[0].lock_held, [True])

    def test_interrupt_sends_one_ctrl_c(self) -> None:
        buddy_mcp.buddy_connect()
        StubLink.instances[0].queued_logs = [b"claude_buddy: at the REPL."]
        result = mcp_debug_tools.buddy_interrupt(settle=0.0)
        self.assertEqual(StubLink.instances[0].interrupts, 1)
        self.assertEqual(result["logs"], ["claude_buddy: at the REPL."])

    def test_interrupt_over_tcp_is_refused_as_a_result_not_an_exception(self) -> None:
        buddy_mcp.buddy_connect()
        StubLink.instances[0].interrupt_error = ReplError("interrupt needs the USB console")
        result = mcp_debug_tools.buddy_interrupt(settle=0.0)
        self.assertFalse(result["ok"])
        self.assertIn("USB console", result["error"])

    def test_interrupt_does_not_open_a_port_of_its_own(self) -> None:
        # Interrupting a device nobody is connected to would open the
        # port purely to send a byte into the dark, and then hold it.
        result = mcp_debug_tools.buddy_interrupt(settle=0.0)
        self.assertFalse(result["ok"])
        self.assertEqual(StubLink.instances, [])

    def test_interrupt_holds_the_lock(self) -> None:
        buddy_mcp.buddy_connect()
        mcp_debug_tools.buddy_interrupt(settle=0.0)
        self.assertEqual(StubLink.instances[0].lock_held, [True])


if __name__ == "__main__":
    unittest.main()
