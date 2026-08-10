"""Type stub for ``hardware``, vendored upstream on the device's flash.

Not shipped from this repository. ``apps/claude_buddy.py`` only uses
``MatrixKeyboard``; see its docstring for the exact shape ``get_key()`` returns
across firmware builds.
"""

class MatrixKeyboard:
    def __init__(self) -> None: ...
    def tick(self) -> None: ...
    def get_key(self) -> int | str | bytes | bytearray | None: ...
