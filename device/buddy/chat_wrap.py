"""チャットパネルの行分割。

幅を測る `chat_font.ChatFont` を受け取るだけで、LCD にも状態にも触らない。
何行になるかを決めるのはここで、それをどこへ描くかは `buddy/chat.py`。
"""

from buddy import chat_font


def wrap(text, avail, font):
    # type: (str, int, chat_font.ChatFont) -> list[str]
    """`text` を `avail` ピクセルに収まる行へ割る。

    貪欲法で、折り返しの機会は 2 種類 — 空白の後と、wide (CJK) グリフの
    両隣。日本語には空白が無いので、空白だけを見る折り返しは巨大な 1 行を
    吐いて切り落とす。それはダッシュボードの `msg` 行が既に抱えている失敗
    そのもので、このモジュールがある理由でもある。

    折り返しの機会が 1 つも無い連なり (長い URL、ハッシュ) は、はみ出させる
    のではなく、収まる最後の文字で強制的に割る。

    `font` は `push` で挟んだ中で渡すこと — 測るため。
    """
    rows = []  # type: list[str]
    for para in text.replace("\r", "").split("\n"):
        if not para.strip():
            rows.append("")
            continue
        rows.extend(_wrap_paragraph(para, avail, font))
    return rows


def _wrap_paragraph(para, avail, font):
    # type: (str, int, chat_font.ChatFont) -> list[str]
    """1 つの段落を `avail` ピクセル以下の行へ割る。"""
    rows = []  # type: list[str]
    line = ""
    line_w = 0
    brk = 0
    for ch in para:
        if not line and ch == " ":
            continue  # 行頭を空白で始めない
        w = font.advance(ch)
        if line and line_w + w > avail:
            row, line = _break_at(line, brk)
            rows.append(row)
            line_w = font.measure(line)
            brk = 0
        if line and _can_break_between(line[-1], ch):
            brk = len(line)
        line += ch
        line_w += w
    if line:
        rows.append(line)
    return rows


def _can_break_between(prev: str, ch: str) -> bool:
    """この 2 文字の間で行を割ってよいか。"""
    if prev == " ":
        return True
    return chat_font.is_wide(prev) or chat_font.is_wide(ch)


def _break_at(line, brk):
    # type: (str, int) -> tuple[str, str]
    """満杯の行を (吐き出す行, 次へ持ち越すぶん) に割る。

    `brk` が 0 なら折り返しの機会が 1 つも無かった — 長い URL やハッシュ —
    ということなので、行はそのまま吐いて何も持ち越さない。これが強制的な
    折り返し。
    """
    if brk:
        return line[:brk].rstrip(), line[brk:]
    return line, ""
