---
name: buddy-chatter
description: 作業中に Cardputer-Adv が独り言を言う機能 (host/mcp/src/buddy_chatter.py と scripts/buddy_chatter_notify.py) を扱うときに使う。喋らない・喋りすぎる・台詞が変、hook が届いていない、chatter を直したのに反映されないときに参照する。ポートの所有権と、タスクをブロックしない設計の根拠もここ。
---

# 作業中の独り言 (chatter)

hook が起きるたびに datagram が飛び、常駐 daemon の worker thread がデバイスに喋らせる。
dog fooding が目的で、音声経路を常時使われる状態に置く。

```
hooks ─datagram─> $XDG_STATE_HOME/buddy/chatter.sock ─> buddy-mcpd (常駐)
  scripts/                                               ├ receiver thread : recvfrom → キュー
  buddy_chatter_notify.py                                └ worker thread   : 流量で動く間隔で 1 行
  (plugin の hooks.json が登録)                             ├ ClaudeCliLineSource (claude -p)
                                                            └ _device_lock を try-acquire
```

**chatter は daemon に 1 つ。** どのセッションの hook で撃たれても同じ chatter が反応する。
複数セッションが同時に繋がっていても、喋る口は 1 つしかない。

## 台詞を書くのは `claude -p`

API クライアントではなく CLI を 1 ターン起動する。CLI はその機械に確実に入っていて
ログイン済みの唯一のもので、モデルも認証もユーザーの設定をそのまま継承できる。
SDK を直接叩くと認証の解決を再実装して追随し続けることになる。

今どれで書いているかは `buddy_chatter_status` の `backend` / `model` / `effort`。

## 動かない・喋らないときの順序

1. **daemon が上がっているか。** `buddy-mcpd status` の `running`。落ちていたら
   `~/.local/state/buddy/buddy-mcpd.log` に理由がある
2. **リンクが上がっているか。** `buddy_chatter_status` の `skipped_offline` が増えていたら
   これ。chatter は自分からポートを開けない。daemon は起動直後に一度だけ開くので、
   その一度がどうだったかは同じ status の `connect_on_start` に出る (`ok: false` なら
   `error`)。試行は一度きりなので、途中で挿したデバイスには `buddy_connect` で繋ぐ
3. **hook が届いているか。** `queued` と各カウンタが全部 0 のままなら socket に何も
   来ていない。手で叩ける:
   `echo '{"hook_event_name":"Stop"}' | python3 scripts/buddy_chatter_notify.py`

   **一番ありがちなのは sandbox。** socket は `~/.local/state/buddy/chatter.sock` に
   あり、許可が無いと `sendto` が EPERM で落ちる。hook は例外を握り潰して exit 0 する
   ので、**失敗は完全に無音**。使うプロジェクトの `.claude/settings.json`
   (または `~/.claude/settings.json`) に足す:

   ```json
   {
     "sandbox": {
       "network": { "allowUnixSockets": ["~/.local/state/buddy"] },
       "filesystem": { "allowWrite": ["~/.local/state/buddy"] }
     }
   }
   ```

   **要るのは `allowUnixSockets` の方。** AF_UNIX への接続は Seatbelt では
   filesystem ではなく network の operation として扱われるので、`allowWrite` だけでは
   通らない。`allowWrite` が要るのは daemon 側が pid・log・socket を書くため。

   plugin は sandbox 設定を配れないので、これは plugin を入れる側の仕事になる。
   sandbox 設定はセッションを再起動するまで反映されない。

   **errno で切り分けられる。** sandbox に塞がれているなら EPERM (errno 1)。
   通過していれば、相手が居なければ ENOENT (2)、居れば ECONNREFUSED (61) など
   別の errno になる。EPERM だけが sandbox の返事:

   ```bash
   python3 -c 'import socket,json; s=socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); \
     s.sendto(json.dumps({"kind":"stop","detail":""}).encode(), \
     "'"$HOME"'/.local/state/buddy/chatter.sock"); print("ok")'
   ```

   届いたかどうかは `tempo` で測れる。`_tempo()` は
   `len(_activity) / (窓 120 秒 / 60) / busy_rate` なので、5 発投げれば
   `5 / 2 / 12 = 0.21` になる。0 のままなら 1 件も届いていない。

   パスの食い違いを疑うなら `buddy_chatter_status` の `socket` と
   `buddy-mcpd status` の `socket` を見比べる
4. **台詞が作れているか。** `generation_failures` が立っていたら `generation_error` を読む。
   - `claude -p wrote no answer (rc N): ...` → CLI 側。末尾に stderr が付く。
     未ログイン、`claude` が daemon の PATH に無い、のどちらか
   - `claude -p reported an error: ...` → ターンは走ったが失敗した。レート制限や残高など
   どれも固定の台詞に落ちるので、完全な沈黙の原因にはならない
5. **VOICEVOX とネットワーク。** `last_error` に `RuntimeError: device refused speak.say` や
   タイムアウトが出る。`docker compose up -d` と WiFi の provisioning は前提

