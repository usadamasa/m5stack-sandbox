---
name: buddy-device-limits
description: Cardputer-Adv の実機で採った実測値。heap の量、PSRAM の有無、MicroPython ランタイムに何があるか (requests / Response.raw / socket API)。デバイス側の設計を決めるとき、メモリ不足で落ちたとき、参考記事の API が実機に無いときに参照する。測り直しは probe_device.py。
---

# 実機の実測値

`host/tools/src/probe_device.py` で採った値。デバイス側の設計はここから来ている。
ファームウェアを入れ替えたら測り直す (REPL が要る、read-only、JSON で出る)。

```bash
uv run python host/tools/src/probe_device.py --port /dev/cu.usbmodem101
```

| 項目 | 実測 | 効いてくる場所 |
| --- | --- | --- |
| heap | `mem_free` 61248。`bytearray(200000)` は失敗 | 大きなバッファを持てない |
| PSRAM | **無し** (`ESP32-S3-FN8`) | 同上。ストリーミング必須 |
| アプリ読み込み時点の空き heap | 69920 (reboot 直後、`.mpy` + launcher 差し替え後) | ブート時から radio が上がっている分減る |
| HTTP | `urequests` は無く **`requests`** (MicroPython 1.20 で改名) | 参考記事は `urequests` で書かれている |
| `Response.raw` | ある (インスタンス属性なのでクラスの `dir()` には出ない) | ストリーミングの土台 |
| socket | `settimeout` / `readinto` あり。既定はブロッキング | 40ms tick に載せられる |
| WAV | `fmt `→`data`、PCM は offset 44。1ch/16bit | ヘッダ解析 |
| 16000Hz 指定 | 2.56 秒で 81964 バイト (既定 24000 の 2/3) | 帯域と heap |

`buddy_status` が返す `sys.heap` は transport と UI を上げた後の値なので、上表より 1 万ほど
小さく出る。

画面側の実測 (フォント・行数・幅) は [buddy-device-code](../buddy-device-code/SKILL.md) にある。
同じ `probe_device.py` が両方を出す。
