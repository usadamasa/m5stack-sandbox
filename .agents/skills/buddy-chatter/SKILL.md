---
name: buddy-chatter
description: 作業中に Cardputer-Adv が独り言を言う機能 (host/mcp/src/buddy_chatter.py と .agents/hooks/buddy_chatter_notify.py) を扱うときに使う。喋らない・喋りすぎる・台詞が変、hook が届いていない、chatter を直したのに反映されないときに参照する。Claude Code と Codex のどちらで動いているか、どちらの LLM が台詞を書くかの判定もここ。ポートの所有権と、タスクをブロックしない設計の根拠もここ。
---

# 作業中の独り言 (chatter)

エージェントの hook が起きるたびに datagram が飛び、MCP server の worker thread が
デバイスに喋らせる。dog fooding が目的で、音声経路を常時使われる状態に置く。

```
hooks ─datagram─> tmp/buddy-chatter.sock ─> MCP server
  .agents/hooks/                              ├ receiver thread : recvfrom → キュー
  buddy_chatter_notify.py                     └ worker thread   : ゆらぎ付き間隔で 1 行
  (Claude Code / Codex 共用)                     ├ RoutingLineSource
                                                 │   ├ claude-code → VertexLineSource
                                                 │   └ codex       → CodexLineSource
                                                 └ _device_lock を try-acquire
```

## 接続元と backend

Claude Code と Codex の両方から使う。**違うのは台詞を書く LLM だけ**で、tool も発話経路も
同じ。組み合わせは固定 — Claude Code なら Vertex AI、Codex なら `codex exec`。2×2 は無い。
それぞれのマシンが既に持っている認証をそのまま使うのが狙いで、交差させるとどちらにも
無い認証情報が要る。

判定は接続してきた側から取り、witness は 2 つある。

| 経路 | いつ | どこ |
| --- | --- | --- |
| MCP の `initialize` の `clientInfo.name` | 接続直後 | `buddy_mcp._ClientProbe` (middleware) |
| hook datagram の `agent` | イベントごと | `.agents/hooks/` の `--agent` |

どちらも同じ `AgentIdentity` に書き、**後から観測したものが勝つ**。どちらも来なければ
`BUDDY_CHATTER_AGENT` (既定 `claude-code`)。認識できない名前は無視され、既に判っている
identity を消さない。

今どちらで動いているかは `buddy_chatter_status` の `agent` / `client` / `backend` / `model`。

## 動かない・喋らないときの順序

1. **リンクが上がっているか。** `buddy_chatter_status` の `skipped_offline` が増えていたら
   これ。chatter は自分からポートを開けないので、`buddy_start_app` か `buddy_connect` が要る
2. **hook が届いているか。** `queued` と各カウンタが全部 0 のままなら socket に何も来ていない。
   手で叩ける:
   `echo '{"hook_event_name":"Stop"}' | python3 .agents/hooks/buddy_chatter_notify.py --agent claude-code`
3. **backend が合っているか。** `backend` が思っているのと違うなら、hook の `--agent` か
   MCP の `clientInfo` のどちらかが嘘をついている。`client` に生の名前が出る
4. **台詞が作れているか。** `generation_failures` が立っていたら `generation_error` を読む。
   - `DefaultCredentialsError` → Vertex の ADC が無い
   - `codex exec wrote no answer (rc N): ...` → Codex CLI 側。末尾に stderr が付く。
     未ログイン、`codex` が PATH に無い、provider に届いていない、のいずれか
   どちらも固定の台詞に落ちるので、完全な沈黙の原因にはならない
5. **VOICEVOX とネットワーク。** `last_error` に `RuntimeError: device refused speak.say` や
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

単体プロセスには MCP の handshake が無いので、接続元は `--agent claude-code` /
`--agent codex` で指定する。既定は `BUDDY_CHATTER_AGENT`。

**ポートは 1 プロセスしか掴めない。** 単体で動かす前に `buddy_disconnect` を呼ぶ。動いている
間は MCP の `buddy_*` からデバイスに触れない。終わったら止めて `buddy_connect` に戻す。

