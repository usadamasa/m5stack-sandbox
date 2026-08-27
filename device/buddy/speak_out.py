"""ブロックを speaker へ渡す口。

`buddy/speak.py` から切り出した。M5.Speaker を起こし、音量を上げ、1 本の
チャンネルの空き具合を見てブロックを渡し、渡したものを GC から守る係。
依存は `speak` -> `speak_out` の一方向で、こちらは player も
`speak_stream` も知らない。

### Timing (実測、Cardputer-Adv)

`M5.Speaker` へは `playRaw` でブロックを渡す。渡し先を 1 チャンネルに固定し、
そのチャンネルの枠 (再生中 + 次の 1 つ) が空いたぶんだけ渡す。どちらも
測って決めた:

- `channel=-1` は「空いているチャンネルを探す」。64 ms のブロックを 40 ms
  おきに渡すと別チャンネルで**重なって**鳴る (128 ms のブロック 8 つが
  133 ms で終わった)。1 本に固定して初めて順に鳴る
- 固定チャンネルの `playRaw` は満杯だと**待つ**。False は返らない (同じ 8
  ブロックが 983 ms)。待たされた tick は UI が止まるので、渡す前に
  `isPlaying(ch)` (0 空 / 1 次が空き / 2 満杯) を見る
- `isPlaying` は DMA へ渡し切った時点で落ちる。出音より約 45 ms 早い
- binding は buffer のポインタを渡すだけで複製しない。渡したブロックの参照を
  落とすと GC がその領域を次の bytes に回し、鳴っている途中で中身が変わる。
  渡した最後の `_KEEP` 個は持ち続ける

1 ブロックは 24 kHz で 4096 byte = 85 ms。app のループは 40 ms + 読み取り
(~20 ms) なので枠は tick ごとに空き、`pump()` が読むのは普通 1 ブロック。
最初の tick だけ 2 つ読んで両方の枠を埋める — 頭のクッションはこれしか無い。
ブロックはレートによらず `_MIN_BLOCK_MS` 以上 (`block_for`): tick より短い
ブロックだと再生が読み取りを追い越す。

再起動後の最初の `playRaw` は、無音を渡しても 10 ms のポップが鳴る —
M5Unified がそこで `begin()` を呼び、ES8311 を起こす。player は生成時に
`begin()` を呼んで、そのポップを台詞の頭から引き離す。
"""

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import Speaker  # noqa: F401

# ブロックが覆う時間の下限: 枠は 2 つしか無いので、tick が 1 つ遅れても
# 鳴り続けるには tick 2 つぶん要る。実測: 64 ms (16 kHz で 2048 byte) は
# dbg.eval を撃つ tick で 3 回途切れ、85 ms (24 kHz で 4096) は途切れなかった。
_MIN_BLOCK_MS = 80
_BYTES_PER_SAMPLE = 2

# 渡し先のチャンネル。-1 は使わない (module docstring の Timing)。
_CHANNEL = 0
QUEUE_FULL = 2  # `isPlaying(ch)` がこの値なら枠が無い
_KEEP = 3  # 渡し済みで持ち続けるブロック: 鳴っている 1 つ + 次の 1 つ + 余裕

# playRaw(data, rate, stereo, repeat, channel, stop_current)
_MONO = False
_ONCE = 1

# How much louder than the firmware's own setting the speaker is driven:
# M5Unified starts it too quiet to make out across a desk. Relative, so
# it survives the firmware moving its default (measured: 64 of 255, so
# this lands on the cap below — as loud as it goes).
_VOLUME_GAIN = 4
_MAX_VOLUME = 255  # setVolume takes a byte


def block_for(rate: int) -> int:
    """`rate` で `_MIN_BLOCK_MS` 以上になる最小の 2 の冪。

    16 kHz と 24 kHz は 4096、48 kHz は 8192。
    """
    need = rate * _BYTES_PER_SAMPLE * _MIN_BLOCK_MS // 1000
    block = 1024
    while block < need:
        block *= 2
    return block


