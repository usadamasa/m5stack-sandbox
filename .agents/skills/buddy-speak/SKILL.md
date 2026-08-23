---
name: buddy-speak
description: Cardputer-Adv に喋らせる経路 (VOICEVOX ENGINE + M5.Speaker) を扱うときに使う。音が出ない・途中で切れる・UI が固まる、buddy_speak.py / buddy_tts.py を直す、WiFi が繋がらないときに参照する。ストリーミングが必須である理由と、WiFi をブート時に上げる設計もここ。
---

# 音声 (speak)

**デバイスが自分で VOICEVOX を叩く。** ホストからはテキストとエンジンの URL しか渡らない。
声はずんだもん (`speaker=3`)。

```
Claude Code -MCP-> USB line {"cmd":"speak.say","text":...,"url":...}
  -> device/buddy_tts.py -HTTP-> VOICEVOX ENGINE (Mac の Docker)
  -> WAV ストリーム -> device/buddy_speak.py -> M5.Speaker
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
`read()` がデータ待ちで止まり 40ms tick ごと固まる。`_StreamSource` が `settimeout(0.02)`
を掛けている (tick の半分)。

- **ブロックは 2048 バイト固定。** 40ms tick で 1 ブロックずつしか読まないので、これより
  小さいと再生が追いつかない
- **端数ブロックはデバイス側で無音パディングする** (`_StreamSource.read_block`)。
  `playRaw` に短いブロックを渡すとクリックが鳴る
- **進捗が 3 秒止まったら諦める** (`_STALL_MS`)。EOF で足りない場合も同じ扱い。
  `Content-Length` で長さが分かっているので、途中で切れたのは異常
- `speak.end` の `stalls` が 0 以外なら供給が間に合っていない

合成中 (`audio_query` → `synthesis` の 2 回の POST) は **UI が数秒止まる**。`_thread` は
GIL 付きなので逃げ場がない。再生が始まってからは 1 tick 1 ブロックで進むので UI は動く。

## WiFi

**デバイスもホストも、実行時には WiFi を一切扱わない。** `/flash/main.py` がブート時に
`/flash/wifi_event.py` の認証情報で接続し、アプリはその link を継承するだけ。書き込みは
`host/tools/src/provision_wifi.py` が一度だけ行う ([buddy-deploy](../buddy-deploy/SKILL.md))。

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
