"""チャットパネルが抱える transcript。

`buddy/chat.py` から切り出した。積まれたメッセージの有界リストと、そこに
幅広 (CJK) のグリフが混じっているかという 1 ビットを持つ係。依存は
`chat` -> `chat_log` -> `chat_font` の一方向で、こちらはパネルの幾何も
描画も知らない。

### MicroPython

No `typing`, no slice deletion — 切り詰めは末尾への rebind で書く。
`device/tests/test_device_constraints.py` が AST で弾く。
"""

from buddy import chat_font

# Bounded so a long session cannot grow the transcript without limit.
# Only the last few rows are ever visible; the rest is scrollback we do
# not have a way to reach yet.
MAX_MESSAGES = 16


def text_of(msg):
    # type: (dict[str, object]) -> str
    """メッセージの本文。

    積むのは `Transcript.append()` で、そこで str へ寄せてから入れている
    ので実行時は常に str。それでも絞り直すのは、dict の値の型が `object`
    だから — `typing.cast` はここでは使えない。
    """
    text = msg.get("text", "")
    return text if isinstance(text, str) else str(text)


class Transcript:
    """役割と本文の組を積んだ有界リスト。"""

    def __init__(self) -> None:
        self._messages = []  # type: list[dict[str, object]]

        # True once any message holds a wide glyph, which is what pulls
        # the whole panel over to the CJK font. Sticky for as long as
        # that message is in the transcript.
        self.has_wide = False

    @property
    def messages(self):
        # type: () -> list[dict[str, object]]
        """積んである順のメッセージ。読む側で書き換えない。"""
        return self._messages

    def append(self, role, text):
        # type: (object, object) -> str
        """1 件積む。

        切り詰めと `has_wide` の再計算までここで済ませ、str へ寄せた本文を
        返す。折り返しを測るのに同じ文字列が要るのはパネルの側だが、寄せる
        のは 1 か所にしておく。
        """
        if not isinstance(text, str):
            text = str(text)
        self._messages.append({"role": role, "text": text})
        if len(self._messages) > MAX_MESSAGES:
            # Rebind rather than `del self._messages[:n]` — see the
            # MicroPython note in the module docstring.
            self._messages = self._messages[len(self._messages) - MAX_MESSAGES :]
        self._refresh_wide()
        return text

    def clear(self) -> None:
        """全部落とす。"""
        self._messages = []
        self.has_wide = False

    def _refresh_wide(self) -> None:
        """Recompute which font the transcript needs.

        Recomputed over the whole transcript rather than or-ed in on
        append: trimming to `MAX_MESSAGES` can evict the only message
        that held a wide glyph, and staying on the 27 px font after that
        would cost two rows for nothing.
        """
        for msg in self._messages:
            for ch in text_of(msg):
                if chat_font.is_wide(ch):
                    self.has_wide = True
                    return
        self.has_wide = False
