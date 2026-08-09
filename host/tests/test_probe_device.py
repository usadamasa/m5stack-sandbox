"""The device probe's two pure halves: what it asks, and what it reads back.

Neither needs a board. What they protect is narrow but real — a probe
that silently reports nothing is worse than one that fails, because the
numbers it feeds into `device/buddy_chat.py` are geometry that nobody
re-derives by hand.
"""

import unittest

from probe_device import CANDIDATE_FONTS, extract, probe_script


class ProbeScriptTest(unittest.TestCase):
    def test_asks_about_every_candidate_font(self) -> None:
        script = probe_script()
        for name in CANDIDATE_FONTS:
            self.assertIn(repr(name), script)

    def test_the_japanese_sample_travels_as_an_escape(self) -> None:
        # The block goes to the device as REPL source text. Sending the
        # literal would make the measurement depend on every layer in
        # between being UTF-8 clean, which is not worth betting on.
        script = probe_script()
        self.assertIn("\\u3042", script)
        self.assertNotIn("あ", script)

    def test_restores_the_font_it_found(self) -> None:
        # setFont is sticky, and this runs against a REPL somebody is
        # about to use for something else.
        self.assertIn("L.setFont(L.FONTS.DejaVu9)", probe_script())


class ExtractTest(unittest.TestCase):
    def test_picks_report_lines_out_of_the_echo(self) -> None:
        echoed = (
            "paste mode; Ctrl-C to cancel\n"
            "=== import M5, gc\n"
            "PROBE fonts ['DejaVu9']\n"
            "PROBE metric DejaVu9 h 15 ja 9 ascii 10 indent 10\n"
            ">>> \n"
        )
        self.assertEqual(
            extract(echoed),
            ["PROBE fonts ['DejaVu9']", "PROBE metric DejaVu9 h 15 ja 9 ascii 10 indent 10"],
        )

    def test_survives_a_prompt_fragment_in_front(self) -> None:
        self.assertEqual(extract("=== PROBE heap 41008"), ["PROBE heap 41008"])

    def test_ignores_the_echoed_source_that_mentions_the_tag(self) -> None:
        # Every print() in the block contains the tag; echoing them back
        # as results would double every line of the report.
        self.assertEqual(extract("=== print('PROBE heap', gc.mem_free())"), [])

    def test_no_output_is_no_lines(self) -> None:
        self.assertEqual(extract(">>> \n"), [])


if __name__ == "__main__":
    unittest.main()
