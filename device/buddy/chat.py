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

### 割れているところ

書体の選択・読み込み・計測は `buddy/chat_font.py`、行の折り返しは
`buddy/chat_wrap.py`。ここに残るのはパネルの幾何、transcript、verb の
振り分けと描画。

### MicroPython

No `typing`, no `__future__`, no slice deletion. See
`host/tests/test_device_constraints.py`, which enforces all three
against the AST. The LCD is injectable so the wrapping and layout logic
— the part with actual bugs in it — is testable on the host without a
board attached.
"""

import json

from buddy import chat_font, chat_wrap

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

# Bounded so a long session cannot grow the transcript without limit.
# Only the last few rows are ever visible; the rest is scrollback we do
# not have a way to reach yet.
_MAX_MESSAGES = 16

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
    return _ROLE_STYLE.get(role, _ROLE_STYLE[_DEFAULT_ROLE])  # pyright: ignore[reportCallIssue, reportUnknownVariableType, reportArgumentType]


class ChatPanel:
    """A transcript rendered into the Buddy dashboard's main panel."""

    # `lcd`, when passed explicitly, is a test double standing in for
    # M5.Lcd — no `typing.Protocol` on MicroPython to name what the two
    # have in common — so it stays unannotated and every access through
    # `self._lcd` below is ignored per-line.
    # `vlw_path` is a seam as much as a setting: pointing it at a real
    # file is how the host tests exercise the VLW branch without a board,
    # and passing "" is how they exercise the fallback. Empty rather than
    # None because annotations here have to be plain builtin names —
    # see `device/tests/test_device_constraints.py`.
    def __init__(self, lcd=None, vlw_path: str = chat_font.VLW_PATH) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        self._lcd = lcd if lcd is not None else _default_lcd()  # pyright: ignore[reportUnknownMemberType]
        self._messages = []  # type: list[dict[str, object]]
        self.active = False

        # True once any message holds a wide glyph, which is what pulls
        # the whole panel over to the CJK font. Sticky for as long as
        # that message is in the transcript.
        self._has_wide = False

        self._font = chat_font.ChatFont(self._lcd, vlw_path)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    # ----- transcript

    def say(self, role, text):
        # type: (object, object) -> int
        """Append one message. Returns how many rows it wraps to."""
        if not isinstance(text, str):
            text = str(text)
        self._messages.append({"role": role, "text": text})
        if len(self._messages) > _MAX_MESSAGES:
            # Rebind rather than `del self._messages[:n]` — see the
            # MicroPython note in the module docstring.
            self._messages = self._messages[len(self._messages) - _MAX_MESSAGES :]
        self.active = True
        self._refresh_wide()
        self._font.push(self._has_wide)
        try:
            return len(chat_wrap.wrap(text, self._body_width(), self._font))
        finally:
            self._font.pop()

    def clear(self) -> None:
        """Drop the transcript and hand the panel back to BuddyUI."""
        self._messages = []
        self._has_wide = False
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
        self._font.push(self._has_wide)
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

    def _refresh_wide(self) -> None:
        """Recompute which font the transcript needs.

        Recomputed over the whole transcript rather than or-ed in on
        append: trimming to `_MAX_MESSAGES` can evict the only message
        that held a wide glyph, and staying on the 27 px font after that
        would cost two rows for nothing.
        """
        for msg in self._messages:
            # `text` is always a str on the wire; msg's value type is the
            # generic `object` from dict[str, object], so the iteration
            # below is ignored per-line rather than narrowed with a
            # `typing.cast` MicroPython does not have.
            for ch in msg.get("text", ""):  # pyright: ignore[reportUnknownVariableType, reportGeneralTypeIssues]
                if chat_font.is_wide(ch):  # pyright: ignore[reportUnknownArgumentType]
                    self._has_wide = True
                    return
        self._has_wide = False

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
        for msg in self._messages:
            prefix, prefix_color, body_color = _role_style(msg.get("role"))
            first = True
            for row in chat_wrap.wrap(msg.get("text", ""), avail, self._font):  # pyright: ignore[reportArgumentType]
                out.append((prefix if first else "", prefix_color, row, body_color))
                first = False
        if max_rows > 0 and len(out) > max_rows:
            out = out[len(out) - max_rows :]
        return out

    def render(self) -> None:
        """Repaint the whole panel. Cheap enough to call unconditionally."""
        # `self._lcd` is duck-typed (see __init__), so every call through
        # `lcd` below is ignored per-line.
        lcd = self._lcd  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        self._font.push(self._has_wide)
        try:
            rows = self.layout(self._max_rows())
            lcd.fillRect(0, _Y0, _W, _Y1 - _Y0, _BLACK)  # pyright: ignore[reportUnknownMemberType]
            y = _Y0
            line_h = self._font.line_height()
            indent = self._font.indent_width(_INDENT)
            for prefix, prefix_color, body, body_color in rows:
                if prefix:
                    lcd.setTextColor(prefix_color, _BLACK)  # pyright: ignore[reportUnknownMemberType]
                    lcd.drawString(prefix, _X0, y)  # pyright: ignore[reportUnknownMemberType]
                lcd.setTextColor(body_color, _BLACK)  # pyright: ignore[reportUnknownMemberType]
                lcd.drawString(body, _X0 + indent, y)  # pyright: ignore[reportUnknownMemberType]
                y += line_h
        finally:
            self._font.pop()

    # ----- metrics

    def _body_width(self) -> int:
        return _W - _RIGHT_PAD - _X0 - self._font.indent_width(_INDENT)

    def _max_rows(self) -> int:
        return (_Y1 - _Y0) // self._font.line_height()
