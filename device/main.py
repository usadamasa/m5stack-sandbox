"""Bring the radio up on boot, then hand the console back.

Replaces the bundle's launcher, which was a menu over /flash/apps plus
an early NimBLE bring-up. Neither is wanted here. The only app is
claude_buddy and the host starts it over the REPL, so the menu is dead
weight; NimBLE is worse than dead weight, because the stack reserves
ESP-IDF heap that the serial transport never touches and the speech
path then cannot get a socket.

What has to stay is the WiFi connect. `claude_buddy` inherits the link
but cannot create one: `connect()` from inside a running app is
accepted and never associates, because by then the largest free region
of ESP-IDF heap is too small to bring a link up. See the "WiFi" section
of CLAUDE.md.

Deliberately does not launch an app. Ending at the REPL is what lets
`buddy_bridge --start` take over without a BtnRST press.

This file cannot be precompiled: MicroPython runs /flash/main.py as
source and never looks for main.mpy. Keeping it short is the substitute.
"""

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
    _centred("REPL is free", 105, _GRAY_MID)


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
