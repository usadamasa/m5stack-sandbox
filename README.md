# m5stack-sandbox

M5Stack Cardputer-Adv を Claude Code から操作するための実験リポジトリ。

Claude Buddy の BLE transport を USB シリアルに差し替えて、Claude Code から直接デバイスと
通信する。

## 何が動くか

```
Claude Code ──MCP / CLI──> host/link ──USB CDC──> Cardputer-Adv
                                                   └ claude_buddy.py + buddy_serial.py
```

- `status` / `name` / `owner` のラウンドトリップと、デバイス発の `hello` の受信
- **画面に出す** — `device/buddy_chat.py` が LCD 上に折り返し付きの会話ログを描く
- **喋らせる** — デバイス自身が LAN 越しに VOICEVOX ENGINE を叩き、返ってきた WAV を
  ストリーミングで `M5.Speaker` へ流す。声はずんだもん

```
Claude Code ──USB CDC──> Cardputer-Adv ──WiFi──> VOICEVOX ENGINE (Mac の Docker)
                              └ M5.Speaker <────── WAV ストリーム
```

USB を渡るのはテキストだけで、合成はデバイスが行う。WiFi の link はブート時に出来上がって
いて、アプリはそれを継承する (認証情報は一度だけ焼く)。

`chat.*` / `speak.*` はどちらも独自 verb で、`claude_buddy.py` の `on_line` で横取りしている。
upstream のファイルには手を入れていない。

### まだ無いもの

- **デバイスから返す口。** キーボード入力をセッションへ返す経路が無く、片道通信のまま
- **スクロールバック。** 日本語だと 1 画面 4 行 × 9 文字。分割して送ると古い行から流れて消える
- **合成中の応答性。** `audio_query` / `synthesis` の POST がメインループを数秒ブロックする
- **ファイル push** (`char_begin` / `file` / `chunk` 系)。`buddy_chars.py` が transport に
  よらず無条件で拒否する

## 構成

uv workspace の 4 member。`device/` はデバイスの `/flash/` へ流し込む overlay (MicroPython)、
`host/` はホスト側の link / MCP server / ツール類。protocol・UI・永続状態のレイヤは
upstream のものが既にデバイスに入っており、本リポジトリでは触らない。