`skipped_busy` が増えるのは異常ではない。本物のツール呼び出しがデバイスを持っている間、
chatter は黙って諦める — そのための try-acquire。

## 直しても反映されないとき

**daemon は起動時にホストのコードを import 済み。** `buddy_chatter.py` を直しても
走っている daemon には届かない。

```bash
buddy-mcpd restart     # これだけ。セッションの再起動は要らない
```

単体プロセスで切り分けたいときは、先に `buddy-mcpd stop` してポートを空ける。

```bash
uv run python host/mcp/src/buddy_chatter.py --port $PORT --once   # 1 行喋って status を吐く
uv run python host/mcp/src/buddy_chatter.py --port $PORT          # 常駐 (発話を stderr に出す)
```

**ポートは 1 プロセスしか掴めない。** 動いている間は MCP の `buddy_*` からデバイスに
触れない。終わったら止めて `buddy-mcpd start` に戻す。

hook は plugin の `hooks/hooks.json` が登録する。plugin を入れ替えた直後は、
セッションを再起動するまで発火しないことがある。その間も idle のタイマーだけは動くので、
独り言そのものは出る。

## 触るときに壊してはいけない性質

- **hook は datagram を投げるだけ。** `AF_UNIX`/`SOCK_DGRAM` に connect も返事も無く、
  listener が居なくても `sendto` が失敗して exit 0 になる。ここで合成や再生をやると、
  その数秒が発火したツール呼び出し全部に乗る
- **hook は標準ライブラリだけで動く。** system の `python3` で毎回のツール呼び出しに
  乗るので、workspace の import も TOML の解析も持ち込まない。socket のパスを
  `buddy_paths` と同じ答えにする数行だけを複製し、一致は `test_hook.py` の契約テストで縛る
- **worker は `_device_lock` を `blocking=False` でしか取らない。** ブロッキングにすると
  chatter が本物のツール呼び出しを待たせる側になる
- **起動時の接続は一度きりで、再試行しない。** ここをループにすると `buddy_disconnect` が
  意味を失い、deploy のために手放したポートを取り返してしまう。開けなかったときは
  `_startup_connect` に理由を残して黙る
- **台詞の生成はロックの外。** `claude -p` はプロセスを 1 つ起こす。ロックを持ったまま
  やるとそのままツール呼び出しの待ち時間になる。生成済みの行は `_pending` に置いて、
  デバイスが空くまで持ち越す
- **生成のターンは道具を持たない。** `claude -p` は `--safe-mode --tools ""
  --no-session-persistence` で、cwd は空の一時ディレクトリ。**`--safe-mode` を外さないこと** —
  外すとこのリポジトリの hook が読み込まれ、生成のターンが chatter へ datagram を投げて
  自分の生成から生成することになる
- **worker は例外を外に出さない。** 誰も見ていないスレッドで死ぬと、デバイスが静かになった
  ことにしか気づけない
- **`_device_lock` は MCP の全 tool が握る。** `ResidentLink.await_ack` は ack 名で先頭一致を
  取るので、同種の request が並ぶと互いの ack を持って帰る

## 間隔

固定間隔はメトロノームに聞こえて数分で気に障る。発話のたびにゆらぎを引き直す。沈黙の閾値は
`uniform(idle_min, idle_max)` そのままで、既定は 60〜180 秒。

発話間隔のほうは、`gap_min`〜`gap_max` (既定 40〜150 秒) のどこから引くかが**セッションの
忙しさで動く**。忙しいときは短いほうの端から、静かなときは長いほうの端から引く。

- **tempo** は直近 `_ACTIVITY_WINDOW` (120 秒) に届いた hook イベントの流量を 0〜1 に
  正規化した値。`BUDDY_CHATTER_BUSY_RATE` (既定 12 件/分) で 1.0 に飽和する。ツール呼び出し
  1 回は Pre と Post の両方で数えるので、12 件/分はおよそ 6 回/分
- 抽選の窓は範囲の `_TEMPO_WIDTH` (0.5) 幅を保ったままスライドする。幅を保つのは、忙しい側で
  ゆらぎが潰れてメトロノームに戻らないようにするため
- tempo は**引いた時点ではなく比較する時点で**読む。長い間隔を引いた直後にイベントが集中
  したら、進行中の待ちがその場で縮む。固定されるのは窓の中のゆらぎだけ
- chatter 自身の `idle` イベントは流量に数えない。数えると独り言が独り言を呼ぶ

今の値は `buddy_chatter_status` の `tempo` / `next_gap_s` / `busy_rate`。`next_gap_s` は
確定値ではなく「今この瞬間の閾値」なので、イベントが来るたびに動く。

うるさいときの下げ方は 3 つ。`BUDDY_CHATTER_VOICE_EVERY` を上げると声は N 回に 1 回で残りは
画面だけになる。`buddy_chatter_start(gap_min=..., gap_max=..., busy_rate=...)` は daemon を
再起動せずにその場で引き直す (`busy_rate` を上げると忙しさに反応しにくくなる)。完全に
黙らせるなら `buddy_chatter_stop`、恒久的には `config.toml` の `[chatter] enabled = false`。