# 既定レートのブロック。テストがブロックを数える単位でもある。24 kHz は
# protocol 側の既定 (`speak._DEFAULT_RATE`) と同じ値だが、あちらを import
# すると依存が逆流するのでここに書く。16 kHz でも同じ 4096 になるので、
# protocol の既定が動いてもこの値はまず動かない。
BLOCK = block_for(24000)


def default_speaker():
    """The real speaker. Imported lazily so the host can import this."""
    import M5

    return M5.Speaker


def _wake_speaker(spk):
    # type: (Speaker) -> None
    """`begin()` を先に呼び、ES8311 を起こすポップを台詞から引き離す (Timing)。
    Never raises: 無い build でも最初の `playRaw` が代わりに起こす。"""
    try:
        spk.begin()
    except Exception as e:
        print("buddy.speak: begin failed:", e)


def _boost_volume(spk):
    # type: (Speaker) -> int | None
    """Turn `spk` up by `_VOLUME_GAIN`. Returns the volume now set.

    Once per boot (the firmware resets the volume on every reset), which is
    what makes multiplying safe. Never raises: a build without volume
    control still plays audio, and losing the utterance over the setting
    would be the wrong trade.
    """
    try:
        before = spk.getVolume()
    except Exception as e:
        print("buddy.speak: getVolume failed:", e)
        return None
    after = before * _VOLUME_GAIN
    if after > _MAX_VOLUME:
        after = _MAX_VOLUME
    try:
        spk.setVolume(after)
    except Exception as e:
        print("buddy.speak: setVolume failed:", e)
        return before
    print("buddy.speak: volume", before, "->", after)
    return after


class SpeakerOut:
    """speaker への出口。渡したブロックを、鳴り終わるまで手放さない。

    生成した時点で speaker を起こし、音量を上げる (どちらもブートに 1 回)。
    `volume` は上げた後の値で、上げられなかった build では None。
    """

    def __init__(self, spk):
        # type: (Speaker) -> None
        # `spk` は本物の `M5.Speaker` かテストの double。面は `.pyi` 側の
        # Protocol で押さえる。
        self._spk = spk
        _wake_speaker(spk)
        self.volume = _boost_volume(spk)
        # 渡し済みで、まだ鳴っているかもしれないブロック。GC 対策 (Timing)。
        self._recent = []  # type: list[bytes]

    def depth(self) -> int:
        """そのチャンネルの枠の埋まり具合。0 空 / 1 次が空き / 2 満杯。"""
        try:
            return int(self._spk.isPlaying(_CHANNEL))
        except Exception as e:
            # 無いなら渡す。満杯なら playRaw が待つ — 音を落とすよりはそちら。
            print("buddy.speak: isPlaying failed:", e)
            return 0

    def push(self, block, rate):
        # type: (bytes, int) -> bool
        """Move one block into the speaker. True if the speaker took it.

        受け取られたブロックは `_KEEP` 個まで持ち続ける — 参照を落とすと
        鳴っている途中で中身が変わる (Timing)。
        """
        if not self._play(block, rate):
            return False
        # Rebind rather than pop(0) / `[*a, b]`: MicroPython has no slice
        # deletion and mpy-cross rejects the star form.
        self._recent = (self._recent + [block])[-_KEEP:]  # noqa: RUF005
        return True

    def _play(self, block, rate):
        # type: (bytes, int) -> bool
        try:
            return bool(self._spk.playRaw(block, rate, _MONO, _ONCE, _CHANNEL, False))
        except Exception as e:
            print("buddy.speak: playRaw failed:", e)
            return True  # drop it rather than wedge the queue

    def stop(self) -> None:
        """Silence the speaker. 渡し済みブロックは持ったまま (`spk.stop()` の
        直後も task は数 ms 読む) — 手放すのは `release()` の仕事。"""
        try:
            self._spk.stop()
        except Exception as e:
            print("buddy.speak: stop failed:", e)

    def release(self) -> None:
        """渡し済みブロックの参照を手放す。もう読まれないと分かったときだけ。"""
        self._recent = []
