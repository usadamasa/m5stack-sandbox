"""Type stub for MicroPython's ``time`` module.

Shadows CPython's real ``time`` (which typeshed already covers) because
MicroPython's build adds ``ticks_ms`` / ``ticks_diff`` / ``sleep_ms`` and this
repository's device code only ever calls those three — never the CPython-side
``time.time()`` / ``time.sleep()`` API, so there is nothing here to reconcile
with typeshed's stub.
"""

def sleep_ms(ms: int) -> None: ...
def ticks_ms() -> int: ...
def ticks_diff(ticks1: int, ticks2: int) -> int: ...
