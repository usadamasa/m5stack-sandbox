---
name: buddy-chatter
description: 作業中に Cardputer-Adv が独り言を言う機能 (host/mcp/src/buddy_chatter.py と .claude/hooks/buddy_chatter_notify.py) を扱うときに使う。喋らない・喋りすぎる・台詞が変、hook が届いていない、chatter を直したのに反映されないときに参照する。ポートの所有権と、タスクをブロックしない設計の根拠もここ。
---

# 作業中の独り言 (chatter)

Claude Code の hook が起きるたびに datagram が飛び、MCP server の worker thread が
デバイスに喋らせる。dog fooding が目的で、音声経路を常時使われる状態に置く。

```
hooks ─datagram─> tmp/buddy-chatter.sock ─> MCP server
  .claude/hooks/                              ├ receiver thread : recvfrom → キュー
  buddy_chatter_notify.py                     └ worker thread   : ゆらぎ付き間隔で 1 行
                                                 ├ VertexLineSource (claude-opus-5)
                                                 └ _device_lock を try-acquire
```

## 動かない・喋らないときの順序

1. **リンクが上がっているか。** `buddy_chatter_status` の `skipped_offline` が増えていたら
   これ。chatter は自分からポートを開けないので、`buddy_start_app` か `buddy_connect` が要る
2. **hook が届いているか。** `queued` と各カウンタが全部 0 のままなら socket に何も来ていない。
   手で叩ける:
   `echo '{"hook_event_name":"Stop"}' | python3 .claude/hooks/buddy_chatter_notify.py`
3. **台詞が作れているか。** `generation_failures` が立っていて `generation_error` に
   `DefaultCredentialsError` が出ていたら ADC が無い。それでも固定の台詞で喋るので、
   完全な沈黙の原因にはならない
4. **VOICEVOX とネットワーク。** `last_error` に `RuntimeError: device refused speak.say` や
   タイムアウトが出る。`docker compose up -d` と WiFi の provisioning は前提

`skipped_busy` が増えるのは異常ではない。本物のツール呼び出しがデバイスを持っている間、
chatter は黙って諦める — そのための try-acquire。

## 直しても反映されないとき

**MCP server はセッション開始時にホストのコードを import 済み。** `buddy_chatter.py` を
直しても走っているサーバには届かない。単体プロセスで確かめる。

```bash
uv run python host/mcp/src/buddy_chatter.py --port $PORT --once   # 1 行喋って status を吐く
uv run python host/mcp/src/buddy_chatter.py --port $PORT          # 常駐 (発話を stderr に出す)
```

**ポートは 1 プロセスしか掴めない。** 単体で動かす前に `buddy_disconnect` を呼ぶ。動いている
間は MCP の `buddy_*` からデバイスに触れない。終わったら止めて `buddy_connect` に戻す。

hook の設定 (`.claude/settings.json`) を足した直後も、セッションを再起動するまでは
発火しないことがある。その間も idle のタイマーだけは動くので、独り言そのものは出る。

## 触るときに壊してはいけない性質

- **hook は datagram を投げるだけ。** `AF_UNIX`/`SOCK_DGRAM` に connect も返事も無く、
  listener が居なくても `sendto` が失敗して exit 0 になる。ここで合成や再生をやると、
  その数秒が発火したツール呼び出し全部に乗る
- **worker は `_device_lock` を `blocking=False` でしか取らない。** ブロッキングにすると
  chatter が本物のツール呼び出しを待たせる側になる
- **台詞の生成はロックの外。** Vertex への往復は数秒あるので、ロックを持ったままやると
  そのままツール呼び出しの待ち時間になる。生成済みの行は `_pending` に置いて、デバイスが
  空くまで持ち越す
- **worker は例外を外に出さない。** 誰も見ていないスレッドで死ぬと、デバイスが静かになった
  ことにしか気づけない
- **`_device_lock` は MCP の全 tool が握る。** `ResidentLink.await_ack` は ack 名で先頭一致を
  取るので、同種の request が並ぶと互いの ack を持って帰る

## 間隔

固定間隔はメトロノームに聞こえて数分で気に障る。発話のたびに `uniform(gap_min, gap_max)` と
`uniform(idle_min, idle_max)` を引き直す。既定は 40〜150 秒と 60〜180 秒。

うるさいときの下げ方は 2 つ。`BUDDY_CHATTER_VOICE_EVERY` を上げると声は N 回に 1 回で残りは
画面だけになる。`buddy_chatter_start(gap_min=..., gap_max=...)` はサーバを再起動せずに
その場で引き直す。完全に黙らせるなら `buddy_chatter_stop`、恒久的には `BUDDY_CHATTER=0`。

## 環境変数

MCP server の環境から読む (`.mcp.json` の `env`、または Claude Code を起動したシェル)。
読むのは起動時 1 回なので、変えたらセッションを再起動する。`buddy_chatter_start` の引数だけは
その場で効く。

| 変数 | 既定 | |
| --- | --- | --- |
| `BUDDY_CHATTER` | `1` | `0` / `false` / `no` で完全に無効。socket も張らない |
| `BUDDY_CHATTER_SOCKET` | `<repo>/tmp/buddy-chatter.sock` | hook 側と一致していること |
| `BUDDY_CHATTER_GAP_MIN` / `_MAX` | `40` / `150` | 発話間隔のゆらぎ幅 (秒) |
| `BUDDY_CHATTER_IDLE_MIN` / `_MAX` | `60` / `180` | 独り言までの沈黙のゆらぎ幅 (秒) |
| `BUDDY_CHATTER_VOICE_EVERY` | `1` | N 回に 1 回だけ声を出す。残りは画面のみ |
| `BUDDY_CHATTER_PROMPT` | `host/mcp/src/chatter_prompt.md` | 口調と性格 |
| `BUDDY_CHATTER_BATCH` | `6` | 1 回の生成で作る台詞の数 |
| `BUDDY_CHATTER_MODEL` | | 台詞の生成に使う model |
| `BUDDY_CHATTER_SPEAKER` / `_RATE` | ずんだもん / 16kHz | VOICEVOX の style id とサンプルレート |

値が壊れていても既定に落ちるだけで起動は止まらない。chatter は装飾なので、設定の誤りが
server を落とす理由にはならない。`BUDDY_CHATTER_SOCKET` を変えたときは hook 側にも同じ値を
渡すこと — 揃っていないと、hook は成功したまま何も届かない。

## 台詞

**口調と性格は `host/mcp/src/chatter_prompt.md` にある。** キャラクターを変えたいときは
コードではなくそれを直す。`BUDDY_CHATTER_PROMPT` で別のファイルを指してもよい。プロンプトの
読み込みは生成時なので、間違ったパスを指しても server は起動する — `generation_error` に
出るだけで、固定の台詞に落ちる。

まとめて生成してキャッシュし、尽きたら次のバッチを作る。1 回の呼び出しで数分ぶん賄うので、
生成の遅延は誰も待っていない。バッチはキャッシュが尽きた時点の文脈で作るため、後ろの行ほど
今の作業から遅れる。これは承知の上の割り切り — 実況ではなく独り言なので。

`thinking` は切らない。切ると `<thinking>` がそのまま本文に漏れる既知の失敗があり、遅延は
問題にならないので絞るなら `effort` の側。

生成された行はパネル 1 枚に収まる長さで切る。1 行 = 1 発話。上限は `max_chars`。
