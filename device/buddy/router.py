# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 MAS Event + Design, LLC
# Copyright 2026 usadamasa
#
# moremas/build-with-claude からの派生。`apps/claude_buddy.py` の `run()` の
# 中にクロージャとして置かれていた振り分けを、そのままクラスへ移した。
"""トランスポートから届いた 1 行と状態変化の行き先を決める。

ハードウェアには触らない。LCD もトランスポートも呼び出し側から渡される
だけなので、ここは CPython の上でそのまま動かせる (`device/tests/
test_app.py`)。組み立てと main loop は `buddy/app.py` にある。

### chat の振り分け

`buddy_protocol.py` は upstream のまま flash にあり、知らない verb は
"unknown cmd" として log に出す。だから chat の verb は protocol 層が
見る前に `on_line()` で抜き取る。部分文字列で先に弾いているのは、普通の
通信が JSON のパースを 2 回払わないための前処理 — verb の一覧は
`buddy/chat.py` にある。

### LCD には書かない

chat の再描画も状態変化も、ここではフラグを立てるだけで、描くのは
`buddy/app.py` の main loop。LCD への書き込みが 1 箇所に集まっている
ことに footer と chat の重なりが依存している。ack は LCD を触らない
ただの書き込みなので、そちらはコールバックから即座に返す。
"""

import gc
import json
import sys

_CHAT_TAG = b'"chat.'
_SPEAK_TAG = b'"speak.'
# debug の verb にも同じ手を使う。そしてこの前処理が、この機能の常駐コスト
# の全部でもある: `buddy.debug` は dbg.* が届くまで import されず、`dbg.off`
# でまた落ちる。ヒープに置かない理由はあのモジュールの docstring にある。
_DBG_TAG = b'"dbg.'
# net.* の verb は意図して無い。main.py が boot のときに wifi_event.py の
# 資格情報から繋ぎ、アプリはそのリンクを引き継ぐ。ここから `connect()` を
# 呼んでも受け付けられるだけで完了しない — アプリが走り出す頃には ESP-IDF
# ヒープの最大領域が 12 KB ほどしか残っていない — ので、`network` には
# 何も触らない。


