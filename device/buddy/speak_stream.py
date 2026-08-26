"""バイト列を、player が欲しがる形のブロックに均す。

`buddy.speak` から切り出した。socket は欲しいぶんの bytes をその場では
くれないので、その差をここで吸収する。依存は `speak` -> `speak_stream` の
一方向で、こちらは player を知らない。
"""

import time

# 短く終わった最後のブロックを埋める無音。
_PAD = b"\x00"

# `read_block` の 1 回が socket を待ってよい時間。tick の半分: 1 回の
# 呼び出しの中で普通の WiFi のゆらぎを乗り切れるだけの長さがあり、かつ
# 予算を使い切った tick でも次の tick が来る前に戻れる短さ。既定のままの
# socket は要求した bytes が揃うまで待つので、ネットワークの気分の長さ
# だけ UI が止まる。
_READ_TIMEOUT_S = 0.02

# ストリームがまったく進まないまま待っていられる時間。止まった USB 転送に
# 対して旧来の bulk transport が持っていた辛抱と同じ。これを過ぎていたら
# AP が居ないか、engine が死んだか、ノートが寝たかで、どれも待って直る
# ものではない。
_STALL_MS = 3000


class StreamSource:
    """バイト列を player 向けの完全なブロックに変える。

    player は `size` ちょうどのブロックを、待たずに欲しがる。socket は
    どちらも寄越さないので、その差をここで吸収する: 途中までの read は
    呼び出しをまたいで貯まり、utterance の最後のブロックは無音で埋められ、
    もう何も出さなくなったストリームは player を空回りさせる代わりに
    `dead` を立てる。

    `left` はストリームがまだ渡していない PCM の byte 数 — 宣言された
    payload を渡し切った時点で 0 になる。
    """

    def __init__(self, stream, total, response=None):
        # type: (object, int, object | None) -> None
        # `stream`/`response` は duck-typed な socket かテストの double —
        # MicroPython には両者の共通点を名指す `typing.Protocol` が無いので、
        # 以下のメンバーアクセスは行ごとに ignore する。
        self._stream = stream
        self._response = response
        self._acc = b""  # type: bytes
        self.left = total
        self.dead = False
        self._last_progress = time.ticks_ms()

        # これが無いと socket は要求したものが揃うまで待ち、UI のループも
        # 一緒に止まる。テストの double には無く、既にバッファになっている
        # ストリームにも無い。
        setter = getattr(stream, "settimeout", None)
        if setter is not None:
            try:
                setter(_READ_TIMEOUT_S)
            except Exception as e:
                print("buddy.speak: settimeout failed:", e)

    def read_block(self, size):
        # type: (int) -> bytes | None
        """`size` byte ちょうどのブロック 1 つ。まだ来ていなければ None。

        短いままでは返さない: `pump()` は結果をそのまま `playRaw` へ渡す
        ので、そこが短いと音になって聞こえるクリックになる。utterance の
        最後のブロックを無音で埋めるのはそれを守るため — 実測の 81920
        byte はたまたま 2048 で割り切れるだけで、この padding は音声が
        socket から来るようになる前はホスト側の仕事だった。

        bytes がまだ来ていないときは None を返す。これはエラーではなく、
        次の tick で試し直す。1 byte も進まないまま `_STALL_MS` を過ぎた
        ときに初めてエラーになり、そこで `dead` が立って `pump()` が
        utterance を not-ok で終わらせる。ストリームが早く終わるのも同じ
        種類の失敗で、扱いも同じ: 長さは Content-Length で先に宣言されて
        いるので、途中で止まったストリームは切られたのであり、隙間を無音
        で埋めるのは、聞き手が最後まで聞いていない utterance を成功として
        報告することになる。
        """
        if self.left <= 0:
            return None

        stream = self._stream
        if stream is None:
            # utterance の途中で close() が走った。もう何も来ないし、
            # left > 0 は早く終わったということ。
            self.dead = True
            return None

        # 最後のブロックは定義から短い。まだ渡されていないぶんだけを要求
        # してから埋める — まるごと 1 ブロック要求すると、サーバーが次に
        # 送るものまで読むか、来ない bytes を待って固まる。
        want = size if size < self.left else self.left

        had = len(self._acc)
        ended = self._fill(stream, want)

        if len(self._acc) > had:  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            self._last_progress = time.ticks_ms()

        if len(self._acc) >= want:  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            return self._take(want, size)

        self._give_up(ended)
        return None

    def _fill(self, stream, want):
        # type: (object, int) -> bool
        """`want` byte 貯まるまで読む。ストリームが終わっていたら True。

        `want` に届かないまま止まるのは失敗ではなく普通の場合で、呼び手は
        次の tick でまた来る。
        """
        while len(self._acc) < want:  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            try:
                # `stream` は duck-typed (__init__ を見よ) なので `.read()` の
                # 結果はどうしても Unknown になり、それを下で `_acc` へ畳み
                # 込むと、このメソッドの残り全部の `_acc` の読み出しが汚染
                # される — 波及させずに行ごとに ignore する。
                chunk = stream.read(want - len(self._acc))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportUnknownArgumentType, reportAttributeAccessIssue]
            except OSError:
                # タイムアウトしたか、ブロックするところだった。この層から
                # は遅い AP と見分けが付かず、どちらも答えは同じ: 次の tick
                # で来い。本物の接続エラーもここでは同じに見えて、stall の
                # 期限が捕まえる。
                chunk = None
            if chunk is None:
                return False
            if not chunk:
                return True
            self._acc += chunk  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportUnknownArgumentType]
        return False

    def _take(self, want, size):
        # type: (int, int) -> bytes
        """ブロックを 1 つ渡す。最後の 1 つなら無音で埋める。"""
        block = self._acc[:want]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        # スライス削除ではなく再束縛: MicroPython の bytes は immutable で、
        # bytearray にも `del b[:n]` が無い。
        self._acc = self._acc[want:]  # pyright: ignore[reportUnknownMemberType]
        # padding ではなく本物の bytes で数える — `pump()` に utterance の
        # 終わりを伝えているのはこれ。
        self.left -= want
        if len(block) < size:  # pyright: ignore[reportUnknownArgumentType]
            block = block + _PAD * (size - len(block))  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return block  # pyright: ignore[reportUnknownVariableType]

    def _give_up(self, ended):
        # type: (bool) -> None
        """来なかったブロックを、もう失敗と見なすかどうかを決める。"""
        if ended:
            print("buddy.speak: stream ended", self.left, "bytes short")
            self.dead = True
        elif time.ticks_diff(time.ticks_ms(), self._last_progress) > _STALL_MS:
            print("buddy.speak: stream stalled with", self.left, "bytes left")
            self.dead = True

    def close(self) -> None:
        """socket を手放す。2 回呼んでも安全。"""
        self.left = 0
        self._acc = b""
        for obj in (self._stream, self._response):
            if obj is None:
                continue
            try:
                obj.close()  # pyright: ignore[reportUnknownMemberType]
            except Exception as e:
                print("buddy.speak: close failed:", e)
        self._stream = None
        self._response = None
