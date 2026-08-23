"""Wire format for the Claude Buddy protocol over USB serial.

The device multiplexes protocol traffic and `print()` logging onto one
channel, so protocol lines carry a sentinel prefix and everything else
is passed through as log output. What lives here is the part both ends
have to agree on byte for byte, plus the two Protocols that let the
helpers in `buddy_verbs` work against either link in `buddy_link`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol, cast

# Keep in sync with _SENTINEL in device/buddy/serial.py.
SENTINEL = b"\x1eBUDDY1 "

# Matches buddy_protocol._send, which uses the same separators. Keeping
# the encodings identical means a byte-level diff of a capture is
# meaningful in both directions.
_JSON_SEPARATORS = (",", ":")

# Protocol payloads are whatever the device chose to send, so the values
# stay Any; naming the alias at least keeps the intent legible.
Message = dict[str, Any]

# What the demux hands back: ("protocol", json body) or ("log", raw line).
Item = tuple[str, bytes]


class SerialPort(Protocol):
    """The slice of `serial.Serial` this module actually uses.

    Narrow on purpose: it is what lets the tests drive a fake port
    without pulling in pyserial's full surface, and it documents the
    contract a replacement transport would have to meet.
    """

    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1, /) -> bytes: ...

    def write(self, data: bytes, /) -> int | None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


SerialFactory = Callable[..., SerialPort]


def encode(obj: Message) -> bytes:
    """Frame one message for the device."""
    body = json.dumps(obj, separators=_JSON_SEPARATORS).encode("utf-8")
    return SENTINEL + body + b"\n"


def decode(payload: bytes) -> Message:
    """Parse one protocol payload (sentinel already stripped)."""
    parsed: Any = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"protocol payload is not an object: {parsed!r}")
    # json.loads の戻りは Any なので isinstance では dict[Unknown, Unknown] にしか
    # 絞り込めない。キーが str であることまでは isinstance で保証できないが、
    # プロトコル上の契約として str キーの object しか送られてこない。
    return cast(Message, parsed)


class Requester(Protocol):
    """The one method the chat helpers need from a link.

    `BuddyLink` and `ResidentLink` both satisfy it, which is what lets
    the CLI and the MCP server share `say`.
    """

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message: ...


class LineDemux:
    """Split a byte stream into protocol messages and log lines.

    Classification happens only on complete lines, which is what makes
    a read that splits the sentinel itself safe.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Item]:
        """Absorb a read and return whatever it completed.

        Returns a list of ``(kind, payload)`` where kind is "protocol"
        (payload is the JSON body) or "log" (payload is the raw line).
        """
        self._buf.extend(chunk)
        out: list[Item] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl]).rstrip(b"\r")
            del self._buf[: nl + 1]
            if not line:
                continue
            if line.startswith(SENTINEL):
                body = line[len(SENTINEL) :]
                if body:
                    out.append(("protocol", body))
                continue
            out.append(("log", line))
        return out
