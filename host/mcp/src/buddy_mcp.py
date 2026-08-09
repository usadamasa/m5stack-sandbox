"""MCP server exposing the Cardputer-Adv over the Buddy serial protocol.

Wraps `buddy_bridge.ResidentLink` so Claude Code can talk to the device
through tool calls instead of shelling out. The link is held open across
calls, which is what makes device-initiated traffic visible to
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
`BUDDY_CHATTER=0` turns the chatter off. Registered via `.mcp.json` at
the repo root.
"""

from __future__ import annotations

import contextlib
import os
import sys
import termios
import threading
from collections.abc import Iterator
from dataclasses import replace

# The server is launched by Claude Code from an arbitrary cwd, so make
# the sibling module importable by absolute path rather than relying on
# the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer

from buddy_bridge import (
    DEFAULT_PACE,
    DEFAULT_RATE,
    ZUNDAMON,
    ResidentLink,
    say,
    speak,
    voicevox_url,
)
from buddy_chatter import ChatterConfig, ChatterService
from device_repl import ReplError

DEFAULT_PORT = os.environ.get("BUDDY_PORT", "/dev/cu.usbmodem101")

server = MCPServer(
    name="buddy",
    version="0.1.0",
    instructions=(
        "Talks to an M5Stack Cardputer-Adv running the Claude Buddy app over "
        "USB serial. Call probe_serial first on a new machine or after a "
        "sandbox settings change; if it reports tcsetattr failure, no other "
        "tool here will work. buddy_start_app is one-way — the device "
        "disables Ctrl-C while its serial transport is up, so returning to "
        "the REPL needs a physical BtnRST press."
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
def _device(port: str | None = None) -> Iterator[ResidentLink]:
    """Take the device for one tool call, opening the port if needed."""
    with _device_lock:
        yield _get_link(port)


def _live_link() -> ResidentLink | None:
    """The link if one is already up, else None. Never opens the port.

    This is what the chatter is given. Handing it `_get_link` instead
    would have it claim the port the moment the server starts, which is
    exactly what `buddy_deploy.py` and `esptool` need it not to do.
    """
    return _link if _link is not None and _link.connected else None


def _chatter_service() -> ChatterService:
    global _chatter
    if _chatter is None:
        _chatter = ChatterService(ChatterConfig.from_env(), _live_link, _device_lock)
    return _chatter


def _decode_logs(logs: list[bytes]) -> list[str]:
    return [line.decode("utf-8", errors="replace") for line in logs]


@server.tool()
def probe_serial(port: str = "") -> dict:
    """Check whether this process may issue the ioctl a serial port needs.

    Opens the device node, reads its termios attributes and writes them
    straight back. The write is the operation Seatbelt gates separately
    from read/write access, so an EPERM here means this process is inside
    the sandbox and the MCP approach is not viable.
    """
    target = port or DEFAULT_PORT
    result: dict = {
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
def buddy_connect(port: str = "") -> dict:
    """Open the serial link and start buffering device output."""
    with _device(port) as link:
        return {"connected": link.connected, "port": link.port}


@server.tool()
def buddy_disconnect() -> dict:
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
def buddy_start_app(settle: float = 8.0, wait: float = 15.0) -> dict:
    """Interrupt to the REPL and launch the Buddy app on the device.

    One-way: the app disables Ctrl-C once its transport is up, so getting
    back to the REPL afterwards requires pressing BtnRST on the device —
    and so does calling this a second time. `wait` bounds how long that
    press is waited for before giving up.

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
def buddy_status(timeout: float = 8.0) -> dict:
    """Ask the device for its status ack (name, owner, battery, heap, stats)."""
    with _device() as link:
        return link.request({"cmd": "status"}, "status", timeout=timeout)


@server.tool()
def buddy_set_name(name: str, timeout: float = 8.0) -> dict:
    """Set the device's display name. Persisted in NVS across reboots."""
    with _device() as link:
        return link.request({"cmd": "name", "name": name}, "name", timeout=timeout)


@server.tool()
def buddy_set_owner(owner: str, timeout: float = 8.0) -> dict:
    """Set the owner string shown on the device. Persisted in NVS."""
    with _device() as link:
        return link.request({"cmd": "owner", "owner": owner}, "owner", timeout=timeout)


@server.tool()
def buddy_say(
    text: str,
    role: str = "claude",
    timeout: float = 8.0,
    pace: float = DEFAULT_PACE,
) -> dict:
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
) -> dict:
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
def buddy_chat_clear(timeout: float = 8.0) -> dict:
    """Wipe the chat panel and hand the screen back to the dashboard."""
    with _device() as link:
        return link.request({"cmd": "chat.clear"}, "chat.clear", timeout=timeout)


@server.tool()
def buddy_chat_info(timeout: float = 8.0) -> dict:
    """Report the chat panel's resolved font, CJK support and geometry."""
    with _device() as link:
        return link.request({"cmd": "chat.info"}, "chat.info", timeout=timeout)


@server.tool()
def buddy_events() -> dict:
    """Drain everything the device has said since the last call.

    Covers both protocol messages the device sent on its own (the `hello`
    it emits on handshake) and plain print() logging from the app.
    """
    with _device() as link:
        msgs, logs = link.events()
        dropped = link.dropped
    return {"messages": msgs, "logs": _decode_logs(logs), "dropped": dropped}


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
) -> dict:
    """Start the idle chatter, optionally retuning how often it talks.

    Nothing is said until a link is up (`buddy_start_app` or
    `buddy_connect`); the chatter never opens the port itself, so that
    `buddy_deploy.py` and `esptool` can still have it.

    Each interval is drawn fresh from `gap_min`..`gap_max` seconds rather
    than being fixed, because a metronome is what makes this annoying.
    `voice_every` speaks aloud on every Nth utterance and shows the rest
    on the panel only — raise it when the room has other people in it.

    Any argument left at -1 keeps its current value. Passing one while
    the chatter is already running restarts it with the new setting.
    """
    global _chatter
    service = _chatter_service()
    overrides = {
        name: value
        for name, value in (
            ("gap_min", gap_min),
            ("gap_max", gap_max),
            ("voice_every", voice_every),
        )
        if value >= 0
    }
    if overrides:
        cfg = replace(service.cfg, **overrides)
        service.stop()
        _chatter = service = ChatterService(cfg, _live_link, _device_lock)
    service.start()
    return service.status()


@server.tool()
def buddy_chatter_stop() -> dict:
    """Stop the idle chatter and release its socket."""
    service = _chatter_service()
    service.stop()
    return service.status()


@server.tool()
def buddy_chatter_status() -> dict:
    """Report what the chatter has been doing, and why it has not.

    `skipped_offline` counts turns where no link was up, `skipped_busy`
    counts turns where a real tool call held the device — both are
    normal. `generation_failures` with a `generation_error` means it has
    fallen back to canned lines: usually absent Vertex credentials.
    """
    return _chatter_service().status()


if __name__ == "__main__":
    # Started here rather than at import: importing this module must not
    # bind a socket or spawn threads, or the tests (and any tooling that
    # merely inspects the server) would race a live one.
    _chatter_service().start()
    server.run("stdio")
