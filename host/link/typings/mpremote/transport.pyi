"""Type stub for `mpremote.transport`.

mpremote (MicroPython 本体が配る remote-control ツール) は `py.typed` を持たず、
stub パッケージも存在しない。ここに置くのは、このリポジトリが実際に触る面だけを
`site-packages/mpremote/transport.py` から写した手書きのスタブ。

`device_repl.py` の `Repl` Protocol が同じ面を宣言し直しているが、スタブが合わせる
相手は Protocol ではなく上流のソースの方 — 引数の名前も既定値も含めて。ずれれば
`_open_transport` の戻り値が `Repl` を満たさなくなり、検査側がそこで言う。
"""

import os
from collections.abc import Callable
from typing import NamedTuple

class TransportError(Exception): ...

class TransportExecError(TransportError):
    status_code: int
    error_output: str

    def __init__(self, status_code: int, error_output: str) -> None: ...

class listdir_result(NamedTuple):
    """`fs_listdir` の 1 行。上流が `namedtuple("dir_result", ...)` で作る。

    クラス名を上流の変数名にそろえてある。`repr` に出るのは `dir_result` の
    方だが、import する側が書くのはこちらの名前。
    """

    name: str
    st_mode: int
    st_ino: int
    st_size: int

class Transport:
    """`fs_*` の置き場。`exec` / `eval` はここには無い。

    上流の `Transport` は `self.exec` の上に `fs_*` を組み立てるだけの抽象で、
    その `exec` を持っているのは `SerialTransport` の方。
    """

    def fs_listdir(self, src: str = "") -> list[listdir_result]: ...
    def fs_stat(self, src: str) -> os.stat_result: ...
    def fs_exists(self, src: str) -> bool: ...
    def fs_isdir(self, src: str) -> bool: ...
    def fs_readfile(
        self,
        src: str,
        chunk_size: int = 256,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bytearray: ...
    def fs_writefile(
        self,
        dest: str,
        data: bytes,
        chunk_size: int = 256,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None: ...
    def fs_mkdir(self, path: str) -> None: ...
    def fs_rmfile(self, path: str) -> None: ...
