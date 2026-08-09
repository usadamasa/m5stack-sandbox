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

### Configuration

`BUDDY_PORT` selects the device (default `/dev/cu.usbmodem101`).
Registered via `.mcp.json` at the repo root.
"""

from __future__ import annotations

import os
import sys
import termios

# The server is launched by Claude Code from an arbitrary cwd, so make
# the sibling module importable by absolute path rather than relying on
# the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer

import buddy_speech
from buddy_bridge import DEFAULT_PACE, ResidentLink, say, speak

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
    link = _get_link(port)
    return {"connected": link.connected, "port": link.port}


@server.tool()
def buddy_disconnect() -> dict:
    """Release the serial port so other tools (push.py, esptool) can use it."""
    global _link
    if _link is None:
        return {"connected": False, "note": "was not connected"}
    _link.disconnect()
    _link = None
    return {"connected": False}


@server.tool()
def buddy_start_app(settle: float = 8.0) -> dict:
    """Interrupt to the REPL and launch the Buddy app on the device.

    One-way: the app disables Ctrl-C once its transport is up, so getting
    back to the REPL afterwards requires pressing BtnRST on the device.
    Returns the startup output, which is where a launch traceback lands.
    """
    link = _get_link()
    link.start_app(settle=settle)
    msgs, logs = link.events()
    return {"messages": msgs, "logs": _decode_logs(logs)}


@server.tool()
def buddy_status(timeout: float = 8.0) -> dict:
    """Ask the device for its status ack (name, owner, battery, heap, stats)."""
    return _get_link().request({"cmd": "status"}, "status", timeout=timeout)


@server.tool()
def buddy_set_name(name: str, timeout: float = 8.0) -> dict:
    """Set the device's display name. Persisted in NVS across reboots."""
    return _get_link().request({"cmd": "name", "name": name}, "name", timeout=timeout)


@server.tool()
def buddy_set_owner(owner: str, timeout: float = 8.0) -> dict:
    """Set the owner string shown on the device. Persisted in NVS."""
    return _get_link().request({"cmd": "owner", "owner": owner}, "owner", timeout=timeout)


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
    acks = say(_get_link(), text, role=role, timeout=timeout, pace=pace)
    return {"parts": len(acks), "acks": acks}


@server.tool()
def buddy_speak(
    text: str,
    voice: str = buddy_speech.DEFAULT_VOICE,
    rate: int = buddy_speech.DEFAULT_RATE,
    show: bool = True,
    timeout: float = 10.0,
) -> dict:
    """Say `text` out loud on the device, and by default show it too.

    Synthesis runs here, not on the device: the Cardputer-Adv is an
    ESP32-S3 and has no Japanese TTS available to it. The audio is
    streamed over the serial link as raw PCM and played through
    M5.Speaker.

    Blocks for about the duration of the audio — the device's speaker
    queue holds a second, so the transfer paces itself against playback.
    `voice` is any macOS voice name (`say -v '?'` lists them; Kyoko is
    the Japanese default).

    Returns the device's `speak.end` ack. A non-zero `stalls` means the
    speaker ran ahead of the link and the audio may have stuttered.
    """
    pcm = buddy_speech.synthesize(text, voice=voice, rate=rate)
    link = _get_link()
    shown = say(link, text, timeout=timeout, pace=0) if show else []
    ack = speak(link, pcm, rate=rate, timeout=timeout)
    return {
        "seconds": round(buddy_speech.duration_s(pcm, rate), 2),
        "shown": len(shown),
        "end": ack,
    }


@server.tool()
def buddy_chat_clear(timeout: float = 8.0) -> dict:
    """Wipe the chat panel and hand the screen back to the dashboard."""
    return _get_link().request({"cmd": "chat.clear"}, "chat.clear", timeout=timeout)


@server.tool()
def buddy_chat_info(timeout: float = 8.0) -> dict:
    """Report the chat panel's resolved font, CJK support and geometry."""
    return _get_link().request({"cmd": "chat.info"}, "chat.info", timeout=timeout)


@server.tool()
def buddy_events() -> dict:
    """Drain everything the device has said since the last call.

    Covers both protocol messages the device sent on its own (the `hello`
    it emits on handshake) and plain print() logging from the app.
    """
    link = _get_link()
    msgs, logs = link.events()
    return {"messages": msgs, "logs": _decode_logs(logs), "dropped": link.dropped}


if __name__ == "__main__":
    server.run("stdio")
