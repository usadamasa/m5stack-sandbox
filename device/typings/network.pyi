"""Type stub for the firmware-provided ``network`` module.

Built into the MicroPython runtime, never shipped from this repository. Only
reached from the dead ``_TRANSPORT == "ble"`` branch in ``apps/claude_buddy.py``
(kept so the diff against upstream stays readable — see that module's docstring),
but basedpyright still type-checks it.
"""

STA_IF: int

class WLAN:
    def __init__(self, interface_id: int) -> None: ...
    def active(self, is_active: bool | None = ...) -> bool: ...
    def disconnect(self) -> None: ...
