"""行の折り返し。

`device/buddy/chat_wrap.py` は幅を測る `ChatFont` を受け取るだけの純関数
なので、パネルも transcript も要らずに直接呼べる。書体を挟まずに呼ぶので
fake は 1:1 のままで、下の数値は倍率を掛けていないもの。fake の尺度と幾何は
`chat_fakes.py` にある。
"""

import unittest

from buddy import chat_font, chat_wrap
from chat_fakes import BODY_PX, FakeLcd


class WrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lcd = FakeLcd()
        self.font = chat_font.ChatFont(self.lcd, vlw_path="")

    def wrap(self, text: str) -> list[str]:
        return chat_wrap.wrap(text, BODY_PX, self.font)

    def test_latin_breaks_on_spaces(self) -> None:
        # 220 px / 6 px = 36 characters per row.
        text = "the quick brown fox jumps over the lazy dog again and again"
        rows = self.wrap(text)
        for row in rows:
            self.assertLessEqual(self.lcd.textWidth(row), BODY_PX)
        # No word is cut in half, and nothing is lost.
        self.assertEqual(" ".join(rows).split(), text.split())

    def test_japanese_breaks_without_spaces(self) -> None:
        # 220 px / 12 px = 18 wide characters per row. A space-only
        # wrapper would emit this as one 30-character row and clip it.
        text = "テストが三件落ちとる。TestFoo が nil を返しとるのが原因だに。"
        rows = self.wrap(text)
        self.assertGreater(len(rows), 1)
        for row in rows:
            self.assertLessEqual(self.lcd.textWidth(row), BODY_PX)
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


if __name__ == "__main__":
    unittest.main()