class Router:
    """コールバックが呼ぶ振り分け。1 つのアプリに 1 つ。

    `ble` / `speech` / `proto` は生成の後から差さる。トランスポートの
    コンストラクタが `on_line` を先に要求し、speech と protocol はその
    トランスポートを要求するため、生成の時点では名前がまだ無い。この
    トランスポートは構築中にコールバックを呼ばない (入口は `poll()` だけ)
    ので None のガードは念のためだが、rx の経路に置いても安いままなので
    残してある。
    """

    def __init__(self, ui, chat, state, chars):
        # type: (object, object, object, object) -> None
        self.ui = ui
        self.chat = chat
        self.state = state
        self.chars = chars
        self.ble = None  # type: object
        self.speech = None  # type: object
        self.proto = None  # type: object
        # 一度でも dbg.* が来たら `buddy.debug` が入る 1 枠。
        self.dbg = None  # type: object
        # chat の表示を変える命令が来た、という印。描くのは main loop。
        self.chat_dirty = False
        # 状態変化のメールボックス。同じく描くのは main loop。
        self.pending_state = None  # type: str | None

    def _reply(self, ack):
        # type: (dict[str, object]) -> None
        """ack を 1 行にして返す。LCD には触らない。"""
        self.ble.send_line(json.dumps(ack, separators=(",", ":")).encode("utf-8"))  # pyright: ignore[reportUnknownMemberType]

    def on_line(self, raw):
        # type: (bytes) -> None
        # ここに来るのは常に bytes。`buddy.serial._handle_line()` は on_line
        # を呼ぶ前に何もデコードしない — 下の chat / speech / dbg 側の
        # `handle_raw()` が受ける bytes|bytearray|str より狭い。
        if _CHAT_TAG in raw and self.ble is not None:
            ack = self.chat.handle_raw(raw)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if ack is not None:
                self._reply(ack)  # pyright: ignore[reportUnknownArgumentType]
                self.chat_dirty = True
                return
        # speak.say は engine から音声を取り切ってから答えるので、合成の
        # あいだこのループが止まる。もともと同期でなければ困る作りでもある:
        # ack が載せる長さと rate は、engine の応答ヘッダが来るまで分からない。
        if _SPEAK_TAG in raw and self.ble is not None and self.speech is not None:
            ack = self.speech.handle_raw(raw)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if ack is not None:
                self._reply(ack)  # pyright: ignore[reportUnknownArgumentType]
                return
        # 前処理の最後。debug の通信は稀なので、そうでない 2 つの後ろへ置いて
        # 普通の経路の負担を変えないようにする。
        if _DBG_TAG in raw and self.ble is not None:
            dbg_ack = self.on_dbg(raw)
            if dbg_ack is not None:
                self._reply(dbg_ack)
                return
        # `self.proto` は protocol ができるまで None を持てるよう `object`
        # として宣言してあるので、読み戻すと BuddyProtocol という具体型は
        # 落ちる。下の呼び出しを行単位で無視しているのはそのためで、
        # 放っておいて連鎖させるよりこちらを取る。
        if self.proto is not None:
            self.proto.on_line(raw)  # pyright: ignore[reportUnknownMemberType]

    def on_dbg(self, raw):
        # type: (bytes | bytearray | str) -> dict[str, object] | None
        # `self.dbg` は None か `buddy.debug` モジュールかのどちらかを持てる
        # よう `object` として宣言してある。読み戻すと具体型が落ちるので、
        # ここも連鎖させずに行単位で無視する。
        mod = self.dbg
        # モジュールを引き込んだそのフレームだけ True。ホスト側にはこれを
        # 自力で知る手立てが無い — 起ち上がったばかりの CLI プロセスには、
        # 前のプロセスが既に読み込ませたかどうかが分からない — ので、推測
        # させずに遷移を報告し、どう扱うかはホストに決めさせる (今のところ:
        # 声に出して言う)。
        entered = mod is None
        if mod is None:
            try:
                from buddy import debug as buddy_debug
            except ImportError as e:
                # `buddy.debug` がまだ無い頃の bundle が載っている。
                # ImportError をトランスポートのコールバックへ逃がして
                # ループごと落とすのではなく、ack でそう言う。
                return {"ack": "dbg", "ok": False, "err": "buddy.debug not on flash: " + str(e)}
            mod = self.dbg = buddy_debug
            # 式から名前で辿れるべき、生きているオブジェクト。
            mod.bind(
                {
                    "ble": self.ble,
                    "chars": self.chars,
                    "chat": self.chat,
                    "proto": self.proto,
                    "speech": self.speech,
                    "state": self.state,
                    "ui": self.ui,
                }
            )
        ack = mod.handle_raw(raw)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if ack is not None and entered and not ack.get("unload"):  # pyright: ignore[reportUnknownMemberType]
            # 落とすためにモジュールを import せざるを得なかった `dbg.off`
            # は、何かへ入ったわけではないので、入ったとは言わない。
            ack["entered"] = True
        if ack is not None and ack.get("unload"):  # pyright: ignore[reportUnknownMemberType]
            # collect の前に参照を全部落とさないと、ack に載る数字が「その
            # モジュールがまだ居るヒープ」に対して測られる。`mod` が見落とし
            # やすい 1 つで、これはこの関数を抜けるまで他の 2 つより長生き
            # する。
            mod = None
            self.dbg = None
            if "buddy.debug" in sys.modules:
                del sys.modules["buddy.debug"]
            # 参照は sys.modules だけではない。MicroPython は submodule を
            # package の属性としても持っていて、そちらは上のエントリより
            # 長生きする — デバイスの上で測った話で、module オブジェクトへの
            # `delattr` は効き、後の `from buddy import debug` は flash を
            # 読み直す。これが無いとモジュールはヒープに残り、ack の `free`
            # はそれを使用中として数える。
            pkg = sys.modules.get("buddy")
            if pkg is not None:
                try:
                    delattr(pkg, "debug")
                except AttributeError:
                    pass
            gc.collect()
            ack["free"] = gc.mem_free()
        return ack  # pyright: ignore[reportUnknownVariableType]

    def on_state(self, s: str) -> None:
        # BuddySerial には pairing の段が無いので、handshake で出す
        # "connected" がそのまま終点になる。UI が PAIR... のバッジを抜けて
        # protocol が hello を出し始めるよう、"encrypted" へ読み替える。
        effective = s
        if s == "connected":
            effective = "encrypted"
        print("claude_buddy: state", s, "->", effective)
        self.pending_state = effective
        if effective == "encrypted" and self.proto is not None:
            self.proto.send_hello()  # pyright: ignore[reportUnknownMemberType]
