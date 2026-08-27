"""Play speech the device fetched for itself.

There is no synthesis here and there could not be: the ESP32-S3 has
neither the flash for a Japanese voice nor the cycles to run one. The
audio comes off WiFi from a VOICEVOX engine the device calls itself
(`buddy/tts.py`). This module is the part that turns that stream into
sound.

### Protocol

    host -> {"cmd":"speak.say","text":"...","url":"http://host:50021",
             "speaker":3,"rate":16000}
    dev  <- {"ack":"speak.say","ok":true,"bytes":81920,"rate":16000}
    dev  <- {"ack":"speak.end","ok":true,"blocks":40,"stalls":0}

The `speak.say` ack goes out once synthesis is done and playback has
begun, not when the request was accepted — the two POSTs to the engine
block for seconds and the app's loop stops for that time. Everything
after that ack runs from `pump()`, a tick at a time, so the UI comes back.

`speak.end` は最後のブロックが鳴り終わってから (`isPlaying` が 0)。`stalls`
は鳴り始めた後に speaker が空になった回数 — 音が途切れた回数で、socket を
待っただけの tick は数えない。

`speak.stop` abandons whatever is in flight.

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

1 ブロックは 16 kHz で 2048 byte = 64 ms。app のループは 40 ms + 読み取り
(~11 ms) なので枠は tick ごとに空き、`pump()` が読むのは普通 1 ブロック。
最初の tick だけ 2 つ読んで両方の枠を埋める — 頭のクッションはこれしか無い。
レートを上げるとブロックも伸びる (`_block_for`): tick より短いブロックだと
再生が読み取りを追い越す。

socket が bytes をくれる形とブロックの差 (途中までしか来ない、最後が短い、
止まる) は `buddy/speak_stream.py` が吸収する。ここは丸ごとのブロックか
「まだ」しか見ない。

### MicroPython

No `typing`, no `__future__`, no slice deletion, no exception chaining.
The speaker, the transport and the fetch are all injectable so the
sequencing is testable on the host without a board — see
`device/tests/test_speak.py`.
"""

import json

from buddy import speak_stream

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import AckSink, Fetch, Speaker  # noqa: F401

# Refuse anything longer than this in one utterance. 16 kHz 16-bit で
# 30 秒、24 kHz なら 20 秒 — a notification is seconds, and this is far
# less than enough to wedge the device for a noticeable time. `buddy.tts`
# carries the same number for the same reason.
_MAX_BYTES = 960000

_DEFAULT_RATE = 16000
_DEFAULT_SPEAKER = 3

# 16 kHz の 1 ブロック = 64 ms。`_block_for` はこれを基準に伸ばす。
_BLOCK = 2048
_BLOCK_RATE = 16000

# 渡し先のチャンネル。-1 は使わない (module docstring の Timing)。
_CHANNEL = 0
# `isPlaying(ch)` がこの値なら枠が無い。
_QUEUE_FULL = 2
# 渡し済みで持ち続けるブロックの数: 鳴っている 1 つ + 次の 1 つ + 余裕。
_KEEP = 3

# M5.Speaker.playRaw(data, rate, stereo, repeat, channel, stop_current).
_MONO = False
_ONCE = 1

# How much louder than the firmware's own setting the speaker is driven:
# M5Unified starts the Cardputer-Adv's small speaker too quiet to make
# out across a desk. Relative, not a fixed byte, so it keeps saying what
# was wanted when the firmware moves its default. Measured, the default
# is 64 of 255, so this lands on the cap below — as loud as it goes.
_VOLUME_GAIN = 4

# setVolume takes a byte.
_MAX_VOLUME = 255


def _block_for(rate: int) -> int:
    """`rate` で 64 ms 以上になる最小の 2 の冪 (2048 の倍数)。

    16 kHz は 2048、24 kHz は 4096、48 kHz は 8192。
    """
    block = _BLOCK
    while block * _BLOCK_RATE < _BLOCK * rate:
        block *= 2
    return block


def _default_speaker():
    """The real speaker. Imported lazily so the host can import this."""
    import M5

    return M5.Speaker


