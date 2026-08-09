"""Framing tests for the host-side Buddy bridge.

The device multiplexes two things onto one USB CDC channel: protocol
lines (sentinel-prefixed JSON) and plain print() logging. Everything
interesting about the host side lives in telling those apart across
arbitrary chunk boundaries, so that is what these cover.

Run with:
    python -m unittest discover -s host/tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from buddy_bridge import SENTINEL, LineDemux, encode  # noqa: E402


class TestEncode(unittest.TestCase):
    def test_prefixes_sentinel_and_terminates_with_newline(self):
        raw = encode({"cmd": "status"})
        self.assertTrue(raw.startswith(SENTINEL))
        self.assertTrue(raw.endswith(b"\n"))

    def test_body_is_compact_json(self):
        raw = encode({"cmd": "name", "name": "buddy"})
        body = raw[len(SENTINEL) : -1]
        # No incidental whitespace — the device buffers this and we cap
        # the line length, so bytes matter.
        self.assertNotIn(b" ", body)
        self.assertEqual(body, b'{"cmd":"name","name":"buddy"}')

    def test_payload_newline_stays_escaped(self):
        # A literal newline in the payload would split one message into
        # two on the device and desynchronise the stream. JSON escaping
        # is what prevents that, so assert the property directly rather
        # than adding a redundant guard: exactly one newline, at the end.
        raw = encode({"cmd": "name", "name": "a\nb"})
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertIn(rb"a\nb", raw)


class TestLineDemux(unittest.TestCase):
    def setUp(self):
        self.d = LineDemux()

    def test_separates_protocol_from_log(self):
        out = self.d.feed(b"buddy_serial: up\n" + SENTINEL + b'{"ack":"status"}\n')
        self.assertEqual(
            out,
            [("log", b"buddy_serial: up"), ("protocol", b'{"ack":"status"}')],
        )

    def test_reassembles_across_chunk_boundary(self):
        whole = SENTINEL + b'{"ack":"status"}\n'
        first, second = whole[:5], whole[5:]
        self.assertEqual(self.d.feed(first), [])
        self.assertEqual(self.d.feed(second), [("protocol", b'{"ack":"status"}')])

    def test_splits_inside_the_sentinel_itself(self):
        # The nastiest boundary: the prefix arrives in two reads, so a
        # naive startswith() on a partial buffer would misclassify.
        whole = SENTINEL + b'{"ok":true}\n'
        cut = len(SENTINEL) - 2
        self.assertEqual(self.d.feed(whole[:cut]), [])
        self.assertEqual(self.d.feed(whole[cut:]), [("protocol", b'{"ok":true}')])

    def test_strips_carriage_return(self):
        out = self.d.feed(SENTINEL + b'{"ok":true}\r\n')
        self.assertEqual(out, [("protocol", b'{"ok":true}')])

    def test_sentinel_must_be_at_line_start(self):
        # A log line that happens to contain the sentinel bytes later on
        # is still a log line.
        line = b"echo: " + SENTINEL + b'{"nope":1}\n'
        out = self.d.feed(line)
        self.assertEqual(out, [("log", line[:-1])])

    def test_empty_protocol_payload_is_dropped(self):
        self.assertEqual(self.d.feed(SENTINEL + b"\n"), [])

    def test_blank_log_lines_are_dropped(self):
        self.assertEqual(self.d.feed(b"\n\n"), [])

    def test_multiple_messages_in_one_chunk(self):
        chunk = SENTINEL + b'{"a":1}\n' + SENTINEL + b'{"b":2}\n'
        self.assertEqual(
            self.d.feed(chunk),
            [("protocol", b'{"a":1}'), ("protocol", b'{"b":2}')],
        )

    def test_partial_trailing_line_is_held(self):
        self.assertEqual(self.d.feed(SENTINEL + b'{"a":1}'), [])
        self.assertEqual(self.d.feed(b"\n"), [("protocol", b'{"a":1}')])


if __name__ == "__main__":
    unittest.main()
