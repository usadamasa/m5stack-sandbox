"""常駐する側のリンク。reader スレッドがポートを読み続ける。

デバイスが下で reboot しても生き延びる 1 本で、MCP server がセッション丸ごと
握るのはこちら。1 回分の open / ask / close で済む `BuddyLink` と、アプリの
起動 (`launch_app`) は `buddy_link` にある。依存はこちらから `buddy_link` への
一方向で、逆は無い。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque

import serial

from buddy_link import DEFAULT_READ_TIMEOUT, launch_app
from buddy_wire import (
    LineDemux,
    Message,
    SerialFactory,
    SerialPort,
    decode,
    encode,
)


class ResidentLink:
    """読み取りをバックグラウンドスレッドで回すセッション。

    `BuddyLink` は呼び出し側が待っている間しか読まない。1 発で終わる CLI の
    実行ならそれでよいが、MCP server は個々のツール呼び出しより長く生きるので、
    デバイス発のトラフィック — ハンドシェイクの `hello` と、その後デバイスが
    押してくるもの — をその合間に捕まえておく必要がある。このクラスは server が
    生きている間ずっとポートを持ち、届いたものを溜める。

    書き込みはロックで直列化し、読み取りは reader スレッドの上でしか起きない
    ので、同じファイルディスクリプタの上で両者が競合することは無い。
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        log_history: int = 500,
        serial_factory: SerialFactory | None = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.read_timeout = read_timeout
        # ログは際限なく流れてくる。長く生きる server の上で無制限に伸ばす
        # のではなく、古いものから捨てる。protocol メッセージのほうは全部
        # 持つ — ack を落とすのは正しさのバグになる。
        self._logs: deque[bytes] = deque(maxlen=log_history)
        self._msgs: deque[Message] = deque()
        self._demux = LineDemux()
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._ser: SerialPort | None = None
        self._serial_factory = serial_factory
        self.dropped = False

    # ----- ライフサイクル

    @property
    def connected(self) -> bool:
        return self._ser is not None

    @property
    def _io(self) -> SerialPort:
        if self._ser is None:
            raise RuntimeError("link is not connected; call connect() first")
        return self._ser

    def connect(self, adopt: SerialPort | None = None) -> None:
        """ポートを開いて読み始める。開いているポートを引き取ることもできる。

        起動を拾う口が `adopt`。`launch_app` が REPL のポートを開いたまま返す
        のは、まさに reader が隙間無くその上から読み始められるようにするため。
        """
        if self._ser is not None:
            return
        if adopt is not None:
            ser = adopt
        else:
            factory: SerialFactory = self._serial_factory or serial.Serial
            ser = factory(self.port, self.baud, timeout=self.read_timeout)
        self._ser = ser
        self.dropped = False
        self._stop.clear()
        # ポートは `self` から読ませるのではなくスレッドへ渡す。disconnect()
        # は上限付きの join のあとで属性を空にするので、read() で止まったまま
        # の reader が居ると、起きたときに None を掴むことになる。
        self._reader = threading.Thread(
            target=self._read_loop, args=(ser,), name="buddy-reader", daemon=True
        )
        self._reader.start()

    def disconnect(self) -> None:
        self._stop.set()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(timeout=2.0)
        ser, self._ser = self._ser, None
        if ser is not None:
            # もう居なくなったポートを閉じて失敗するのは、報せるほどのことでは
            # ない。
            with contextlib.suppress(Exception):
                ser.close()

    # ----- reader スレッド

    def _read_loop(self, ser: SerialPort) -> None:
        while not self._stop.is_set():
            try:
                waiting = ser.in_waiting
                data = ser.read(waiting if waiting else 1)
            except OSError:
                # SerialException は OSError の派生なので、閉じたポートと
                # reset したデバイスの ENXIO の両方をこれで拾う。
                with self._cv:
                    self.dropped = True
                    self._cv.notify_all()
                return
            if not data:
                continue
            items = self._demux.feed(data)
            if not items:
                continue
            with self._cv:
                for kind, payload in items:
                    if kind == "protocol":
                        try:
                            self._msgs.append(decode(payload))
                        except ValueError:
                            self._logs.append(b"<undecodable protocol line> " + payload)
                    else:
                        self._logs.append(payload)
                self._cv.notify_all()

    # ----- トラフィック

    def send(self, obj: Message) -> None:
        with self._write_lock:
            self._io.write(encode(obj))
            self._io.flush()

    def interrupt(self) -> None:
        """Ctrl-C をデバイスへ送る。詳細は `BuddyLink.interrupt`。

        他の書き込みと同じく write ロックの下で送るので、誰かが線へ載せている
        途中のフレームに割り込むことは無い。reader スレッドはポートに付いた
        まま: アプリの去り際の出力と、その後ろの REPL バナーが、効いたことを
        教えてくれる。
        """
        with self._write_lock:
            self._io.write(b"\x03")
            self._io.flush()

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message:
        """reader スレッドが一致する ack を上げてくるのを待つ。"""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for i, msg in enumerate(self._msgs):
                    if msg.get("ack") == expect:
                        del self._msgs[i]
                        return msg
                if self.dropped:
                    raise ConnectionError(f"device dropped off USB while waiting for {expect!r}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no {expect!r} ack within {timeout:.1f}s")
                self._cv.wait(remaining)

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        """`obj` を送り、reader が一致する ack を上げてくるのを待つ。"""
        self.send(obj)
        return self.await_ack(expect, timeout)

    def events(self) -> tuple[list[Message], list[bytes]]:
        """前回の呼び出し以降に溜まったものを全部吐き出す。"""
        with self._cv:
            msgs = list(self._msgs)
            logs = list(self._logs)
            self._msgs.clear()
            self._logs.clear()
        return msgs, logs

    def start_app(self, settle: float = 8.0, wait: float = 15.0) -> None:
        """アプリを起動し直し、同じポートを読む状態で戻ってくる。

        ポートは 2 箇所に同時には置けない。import を走らせるには REPL が
        ポートを要るので、先に reader を止め、起動が返してきたポートの上で
        始め直す。ここでは何も drain しない — 起動時の出力は、import に失敗
        したときの traceback も含めて reader が `events()` のために集める。

        走っているアプリはまず interrupt する。誰もボードに触らずに REPL を
        得られるのがこれ。応答しなかったときのために `wait` があり、短くして
        あるのは意図的: BtnRST が押されるのを 3 分待ってブロックするツール
        呼び出しは、押してくれと言って戻るツール呼び出しより悪い。
        """
        if self.connected:
            # best-effort。既に REPL に居るデバイスはこれを無視するし、居なく
            # なったポートはどのみち起動側が報告する。
            with contextlib.suppress(Exception):
                self.interrupt()
                time.sleep(0.5)
        self.disconnect()
        ser = launch_app(self.port, self.baud, self.read_timeout, wait=wait)
        self.connect(adopt=ser)
        time.sleep(settle)
