"""チャットパネル用の日本語フォントを VLW にして書き出す。

インストール済みの UIFlow ビルドが持つ日本語フォントは 24px ビットマップの
`EFontJA24` / `AlibabaSansJA24` だけで、`setTextSize` で縮めると最近傍で
間引かれて画数の多い漢字が潰れる。110px のパネルに読める日本語をもっと
入れるには、任意サイズのフォントを外から与えるしかない。

M5GFX はそのために VLW (Processing 由来のスムーズフォント形式) を読める。
`M5.Lcd.loadFont(font=...)` にパスを渡すとファイルを掴んだままにして、
グリフのビットマップは描くたびに読みに行く。メモリに常駐するのはグリフ
属性の配列だけなので、ヒープ 99KB の機体でも数千字を積める。

    uv run python host/tools/src/make_vlw.py --font BIZUDGothic-Regular.ttf \
        --size 16 --out tmp/buddy-ja-16.vlw

`--port` を渡すとそのままデバイスの `/flash/` へ転送する。デプロイ
(`buddy_deploy.py`) とは別の経路にしてあるのは、フォントが数百 KB あって
毎回の転送に載せる意味がないため。一度置けば firmware を焼き直すまで残る。

### VLW のバイナリレイアウト

M5GFX の `VLWfont::loadFont` (`src/lgfx/v1/lgfx_fonts.cpp`) が読む順に:

    ヘッダ 24 バイト = big-endian uint32 x 6
      [0] グリフ数
      [1] version。読まれるが使われない
      [2] yAdvance
      [3] 使われない
      [4] ascent   (絶対値が取られる)
      [5] descent  (同上)

    グリフ表 28 バイト x グリフ数 = big-endian uint32 x 7
      [0] コードポイント  [1] 高さ      [2] 幅    [3] xAdvance
      [4] dY (ベースラインからの上方向) [5] dX    [6] 未使用

    ビットマップ領域は 24 + グリフ数 * 28 から、表と同じ順に
    幅 * 高さ バイト。1 ピクセル 1 バイトのアルファ。

グリフ表はコードポイントの昇順でなければならない。M5GFX は `lower_bound`
で引くので、順序が崩れると引けない文字が出る。コードポイントは uint16 に
切り詰められるため BMP の外は載せられない。

### ヘッダの ascent / descent を 0 にしてある理由

M5GFX は `maxAscent` / `maxDescent` をヘッダの値から始めて、グリフごとの
dY と 高さ-dY で広げ、最後に `yAdvance = maxAscent + maxDescent` とする。
つまりヘッダに実フォントの ascent/descent を書くと、その分だけ行が高くなる。
16px の書体でも行送りが 20px を超え、110px のパネルで 1 行損をする。

0 を書けば行の高さは実際に置かれたグリフの外接だけで決まる。M5GFX は
どちらも `abs()` を通すので 0 は安全で、ASCII (0x20-0xA0) と 0xFF 超の
コードポイントはどちらも広げる側に参加する — 収録している文字はすべて
そのどちらかなので、`maxAscent` が 0 のままになることはない。
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast

from PIL import ImageFont

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# ヘッダとグリフ表のフィールド数。どちらも big-endian uint32 の並び。
_HEADER_FIELDS = 6
_GLYPH_FIELDS = 7
_HEADER_BYTES = _HEADER_FIELDS * 4
_GLYPH_BYTES = _GLYPH_FIELDS * 4

# M5GFX がコードポイントを uint16 に切り詰めるので、載せられるのは BMP だけ。
_MAX_CODEPOINT = 0xFFFF

# 未収録文字を判定するための踏み台。私用領域のここに実際のグリフを持つ
# 日本語フォントは無いので、これをラスタライズした結果が .notdef になる。
_NOTDEF_PROBE = "\ue000"


class _Mask(Protocol):
    """`getmask2` が返すラスタ。

    Pillow は自分で型を配っているのに、ここだけ Unknown で返ってくる。
    上流の注釈は `tuple[Image.core.ImagingCore, tuple[int, int]]` だが、
    その `Image.core` は `_imaging` の import が失敗したときに
    `DeferredError.new()` (戻り値 `Any`) を代入する try/except で定義されて
    いる。注釈の解決に使われるのは宣言型の方なので、`Any.ImagingCore` に
    なって型が落ちる。Pillow 側の問題で、こちらからは直せない。

    そこで依存している面だけを名前で押さえる。`mode="L"` で頼んだ結果なので
    中身は 1 ピクセル 1 バイト、行優先で幅 * 高さ ぶん並んでいる — VLW の
    ビットマップ領域がそのまま要求する形。
    """

    @property
    def size(self) -> tuple[int, int]: ...

    def __buffer__(self, flags: int, /) -> memoryview: ...


class _RasterFont(Protocol):
    """`getmask2` を上流の署名のまま、ラスタ側だけ `_Mask` にして持つ。

    `FreeTypeFont` をこれへ cast して呼ぶ。受けた後で `_Mask` へ寄せるのでも
    同じ型に着くが、それだと Unknown を受ける行が残り、そこに ignore が要る。
    呼ぶ前に面を絞れば ignore が要らず、型の出どころもここ 1 箇所になる。
    """

    def getmask2(self, text: str, mode: str = ...) -> tuple[_Mask, tuple[int, int]]: ...


class Glyph(NamedTuple):
    """VLW のグリフ 1 つ。`bitmap` は幅 * 高さ バイトのアルファ。"""

    codepoint: int
    width: int
    height: int
    x_advance: int
    dx: int
    dy: int
    bitmap: bytes


def jis_level1_kanji() -> str:
    """JIS X 0208 第 1 水準の漢字 2965 字。

    常用漢字を含み、日常の文には十分足りる。文字表を同梱せずに済ませて
    いるのは、EUC-JP の符号化がそのまま区点になっているため — 第 1 水準は
    16 区から 47 区で、区 k 点 t が 2 バイトの (0xA0+k, 0xA0+t) に対応する。
    第 2 水準 (48-84 区) を足すと 3390 字増えてフラッシュを食うので、
    ここでは採らない。
    """
    out: list[str] = []
    for ku in range(16, 48):
        for ten in range(1, 95):
            try:
                out.append(bytes((0xA0 + ku, 0xA0 + ten)).decode("euc_jp"))
            except UnicodeDecodeError:
                # 区の末尾は埋まっていない。空きは飛ばす。
                continue
    return "".join(out)


def default_charset() -> str:
    """パネルに出うる文字をひととおり。

    ASCII とかな、CJK の約物、全角形、そして第 1 水準漢字。矢印や三点
    リーダのように、パネルに流す文でよく出るのに上のどの範囲にも入らない
    ものだけを個別に足してある。
    """
    ranges: Sequence[tuple[int, int]] = (
        (0x0020, 0x007E),  # ASCII
        (0x3000, 0x303F),  # CJK の約物
        (0x3040, 0x309F),  # ひらがな
        (0x30A0, 0x30FF),  # カタカナ
        (0xFF01, 0xFF5E),  # 全角英数記号
        (0xFF61, 0xFF9F),  # 半角カナ
    )
    # 見た目が ASCII に紛らわしい文字が混ざるのは意図どおり。ここは
    # 「フォントに載せる文字」の列挙であって、コードが読む文字列ではない。
    extras = "‐‑–—―‘’“”…※→←↑↓○●△▲□■◇◆☆★℃±×÷≒≠≦≧∞"  # noqa: RUF001
    chars = [chr(cp) for lo, hi in ranges for cp in range(lo, hi + 1)]
    return "".join(chars) + extras + jis_level1_kanji()


def _render(font: ImageFont.FreeTypeFont, ch: str, ascent: int) -> Glyph:
    """1 文字をラスタライズする。

    フォントが持たない文字でも FreeType は .notdef を返してくるので、
    ここでは収録の有無を判定しない。それは呼び出し側が `_NOTDEF_PROBE`
    との一致で決める。
    """
    # Pillow がラスタ側を Unknown で返す理由と、cast している相手は `_Mask` の
    # docstring にある。
    mask, offset = cast("_RasterFont", font).getmask2(ch, mode="L")
    width, height = mask.size
    bitmap = bytes(mask)
    if width * height != len(bitmap):
        msg = f"unexpected mask stride for {ch!r}: {width}x{height} != {len(bitmap)}"
        raise ValueError(msg)
    return Glyph(
        codepoint=ord(ch),
        width=width,
        height=height,
        # getlength は float を返す。VLW の xAdvance は uint8 なので丸める。
        x_advance=round(font.getlength(ch)),
        dx=offset[0],
        # getmask2 の offset はアセンダ線からの下向き。VLW の dY は
        # ベースラインからの上向きなので、ascent との差を取る。
        dy=ascent - offset[1],
        bitmap=bitmap,
    )


def _fits(glyph: Glyph) -> bool:
    """M5GFX が読み戻せる範囲に収まっているか。

    幅と xAdvance は uint8、dX は int8、dY は int16 にキャストされる。
    はみ出したグリフを黙って載せると、デバイス側で幅が化けて隣の文字に
    重なる。載せずに落とすほうが原因を追える。
    """
    return (
        glyph.codepoint <= _MAX_CODEPOINT
        and 0 <= glyph.width <= 0xFF
        and 0 <= glyph.x_advance <= 0xFF
        and -0x80 <= glyph.dx <= 0x7F
        and -0x8000 <= glyph.dy <= 0x7FFF
    )


def rasterize(font_path: Path, size: int, chars: Iterable[str]) -> list[Glyph]:
    """`chars` のうちフォントが持っている文字を `size` px で起こす。

    重複は落とし、コードポイントの昇順に並べる。M5GFX が `lower_bound` で
    引く以上、順序はフォーマットの一部。
    """
    font = ImageFont.truetype(font_path, size)
    ascent, _descent = font.getmetrics()

    # .notdef の見た目。フォントが持たない文字はすべてこれになるので、
    # 一致したものを未収録として落とす。cmap を読むために fontTools を
    # 足すより、これで足りる。
    notdef = _render(font, _NOTDEF_PROBE, ascent)
    notdef_shape = (notdef.width, notdef.height, notdef.bitmap)

    glyphs: dict[int, Glyph] = {}
    for ch in chars:
        codepoint = ord(ch)
        if codepoint in glyphs:
            continue
        glyph = _render(font, ch, ascent)
        if (glyph.width, glyph.height, glyph.bitmap) == notdef_shape:
            continue
        if not _fits(glyph):
            continue
        glyphs[codepoint] = glyph
    return [glyphs[cp] for cp in sorted(glyphs)]


def build_vlw(glyphs: Sequence[Glyph], size: int) -> bytes:
    """グリフの並びを VLW のバイト列にする。

    `size` はヘッダの yAdvance に入る。M5GFX はこれを最終的な行送りとしては
    使わず、空白の幅 (`yAdvance * 2 / 7`) の算出にだけ使う。行送りは
    グリフの外接から決まる — モジュール docstring の ascent/descent の項を見よ。

    並べ替えはここでやる。順序はフォーマットの一部 (M5GFX は `lower_bound`
    で引く) であって呼び出し側の作法ではない、というのが理由。順序が崩れた
    ファイルは `loadFont` を素通りし、引けない文字が空白になって初めて分かる。
    """
    if not glyphs:
        msg = "no glyphs to write; the font covered none of the requested characters"
        raise ValueError(msg)

    ordered = sorted(glyphs, key=lambda g: g.codepoint)
    # 重複も lower_bound を壊す。黙って落とすと、落とした側のメトリクスが
    # 効いていない理由を後から追えない。
    if len({g.codepoint for g in ordered}) != len(ordered):
        msg = "duplicate codepoints in the glyph list"
        raise ValueError(msg)

    header = struct.pack(f">{_HEADER_FIELDS}I", len(ordered), 11, size, 0, 0, 0)
    table = b"".join(
        struct.pack(
            f">{_GLYPH_FIELDS}I",
            g.codepoint,
            g.height,
            g.width,
            g.x_advance,
            g.dy & 0xFFFFFFFF,
            g.dx & 0xFFFFFFFF,
            0,
        )
        for g in ordered
    )
    return header + table + b"".join(g.bitmap for g in ordered)


def summarize(glyphs: Sequence[Glyph], blob: bytes) -> dict[str, object]:
    """人とデバイスの両方の予算に効く数字。

    `heap` は M5GFX が `loadFont` で常駐させる配列の実測見積り。グリフ 1 つ
    あたり unicode(2) + width(1) + xAdvance(1) + dX(1) + dY(2) + bitmap ptr(4)。
    ここが機体の空きヒープを超えると `loadFont` は黙って失敗する。
    """
    per_glyph = 2 + 1 + 1 + 1 + 2 + 4
    return {
        "glyphs": len(glyphs),
        "bytes": len(blob),
        "table_bytes": _HEADER_BYTES + len(glyphs) * _GLYPH_BYTES,
        "heap_estimate": len(glyphs) * per_glyph,
        "max_height": max(g.height for g in glyphs),
        "line_height": max(g.dy for g in glyphs) + max(g.height - g.dy for g in glyphs),
    }


def push(blob: bytes, port: str, dest: str) -> None:
    """生成した VLW をデバイスの flash に置く。

    転送前に空きを見るのは、途中で溢れると中途半端なファイルが残って
    `loadFont` が黙って失敗するため。失敗の理由が残らないのが VLW 経路の
    いちばん厄介なところなので、ホスト側で分かることはホスト側で言う。
    """
    # 遅延 import。生成だけしたい呼び出しに、シリアルまわりの依存を
    # 引き込ませないため。
    from device_repl import ReplError, connect_repl

    repl = connect_repl(port)
    try:
        repl.exec("import os\n_s = os.statvfs('/flash')\n")
        free: int = repl.eval("_s[0] * _s[3]")
        if free < len(blob):
            msg = f"/flash has {free} bytes free, need {len(blob)}"
            raise ReplError(msg)
        repl.fs_writefile(dest, blob)
    finally:
        repl.exit_raw_repl()
        repl.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True, help="TTF か OTF のパス")
    parser.add_argument("--size", type=int, default=16, help="ピクセルサイズ")
    parser.add_argument("--out", type=Path, required=True, help="書き出す .vlw")
    parser.add_argument("--port", help="渡すとデバイスの flash へ転送する")
    parser.add_argument(
        "--dest",
        default="/flash/buddy-ja.vlw",
        help="デバイス上の置き場所。device/buddy/chat_font.py の VLW_PATH と揃える",
    )
    args = parser.parse_args(argv)

    glyphs = rasterize(args.font, args.size, default_charset())
    blob = build_vlw(glyphs, args.size)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(blob)

    for key, value in summarize(glyphs, blob).items():
        print(f"{key}: {value}")
    print(f"out: {args.out}")

    if args.port:
        push(blob, args.port, args.dest)
        print(f"pushed: {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
