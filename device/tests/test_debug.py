# pyright: reportPrivateUsage=false
"""Verb dispatch for the on-device debug module.

`device/buddy_debug.py` is the thing you reach for when the device is
misbehaving, which is the worst possible moment to discover that the
module itself is broken. Everything here runs on CPython: the dispatch,
the source-length cap, the repr clipping and the unload handshake are
plain Python, and the two firmware modules it touches (`micropython`,
`esp32`) are replaced with fakes.

What is deliberately not covered: `dbg.frag` beyond "it called
mem_info", because the output goes to stdout and is the host's problem,
and the real `esp32.idf_heap_info`, which has no CPython counterpart.

Whitebox by design: this pokes `_MAX_REPR` / `_MAX_SOURCE` directly, so
basedpyright's private-member check is switched off for this file rather
than silenced at each use.
"""

import json
import unittest
from typing import cast

import buddy_debug
from buddy_debug import _MAX_REPR, _MAX_SOURCE


class _FakeGc:
    """Stand in for MicroPython's gc.

    CPython's stops at `collect()`; `mem_free` and `mem_alloc` are
    MicroPython additions, and they are most of what this module reports.
    Each collect hands back a fixed amount so `freed` has something to be.
    """

    _TOTAL = 160_000

    def __init__(self) -> None:
        self.free = 60_000
        self.collects = 0

    def collect(self) -> None:
        self.collects += 1
        self.free += 1_000

    def mem_free(self) -> int:
        return self.free

    def mem_alloc(self) -> int:
        return self._TOTAL - self.free


class _FakeMicropython:
    def __init__(self) -> None:
        self.mem_info_calls: list[int] = []

    def mem_info(self, verbose: int = 0) -> None:
        self.mem_info_calls.append(verbose)


class _FakeEsp32:
    HEAP_DATA = 4

    def __init__(self, regions: list[tuple[int, int, int, int]] | None = None) -> None:
        # (total, free, largest_free_block, min_free_ever) per region, which
        # is the shape MicroPython's esp32 module documents.
        self.regions = regions if regions is not None else [(200_000, 90_000, 40_000, 70_000)]

    def idf_heap_info(self, _which: int) -> list[tuple[int, int, int, int]]:
        return self.regions


class _Probe:
    """Stands in for a bound app object with the attributes `dbg.state` reads."""

    def __init__(self, active: bool) -> None:
        self.active = active
        self.connected = active
        self.advertised_name = "Claude_serial"


class DebugModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gc = _FakeGc()
        self.micropython = _FakeMicropython()
        self.esp32 = _FakeEsp32()
        for name, fake in (
            ("gc", self.gc),
            ("micropython", self.micropython),
            ("esp32", self.esp32),
        ):
            self.addCleanup(setattr, buddy_debug, name, getattr(buddy_debug, name))
            setattr(buddy_debug, name, fake)

        self.ns: dict[str, object] = {"ble": _Probe(True), "chat": _Probe(False), "answer": 42}
        buddy_debug.bind(self.ns)
        self.addCleanup(buddy_debug.bind, {})

    # ----- dispatch

    def test_unknown_cmd_is_not_ours(self) -> None:
        # buddy_protocol owns "unknown cmd" reporting; returning None is
        # what lets a non-dbg line fall through to it.
        self.assertIsNone(buddy_debug.handle({"cmd": "status"}))

    def test_handle_raw_parses_a_json_frame(self) -> None:
        ack = buddy_debug.handle_raw(b'{"cmd":"dbg.gc"}')
        assert ack is not None
        self.assertEqual(ack["ack"], "dbg.gc")

    def test_handle_raw_ignores_malformed_input(self) -> None:
        self.assertIsNone(buddy_debug.handle_raw(b"not json at all"))
        self.assertIsNone(buddy_debug.handle_raw(b'"a bare string"'))

    def test_id_is_echoed(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.gc", "id": "abc"})
        assert ack is not None
        self.assertEqual(ack["id"], "abc")

    def test_every_ack_is_json_serialisable(self) -> None:
        # The ack goes straight into json.dumps on the way out. A value
        # that cannot be encoded raises inside the transport callback,
        # which on this device means a silent dropped reply.
        for cmd in ("dbg.mem", "dbg.frag", "dbg.gc", "dbg.state", "dbg.off"):
            with self.subTest(cmd=cmd):
                json.dumps(buddy_debug.handle({"cmd": cmd}))

    # ----- memory verbs

    def test_mem_reports_both_heaps(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.mem"})
        assert ack is not None
        self.assertTrue(ack["ok"])
        self.assertIsInstance(ack["free"], int)
        self.assertIsInstance(ack["alloc"], int)
        # The ESP-IDF heap is the one a failing socket allocation comes
        # out of, and gc.mem_free() says nothing about it.
        self.assertEqual(ack["idf_free"], 90_000)
        self.assertEqual(ack["idf_largest"], 40_000)

    def test_mem_survives_a_firmware_without_esp32(self) -> None:
        buddy_debug.esp32 = None
        ack = buddy_debug.handle({"cmd": "dbg.mem"})
        assert ack is not None
        self.assertTrue(ack["ok"])
        self.assertNotIn("idf_free", ack)

    def test_mem_survives_an_empty_region_list(self) -> None:
        self.esp32.regions = []
        ack = buddy_debug.handle({"cmd": "dbg.mem"})
        assert ack is not None
        self.assertNotIn("idf_largest", ack)

    def test_frag_prints_the_verbose_map(self) -> None:
        # The heap map is far too big to marshal into an ack. It goes out
        # as print(), which the transport passes through as a log line.
        ack = buddy_debug.handle({"cmd": "dbg.frag"})
        assert ack is not None
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["to"], "log")
        self.assertEqual(self.micropython.mem_info_calls, [1])

    def test_gc_reports_before_and_after(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.gc"})
        assert ack is not None
        self.assertEqual(ack["freed"], cast(int, ack["after"]) - cast(int, ack["before"]))

    # ----- state

    def test_state_probes_bound_objects(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.state"})
        assert ack is not None
        self.assertTrue(ack["ble.connected"])
        self.assertFalse(ack["chat.active"])
        self.assertEqual(ack["ble.advertised_name"], "Claude_serial")

    def test_state_skips_objects_that_are_not_bound(self) -> None:
        # `speech` is None on a build that never brought the player up.
        buddy_debug.bind({})
        ack = buddy_debug.handle({"cmd": "dbg.state"})
        assert ack is not None
        self.assertEqual(ack, {"ok": True, "ack": "dbg.state"})

    # ----- eval / exec

    def test_eval_sees_the_bound_namespace(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.eval", "src": "answer * 2"})
        assert ack is not None
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["repr"], "84")

    def test_eval_can_reach_builtins(self) -> None:
        # eval() with an explicit globals dict has to still resolve len,
        # sorted and friends, or the escape hatch is useless.
        ack = buddy_debug.handle({"cmd": "dbg.eval", "src": "len('abcd')"})
        assert ack is not None
        self.assertEqual(ack["repr"], "4")

    def test_eval_clips_a_long_repr(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.eval", "src": "'x' * 5000"})
        assert ack is not None
        self.assertEqual(len(cast(str, ack["repr"])), _MAX_REPR + 3)
        self.assertTrue(cast(str, ack["repr"]).endswith("..."))

    def test_eval_refuses_an_overlong_source(self) -> None:
        # Compiling at runtime builds a parse tree and bytecode in the GC
        # heap, which is the fragmentation this bundle went to .mpy to
        # avoid. The cap keeps the escape hatch from reopening it.
        ack = buddy_debug.handle({"cmd": "dbg.eval", "src": "1 + " * _MAX_SOURCE})
        assert ack is not None
        self.assertFalse(ack["ok"])
        self.assertIn("too long", cast(str, ack["err"]))

    def test_eval_refuses_an_empty_source(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.eval"})
        assert ack is not None
        self.assertFalse(ack["ok"])

    def test_eval_reports_an_error_instead_of_raising(self) -> None:
        # This runs inside the transport's on_line callback. An escaping
        # exception would take the app's main loop down with it, which is
        # a spectacular way for a debug tool to behave.
        ack = buddy_debug.handle({"cmd": "dbg.eval", "src": "1 / 0"})
        assert ack is not None
        self.assertFalse(ack["ok"])
        self.assertIn("ZeroDivisionError", cast(str, ack["err"]))

    def test_eval_reports_a_syntax_error(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.eval", "src": "this is not python"})
        assert ack is not None
        self.assertFalse(ack["ok"])

    def test_exec_mutates_the_namespace(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.exec", "src": "answer = 7"})
        assert ack is not None
        self.assertTrue(ack["ok"])
        self.assertEqual(self.ns["answer"], 7)

    def test_exec_reports_an_error_instead_of_raising(self) -> None:
        ack = buddy_debug.handle({"cmd": "dbg.exec", "src": "raise OSError('nope')"})
        assert ack is not None
        self.assertFalse(ack["ok"])
        self.assertIn("OSError", cast(str, ack["err"]))

    # ----- unload

    def test_off_asks_the_caller_to_unload(self) -> None:
        # buddy_debug cannot delete itself from sys.modules — the caller
        # holds the other reference. The flag is the handshake.
        ack = buddy_debug.handle({"cmd": "dbg.off"})
        assert ack is not None
        self.assertTrue(ack["unload"])

    def test_no_other_verb_asks_for_an_unload(self) -> None:
        for cmd in ("dbg.mem", "dbg.frag", "dbg.gc", "dbg.state"):
            with self.subTest(cmd=cmd):
                ack = buddy_debug.handle({"cmd": cmd})
                assert ack is not None
                self.assertNotIn("unload", ack)


if __name__ == "__main__":
    unittest.main()
