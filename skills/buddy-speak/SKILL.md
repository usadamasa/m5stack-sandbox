---
name: buddy-speak
description: Cardputer-Adv に喋らせる経路 (VOICEVOX ENGINE + M5.Speaker) を扱うときに使う。音が出ない・途中で切れる・UI が固まる、buddy/speak.py / buddy/tts.py を直す、WiFi が繋がらないときに参照する。ストリーミングが必須である理由と、WiFi をブート時に上げる設計もここ。
---

# 音声 (speak)

**デバイスが自分で VOICEVOX を叩く。** ホストからはテキストとエンジンの URL しか渡らない。
声はずんだもん (`speaker=3`)。

```
Claude Code -MCP-> USB line {"cmd":"speak.say","text":...,"url":...}
  -> device/buddy/tts.py -HTTP-> VOICEVOX ENGINE (Mac の Docker)
  -> WAV ストリーム -> device/buddy/speak.py -> M5.Speaker
```

エンジンは `docker compose up -d` で立てる。**`-p` は `0.0.0.0` に bind すること。**
VOICEVOX の README の例は `127.0.0.1:50021:50021` だが、それだと Mac の loopback にしか
listen しないのでデバイスから届かない。`voicevox_url()` は loopback アドレスを渡されたら
ホスト側でエラーにする — デバイスまで届けると接続タイムアウトとして数秒後に出るため、
原因から遠い。エンジンの場所は `$VOICEVOX_URL`、未設定ならこのマシンの LAN アドレスを
自動検出する。

on-device TTS は無い。Cardputer-Adv は ESP32-S3 で、M5Stack の on-device TTS
(StackFlow の MeloTTS) は別基板の Module LLM (AX630C, Linux) が要る。Espressif の
`esp-tts` は中国語のみ。

## ストリーミングでしか流せない

PSRAM が無く heap も数十 KB しかないので、WAV 全体をメモリに載せる経路は使えない
(測り方は [buddy-device-limits](../buddy-device-limits/SKILL.md))。`M5.Speaker` の
`playWav` / `playWavFile` も WAV 全体を渡す API なので使えず、`playRaw` にブロックを
送り続けている。

`res.raw` は素のソケットで、**MicroPython のデフォルトはブロッキング**。何もしないと
`read()` がデータ待ちで止まり 40ms tick ごと固まる。`StreamSource` が `settimeout(0.02)`
を掛けている (tick の半分)。

- **`playRaw` のチャンネルは 0 に固定。** `channel=-1` は「空いているチャンネルを探す」で、
  ブロックが別チャンネルで**重なって**鳴る (実測: 128ms のブロック 8 つが 133ms で終わる)。
  以前はこれで発話が 1.4 倍速に潰れていた
- **チャンネルの枠は 2 つ (再生中 + 次)。** 満杯の `playRaw` は False を返さず**待つ**ので、
  渡す前に `isPlaying(0)` (0 空 / 1 次が空き / 2 満杯) を見る。最初の tick は 2 つ、
  以降は普通 1 つ。`isPlaying` は DMA へ渡し切った時点で落ちる (出音より約 45ms 早い)
- **ブロックは 80ms 以上の最小の 2 の冪** (`_block_for`: 16k/24k で 4096、48k で 8192)。
  クッションは枠 2 つぶんしか無いので、tick より短いブロックだと再生が読み取りを追い越す。
  64ms (16k で 2048) は `dbg.eval` を撃つ tick で途切れた
- **渡したブロックの参照は落とさない。** binding はポインタを渡すだけで複製しない。
  落とすと GC がその領域を次の bytes に回し、鳴っている途中で中身が変わる。最後の 3 つを
  `_recent` に持つ
- **端数ブロックはデバイス側で無音パディングする** (`StreamSource.read_block`)。
  `playRaw` に短いブロックを渡すとクリックが鳴る
