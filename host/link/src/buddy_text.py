"""Turning Claude's prose into something the device's panel can show.

No I/O here, which is the point: this is the half of `buddy_verbs.say`
that can be checked without a device, and `device/tests/test_chat.py`
pairs these constants against the panel's own to keep the two in step.
"""

from __future__ import annotations

import re

# ----- chat
#
# The device renders a transcript in a 216x110 px panel. Text arrives
# here as whatever Claude wrote, which is prose with markdown in it, so
# it gets flattened and split before it goes on the wire.

# Roughly one panel's worth of text, which is the unit that matters:
# the device renders the *tail* of its transcript, so a message longer
# than the screen loses its opening before anyone can read it.
#
# Both numbers come from the metrics measured in device/buddy/chat.py
# and have to move with them. The panel picks its font from the content,
# and so does `_limit_for` below:
#
#   VLW 16 px        18 px tall, 16 px/glyph -> 6 rows x 13 chars
#   DejaVu12 @0.75   12 px tall,  9 px/glyph -> 9 rows x 24 chars
#
# Rounded down, because wrapping leaves a ragged right edge and a part
# that overflows by one row is a part whose first line is already gone.
#
# The Japanese figure assumes the VLW is installed. A board still on the
# built-in fallback fits 5 rows of 12 instead, so the tail of a
# full-length part scrolls its own opening off — visible, and fixed by
# running `host/tools/src/make_vlw.py --port ...` rather than by shrinking
# this. Every ack carries `vlw` so the state is diagnosable from here.
MAX_SAY_CHARS_WIDE = 68
MAX_SAY_CHARS = 184

# Keep in step with `_WIDE_FROM` in device/buddy/chat.py: the host has to
# predict which font the panel will choose, and it chooses on this.
_WIDE_FROM = 0x1100

# Seconds between parts of a split message. The panel shows only its
# last rows, so a burst would scroll past unread.
DEFAULT_PACE = 2.0

# Characters after which a split reads as a pause rather than a cut. The
# fullwidth forms are deliberate, not a paste accident: this text is
# mostly Japanese, where they are the sentence ends that actually occur.
_SENTENCE_ENDS = "。！？!?."  # noqa: RUF001


# Markdown that the panel has no way to render. Six rows of thirteen
# Japanese characters is the whole budget, so every character spent on
# syntax is one the reader does not get: there is no bold to show, a
# `##` costs a sixth of a row, and a code block is unreadable at this
# size anyway. All of it is flattened away here.
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HEADING = re.compile(r"^#{1,6}\s+")
_QUOTE = re.compile(r"^>\s?")
_BULLET = re.compile(r"^[-*+]\s+")
_ORDERED = re.compile(r"^\d+[.)]\s+")
_RULE = re.compile(r"^(?:[-*_]\s*){3,}$")
_STRONG = re.compile(r"\*\*([^*]+)\*\*")
_EMPHASIS = re.compile(r"\*([^*\n]+)\*")
# Underscore emphasis, but only where both delimiters sit outside a
# word. Without the boundary guards this rewrites `buddy_chat` to
# `buddychat`, which is worse than leaving the markup in. The body is
# allowed to contain underscores — `_buddy_chat.py_` is emphasis around
# an identifier — which is why the match is non-greedy: it stops at the
# first `_` that is itself at a word boundary.
_STRONG_UNDER = re.compile(r"(?<![^\W_])__([^\n]+?)__(?![^\W_])")
_UNDERLINE = re.compile(r"(?<![^\W_])_([^\n]+?)_(?![^\W_])")
_CODE_SPAN = re.compile(r"`+")

_FENCE = "```"
_CODE_MARKER = "[code]"


def _strip_markup(line: str) -> str:
    """Reduce one line of markdown to something the panel can show.

    Structure that survives at this size is kept: a list is still a list
    (every bullet style collapses to a compact ``- ``), and link text
    outlives its URL. Everything that only exists to be styled — bold,
    emphasis, code spans, heading hashes, blockquote markers, horizontal
    rules — is dropped.
    """
    line = _QUOTE.sub("", line)
    if _RULE.match(line):
        return ""
    line = _HEADING.sub("", line)
    if _BULLET.match(line) or _ORDERED.match(line):
        line = "- " + _BULLET.sub("", _ORDERED.sub("", line))
    line = _LINK.sub(r"\1", line)
    line = _STRONG.sub(r"\1", line)
    line = _STRONG_UNDER.sub(r"\1", line)
    line = _EMPHASIS.sub(r"\1", line)
    line = _UNDERLINE.sub(r"\1", line)
    line = _CODE_SPAN.sub("", line)
    return line.strip()


def normalize_for_device(text: str) -> str:
    """Flatten `text` into what should appear on the panel.

    Blank lines are dropped rather than preserved: they cost a row out
    of five and separate nothing the reader cannot already see from the
    colour change between messages.

    Fenced code is replaced by a single `[code]` marker. The marker is
    emitted on the opening fence, so a block whose fence is never closed
    still announces itself rather than vanishing — but everything after
    an unclosed fence is treated as code and dropped, which is the same
    thing every other markdown reader does with it.
    """
    lines: list[str] = []
    in_fence = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.strip().startswith(_FENCE):
            in_fence = not in_fence
            if in_fence:
                lines.append(_CODE_MARKER)
            continue
        if in_fence:
            continue
        line = _strip_markup(" ".join(raw.split()))
        if line:
            lines.append(line)
    return "\n".join(lines)


def _split_paragraph(para: str, limit: int) -> list[str]:
    """Break one over-long paragraph at the latest decent boundary."""
    out: list[str] = []
    while len(para) > limit:
        window = para[:limit]
        cut = max(window.rfind(c) for c in _SENTENCE_ENDS) + 1
        if cut <= 0:
            # No sentence end in reach. A space is the next best seam;
            # Japanese has none, so fall through to a hard cut rather
            # than emitting a message the device would have to clip.
            cut = window.rfind(" ") + 1
        if cut <= 0:
            cut = limit
        out.append(para[:cut].strip())
        para = para[cut:].lstrip()
    if para:
        out.append(para)
    return out


def _limit_for(text: str) -> int:
    """How much of `text` fits on one panel.

    Mirrors the font choice in `device/buddy/chat.py`: one wide glyph
    anywhere in the transcript pulls the whole panel onto the Japanese
    face, so a single Japanese character in an otherwise ASCII message
    costs three rows and eleven characters per row.
    """
    for ch in text:
        if ord(ch) >= _WIDE_FROM:
            return MAX_SAY_CHARS_WIDE
    return MAX_SAY_CHARS


def split_for_device(text: str, limit: int | None = None) -> list[str]:
    """Break normalized `text` into messages, in order.

    Paragraphs are packed together while they fit so a short exchange
    stays one bubble on screen, and only a paragraph that cannot fit on
    its own gets cut mid-sentence. `limit` defaults to whatever the
    panel can hold for this text; pass one to override.
    """
    if limit is None:
        limit = _limit_for(text)
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    parts: list[str] = []
    current = ""
    for para in text.split("\n"):
        for piece in _split_paragraph(para, limit) or [""]:
            if not piece:
                continue
            candidate = piece if not current else current + "\n" + piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts
