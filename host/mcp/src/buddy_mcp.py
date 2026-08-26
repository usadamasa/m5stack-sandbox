"""MCP server exposing the Cardputer-Adv over the Buddy serial protocol.

Wraps `buddy_link.ResidentLink` so a coding agent can talk to the
device through tool calls instead of shelling out. The link is held open
across calls, which is what makes device-initiated traffic visible to
`buddy_events` rather than being lost between invocations.

### The open question this server answers

Claude Code's sandbox is documented as covering the Bash tool and its
child processes; MCP servers are spawned by Claude Code itself and are
not mentioned. If that reading is right, this process is outside the
Seatbelt container and can issue the `tcsetattr` ioctl that a serial
port needs. If it is wrong, `probe_serial` reports EPERM and the whole
MCP approach has to be replaced by a Bash-launched resident bridge.

Run `probe_serial` first. Everything else depends on its answer.

### One device, two callers

`buddy_chatter` runs a worker thread in this process that speaks on its
own while Claude works. That makes this the first place where two things
want the link at once, and `ResidentLink.await_ack` matches acks by name
and pops the first that fits — two overlapping requests of the same kind
would hand each other's answers back. So every tool below holds
`_device_lock` for the whole of its exchange with the device, and the
chatter only ever takes that lock when it is already free.

### Configuration

`BUDDY_PORT` selects the device (default `/dev/cu.usbmodem101`).
`BUDDY_CHATTER=0` turns the chatter off. `BUDDY_CONNECT_ON_START=1` has
the server open the port once as it starts, so that the muttering runs
from the beginning of a session rather than from the first time somebody
calls `buddy_connect`. Registered via `.mcp.json` — see `README.md`.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import sys
import termios
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal, cast

# The server is launched by the agent from an arbitrary cwd, so make the
# sibling module importable by absolute path rather than relying on the
# working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer

import buddy_paths
from buddy_chatter import ChatterService
from buddy_link import ResidentLink
from buddy_text import DEFAULT_PACE
from buddy_verbs import (
    DEBUG_OPS,
    DEFAULT_RATE,
    ZUNDAMON,
    announce_debug_entry,
    debug,
    say,
    speak,
    voicevox_url,
)
from chatter_core import ChatterConfig
from device_repl import ReplError

# The device node a Cardputer-Adv comes up as on macOS. Named as a
# constant because `buddy-mcpd status` reports the resolved port too,
# and two copies of this string would drift.
FALLBACK_PORT = "/dev/cu.usbmodem101"
DEFAULT_PORT = buddy_paths.environment().get("BUDDY_PORT") or FALLBACK_PORT

# Where the resident daemon listens, and what `.mcp.json` is registered
# against. The registration is a static URL, so this number is a
# contract rather than a preference: changing it in `config.toml` means
# changing the registration too. `buddy-mcpd status` prints the URL it
# is actually serving, which is the place that discrepancy shows up.
DEFAULT_HTTP_PORT = 8787
HTTP_HOST = "127.0.0.1"
HTTP_PATH = "/mcp"

# How long uvicorn may wait for open connections before dropping them.
#
# It has to be bounded. `Server._wait_tasks_to_complete` finishes with
# `await server.wait_closed()`, which has no escape for `force_exit` —
# so a stop that lands while a request is in flight waits on a client
# that may never come back. The supervisor then SIGKILLs the daemon, and
# nothing in this process gets to release the serial port or the socket.
#
# Must leave room inside `buddy_mcpd.TERM_GRACE` for `_shutdown` (~1s),
# or the bound buys nothing.
SHUTDOWN_TIMEOUT = 3

server = MCPServer(
    name="buddy",
    version="0.1.0",
    instructions=(
        "Talks to an M5Stack Cardputer-Adv running the Claude Buddy app over "
        "USB serial. Call probe_serial first on a new machine or after a "
        "sandbox settings change; if it reports tcsetattr failure, no other "
        "tool here will work. The running app has no REPL of its own — use "
        "buddy_debug to inspect it in place, and buddy_interrupt to drop it "
        "back to a prompt without touching the board."
    ),
)

_link: ResidentLink | None = None

# Held for the whole of one exchange with the device — send and the ack
# that answers it — by anything that talks to it. Not reentrant: no tool
# here nests, and a plain lock is what `acquire(blocking=False)` on the
# chatter's side needs to mean "somebody else is mid-request".
_device_lock = threading.Lock()

_chatter: ChatterService | None = None


def _get_link(port: str | None = None) -> ResidentLink:
    global _link
    target = port or DEFAULT_PORT
    if _link is not None and _link.connected and _link.port != target:
        _link.disconnect()
        _link = None
    if _link is None or not _link.connected:
        _link = ResidentLink(target)
        _link.connect()
    return _link


@contextlib.contextmanager
def _device(port: str | None = None) -> Generator[ResidentLink]:
    """Take the device for one tool call, opening the port if needed."""
    with _device_lock:
        yield _get_link(port)


def _live_link() -> ResidentLink | None:
    """The link if one is already up, else None. Never opens the port.

    This is what the chatter is given. Handing it `_get_link` instead
    would have it claim the port back whenever it fancied a line, which
    is exactly what `buddy_deploy.py` and `esptool` need it not to do:
    `buddy_disconnect` has to stay the last word on who holds the port.

    `_connect_on_start` below is the one opening, and it is a single
    attempt at startup rather than anything this function can trigger.
    """
    return _link if _link is not None and _link.connected else None


# What `_connect_on_start` made of its one attempt, or None if it never
# ran. Reported through `buddy_chatter_status`: a chatter that is silent
# because the port was never opened looks the same from `skipped_offline`
# as one whose device was unplugged mid-session.
_startup_connect: dict[str, Any] | None = None


def _connect_on_start_wanted(env: Mapping[str, str], *, default: bool = False) -> bool:
    """Whether the server should take the port as it starts.

    The default depends on what kind of process this is, which is why
    it is a parameter. A server spawned by one agent over stdio is a
    guest and leaves the port alone until asked — grabbing it would
    surprise `buddy_deploy.py`, which needs it free. The resident daemon
    is the opposite: holding the port is the whole reason it exists, and
    one that waited to be asked would leave the device silent until some
    session happened to call `buddy_connect`.

    Either way `BUDDY_CONNECT_ON_START` (or `connect_on_start` in
    `config.toml`) has the last word.
    """
    raw = env.get("BUDDY_CONNECT_ON_START", "")
    if not raw:
        return default
    return raw in ("1", "true", "yes")


def _connect_on_start(port: str | None = None) -> dict[str, Any]:
    """Open the port once, for the chatter's benefit. Never raises.

    Runs on its own thread beside the session's first tool calls, so it
    takes `_device_lock` like any of them; `_get_link` on a free port is
    a reader thread and a handshake, not an exchange with the device,
    but a tool call landing in the middle of it would still find a link
    half-built.

    One attempt. Failing here means the board is unplugged or another
    process holds the port, and neither is fixed by trying again — the
    agent can call `buddy_connect` once it is. Nobody is waiting on this
    result either, so an exception would only end the thread silently:
    it is recorded instead.
    """
    global _startup_connect
    try:
        with _device(port) as link:
            _startup_connect = {"ok": True, "port": link.port}
            log.info("port opened: %s", link.port)
    except Exception as exc:
        _startup_connect = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        # Warning rather than error: the daemon is still useful without
        # the device, and the board being unplugged is an ordinary state
        # rather than a fault. But it belongs in the log — otherwise the
        # only way to learn the port was never opened is to ask a tool.
        log.warning("port not opened: %s", _startup_connect["error"])
    return _startup_connect


def _chatter_service() -> ChatterService:
    global _chatter
    if _chatter is None:
        _chatter = ChatterService(ChatterConfig.from_env(), _live_link, _device_lock)
    return _chatter


def _decode_logs(logs: list[bytes]) -> list[str]:
    return [line.decode("utf-8", errors="replace") for line in logs]


@server.tool()
def probe_serial(port: str = "") -> dict[str, Any]:
    """Check whether this process may issue the ioctl a serial port needs.

    Opens the device node, reads its termios attributes and writes them
    straight back. The write is the operation Seatbelt gates separately
    from read/write access, so an EPERM here means this process is inside
    the sandbox and the MCP approach is not viable.
    """
    target = port or DEFAULT_PORT
    result: dict[str, Any] = {
        "port": target,
        "open": False,
        "tcgetattr": False,
        "tcsetattr": False,
    }
    try:
        fd = os.open(target, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        result["error"] = f"open failed: {e.strerror} (errno {e.errno})"
        result["verdict"] = "device node unavailable — is it plugged in and powered?"
        return result

    result["open"] = True
    try:
        attrs = termios.tcgetattr(fd)
        result["tcgetattr"] = True
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        result["tcsetattr"] = True
    except (OSError, termios.error) as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        os.close(fd)

    if result["tcsetattr"]:
        result["verdict"] = "outside the sandbox — serial tools will work"
    else:
        result["verdict"] = (
            "ioctl denied — this process is sandboxed; use a Bash-launched "
            "resident bridge instead of MCP"
        )
    return result


@server.tool()
def buddy_connect(port: str = "") -> dict[str, Any]:
    """Open the serial link and start buffering device output."""
    with _device(port) as link:
        return {"connected": link.connected, "port": link.port}


@server.tool()
def buddy_disconnect() -> dict[str, Any]:
    """Release the serial port so other tools (push.py, esptool) can use it."""
    global _link
    # Under the lock: closing the port while the chatter is mid-utterance
    # would surface as an ENXIO on a write it is in the middle of.
    with _device_lock:
        if _link is None:
            return {"connected": False, "note": "was not connected"}
        _link.disconnect()
        _link = None
    return {"connected": False}


@server.tool()
def buddy_start_app(settle: float = 8.0, wait: float = 15.0) -> dict[str, Any]:
    """Interrupt to the REPL and launch the Buddy app on the device.

    A running app is Ctrl-C'd first, so calling this twice in a row
    works. `wait` bounds how long a BtnRST press is waited for when that
    does not get us a prompt — a device wedged below the Python level, or
    a bundle old enough to still disable the interrupt.

    Returns the startup output, which is where a launch traceback lands.
    """
    with _device() as link:
        try:
            link.start_app(settle=settle, wait=wait)
        except ReplError as e:
            return {"started": False, "error": str(e)}
        msgs, logs = link.events()
    return {"started": True, "messages": msgs, "logs": _decode_logs(logs)}


@server.tool()
def buddy_status(timeout: float = 8.0) -> dict[str, Any]:
    """Ask the device for its status ack (name, owner, battery, heap, stats)."""
    with _device() as link:
        return link.request({"cmd": "status"}, "status", timeout=timeout)


@server.tool()
def buddy_set_name(name: str, timeout: float = 8.0) -> dict[str, Any]:
    """Set the device's display name. Persisted in NVS across reboots."""
    with _device() as link:
        return link.request({"cmd": "name", "name": name}, "name", timeout=timeout)


@server.tool()
def buddy_set_owner(owner: str, timeout: float = 8.0) -> dict[str, Any]:
    """Set the owner string shown on the device. Persisted in NVS."""
    with _device() as link:
        return link.request({"cmd": "owner", "owner": owner}, "owner", timeout=timeout)


@server.tool()
def buddy_say(
    text: str,
    role: str = "claude",
    timeout: float = 8.0,
    pace: float = DEFAULT_PACE,
) -> dict[str, Any]:
    """Show `text` on the device's chat panel.

    Markdown is flattened (the panel cannot render it and every symbol
    costs a character) and the result is split into panel-sized parts,
    sent in order with `pace` seconds between them so a long message
    stays readable as it scrolls. This call therefore blocks for roughly
    `pace * (parts - 1)` seconds; pass `pace=0` if nobody is watching.

    `role` picks the colour and prefix: "claude" (orange `>`), "user"
    (cyan `<`) or "sys" (red `!`).

    Each ack reports the font the panel resolved and whether the build
    has Japanese glyphs at all (`cjk`). If `cjk` is false, non-Latin
    text is on screen as blanks — a firmware font gap, not a transfer
    failure.
    """
    with _device() as link:
        acks = say(link, text, role=role, timeout=timeout, pace=pace)
    return {"parts": len(acks), "acks": acks}


@server.tool()
def buddy_speak(
    text: str,
    speaker: int = ZUNDAMON,
    engine: str = "",
    rate: int = DEFAULT_RATE,
    show: bool = True,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Say `text` out loud on the device, and by default show it too.

    The device fetches its own audio: it calls a VOICEVOX engine over
    WiFi and streams the WAV straight into M5.Speaker. Nothing but the
    text crosses the cable.

    The device must already be on the network, which is a one-time setup
    step rather than anything this server does: `host/provision_wifi.py`
    writes the credentials into the bundle's boot-time connect, and from
    then on the device associates by itself at power-on. Without that,
    this fails with a connection error from the device.

    The engine runs in Docker on this Mac and must be published on all
    interfaces (`-p 50021:50021`), because the device reaches it over
    the LAN and not over loopback. `engine` defaults to `$VOICEVOX_URL`
    and then to this machine's LAN address.

    `speaker` is a VOICEVOX style id from the engine's `/speakers`;
    3 is Zundamon (normal), 2 is Shikoku Metan, 8 is Kasukabe Tsumugi.

    Blocks for synthesis (seconds) plus playback. Returns the device's
    `speak.end` ack — a non-zero `stalls` means the device ran out of
    audio waiting on the network and the utterance gapped.
    """
    url = voicevox_url(engine or None)
    with _device() as link:
        shown = say(link, text, timeout=timeout, pace=0) if show else []
        ack = speak(link, text, url=url, speaker=speaker, rate=rate, timeout=timeout)
    return {"engine": url, "shown": len(shown), "end": ack}


@server.tool()
def buddy_chat_clear(timeout: float = 8.0) -> dict[str, Any]:
    """Wipe the chat panel and hand the screen back to the dashboard."""
    with _device() as link:
        return link.request({"cmd": "chat.clear"}, "chat.clear", timeout=timeout)


@server.tool()
def buddy_chat_info(timeout: float = 8.0) -> dict[str, Any]:
    """Report the chat panel's resolved font, CJK support and geometry."""
    with _device() as link:
        return link.request({"cmd": "chat.info"}, "chat.info", timeout=timeout)


@server.tool()
def buddy_events() -> dict[str, Any]:
    """Drain everything the device has said since the last call.

    Covers both protocol messages the device sent on its own (the `hello`
    it emits on handshake) and plain print() logging from the app.
    """
    with _device() as link:
        msgs, logs = link.events()
        dropped = link.dropped
    return {"messages": msgs, "logs": _decode_logs(logs), "dropped": dropped}


# ----- debug
#
# The app owns the console for the length of its run, so the REPL that
# would answer "what is the heap doing" is not there while the state
# worth looking at exists. These two are the way in: one asks the running
# app, the other ends it and hands the prompt back.


@server.tool()
def buddy_debug(
    op: str = "mem",
    src: str = "",
    timeout: float = 8.0,
    settle: float = 0.4,
    announce: bool = True,
) -> dict[str, Any]:
    """Inspect the running app in place, without stopping it.

    `op` is one of:

      mem    both heaps. `free`/`alloc` are MicroPython's; `idf_free` and
             `idf_largest` are the ESP-IDF heap that sockets come out of,
             and the one a failing `buddy_speak` is usually short of.
      frag   dump the heap map. Arrives in `logs`, not in `ack`.
      gc     collect, and report the free heap either side of it.
      state  the transport, chat panel and speech player at a glance.
      eval   evaluate `src` against the app's live objects (`ble`, `chat`,
             `speech`, `state`, `ui`, `proto`, `chars`). Capped at 192
             characters — it compiles on the device.
      exec   run `src` as a statement. Output goes to `logs`.
      off    unload the debug module and report the heap it gave back.

    The device imports its debug module on the first of these and drops
    it on `off`, so a long inspection session is worth closing out. Bulky
    answers and tracebacks come back in `logs`; `settle` is how long we
    wait for them after the ack.

    That first call is also said out loud, so the room knows the device
    is being poked at rather than working. `announce=False` skips it —
    the announcement costs a VOICEVOX round trip and a second of
    playback, which is a long time to add to a tight measurement loop.
    """
    if op not in DEBUG_OPS:
        return {"ok": False, "error": f"unknown op {op!r}; expected one of {', '.join(DEBUG_OPS)}"}
    with _device() as link:
        try:
            ack = debug(link, op, src=src, timeout=timeout)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        # Inside the lock: the announcement is another exchange with the
        # device, and letting the chatter in between would cross the acks.
        announced = announce and announce_debug_entry(link, ack)
        if settle:
            time.sleep(settle)
        _msgs, logs = link.events()
    return {"ok": True, "ack": ack, "announced": announced, "logs": _decode_logs(logs)}


@server.tool()
def buddy_interrupt(settle: float = 1.0) -> dict[str, Any]:
    """Ctrl-C the running app back to the REPL. Does not reboot.

    The app tears its transport down and stops at a live prompt with the
    screen reading "REPL"; the port stays open and this link keeps
    reading it. Use it before `buddy_start_app`, or before handing the
    port to `buddy_deploy.py` — though that one needs `buddy_disconnect`
    as well, since only one process can hold the port.

    Refuses to open a port of its own: with nothing connected there is no
    app to interrupt, and claiming the port to find that out would lock
    out the tool that does need it.
    """
    with _device_lock:
        link = _live_link()
        if link is None:
            return {"ok": False, "error": "not connected; nothing to interrupt"}
        link.interrupt()
        if settle:
            time.sleep(settle)
        _msgs, logs = link.events()
    return {"ok": True, "logs": _decode_logs(logs)}


# ----- chatter
#
# The device muttering to itself while Claude works. Driven by Claude
# Code's hooks over a datagram socket, not by tool calls — the whole
# point is that it costs the work nothing. See `buddy_chatter`.


@server.tool()
def buddy_chatter_start(
    gap_min: float = -1.0,
    gap_max: float = -1.0,
    voice_every: int = -1,
    busy_rate: float = -1.0,
    model: str = "",
    effort: str = "",
    batch: int = -1,
) -> dict[str, Any]:
    """Start the idle chatter, optionally retuning how often it talks.

    Nothing is said until a link is up (`buddy_start_app` or
    `buddy_connect`); the chatter never opens the port itself, so that
    `buddy_deploy.py` and `esptool` can still have it. With
    `BUDDY_CONNECT_ON_START=1` the server has already made one such
    opening on its own behalf as it started, which is why a session
    normally arrives here with a link already up.

    Each interval is drawn fresh from `gap_min`..`gap_max` seconds rather
    than being fixed, because a metronome is what makes this annoying.
    Where in that range it is drawn follows how busy the session is:
    `busy_rate` is the hook events per minute that count as fully busy
    and put the gap at the short end of the range. Raise it to make the
    device harder to excite. `voice_every` speaks aloud on every Nth utterance and shows
    the rest on the panel only — raise it when the room has other people
    in it.

    `model` and `effort` are what writes the lines, when Claude Code is
    the one connected: a model alias or id (`sonnet`, `haiku`,
    `claude-opus-5`) and one of `low`/`medium`/`high`/`xhigh`/`max`.
    Turn them up when the muttering has gone flat and down when it is
    costing more than it is worth. `batch` is how many lines one
    generation produces — a larger batch is cheaper per line and lags
    the session further, since later lines were written from what was
    happening when the batch was filled.

    Any numeric argument left at -1, and any string left empty, keeps
    its current value. Passing one while the chatter is already running
    restarts it with the new setting.
    """
    global _chatter
    service = _chatter_service()
    # Two sentinels, because the settings are of two kinds. -1 for the
    # numbers, since every one of them is a count or a duration and
    # negatives are meaningless; empty for the strings, since "" is
    # already what `effort` means by "leave the CLI's default alone".
    overrides: dict[str, Any] = {
        name: value
        for name, value in (
            ("gap_min", gap_min),
            ("gap_max", gap_max),
            ("voice_every", voice_every),
            ("busy_rate", busy_rate),
            ("batch", batch),
        )
        if value >= 0
    }
    overrides.update(
        {name: value for name, value in (("model", model), ("effort", effort)) if value}
    )
    if overrides:
        cfg = replace(service.cfg, **overrides)
        service.stop()
        _chatter = service = ChatterService(cfg, _live_link, _device_lock)
    service.start()
    return service.status()


@server.tool()
def buddy_chatter_stop() -> dict[str, Any]:
    """Stop the idle chatter and release its socket."""
    service = _chatter_service()
    service.stop()
    return service.status()


@server.tool()
def buddy_chatter_status() -> dict[str, Any]:
    """Report what the chatter has been doing, and why it has not.

    `skipped_offline` counts turns where no link was up, `skipped_busy`
    counts turns where a real tool call held the device — both are
    normal. `generation_failures` with a `generation_error` means it has
    fallen back to canned lines: usually the agent's CLI missing from
    the server's PATH, or not logged in.

    `backend`, `model` and `effort` say who is writing the lines.

    `connect_on_start` appears when the server was asked to open the
    port for itself (`BUDDY_CONNECT_ON_START=1`) and says how that one
    attempt went. Absent means it was never asked.
    """
    status = _chatter_service().status()
    if _startup_connect is not None:
        status["connect_on_start"] = _startup_connect
    return status


# ----- logging
#
# The daemon writes to a file nobody is watching, appended to across
# restarts. Two things follow. Every line needs a timestamp, or a line
# cannot be attributed to a run — and "when did it stop" is the first
# question ever asked of this file. And each run has to say what it is,
# because `Started server process [19259]` on its own does not say which
# device or which transport.

LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

log = logging.getLogger("buddy.mcp")


def configure_logging() -> None:
    """Put a timestamp on every line, uvicorn's included.

    uvicorn's own default is `INFO:     message` with no date, and
    `run_streamable_http_async` gives no way to pass a `log_config`
    through — so the shipped config dict is edited in place before the
    server is built. Idempotent, because prefixing twice is worse than
    not prefixing at all.

    `basicConfig` covers everything that is not uvicorn: the MCP SDK
    logs session lifecycle at INFO through the root logger, and those
    lines want the same treatment. `force=True` because `basicConfig` is
    a no-op once the root logger has any handler at all, and by the time
    this runs something usually has installed one — which is how the
    first attempt shipped with uvicorn's lines timestamped and every
    other line bare.
    """
    from uvicorn.config import LOGGING_CONFIG

    logging.basicConfig(format=LOG_FORMAT, datefmt=LOG_DATEFMT, level=logging.INFO, force=True)
    formatters = cast("dict[str, dict[str, Any]]", LOGGING_CONFIG["formatters"])
    for spec in formatters.values():
        fmt = str(spec["fmt"])
        if "%(asctime)s" not in fmt:
            spec["fmt"] = f"%(asctime)s {fmt}"
        spec["datefmt"] = LOG_DATEFMT


# ----- entry point


Transport = Literal["stdio", "streamable-http"]


def transport_options(
    argv: Sequence[str], env: Mapping[str, str]
) -> tuple[Transport, dict[str, Any]]:
    """Which transport to listen on, and how.

    Split out from `main` so it can be read without starting anything.

    `stdio` stays the default: an agent that spawns this process owns
    it, and there is one client by construction. `--http` is what the
    resident daemon asks for, and is the only way more than one session
    can share one serial port.

    Stateless on purpose. Streamable HTTP would otherwise keep a session
    id on the server, and restarting the daemon — the thing this whole
    arrangement exists to make cheap — would 404 every client that was
    already connected.
    """
    parser = argparse.ArgumentParser(prog="buddy-mcp", description=__doc__)
    parser.add_argument(
        "--http", action="store_true", help="listen on streamable HTTP instead of stdio"
    )
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default 8787)")
    args = parser.parse_args(list(argv))
    if not args.http:
        return "stdio", {}
    port = args.port if args.port is not None else http_port(env)
    return "streamable-http", {
        "host": HTTP_HOST,
        "port": port,
        "stateless_http": True,
    }


