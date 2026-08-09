# m5stack-sandbox

M5Stack Cardputer-Adv を Claude Code から双方向に操作するための実験リポジトリ。

Claude Buddy の BLE transport を USB シリアルに差し替えることで、Claude Desktop の
Hardware Buddy を経由せずに Claude Code (Vertex AI backend) からデバイスと通信する。

## 現在の状況

デバイスのプロビジョニングから device / host 両側の transport、MCP server 化まで
一通り動いている。動作確認済みの経路:

```
Claude Code ──Bash──> host/buddy_bridge.py ──USB CDC──> Cardputer-Adv
                                                         └ claude_buddy.py
                                                           └ buddy_serial.py
```

`status` / `name` / `owner` のラウンドトリップと、デバイス発の `hello` の受信を実機で確認済み。

ホストからデバイスへメッセージを送って画面に出す経路と、それを喋らせる経路が通っている。
upstream の protocol を拡張せずに `chat.*` / `speak.*` という独自 verb を
`claude_buddy.py` の `on_line` で横取りする方式で、upstream のファイルには手を入れていない。

- `device/buddy_chat.py` が LCD 上に折り返し付きの会話ログを描く
- `device/buddy_speak.py` がホストから流れてくる PCM を `M5.Speaker` へ流す。合成は
  ホストの macOS `say`。実機で日本語の発話を確認済み (5.1 秒 / 81 ブロック / underrun 0)

未解決:

- **デバイスから返す口がない。** キーボード入力をセッションへ返す経路はまだ無く、
  表示も発話も往路だけの片道通信になっている。
- 日本語フォントは 24px 一択で、画面には 4 行 × 9 文字しか入らない。分割して送ると古い行から
  流れて消える。スクロールバックを読む手段はまだ無い。
- 発話中はキーボードが効かない。バルク転送とポンプがメインループを占める。
- ファイル push (`char_begin` / `file` / `chunk` 系) は使えない。`buddy_chars.py:136-141` が
  transport によらず無条件で拒否する。

## 構成

`device/` と `host/` の 2 つに分かれている。

- **`device/`** — デバイスの `/flash/` へ流し込む overlay。MicroPython で動く。
  BLE transport と同じインタフェースを持つシリアル transport と、そこへ差し替えた
  アプリ本体。protocol / UI / 永続状態のレイヤは upstream のものが既にデバイスに
  入っており、本リポジトリでは触らない。
- **`host/`** — ホスト側。フレーミングを解いてコマンドを送るクライアント、それを
  MCP tool として公開するサーバ、overlay の転送、ファームウェアの取得。
  テストは `host/tests/` にあり、全て実機なしで走る。

## 前提環境

### Python

```bash
uv sync
```

`.venv` が作られる。以降のコマンドは `uv run` を通す。

### デバイスのプロビジョニング

