---
name: buddy-debug
description: 走っている Buddy アプリの中を覗く、アプリを止めて REPL に戻す、落ちた理由を読むときに使う。dbg.* verb と buddy_debug / buddy_interrupt tool、Ctrl-C が効く根拠、遅延 import でメモリを食わない仕組みを扱う。デバイスの動作確認中に heap を測りたいとき、アプリが黙って落ちたとき、REPL に入れないときに参照する。
---

# 走っているアプリを覗く

アプリが上がっている間、デバイス側に REPL は無い。コンソールは 1 本しかなく、アプリが
そこで sentinel protocol を喋っている。代わりに 2 つの入口がある。

| したいこと | 手段 |
| --- | --- |
| 止めずに中を見る | `dbg.*` verb (MCP `buddy_debug` / CLI `--dbg`) |
| 止めて REPL に降りる | Ctrl-C (MCP `buddy_interrupt` / CLI `--interrupt`) |

## 出力は 2 つの経路に分かれる

`buddy.serial` の framing は sentinel の付かない行を全て log として host に流す。
つまり **デバイス側の `print()` はそのまま host に届く**。

- **ack** — 小さい構造化データ。`free` / `alloc` / `repr` など
- **log** — 大きいもの全部。`mem_info(1)` のヒープマップ、traceback

`buddy_debug` tool は両方返す。CLI は ack を 1 行、log を `log |` 付きで出す。
ack だけ見て「ok: true」で終わらせないこと。**欲しい情報は log の側にあることが多い**。

## verb

```bash
PORT=/dev/cu.usbmodem101
uv run python host/link/src/buddy_bridge.py --port $PORT --dbg mem
uv run python host/link/src/buddy_bridge.py --port $PORT --dbg eval --dbg-src 'chat.info()'
```

| verb | 返るもの | 使いどころ |
| --- | --- | --- |
| `mem` | `free` `alloc` (MicroPython heap) + `idf_free` `idf_largest` (ESP-IDF heap) | まずこれ |
| `frag` | ヒープマップ (log) | `mem_free` は足りているのに MemoryError が出るとき |
| `gc` | collect の前後 | 回収できる量を知りたいとき |
| `state` | transport / chat / speech の要約 | 画面や音が変なとき |
| `eval` | 式の `repr` (240 文字で切る) | 上の 4 つで足りないとき |
| `exec` | 文の実行。出力は log へ | 同上 |
| `off` | モジュールを unload。返る heap も報告 | 覗き終わったら |

`eval` / `exec` の名前空間には `ble` `chat` `chars` `proto` `speech` `state` `ui` が入っている。
`exec` はこの名前空間を書き換えるので、`chat = None` のような代入は本当に壊す。

## 二つの heap を見分ける

`gc.mem_free()` は **MicroPython heap** しか見ていない。socket が取れない・WiFi が上がらないの
原因はたいてい **ESP-IDF heap** の側で、そちらは `dbg.mem` の `idf_*` にしか出ない。

`idf_largest` (最大の連続ブロック) が `idf_free` (合計) より大幅に小さいときは断片化。
socket は連続領域を 1 本要求するので、合計が余っていても取れない。

実測の目安 (アプリ稼働中、`--speak` していない状態):

```
free 69712  alloc 63856  idf_free 40200  idf_largest 27648
```

## メモリを食わない仕組み

`buddy/debug.mpy` は flash に常駐するが **import されない**。`dbg.*` frame が来た瞬間に
`apps/claude_buddy.py` の `on_dbg` が import し、`dbg.off` で `del sys.modules` +
`delattr(sys.modules["buddy"], "debug")` + `gc.collect()` する。使っていない間のコストは
`_DBG_TAG in raw` の substring 判定だけ。

**`del sys.modules` だけでは足りない。** MicroPython は submodule を親 package の属性にも
入れるので、そちらの参照が残るとモジュールは heap に居座る
([buddy-deploy](../buddy-deploy/SKILL.md))。

実測 (`dbg.gc` の after と `dbg.off` の free の差):

- 常駐している間の保持量: **64 バイト**
- import 1 回あたりの一時的な消費: **約 5.5 KB** (collect で戻る)

一時消費のほうが効くので、`--speak` の最中や heap がギリギリのときに初回の `dbg.*` を
撃つのは避ける。

**unload するときは参照を全部切ってから collect する。** `dbg_holder` と `sys.modules` を
消しても、`on_dbg` のローカル `mod` が関数を抜けるまで生きている。これを消し忘れると
ack の `free` が嘘になる。

**`eval` / `exec` は実行時にパースする。** parse tree と bytecode が GC heap に積まれる。
これは `.py` をやめて `.mpy` にした理由 (`buddy_deploy.py` の docstring) と同じ穴なので、
`_MAX_SOURCE` = 192 文字で頭打ちにし、前後で `gc.collect()` している。日常の確認は固定
verb で済ませること。

## デバッグモードに入ると喋る

初回の `dbg.*` で「デバッグモードに入ったのだ」と声が出る。**画面には出さない** —
パネル自体が調査対象のことがあり、上書きしたら本末転倒だから。

どの呼び出しが初回かはデバイスしか知らない (host のプロセスは前のプロセスが import 済みか
分からない)。だからデバイス側が import したフレームの ack に `entered: true` を立て、host が
それを見て喋らせる。

止めたいときは MCP なら `announce=False`、CLI なら `--dbg-silent`。VOICEVOX が落ちていても
`dbg.*` 自体は失敗しない (握り潰して `announced: false` を返す)。

## Ctrl-C で REPL に戻る

`micropython.kbd_intr(-1)` はもう掛けていない。理由:

- host が線に流すバイトは全て `json.dumps` の出力。`ensure_ascii` の既定が 0x00〜0x1F を
  `\uXXXX` に逃がすので、payload から 0x03 は出てこない
- 生バイナリを流していた bulk audio mode は削除済み

アプリは `KeyboardInterrupt` を捕まえて **reboot せずに** REPL で止まる。画面に `REPL` と出る。
再起動は REPL から `machine.reset()`、あるいは `--start`。

**再び raw binary をこのチャネルに載せるなら `kbd_intr(-1)` を戻すこと。**

`buddy_deploy.py` / `provision_wifi.py` / `probe_device.py` / `--start` は
`enter_raw_repl` が先頭で Ctrl-C を撃つので、勝手に REPL に入る。BtnRST 待ちのループは
残っているが、それは Python の下で刺さったデバイス用の最後の手段。

## 落ちた理由を読む

- **main loop の例外は `sys.print_exception` してから finally に落ちる。** 以前は
  `finally: machine.reset()` が traceback を食っていた
- **`micropython.alloc_emergency_exception_buf(100)` を `main.py` で確保している。**
  callback や ISR で起きた例外はメモリを確保できないので、これが無いと
  「no memory to create exception」しか出ない
- **`gc.threshold(mem_alloc + mem_free // 4)`** を transport 起動後に設定している。
  既定は「確保に失敗したら collect」で、断片化した heap ではその時点で手遅れ

## 踏んだこと

- **`link.pump()` は返しながら drain する。** `pump()` してから `drain()` を呼ぶと空。
  これで `--start` の起動ログが丸ごと消えていた (import 失敗の traceback ごと)。
  `_dump(*link.pump(...))` と書く
- **MCP server はセッション開始時の host コードを持っている。** ホスト側を直しても
  走っているサーバには反映されない。実機検証は `uv run` の別プロセスで行う
- **ポートは 1 プロセスしか掴めない。** MCP が繋いだままだと CLI 側が開けない。
  `buddy_disconnect` を先に呼ぶ