member とファイルの一覧は [CLAUDE.md](CLAUDE.md#構成) にある。

## セットアップ

### Python

```bash
uv sync --all-packages
```

`.venv` が 1 つ作られる。以降のコマンドは `uv run` を通す (シリアルポートを開く
`tcsetattr` が sandbox で拒否されるため)。

### CLI ツール

ライセンス検査に `trivy` を使う。バージョンは `aqua.yaml` が持つ。

```bash
aqua install
direnv allow   # 対話シェルから trivy を直接叩くとき
```

`aqua` と `direnv` 自体は別途入れておく (`brew install aqua direnv`)。`poe license` は
`aqua exec` 越しに呼ぶので direnv は要らない。`.envrc` が要るのは、シェルから `trivy` を
そのまま打ちたいときだけ。

### VOICEVOX ENGINE

喋らせるのに要る。デバイスが LAN 越しに叩く。

```bash
docker compose up -d
```

**`compose.yaml` はポートを `0.0.0.0` に bind している。** VOICEVOX の README にある
`-p '127.0.0.1:50021:50021'` だと Mac の loopback にしか listen せず、デバイスからは届かない。
エンジンの場所は `$VOICEVOX_URL`、未設定ならこのマシンの LAN アドレスを自動検出する。

### デバイスの初期化

ファームウェア書き込みと upstream バンドルの配置は `cwc-makers` プラグインの `m5-onboard`
スキルが行う。本リポジトリはそこに依存しない。

ファームウェアの取得が途中で切れるときは、先に
`uv run python host/tools/src/fetch_firmware.py --device cardputer-adv` を走らせる。
`Range: bytes=N-` でレジュームし、スキルが読むのと同じキャッシュに置く。

## 使い方

```bash
PORT=/dev/cu.usbmodem101

# overlay を転送する (REPL に居ることが前提。居なければ BtnRST を待つ)
# 転送後はアプリが起動して喋る。喋らせないなら --no-speak
uv run python host/tools/src/buddy_deploy.py --port $PORT

# アプリを起動して状態を取得する
uv run python host/link/src/buddy_bridge.py --port $PORT --start --status

# 走っているアプリへコマンドを送る
uv run python host/link/src/buddy_bridge.py --port $PORT --name Mikawa --owner usadamasa --watch 5

# 画面にメッセージを出す (--role user で相手側の色になる)
uv run python host/link/src/buddy_bridge.py --port $PORT --say "テストが3件落ちとる。"
uv run python host/link/src/buddy_bridge.py --port $PORT --chat-clear

# 喋らせる (画面にも同じ文が出る。--no-show で音だけ)
uv run python host/link/src/buddy_bridge.py --port $PORT --speak "直したのん。もう一回まわす?"

# WiFi を焼く (一度だけ。PSK は $BUDDY_WIFI_PSK から。--verify は reboot して確認する)
export BUDDY_WIFI_PSK=...
uv run python host/tools/src/provision_wifi.py --port $PORT --ssid MyNetwork --verify

# 実機のフォント一覧と実測メトリクスを取る (REPL が要る、read-only、JSON)
uv run python host/tools/src/probe_device.py --port $PORT
```

`--start` は片道。アプリは transport 起動時に `micropython.kbd_intr(-1)` で Ctrl-C を
無効化するため、REPL に戻るには本体背面の BtnRST を押す。

REPL を要求するもの (`buddy_deploy.py`、`provision_wifi.py`、`buddy_bridge.py --start`、
`probe_device.py`) は BtnRST が押されるまでポーリングして待つ。待ち時間は `--wait` 秒
(既定 180、0 で待たない)。MCP の `buddy_start_app` だけはツール呼び出しを長時間ブロック
しないよう既定 15 秒。

### MCP 経由

`.mcp.json` は Claude Code の起動時に読み込まれるため、変更した後はセッションの再起動が要る。

| tool | 用途 |
| --- | --- |
| `probe_serial` | `tcsetattr` が通るかの判定。**最初にこれを呼ぶ** |
| `buddy_connect` / `buddy_disconnect` | シリアルの掴み直し。`buddy_deploy.py` を使う前は disconnect する |
| `buddy_start_app` | REPL 経由でアプリを起動。起動時のトレースバックも返る |
| `buddy_status` | status ack を取得 |
| `buddy_set_name` / `buddy_set_owner` | NVS に永続化される表示名とオーナー |
| `buddy_say` | 画面にメッセージを出す。markdown は潰され、画面 1 枚ずつに分割して送られる |
| `buddy_speak` | 喋らせる。デバイスが VOICEVOX を叩く。既定では画面にも同じ文を出す |
| `buddy_chat_clear` | 会話ログを消してダッシュボードへ戻す |
| `buddy_chat_info` | パネルが選んだフォントと行数・幅 |
| `buddy_events` | 前回の呼び出し以降にデバイスが発した全て (protocol + ログ) |
| `buddy_chatter_start` / `_stop` / `_status` | 作業中の独り言 (下記) の開始・停止・様子見 |

`buddy_say` は分割したパートの間に既定で 2 秒空ける (`pace`)。画面は末尾 4 行しか映らないので、
まとめて送ると読む前に流れていく。誰も見ていないなら `pace=0` でよい。

`buddy_speak` は合成と再生の長さぶんブロックする。事前に `docker compose up -d` と、
一度だけ WiFi の provisioning が要る。

`ResidentLink` がバックグラウンドスレッドでポートを読み続けるため、ツール呼び出しの合間に
届いたメッセージも `buddy_events` で回収できる。

### 作業中に喋らせる (chatter)

Claude Code が作業している間、デバイスが勝手に独り言を言う。dog fooding のための機能で、
音声経路を「思い出したときに呼ぶ」から「常時使われる」に変えるのが目的。

```
Claude Code hooks ─datagram─> tmp/buddy-chatter.sock ─> MCP server の worker thread
                                                          └─ 台詞を生成してキャッシュ
                                                          └─ ResidentLink で発話
```

**タスクを一切ブロックしない。** hook は `.claude/hooks/buddy_chatter_notify.py` が
datagram を 1 発投げて終わり (約 40ms、listener が居なくても exit 0)。合成と再生は
MCP server 側のスレッドが自分の時間でやる。そのスレッドはデバイスのロックを
`blocking=False` でしか取らないので、本物のツール呼び出しを待たせることが無い。

**間隔は毎回引き直す。** 固定間隔はメトロノームに聞こえて数分で気に障るため。

`buddy_start_app` か `buddy_connect` でリンクが上がるまでは何も喋らない。chatter が
自分からポートを開けることは無い (`buddy_deploy.py` や `esptool` のため)。

**喋る内容を変えたいときは `host/mcp/src/chatter_prompt.md` を直す。** コードは触らなくてよい。

間隔・音量の頻度・無効化といった調整は環境変数と `buddy_chatter_start` で行う。一覧は
`.claude/skills/buddy-chatter/SKILL.md` にある。

MCP server はセッション開始時にホストのコードを import 済みなので、`buddy_chatter.py` を
直しても走っているサーバには届かない。単体プロセスで動かす口がある。

```bash
uv run python host/mcp/src/buddy_chatter.py --port $PORT --once   # 1 行喋って終わる
uv run python host/mcp/src/buddy_chatter.py --port $PORT          # 常駐する
```

こちらはポートを自分で掴む。**先に `buddy_disconnect` を呼ぶこと。** 走っている間は MCP の
`buddy_*` からデバイスに触れない。

## 品質チェック

```bash
uv run ruff check
uv run ruff format
uv run --directory host/link pytest --cov      # member ごと (device / host/link / host/mcp / host/tools)
uv run --directory host/link basedpyright      # 同上
uv run python host/tools/src/buddy_deploy.py --compile-only
uv run poe license        # 依存のライセンスを trivy の分類で検査する
uv run poe license-list   # 依存が名乗るライセンスを一覧するだけ
```

同じものが GitHub Actions で回る。デバイスは要らない。`--compile-only` が要るのは、`ruff` と
`basedpyright` が通っても MicroPython のパーサが受け取るとは限らないから。

## 既知の制約

- デバイスはバッテリー駆動で、USB を抜くと電源が落ちる。挿し直しただけでは起動しないことが
  あるため、側面の電源ボタンを押す
- `/dev/cu.*` が現れないときは、USB バス上に居るかを先に確認する。`ioreg -p IOUSB` に出ていて
  `IOUSBHostInterface` が 0 個なら、列挙はしているがインタフェースが構成されていない中間状態で、
  電源の入れ直しで解消する
- ポートは 1 プロセスしか掴めない。MCP server が掴んでいる間は CLI から触れない

## ライセンス

[Apache-2.0](LICENSE)。
[moremas/build-with-claude](https://github.com/moremas/build-with-claude) 由来のコードが
一部にあり、その帰属は [NOTICE](NOTICE) に、変更点は各ファイルのヘッダにある。

## クレジット

- VOICEVOX:ずんだもん
- 「VOICEVOX」は廣芝和之の商標、「ずんだもん」は SSS 合同会社の商標
