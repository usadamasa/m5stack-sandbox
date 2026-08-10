#!/usr/bin/env python3
"""Tell the Buddy MCP server that something happened. Fire and forget.

Registered on most of the agent's hook events. Reduces the payload to a
kind and a short subject, sends one datagram, and exits 0 — whatever
happened.

### One script, both agents

Claude Code and Codex share the command-handler shape and the common
event fields on stdin. Their event sets are not identical, so each
product keeps a thin registration file. Both registrations call this
script unchanged, apart from `--agent`.

That flag is the point of the difference. It rides along in the datagram
and is how the server knows which model should write the muttering —
Vertex AI for Claude Code, the Codex CLI for Codex. Registering without
it is not an error: the server falls back to guessing from the
environment, and then to its default.

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

# .agents/hooks/buddy_chatter_notify.py -> repo root. Must agree with
# DEFAULT_SOCKET in host/mcp/src/buddy_chatter.py.
SOCKET = os.environ.get("BUDDY_CHATTER_SOCKET") or str(
    Path(__file__).resolve().parents[2] / "tmp" / "buddy-chatter.sock"
)

# Keys in `tool_input` that say what a call is about, most specific
# first. Falling through all of them leaves just the tool's name, which
# is still worth sending.
_SUBJECT_KEYS = ("command", "pattern", "description", "query", "url")
_PATH_KEYS = ("file_path", "notebook_path", "path")

# The detail is pasted into a prompt on the other side; the server
# clamps it too, but there is no reason to put a whole Bash heredoc on
# the wire first.
_MAX_DETAIL = 100

# Variables each agent leaves in a hook's environment, checked only when
# `--agent` was not passed. Neither agent documents these as an
# interface, so a wrong answer has to be harmless — and it is: it picks
# which model writes a line of muttering. Kept in step with
# `_ENV_MARKERS` in host/mcp/src/buddy_agent.py, and duplicated rather
# than imported because this runs on every tool call and may not have
# the repo's src directory on its path.
_ENV_AGENTS = (
    ("CODEX_HOME", "codex"),
    ("CODEX_SANDBOX", "codex"),
    ("CLAUDECODE", "claude-code"),
    ("CLAUDE_CODE_ENTRYPOINT", "claude-code"),
    ("CLAUDE_PROJECT_DIR", "claude-code"),
)


def agent_from(argv: list[str], env: dict[str, str]) -> str:
    """Who is running us: `--agent NAME`, else a guess, else nothing.

    Parsed by hand rather than with argparse. This is on the critical
    path of every tool call, and importing argparse to read one flag is
    more work than reading the flag.
    """
    for i, arg in enumerate(argv):
        if arg.startswith("--agent="):
            return arg.split("=", 1)[1]
        if arg == "--agent" and i + 1 < len(argv):
            return argv[i + 1]
    for var, agent in _ENV_AGENTS:
        if env.get(var):
            return agent
    return ""


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


def classify(payload: dict) -> tuple[str, str] | None:
    """Reduce a hook payload to (kind, detail), or None to send nothing."""
    event = str(payload.get("hook_event_name") or "")
    if event == "PreToolUse":
        return "tool", _tool_detail(payload)
    if event == "PostToolUse":
        return ("error" if _failed(payload) else "tool"), _tool_detail(payload)
    if event == "PostToolUseFailure":
        return "error", _tool_detail(payload)
    if event == "Notification":
        return "notify", str(payload.get("message") or "")[:_MAX_DETAIL]
    if event == "Stop":
        return "stop", ""
    if event == "SessionStart":
        return "session", str(payload.get("source") or "")
    if event == "UserPromptSubmit":
        return "prompt", " ".join(str(payload.get("prompt") or "").split())[:_MAX_DETAIL]
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        classified = classify(payload)
        if classified is None:
            return 0
        kind, detail = classified
        message = {"kind": kind, "detail": detail}
        agent = agent_from(sys.argv[1:], dict(os.environ))
        if agent:
            message["agent"] = agent
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            # Bounded so a full receive buffer cannot park a tool call
            # here. Losing a line of chatter is not worth a millisecond
            # of anyone's attention.
            sock.settimeout(0.2)
            sock.sendto(json.dumps(message, ensure_ascii=False).encode(), SOCKET)
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
