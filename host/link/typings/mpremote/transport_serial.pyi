"""Type stub for `mpremote.transport_serial`.

由来と方針は隣の `transport.pyi` にある。こちらは raw REPL を喋る実装の側で、
`device_repl.py` の `_open_transport` が組み立てるのがこのクラス。
"""

from collections.abc import Callable
from typing import Any

from serial import Serial

from .transport import Transport

class SerialTransport(Transport):
    # 開いている pyserial のポート。上流の `repl` コマンドがコンソールを端末へ
    # 渡すのに掴むので、内部ではなく支えられた継ぎ目。`mount_local` の後だけは
    # 上流が `SerialIntercept` に差し替えるが、このリポジトリは mount しない。
    serial: Serial
    device_name: str
    in_raw_repl: bool
    mounted: bool

    def __init__(
        self,
        device: str,
        baudrate: int = 115200,
        wait: int = 0,
        exclusive: bool = True,
        timeout: float | None = None,
    ) -> None: ...
    def close(self) -> None: ...
    def enter_raw_repl(self, soft_reset: bool = True, timeout_overall: int = 10) -> None: ...
    def exit_raw_repl(self) -> None: ...
    def exec_raw_no_follow(self, command: str | bytes) -> None: ...
    def exec(self, command: str, data_consumer: Callable[[bytes], None] | None = None) -> bytes: ...
    # CPython の組み込みではない。上流はデバイスの側で `print(repr(<式>))` を
    # 走らせ、返った literal を `ast.literal_eval` に通す。何が来るかは式を
    # 書いた側しか知らないので `Any` が正しい。
    def eval(self, expression: str, parse: bool = True) -> Any: ...  # noqa: ANN401
