"""チャットパネルが使う書体と、その書体で測った値。

`buddy/chat.py` から切り出した。driver へ書体を載せて外し、載っている間の
文字送りと行高を測る係。

### Fonts

Measured on the installed UIFlow 2.0 build. Re-measure after a firmware
change with `uv run python host/tools/src/probe_device.py`:

    font              fontHeight()   "あ"   "A"
    EFontJA24              27         23    17
    AlibabaSansJA24        27         23    17
    DejaVu12               16         --    12
    DejaVu9                15         --    10

24 px is the *only* size at which this build has Japanese glyphs, and at
1:1 it costs three quarters of the panel: four rows of nine characters.
Neither built-in face can do better, so the panel brings its own.

### The VLW face

`M5.Lcd.loadFont()` reads a VLW — the Processing smooth-font format that
M5GFX supports — off flash, and `host/tools/src/make_vlw.py` generates
one from a TTF. The installed file is BIZ UDGothic at 16 px, and it is
what the panel draws Japanese with:

    face                 fontHeight()   "あ"   "A"   panel
    VLW (16 px)               18         16     8    6 rows x 13 chars
    EFontJA24 @0.75           20         17    12    5 rows x 12 chars
    EFontJA24 @1.0            27         23    17    4 rows x  9 chars
    DejaVu12  @0.75           12         --     9    9 rows x 24 chars

Measured on the device: loading it takes 57 ms, costs 19.5 KB of heap
while it is loaded, and the file itself is 930 KB of the 1.6 MB that was
free on flash. The glyph bitmaps stay in the file — M5GFX keeps only the
per-glyph attribute arrays in memory and reads pixels as it draws — which
is what makes 3476 glyphs affordable on a board with 99 KB of heap.

Antialiased 16 px is legible in a way that a decimated 24 px bitmap is
not, which is the whole reason for the detour: `setTextSize` scales the
built-in faces by dropping pixel rows, and kanji fill in as it does.

### Choosing a face

Still chosen per repaint from the content. Japanese pulls the panel onto
the VLW; an all-ASCII transcript stays on DejaVu12, which at 0.75 packs
216 characters against the VLW's 162. A file path wrapped every twelve
characters is not readable, and that is what pinning a CJK face would do
to every ASCII message.

`WIDE_SCALE` and `NARROW_SCALE` exist for the built-in faces, which
have one size each. `setTextSize` takes a float and the driver reports
the scaled metrics back through `fontHeight()` and `textWidth()`, so
nothing below does the arithmetic itself — it just measures. The VLW is
already the right size and draws at 1:1.

A board with no VLW on it falls back to EFontJA24 at `WIDE_SCALE`. That
is the pre-VLW behaviour and it still reads; it just fits less. Run
`make_vlw.py --port ...` to install the font.

### Bracketing

`setFont`, `setTextSize` and the loaded VLW are all sticky on this
driver, so every entry point that measures or draws brackets itself with
`ChatFont.push` / `ChatFont.pop` and hands DejaVu9 at 1:1 back to
`BuddyUI`. Restoring the base font is what drops the VLW, so `push`
reloads it — 57 ms, paid per bracketed section rather than per glyph.

Widths are measured per character and cached — the proportional-font
warning in `buddy_ui_cp` applies doubly here — and the cache is dropped
whenever the selected face *or* its scale changes.
"""

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import Lcd  # noqa: F401

# 生成された日本語の書体。`host/tools/src/make_vlw.py --port ...` が置く
# もので、provisioning の一手であってデプロイの対象ではない — 930 KB あって
# 中身も変わらない。一度も入れたことのない基板には無いので、以下の経路は
# 全て VLW 無しでも動く必要がある。
VLW_PATH = "/flash/buddy-ja.vlw"

# `info()` が VLW をこう呼ぶ。パスではなく固定の名前にしてあるのは、ack を
# 読むホストが知りたいのは書体の「種類」だからで、パスの方は入れた本人が
# 既に知っている。
VLW_NAME = "vlw"

