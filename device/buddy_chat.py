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
change with `uv run python host/probe_device.py`:

    font              fontHeight()   "あ"   "A"
    EFontJA24              27         23    17
    AlibabaSansJA24        27         23    17
    DejaVu12               16         --    12
    DejaVu9                15         --    10

24 px is the *only* size at which this build has Japanese glyphs, and it
costs three quarters of the panel: four rows of nine characters. So the
font is chosen per repaint from the content — the moment any wide
character is in the transcript we switch to EFontJA24 and accept the
four rows, and until then DejaVu12 gives six rows of seventeen. A file
path wrapped every twelve characters is not readable, and that is what
pinning the CJK font would do to every ASCII message.

`setFont` is sticky on this driver, so every entry point that measures
or draws brackets itself with `_push_font` / `_pop_font` and hands
DejaVu9 back to `BuddyUI`. Widths are measured per character and cached
— the proportional-font warning in `buddy_ui_cp` applies doubly here —
and the cache is dropped whenever the selected font changes.

### MicroPython

No `typing`, no `__future__`, no slice deletion. See
`host/tests/test_device_constraints.py`, which enforces all three
against the AST. The LCD is injectable so the wrapping and layout logic
— the part with actual bugs in it — is testable on the host without a
board attached.
"""

import json

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
# 110 px is four rows of EFontJA24 or six of DejaVu12.
_Y0 = 0
_Y1 = 110

# Rows are painted at exactly fontHeight() with no extra leading: both
# fonts here already carry padding inside the glyph cell, and at 27 px a
# row of leading is a quarter of the panel.
_LEADING = 0

# Bounded so a long session cannot grow the transcript without limit.
# Only the last few rows are ever visible; the rest is scrollback we do
# not have a way to reach yet.
_MAX_MESSAGES = 16

# Used when the driver has no fontHeight(). Never hit on UIFlow 2.0,
# but the alternative is a ZeroDivision in the row-count maths.
_FALLBACK_LINE_H = 12

# Fonts with Japanese glyphs, best first. Both are 27 px tall on this
# build; EFontJA24 is the bitmap face, AlibabaSansJA24 the fallback if a
# future build drops it.
_WIDE_FONTS = ("EFontJA24", "AlibabaSansJA24")

# Used while the transcript is ASCII-only, where they buy two extra rows
# and five extra characters per row over the CJK face.
_NARROW_FONTS = ("DejaVu12", "DejaVu9")

# The font BuddyUI expects to find when it paints. Restored on the way
# out of every bracketed section.
_BASE_FONT = "DejaVu9"

# prefix, prefix colour, body colour. Keyed on `object` rather than `str`:
# `role` is whatever the wire handed us, and `.get()` has to accept that
# without narrowing the key type first.
_ROLE_STYLE = {
    "claude": ("> ", _ORANGE, _CREAM),
    "user": ("< ", _CYAN, _GRAY_MID),
    "sys": ("! ", _RED, _GRAY_MID),
}  # type: dict[object, tuple[str, int, int]]
_DEFAULT_ROLE = "claude"

# Above this codepoint a glyph is wide enough — and, more importantly,
# standalone enough — that a line may be broken on either side of it
# without a space. Covers kana, CJK ideographs and fullwidth forms.
_WIDE_FROM = 0x1100


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


def _is_wide(ch: str) -> bool:
    return ord(ch) >= _WIDE_FROM


def _can_break_between(prev: str, ch: str) -> bool:
    """True if a line may be split between these two characters."""
    if prev == " ":
        return True
    return _is_wide(prev) or _is_wide(ch)


class ChatPanel:
    """A transcript rendered into the Buddy dashboard's main panel."""

    # `lcd`, when passed explicitly, is a test double standing in for
    # M5.Lcd — no `typing.Protocol` on MicroPython to name what the two
    # have in common — so it stays unannotated and every access through
    # `self._lcd` below is ignored per-line.
    def __init__(self, lcd=None) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        self._lcd = lcd if lcd is not None else _default_lcd()  # pyright: ignore[reportUnknownMemberType]
        self._messages = []  # type: list[dict[str, object]]
        self.active = False

        # Per-character advance widths under the chat font. Populated on
        # demand inside a _push_font bracket; a CJK transcript repeats
        # the same few hundred glyphs, so this converges fast.
        self._char_w = {}  # type: dict[str, int]
        self._indent_w = None  # type: int | None
        self._line_h = None  # type: int | None

        # True once any message holds a wide glyph, which is what pulls
        # the whole panel over to the CJK font. Sticky for as long as
        # that message is in the transcript.
        self._has_wide = False

        self._font_name = None  # type: str | None
        self._wide_name = None  # type: str | None
        self._wide_font = None  # type: object | None
        self._narrow_name = None  # type: str | None
        self._narrow_font = None  # type: object | None
        self._base_font = None  # type: object | None
        self._resolve_fonts()

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
        self._push_font()
        try:
            return len(self._wrap(text, self._body_width()))
        finally:
            self._pop_font()

    def clear(self) -> None:
        """Drop the transcript and hand the panel back to BuddyUI."""
        self._messages = []
        self._has_wide = False
        self.active = False

    def info(self):
        # type: () -> dict[str, object]
        """Font and geometry, for the host to sanity-check against.

        `cjk` reports whether this build *has* a Japanese face, not
        whether one is selected right now — the panel switches back to
        the narrower Latin font whenever the transcript allows it, and a
        host reading `cjk: false` off that would draw the wrong
        conclusion about why its text looked wrong.
        """
        self._push_font()
        try:
            return {
                "font": self._font_name or "default",
                "cjk": self._wide_font is not None,
                "rows": self._max_rows(),
                "px": self._body_width(),
            }
        finally:
            self._pop_font()

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
                if _is_wide(ch):  # pyright: ignore[reportUnknownArgumentType]
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

        Must be called inside a _push_font bracket — it measures.
        """
        avail = self._body_width()
        out = []  # type: list[tuple[str, int, str, int]]
        for msg in self._messages:
            prefix, prefix_color, body_color = _role_style(msg.get("role"))
            first = True
            for row in self._wrap(msg.get("text", ""), avail):  # pyright: ignore[reportArgumentType]
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
        self._push_font()
        try:
            rows = self.layout(self._max_rows())
            lcd.fillRect(0, _Y0, _W, _Y1 - _Y0, _BLACK)  # pyright: ignore[reportUnknownMemberType]
            y = _Y0
            line_h = self._line_height()
            for prefix, prefix_color, body, body_color in rows:
                if prefix:
                    lcd.setTextColor(prefix_color, _BLACK)  # pyright: ignore[reportUnknownMemberType]
                    lcd.drawString(prefix, _X0, y)  # pyright: ignore[reportUnknownMemberType]
                lcd.setTextColor(body_color, _BLACK)  # pyright: ignore[reportUnknownMemberType]
                lcd.drawString(body, _X0 + self._indent_width(), y)  # pyright: ignore[reportUnknownMemberType]
                y += line_h
        finally:
            self._pop_font()

    # ----- wrapping

    def _wrap(self, text, avail):
        # type: (str, int) -> list[str]
        """Break `text` into rows no wider than `avail` pixels.

        Greedy, with two kinds of break opportunity: after a space, and
        on either side of a wide (CJK) glyph. Japanese has no spaces, so
        a space-only wrapper would emit one enormous row and clip it —
        which is exactly the failure the dashboard's `msg` line already
        has and the reason this module exists.

        A run with no break opportunity at all (a long URL, a hash) is
        hard-broken at the last character that fits rather than allowed
        to overflow.
        """
        rows = []  # type: list[str]
        for para in text.replace("\r", "").split("\n"):
            if not para.strip():
                rows.append("")
                continue
            line = ""
            line_w = 0
            brk = 0
            for ch in para:
                if not line and ch == " ":
                    continue  # never start a row on a space
                w = self._advance(ch)
                if line and line_w + w > avail:
                    if brk:
                        rows.append(line[:brk].rstrip())
                        line = line[brk:]
                    else:
                        rows.append(line)
                        line = ""
                    line_w = self._measure(line)
                    brk = 0
                if line and _can_break_between(line[-1], ch):
                    brk = len(line)
                line += ch
                line_w += w
            if line:
                rows.append(line)
        return rows

    def _advance(self, ch: str) -> int:
        w = self._char_w.get(ch)
        if w is None:
            w = self._lcd.textWidth(ch)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            self._char_w[ch] = w
        return w  # pyright: ignore[reportUnknownVariableType]

    def _measure(self, text: str) -> int:
        total = 0
        for ch in text:
            total += self._advance(ch)
        return total

    # ----- metrics

    def _body_width(self) -> int:
        return _W - _RIGHT_PAD - _X0 - self._indent_width()

    def _indent_width(self) -> int:
        if self._indent_w is None:
            self._indent_w = self._measure("> ")
        return self._indent_w

    def _line_height(self) -> int:
        line_h = self._line_h
        if line_h is None:
            font_height = getattr(self._lcd, "fontHeight", None)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            height = int(font_height()) if font_height is not None else 0
            line_h = (height + _LEADING) if height else _FALLBACK_LINE_H
            self._line_h = line_h
        return line_h

    def _max_rows(self) -> int:
        return (_Y1 - _Y0) // self._line_height()

    # ----- font bracketing

    def _resolve_fonts(self) -> None:
        fonts = getattr(self._lcd, "FONTS", None)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if fonts is None:
            return
        self._base_font = getattr(fonts, _BASE_FONT, None)
        self._wide_name, self._wide_font = self._first_font(fonts, _WIDE_FONTS)
        self._narrow_name, self._narrow_font = self._first_font(fonts, _NARROW_FONTS)
        if self._wide_font is None:
            # Not fatal — Latin still draws. But Japanese will come out
            # as blanks, and `cjk` in every ack is how the host finds
            # that out without anyone looking at the device.
            print("buddy_chat: no CJK font on this build; Japanese will not render")
        if self._narrow_font is None and self._wide_font is None:
            print("buddy_chat: no known font on this build, using the driver default")

    def _first_font(self, fonts, names):
        # type: (object, tuple[str, ...]) -> tuple[str, object] | tuple[None, None]
        for name in names:
            font = getattr(fonts, name, None)
            if font is not None:
                return name, font
        return None, None

    def _select_font(self):
        # type: () -> tuple[str | None, object | None]
        """The (name, font) the current transcript should be drawn in."""
        if self._has_wide and self._wide_font is not None:
            return self._wide_name, self._wide_font
        if self._narrow_font is not None:
            return self._narrow_name, self._narrow_font
        return self._wide_name, self._wide_font

    def _push_font(self) -> None:
        name, font = self._select_font()
        if name != self._font_name:
            # Every cached number below was measured under the old face.
            self._font_name = name
            self._char_w = {}
            self._indent_w = None
            self._line_h = None
        if font is None:
            return
        try:
            self._lcd.setFont(font)  # pyright: ignore[reportUnknownMemberType]
        except Exception as e:
            print("buddy_chat: setFont failed:", e)

    def _pop_font(self) -> None:
        if self._base_font is None:
            return
        try:
            self._lcd.setFont(self._base_font)  # pyright: ignore[reportUnknownMemberType]
        except Exception as e:
            print("buddy_chat: font restore failed:", e)