def serve_http(options: Mapping[str, Any]) -> None:
    """Serve over streamable HTTP, with a bounded graceful shutdown.

    Assembled here rather than through `server.run("streamable-http")`
    because that path builds its own `uvicorn.Config` from host, port
    and log level alone — `timeout_graceful_shutdown` cannot be reached
    through it, and without it a stop can hang indefinitely on a single
    in-flight request. See `SHUTDOWN_TIMEOUT`.
    """
    import uvicorn

    app = server.streamable_http_app(
        streamable_http_path=HTTP_PATH,
        stateless_http=bool(options["stateless_http"]),
        host=str(options["host"]),
    )
    config = uvicorn.Config(
        app,
        host=str(options["host"]),
        port=int(options["port"]),
        log_level=server.settings.log_level.lower(),
        timeout_graceful_shutdown=SHUTDOWN_TIMEOUT,
    )
    uvicorn.Server(config).run()


def http_port(env: Mapping[str, str]) -> int:
    """The configured port, or the agreed default.

    Unparseable falls back rather than raising: the registration that
    reaches this server is a static URL on the default port, so a daemon
    that refused to start over a typo would take the whole thing down
    for a setting nobody meant to change.
    """
    try:
        return int(env.get("BUDDY_HTTP_PORT", ""))
    except ValueError:
        return DEFAULT_HTTP_PORT


