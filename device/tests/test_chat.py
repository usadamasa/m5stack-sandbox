# pyright: reportPrivateUsage=false
"""Chat panel: wrapping, layout and command dispatch, plus host framing.

`device/buddy/chat.py` runs on MicroPython, but everything interesting
about it — where a line breaks, which rows survive the clip, which font
it picked — is plain Python over an injected LCD. That is the whole
reason the LCD is injectable: a wrapping bug otherwise only shows up as
"the text looks wrong on the bench", which is a slow way to find an
off-by-one.

The fake panel below uses a deliberately crude metric — 6 px per Latin
character, 12 px per wide one, before scaling — so the expected row
contents can be worked out by hand instead of by running the code and
blessing whatever came out. It is *not* the real hardware's metric; the
real numbers are in `device/buddy/chat.py`, measured by
`host/tools/src/probe_device.py`.

The fake does honour `setTextSize` and `loadFont`, because those are
what the panel's cache invalidation turns on: a face change that does
not drop the measured widths is a bug that only shows as slightly wrong
wrapping, which is exactly the kind that survives a bench check.

This is also a whitebox test of `ChatPanel`'s private wrapping
internals, hence the file-level `reportPrivateUsage=false` above.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import buddy_bridge
from buddy import chat as buddy_chat
from buddy.chat import ChatPanel
from buddy_bridge import (
    MAX_SAY_CHARS,
    MAX_SAY_CHARS_WIDE,
    Message,
    normalize_for_device,
    say,
    split_for_device,
)

# Mirrors the geometry constants in buddy.chat so the arithmetic in the
# assertions below is visible rather than magic.
#
# The `_RAW` heights are what the fake reports at 1:1; the panel scales
# them, and the derived values follow whatever the production constants
# say. Picking 28/16 rather than round numbers keeps the fake's row
# counts the same as the board's — six rows of Japanese, nine of Latin —
# so a clipping test that passes here means something.
_X0 = 4
_WIDE_H_RAW = 28
_NARROW_H_RAW = 16
_WIDE_PX_RAW = 12
_NARROW_PX_RAW = 6

_WIDE_H = int(_WIDE_H_RAW * buddy_chat._WIDE_SCALE)
_NARROW_H = int(_NARROW_H_RAW * buddy_chat._NARROW_SCALE)
_ROWS = 110 // _WIDE_H
_NARROW_ROWS = 110 // _NARROW_H

# WrapTest drives `_wrap` directly, outside a `_push_font` bracket, so
# the fake is still at 1:1 there and these are the unscaled numbers.
_INDENT_PX = 2 * _NARROW_PX_RAW  # textWidth("> ") at 1:1
_BODY_PX = 240 - _X0 - 4 - _INDENT_PX  # 220


class _FakeFonts:
    def __init__(self, names: tuple[str, ...]) -> None:
        for name in names:
            setattr(self, name, f"<font {name}>")


class FakeLcd:
    """Enough of the M5GFX surface for the panel to draw into.

    Font height depends on which face is selected, as it does on the
    device: the Japanese faces are the tall ones, and picking one is what
    costs the panel its rows. `setTextSize` scales both the height and
    the per-character advance, truncating as the driver does.
    """

    def __init__(
        self,
        fonts: tuple[str, ...] = ("EFontJA24", "DejaVu12", "DejaVu9"),
        omit: tuple[str, ...] = (),
    ) -> None:
        # `omit` stands in for an older build that never had the call.
        # Setting the name to None on the instance shadows the method, so
        # the panel's `getattr(lcd, name, None)` probe comes back empty —
        # which is exactly what it would see on such a board.
        self.__dict__.update(dict.fromkeys(omit))
        self.FONTS = _FakeFonts(fonts)
        self._color = 0
        self.font: str | None = None
        self.font_calls: list[str] = []
        self.text_size: float = 1.0
        self.loaded: str | None = None
        self.load_calls: list[str] = []
        self.unload_calls = 0
        self.rects: list[tuple[int, int, int, int, int]] = []
        self.drawn: list[tuple[str, int, int, int]] = []

    # -- driver surface. camelCase because M5GFX is.

    def setFont(self, font: str) -> None:
        # Selecting a built-in face drops any loaded VLW, as it does on
        # the device — which is the reason the panel has to reload.
        self.loaded = None
        self.font = font
        self.font_calls.append(font)

    def loadFont(self, font: str) -> None:
        self.loaded = font
        self.load_calls.append(font)

    def unloadFont(self) -> None:
        self.loaded = None
        self.unload_calls += 1

    def setTextSize(self, scale: float) -> None:
        self.text_size = scale

    def fontHeight(self) -> int:
        if self.loaded is not None:
            return int(_WIDE_H_RAW * self.text_size)
        raw = _WIDE_H_RAW if self.font == "<font EFontJA24>" else _NARROW_H_RAW
        return int(raw * self.text_size)

    def textWidth(self, text: str) -> int:
        raw = sum(_WIDE_PX_RAW if ord(ch) >= 0x1100 else _NARROW_PX_RAW for ch in text)
        return int(raw * self.text_size)

    def fillRect(self, x: int, y: int, w: int, h: int, color: int) -> None:
        self.rects.append((x, y, w, h, color))

    def setTextColor(self, fg: int, _bg: int) -> None:
        self._color = fg

    def drawString(self, text: str, x: int, y: int) -> None:
        self.drawn.append((text, x, y, self._color))

    # -- test helpers

    def rows(self) -> list[str]:
        """Body text of each painted row, in paint order.

        Bodies are drawn indented past the role prefix, prefixes flush at
        `_X0`. Keyed on that rather than on an exact x so the split does
        not have to track whatever scale the indent was measured at.
        """
        return [text for text, x, _y, _c in self.drawn if x > _X0]

    def prefixes(self) -> list[str]:
        return [text for text, x, _y, _c in self.drawn if x == _X0]


def panel_without_vlw(lcd: FakeLcd) -> ChatPanel:
    """A panel on the built-in faces.

    Opting out explicitly rather than letting the default path fail to
    stat: the default is a device path, and a test that depends on it not
    existing on the host would pass for the wrong reason the day someone
    runs the suite on a board.
    """
    return ChatPanel(lcd=lcd, vlw_path="")


class WrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lcd = FakeLcd()
        self.panel = panel_without_vlw(self.lcd)

    def wrap(self, text: str) -> list[str]:
        return self.panel._wrap(text, _BODY_PX)

    def test_latin_breaks_on_spaces(self) -> None:
        # 220 px / 6 px = 36 characters per row.
        text = "the quick brown fox jumps over the lazy dog again and again"
        rows = self.wrap(text)
        for row in rows:
            self.assertLessEqual(self.lcd.textWidth(row), _BODY_PX)
        # No word is cut in half, and nothing is lost.
        self.assertEqual(" ".join(rows).split(), text.split())

    def test_japanese_breaks_without_spaces(self) -> None:
        # 220 px / 12 px = 18 wide characters per row. A space-only
        # wrapper would emit this as one 30-character row and clip it.
        text = "テストが三件落ちとる。TestFoo が nil を返しとるのが原因だに。"
        rows = self.wrap(text)
        self.assertGreater(len(rows), 1)
        for row in rows:
            self.assertLessEqual(self.lcd.textWidth(row), _BODY_PX)
        # Spaces at a break get eaten; nothing else may be.
        self.assertEqual("".join(rows).replace(" ", ""), text.replace(" ", ""))

    def test_unbreakable_run_is_hard_cut(self) -> None:
        text = "x" * 100
        rows = self.wrap(text)
        self.assertEqual(rows, ["x" * 36, "x" * 36, "x" * 28])

    def test_explicit_newlines_start_new_rows(self) -> None:
        self.assertEqual(self.wrap("one\ntwo"), ["one", "two"])

    def test_rows_never_start_on_a_space(self) -> None:
        rows = self.wrap("word " * 20)
        self.assertFalse([row for row in rows if row.startswith(" ")])


class LayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lcd = FakeLcd()
        self.panel = panel_without_vlw(self.lcd)

    def test_prefix_only_on_the_first_row_of_a_message(self) -> None:
        # ASCII, so the narrow face at _NARROW_SCALE: 224 px of body
        # against a 4 px advance is 56 characters to a row.
        self.panel.say("claude", "x" * 100)
        self.panel.render()
        self.assertEqual(self.lcd.prefixes(), ["> "])
        self.assertEqual(len(self.lcd.rows()), 2)

    def test_roles_get_distinct_prefixes(self) -> None:
        self.panel.say("claude", "hi")
        self.panel.say("user", "yo")
        self.panel.render()
        self.assertEqual(self.lcd.prefixes(), ["> ", "< "])

    def test_oldest_rows_are_dropped_when_the_panel_overflows(self) -> None:
        # Japanese, so the tall face is selected and the panel holds
        # only _ROWS rows.
        count = _ROWS + 3
        self.assertLessEqual(count, buddy_chat._MAX_MESSAGES, "overflow test outgrew the buffer")
        for i in range(count):
            self.panel.say("claude", f"行{i}")
        self.panel.render()
        self.assertEqual(self.lcd.rows(), [f"行{i}" for i in range(count - _ROWS, count)])

    def test_panel_is_cleared_before_each_repaint(self) -> None:
        self.panel.say("claude", "hi")
        self.panel.render()
        self.assertEqual(self.lcd.rects, [(0, 0, 240, 110, 0x000000)])

    def test_transcript_is_bounded(self) -> None:
        for i in range(buddy_chat._MAX_MESSAGES + 5):
            self.panel.say("claude", str(i))
        self.assertEqual(len(self.panel._messages), buddy_chat._MAX_MESSAGES)


class FontTest(unittest.TestCase):
    def test_japanese_pulls_the_panel_onto_the_cjk_face(self) -> None:
        panel = panel_without_vlw(FakeLcd())
        # Nothing wide yet: stay on the short face and keep the rows.
        self.assertEqual(panel.info()["font"], "DejaVu12")
        self.assertEqual(panel.info()["rows"], _NARROW_ROWS)

        panel.say("claude", "テスト")
        self.assertEqual(panel.info()["font"], "EFontJA24")
        self.assertEqual(panel.info()["rows"], _ROWS)

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
        self.assertEqual(lcd.text_size, buddy_chat._BASE_SCALE)

    def test_the_scale_is_applied_while_drawing(self) -> None:
        # Not just restored afterwards: if the bracket never applied it,
        # every measurement would be taken at 1:1 and the panel would
        # wrap for a font size it is not drawing at.
        lcd = FakeLcd()
        panel = panel_without_vlw(lcd)
        panel.say("claude", "テスト")
        self.assertEqual(panel.info()["scale"], buddy_chat._WIDE_SCALE)
        self.assertEqual(panel.info()["rows"], _ROWS)

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
        self.assertEqual(panel.info()["rows"], 110 // _WIDE_H_RAW)


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
        self.assertEqual(info["font"], buddy_chat._VLW_NAME)
        self.assertTrue(info["vlw"])
        self.assertEqual(info["scale"], buddy_chat._VLW_SCALE)
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

        lcd.loadFont = explode  # pyright: ignore[reportAttributeAccessIssue]
        panel = ChatPanel(lcd=lcd, vlw_path=str(self.vlw))
        panel.say("claude", "テスト")
        self.assertEqual(panel.info()["font"], "EFontJA24")
        self.assertFalse(panel.info()["vlw"])

    def test_the_vlw_is_used_when_it_is_the_only_face(self) -> None:
        panel = ChatPanel(lcd=FakeLcd(fonts=()), vlw_path=str(self.vlw))
        panel.say("claude", "hi")
        self.assertEqual(panel.info()["font"], buddy_chat._VLW_NAME)


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = panel_without_vlw(FakeLcd())

    def test_say_acks_and_activates(self) -> None:
        ack = self.panel.handle_raw(b'{"cmd":"chat.say","text":"hi","id":"say-0"}')
        assert ack is not None
        self.assertEqual(ack["ack"], "chat.say")
        self.assertTrue(ack["ok"])
        self.assertTrue(ack["active"])
        self.assertEqual(ack["id"], "say-0")
        self.assertEqual(ack["wrapped"], 1)

    def test_clear_releases_the_panel(self) -> None:
        self.panel.handle({"cmd": "chat.say", "text": "hi"})
        ack = self.panel.handle({"cmd": "chat.clear"})
        assert ack is not None
        self.assertFalse(ack["active"])
        self.assertEqual(self.panel._messages, [])

    def test_info_does_not_activate(self) -> None:
        ack = self.panel.handle({"cmd": "chat.info"})
        assert ack is not None
        self.assertFalse(ack["active"])
        self.assertIn("rows", ack)

    def test_other_commands_fall_through(self) -> None:
        # buddy_protocol still has to see everything that is not ours,
        # or status/name/owner stop working the moment chat is wired in.
        self.assertIsNone(self.panel.handle({"cmd": "status"}))
        self.assertIsNone(self.panel.handle_raw(b'{"cmd":"status"}'))

    def test_malformed_input_is_not_ours_either(self) -> None:
        self.assertIsNone(self.panel.handle_raw(b"not json"))
        self.assertIsNone(self.panel.handle_raw(b"[1,2,3]"))


class NormalizeTest(unittest.TestCase):
    def test_collapses_whitespace_and_drops_blank_lines(self) -> None:
        self.assertEqual(
            normalize_for_device("a   b\r\n\n\n  c  \n"),
            "a b\nc",
        )

    def test_empty_text_stays_empty(self) -> None:
        self.assertEqual(normalize_for_device("   \n\n"), "")

    def test_drops_styling_that_the_panel_cannot_render(self) -> None:
        self.assertEqual(
            normalize_for_device("## 見出し\n**太字**と`code`と*強調*"),
            "見出し\n太字とcodeと強調",
        )

    def test_keeps_list_structure_but_compacts_the_bullet(self) -> None:
        self.assertEqual(
            normalize_for_device("* one\n+ two\n1. three\n2) four"),
            "- one\n- two\n- three\n- four",
        )

    def test_keeps_link_text_and_drops_the_url(self) -> None:
        self.assertEqual(
            normalize_for_device("[README](https://example.com/very/long/path) を見て"),
            "README を見て",
        )

    def test_underscores_inside_identifiers_survive(self) -> None:
        # The whole point of the boundary guards on the emphasis regex:
        # mangling buddy_chat to buddychat would be worse than leaving
        # the markup in.
        self.assertEqual(
            normalize_for_device("_buddy_chat.py_ を直した"),
            "buddy_chat.py を直した",
        )
        self.assertEqual(
            normalize_for_device("__buddy_chat.py__ を直した"),
            "buddy_chat.py を直した",
        )
        # No delimiter at a word boundary: leave it entirely alone.
        self.assertEqual(
            normalize_for_device("host/buddy_bridge.py と device/buddy_chat.py"),
            "host/buddy_bridge.py と device/buddy_chat.py",
        )

    def test_code_blocks_collapse_to_a_marker(self) -> None:
        self.assertEqual(
            normalize_for_device("直した:\n```python\nx = 1\ny = 2\n```\nどう?"),
            "直した:\n[code]\nどう?",
        )

    def test_an_unclosed_fence_still_announces_itself(self) -> None:
        self.assertEqual(normalize_for_device("見て:\n```\nx = 1"), "見て:\n[code]")

    def test_blockquotes_and_rules_go_away(self) -> None:
        self.assertEqual(normalize_for_device("> quoted\n---\nplain"), "quoted\nplain")


class SplitTest(unittest.TestCase):
    def test_short_text_is_one_part(self) -> None:
        self.assertEqual(split_for_device("hello"), ["hello"])

    def test_paragraphs_are_packed_while_they_fit(self) -> None:
        self.assertEqual(split_for_device("a\nb\nc", limit=10), ["a\nb\nc"])

    def test_split_prefers_a_sentence_boundary(self) -> None:
        text = "一文目です。" + "あ" * 30
        parts = split_for_device(text, limit=20)
        self.assertEqual(parts[0], "一文目です。")

    def test_unbreakable_text_is_hard_cut_at_the_limit(self) -> None:
        parts = split_for_device("あ" * 45, limit=20)
        self.assertEqual(parts, ["あ" * 20, "あ" * 20, "あ" * 5])

    def test_every_part_respects_the_limit(self) -> None:
        text = normalize_for_device("これはテストです。" * 40)
        for part in split_for_device(text):
            self.assertLessEqual(len(part), MAX_SAY_CHARS_WIDE)

    def test_the_limit_follows_the_font_the_panel_will_pick(self) -> None:
        # One Japanese character anywhere costs the panel two rows and
        # eight characters per row, so the split has to shrink with it.
        self.assertEqual(len(split_for_device("a" * MAX_SAY_CHARS)), 1)
        self.assertGreater(len(split_for_device("あ" + "a" * MAX_SAY_CHARS)), 1)

    def test_a_zero_limit_is_rejected(self) -> None:
        # Silently returning [] here would look like a successful send
        # that put nothing on screen.
        with self.assertRaises(ValueError):
            split_for_device("x", limit=0)


class _RecordingLink:
    """A link that records requests and answers every one with an ack."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        self.sent.append(obj)
        return {"ack": expect, "ok": True, "timeout": timeout}


