# pyright: reportPrivateUsage=false
"""Chat panel: layout and command dispatch, plus host framing.

`device/buddy/chat.py` runs on MicroPython, but everything interesting
about it — which rows survive the clip, what an ack carries — is plain
Python over an injected LCD.

書体の選択と VLW は `test_chat_font.py`、行の折り返しは
`test_chat_wrap.py`。fake の LCD と、その尺度から出る幾何の定数は
`chat_fakes.py` にある。

This is also a whitebox test of `ChatPanel`'s private transcript
buffer, hence the file-level `reportPrivateUsage=false` above.
"""

import unittest
from unittest import mock

import buddy_verbs
from buddy import chat as buddy_chat
from buddy_text import (
    MAX_SAY_CHARS,
    MAX_SAY_CHARS_WIDE,
    normalize_for_device,
    split_for_device,
)
from buddy_verbs import say
from buddy_wire import Message
from chat_fakes import ROWS, FakeLcd, panel_without_vlw


class LayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lcd = FakeLcd()
        self.panel = panel_without_vlw(self.lcd)

    def test_prefix_only_on_the_first_row_of_a_message(self) -> None:
        # ASCII, so the narrow face at NARROW_SCALE: 224 px of body
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
        # only ROWS rows.
        count = ROWS + 3
        self.assertLessEqual(count, buddy_chat._MAX_MESSAGES, "overflow test outgrew the buffer")
        for i in range(count):
            self.panel.say("claude", f"行{i}")
        self.panel.render()
        self.assertEqual(self.lcd.rows(), [f"行{i}" for i in range(count - ROWS, count)])

    def test_panel_is_cleared_before_each_repaint(self) -> None:
        self.panel.say("claude", "hi")
        self.panel.render()
        self.assertEqual(self.lcd.rects, [(0, 0, 240, 110, 0x000000)])

    def test_transcript_is_bounded(self) -> None:
        for i in range(buddy_chat._MAX_MESSAGES + 5):
            self.panel.say("claude", str(i))
        self.assertEqual(len(self.panel._messages), buddy_chat._MAX_MESSAGES)


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
        with mock.patch.object(buddy_verbs.time, "sleep") as nap:
            say(link, "あ" * (MAX_SAY_CHARS_WIDE * 3), pace=1.5)
        self.assertEqual(nap.call_args_list, [mock.call(1.5), mock.call(1.5)])


if __name__ == "__main__":
    unittest.main()
