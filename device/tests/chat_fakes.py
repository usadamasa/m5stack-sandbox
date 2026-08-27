"""チャットパネルのテストが使う LCD の fake と、その fake の幾何。

`test_chat.py` / `test_chat_font.py` / `test_chat_wrap.py` が使う
(`test_chat_log.py` は LCD が要らないので使わない)。

`device/buddy/chat.py` は MicroPython で走るが、面白いところ — どこで行が
折り返るか、どの行が切り落とされるか、どの書体を選ぶか — は注入された LCD
の上の素の Python。LCD を注入できるようにしてあるのはそのためで、折り返しの
バグが「実機で見ると変」としてしか出ないのでは off-by-one を捕まえるのに
時間がかかりすぎる。

この fake はわざと粗い尺度を使う — 倍率を掛ける前でラテン文字 1 字 6 px、
wide な字 12 px — ので、期待する行の中身はコードを走らせて出てきたものを
追認するのではなく手で出せる。実機の尺度では **ない**。本物の数値は
`device/buddy/chat_font.py` の docstring にあり、
`host/tools/src/probe_device.py` が測る。

fake が `setTextSize` と `loadFont` を実装しているのは、パネルのキャッシュ
破棄がその 2 つで起きるため。測った幅を落とさない書体切り替えは、少しだけ
おかしい折り返しとしてしか現れない — 実機の目視をそのまま通り抜ける類の
バグ。
"""

from buddy import chat_font
from buddy.chat import ChatPanel

# `buddy.chat` / `buddy.chat_font` の幾何の定数を写したもの。テスト側の
# 算術を魔法ではなく見えるものにするため。
#
# `_RAW` の高さは fake が 1:1 で報告する値。パネルがそれを倍率で縮め、
# 派生する値は本番の定数に従う。28/16 という半端な数にしてあるのは、fake の
# 行数を実機と同じ — 日本語 6 行、ラテン文字 9 行 — に保つため。そうして
# はじめて、切り落としのテストが何かを意味する。
X0 = 4
WIDE_H_RAW = 28
NARROW_H_RAW = 16
WIDE_PX_RAW = 12
NARROW_PX_RAW = 6

_WIDE_H = int(WIDE_H_RAW * chat_font.WIDE_SCALE)
_NARROW_H = int(NARROW_H_RAW * chat_font.NARROW_SCALE)
ROWS = 110 // _WIDE_H
NARROW_ROWS = 110 // _NARROW_H

# 折り返しのテストは書体を挟まずに直接呼ぶので、fake はそこでも 1:1 の
# まま。以下は倍率を掛けていない数値。
INDENT_PX = 2 * NARROW_PX_RAW  # 1:1 での textWidth("> ")
BODY_PX = 240 - X0 - 4 - INDENT_PX  # 220


class _FakeFonts:
    """`M5.Lcd.FONTS`。どの名前が載っているかはビルドが決めるので、実機側も
    ここも属性は動的に生える。"""

    def __init__(self, names: tuple[str, ...]) -> None:
        for name in names:
            setattr(self, name, f"<font {name}>")

    def __getattr__(self, name: str) -> object:
        # 既定の探索が空振りしたときだけ呼ばれるので、振る舞いは足していない。
        # 書いてあるのは「名前は静的には決まらない」という宣言の方で、
        # パネルが `getattr(fonts, name, None)` で探るのはそのため。
        raise AttributeError(name)


class FakeLcd:
    """パネルが描き込むぶんの M5GFX の面。

    フォントの高さは選ばれている書体で変わる — 実機と同じで、日本語の書体
    が背の高い方であり、それを選ぶことがパネルの行数を削る。`setTextSize`
    は高さと 1 文字の送りの両方を、driver がするように切り捨てで縮める。
    """

    def __init__(
        self,
        fonts: tuple[str, ...] = ("EFontJA24", "DejaVu12", "DejaVu9"),
        omit: tuple[str, ...] = (),
    ) -> None:
        # `omit` はその呼び出しを持たない古いビルドの代役。インスタンスの
        # 属性を None にするとメソッドが隠れるので、パネルの
        # `getattr(lcd, name, None)` の探りは空で返る — そういう基板から
        # 見えるのと同じ状態になる。
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

    # -- driver の面。M5GFX がそうなので camelCase のまま。

    # 書体のハンドルが `object` なのは driver がそうだから。内蔵書体では
    # `FONTS` から引いた不透明な値が、VLW ではパスの `str` が来る。この
    # fake はどちらも `str` で表すが、受け口は本物と同じ広さにしておく。
    def setFont(self, font: object) -> None:
        # 内蔵書体を選ぶと読み込み済みの VLW が外れる。実機と同じで、
        # パネルが読み直さなければならない理由でもある。
        self.loaded = None
        self.font = str(font)
        self.font_calls.append(self.font)

    def loadFont(self, font: object) -> None:
        self.loaded = str(font)
        self.load_calls.append(self.loaded)

    def unloadFont(self) -> None:
        self.loaded = None
        self.unload_calls += 1

    def setTextSize(self, scale: float) -> None:
        self.text_size = scale

    def fontHeight(self) -> int:
        if self.loaded is not None:
            return int(WIDE_H_RAW * self.text_size)
        raw = WIDE_H_RAW if self.font == "<font EFontJA24>" else NARROW_H_RAW
        return int(raw * self.text_size)

    def textWidth(self, text: str) -> int:
        raw = sum(WIDE_PX_RAW if ord(ch) >= 0x1100 else NARROW_PX_RAW for ch in text)
        return int(raw * self.text_size)

    def fillRect(self, x: int, y: int, w: int, h: int, color: int) -> None:
        self.rects.append((x, y, w, h, color))

    def setTextColor(self, fg: int, _bg: int) -> None:
        self._color = fg

    def drawString(self, text: str, x: int, y: int) -> None:
        self.drawn.append((text, x, y, self._color))

    # -- テスト用の補助

    def rows(self) -> list[str]:
        """描かれた順に、各行の本文。

        本文は役割の接頭辞のぶん字下げして描かれ、接頭辞は `X0` にそろう。
        正確な x ではなくそこで分けているのは、字下げがどの倍率で測られた
        かをこちらが追わなくて済むようにするため。
        """
        return [text for text, x, _y, _c in self.drawn if x > X0]

    def prefixes(self) -> list[str]:
        return [text for text, x, _y, _c in self.drawn if x == X0]


def panel_without_vlw(lcd: FakeLcd) -> ChatPanel:
    """内蔵書体だけのパネル。

    既定の経路が stat に失敗するのに任せるのではなく、明示的に降りる。
    既定はデバイス上のパスで、それが存在しないことに依存するテストは、
    誰かが実機の上でスイートを走らせた日に間違った理由で通る。
    """
    return ChatPanel(lcd=lcd, vlw_path="")
