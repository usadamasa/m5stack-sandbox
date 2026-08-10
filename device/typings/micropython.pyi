"""Type stub for the firmware-provided ``micropython`` module.

Never shipped from this repository — it is built into the MicroPython runtime.
Covers only what ``device/`` calls: see ``main.py``, ``buddy_serial.py`` and
``buddy_debug.py``.
"""

def alloc_emergency_exception_buf(size: int) -> None: ...
def kbd_intr(chr: int) -> None: ...
def mem_info(verbose: int = ...) -> None: ...
