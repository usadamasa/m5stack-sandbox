"""Where the daemon's config, state and socket live. XDG, not the repo.

### Why not repo-relative

The chatter's socket used to be derived from the git root, which worked
while the MCP server was spawned per session from inside this checkout.
A resident daemon is started once from wherever the user happens to be,
and its hook fires from whatever project the session is in — the two
sides no longer share a repository to measure from. An absolute path
that both can compute from the environment alone is the only thing left
that they agree on.

### Why the config file flattens onto `BUDDY_*`

The server and the chatter already read their settings from the
environment, every one of those names is documented where it is read,
and the tools that retune the chatter at runtime work in the same terms.
A config file that maps onto those names one for one adds a place to put
them and nothing else to learn. So `config.toml` is not a schema of its
own: `port` becomes `BUDDY_PORT`, and `gap_min` under `[chatter]`
becomes `BUDDY_CHATTER_GAP_MIN`.

The environment still wins, so an exported variable overrides the file
for one run without editing it.

### What deliberately is not in the file

The socket path. The hook has to find it too, and the hook runs on the
system `python3` with no repository on its path and a millisecond of
budget — parsing TOML there would be both fragile and slow. It is either
`BUDDY_CHATTER_SOCKET` or the XDG default, and those few lines are
duplicated in the hook with a contract test holding them together.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

APP = "buddy"

# Names the daemon writes under the state directory.
PID_NAME = "buddy-mcpd.pid"
LOG_NAME = "buddy-mcpd.log"
SOCKET_NAME = "chatter.sock"
HEALTH_NAME = "health.json"
CONFIG_NAME = "config.toml"


def _home(env: Mapping[str, str], var: str, fallback: str) -> Path:
    """An XDG base directory, with the spec's rule about relative values.

    The spec says a relative path is invalid and the default applies.
    That matters here rather than being pedantry: a daemon that resolved
    one against its own cwd would keep its pid file somewhere different
    on every start, and `buddy-mcpd stop` would find nothing.
    """
    raw = env.get(var, "")
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path(env.get("HOME", "~")).expanduser() / fallback


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where `config.toml` is read from."""
    env = os.environ if env is None else env
    return _home(env, "XDG_CONFIG_HOME", ".config") / APP


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where the pid file, the log and the socket live."""
    env = os.environ if env is None else env
    return _home(env, "XDG_STATE_HOME", ".local/state") / APP


def config_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / CONFIG_NAME


def socket_path(env: Mapping[str, str] | None = None) -> Path:
    """The chatter's datagram socket. Must match the hook's own answer."""
    env = os.environ if env is None else env
    override = env.get("BUDDY_CHATTER_SOCKET", "")
    if override:
        return Path(override)
    return state_dir(env) / SOCKET_NAME


def projects_dir(env: Mapping[str, str] | None = None) -> Path:
    """Claude Code がセッションの transcript を置くところ。

    ここにあるものは buddy のものではない。chatter が読みに行くのは、hook が
    名乗ったセッションが何をしているかを知る手立てが他に無いから — 線に
    載るのは `kind` と 100 文字の `detail` だけで、それはどのセッションの
    ものかを言わない。

    `CLAUDE_CONFIG_DIR` に従うのは、それを決めているのが Claude Code の側
    だから。相対値を捨てる理由は XDG のときと同じで、`_home` がそれを持って
    いる。
    """
    env = os.environ if env is None else env
    return _home(env, "CLAUDE_CONFIG_DIR", ".claude") / "projects"


def pid_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / PID_NAME


def log_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / LOG_NAME


def health_path(env: Mapping[str, str] | None = None) -> Path:
    """起動時の疏通確認の結果。daemon が書き、`buddy-mcpd status` が読む。

    pid ファイルと同じ扱いで、これも「そのとき何が見えたか」でしかない。
    daemon が生きているかは別に問うこと。
    """
    return state_dir(env) / HEALTH_NAME


def _flatten(table: Mapping[str, Any], prefix: str) -> dict[str, str]:
    """One TOML table into `BUDDY_*` names. One level of nesting only.

    Deeper nesting is not rejected so much as not invented: every
    setting this maps onto is a flat name already, and a config file
    that could express something the readers cannot would be a trap.
    """
    out: dict[str, str] = {}
    for key, value in table.items():
        name = f"{prefix}_{key}".upper()
        if isinstance(value, dict):
            # `isinstance` alone narrows to `dict[Unknown, Unknown]`;
            # TOML keys are strings by construction.
            out.update(_flatten(cast("Mapping[str, Any]", value), name))
        elif isinstance(value, bool):
            # "1"/"0", not TOML's true/false: that is what `_bool_env`
            # and `_connect_on_start_wanted` on the other side accept.
            out[name] = "1" if value else "0"
        else:
            out[name] = str(value)
    return out


def config_env(path: Path | None = None) -> dict[str, str]:
    """Read `config.toml` as `BUDDY_*` settings. Missing file is empty.

    A malformed file raises. Falling back to the defaults would turn a
    typo into "the daemon opened the wrong device", noticed days later;
    refusing to start says it at once.
    """
    path = config_path() if path is None else path
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path} is not readable as config.toml: {exc}") from exc
    return _flatten(parsed, "BUDDY")


def merge_env(env: Mapping[str, str], from_file: Mapping[str, str]) -> dict[str, str]:
    """The environment over the file. Empty counts as unset.

    `FOO=` is how a variable gets cleared by accident in a shell, and a
    blank that shadowed a configured value would be a puzzle to debug.
    """
    merged = dict(from_file)
    merged.update({k: v for k, v in env.items() if v or k not in merged})
    return merged


def environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The settings the server should run on: the file, then the process."""
    env = os.environ if env is None else env
    return merge_env(env, config_env(config_path(env)))
