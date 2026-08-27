"""Chat panel: what the Claude Code session says, on the Cardputer LCD.

The Buddy dashboard that `buddy_ui_cp.py` draws is a status readout —
one clipped line for `msg`, and a permission box that only answers
yes/no. That is enough to report state and not enough to hold a
conversation. This module takes over the main panel and renders a
scrolling transcript instead: word-wrapped, multi-line, colour-coded by
who is speaking.

### Why the commands are handled here and not in buddy_protocol

`buddy_protocol.py` lives on the device's flash unmodified — it comes
from upstream and this repository deliberately does not ship a copy.
Its dispatcher logs anything it does not recognise as "unknown cmd", so
new verbs have to be peeled off before it sees them. `claude_buddy.py`
owns the `on_line` callback, which is the one seam we control, and it
routes `chat.*` here. Everything else falls through untouched.

    {"cmd":"chat.say","role":"claude","text":"..."}  -> append + repaint
    {"cmd":"chat.clear"}                             -> drop the transcript
    {"cmd":"chat.info"}                              -> report font/geometry

Every ack carries the resolved font name and a `cjk` flag. The host
needs that: whether Japanese renders at all depends on which fonts the
installed UIFlow build happens to ship, and a panel full of blank boxes
should be diagnosable from the host side rather than by squinting at
the device.

### Screen region

We own y=0..110 while a transcript is up: everything except the hint
strip. The "Claude Buddy / LINKED" header goes with it, which is a
deliberate trade — at 27 px per row that header is a whole row of text,
and the connection state it shows is already implied by messages
arriving. The hint strip stays because Q is the only way out of the app
short of BtnRST, and a legend nobody can see is not a legend.

Two consequences for the caller: `BuddyUI.update_footer()` must not run
while `active` is set (it paints y=96..110, inside us), and clearing the
transcript has to repaint the chrome we covered.

### 割れているところ

書体の選択・読み込み・計測は `buddy/chat_font.py`、transcript の保持は
`buddy/chat_log.py`、行の折り返しは `buddy/chat_wrap.py`。ここに残るのは
パネルの幾何、verb の振り分けと描画。どの書体をなぜ選ぶか、VLW が何者かと
いう経緯は `chat_font.py` の docstring にある。

### MicroPython

No `typing`, no `__future__`, no slice deletion. See
`host/tests/test_device_constraints.py`, which enforces all three
against the AST. The LCD is injectable so the wrapping and layout logic
— the part with actual bugs in it — is testable on the host without a
board attached.
"""

import json

from buddy import chat_font, chat_log, chat_wrap

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import Lcd  # noqa: F401

# Anthropic palette, inlined. Same values as buddy_ui_cp.py, duplicated
# for the same reason it duplicates them: importing that module pulls in
# `M5` at module scope, which does not exist off-device.
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_BLACK = 0x000000
_CYAN = 0x00FFFF
_GRAY_MID = 0x777777
_RED = 0xFF0000

_W = 240
_X0 = 4
_RIGHT_PAD = 4

# Top of the screen down to just above the hint strip hairline (y=111).
# 110 px is six rows of the VLW or nine of DejaVu12 at 0.75.
_Y0 = 0
_Y1 = 110

# prefix, prefix colour, body colour. Keyed on `object` rather than `str`:
# `role` is whatever the wire handed us, and `.get()` has to accept that
# without narrowing the key type first.
_ROLE_STYLE = {
    "claude": ("> ", _ORANGE, _CREAM),
    "user": ("< ", _CYAN, _GRAY_MID),
    "sys": ("! ", _RED, _GRAY_MID),
}  # type: dict[object, tuple[str, int, int]]
_DEFAULT_ROLE = "claude"

# 本文の字下げ幅を測るための文字列。`_ROLE_STYLE` の接頭辞はどれも同じ
# 2 文字なので、どれで測っても同じ幅になる。
_INDENT = "> "


def _default_lcd():
    """The real panel. Imported lazily so the host can import this."""
    import M5

    return M5.Lcd


def _role_style(role):
    # type: (object) -> tuple[str, int, int]
    # `role` is whatever the wire handed us in a "role" field — dict.get()
    # works with any hashable key, so a malformed non-str value still
    # resolves to the default style rather than raising.
    return _ROLE_STYLE.get(role, _ROLE_STYLE[_DEFAULT_ROLE])


