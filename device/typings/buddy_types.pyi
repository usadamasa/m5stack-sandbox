"""device/ が注入で受け取る相手の面。実体は無く、型検査だけのモジュール。

`device/` の注釈には組み込みの名前 1 つしか書けない
(`device/tests/test_device_constraints.py`)。MicroPython が注釈をパース
するためで、`typing.Protocol` はそもそもファームウェアに無い。richer な型を
書ける場所は PEP 484 の `# type:` コメントだけ — あれはデバイスの parser が
一度も見ない。

そこで名前をここに置き、使う側は実行されない枝から import する。

    _TYPE_CHECKING = False
    if _TYPE_CHECKING:
        from buddy_types import Lcd  # noqa: F401

    def __init__(
        self,
        lcd,  # type: Lcd
    ) -> None: ...

`_TYPE_CHECKING = False` は基板の上でただの `False` で、import は走らない
(Pillow の `ImageFont.py` も `typing.TYPE_CHECKING` を使わない事情から同じ形を
とっている)。basedpyright はこの枝の symbol を束縛するので、`# type:`
コメントから名前が引ける。`.pyi` は `device/` の `*.py` を歩く制約テストにも
デプロイの束にも入らない。

ここに並ぶのはどれも Protocol で、具象クラスではない。デバイスの上に居る
本物 (`M5.Lcd` / `buddy_ui_cp.BuddyUI` / ファームウェアの socket) と
`device/tests/` の double が、同じ面の両側に立てるようにするため — 継承の
関係は無く、共通の base を置ける先も無い。

宣言するのは `device/` のコードが実際に呼ぶメンバーだけ。ここに書いた面が
そのまま double への要求になるので、広げれば double を重くするだけになる。
"""

from collections.abc import Callable
from typing import Protocol, TypedDict

# ---------------------------------------------------------------- 画面

class Fonts(Protocol):
    """`Lcd.FONTS`。載っている書体の集合はビルドが決めるので、名前は
    getattr で引く。"""

    def __getattr__(self, name: str) -> object: ...

class Lcd(Protocol):
    """M5GFX の driver のうち、チャットパネルが触るぶん。

    名前が camelCase なのは M5GFX がそうだから。引数を位置専用 (`/`) に
    してあるのは、これが C の binding で、呼ぶ側も位置でしか渡さないため。
    そうしないと引数の名前まで Protocol の要求になり、`colour` と `color`
    のような綴りの違いで実装が外れる。

    書体のハンドル (`setFont` / `loadFont` の引数) が `object` なのは、
    内蔵書体では `FONTS` から引いた不透明な値、VLW ではパスの `str` が
    来るため。
    """

    # 読むだけの property。書き換えられる属性にすると型が不変になり、
    # `Fonts` を満たす別の型を持つ実装がこの Protocol から外れる。
    @property
    def FONTS(self) -> Fonts: ...
    def fillRect(self, x: int, y: int, w: int, h: int, colour: int, /) -> None: ...
    def setTextColor(self, fg: int, bg: int, /) -> None: ...
    def setTextSize(self, size: float, /) -> None: ...
    def drawString(self, text: str, x: int, y: int, /) -> None: ...
    def textWidth(self, text: str, /) -> int: ...
    def fontHeight(self) -> int: ...
    def setFont(self, font: object, /) -> None: ...
    def loadFont(self, font: object, /) -> None: ...
    def unloadFont(self) -> None: ...

class Screen(Protocol):
    """畳むときに画面を消して一言書くぶん。`Lcd` とは別にしてある。

    こちらに書体も計測も要らない。同じ driver の別の切り口で、要求を
    足すと `device/tests/` の double が両方を背負うことになる。
    """

    def fillScreen(self, colour: int, /) -> None: ...
    def setTextColor(self, fg: int, bg: int, /) -> None: ...
    def drawString(self, text: str, x: int, y: int, /) -> None: ...

class M5Module(Protocol):
    """ファームウェアの `M5` モジュールのうち、`run()` が畳むのに使うぶん。"""

    @property
    def Lcd(self) -> Screen: ...

class UiModule(Protocol):
    """`buddy_ui_cp` モジュールのうち、畳むときに要る色。"""

    BLACK: int
    GRAY_DIM: int

# ------------------------------------------------------------ スピーカー

class Speaker(Protocol):
    """`M5.Speaker` のうち player が触るぶん。"""

    def getVolume(self) -> int: ...
    def setVolume(self, volume: int, /) -> None: ...
    def stop(self) -> None: ...
    # 引数が位置専用なのは `Lcd` と同じ理由 — C の binding で、呼ぶ側も
    # 位置でしか渡さない。
    def playRaw(
        self,
        data: bytes,
        rate: int,
        stereo: bool,
        repeat: int,
        channel: int,
        stop_current: bool,
        /,
    ) -> bool: ...

# ------------------------------------------------------ バイト列の口

class ByteStream(Protocol):
    """`fetch_speech` が渡してくる読み口。ファームウェアの socket か、
    その前に buffer を挟んだ `_PrefixedStream`、あるいはテストの double。

    `settimeout` はここに書かない。持たない相手があり、呼ぶ側が
    `getattr` で探ってから使う。
    """

    def read(self, n: int) -> bytes | None: ...
    def close(self) -> None: ...

