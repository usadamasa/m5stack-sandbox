"""受信側 — hook が投げたデータグラムを拾ってキューへ積む。

`ChatterConfig` すら見ず、socket のパスと `chatter_core.parse_event` だけを
知っている。依存は service → inbox / pace / lines → core の一方向。

### 送り主は待たない

hook は接続していないデータグラムソケットへ `sendto` を 1 回して戻る。
connect もハンドシェイクも返事も無く、受け手が居る必要すら無い —
このプロセスが走っていなければ送信は失敗し、それでも hook は 0 で終わる。
ツール呼び出しがこれを待つことは決して無い。だからここは「受けて積む」
以上のことを何もしない: 台詞を組み立てるのも喋るのも worker の側の仕事で、
受信スレッドがそこで詰まると次のデータグラムを取りこぼす。
"""

from __future__ import annotations

import socket
import threading
from contextlib import suppress
from pathlib import Path
from queue import Empty, Full, Queue

from chatter_core import Event, parse_event

# イベントが来ないときに worker が目を覚ます間隔。idle の台詞がどれだけ
# 速やかに落ちるかを縛るだけなので、1 秒で十分。
_TICK = 1.0

# データグラム 1 つは数百バイトの JSON なので、これは目標ではなく余裕。
# 大きすぎる送信はカーネルが切り詰め、その結果パースに失敗する — 出鱈目な
# 送り主に対してはそれが正しい結末になる。
_MAX_DATAGRAM = 8192

# デバイスが忙しい間にツール呼び出しの連打がキューを際限なく育てられない
# ように上限を持つ。捨てる値打ちがあるのは古いイベントの方。
_QUEUE_DEPTH = 64


class Inbox:
    """socket を持ち、受信スレッドを 1 本回し、イベントをキューへ積む。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: Queue[Event] = Queue(maxsize=_QUEUE_DEPTH)
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped = 0

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        """socket を bind して受信を始める。bind に失敗したら何も走らない。"""
        if self._thread is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 殺されたサーバーが置いていったデータグラムソケットはパスを塞ぐ。
        # 誰もそれを listen していないので、消しても壊すものは無い。
        with suppress(FileNotFoundError):
            self._path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.bind(str(self._path))
        self._sock = sock
        self._stop.clear()
        thread = threading.Thread(target=self._receive, name="buddy-chatter-rx", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        """受信を止めて socket を片付ける。一度も start していなくても安全。"""
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            with suppress(OSError):
                sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with suppress(FileNotFoundError, OSError):
            self._path.unlink()

    def get(self, timeout: float = _TICK) -> Event | None:
        """次のイベントを取る。何も来なければ None を返す。"""
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def _receive(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(_MAX_DATAGRAM)
            except TimeoutError:
                continue
            except OSError:
                # stop() に足元で socket を閉じられた。
                return
            ev = parse_event(data)
            if ev is None:
                continue
            try:
                self._queue.put_nowait(ev)
            except Full:
                self.dropped += 1
