"""Supervisor for the resident Buddy MCP server. `buddy-mcpd`.

### What this exists for

The MCP server used to be spawned per session by the agent, which meant
one process per session competing for a serial port that takes exactly
one owner, and it meant a change to the host code only reached a server
that was restarted with the whole session. A single resident process
listening on HTTP fixes both: sessions come and go against it, and
`buddy-mcpd restart` is the whole of "pick up my edit".

### Why a supervisor and not launchd

Because the failure everyone actually hits is "who has the port". A
command that says it in one line, and a log file at a fixed path, are
worth more here than starting itself at login. Nothing stops a launchd
job calling `buddy-mcpd start` later.

### Why the pid is checked and the file is not trusted

A crash leaves the pid file behind. Treating the file as the truth
would mean the daemon can never start again without a manual delete —
so the file only ever names a candidate, and whether it is alive is
asked of the operating system.

### Sandboxing

The daemon issues `tcsetattr` on a USB serial device, which Seatbelt
refuses. It has to be started from outside the agent's sandbox: this is
why the process is spawned in its own session and detached rather than
being kept as a child of whatever launched it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import buddy_paths
from buddy_mcp import FALLBACK_PORT, HTTP_HOST, http_port

# How long each stage of a stop waits. Split unevenly on purpose.
#
# A daemon nobody is attached to answers the first SIGTERM in well under
# a second, so waiting longer there only makes the common case slow. The
# second stage is the one that needs room: it is reached only when a
# session is holding an HTTP connection open, and after the interrupt
# uvicorn still has to tear that down while the daemon lets go of the
# serial port and the socket. Measured at ~1s for the cleanup alone.
TERM_GRACE = 2.0
INT_GRACE = 10.0

Spawn = Callable[[list[str], Path], int]
Kill = Callable[[int, int], None]
Alive = Callable[[int], bool]


def is_running(pid: int) -> bool:
    """Whether a process with this pid exists and we may signal it.

    `EPERM` counts as running: a pid owned by somebody else is still a
    pid in use, and refusing to start is the right answer either way.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def read_pid(env: Mapping[str, str] | None = None) -> int | None:
    """The pid the file names, or None if there is nothing readable.

    A truncated or empty file reads as "not running" rather than as an
    error: it is what a crash mid-write leaves, and the caller's next
    move — check whether it is alive — handles it correctly anyway.
    """
    try:
        raw = buddy_paths.pid_path(env).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _clear_pid(env: Mapping[str, str] | None) -> None:
    with contextlib.suppress(OSError):
        buddy_paths.pid_path(env).unlink()


def _spawn_detached(argv: list[str], log: Path) -> int:
    """Start the daemon in its own session, output appended to the log.

    `start_new_session` is what makes it survive the shell — and the
    agent — that asked for it. Output goes to a file because a detached
    process has nowhere else to put it, and because "why did it not
    start" is answered there.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as sink:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


def _daemon_argv() -> list[str]:
    """How to launch the server, from wherever this is installed.

    `-m` against the running interpreter rather than the `buddy-mcp`
    console script: both are installed together, but only the
    interpreter's own path is certain to be on this process's PATH when
    an agent launched it.
    """
    return [sys.executable, "-m", "buddy_mcp", "--http"]


def start(
    env: Mapping[str, str] | None = None,
    spawn: Spawn = _spawn_detached,
    running: Alive = is_running,
) -> dict[str, Any]:
    """Start the daemon unless one is already up."""
    pid = read_pid(env)
    if pid is not None and running(pid):
        return {"started": False, "pid": pid, "note": "already running"}
    state = buddy_paths.state_dir(env)
    state.mkdir(parents=True, exist_ok=True)
    spawned = spawn(_daemon_argv(), buddy_paths.log_path(env))
    buddy_paths.pid_path(env).write_text(f"{spawned}\n", encoding="utf-8")
    return {"started": True, "pid": spawned, "log": str(buddy_paths.log_path(env))}


def stop(
    env: Mapping[str, str] | None = None,
    kill: Kill = os.kill,
    running: Alive = is_running,
    term_grace: float = TERM_GRACE,
    int_grace: float = INT_GRACE,
) -> dict[str, Any]:
    """Stop the daemon and release the port. Idempotent."""
    pid = read_pid(env)
    if pid is None or not running(pid):
        _clear_pid(env)
        return {"stopped": False, "note": "was not running"}
    kill(pid, signal.SIGTERM)
    forced = False
    if not _gone_within(pid, running, term_grace):
        # uvicorn reads the first signal as "finish serving what you
        # have" and then waits for open HTTP connections to close — and
        # a session that is still attached never closes one.
        #
        # SIGINT, not a second SIGTERM: uvicorn only sets `force_exit`
        # when the repeat is an interrupt, and treats a repeat SIGTERM
        # as another ordinary shutdown request. That is what its own
        # "(CTRL+C to force quit)" line is saying. Without this every
        # stop ends in SIGKILL and `forced` stops meaning anything.
        kill(pid, signal.SIGINT)
        if not _gone_within(pid, running, int_grace):
            # It had its chance. A daemon that will not let go of the
            # serial port is worse than one killed with the port still
            # open — the kernel closes the fd either way.
            kill(pid, signal.SIGKILL)
            forced = True
    _clear_pid(env)
    return {"stopped": True, "pid": pid, "forced": forced}


def _gone_within(pid: int, running: Alive, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while running(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def status(env: Mapping[str, str] | None = None, running: Alive = is_running) -> dict[str, Any]:
    """What is up, where it listens, and where to read about it.

    `url` is printed because the registration that reaches this server
    is a static URL on the default port: if `config.toml` has moved the
    port, this line is where the two stop matching.
    """
    resolved = buddy_paths.environment(env)
    pid = read_pid(env)
    live = pid is not None and running(pid)
    return {
        "running": live,
        "pid": pid if live else None,
        "url": f"http://{HTTP_HOST}:{http_port(resolved)}/mcp",
        "port": resolved.get("BUDDY_PORT") or FALLBACK_PORT,
        "pid_file": str(buddy_paths.pid_path(env)),
        "log": str(buddy_paths.log_path(env)),
        "socket": str(buddy_paths.socket_path(env)),
        "config": str(buddy_paths.config_path(env)),
    }


def restart(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Stop and start. The whole of "pick up my edit"."""
    return {"stopped": stop(env), "started": start(env)}


def main(argv: Sequence[str] | None = None) -> int:
    """The console script `buddy-mcpd` lands here."""
    import argparse

    parser = argparse.ArgumentParser(prog="buddy-mcpd", description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "restart", "status"))
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    actions: dict[str, Callable[[], dict[str, Any]]] = {
        "start": start,
        "stop": stop,
        "restart": restart,
        "status": status,
    }
    result = actions[args.action]()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == "status" and not result["running"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
