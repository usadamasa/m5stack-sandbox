"""Type stub for the firmware-provided ``esp32`` module.

Built into the MicroPython runtime, never shipped from this repository.
``buddy_debug.py`` is the only caller.
"""

HEAP_DATA: int

def idf_heap_info(capability: int) -> list[tuple[int, int, int, int]]: ...
