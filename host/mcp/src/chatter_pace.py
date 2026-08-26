"""ペーシング — いつ喋るかを決める側。

`ChatterConfig` と `time` / `random` しか見ない。デバイスにも socket にも
触らないので、テストはクロックを変数にして回せる。依存は
service → pace / lines → core の一方向で、ここから喋る側を import する
ことは無い。

### 刻まないこと

固定の間隔はメトロノームに聞こえて数分で耳障りになる。間隔は毎回範囲から
引き直し、idle と見なす沈黙の長さも同じように引き直す。そうするとデバイスは
しばらく黙ってから続けて 2 つ喋る — 実際に部屋に居る人間の形になる。

範囲そのものも固定ではない。既に届いている hook イベントがセッションの
忙しさを語っているので、長いコンパイルの間も編集の連打の間も同じ速さで
喋る相棒は付いて来ていないことになる。そこで間隔を引く窓は設定された範囲の
上を滑る: 忙しいセッションは短い側から、静かなセッションは長い側から引く。
窓は両端でも幅を保ち、それが忙しい側でメトロノームへ潰れるのを止めている。

忙しさは引いた時点ではなく読んだ時点で見る。長い間隔を引いた後に始まった
連打は、進行中の待ちをその場で縮める — 誰の目にも留まるのはこの場合なので、
ここが肝心になる。1 回の発話につき固定されるのはジッターだけで、それが
落ちる場所は動く。
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable

from chatter_core import ChatterConfig

# 忙しさを判断するときにさかのぼる長さ。1 回の遅いツール呼び出しが沈黙と
# 読まれない程度に長く、連打の始まりにデバイスが気付く程度に短く。
ACTIVITY_WINDOW = 120.0

# 間隔の範囲のうち 1 回の抽選が張る幅。残りは忙しさが抽選をスライドさせる
# ぶん: 半分なら忙しいセッションは下半分から、静かなセッションは上半分から
# 引き、どちらも同じ量のジッターを持つ。
TEMPO_WIDTH = 0.5

# 記憶するイベント時刻の上限。暴走した送り主が prune の合間に deque を
# 育てられないようにするためのもので、そもそも忙しさが飽和する数より遥かに
# 大きい。
_ACTIVITY_DEPTH = 256


class Pacer:
    """次の発話がいつ due になるかを持つ。

    `ChatterService` から切り離してあるのは、ここが I/O を一切持たないから。
    クロックと乱数を渡せば、デバイスもスレッドも無しに挙動を確かめられる。
    """

    def __init__(
        self,
        cfg: ChatterConfig,
        rng: random.Random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._rng = rng
        self._clock = clock

        now = clock()
        self._last_event = now
        self._last_utterance = now
        # イベントが届いた時刻。新しいものが後ろ。セッションの忙しさを語る
        # ために読む。`tempo` を参照。
        self._activity: deque[float] = deque(maxlen=_ACTIVITY_DEPTH)
        # 間隔のジッター。抽選窓に対する割合で、1 回の発話につき固定される。
        # 落ちる窓の方は忙しさとともに動くので、間隔そのものは比較する時点で
        # 初めて決まる。
        self._gap_u = self._rng.random()
        self._idle = self._draw(cfg.idle_min, cfg.idle_max)

    def _draw(self, low: float, high: float) -> float:
        """間隔を 1 つ、ジッター付きで引く。設定が壊れていても負にはしない。"""
        low = max(0.0, low)
        high = max(low, high)
        return self._rng.uniform(low, high)

    @property
    def idle_s(self) -> float:
        """今の idle のしきい値。`status()` が報告する。"""
        return self._idle

    def tempo(self) -> float:
        """セッションの忙しさ。0 (無音) から 1 (飽和) まで。

        到着時ではなく読み出し時に prune する: deque を刈っておく必要がある
        のはここだけで、ここでやればイベントの来なかった tick でも答えが今の
        ものになる。

        これを読むスレッドは 2 つある — 毎 tick の worker と、
        `buddy_chatter_status` に答えている方 — ので、2 つの pruner が最後の
        要素を取り合って片方が既に消えているのを見つけることがある。append は
        必ず右側なので、それを捕まえれば足りる。ここでロックを取ると、ツール
        呼び出しが待たされる先が 1 つ増えることになる。
        """
        now = self._clock()
        while self._activity:
            try:
                if now - self._activity[0] <= ACTIVITY_WINDOW:
                    break
                self._activity.popleft()
            except IndexError:
                break
        if not self._activity:
            return 0.0
        rate = self._cfg.busy_rate
        if rate <= 0.0:
            # 割る相手が無い。少しでも活動があれば忙しさを満杯と読むのが
            # 「即座に飽和する」に合う解釈になる。
            return 1.0
        per_minute = len(self._activity) / (ACTIVITY_WINDOW / 60.0)
        return min(1.0, per_minute / rate)

    def gap_now(self) -> float:
        """今この瞬間から見た、今の発話が待つべき長さ。

        連打が進行中の間隔を縮められるように、確かめるたびに計算し直す。
        固定されているのは抽選窓の中のジッターだけ。
        """
        low = max(0.0, self._cfg.gap_min)
        high = max(low, self._cfg.gap_max)
        span = high - low
        if span <= 0.0:
            return low
        width = span * TEMPO_WIDTH
        start = low + (1.0 - self.tempo()) * (span - width)
        return start + self._gap_u * width

    def note_activity(self, now: float) -> None:
        """イベントが 1 つ届いたことを記録する。

        間隔の判定より前に数える: 間隔の中に落ちたイベントはまだ喋る値打ちの
        あることを言っていないが、それでもセッションが忙しいという事実の
        構成要素ではある。
        """
        self._last_event = now
        self._activity.append(now)

    def waiting(self, now: float) -> bool:
        """まだ前の発話からの間隔が明けていないか。"""
        return now - self._last_utterance < self.gap_now()

    def idle_due(self, now: float) -> bool:
        """デバイスが自分から何か言い出すだけ黙っていたか。"""
        return now - self._last_event >= self._idle

    def rearm(self, now: float) -> None:
        """1 回の発話ぶんを使い切り、次の間隔と idle を引き直す。"""
        self._last_utterance = now
        self._last_event = now
        self._gap_u = self._rng.random()
        self._idle = self._draw(self._cfg.idle_min, self._cfg.idle_max)
