"""VLW ライター。読み手は M5GFX なので、検証はその読み方でやる。

`device/buddy/chat.py` の他の部分と違って、ここは失敗しても例外が出ない。
`loadFont` は壊れたファイルを黙って受け取り、前の書体を選んだままにする
だけなので、バイト列が仕様どおりであることをホスト側で確かめられないと、
不具合は「なぜか日本語が 24px のまま」という形でしか現れない。

そのため以下のテストは生成物を struct で読み戻す。M5GFX が読む順序
(ヘッダ 24 バイト、グリフ表 28 バイト x N、それからビットマップ) と
同じ手順を踏むことで、レイアウトのずれをそのまま検出する。

実フォントを使うのは 1 か所だけ (`RasterizeTest`)。ラスタライズは Pillow の
仕事で、ここで見たいのは「Pillow の出力を VLW の座標系に移し替える計算」の
ほう。だから残りは手で組んだ `Glyph` を通す。
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from make_vlw import (
    Glyph,
    build_vlw,
    default_charset,
    jis_level1_kanji,
    rasterize,
    summarize,
)

_HEADER_BYTES = 24
_GLYPH_BYTES = 28


def _glyph(codepoint: int, width: int = 2, height: int = 3, fill: int = 0xFF) -> Glyph:
    return Glyph(
        codepoint=codepoint,
        width=width,
        height=height,
        x_advance=width + 1,
        dx=1,
        dy=height,
        bitmap=bytes([fill]) * (width * height),
    )


def _read_header(blob: bytes) -> tuple[int, ...]:
    return struct.unpack(">6I", blob[:_HEADER_BYTES])


def _read_table(blob: bytes, count: int) -> list[tuple[int, ...]]:
    return [
        struct.unpack(">7I", blob[_HEADER_BYTES + i * _GLYPH_BYTES :][:_GLYPH_BYTES])
        for i in range(count)
    ]


class HeaderTest(unittest.TestCase):
    def test_header_reports_the_glyph_count_and_size(self) -> None:
        blob = build_vlw([_glyph(ord("a")), _glyph(ord("b"))], 16)
        count, version, y_advance, unused, ascent, descent = _read_header(blob)
        self.assertEqual(count, 2)
        self.assertEqual(version, 11)
        self.assertEqual(y_advance, 16)
        self.assertEqual(unused, 0)
        # 0 なのは意図。行の高さをグリフの外接だけで決めさせるため —
        # 理由は make_vlw のモジュール docstring にある。
        self.assertEqual((ascent, descent), (0, 0))

    def test_an_empty_font_is_refused(self) -> None:
        # 空の VLW を書くと loadFont は黙って受け取り、何も描かなくなる。
        # 書き出す前に落ちたほうが原因が分かる。
        with self.assertRaises(ValueError):
            build_vlw([], 16)


class TableTest(unittest.TestCase):
    def test_fields_are_in_the_order_m5gfx_reads_them(self) -> None:
        glyph = Glyph(
            codepoint=0x3042,
            width=13,
            height=14,
            x_advance=15,
            dx=2,
            dy=12,
            bitmap=bytes(13 * 14),
        )
        (row,) = _read_table(build_vlw([glyph], 16), 1)
        codepoint, height, width, x_advance, dy, dx, unused = row
        self.assertEqual(codepoint, 0x3042)
        # 高さと幅がこの順なのが VLW の並び。取り違えると、デバイス側では
        # 正方形でない字だけが崩れる。
        self.assertEqual(height, 14)
        self.assertEqual(width, 13)
        self.assertEqual(x_advance, 15)
        self.assertEqual(dy, 12)
        self.assertEqual(dx, 2)
        self.assertEqual(unused, 0)

    def test_negative_deltas_survive_the_round_trip(self) -> None:
        # dX は int8、dY は int16 として読み戻される。符号付きの値を
        # そのまま uint32 に詰めると、デバイス側で桁が化ける。
        glyph = _glyph(ord("j"))._replace(dx=-3, dy=-2)
        (row,) = _read_table(build_vlw([glyph], 16), 1)
        self.assertEqual(struct.unpack(">h", struct.pack(">I", row[4])[2:])[0], -2)
        self.assertEqual(struct.unpack(">b", struct.pack(">I", row[5])[3:])[0], -3)

    def test_glyphs_are_written_in_codepoint_order(self) -> None:
        # M5GFX は lower_bound で引く。順序が崩れると、引けない文字が
        # 空白になって出る。
        blob = build_vlw([_glyph(cp) for cp in (0x3042, 0x0041, 0x9FA0)], 16)
        table = _read_table(blob, 3)
        self.assertEqual([row[0] for row in table], [0x0041, 0x3042, 0x9FA0])

    def test_bitmaps_are_reordered_with_the_table(self) -> None:
        # 並べ替えが表だけに効いてビットマップに効かないと、全部の字が
        # 隣の字の絵で出る。表と同じ順であることまで見る。
        high = _glyph(0x3042, width=1, height=1, fill=0xAA)
        low = _glyph(0x0041, width=1, height=1, fill=0xBB)
        blob = build_vlw([high, low], 16)
        start = _HEADER_BYTES + 2 * _GLYPH_BYTES
        self.assertEqual(blob[start : start + 2], b"\xbb\xaa")

    def test_duplicate_codepoints_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_vlw([_glyph(ord("a")), _glyph(ord("a"))], 16)


class BitmapTest(unittest.TestCase):
    def test_bitmaps_follow_the_table_in_the_same_order(self) -> None:
        first = _glyph(ord("a"), width=2, height=2, fill=0x11)
        second = _glyph(ord("b"), width=3, height=1, fill=0x22)
        blob = build_vlw([first, second], 16)
        start = _HEADER_BYTES + 2 * _GLYPH_BYTES
        self.assertEqual(blob[start : start + 4], b"\x11" * 4)
        self.assertEqual(blob[start + 4 : start + 7], b"\x22" * 3)
        self.assertEqual(len(blob), start + 7)

    def test_one_byte_per_pixel(self) -> None:
        glyph = _glyph(ord("a"), width=4, height=5)
        blob = build_vlw([glyph], 16)
        self.assertEqual(len(blob) - _HEADER_BYTES - _GLYPH_BYTES, 4 * 5)


class CharsetTest(unittest.TestCase):
    def test_level_one_is_the_expected_size(self) -> None:
        # JIS X 0208 第 1 水準は 2965 字。区点から導いているので、
        # 数が合わなければ導出のほうが壊れている。
        kanji = jis_level1_kanji()
        self.assertEqual(len(kanji), 2965)
        self.assertEqual(len(set(kanji)), 2965)

    def test_level_one_holds_common_kanji_and_stops_at_level_two(self) -> None:
        kanji = set(jis_level1_kanji())
        for ch in "日本語調査中漢字":
            self.assertIn(ch, kanji)
        # 上の境界。48 区の頭は第 2 水準の 1 文字目なので、これが混ざって
        # いれば範囲を 1 区ぶん取りすぎている。文字を名指しにしないのは、
        # 第 1 水準が読み順で並んでいて直感が当てにならないため — 「鰯」は
        # 画数から想像するのと違って第 1 水準に入っている。
        self.assertNotIn(bytes((0xA0 + 48, 0xA0 + 1)).decode("euc_jp"), kanji)
        self.assertIn(bytes((0xA0 + 47, 0xA0 + 1)).decode("euc_jp"), kanji)

    def test_the_default_charset_covers_what_the_panel_shows(self) -> None:
        charset = set(default_charset())
        for ch in "aZ0 あアー漢。、々…→":
            self.assertIn(ch, charset)

    def test_the_default_charset_stays_in_the_bmp(self) -> None:
        # M5GFX はコードポイントを uint16 に切り詰める。BMP の外の文字を
        # 載せると、別の文字として引かれる。
        self.assertLessEqual(max(ord(ch) for ch in default_charset()), 0xFFFF)


class SummaryTest(unittest.TestCase):
    def test_line_height_matches_what_m5gfx_derives(self) -> None:
        # M5GFX は maxAscent + maxDescent を行送りにする。要約がそれと
        # 違う数を出すと、パネルの行数見積りが狂う。
        tall = _glyph(ord("A"), width=2, height=10)._replace(dy=10)
        deep = _glyph(ord("g"), width=2, height=10)._replace(dy=7)
        blob = build_vlw([tall, deep], 16)
        summary = summarize([tall, deep], blob)
        self.assertEqual(summary["line_height"], 10 + 3)
        self.assertEqual(summary["glyphs"], 2)
        self.assertEqual(summary["bytes"], len(blob))


class RasterizeTest(unittest.TestCase):
    """Pillow の出力を VLW の座標系に移すところ。実フォントが要る。"""

    # リポジトリ相対。フォントは再取得できるのでコミットしていない。
    FONT = Path(__file__).resolve().parents[3] / "tmp/fonts/BIZUDGothic-Regular.ttf"

    def setUp(self) -> None:
        if not self.FONT.exists():
            self.skipTest(f"{self.FONT} が無い。README のフォント取得手順を見よ")

    def test_a_kanji_lands_inside_the_em_box(self) -> None:
        (glyph,) = rasterize(self.FONT, 16, "漢")
        self.assertEqual(glyph.codepoint, ord("漢"))
        # 16px の全角。外接が em を超えていたら dY の基準を取り違えている。
        self.assertLessEqual(glyph.height, 17)
        self.assertLessEqual(glyph.width, 17)
        self.assertEqual(len(glyph.bitmap), glyph.width * glyph.height)
        self.assertGreater(glyph.dy, 0)
        self.assertEqual(glyph.x_advance, 16)

    def test_characters_the_font_lacks_are_dropped(self) -> None:
        # 私用領域。.notdef と同じ絵になるので載らない。
        self.assertEqual(rasterize(self.FONT, 16, "\ue000"), [])


if __name__ == "__main__":
    unittest.main()