class SocketStream(ByteStream, Protocol):
    """タイムアウトを設定できる読み口。

    `ByteStream` と分けてあるのは、`StreamSource` が受け取る側には
    `settimeout` を持たない相手があるため。こちらは HTTP の response が
    抱えている socket そのもので、`buddy.tts._PrefixedStream` は
    `settimeout` を無条件で転送する。
    """

    def settimeout(self, seconds: float) -> None: ...

class Closeable(Protocol):
    """閉じる口だけ。HTTP の response を手放すのに要る。"""

    def close(self) -> None: ...

# --------------------------------------------------------------- HTTP

class HttpResponse(Protocol):
    """ファームウェアの `requests` が返す response のうち、`buddy.tts` が
    読むぶん。"""

    status_code: int

    # 読むだけの property として宣言する。書き換えられる属性にすると型が
    # 不変になり、`SocketStream` を満たす別の型を持つ実装 —
    # ファームウェアの `requests.Response` も、テストの double も — が
    # この Protocol から外れる。
    @property
    def raw(self) -> SocketStream: ...
    def json(self) -> dict[str, object]: ...
    def close(self) -> None: ...

class HttpClient(Protocol):
    """ファームウェアの `requests` モジュールの面。"""

    def post(
        self,
        url: str,
        data: bytes | None = ...,
        headers: dict[str, str] | None = ...,
    ) -> HttpResponse: ...

class SpeechSource(TypedDict):
    """`buddy.tts.fetch_speech` が返すもの。

    dict のままなのは、これを組み立てるのも読むのもデバイスの上だから —
    クラスを 1 つ増やせばヒープを 1 つ増やす。それでも `dict[str, object]`
    にしないのは、`stream` を受け取る側が `ByteStream` を要求していて、
    `object` からそこへ降りる手 (`typing.cast`) が MicroPython には無いため。
    """

    stream: ByteStream
    bytes: int
    rate: int
    response: Closeable

# `buddy.tts.fetch_speech` の面。`buddy.speak` が注入で受け取る。
Fetch = Callable[[str, str, int, int], SpeechSource]

# ---------------------------------------------------- トランスポート

# `BuddySerial` が受け取る 2 つのコールバック。`Router.on_line` /
# `Router.on_state` がそのまま入る。
LineCallback = Callable[[bytes], None]
StateCallback = Callable[[str], None]

class AckSink(Protocol):
    """ack を 1 行返す先。

    `Transport` と分けてあるのは、player と Router が要るのがこの 1 つ
    だけだから。`False` はセッションが無いという意味で、返しても呼び手は
    どうにもできない — 送る先が居ないのは失敗ではない。
    """

    def send_line(self, payload: bytes, /) -> bool: ...

class Transport(AckSink, Protocol):
    """`buddy.serial.BuddySerial` のうち、アプリが触るぶん。"""

    @property
    def advertised_name(self) -> str: ...
    def poll(self) -> None: ...
    def deinit(self) -> None: ...

# ------------------------------------------------- upstream のピアの面

class Ui(Protocol):
    """`buddy_ui_cp.BuddyUI` のうち、アプリが呼ぶぶん。

    `_redraw_chrome` はここに無い。upstream の private なヘルパで、
    `getattr` で探ってから使う。色の定数もここには無い —
    `buddy_ui_cp` はあれをモジュールの側に持っている (`UiModule`)。
    """

    def update_identity(self, name: str, owner: str) -> None: ...
    def update_footer(self, stats: dict[str, object], battery: dict[str, object]) -> None: ...
    def set_connection(self, state: str) -> None: ...
    def update_heartbeat(self, hb: dict[str, object]) -> None: ...
    def restore_button_hints(self) -> None: ...

class State(Protocol):
    """`buddy_state.BuddyState` のうち、アプリが読むぶん。"""

    name: str
    owner: str

    def stats(self) -> dict[str, object]: ...
    def tick_nap(self) -> None: ...

class Proto(Protocol):
    """`buddy_protocol.BuddyProtocol` のうち、Router が呼ぶぶん。"""

    def on_line(self, raw: bytes) -> None: ...
    def send_hello(self) -> None: ...

# ------------------------------------------------ 本リポジトリ側の面

class Chat(Protocol):
    """`buddy.chat.ChatPanel`。Router と main loop から見た面。"""

    active: bool

    def handle_raw(self, raw: bytes) -> dict[str, object] | None: ...
    def render(self) -> None: ...

class Speech(Protocol):
    """`buddy.speak.SpeechPlayer`。同じく Router と main loop から見た面。"""

    active: bool

    def handle_raw(self, raw: bytes) -> dict[str, object] | None: ...
    def pump(self) -> None: ...
    def stop(self) -> None: ...

class DebugModule(Protocol):
    """`buddy.debug`。`dbg.*` が届いたときだけ import され、`dbg.off` で
    また落ちるので、Router は module オブジェクトを 1 枠だけ持つ。"""

    def bind(self, ns: dict[str, object], /) -> None: ...
    # `Chat` / `Speech` より広いのは、`Router.on_dbg` が受けたものを
    # そのまま渡すため。本物の `buddy.debug.handle_raw` もこの 3 つを取る。
    def handle_raw(self, raw: bytes | bytearray | str, /) -> dict[str, object] | None: ...