class ChatPanel:
    """A transcript rendered into the Buddy dashboard's main panel."""

    # `lcd`, when passed explicitly, is a test double standing in for
    # M5.Lcd. The two share no base class, so the surface is pinned by a
    # Protocol in `device/typings/buddy_types.pyi` and named from a
    # `# type:` comment — annotations here have to be plain builtin names
    # (see `device/tests/test_device_constraints.py`).
    # `vlw_path` is a seam as much as a setting: pointing it at a real
    # file is how the host tests exercise the VLW branch without a board,
    # and passing "" is how they exercise the fallback. Empty rather than
    # None for the same constraint.
    def __init__(
        self,
        lcd=None,  # type: Lcd | None
        vlw_path: str = chat_font.VLW_PATH,
    ) -> None:
        self._lcd = lcd if lcd is not None else _default_lcd()
        self._log = chat_log.Transcript()
        self.active = False
        self._font = chat_font.ChatFont(self._lcd, vlw_path)

    # ----- transcript

    def say(self, role, text):
        # type: (object, object) -> int
        """Append one message. Returns how many rows it wraps to."""
        body = self._log.append(role, text)
        self.active = True
        self._font.push(self._log.has_wide)
        try:
            return len(chat_wrap.wrap(body, self._body_width(), self._font))
        finally:
            self._font.pop()

    def clear(self) -> None:
        """Drop the transcript and hand the panel back to BuddyUI."""
        self._log.clear()
        self.active = False

    def info(self):
        # type: () -> dict[str, object]
        """Font and geometry, for the host to sanity-check against.

        `cjk` reports whether this board *can* draw Japanese at all, not
        whether it is doing so right now — the panel switches back to the
        narrower Latin font whenever the transcript allows it, and a host
        reading `cjk: false` off that would draw the wrong conclusion
        about why its text looked wrong.

        `vlw` separates the two Japanese paths. Both draw, but the
        fallback fits a third less on screen, so a host whose messages
        arrive clipped can tell from here whether the font was ever
        installed rather than guessing.
        """
        self._font.push(self._log.has_wide)
        try:
            return {
                "font": self._font.name or "default",
                "cjk": self._font.has_cjk(),
                "vlw": self._font.vlw is not None,
                "scale": self._font.scale,
                "rows": self._max_rows(),
                "px": self._body_width(),
            }
        finally:
            self._font.pop()

    # ----- command dispatch

    def handle_raw(self, raw):
        # type: (bytes | bytearray | str) -> dict[str, object] | None
        """Parse one wire line and dispatch it. None if it is not ours.

        Deliberately quiet about malformed input: `buddy_protocol` is
        the layer that owns "bad line" reporting, and anything we reject
        here still falls through to it.
        """
        try:
            msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(msg, dict):
            return None
        # json.loads() is untyped, so isinstance() only narrows `msg` to
        # dict[Unknown, Unknown] rather than the dict[str, object] handle()
        # declares. Runtime-safe regardless: the isinstance check above
        # already guarantees this is a dict.
        return self.handle(msg)  # pyright: ignore[reportUnknownArgumentType]

    def handle(self, msg):
        # type: (dict[str, object]) -> dict[str, object] | None
        """Dispatch one parsed command. None if the cmd is not ours."""
        cmd = msg.get("cmd")
        if cmd == "chat.say":
            rows = self.say(msg.get("role", _DEFAULT_ROLE), msg.get("text", ""))
            ack = {"ack": "chat.say", "ok": True, "wrapped": rows}  # type: dict[str, object]
        elif cmd == "chat.clear":
            self.clear()
            ack = {"ack": "chat.clear", "ok": True}
        elif cmd == "chat.info":
            ack = {"ack": "chat.info", "ok": True}
        else:
            return None
        ack["active"] = self.active
        ack.update(self.info())
        # Echoed so a host that pipelines several messages can match
        # acks to sends without relying on ordering.
        if "id" in msg:
            ack["id"] = msg["id"]
        return ack

    # ----- layout

    def layout(self, max_rows):
        # type: (int) -> list[tuple[str, int, str, int]]
        """Rows to paint, oldest first, clipped to the newest `max_rows`.

        Each row is ``(prefix, prefix_colour, body, body_colour)``; the
        prefix is empty on continuation rows so a wrapped message reads
        as one block.

        Must be called inside a `ChatFont.push` bracket — it measures.
        """
        avail = self._body_width()
        out = []  # type: list[tuple[str, int, str, int]]
        for msg in self._log.messages:
            prefix, prefix_color, body_color = _role_style(msg.get("role"))
            first = True
            for row in chat_wrap.wrap(chat_log.text_of(msg), avail, self._font):
                out.append((prefix if first else "", prefix_color, row, body_color))
                first = False
        if max_rows > 0 and len(out) > max_rows:
            out = out[len(out) - max_rows :]
        return out

    def render(self) -> None:
        """Repaint the whole panel. Cheap enough to call unconditionally."""
        lcd = self._lcd
        self._font.push(self._log.has_wide)
        try:
            rows = self.layout(self._max_rows())
            lcd.fillRect(0, _Y0, _W, _Y1 - _Y0, _BLACK)
            y = _Y0
            line_h = self._font.line_height()
            indent = self._font.indent_width(_INDENT)
            for prefix, prefix_color, body, body_color in rows:
                if prefix:
                    lcd.setTextColor(prefix_color, _BLACK)
                    lcd.drawString(prefix, _X0, y)
                lcd.setTextColor(body_color, _BLACK)
                lcd.drawString(body, _X0 + indent, y)
                y += line_h
        finally:
            self._font.pop()

    # ----- metrics

    def _body_width(self) -> int:
        return _W - _RIGHT_PAD - _X0 - self._font.indent_width(_INDENT)

    def _max_rows(self) -> int:
        return (_Y1 - _Y0) // self._font.line_height()