hook の設定 (`.claude/settings.json`、Codex なら `.codex/hooks.json`) を足した直後も、
セッションを再起動するまでは発火しないことがある。その間も idle のタイマーだけは動くので、
独り言そのものは出る。Codex は新しい hook を初回に trust させる (`New hook - review
required`)。trust するまでは発火しない。

**`codex exec` は sandbox の中では動かない。** 素の Bash から呼ぶと
`failed to initialize in-process app-server client: Operation not permitted` になる
(seatbelt の入れ子)。`uv *` は `excludedCommands` に入っているので、上の
`uv run python host/mcp/src/buddy_chatter.py --once --agent codex` なら Codex 経路も
そのまま試せる。

## 触るときに壊してはいけない性質

- **hook は datagram を投げるだけ。** `AF_UNIX`/`SOCK_DGRAM` に connect も返事も無く、
  listener が居なくても `sendto` が失敗して exit 0 になる。ここで合成や再生をやると、
  その数秒が発火したツール呼び出し全部に乗る
- **worker は `_device_lock` を `blocking=False` でしか取らない。** ブロッキングにすると
  chatter が本物のツール呼び出しを待たせる側になる
- **台詞の生成はロックの外。** Vertex への往復は数秒、`codex exec` は 1 ターン丸ごとで
  もっと掛かる。ロックを持ったままやるとそのままツール呼び出しの待ち時間になる。生成済みの
  行は `_pending` に置いて、デバイスが空くまで持ち越す
- **backend は使うまで作らない。** 構築が認証を触る (Vertex は ADC を解決し、Codex は
  インストールを要求する)。`RoutingLineSource` は最初の 1 行を求められた時点で初めて作る
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

MCP server の環境から読む (`.mcp.json` の `env`、Codex なら
`.codex/config.toml` の `[mcp_servers.buddy] env`、または起動したシェル)。
読むのは起動時 1 回なので、変えたらセッションを再起動する。
`buddy_chatter_start` の引数だけはその場で効く。

| 変数 | 既定 | |
| --- | --- | --- |
| `BUDDY_CHATTER` | `1` | `0` / `false` / `no` で完全に無効。socket も張らない |
| `BUDDY_CHATTER_SOCKET` | `<repo>/tmp/buddy-chatter.sock` | hook 側と一致していること |
| `BUDDY_CHATTER_GAP_MIN` / `_MAX` | `40` / `150` | 発話間隔のゆらぎ幅 (秒) |
| `BUDDY_CHATTER_IDLE_MIN` / `_MAX` | `60` / `180` | 独り言までの沈黙のゆらぎ幅 (秒) |
| `BUDDY_CHATTER_VOICE_EVERY` | `1` | N 回に 1 回だけ声を出す。残りは画面のみ |
| `BUDDY_CHATTER_PROMPT` | `host/mcp/src/chatter_prompt.md` | 口調と性格 |
| `BUDDY_CHATTER_BATCH` | `6` | 1 回の生成で作る台詞の数 |
| `BUDDY_CHATTER_AGENT` | `claude-code` | 誰も名乗らなかったときの接続元 |
| `BUDDY_CHATTER_MODEL` | `claude-opus-5` | Vertex 側の model |
| `BUDDY_CHATTER_CODEX_BIN` | `codex` | Codex CLI の場所 |
| `BUDDY_CHATTER_CODEX_MODEL` | | 空なら `~/.codex/config.toml` の設定に任せる |
| `BUDDY_CHATTER_CODEX_TIMEOUT` | `180` | `codex exec` を諦めるまで (秒) |
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

**backend が変わっても prompt も schema も同じ。** どちらも `{"lines": [...]}` を要求し、
`LINES_SCHEMA` を共有する。違いは渡し方だけ — Vertex は `system` に persona を置けるが、
`codex exec` には system の口が無いので persona と本文を 1 本に繋いで stdin に流す。

`codex exec` は `--ephemeral --skip-git-repo-check --sandbox read-only` で、空の一時
ディレクトリを cwd にして走らせる。独り言のために session file を残したり、たまたま
そこにあった `AGENTS.md` に引っ張られたりしないため。
