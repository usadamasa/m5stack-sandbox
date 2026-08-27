"""Type stub for the firmware-provided ``network`` module.

Built into the MicroPython runtime, never shipped from this repository.
Reached from the dead ``_TRANSPORT == "ble"`` branch in ``apps/claude_buddy.py``
(kept so the diff against upstream stays readable — see that module's docstring)
and from ``buddy/speak_stream.py``, which turns power management off for the
length of an utterance. Nothing here connects or disconnects at run time —
``/flash/main.py`` does that at boot.
"""

STA_IF: int

class WLAN:
    # `config("pm")` の値。ESP32 では順に 0 / 1 / 2 (実測: 既定は 1)。
    PM_NONE: int
    PM_PERFORMANCE: int
    PM_POWERSAVE: int

    def __init__(self, interface_id: int) -> None: ...
    def active(self, is_active: bool | None = ...) -> bool: ...
    def disconnect(self) -> None: ...
    # 読みは位置引数 1 つ (`config("pm")`)、書きはキーワード (`config(pm=0)`)。
    def config(self, *args: str, **kwargs: object) -> object: ...
