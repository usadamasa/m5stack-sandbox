"""Type stub for ``buddy_protocol``, vendored upstream on the device's flash.

Reused byte-for-byte from the Basic build (see ``apps/claude_buddy.py``'s
docstring); not shipped from this repository. Only the members
``apps/claude_buddy.py`` calls are declared.
"""

from buddy_chars import CharReceiver
from buddy_state import BuddyState
from buddy_ui_cp import BuddyUI

class BuddyProtocol:
    def __init__(
        self,
        state: BuddyState,
        ui: BuddyUI,
        chars: CharReceiver,
        ble: object,
        battery_reader: object,
        permission_pending: dict[str, object] | None = None,
    ) -> None: ...
    def on_line(self, raw: bytes) -> None: ...
    def send_hello(self) -> None: ...
    def unpair_pending(self) -> bool: ...
    def confirm_unpair(self) -> None: ...
    def cancel_unpair(self) -> None: ...
    def send_permission(self, decision: str) -> bool: ...
