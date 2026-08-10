"""Type stub for ``buddy_state``, vendored upstream on the device's flash.

Reused byte-for-byte from the Basic build (see ``apps/claude_buddy.py``'s
docstring); not shipped from this repository.
"""

class BuddyState:
    name: str
    owner: str

    def __init__(self) -> None: ...
    def stats(self) -> dict[str, object]: ...
    def tick_nap(self) -> None: ...