# 日本語のグリフを持つ内蔵書体。良い順。このビルドではどちらも 27 px で、
# EFontJA24 がビットマップの本命、AlibabaSansJA24 は将来のビルドが前者を
# 落としたときの控え。VLW が無いときにだけ届く。
_WIDE_FONTS = ("EFontJA24", "AlibabaSansJA24")

# transcript が ASCII だけのときに使う。DejaVu12 を 0.75 に縮めた方が VLW
# より詰まる — 9 行 24 文字に対して 6 行 27 文字。背の高い書体が要るのは
# 日本語であってラテン文字ではない。
_NARROW_FONTS = ("DejaVu12", "DejaVu9")

# BuddyUI が描くときに選ばれていると思っている書体。挟んだ区間から抜ける
# たびに戻す。
_BASE_FONT = "DejaVu9"

# 書体ごとの倍率。内蔵書体はサイズが 1 つきりなので縮めて使う。0.75 は
# driver が画素行を間引いた後でも漢字が判別できる境目。VLW は生成時が
# 最終サイズなので、縮めれば潰れるだけ。
WIDE_SCALE = 0.75
NARROW_SCALE = 0.75
VLW_SCALE = 1.0

# BuddyUI の chrome が描かれる倍率。`_BASE_FONT` と一緒に戻す。
BASE_SCALE = 1.0

# `_select_face` が、その書体をどの driver 呼び出しで載せるかを添える。
# VLW はパスで load し、内蔵書体はハンドルで select する。この 2 つは
# 交換できないので、`push` が手元の値の型から推測せずに済むようにする。
_KIND_VLW = "vlw"
_KIND_BUILTIN = "builtin"

# このコードポイント以上のグリフは、幅が広く — そして何より単独で立つので
# — 空白が無くても両隣で行を割ってよい。かな・漢字・全角形を覆う。
_WIDE_FROM = 0x1100

# 行は fontHeight() ちょうどで描き、行間は足さない。ここに並ぶ書体はどれも
# グリフの箱の中に余白を持っているし、110 px のパネルでは行間 1 つが丸ごと
# 1 行分の文字を潰す。
_LEADING = 0

# driver に fontHeight() が無いときに使う。UIFlow 2.0 では踏まないが、
# 代わりに待っているのは行数の計算での ZeroDivision。
_FALLBACK_LINE_H = 12


def is_wide(ch: str) -> bool:
    return ord(ch) >= _WIDE_FROM


