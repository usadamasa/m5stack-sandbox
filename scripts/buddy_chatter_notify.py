#!/usr/bin/env python3
"""Tell the Buddy MCP server that something happened. Fire and forget.

Registered on most of the agent's hook events. Reduces the payload to a
kind and a short subject, sends one datagram, and exits 0 — whatever
happened.

### Why a datagram

The socket is `AF_UNIX`/`SOCK_DGRAM`, which has no connection to set up,
no reply to wait for, and no requirement that anybody be listening. If
the MCP server is not running the `sendto` fails immediately and this
still exits 0. That is the property that matters: a hook is on the
critical path of every tool call it fires on, so it must cost
essentially nothing and must never be able to fail the call.

Nothing is spoken here. Synthesis and playback take seconds; doing them
in the hook would add those seconds to the tool call. The server's
worker thread does that part, on its own time.

Stdlib only, and no stdout. `UserPromptSubmit` and `SessionStart` inject
a hook's stdout into Claude's context, and this has nothing to say to it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def socket_path(env: dict) -> str:
    """Where the daemon is listening. Must agree with `buddy_paths`.

    Duplicated rather than imported: this runs on the system `python3`
    on every tool call, with no repository on its path and no time to
    spend. The contract test in host/mcp/tests holds the two answers
    together.

    A relative `XDG_STATE_HOME` is invalid per the spec and the default
    applies — the same rule the other side follows, which matters
    because the two have different working directories.
    """
    override = env.get("BUDDY_CHATTER_SOCKET")
    if override:
        return override
    raw = env.get("XDG_STATE_HOME", "")
    base = (
        Path(raw)
        if raw and os.path.isabs(raw)
        else Path(env.get("HOME", "~")).expanduser() / ".local/state"
    )
    return str(base / "buddy" / "chatter.sock")


SOCKET = socket_path(dict(os.environ))

# Keys in `tool_input` that say what a call is about, most specific
# first. Falling through all of them leaves just the tool's name, which
# is still worth sending.
_SUBJECT_KEYS = ("command", "pattern", "description", "query", "url")
_PATH_KEYS = ("file_path", "notebook_path", "path")

# The detail is pasted into a prompt on the other side; the server
# clamps it too, but there is no reason to put a whole Bash heredoc on
# the wire first.
_MAX_DETAIL = 100


def _tool_detail(payload: dict) -> str:
    name = str(payload.get("tool_name") or "tool")
    args = payload.get("tool_input")
    if not isinstance(args, dict):
        return name
    for key in _SUBJECT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}: {' '.join(value.split())[:_MAX_DETAIL]}"
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return f"{name}: {os.path.basename(value)}"
    return name


def _failed(payload: dict) -> bool:
    """Whether a completed tool call went wrong.

    `PostToolUseFailure` covers the calls Claude Code itself treats as
    failures — a non-zero exit from Bash, and so a failing test run.
    This catches the rest: tools that report trouble inside an otherwise
    successful response.
    """
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return False
    return bool(
        response.get("is_error") or response.get("error") or response.get("success") is False
    )


def _text(payload: dict, key: str) -> str:
    """A payload field as a string. Missing, null and empty all read the same."""
    return str(payload.get(key) or "")


def classify(payload: dict) -> tuple[str, str] | None:
    """Reduce a hook payload to (kind, detail), or None to send nothing."""
    event = _text(payload, "hook_event_name")
    if event == "PreToolUse":
        return "tool", _tool_detail(payload)
    if event == "PostToolUse":
        return ("error" if _failed(payload) else "tool"), _tool_detail(payload)
    if event == "PostToolUseFailure":
        return "error", _tool_detail(payload)
    if event == "Notification":
        return "notify", _text(payload, "message")[:_MAX_DETAIL]
    if event == "Stop":
        return "stop", ""
    if event == "SessionStart":
        return "session", _text(payload, "source")
    if event == "UserPromptSubmit":
        return "prompt", " ".join(_text(payload, "prompt").split())[:_MAX_DETAIL]
    return None


def message(payload: dict) -> dict | None:
    """線に載せるデータグラム 1 つ。送るものが無ければ None。

    `session_id` は載せるが `transcript_path` は載せない。どちらも payload に
    あるが、socket はこのマシンの誰にでも開いているので、daemon が開く
    ファイルを送り主に決めさせるわけにはいかない。向こう側は名乗られた
    セッションを UUID として検めたうえで、自分の知っている置き場から
    transcript を引く。
    """
    classified = classify(payload)
    if classified is None:
        return None
    kind, detail = classified
    return {"kind": kind, "detail": detail, "session": _text(payload, "session_id")}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        built = message(payload)
        if built is None:
            return 0
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            # Bounded so a full receive buffer cannot park a tool call
            # here. Losing a line of chatter is not worth a millisecond
            # of anyone's attention.
            sock.settimeout(0.2)
            sock.sendto(json.dumps(built, ensure_ascii=False).encode(), SOCKET)
        finally:
            sock.close()
    except Exception:
        # Nowhere for an error to go, and nothing it could usefully do.
        # A hook that fails loudly here would be worse than a quiet
        # device.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