def _boost_volume(spk):
    # type: (Speaker) -> int | None
    """Turn `spk` up by `_VOLUME_GAIN`. Returns the volume now set.

    Called once, when the player is built — once per boot, which is what
    makes multiplying safe: the firmware resets the volume on every reset,
    and re-importing the app in a live session fails on memory first.

    Never raises: a build without volume control still plays audio, and
    losing the utterance over the setting would be the wrong trade.
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


def _default_fetch():
    """The real fetch. Lazy for the same reason as the speaker."""
    from buddy import tts as buddy_tts

    return buddy_tts.fetch_speech


# ----- ワイヤから来た値の絞り込み
#
# 届いた命令は `dict[str, object]` で値の型を名乗らない。`fetch` は
# str/str/int/int を要る。`typing.cast` が MicroPython に無いので実際に絞る。
# 壊れた値はここか `fetch` が投げ、`_say` の try が ack の err に載せる。


def _str_of(msg, key):
    # type: (dict[str, object], str) -> str
    """本文と URL。str でない値は `str()` に通す (`ChatPanel.say` と同じ扱い)。"""
    value = msg.get(key, "")
    return value if isinstance(value, str) else str(value)


def _int_of(msg, key, default):
    # type: (dict[str, object], str, int) -> int
    """`int(msg[key])`。数にならない値では投げる — 黙って既定へ落とすと、
    ホストは頼んだ rate と違う音を聞くまで間違いに気付けない。"""
    value = msg.get(key, default)
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError("not a number: " + repr(value))


class SpeechPlayer:
    """Fetches one utterance at a time and streams it into the speaker."""

    # `transport` / `speaker` / `fetch` は注入で受け取る相手 (本物か double)。
    # 面は `.pyi` 側の Protocol で押さえ、名前は `# type:` コメントから引く
    # (注釈は組み込みの名前 1 つに限る — `test_device_constraints.py`)。
    def __init__(
        self,
        transport,  # type: AckSink
        speaker=None,  # type: Speaker | None
        fetch=None,  # type: Fetch | None
    ) -> None:
        self._t = transport
        self._spk = speaker if speaker is not None else _default_speaker()
        self._fetch = fetch
        self.volume = _boost_volume(self._spk)

        self.active = False
        self.text = ""
        self._rate = _DEFAULT_RATE  # type: int
        self._block = _BLOCK  # type: int
        self._source = None
        self._blocks_done = 0  # type: int
        # 読んだが speaker がまだ受け取っていないブロック (0 か 1 つ)。
        self._held = []  # type: list[bytes]
        # 渡し済みで、まだ鳴っているかもしれないブロック。GC 対策 (Timing)。
        self._recent = []  # type: list[bytes]
        self._stalls = 0  # type: int
        # 直前の tick も speaker が空だった (続く空きを 1 回に数える)。
        self._starved = False
        # 読み切った (True) / 切れた (False) / 読んでいる最中 (None)。
        self._done = None  # type: bool | None

    # ----- command dispatch

    def handle_raw(self, raw):
        # type: (bytes | bytearray | str) -> dict[str, object] | None
        """Parse one wire line and dispatch it. None if it is not ours."""
        try:
            msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(msg, dict):
            return None
        # json.loads() は untyped で、isinstance は dict[Unknown, Unknown] まで
        # しか絞れない。実行時は上の検査で dict と分かっている。
        return self.handle(msg)  # pyright: ignore[reportUnknownArgumentType]

    def handle(self, msg):
        # type: (dict[str, object]) -> dict[str, object] | None
        """Dispatch one parsed command. None if the cmd is not ours."""
        cmd = msg.get("cmd")
        if cmd == "speak.say":
            return self._say(msg)
        if cmd == "speak.stop":
            self.stop()
            return {"ack": "speak.stop", "ok": True}
        return None

    def _say(self, msg):
        # type: (dict[str, object]) -> dict[str, object]
        text = _str_of(msg, "text")
        url = _str_of(msg, "url")
        if not url:
            return {"ack": "speak.say", "ok": False, "err": "no engine url"}

        fetch = self._fetch if self._fetch is not None else _default_fetch()

        # Whatever was playing loses; the host asked for something new.
        # Done before the fetch so the speaker is not still working
        # through the tail of the last line while this one synthesises.
        self.stop()

        try:
            got = fetch(
                url,
                text,
                _int_of(msg, "speaker", _DEFAULT_SPEAKER),
                _int_of(msg, "rate", _DEFAULT_RATE),
            )
        except Exception as e:
            return {"ack": "speak.say", "ok": False, "err": str(e)}

        total = got["bytes"]
        if total < 1 or total > _MAX_BYTES:
            try:
                got["response"].close()
            except Exception:
                pass
            return {"ack": "speak.say", "ok": False, "err": "length out of range"}

        self._rate = got["rate"]
        self._block = _block_for(self._rate)
        self._source = speak_stream.StreamSource(got["stream"], total, got["response"])
        self._blocks_done = 0
        self._stalls = 0
        self._starved = False
        self._done = None
        # 前の台詞のブロックは、fetch の往復 (秒) を挟んだここで手放す。
        self._recent = []
        self.text = text
        self.active = True

        return {"ack": "speak.say", "ok": True, "bytes": total, "rate": self._rate}

    def stop(self) -> None:
        """Silence the speaker and abandon any transfer in flight.

        `_recent` は残す: `spk.stop()` の直後も task は数 ms は読んでいる。
        次の `_say` か `_finish` が手放す。
        """
        if self._source is not None:
            self._source.close()
            self._source = None
        self.active = False
        self._held = []
        self._done = None
        self.text = ""
        try:
            self._spk.stop()
        except Exception as e:
            print("buddy.speak: stop failed:", e)

    # ----- main-loop pump

    def pump(self) -> None:
        """Feed the speaker what its queue has room for. Called every tick.

        普通は 1 ブロック、最初の tick だけ 2 つ (module docstring の Timing)。
        """
        source = self._source
        if not self.active or source is None:
            return

        depth = self._depth()
        self._note_gap(source, depth)
        while depth < _QUEUE_FULL and self._done is None and self._hand_one(source):
            depth += 1

        if self._done is not None and not self._held and self._depth() == 0:
            self._finish(self._done)

    def _depth(self) -> int:
        """そのチャンネルの枠の埋まり具合。0 空 / 1 次が空き / 2 満杯。"""
        try:
            return int(self._spk.isPlaying(_CHANNEL))
        except Exception as e:
            # 無いなら 0 を返して渡す。満杯なら playRaw が待つので、音は
            # 出るが tick が止まる。音を落とすよりはそちら。
            print("buddy.speak: isPlaying failed:", e)
            return 0

    def _note_gap(self, source, depth):
        # type: (speak_stream.StreamSource, int) -> None
        """鳴り始めた後に speaker が空だったら 1 回数える。続く tick は同じ 1 回。"""
        pending = bool(self._held) or source.left > 0
        starving = depth == 0 and self._blocks_done > 0 and self._done is None and pending
        if starving and not self._starved:
            self._stalls += 1
        self._starved = starving

    def _hand_one(self, source):
        # type: (speak_stream.StreamSource) -> bool
        """Move one block toward the speaker. True if the speaker took it."""
        if not self._held:
            if source.left <= 0:
                self._done = True
                return False
            block = source.read_block(self._block)
            if block is None:
                if source.dead:
                    self._done = False
                return False
            self._held.append(block)
        block = self._held[0]
        if not self._play(block):
            return False
        # Rebind rather than pop(0): consistency with the bytearray
        # idiom this bundle uses everywhere, since MicroPython has
        # no slice deletion and mixing the two reads badly.
        self._held = self._held[1:]
        # `[*a, b]` は mpy-cross が通さない。連結で書く。
        self._recent = (self._recent + [block])[-_KEEP:]  # noqa: RUF005
        self._blocks_done += 1
        self._starved = False
        return True

    def _play(self, block: bytes) -> bool:
        try:
            return bool(self._spk.playRaw(block, self._rate, _MONO, _ONCE, _CHANNEL, False))
        except Exception as e:
            print("buddy.speak: playRaw failed:", e)
            return True  # drop it rather than wedge the queue

    def _finish(self, ok: bool) -> None:
        blocks = self._blocks_done
        stalls = self._stalls
        self.active = False
        self._held = []
        # `isPlaying` が 0 になるまで待った後なので、もう読まれない。
        self._recent = []
        self._done = None
        if self._source is not None:
            self._source.close()
            self._source = None
        self._t.send_line(
            json.dumps(
                {
                    "ack": "speak.end",
                    "ok": bool(ok),
                    "blocks": blocks,
                    "stalls": stalls,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        )