初期化 (ファームウェア書き込みと upstream バンドルの配置) は `cwc-makers` プラグインの
`m5-onboard` スキルが行う。スキルは
[moremas/build-with-claude](https://github.com/moremas/build-with-claude) のローカル
クローンを要求するので、`/maker-setup` で作る。本リポジトリはそのクローンに依存しない
(overlay の転送は `host/buddy_push.py` が行う)。

> ファームウェアの取得がプロキシに切られて `IncompleteRead` や MD5 不一致で
> 落ちるときは、`host/fetch_firmware.py` を先に走らせる。`Range: bytes=N-` で
> レジュームし、スキルが読むのと同じキャッシュに置くので、その後は
> スキルの書き込み手順がキャッシュヒットで進む。
>
> ```bash
> uv run python host/fetch_firmware.py --device cardputer-adv
> ```

### 品質チェック

```bash
uv run ruff check      # lint
uv run ruff format     # format
uv run basedpyright    # 型検査
uv run pytest --cov    # テスト + カバレッジ
```

同じものが GitHub Actions で回る。デバイスは要らない。

## 使い方

```bash
PORT=/dev/cu.usbmodem101

# デバイスへ overlay を転送 (REPL に居ることが前提。居なければ止まる)
uv run python host/buddy_push.py --port $PORT

# アプリを起動して状態を取得
uv run python host/buddy_bridge.py --port $PORT --start --status

# 走っているアプリへコマンドを送る
uv run python host/buddy_bridge.py --port $PORT --name Mikawa --owner usadamasa --watch 5

# 画面にメッセージを出す (--role user で相手側の色になる)
uv run python host/buddy_bridge.py --port $PORT --say "テストが3件落ちとる。"
uv run python host/buddy_bridge.py --port $PORT --chat-clear

# 喋らせる (画面にも同じ文が出る。--no-show で音だけ)
uv run python host/buddy_bridge.py --port $PORT --speak "直したのん。もう一回まわす?"
uv run python host/buddy_bridge.py --port $PORT --speak "hello" --voice Samantha

# 実機のフォント一覧と実測メトリクスを取る (REPL が要る、read-only)
uv run python host/probe_device.py --port $PORT
```

`--start` は片道。アプリは transport 起動時に `micropython.kbd_intr(-1)` で Ctrl-C を
無効化するため、REPL に戻るには本体背面の BtnRST を押す。`buddy_push.py` は転送前に
REPL の応答を確認し、返らなければ 1 バイトも書かずに止まる (無反応の相手に書き込んで
「成功したのに何も入っていない」状態になるのを防ぐため)。

### MCP 経由

`.mcp.json` は Claude Code の起動時に読み込まれるため、追加・変更した後はセッションの
再起動が要る。再起動後に使えるツール:

| tool | 用途 |
| --- | --- |
| `probe_serial` | `tcsetattr` が通るかの判定。**最初にこれを呼ぶ** |
| `buddy_connect` / `buddy_disconnect` | シリアルの掴み直し。`buddy_push.py` を使う前は disconnect する |
| `buddy_start_app` | REPL 経由でアプリを起動。起動時のトレースバックも返る |
| `buddy_status` | status ack を取得 |
| `buddy_set_name` / `buddy_set_owner` | NVS に永続化される表示名とオーナー |
| `buddy_say` | 画面にメッセージを出す。markdown は潰され、画面 1 枚ずつに分割して送られる |
| `buddy_speak` | 喋らせる。ホストで合成して PCM を流す。既定では画面にも同じ文を出す |
| `buddy_chat_clear` | 会話ログを消してダッシュボードへ戻す |
| `buddy_chat_info` | パネルが選んだフォントと行数・幅 |
| `buddy_events` | 前回の呼び出し以降にデバイスが発した全て (protocol + ログ) |

`buddy_say` は分割したパートの間に既定で 2 秒空ける (`pace`)。画面は末尾 4 行しか映らないので、
まとめて送ると読む前に流れていく。誰も見ていないなら `pace=0` でよい。

日本語だと 1 画面 4 行 × 9 文字しか入らない。ホスト側の分割上限はこの実測値
(`MAX_SAY_CHARS_WIDE = 32`) から来ていて、`device/buddy_chat.py` のフォント表と対になっている。
ファームウェアを入れ替えたら `host/probe_device.py` で測り直す。

`buddy_speak` は音声の長さぶんブロックする。合成は macOS の `say` で、声は
`say -v '?'` に出るものなら何でも指定できる (既定は Kyoko)。**sandbox の中では `say` が
無音のファイルを exit 0 で吐く**ので、MCP server 経由か `uv run` 経由でしか動かない。

`ResidentLink` がバックグラウンドスレッドでポートを読み続けるため、ツール呼び出しの
合間に届いたメッセージも `buddy_events` で回収できる。ポートは1プロセスしか掴めないので、
`buddy_push.py` や `esptool` を使う前には `buddy_disconnect` する。

## 既知の制約

- デバイスはバッテリー駆動で、USB を抜くと電源が落ちる。挿し直しただけでは起動しない
  ことがあるため、側面の電源ボタンを押す。
- `/dev/cu.*` が現れないときは、USB バス上に居るかを先に確認する。`ioreg -p IOUSB` に
  出ていて `IOUSBHostInterface` が 0 個なら、列挙はしているがインタフェースが構成されて
  いない中間状態で、電源の入れ直しで解消する。
