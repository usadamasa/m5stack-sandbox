"""エミュレータが `buddy.app.run()` へ差し出す基板側。`buddy_emu` から切り出した。

2 種類ある。ファームウェアのモジュール (`M5.Lcd` / `M5.Speaker`) と、flash に
しか無い upstream のピア (`buddy_ui_cp` / `buddy_state` / `buddy_protocol`) の
stand-in。前者は本当に描き、後者は形だけ。何が本物でないかは `buddy_emu` の
docstring にまとめてある。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from PIL import Image, ImageDraw, ImageFont

W, H = 240, 135

# 実機で測った fontHeight() (chat_font.py の表)。name -> (glyph px, height)。
_FACES: dict[str, tuple[int, int]] = {
    "EFontJA24": (24, 27),
    "AlibabaSansJA24": (24, 27),
    "DejaVu12": (12, 16),
    "DejaVu9": (9, 15),
    "vlw": (16, 18),
}

ORANGE = 0xCC785C
CREAM = 0xF0EEE6
GRAY_MID = 0x777777
GRAY_DIM = 0x444444
BLACK = 0x000000


def _rgb(colour: int) -> tuple[int, int, int]:
    return (colour >> 16) & 0xFF, (colour >> 8) & 0xFF, colour & 0xFF


class _Fonts:
    """`M5.Lcd.FONTS`。属性の値は driver と同じく不透明なハンドル (ここでは名前)。"""

    def __init__(self) -> None:
        for name in _FACES:
            setattr(self, name, name)


class Lcd:
    """M5GFX の面のうち、このリポジトリのコードが触るぶんを Pillow に写す。"""

    def __init__(self, font: Path | None) -> None:
        self.image = Image.new("RGB", (W, H))
        self.FONTS = _Fonts()
        self.drawn: list[tuple[str, int, int, int]] = []
        self.frame = 0
        self._font_path = font
        self._fonts: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        self._face = "DejaVu9"
        self._loaded: str | None = None
        self._scale = 1.0
        self._fg = CREAM
        self._bg = BLACK
        self._lock = threading.Lock()

    # -- 書体

    def setFont(self, font: object) -> None:
        self._loaded = None
        self._face = str(font)

    def loadFont(self, _path: object) -> None:
        self._loaded = "vlw"

    def unloadFont(self) -> None:
        self._loaded = None

    def setTextSize(self, size: float) -> None:
        self._scale = size

    def _metrics(self) -> tuple[int, int]:
        px, height = _FACES.get(self._loaded or self._face, _FACES["DejaVu9"])
        return max(1, int(px * self._scale)), int(height * self._scale)

    def _pil_font(self, px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        font = self._fonts.get(px)
        if font is None:
            font = (
                ImageFont.truetype(str(self._font_path), px)
                if self._font_path is not None
                else ImageFont.load_default(px)
            )
            self._fonts[px] = font
        return font

    def fontHeight(self) -> int:
        return self._metrics()[1]

    def textWidth(self, text: str) -> int:
        return int(self._pil_font(self._metrics()[0]).getlength(text))

    # -- 描画

    def setTextColor(self, fg: int, bg: int = BLACK) -> None:
        self._fg, self._bg = fg, bg

    def fillScreen(self, colour: int) -> None:
        self.fillRect(0, 0, W, H, colour)

    def fillRect(self, x: int, y: int, w: int, h: int, colour: int) -> None:
        with self._lock:
            ImageDraw.Draw(self.image).rectangle((x, y, x + w - 1, y + h - 1), fill=_rgb(colour))
            self.frame += 1

    def drawString(self, text: str, x: int, y: int) -> None:
        px, height = self._metrics()
        font = self._pil_font(px)
        width = int(font.getlength(text))
        with self._lock:
            draw = ImageDraw.Draw(self.image)
            draw.rectangle((x, y, x + width - 1, y + height - 1), fill=_rgb(self._bg))
            draw.text((x, y + (height - px) // 2), text, font=font, fill=_rgb(self._fg))
            self.drawn.append((text, x, y, self._fg))
            self.frame += 1

    def snapshot(self) -> Image.Image:
        with self._lock:
            return self.image.copy()


class Speaker:
    """`M5.Speaker`。ブロックを数えるだけで、いつでも空いている。"""

    def __init__(self) -> None:
        self.volume = 64
        self.blocks = 0

    def begin(self) -> bool:
        return True

    def getVolume(self) -> int:
        return self.volume

    def setVolume(self, volume: int) -> None:
        self.volume = volume

    def stop(self) -> None:
        pass

    def isPlaying(self, _channel: int = 0) -> int:
        return 0

    def playRaw(self, *_args: object) -> bool:
        self.blocks += 1
        return True


# ---------------------------------------------------------------- upstream の stand-in


class BuddyState:
    name = "Buddy"
    owner = "emu"

    def __init__(self) -> None:
        self.naps = 0

    def stats(self) -> dict[str, object]:
        return {"naps": self.naps}

    def tick_nap(self) -> None:
        self.naps += 1


class BuddyUI:
    """`buddy_ui_cp.BuddyUI` の見た目だけ。header / footer / hint strip を描く。"""

    def __init__(self, lcd: Lcd) -> None:
        self._lcd = lcd
        self._state = "IDLE"
        lcd.fillScreen(BLACK)
        self._redraw_chrome()

    def _text(self, text: str, x: int, y: int, fg: int, bg: int = BLACK) -> None:
        self._lcd.setTextColor(fg, bg)
        self._lcd.drawString(text, x, y)

    def _redraw_chrome(self) -> None:
        self._lcd.fillRect(0, 0, W, 22, ORANGE)
        self._text("Claude Buddy", 4, 4, BLACK, ORANGE)
        self._text(self._state, W - 4 - self._lcd.textWidth(self._state), 4, BLACK, ORANGE)
        self._lcd.fillRect(0, 22, W, 89, BLACK)
        self.restore_button_hints()

    def update_identity(self, name: str, owner: str) -> None:
        self._text(f"{name} / {owner}", 4, 30, CREAM)

    def update_footer(self, stats: dict[str, object], battery: dict[str, object]) -> None:
        self._lcd.fillRect(0, 96, W, 14, BLACK)
        usb = "USB" if battery.get("usb") else "BAT"
        line = f"naps {stats.get('naps', 0)}  {battery.get('pct', 0)}%  {usb}"
        self._text(line, 4, 96, GRAY_MID)

    def set_connection(self, state: str) -> None:
        self._state = state.upper()
        self._redraw_chrome()

    def update_heartbeat(self, _hb: dict[str, object]) -> None:
        pass

    def restore_button_hints(self) -> None:
        self._lcd.fillRect(0, 111, W, 1, GRAY_DIM)
        self._lcd.fillRect(0, 112, W, H - 112, BLACK)
        self._text("Y:yes  N:no  Q:quit", 4, 116, GRAY_MID)


class _AckSink(Protocol):
    def send_line(self, payload: bytes, /) -> bool: ...


class BuddyProtocol:
    """`buddy_protocol.BuddyProtocol` のうち、ホストが当てにする `status` と hello。"""

    def __init__(
        self,
        state: BuddyState,
        ble: _AckSink,
        battery_reader: Callable[[], dict[str, object]],
        **_kw: object,
    ) -> None:
        self._state = state
        self._ble = ble
        self._battery = battery_reader

    def _send(self, obj: dict[str, object]) -> None:
        _ = self._ble.send_line(json.dumps(obj).encode("utf-8"))

    def send_hello(self) -> None:
        self._send({"evt": "hello", "name": self._state.name, "emu": True})

    def on_line(self, raw: bytes) -> None:
        try:
            parsed: object = json.loads(raw)
        except ValueError:
            return
        if not isinstance(parsed, dict):
            return
        msg = cast(dict[str, object], parsed)
        if msg.get("cmd") != "status":
            print("buddy_protocol(emu): unknown cmd", raw)
            return
        ack: dict[str, object] = {
            "ack": "status",
            "ok": True,
            "name": self._state.name,
            "stats": self._state.stats(),
            "battery": self._battery(),
            "emu": True,
        }
        if "id" in msg:
            ack["id"] = msg["id"]
        self._send(ack)


class NoSerial:
    """繋がらない `BuddySerial`。面は `device_fakes.FakeTransport` と同じ。"""

    advertised_name = "Claude_serial(emu)"
    pairing_supported = False
    encrypted = False
    connected = False

    def __init__(self, **_kw: object) -> None:
        pass

    def send_line(self, _payload: bytes) -> bool:
        return False

    def poll(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def forget_bonds(self) -> None:
        pass

    def deinit(self) -> None:
        pass
