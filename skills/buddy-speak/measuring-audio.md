# 出音を測る

ノイズ・速さ・途切れは、人が聞いて確かめなくても、Mac のマイクで録れば数値で測れる。
音の不具合を扱うときは先に録る。

## 録る

`sounddevice` の `sd.rec(n, samplerate=sr, channels=1, dtype="int16")` と `sd.wait()` で録り、
numpy の配列として扱う。スクリプトは `tmp/` に置いて次の形で回す。

```bash
uv run --no-project --with sounddevice --with numpy python tmp/rec.py
```

`uv` は sandbox の外で動くので、マイクの許可 (TCC) もそのまま通る。
デバイスを鳴らす前に数秒の暗騒音を録り、その rms を以降の比較の基準にする。

## 測るもの

| 知りたいこと | やり方 | 正しいときの読み |
| --- | --- | --- |
| 発話の長さ・余計な音 | 録音の包絡 (10 ms ごとの rms) と、VOICEVOX から取った同じ台詞の WAV の包絡を相互相関で揃える | corr 0.9 前後で揃う。WAV の頭 160 ms は無音なので、そこに録音側の音があれば発話とは別のノイズ |
| 再生レート | `dbg.exec` で周期 8 サンプルの矩形波を `playRaw` に渡し、録音を FFT | rate / 8 Hz に峰 (16k で 2000 Hz、24k で 3000 Hz) |
| キューの深さ | 同じ `dbg.exec` で `playRaw` を続けて撃ち、返り値と所要 ms を見る | 枠 2 つが埋まった後の `playRaw` は待つ。枠の仕組みは [SKILL.md](SKILL.md) の「チャンネルの枠」 |

- `dbg.exec` が受けるソースは 192 文字までなので、`from time import*` と alias で削る
  (上限の根拠は [buddy-debug](../buddy-debug/SKILL.md))
- VOICEVOX の WAV は python の `urllib` で `audio_query` → `synthesis` を POST して取る
