"""Which coding agent is driving this server, and how we found out.

The MCP server and the chatter are used from Claude Code and from Codex.
Almost nothing differs between them — the tools are the same and the
device does not care who asked. One thing does: where the chatter's
lines come from. Claude Code's machine has application-default
credentials for Vertex AI; Codex's machine has a configured Codex CLI.
Each agent gets the model it already pays for, so the pairing is fixed
rather than a 2x2 of agent times backend.

### Why this is resolved at runtime and not at install time

The server is registered separately by each agent (`.mcp.json` for
Claude Code, `[mcp_servers]` in `config.toml` for Codex), so an
environment variable in each registration would work. It would also mean
the two registrations describe *different* servers, and that the same
checkout behaves differently depending on which file launched it. The
identity is a property of the connection, so it is read from the
connection.

### Two witnesses

`initialize` carries `clientInfo.name`, which is the direct answer and
arrives before any tool call. The chatter's hook datagrams carry an
`agent` field too, which covers the standalone runner (no MCP handshake
at all) and any future arrangement where the events and the tool calls
come from different places.

Both write here and the last one wins. That is not a conflict-resolution
policy so much as an admission that there is nothing to resolve: one
server process serves one client, so the two witnesses agree, and when
they cannot the more recent one is the one still talking.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

# The agents this knows about. Anything else resolves to `UNKNOWN`, which
# is not an error — it just means the default applies.
CLAUDE_CODE = "claude-code"
CODEX = "codex"
UNKNOWN = "unknown"

# Substrings, not exact names. `clientInfo.name` is whatever the client
# chose to call itself and has changed shape before ("claude-code",
# "claude-ai", "codex-mcp-client"); matching loosely is what keeps a
# rename from silently falling back to the default backend.
_MARKERS: tuple[tuple[str, str], ...] = (
    ("codex", CODEX),
    ("claude", CLAUDE_CODE),
)

# Environment variables each agent leaves in a hook's environment. Only
# consulted when nobody passed an explicit name — the hook takes
# `--agent`, and both registrations in this repo pass it.
_ENV_MARKERS: tuple[tuple[str, str], ...] = (
    ("CODEX_HOME", CODEX),
    ("CODEX_SANDBOX", CODEX),
    ("CLAUDECODE", CLAUDE_CODE),
    ("CLAUDE_CODE_ENTRYPOINT", CLAUDE_CODE),
    ("CLAUDE_PROJECT_DIR", CLAUDE_CODE),
)


def identify(name: str | None) -> str:
    """Resolve a client name to one of the constants above.

    Case-insensitive and substring-based, on purpose — see `_MARKERS`.
    `codex` is tested first because "codex" is the more specific claim:
    a client calling itself both is a Codex build of something.
    """
    if not name:
        return UNKNOWN
    lowered = name.lower()
    for marker, agent in _MARKERS:
        if marker in lowered:
            return agent
    return UNKNOWN


def identify_env(env: Mapping[str, str]) -> str:
    """Guess the agent from the variables it exported. Best effort.

    A fallback for a hook registered without `--agent`. Neither agent
    documents these as an interface, so a wrong answer here has to be
    harmless: it only picks which model writes the muttering.
    """
    for var, agent in _ENV_MARKERS:
        if env.get(var):
            return agent
    return UNKNOWN


class AgentIdentity:
    """The agent currently believed to be driving, shared across threads.

    Written from the MCP dispatcher's event loop and read from the
    chatter's worker thread, so the assignment is guarded. It is one
    string and the readers tolerate a stale answer — the lock is for
    clarity about that, not for correctness of a Python attribute store.
    """

    def __init__(self, default: str = CLAUDE_CODE) -> None:
        self._default = default if default in (CLAUDE_CODE, CODEX) else CLAUDE_CODE
        self._observed = UNKNOWN
        self._client_name = ""
        self._lock = threading.Lock()

    @property
    def default(self) -> str:
        """What `current` answers until something is observed."""
        return self._default

    @property
    def current(self) -> str:
        """The agent to build a line source for. Never `UNKNOWN`."""
        with self._lock:
            return self._observed if self._observed != UNKNOWN else self._default

    @property
    def observed(self) -> str:
        """What was actually seen, `UNKNOWN` included. For reporting."""
        with self._lock:
            return self._observed

    @property
    def client_name(self) -> str:
        """The raw name the client gave, before `identify`. For reporting."""
        with self._lock:
            return self._client_name

    def observe(self, name: str | None) -> str:
        """Record a witness. Unrecognised names are ignored, not stored.

        Ignoring rather than storing `UNKNOWN` matters: an MCP client we
        do not know about must not un-set what the hooks already told
        us, and vice versa.
        """
        agent = identify(name)
        if agent == UNKNOWN:
            return self.current
        with self._lock:
            self._observed = agent
            if name:
                self._client_name = name
            return agent

    def status(self) -> dict[str, str]:
        with self._lock:
            return {
                "agent": self._observed if self._observed != UNKNOWN else self._default,
                "observed": self._observed,
                "client": self._client_name,
                "default": self._default,
            }
