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

1 行を JSON にするのはこのモジュールの `_decode()` だけで、chat / speech /
debug が受け取るのは解けた後の dict。前置きを含む行は 3 つの verb 層へ
順に回るので、各層が自前で解いていた頃は 1 行に対して同じパースを最大 3 回
払っていた。前処理はこの 1 回のパースを普通の通信へ回さないためにあり、
どちらもここに並んでいるのが読める形になる。

### LCD には書かない

chat の再描画も状態変化も、ここではフラグを立てるだけで、描くのは
`buddy/app.py` の main loop。LCD への書き込みが 1 箇所に集まっている
ことに footer と chat の重なりが依存している。ack は LCD を触らない
ただの書き込みなので、そちらはコールバックから即座に返す。
"""

import gc
import json
import sys

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import (  # noqa: F401
        Chat,
        DebugModule,
        Proto,
        Speech,
        State,
        Transport,
        Ui,
    )

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


def _decode(raw):
    # type: (bytes | bytearray | str) -> dict[str, object] | None
    """Parse one wire line. None if it is not a JSON object.

    Deliberately quiet about malformed input: `buddy_protocol` is the
    layer that owns "bad line" reporting, and anything we reject here
    still falls through to it.
    """
    try:
        msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(msg, dict):
        return None
    # json.loads() is untyped, so isinstance() only narrows `msg` to
    # dict[Unknown, Unknown] rather than the dict[str, object] the verb
    # layers declare. Runtime-safe regardless: the isinstance check above
    # already guarantees this is a dict.
    return msg  # pyright: ignore[reportUnknownVariableType]


def _has_tag(raw):
    # type: (bytes) -> bool
    """独自 verb の前置きを含むか。JSON を解く前に払う唯一の代金。"""
    return _CHAT_TAG in raw or _SPEAK_TAG in raw or _DBG_TAG in raw


def _forget_debug_module():
    # type: () -> None
    """`buddy.debug` への参照を落とす。collect も測定もしない。

    ここで集めないのは、この関数のフレームが載ったままヒープを測ると、
    ack の `free` がそのぶん小さく出るため。呼び出し側 (`Router.on_dbg`)
    が戻ってから `gc.collect()` して数える。
    """
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
        # type: (Ui, Chat, State, object) -> None
        self.ui = ui
        self.chat = chat
        self.state = state
        # `chars` はここでは何も呼ばれない。upstream の `BuddyProtocol` へ
        # 渡すのと `dbg.eval` の名前空間へ載せるだけなので、面を宣言しても
        # double へ要求が増えるだけになる。
        self.chars = chars
        self.ble = None  # type: Transport | None
        self.speech = None  # type: Speech | None
        self.proto = None  # type: Proto | None
        # 一度でも dbg.* が来たら `buddy.debug` が入る 1 枠。
        self.dbg = None  # type: DebugModule | None
        # chat の表示を変える命令が来た、という印。描くのは main loop。
        self.chat_dirty = False
        # 状態変化のメールボックス。同じく描くのは main loop。
        self.pending_state = None  # type: str | None

    def _reply(self, ack):
        # type: (dict[str, object]) -> None
        """ack を 1 行にして返す。LCD には触らない。"""
        if self.ble is None:
            # 呼ぶのは `_intercept` だけで、そちらは `on_line` が
            # `self.ble` を確かめた後にしか走らない。それでも書くのは、
            # 生成の後から差さる枠だという事実がここからは読めないため。
            return
        self.ble.send_line(json.dumps(ack, separators=(",", ":")).encode("utf-8"))

    def _intercept(self, raw):
        # type: (bytes) -> bool
        """protocol 層より先に握れるか試す。握ったら True。

        呼ぶのは `on_line` だけで、`self.ble` があることは向こうが確かめて
        いる — 前処理の 3 つがそろって ack を返す先を要るので、判定は 1 回に
        まとめてある。順序 (chat -> speak -> dbg) と、bytes の部分文字列で
        先に弾く形は元のまま。解くのはここで 1 回だけで、下の 3 つには同じ
        dict が回る。
        """
        if not _has_tag(raw):
            return False
        msg = _decode(raw)
        if msg is None:
            return False
        if _CHAT_TAG in raw:
            ack = self.chat.handle(msg)
            if ack is not None:
                self._reply(ack)
                self.chat_dirty = True
                return True
        # speak.say は engine から音声を取り切ってから答えるので、合成の
        # あいだこのループが止まる。もともと同期でなければ困る作りでもある:
        # ack が載せる長さと rate は、engine の応答ヘッダが来るまで分からない。
        if _SPEAK_TAG in raw and self.speech is not None:
            ack = self.speech.handle(msg)
            if ack is not None:
                self._reply(ack)
                return True
        # 前処理の最後。debug の通信は稀なので、そうでない 2 つの後ろへ置いて
        # 普通の経路の負担を変えないようにする。
        if _DBG_TAG in raw:
            dbg_ack = self.on_dbg(msg)
            if dbg_ack is not None:
                self._reply(dbg_ack)
                return True
        return False

    def on_line(self, raw):
        # type: (bytes) -> None
        # ここに来るのは常に bytes。`buddy.serial._handle_line()` は on_line
        # を呼ぶ前に何もデコードしない — `_decode()` が受ける
        # bytes|bytearray|str より狭い。
        #
        # 1 行ごとに増える呼び出しは `_intercept` の 1 つだけ。ack を返す先が
        # まだ無いあいだは何も握らないので、その 1 つも `ble` があるときしか
        # 出ない。
        if self.ble is not None and self._intercept(raw):
            return
        if self.proto is not None:
            self.proto.on_line(raw)

    def on_dbg(self, msg):
        # type: (dict[str, object]) -> dict[str, object] | None
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
        ack = mod.handle(msg)
        if ack is not None and entered and not ack.get("unload"):
            # 落とすためにモジュールを import せざるを得なかった `dbg.off`
            # は、何かへ入ったわけではないので、入ったとは言わない。
            ack["entered"] = True
        if ack is not None and ack.get("unload"):
            # collect の前に参照を全部落とさないと、ack に載る数字が「その
            # モジュールがまだ居るヒープ」に対して測られる。`mod` が見落とし
            # やすい 1 つで、これはこの関数を抜けるまで他の 2 つより長生き
            # する。
            mod = None
            self.dbg = None
            # 残りの参照 (sys.modules と package の属性) はこちらが落とす。
            # 集めて数えるのは、あの関数のフレームが消えてからのここ。
            _forget_debug_module()
            gc.collect()
            ack["free"] = gc.mem_free()
        return ack

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
            self.proto.send_hello()