def main(argv: Sequence[str] | None = None) -> int:
    """Run the server. The console script `buddy-mcp` lands here."""
    env = buddy_paths.environment()
    transport, options = transport_options(sys.argv[1:] if argv is None else argv, env)
    configure_logging()
    # The header of this run in the appended-to log: which transport,
    # where it listens, and which device it will reach for.
    where = f" on {HTTP_HOST}:{options['port']}" if transport != "stdio" else ""
    log.info("starting: transport=%s%s device=%s", transport, where, DEFAULT_PORT)
    # Started here rather than at import: importing this module must not
    # bind a socket or spawn threads, or the tests (and any tooling that
    # merely inspects the server) would race a live one.
    _chatter_service().start()
    if _connect_on_start_wanted(env, default=transport == "streamable-http"):
        # On a thread: opening the port is a reader plus a handshake,
        # and the agent's `initialize` should not wait behind it.
        threading.Thread(target=_connect_on_start, name="buddy-connect", daemon=True).start()
    install_shutdown_handlers()
    try:
        if transport == "stdio":
            server.run("stdio")
        else:
            serve_http(options)
    finally:
        _shutdown()
        log.info("stopped")
    return 0


def _shutdown() -> None:
    """Let go of the socket and the port. Never raises, safe to repeat.

    Without this a stop left the datagram socket behind, which made
    `buddy-mcpd stop` indistinguishable from a daemon that was killed.

    The port matters more than the socket: the next thing to want it is
    usually `buddy_deploy.py`, and `buddy-mcpd stop` exists precisely to
    hand it over.
    """
    if _chatter is not None:
        with contextlib.suppress(Exception):
            _chatter.stop()
    with contextlib.suppress(Exception):
        buddy_disconnect()


def shutdown_on_signal(signum: int, _frame: object = None) -> None:
    """Clean up, then die the way the sender asked.

    Restoring the default and re-raising rather than exiting: the exit
    status a supervisor sees should say "terminated by SIGTERM", not
    "returned 0". `_shutdown` is safe to run twice, which matters
    because `main`'s `finally` may also reach it.
    """
    _shutdown()
    signal.signal(signum, signal.SIG_DFL)
    signal.raise_signal(signum)


def install_shutdown_handlers() -> None:
    """Own SIGTERM and SIGINT before uvicorn borrows them.

    uvicorn captures both for the length of `serve()`, and on the way
    out restores whatever was installed beforehand and re-raises what it
    caught — so that the process exits the way the sender asked. The
    default action for SIGTERM is to die on the spot, which is why
    `main`'s `finally` never ran and a cleanly stopped daemon still left
    its socket behind. Installing these first means the handler uvicorn
    restores is this one.

    The lifespan shutdown is not a substitute: uvicorn skips it entirely
    when `force_exit` is set, which is exactly the case
    `buddy-mcpd stop` reaches when a session is still attached.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown_on_signal)


if __name__ == "__main__":
    raise SystemExit(main())
