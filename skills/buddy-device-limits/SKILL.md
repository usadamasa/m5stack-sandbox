---
name: buddy-device-limits
description: Cardputer-Adv の実機の制約と、その測り方。heap がどれだけ残るか、PSRAM の有無、MicroPython ランタイムに何があるか (requests / Response.raw / socket API) を、どのコマンドのどの値で読むか。デバイス側の設計を決めるとき、メモリ不足で落ちたとき、参考記事の API が実機に無いときに参照する。
---

# 実機の制約と、その測り方

デバイス側の設計はこの実機の制約から来ている。ただし **数値そのものはここに書かない**。
必要になった時点で測る。

## 測る

```bash
uv run python host/tools/src/probe_device.py --port /dev/cu.usbmodem101
```

REPL が要る (走っているアプリはハンドシェイクの Ctrl-C で畳まれる)。read-only で、flash に
も状態にも触らない。JSON で出るので、ファームウェアを入れ替えたら前回の出力と diff する。

| 知りたいこと | どこを読むか |
| --- | --- |
| heap の上限 | probe の `heap`。`gc.collect()` した後の `gc.mem_free()` |
| 目当てのバッファを確保できるか | REPL で `bytearray(N)` を実際の N で試す。無理なら `MemoryError` |
| アプリ読み込み時点の空き heap | 起動ログの `claude_buddy: gc done, free=`。ブート起動とホストからの起動で違う値になる |
| 走っているアプリの heap | `--dbg mem` ([buddy-debug](../buddy-debug/SKILL.md))、または `buddy_status` の `sys.heap` |
| HTTP クライアントの名前 | probe の `network.http.module` |
| `Response.raw` があるか | probe の `network.http.response` |
| socket の API | probe の `network.socket` |
| radio の状態 | probe の `network.wlan` |
| フォント・行数・幅 | probe の `display`。読み方は [buddy-device-code](../buddy-device-code/SKILL.md) |

## 測っても変わらないこと

数字と違って、これらは実機とファームウェアの性質そのもの。設計の前提はここに置く。

- **PSRAM は無い** (`ESP32-S3-FN8`)。heap は内蔵 SRAM だけで、増やす手立てが無い。
  だから音声はストリーミングが必須で、発話 1 回ぶんをバッファに持つ設計は取れない
- **`urequests` ではなく `requests`。** MicroPython 1.20 での改名で、参考記事はたいてい
  古い名前で書かれている。どちらが載っているかは probe の `network.http.module` が言う
- **`Response.raw` はインスタンス属性。** クラスの `dir()` には出ないので、無いと判断する
  前に実物を見る。これが無いとストリーミングができず、`content` で全体を heap に載せる
  しかなくなる
- **socket の既定はブロッキング。** アプリの 40ms tick に載せるには `settimeout` が要る
- **non-blocking な socket を素で呼ぶと、毎 tick 例外を確保する。** 相手が居ない
  `accept()` / `recv()` は `OSError(EAGAIN)` を投げる。40ms tick に載せると 1 秒あたり
  25 個の例外オブジェクトが積まれ、発話中の heap では詰まりの種になる。`select.poll` に
  登録して、読めるときだけ呼ぶ (`buddy/serial.py` / `buddy/netlink.py`)
- **`errno` は CPython より小さい。** `EWOULDBLOCK` が無く、モジュール読み込みの時点で
  `AttributeError` になってアプリが起動しない (`--compile-only` では捕まらない)。
  `EAGAIN` / `EINPROGRESS` / `ECONNRESET` / `ETIMEDOUT` はある。名前で引くなら
  `getattr(errno, name, None)` (`device/buddy/netlink.py`)
- **`buddy_status` の `sys.heap` は probe の `heap` より小さい。** transport と UI を上げた
  後の値だから。同じ量として比べない
- **WAV は `fmt ` の後に `data` が来て、PCM は 1ch/16bit。** 通常は本体が offset 44 から
  始まるが、`device/buddy/tts.py` はそれを前提にせずチャンクを歩く。取得バイト数は
  `rate × 2 × 秒数 + ヘッダ` なので、`outputSamplingRate` を下げれば比例して減る
  ([buddy-speak](../buddy-speak/SKILL.md))