## 設定

`$XDG_CONFIG_HOME/buddy/config.toml` (既定 `~/.config/buddy/config.toml`) か環境変数。
キーは機械的に対応する — `[chatter]` の `gap_min` が `BUDDY_CHATTER_GAP_MIN`。
優先順位は **環境変数 > config.toml > 既定値**。読むのは daemon の起動時 1 回なので、
変えたら `buddy-mcpd restart`。`buddy_chatter_start` の引数だけはその場で効く。

| 変数 | 既定 | |
| --- | --- | --- |
| `BUDDY_CHATTER` | `1` | `0` / `false` / `no` で完全に無効。socket も張らない |
| `BUDDY_CONNECT_ON_START` | daemon では有効 | ポートを起動時に開くか。読むのは `buddy_mcp.py` |
| `BUDDY_CHATTER_SOCKET` | `$XDG_STATE_HOME/buddy/chatter.sock` | hook 側と一致していること |
| `BUDDY_CHATTER_GAP_MIN` / `_MAX` | `40` / `150` | 発話間隔のゆらぎ幅 (秒) |
| `BUDDY_CHATTER_IDLE_MIN` / `_MAX` | `60` / `180` | 独り言までの沈黙のゆらぎ幅 (秒) |
| `BUDDY_CHATTER_BUSY_RATE` | `12` | tempo が 1.0 に飽和する hook イベント数 (件/分) |
| `BUDDY_CHATTER_VOICE_EVERY` | `1` | N 回に 1 回だけ声を出す。残りは画面のみ |
| `BUDDY_CHATTER_PROMPT` | `host/mcp/src/chatter_prompt.md` | 口調と性格 |
| `BUDDY_CHATTER_BATCH` | `6` | 1 回の生成で作る台詞の数 |
| `BUDDY_CHATTER_CLAUDE_BIN` | `claude` | Claude CLI の場所 |
| `BUDDY_CHATTER_MODEL` | `sonnet` | `claude -p --model` に渡す。alias でも id でもよい |
| `BUDDY_CHATTER_EFFORT` | `low` | `claude -p --effort`。空なら CLI の既定に任せる |
| `BUDDY_CHATTER_CLAUDE_TIMEOUT` | `120` | `claude -p` を諦めるまで (秒) |
| `BUDDY_CHATTER_SPEAKER` / `_RATE` | ずんだもん / 16kHz | VOICEVOX の style id とサンプルレート |

値が壊れていても既定に落ちるだけで起動は止まらない。chatter は装飾なので、設定の誤りが
daemon を落とす理由にはならない。socket のパスだけは `config.toml` に置けない —
hook がそこを読まないため、環境変数か XDG 既定値で揃える。

## 台詞

**口調と性格は `host/mcp/src/chatter_prompt.md` にある。** キャラクターを変えたいときは
コードではなくそれを直す。`BUDDY_CHATTER_PROMPT` で別のファイルを指してもよい。プロンプトの
読み込みは生成時なので、間違ったパスを指しても daemon は起動する — `generation_error` に
出るだけで、固定の台詞に落ちる。

まとめて生成してキャッシュし、尽きたら次のバッチを作る。1 回の呼び出しで数分ぶん賄うので、
生成の遅延は誰も待っていない。バッチはキャッシュが尽きた時点の文脈で作るため、後ろの行ほど
今の作業から遅れる。これは承知の上の割り切り — 実況ではなく独り言なので。

絞るなら `effort` の側で、`thinking` そのものは切らない。切ると `<thinking>` がそのまま
本文に漏れる既知の失敗があり、遅延はここでは誰も待っていない。既定は `low`。

**モデルと effort と batch はその場で変えられる。**
`buddy_chatter_start(model="haiku", effort="high", batch=3)` は daemon を再起動せずに
次のバッチから効く。既定は `sonnet` / `low` / `6` — 独り言を 1 行書くのに大きいモデルは
要らず、これはセッション中ずっと回るため。台詞が平板だと感じたら上げ、掛かりすぎるなら
下げる。今どれで書いているかは `buddy_chatter_status` の `model` / `effort` / `batch`。

生成された行はパネル 1 枚に収まる長さで切る。1 行 = 1 発話。上限は `max_chars`。

生成は空の一時ディレクトリを cwd にして走らせる。独り言のために session file を残したり、
たまたまそこにあった `CLAUDE.md` に引っ張られたりしないため。渡すのは
`--safe-mode --tools "" --no-session-persistence --output-format json --json-schema` で、
persona は `--system-prompt`、本文は stdin。

`claude -p --output-format json` の出力は 2 通り観測されている — ドキュメントどおりの
result オブジェクト 1 個と、stream event 全部の配列。`_cli_answer` はどちらも受け、配列なら
最後の `result` を取る。中身は `structured_output` を優先し、無ければ `result` の文字列を
JSON として読む。
