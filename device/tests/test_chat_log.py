"""transcript の有界リストと、幅広のグリフを含むかの再計算。

`device/buddy/chat_log.py` は LCD にも driver にも触らない — 積んで、溢れたら
古い方から落として、`has_wide` を積み直すだけ。その 1 ビットがパネルの書体に
どう出るかは `test_chat_font.py`、実際に描かれる行は `test_chat.py`。
"""

import unittest

from buddy.chat_log import MAX_MESSAGES, Transcript, text_of


class BoundedTest(unittest.TestCase):
    def test_transcript_is_bounded(self) -> None:
        log = Transcript()
        for i in range(MAX_MESSAGES + 5):
            log.append("claude", str(i))
        self.assertEqual(len(log.messages), MAX_MESSAGES)

    def test_the_oldest_messages_are_the_ones_dropped(self) -> None:
        log = Transcript()
        for i in range(MAX_MESSAGES + 3):
            log.append("claude", str(i))
        self.assertEqual(text_of(log.messages[0]), "3")
        self.assertEqual(text_of(log.messages[-1]), str(MAX_MESSAGES + 2))

    def test_the_body_is_coerced_to_str_on_the_way_in(self) -> None:
        # 積む側が渡すのは wire から来た値そのもの。str でないものが
        # transcript に残ると、幅を測る側が後で落ちる。
        log = Transcript()
        self.assertEqual(log.append("claude", 42), "42")
        self.assertEqual(text_of(log.messages[0]), "42")

    def test_clear_empties_the_transcript(self) -> None:
        log = Transcript()
        log.append("claude", "テスト")
        log.clear()
        self.assertEqual(log.messages, [])
        self.assertFalse(log.has_wide)


class WideTest(unittest.TestCase):
    def test_a_wide_glyph_anywhere_marks_the_transcript(self) -> None:
        log = Transcript()
        log.append("claude", "ascii")
        self.assertFalse(log.has_wide)
        log.append("claude", "テスト")
        self.assertTrue(log.has_wide)

    def test_evicting_the_last_wide_message_clears_the_flag(self) -> None:
        # or-ed in on append rather than recomputed, this would stay True
        # for the rest of the session and cost the panel two rows for
        # nothing.
        log = Transcript()
        log.append("claude", "テスト")
        for i in range(MAX_MESSAGES):
            log.append("claude", f"ascii {i}")
        self.assertFalse(log.has_wide)


if __name__ == "__main__":
    unittest.main()
