# pyright: reportPrivateUsage=false
"""書体の選択と、VLW の読み込み。

`device/buddy/chat_font.py` は driver へ書体を載せて外し、載っている間の
文字送りと行高を測る。見えるところは `ChatPanel.info()` に出る書体名・倍率・
行数なので、テストもそこから見る。fake の尺度と幾何は `chat_fakes.py`。

切り落としのテストが `buddy.chat._MAX_MESSAGES` を直接読むので、
basedpyright の private-member の検査はこのファイルごと切ってある
(冒頭の `reportPrivateUsage=false`)。
"""

import tempfile
import unittest
from pathlib import Path

from buddy import chat as buddy_chat
from buddy import chat_font
from buddy.chat import ChatPanel
from chat_fakes import NARROW_ROWS, ROWS, WIDE_H_RAW, FakeLcd, panel_without_vlw


class FontTest(unittest.TestCase):
    def test_japanese_pulls_the_panel_onto_the_cjk_face(self) -> None:
        panel = panel_without_vlw(FakeLcd())
        # Nothing wide yet: stay on the short face and keep the rows.
        self.assertEqual(panel.info()["font"], "DejaVu12")
        self.assertEqual(panel.info()["rows"], NARROW_ROWS)

        panel.say("claude", "テスト")
        self.assertEqual(panel.info()["font"], "EFontJA24")
        self.assertEqual(panel.info()["rows"], ROWS)

    def test_clearing_gives_the_rows_back(self) -> None:
        panel = panel_without_vlw(FakeLcd())
        panel.say("claude", "テスト")
        panel.clear()
        self.assertEqual(panel.info()["font"], "DejaVu12")

    def test_evicting_the_last_japanese_message_gives_the_rows_back(self) -> None:
        # _refresh_wide recomputes over the whole transcript rather than
        # latching, so scrolling the only Japanese message out of the
        # buffer drops back to the short face.
        panel = panel_without_vlw(FakeLcd())
        panel.say("claude", "テスト")
        for i in range(buddy_chat._MAX_MESSAGES):
            panel.say("claude", f"ascii {i}")
        self.assertEqual(panel.info()["font"], "DejaVu12")

    def test_cjk_reports_the_build_not_the_selection(self) -> None:
        # False here means "this board cannot draw Japanese at all",
        # which is a different problem from "the transcript happens to
        # be Latin right now". Conflating them would send someone
        # looking for a transfer bug that is not there.
        self.assertTrue(panel_without_vlw(FakeLcd()).info()["cjk"])
        latin_only = FakeLcd(fonts=("DejaVu12", "DejaVu9"))
        self.assertFalse(panel_without_vlw(latin_only).info()["cjk"])

    def test_falls_back_when_the_build_has_no_cjk_font(self) -> None:
        panel = panel_without_vlw(FakeLcd(fonts=("DejaVu12", "DejaVu9")))
        panel.say("claude", "テスト")
        self.assertEqual(panel.info()["font"], "DejaVu12")

    def test_base_font_and_scale_are_restored_after_drawing(self) -> None:
        # BuddyUI paints the footer and hint strip and assumes DejaVu9 at
        # 1:1 is still selected. Both setFont and setTextSize are sticky
        # on this driver, so leaving either behind resizes its chrome.
        lcd = FakeLcd()
        panel = panel_without_vlw(lcd)
        panel.say("claude", "テスト")
        panel.render()
        self.assertEqual(lcd.font, "<font DejaVu9>")
        self.assertEqual(lcd.text_size, chat_font.BASE_SCALE)

    def test_the_scale_is_applied_while_drawing(self) -> None:
        # Not just restored afterwards: if the bracket never applied it,
        # every measurement would be taken at 1:1 and the panel would
        # wrap for a font size it is not drawing at.
        lcd = FakeLcd()
        panel = panel_without_vlw(lcd)
        panel.say("claude", "テスト")
        self.assertEqual(panel.info()["scale"], chat_font.WIDE_SCALE)
        self.assertEqual(panel.info()["rows"], ROWS)

    def test_a_driver_without_fonts_still_works(self) -> None:
        lcd = FakeLcd(fonts=())
        panel = panel_without_vlw(lcd)
        panel.say("claude", "hi")
        panel.render()
        self.assertEqual(panel.info()["font"], "default")
        self.assertEqual(lcd.rows(), ["hi"])

    def test_a_driver_without_settextsize_still_works(self) -> None:
        # An older build without the call should measure and draw at 1:1
        # rather than raise once per repaint.
        lcd = FakeLcd(omit=("setTextSize",))
        panel = panel_without_vlw(lcd)
        panel.say("claude", "テスト")
        panel.render()
        self.assertEqual(panel.info()["rows"], 110 // WIDE_H_RAW)


class VlwTest(unittest.TestCase):
    """The generated Japanese face, which is a file rather than a font."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        # Content is never read on the host — `_resolve_vlw` only stats —
        # so what matters is that the path exists.
        self.vlw = Path(self._dir.name) / "buddy-ja.vlw"
        self.vlw.write_bytes(b"\0" * 24)
        self.lcd = FakeLcd()
        self.panel = ChatPanel(lcd=self.lcd, vlw_path=str(self.vlw))

    def test_japanese_takes_the_vlw_over_the_built_in_face(self) -> None:
        self.panel.say("claude", "テスト")
        info = self.panel.info()
        self.assertEqual(info["font"], chat_font.VLW_NAME)
        self.assertTrue(info["vlw"])
        self.assertEqual(info["scale"], chat_font.VLW_SCALE)
        self.assertIn(str(self.vlw), self.lcd.load_calls)

    def test_latin_stays_on_the_narrow_face(self) -> None:
        # The VLW is the tall face. An ASCII transcript fits more on
        # DejaVu12, so having a VLW installed must not cost those rows.
        self.panel.say("claude", "buddy_chat.py")
        self.assertEqual(self.panel.info()["font"], "DejaVu12")

    def test_the_face_is_reloaded_after_the_bracket_drops_it(self) -> None:
        # Restoring the base font unloads the VLW on this driver, so a
        # second repaint has to load it again — otherwise it would draw
        # Japanese in DejaVu9.
        self.panel.say("claude", "テスト")
        self.panel.render()
        first = len(self.lcd.load_calls)
        self.panel.render()
        self.assertEqual(len(self.lcd.load_calls), first + 1)
        self.assertIsNone(self.lcd.loaded)

    def test_a_missing_file_falls_back_to_the_built_in_face(self) -> None:
        panel = ChatPanel(lcd=FakeLcd(), vlw_path=str(self.vlw) + ".nope")
        panel.say("claude", "テスト")
        info = panel.info()
        self.assertEqual(info["font"], "EFontJA24")
        self.assertFalse(info["vlw"])
        # Still able to draw Japanese, just less of it.
        self.assertTrue(info["cjk"])

    def test_a_driver_without_loadfont_falls_back(self) -> None:
        lcd = FakeLcd(omit=("loadFont",))
        panel = ChatPanel(lcd=lcd, vlw_path=str(self.vlw))
        panel.say("claude", "テスト")
        self.assertEqual(panel.info()["font"], "EFontJA24")

    def test_a_driver_that_rejects_the_file_gives_up_on_it(self) -> None:
        # loadFont is silent about a bad file, but it can still raise on
        # a call it does not like. Retrying that every repaint would
        # print a traceback per frame and never draw anything else.
        lcd = FakeLcd()

        def explode(_font: str) -> None:
            msg = "no"
            raise OSError(msg)

        lcd.loadFont = explode
        panel = ChatPanel(lcd=lcd, vlw_path=str(self.vlw))
        panel.say("claude", "テスト")
        self.assertEqual(panel.info()["font"], "EFontJA24")
        self.assertFalse(panel.info()["vlw"])

    def test_the_vlw_is_used_when_it_is_the_only_face(self) -> None:
        panel = ChatPanel(lcd=FakeLcd(fonts=()), vlw_path=str(self.vlw))
        panel.say("claude", "hi")
        self.assertEqual(panel.info()["font"], chat_font.VLW_NAME)


if __name__ == "__main__":
    unittest.main()
