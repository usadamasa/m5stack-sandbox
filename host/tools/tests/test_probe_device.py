"""The device probe, now that its answers are objects rather than text.

The tagged-line format and the echo scraper are gone: the raw REPL hands
`eval` a `repr` and it is parsed here, so there is no transcript to pick
report lines out of. What is left worth protecting is narrow but real —
a probe that changes the device it is measuring, and a measurement that
gets mislabelled on the way back. The numbers feed the geometry in
`device/buddy/chat.py`, which nobody re-derives by hand.
"""

from __future__ import annotations

import unittest

# buddy-host-link は workspace 内の同居パッケージで py.typed が無いため
# stub 未整備扱いになる。py.typed の追加は host/link 側の担当範囲。
from fake_repl import FakeRepl
from probe_device import (
    CANDIDATE_FONTS,
    METRIC_FIELDS,
    probe_display,
    probe_heap,
    probe_network,
)


def _display_repl(metrics: dict[str, tuple[int, int, int, int]] | None = None) -> FakeRepl:
    return FakeRepl(
        {
            "_m": {"EFontJA24": (27, 23, 12, 24)} if metrics is None else metrics,
            "sorted(n for n in dir(M5.Lcd.FONTS) if not n.startswith('_'))": ["DejaVu9"],
            "sorted(n for n in dir(M5.Speaker) if not n.startswith('_'))": ["playRaw"],
        }
    )


class ProbeDisplayTest(unittest.TestCase):
    def test_asks_about_every_candidate_font(self) -> None:
        repl = _display_repl()
        probe_display(repl)
        for name in CANDIDATE_FONTS:
            self.assertIn(repr(name), repl.source)

    def test_the_japanese_sample_travels_as_an_escape(self) -> None:
        # It reaches the device as source text. Sending the literal
        # would make the measurement depend on every layer in between
        # being UTF-8 clean, which is not worth betting on.
        repl = _display_repl()
        probe_display(repl)
        self.assertIn("\\u3042", repl.source)
        self.assertNotIn("あ", repl.source)

    def test_puts_the_small_font_back(self) -> None:
        # setFont is sticky, and this runs against a REPL somebody is
        # about to use for something else. Leaving a 24 px face selected
        # makes the launcher redraw its footer at the wrong size — a
        # change to a device this tool documents itself as not changing.
        repl = _display_repl()
        probe_display(repl)
        self.assertTrue(repl.source.rstrip().endswith("_L.setFont(_L.FONTS.DejaVu9)"))

    def test_labels_the_measurements(self) -> None:
        # The device returns a bare tuple. Mixing up the Japanese and
        # ASCII widths would silently halve the panel's budget.
        repl = _display_repl({"EFontJA24": (27, 23, 12, 24)})
        got = probe_display(repl)
        self.assertEqual(
            got["metrics"]["EFontJA24"],
            dict(zip(METRIC_FIELDS, (27, 23, 12, 24), strict=True)),
        )

    def test_a_font_the_build_lacks_is_absent_not_an_error(self) -> None:
        repl = _display_repl({})
        self.assertEqual(probe_display(repl)["metrics"], {})


class ProbeNetworkTest(unittest.TestCase):
    def _repl(self, http: str | None) -> FakeRepl:
        def on_exec(command: str) -> None:
            if command.startswith("import ") and " as _http" in command:
                name = command.split()[1]
                if name != http:
                    raise ImportError(name)

        return FakeRepl(
            {
                "sorted(n for n in dir(_http) if not n.startswith('_'))": ["get", "post"],
                "sorted(n for n in dir(_http.Response) if not n.startswith('_'))": ["raw", "text"],
                "sorted(n for n in dir(_s) if not n.startswith('_'))": ["readinto", "settimeout"],
                "(_w.active(), _w.isconnected(), _w.ifconfig())": (True, False, ("0.0.0.0",)),
            },
            on_exec=on_exec,
        )

    def test_finds_the_module_this_firmware_actually_ships(self) -> None:
        # Renamed from urequests in 1.20. Every article about this is
        # written against the old name, and device/buddy/tts.py has to
        # import whichever one is there.
        got = probe_network(self._repl("requests"))
        self.assertEqual(got["http"]["module"], "requests")

    def test_asks_whether_the_response_can_be_streamed(self) -> None:
        # `Response.raw` is the whole RAM design. Without it the only
        # route to the body is `content`, which lands a full utterance
        # in heap at once instead of a block at a time.
        got = probe_network(self._repl("requests"))
        self.assertIn("raw", got["http"]["response"])

    def test_falls_back_to_the_old_name(self) -> None:
        got = probe_network(self._repl("urequests"))
        self.assertEqual(got["http"]["module"], "urequests")

    def test_no_http_client_is_reported_rather_than_raised(self) -> None:
        # The point of a probe is to find out. A build with neither
        # module is an answer, not a crash — and the socket and the
        # radio still get reported.
        got = probe_network(self._repl(None))
        self.assertIsNone(got["http"])
        self.assertIn("settimeout", got["socket"])
        self.assertEqual(got["wlan"][0], True)

    def test_closes_the_socket_it_opened(self) -> None:
        # LWIP has few slots and the REPL stays up afterwards.
        repl = self._repl("requests")
        probe_network(repl)
        self.assertIn("_s.close()", repl.source)

    def test_leaves_the_radio_alone(self) -> None:
        # The launcher has already associated by the time anyone runs
        # this. Bringing the interface up or down would change the very
        # thing being measured.
        repl = self._repl("requests")
        probe_network(repl)
        self.assertNotIn("active(True)", repl.source)
        self.assertNotIn("connect(", repl.source)


class ProbeHeapTest(unittest.TestCase):
    def test_collects_before_measuring(self) -> None:
        # Free heap before a collection is whatever the last allocation
        # happened to leave, which is not a number anything can be sized
        # against.
        repl = FakeRepl({"gc.mem_free()": 61248})
        self.assertEqual(probe_heap(repl), 61248)
        self.assertIn("gc.collect()", repl.source)


if __name__ == "__main__":
    unittest.main()