class SayTest(unittest.TestCase):
    def test_sends_one_frame_per_part_in_order(self) -> None:
        link = _RecordingLink()
        text = "あ" * (MAX_SAY_CHARS_WIDE * 3)
        acks = say(link, text, role="user", pace=0)
        self.assertEqual(len(acks), 3)
        self.assertEqual([m["id"] for m in link.sent], ["say-0", "say-1", "say-2"])
        self.assertEqual({m["cmd"] for m in link.sent}, {"chat.say"})
        self.assertEqual({m["role"] for m in link.sent}, {"user"})
        self.assertEqual("".join(m["text"] for m in link.sent), text)

    def test_normalizes_before_splitting(self) -> None:
        link = _RecordingLink()
        say(link, "  hello   world  ", pace=0)
        self.assertEqual(link.sent[0]["text"], "hello world")

    def test_paces_multi_part_sends(self) -> None:
        # The panel shows its last rows only, so parts sent flat out
        # scroll past unread. One gap per seam, none before the first.
        link = _RecordingLink()
        with mock.patch.object(buddy_bridge.time, "sleep") as nap:
            say(link, "あ" * (MAX_SAY_CHARS_WIDE * 3), pace=1.5)
        self.assertEqual(nap.call_args_list, [mock.call(1.5), mock.call(1.5)])


if __name__ == "__main__":
    unittest.main()
