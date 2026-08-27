"""Play speech the device fetched for itself.

There is no synthesis here and there could not be: the ESP32-S3 has
neither the flash for a Japanese voice nor the cycles to run one. The
audio comes off WiFi from a VOICEVOX engine the device calls itself
(`buddy/tts.py`). This module is the part that turns that stream into
sound.

### Protocol

    host -> {"cmd":"speak.say","text":"...","url":"http://host:50021",
             "speaker":3,"rate":24000}
    dev  <- {"ack":"speak.say","ok":true,"bytes":122880,"rate":24000}
    dev  <- {"ack":"speak.end","ok":true,"blocks":30,"stalls":0}

The `speak.say` ack goes out once synthesis is done and playback has
begun — the two POSTs to the engine block for seconds and the app's loop
stops for that time. After that ack `pump()` runs a tick at a time.

`speak.end` は最後のブロックが鳴り終わってから (`isPlaying` が 0)。`stalls`
は鳴り始めた後に speaker が空になった回数 — 音が途切れた回数で、socket を
待っただけの tick は数えない。`speak.stop` abandons whatever is in flight.

### 割れているところ

speaker を起こして音量を上げ、チャンネルの空きを見てブロックを渡し、
渡したものを GC から守るのは `buddy/speak_out.py`。ブロック長の根拠と
`playRaw` まわりの実測もあちらの docstring にある。ここに残るのは発話の
ライフサイクルと `speak.*` verb の振り分け。

socket が bytes をくれる形とブロックの差 (途中までしか来ない、最後が短い、
止まる) は `buddy/speak_stream.py` が吸収する。ここは丸ごとのブロックか
「まだ」しか見ない。

MicroPython: no `typing`, no `__future__`, no slice deletion, no exception
chaining. Speaker, transport and fetch are injectable — `device/tests/`.
"""

import json
import time

from buddy import speak_out, speak_stream

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import AckSink, Fetch, Speaker  # noqa: F401

# Refuse anything longer than this in one utterance: 24 kHz 16-bit で
# 20 秒 (16 kHz で 30 秒)。`buddy.tts` carries the same number.
_MAX_BYTES = 960000

# ホスト側の `buddy_verbs.DEFAULT_RATE` と同じ値。理由はそちら。
_DEFAULT_RATE = 24000
_DEFAULT_SPEAKER = 3


def _default_fetch():
    """The real fetch. Imported lazily so the host can import this."""
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
    """`int(msg[key])`。数にならない値では投げる — 黙って既定へ落とすと気付けない。"""
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
        spk = speaker if speaker is not None else speak_out.default_speaker()
        self._out = speak_out.SpeakerOut(spk)
        self._fetch = fetch
        self.volume = self._out.volume

        self.active = False
        self.text = ""
        self._rate = _DEFAULT_RATE  # type: int
        self._block = speak_out.BLOCK  # type: int
        self._source = None
        self._blocks_done = 0  # type: int
        # 読んだが speaker がまだ受け取っていないブロック (0 か 1 つ)。
        self._held = []  # type: list[bytes]
        self._stalls = 0  # type: int
        # 直前の tick も speaker が空だった (続く空きを 1 回に数える)。
        self._starved = False
        self._last_hand = time.ticks_ms()
        # 読み切った (True) / 切れた (False) / 読んでいる最中 (None)。
        self._done = None  # type: bool | None

    # ----- command dispatch

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

        # Whatever was playing loses. Before the fetch, so the tail of the
        # last line is not still sounding while this one synthesises.
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
        self._block = speak_out.block_for(self._rate)
        self._source = speak_stream.StreamSource(got["stream"], total, got["response"])
        self._blocks_done = 0
        self._stalls = 0
        self._starved = False
        self._done = None
        # 前の台詞のブロックは、fetch の往復 (秒) を挟んだここで手放す。
        self._out.release()
        self.text = text
        self.active = True

        return {"ack": "speak.say", "ok": True, "bytes": total, "rate": self._rate}

    def stop(self) -> None:
        """Silence the speaker and abandon any transfer in flight. 渡し済みの
        ブロックは残す (`spk.stop()` の直後も task は数 ms 読む) — 次の `_say`
        が `release()` する。"""
        if self._source is not None:
            self._source.close()
            self._source = None
        self.active = False
        self._held = []
        self._done = None
        self.text = ""
        self._out.stop()

    # ----- main-loop pump

    def pump(self) -> None:
        """Feed the speaker what its queue has room for. Called every tick."""
        source = self._source
        if not self.active or source is None:
            return

        depth = self._out.depth()
        self._note_gap(source, depth)
        while depth < speak_out.QUEUE_FULL and self._done is None and self._hand_one(source):
            depth += 1

        if self._done is not None and not self._held and self._out.depth() == 0:
            self._finish(self._done)

    def _note_gap(self, source, depth):
        # type: (speak_stream.StreamSource, int) -> None
        """鳴り始めた後に speaker が空だったら 1 回数える。続く tick は同じ 1 回。"""
        pending = bool(self._held) or source.left > 0
        starving = depth == 0 and self._blocks_done > 0 and self._done is None and pending
        if starving and not self._starved:
            self._stalls += 1
            # 番号、渡してからの ms、溜まった bytes。ms 長く bytes 少なければ WiFi 待ち。
            idle = time.ticks_diff(time.ticks_ms(), self._last_hand)
            print("buddy.speak: ran dry after block", self._blocks_done, idle, source.buffered)
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
        if not self._out.push(block, self._rate):
            return False
        # Rebind rather than pop(0) / `[*a, b]`: MicroPython has no slice
        # deletion and mpy-cross rejects the star form.
        self._held = self._held[1:]
        self._blocks_done += 1
        self._starved = False
        self._last_hand = time.ticks_ms()
        return True

    def _finish(self, ok: bool) -> None:
        blocks = self._blocks_done
        stalls = self._stalls
        self.active = False
        self._held = []
        # `isPlaying` が 0 になるまで待った後なので、もう読まれない。
        self._out.release()
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
