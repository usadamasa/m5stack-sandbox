"""Bring the radio up on boot, then start the app.

Replaces the bundle's launcher, which was a menu over /flash/apps plus
an early NimBLE bring-up. Neither is wanted here. The only app is
claude_buddy, so the menu is dead weight; NimBLE is worse than dead
weight, because the stack reserves ESP-IDF heap that the serial
transport never touches and the speech path then cannot get a socket.

The WiFi connect has to come first. `claude_buddy` inherits the link
but cannot create one: `connect()` from inside a running app is
accepted and never associates, because by then the largest free region
of ESP-IDF heap is too small to bring a link up. See the "WiFi" section
of the buddy-speak skill. A failed connect is not a reason to stop —
only speech needs the network, and the chat panel over serial does not.

Launching here is what makes power-on enough; nothing on the host has
to run for the device to be usable. It stopped at the REPL instead for
as long as Ctrl-C was disabled, when the only way out of a running app
was a reboot. Now the interrupt lands in the app's main loop, which
tears its transport down and returns rather than resetting, and this
file falls off its end onto a live prompt — so `buddy_deploy` and
`buddy_bridge` still take the port over through the raw-REPL handshake,
without a BtnRST press.

The case to know about is a crash. An exception the app does not handle
ends in its `finally`, which calls machine.reset() — so the board boots,
launches, crashes and boots again. The way out is Ctrl-C during the WiFi
connect above: KeyboardInterrupt is caught nowhere in this file and
lands at the REPL. `connect_repl` polls exactly that way, so the host
tools break the cycle on their own.

This file cannot be precompiled: MicroPython runs /flash/main.py as
source and never looks for main.mpy. Keeping it short is the substitute.
"""

import gc
import sys

import M5
import micropython
import wifi_event

# Reserved before anything can fail. An exception raised in an interrupt
# or a scheduled callback cannot allocate, so without this buffer those
# report as "no memory to create exception" with no traceback at all —
# and callbacks are where this bundle's hardest failures live.
micropython.alloc_emergency_exception_buf(100)

_LCD = M5.Lcd
_W = 240

# Same palette as the launcher this replaces, so the boot screen still
# looks like part of the bundle.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_GRAY_MID = 0x777777
_RED = 0xFF0000


def _centred(text: str, y: int, colour: int) -> None:
    _LCD.setTextColor(colour, _BLACK)
    _LCD.drawString(text, (_W - _LCD.textWidth(text)) // 2, y)


def _report(result):
    # type: (dict[str, object]) -> None
    _LCD.fillScreen(_BLACK)
    _LCD.setTextSize(1)
    if result.get("ok"):
        _centred("Buddy ready", 30, _ORANGE)
        _centred("IP: " + str(result.get("ip", "?")), 55, _CREAM)
        _centred("on " + str(result.get("ssid", "?")), 75, _GRAY_MID)
    else:
        _centred("WiFi: offline", 30, _RED)
        _centred(str(result.get("err", ""))[:30], 55, _GRAY_MID)
    _centred("starting app", 105, _GRAY_MID)


# boot.py has already called this; the launcher called it again anyway
# and guarded the call, which is the behaviour worth copying.
try:
    M5.begin()
except Exception as e:
    print("main: M5.begin() warning:", e)

try:
    status = wifi_event.connect()  # type: dict[str, object]
except Exception as e:
    # wifi_event imports network at call time, so a build without a
    # working network module raises here rather than at import.
    status = {"ok": False, "err": "exception: " + str(e)}

print("main: wifi", status)

try:
    _report(status)
except Exception as e:
    print("main: report failed:", e)

# The same three steps as `buddy_link.LAUNCH_SOURCE`, so that the two
# ways into the app agree. `del sys.modules[...]` is the one line not
# copied: a boot is a fresh interpreter and there is nothing cached to
# drop. `device/tests/test_boot.py` holds the rest of the pair together.
#
# The collect is not a formality. The splash above has just churned the
# heap, and the app is by some way the largest import on the board — the
# fragmentation this bundle is built around (see buddy_deploy.py) is
# worst right here.
#
# `__import__` rather than `import claude_buddy`: the app runs from its
# module body, so the name it would bind is never read, and spelling it
# as a call says that the import *is* the call.
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

gc.collect()
__import__("claude_buddy")
