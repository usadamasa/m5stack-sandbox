"""Type stub for the firmware-provided ``M5`` module (M5Unified/M5GFX, via UIFlow 2.0).

``M5`` lives on the device's flash only; nothing under ``device/`` ships it and it is
never imported at runtime by anything outside the firmware itself. This stub exists
purely so basedpyright can give ``M5.Lcd`` / ``M5.Speaker`` concrete types under
strict mode instead of leaving every attribute access on them ``Unknown``. It is not
part of the deployed bundle (see ``tests/test_device_constraints.py``, which only
walks ``*.py`` under ``device/``) and covers only the attributes this repository's
code actually touches.
"""

class _Fonts:
    # Font objects are looked up by name via getattr() in buddy_chat.py — the exact
    # set of names on this class depends on which fonts the installed UIFlow build
    # ships. `object` covers all of them without pretending to enumerate the set.
    def __getattr__(self, name: str) -> object: ...

class _Lcd:
    FONTS: _Fonts

    def fillScreen(self, colour: int) -> None: ...
    def fillRect(self, x: int, y: int, w: int, h: int, colour: int) -> None: ...
    def setTextColor(self, fg: int, bg: int = ...) -> None: ...
    # Fractional scales are accepted and measured back through
    # fontHeight()/textWidth(); buddy_chat.py draws the built-in faces at
    # 0.75 on the strength of that.
    def setTextSize(self, size: float) -> None: ...
    def drawString(self, text: str, x: int, y: int) -> None: ...
    def textWidth(self, text: str) -> int: ...
    def fontHeight(self) -> int: ...
    def setFont(self, font: object) -> None: ...
    # VLW off flash. Reports nothing on failure — a missing path and a
    # truncated file both leave the previous face selected — so callers
    # have to establish for themselves that the file is there.
    def loadFont(self, font: object) -> None: ...
    def unloadFont(self) -> None: ...

class _Speaker:
    def getVolume(self) -> int: ...
    def setVolume(self, volume: int) -> None: ...
    def stop(self) -> None: ...
    def playRaw(
        self,
        data: bytes,
        rate: int,
        stereo: bool,
        repeat: int,
        channel: int,
        stop_current: bool,
    ) -> bool: ...

Lcd: _Lcd
Speaker: _Speaker

def begin() -> None: ...