- **進捗が 3 秒止まったら諦める** (`_STALL_MS`)。EOF で足りない場合も同じ扱い。
  `Content-Length` で長さが分かっているので、途中で切れたのは異常
- `speak.end` は `isPlaying` が 0 になってから。`stalls` は鳴り始めた後に speaker が空に
  なった回数で、0 でなければその回数だけ音が途切れている
- **再起動後の最初の `playRaw` は、無音を渡しても 10ms のポップが鳴る。** M5Unified が
  そこで `begin()` を呼び ES8311 を起こす。`SpeechPlayer` は生成時に `begin()` を呼んで、
  ポップを台詞の頭から引き離す

合成中 (`audio_query` → `synthesis` の 2 回の POST) は **UI が数秒止まる**。`_thread` は
GIL 付きなので逃げ場がない。再生が始まってからは tick ごとに枠が空くぶんしか読まないので
UI は動く。

## サンプリングレート

既定は 24 kHz (`buddy_verbs.DEFAULT_RATE`)。VOICEVOX のネイティブで、エンジン側の
リサンプルが入らない。実測 (2.56 秒の台詞、`dbg.eval` を 120ms おきに撃ちながら):

| rate | block | ack→end / 想定 | stalls | heap の底 |
| --- | --- | --- | --- | --- |
| 16k | 2048 (64ms) | 1.03 | 3 | ~58 KB |
| 24k | 4096 (85ms) | 1.02 | 0 | ~54 KB |
| 48k | 8192 (85ms) | 1.03 | 0 | ~35 KB |

48k も鳴るが、エンジンが upsample するだけで情報は増えず、転送は 2 倍、8 KB の連続確保を
tick ごとに繰り返す (16 KB の確保は既に失敗する heap)。上げる理由が無い。
`--rate` / `BUDDY_CHATTER_RATE` で個別には変えられる。`_MAX_BYTES` (960000) は 24k で
20 秒。

## WiFi

**デバイスもホストも、実行時には WiFi を繋いだり切ったりしない。** `/flash/main.py` が
ブート時に `/flash/wifi_event.py` の認証情報で接続し、アプリはその link を継承するだけ。
書き込みは `host/tools/src/provision_wifi.py` が一度だけ行う
([buddy-deploy](../buddy-deploy/SKILL.md))。

触るのは省電力 (`WLAN.config(pm=...)`) だけ。既定の `PM_PERFORMANCE` だと socket が
300ms 止まることがあり (実測: 9.5 秒の台詞 7 回中 2 回、325ms 待って 1.2KB しか来ない)、
枠 2 つぶんのクッション (170ms) では覆えない。`StreamSource` が生成時に `PM_NONE` にして
`close()` で戻す。切った 6 回は途切れ 0。

なぜアプリ側で繋げないか (実測):

- アプリ稼働中の `connect()` は受理されるが association が完了しない。15 秒後も `status()`
  は "connecting"。ランチャーだけ載った状態で ESP-IDF heap の最大領域が ~12 KB しかなく、
  link を上げるのに DRAM が足りない
- したがって radio はアプリ起動前に上がっている必要がある。**アプリは link を継承できるが、
  作れない**

なぜ NVS ではなく `wifi_event.py` か (実測):

- UIFlow の startup は `uiflow/ssid0` / `uiflow/pswd0` を読む。キーは**存在する**が空文字
- ただし `uiflow/boot_option` が **2** ("user app mode") で、UIFlow のフレームワーク自体を
  迂回して `/flash/main.py` を直接走らせている。**NVS を読む経路が通らない**
- `/flash/wifi_event.py` の docstring 自身が「他所で使うなら SSID / PASSWORD を差し替えろ」
  と案内している。想定された拡張点

`/flash/wifi_event.py` に元から焼かれている `cardputer` / `cardconnect` は M5Stack の
展示会場の AP で、会場で配られる公開パスワード。ファイル冒頭にそう明記されている。
