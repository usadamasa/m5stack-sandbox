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
`mcp_state.device_lock` for the whole of its exchange with the device,
and the chatter only ever takes that lock when it is already free.

### Configuration

`BUDDY_PORT` selects the device (default `/dev/cu.usbmodem101`).
`BUDDY_CHATTER=0` turns the chatter off. `BUDDY_CONNECT_ON_START=1` has
the server open the port once as it starts, so that the muttering runs
from the beginning of a session rather than from the first time somebody
calls `buddy_connect`. Registered via `.mcp.json` — see `README.md`.

### 責務

このモジュールはデバイスを触る tool だけを持つ。`server` とモジュール状態は
`mcp_state`、debug と chatter の tool は `mcp_debug_tools` /
`mcp_chatter_tools`、起動口は `buddy_mcp_serve` にある。
"""

from __future__ import annotations

import os
import sys
import termios
from typing import Any

# The server is launched by the agent from an arbitrary cwd, so make the
# sibling module importable by absolute path rather than relying on the
# working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_state
from buddy_text import DEFAULT_PACE
from buddy_verbs import DEFAULT_RATE, ZUNDAMON, say, speak, voicevox_url
from device_repl import ReplError
from mcp_state import DEFAULT_PORT, server


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
    with mcp_state.device(port) as link:
        return {"connected": link.connected, "port": link.port}


@server.tool()
def buddy_disconnect() -> dict[str, Any]:
    """Release the serial port so other tools (push.py, esptool) can use it."""
    # Under the lock: closing the port while the chatter is mid-utterance
    # would surface as an ENXIO on a write it is in the middle of.
    with mcp_state.device_lock:
        if mcp_state.link is None:
            return {"connected": False, "note": "was not connected"}
        mcp_state.link.disconnect()
        mcp_state.link = None
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
    with mcp_state.device() as link:
        try:
            link.start_app(settle=settle, wait=wait)
        except ReplError as e:
            return {"started": False, "error": str(e)}
        msgs, logs = link.events()
    return {"started": True, "messages": msgs, "logs": mcp_state.decode_logs(logs)}


@server.tool()
def buddy_status(timeout: float = 8.0) -> dict[str, Any]:
    """Ask the device for its status ack (name, owner, battery, heap, stats)."""
    with mcp_state.device() as link:
        return link.request({"cmd": "status"}, "status", timeout=timeout)


@server.tool()
def buddy_set_name(name: str, timeout: float = 8.0) -> dict[str, Any]:
    """Set the device's display name. Persisted in NVS across reboots."""
    with mcp_state.device() as link:
        return link.request({"cmd": "name", "name": name}, "name", timeout=timeout)


@server.tool()
def buddy_set_owner(owner: str, timeout: float = 8.0) -> dict[str, Any]:
    """Set the owner string shown on the device. Persisted in NVS."""
    with mcp_state.device() as link:
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
    with mcp_state.device() as link:
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
    with mcp_state.device() as link:
        shown = say(link, text, timeout=timeout, pace=0) if show else []
        ack = speak(link, text, url=url, speaker=speaker, rate=rate, timeout=timeout)
    return {"engine": url, "shown": len(shown), "end": ack}


@server.tool()
def buddy_chat_clear(timeout: float = 8.0) -> dict[str, Any]:
    """Wipe the chat panel and hand the screen back to the dashboard."""
    with mcp_state.device() as link:
        return link.request({"cmd": "chat.clear"}, "chat.clear", timeout=timeout)


@server.tool()
def buddy_chat_info(timeout: float = 8.0) -> dict[str, Any]:
    """Report the chat panel's resolved font, CJK support and geometry."""
    with mcp_state.device() as link:
        return link.request({"cmd": "chat.info"}, "chat.info", timeout=timeout)


@server.tool()
def buddy_events() -> dict[str, Any]:
    """Drain everything the device has said since the last call.

    Covers both protocol messages the device sent on its own (the `hello`
    it emits on handshake) and plain print() logging from the app.
    """
    with mcp_state.device() as link:
        msgs, logs = link.events()
        dropped = link.dropped
    return {"messages": msgs, "logs": mcp_state.decode_logs(logs), "dropped": dropped}
