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
- `device/buddy_tts.py` が WiFi 経由で VOICEVOX ENGINE を叩き、`device/buddy_speak.py` が
  返ってきた WAV をストリーミングで `M5.Speaker` へ流す。声はずんだもん

音声はもともとホストの macOS `say` で合成して USB のバルク転送で送っていたが、
デバイス自身が API を叩く形に置き換えた。USB を渡るのはテキストだけになっている。

```
Claude Code ──USB CDC──> Cardputer-Adv ──WiFi──> VOICEVOX ENGINE (Mac の Docker)
                              └ M5.Speaker <────── WAV ストリーム
```

置き換えの制約は実機の実測から来ている。**PSRAM が無く heap が 30KB 程度しか残らない**
ので、WAV 全体をメモリに載せる経路 (`Response.content`、`M5.Speaker.playWav`) はどれも
使えず、ソケットから 2048 バイトずつ読んで鳴らしながら受ける形になっている。

未解決:

- **デバイスから返す口がない。** キーボード入力をセッションへ返す経路はまだ無く、
  表示も発話も往路だけの片道通信になっている。
- 日本語フォントは 24px 一択で、画面には 4 行 × 9 文字しか入らない。分割して送ると古い行から
  流れて消える。スクロールバックを読む手段はまだ無い。
- **合成中は UI が数秒止まる。** `audio_query` / `synthesis` の POST がメインループを
  ブロックする。`_thread` は GIL 付きなので逃げ場がない。
- WiFi 設定はブートごとに `net.config` で入れ直す必要がある。デバイスには保存しない。
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

### VOICEVOX ENGINE

喋らせるのに要る。デバイスが LAN 越しに叩く。

```bash
docker compose up -d
```

**`compose.yaml` はポートを `0.0.0.0` に bind している。** VOICEVOX の README にある
`-p '127.0.0.1:50021:50021'` だと Mac の loopback にしか listen せず、デバイスからは
届かない。`voicevox_url()` は loopback アドレスを渡されたらホスト側でエラーにする —
デバイスまで届けてしまうと接続タイムアウトとして数秒後に出るため、原因から遠い。

エンジンの場所は `$VOICEVOX_URL`、未設定ならこのマシンの LAN アドレスを自動検出する。

> `sandbox` は loopback 宛でも `curl` を拒否する。疎通確認は `uv run` を通した Python
> から行う。

### デバイスのプロビジョニング

初期化 (ファームウェア書き込みと upstream バンドルの配置) は `cwc-makers` プラグインの
`m5-onboard` スキルが行う。スキルは
[moremas/build-with-claude](https://github.com/moremas/build-with-claude) のローカル
クローンを要求するので、`/maker-setup` で作る。本リポジトリはそのクローンに依存しない
(overlay の転送は `host/buddy_push.py` が行う)。

転送と REPL 実行は MicroPython 公式の [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)
に任せている。ライブラリとして使っており (`mpremote.transport_serial.SerialTransport`)、
CLI は叩かない。以前は paste mode (Ctrl-E / Ctrl-D) を自前で駆動していたが、paste mode には
フロー制御が無く、長い転送がデバイスの rx を追い越して無言で切れる。raw REPL の raw-paste
mode はウィンドウ制御付きで、しかもソースをエコーバックしないので、結果を正規表現で
拾い直す必要も無くなった。

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

# WiFi に繋ぐ (ブートごとに要る。パスワードは $BUDDY_WIFI_PSK から)
export BUDDY_WIFI_PSK=...
uv run python host/buddy_bridge.py --port $PORT --wifi MyNetwork

# 喋らせる (画面にも同じ文が出る。--no-show で音だけ)
uv run python host/buddy_bridge.py --port $PORT --speak "直したのん。もう一回まわす?"
uv run python host/buddy_bridge.py --port $PORT --speak "hello" --speaker 8

# 実機のフォント一覧と実測メトリクスを取る (REPL が要る、read-only、JSON で出る)
uv run python host/probe_device.py --port $PORT
```

`--start` は片道。アプリは transport 起動時に `micropython.kbd_intr(-1)` で Ctrl-C を
無効化するため、REPL に戻るには本体背面の BtnRST を押す。

REPL を要求するもの (`buddy_push.py`、`buddy_bridge.py --start` / `--wifi`、
`probe_device.py`) は BtnRST が押されるまでポーリングして待つ。「押してから実行し直す」を
求めない。待ち時間は `--wait` 秒 (既定 180、0 で待たない)。MCP の `buddy_start_app` だけは
ツール呼び出しを長時間ブロックしないよう既定 15 秒。

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
| `buddy_net_config` | WiFi に繋ぐ。`buddy_speak` の前にブートごとに一度 |
| `buddy_speak` | 喋らせる。デバイスが VOICEVOX を叩く。既定では画面にも同じ文を出す |
| `buddy_chat_clear` | 会話ログを消してダッシュボードへ戻す |
| `buddy_chat_info` | パネルが選んだフォントと行数・幅 |
| `buddy_events` | 前回の呼び出し以降にデバイスが発した全て (protocol + ログ) |

`buddy_say` は分割したパートの間に既定で 2 秒空ける (`pace`)。画面は末尾 4 行しか映らないので、
まとめて送ると読む前に流れていく。誰も見ていないなら `pace=0` でよい。

日本語だと 1 画面 4 行 × 9 文字しか入らない。ホスト側の分割上限はこの実測値
(`MAX_SAY_CHARS_WIDE = 32`) から来ていて、`device/buddy_chat.py` のフォント表と対になっている。
ファームウェアを入れ替えたら `host/probe_device.py` で測り直す。

`buddy_speak` は合成と再生の長さぶんブロックする。合成はデバイス自身が VOICEVOX ENGINE
を叩いて行う (声はずんだもん、`speaker=3`)。ホストからはテキストとエンジンの URL しか
渡らない。事前に `docker compose up -d` と `buddy_net_config` が要る。

`ResidentLink` がバックグラウンドスレッドでポートを読み続けるため、ツール呼び出しの
合間に届いたメッセージも `buddy_events` で回収できる。ポートは1プロセスしか掴めないので、
`buddy_push.py` や `esptool` を使う前には `buddy_disconnect` する。

## 既知の制約

- デバイスはバッテリー駆動で、USB を抜くと電源が落ちる。挿し直しただけでは起動しない
  ことがあるため、側面の電源ボタンを押す。
- `/dev/cu.*` が現れないときは、USB バス上に居るかを先に確認する。`ioreg -p IOUSB` に
  出ていて `IOUSBHostInterface` が 0 個なら、列挙はしているがインタフェースが構成されて
  いない中間状態で、電源の入れ直しで解消する。
