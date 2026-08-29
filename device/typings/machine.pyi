"""Type stub for the firmware-provided ``machine`` module.

Built into the MicroPython runtime, never shipped from this repository.
``buddy/app.py`` calls ``reset()`` on the way out and reads
``reset_cause()`` once at start-up.
"""

# esp32 port: 1 PWRON / 2 HARD / 3 WDT / 4 DEEPSLEEP / 5 SOFT. ``reset()``
# comes back as 2, not 5 (measured 2026-08-29 on the Cardputer-Adv).
def reset_cause() -> int: ...
def reset() -> None: ...