class ChatFont:
    """driver に載っている書体と、その書体で測った値のキャッシュ。

    `setFont` も `setTextSize` も読み込んだ VLW も sticky なので、測る側も
    描く側も `push` / `pop` で挟み、DejaVu9 の 1:1 を `BuddyUI` へ返す。
    base font へ戻すと VLW は外れるため、`push` は毎回読み直す — 57 ms を
    グリフごとではなく区間ごとに払う。

    幅は 1 文字ずつ測ってキャッシュする (`buddy_ui_cp` のプロポーショナル
    フォントの注意書きがここでは二重に効く)。書体か倍率のどちらかが変われば
    キャッシュは丸ごと捨てる。
    """

    # `lcd` は `M5.Lcd` か、それを模したテストダブル。両者に共通の base は
    # 無いので、面は `.pyi` 側の Protocol で押さえる。注釈ではなく `# type:`
    # コメントに書くのは、ここの注釈が組み込みの名前 1 つでなければならない
    # ため (`device/tests/test_device_constraints.py`)。
    # `vlw_path` は設定であると同時に継ぎ目でもある: 実在するファイルを
    # 指せば実機なしで VLW の枝を通せるし、"" を渡せばフォールバックの枝に
    # 入る。None ではなく空文字なのも同じ制約から。
    def __init__(
        self,
        lcd,  # type: Lcd
        vlw_path: str = VLW_PATH,
    ) -> None:
        self._lcd = lcd

        # いま選ばれている書体の名前と倍率。`chat.info()` がそのまま ack へ
        # 載せる。
        self.name = None  # type: str | None
        self.scale = BASE_SCALE

        self._wide_name = None  # type: str | None
        self._wide_font = None  # type: object | None
        self._narrow_name = None  # type: str | None
        self._narrow_font = None  # type: object | None
        self._base_font = None  # type: object | None

        # チャット書体での 1 文字あたりの送り幅。`push` で挟んだ中で必要に
        # なったぶんだけ埋まる。CJK の transcript は同じ数百グリフを繰り返す
        # ので、すぐ収束する。
        self._char_w = {}  # type: dict[str, int]
        self._indent_w = None  # type: int | None
        self._line_h = None  # type: int | None

        # driver が `setTextSize` を断ったら立てる。再試行せずラッチするのは、
        # そうしないと再描画ごとに traceback が 1 本出るため。数値は計算では
        # なく driver から読み戻すので、どちらに転んでも辻褄は合う。
        self._can_scale = getattr(self._lcd, "setTextSize", None) is not None

        # この基板に VLW があり、このビルドが読めるなら、そのパス。
        self.vlw = self._resolve_vlw(vlw_path)

        # VLW がいま driver に載っているか。base font へ戻すと外れるので、
        # `push` が読み直す必要があるかはこれで分かる。
        self._vlw_loaded = False

        self._resolve_fonts()

    def has_cjk(self) -> bool:
        """この基板が日本語を描けるか。いま描いているかではない。"""
        return self.vlw is not None or self._wide_font is not None

    # ----- 書体を挟む

    def push(self, has_wide: bool) -> None:
        """transcript に合う書体を driver へ載せる。

        `has_wide` は transcript が wide なグリフを持っているか。
        """
        name, kind, handle, scale = self._select_face(has_wide)
        if name != self.name or scale != self.scale:
            # 下のキャッシュは全て前の書体で測った値。
            self.name = name
            self.scale = scale
            self._drop_metrics()
        if kind == _KIND_VLW:
            self._load_vlw(handle)
        elif handle is not None:
            self._unload_vlw()
            try:
                self._lcd.setFont(handle)
            except Exception as e:
                print("buddy.chat: setFont failed:", e)
        self._set_scale(scale)

    def pop(self) -> None:
        # 順番が効くのは、戻り先の書体が無いときでも倍率は戻さなければ
        # ならない、という一点だけ。BuddyUI の chrome はどの書体が選ばれて
        # いようと 1:1 で描かれる。
        self._unload_vlw()
        if self._base_font is not None:
            try:
                self._lcd.setFont(self._base_font)
            except Exception as e:
                print("buddy.chat: font restore failed:", e)
        self._set_scale(BASE_SCALE)

    # ----- 計測。`push` で挟んだ中でだけ呼ぶこと

    def advance(self, ch: str) -> int:
        w = self._char_w.get(ch)
        if w is None:
            w = self._lcd.textWidth(ch)
            self._char_w[ch] = w
        return w

    def measure(self, text: str) -> int:
        total = 0
        for ch in text:
            total += self.advance(ch)
        return total

    def indent_width(self, prefix: str) -> int:
        """役割の接頭辞の幅。呼び出しごとに同じ文字列が来る前提でキャッシュする。"""
        if self._indent_w is None:
            self._indent_w = self.measure(prefix)
        return self._indent_w

    def line_height(self) -> int:
        line_h = self._line_h
        if line_h is None:
            font_height = getattr(self._lcd, "fontHeight", None)
            height = int(font_height()) if font_height is not None else 0
            line_h = (height + _LEADING) if height else _FALLBACK_LINE_H
            self._line_h = line_h
        return line_h

    # ----- 書体の解決と読み込み

    def _resolve_vlw(self, path):
        # type: (str) -> str | None
        """VLW のパス。そこにファイルがあり、読める driver があるときだけ。

        `path` が空なら「探さない」で、呼び出し側が降りるための入口。

        `loadFont` は失敗を何も報せない — 存在しないパスも長さ 0 の blob も
        受け取って、前の書体を選んだまま帰る — ので、存在の確認はここで
        やるしかない。構築時に 1 度 stat する方が、再描画のたびに「書体が
        変わっていない」と気付くより安い。
        """
        if not path:
            return None
        if getattr(self._lcd, "loadFont", None) is None:
            return None
        try:
            import os

            os.stat(path)
        except (ImportError, OSError):
            print("buddy.chat: no VLW at", path, "- falling back to the built-in CJK face")
            return None
        return path

    def _resolve_fonts(self) -> None:
        fonts = getattr(self._lcd, "FONTS", None)
        if fonts is None:
            return
        self._base_font = getattr(fonts, _BASE_FONT, None)
        self._wide_name, self._wide_font = self._first_font(fonts, _WIDE_FONTS)
        self._narrow_name, self._narrow_font = self._first_font(fonts, _NARROW_FONTS)
        if self.vlw is None and self._wide_font is None:
            # 致命的ではない — ラテン文字は描ける。ただし日本語は空白の箱に
            # なるので、ack ごとに載る `cjk` が、誰も実機を覗かずにそれを
            # 知るための唯一の口になる。
            print("buddy.chat: no CJK font on this build; Japanese will not render")
        if self._narrow_font is None and self._wide_font is None:
            print("buddy.chat: no known font on this build, using the driver default")

    def _first_font(self, fonts, names):
        # type: (object, tuple[str, ...]) -> tuple[str, object] | tuple[None, None]
        for name in names:
            font = getattr(fonts, name, None)
            if font is not None:
                return name, font
        return None, None

    def _select_face(self, has_wide):
        # type: (bool) -> tuple[str | None, str, object | None, float]
        """この transcript が欲しがっている (名前, 種類, ハンドル, 倍率)。

        日本語は VLW があれば VLW、無ければ内蔵の CJK 書体。それ以外は
        ラテン文字用の狭い書体で、同じパネル高なら VLW より詰まる。
        """
        if has_wide:
            if self.vlw is not None:
                return VLW_NAME, _KIND_VLW, self.vlw, VLW_SCALE
            if self._wide_font is not None:
                return self._wide_name, _KIND_BUILTIN, self._wide_font, WIDE_SCALE
        if self._narrow_font is not None:
            return self._narrow_name, _KIND_BUILTIN, self._narrow_font, NARROW_SCALE
        if self.vlw is not None:
            return VLW_NAME, _KIND_VLW, self.vlw, VLW_SCALE
        if self._wide_font is not None:
            return self._wide_name, _KIND_BUILTIN, self._wide_font, WIDE_SCALE
        return None, _KIND_BUILTIN, None, BASE_SCALE

    def _drop_metrics(self) -> None:
        self._char_w = {}
        self._indent_w = None
        self._line_h = None

    def _load_vlw(self, path):
        # type: (object) -> None
        if self._vlw_loaded:
            return
        try:
            self._lcd.loadFont(path)
        except Exception as e:
            # ここに来たのは driver が呼び出しそのものを断ったということで、
            # `_resolve_vlw` が守っている「黙って失敗する」のとは別。次の
            # 再描画が内蔵書体を取るように VLW を落とす — 永久に再試行させ
            # ないため。
            print("buddy.chat: loadFont failed:", e)
            self.vlw = None
            self.name = None
            return
        self._vlw_loaded = True

    def _unload_vlw(self) -> None:
        if not self._vlw_loaded:
            return
        self._vlw_loaded = False
        unload = getattr(self._lcd, "unloadFont", None)
        if unload is None:
            return
        try:
            unload()
        except Exception as e:
            print("buddy.chat: unloadFont failed:", e)

    def _set_scale(self, scale: float) -> None:
        if not self._can_scale:
            return
        try:
            self._lcd.setTextSize(scale)
        except Exception as e:
            print("buddy.chat: setTextSize failed:", e)
            # キャッシュしてある値は全て、driver が一度も当てていない倍率で
            # 測ったもの。能力の方も数値の方も捨てる。
            self._can_scale = False
            self._drop_metrics()
