# m5stack-sandbox

M5Stack Cardputer-Adv を Claude Code から操作するための実験リポジトリ。

Claude Buddy の BLE transport を USB シリアルに差し替えて、エージェントから直接デバイスと
通信する。デバイスに繋がるのは常駐 daemon 1 つで、セッションはそこへ HTTP でぶら下がる。

## 何が動くか

```
Claude Code ──HTTP──> buddy-mcpd (常駐) ──> host/link ──USB CDC──> Cardputer-Adv
  (複数セッション)                                                      └ buddy/app.py + buddy/serial.py
```

- `status` / `name` / `owner` のラウンドトリップと、デバイス発の `hello` の受信
- **画面に出す** — `device/buddy/chat.py` が LCD 上に折り返し付きの会話ログを描く
- **喋らせる** — デバイス自身が LAN 越しに VOICEVOX ENGINE を叩き、返ってきた WAV を
  ストリーミングで `M5.Speaker` へ流す。声はずんだもん

```
Claude Code ──USB CDC──> Cardputer-Adv ──WiFi──> VOICEVOX ENGINE (Mac の Docker)
                              └ M5.Speaker <────── WAV ストリーム
```

USB を渡るのはテキストだけで、合成はデバイスが行う。WiFi の link はブート時に出来上がって
いて、アプリはそれを継承する (認証情報は一度だけ焼く)。

`chat.*` / `speak.*` はどちらも独自 verb で、`buddy/router.py` の `on_line` で横取りしている。
upstream のファイルには手を入れていない。

### まだ無いもの

- **デバイスから返す口。** デバイス側の入力をセッションへ返す経路が無く、片道通信のまま。
  キーボードは読んでいない (upstream の Y/N/Q は落とした)。issue #33 で音声入力から作り直す
- **スクロールバック。** 日本語だと 1 画面 4 行 × 9 文字。分割して送ると古い行から流れて消える
- **合成中の応答性。** `audio_query` / `synthesis` の POST がメインループを数秒ブロックする
- **ファイル push** (`char_begin` / `file` / `chunk` 系)。`buddy_chars.py` が transport に
  よらず無条件で拒否する

## 構成

uv workspace の 4 member。`device/` はデバイスの `/flash/` へ流し込む overlay (MicroPython)、
`host/` はホスト側の link / MCP server / ツール類。protocol・UI・永続状態のレイヤは
upstream のものが既にデバイスに入っており、本リポジトリでは触らない。

member とファイルの一覧は [CLAUDE.md](CLAUDE.md#構成) にある。

## overlay とは

**本リポジトリはファームウェアイメージを配布しない。** デバイスの `/flash` には既に
2 つの層が載っている — UIFlow 2.0 のユーザーファイルシステムと、その上に
[moremas/build-with-claude](https://github.com/moremas/build-with-claude) の Claude Buddy。
ここが送り込むのはその一部の上書き・追加・削除で、それを overlay と呼んでいる。

デプロイ後の `/flash` は次のようになる。`buddy_deploy.py` が最後に出す `report_flash` が
現物で、下表はその各項目の持ち主。

| flash の項目 | 持ち主 | 本リポジトリの扱い |
| --- | --- | --- |
| `.frozen` / `/lib` / `/system` (`sys.path` 上、`/flash` 外) | ファームウェア | 触らない |
| `README.md` / `libs/` / `res/` / `certificate/` | UIFlow のユーザー FS の雛形 | 触らない |
| `boot.py` | UIFlow | 触らない (`uiflow/boot_option` が 2 なので `main.py` へ素通しする) |
| `buddy_protocol.mpy` / `buddy_ui_cp.mpy` / `buddy_state.mpy` / `buddy_chars.mpy` | upstream | **読んで `.mpy` にして書き戻す。** 中身は変えない |
| `buddy/` (`app` / `router` / `chat` / `chat_font` / `chat_wrap` / `debug` / `serial` / `speak` / `tts`) | 本リポジトリ | 追加 |
| `apps/claude_buddy.mpy` | upstream 派生 | 置き換え (中身は `buddy/app.py` へ移し、ここは `run()` を渡す起動口だけ) |
| `main.py` | 本リポジトリ | **置き換え。** upstream のランチャーは捨てた |
| `buddy_ble` / `burst_frames.py` / `apps/snake.py` / `apps/hello_cardputer.py` | upstream | **消す。** NimBLE が確保する ESP-IDF heap が発話のソケットに要る |
| `buddy-ja.vlw` | 生成物 | 別経路 (`make_vlw.py`)。930KB あるのでデプロイでは触らない |
| `wifi_event.py` | UIFlow | 別経路 (`provision_wifi.py`) で認証情報だけ書き換える |

境界の引き方には理由がある。

- **upstream のファイルは書き換えない。** 再配布しないと NOTICE で宣言しており、
  `vendor/device/` の退避が `.py` を消した後の唯一の控えになる。だから
  `buddy_protocol` を拡張する代わりに、`buddy/router.py` の `on_line` で独自 verb
  (`chat.*` / `speak.*` / `dbg.*`) を先に横取りしている。**upstream を触らずに protocol を
  伸ばせる唯一の場所がここ**
- **自前のモジュールは `/flash/buddy/` にまとめる。** flash の階層がそのまま境界になり、
  ディレクトリ一覧を見れば誰のものか分かる
- **消すものにも理由が要る。** 消せば upstream の控えは `vendor/` にしか無くなる。
  だから `buddy_deploy.py` は消す前に必ず退避する

転送で守ることは [buddy-deploy skill](skills/buddy-deploy/SKILL.md) にある。

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

### チャットパネルの日本語フォント

一度だけ。ファームウェアが持つ日本語フォントは 24px のビットマップしかなく、110px の
パネルに 4 行 × 9 文字しか入らない。`setTextSize` で縮めることはできるが、最近傍で画素行が
間引かれて画数の多い漢字が潰れる。そこで VLW を生成して flash に置く。

```bash
# BIZ UDGothic (SIL OFL 1.1) を取る。再配布しないのでリポジトリには入れていない
mkdir -p tmp/fonts
curl -sSLo tmp/fonts/BIZUDGothic-Regular.ttf \
  https://raw.githubusercontent.com/googlefonts/morisawa-biz-ud-gothic/main/fonts/ttf/BIZUDGothic-Regular.ttf

uv run python host/tools/src/make_vlw.py \
  --font tmp/fonts/BIZUDGothic-Regular.ttf --size 16 \
  --out tmp/buddy-ja-16.vlw --port $PORT
```

JIS 第 1 水準まで 3476 グリフで 930KB。実測でヒープを 19.5KB 使い、`loadFont` は 57ms、
パネルには **6 行 × 13 文字** 入る。転送には 3 分ほどかかるが、焼き直すまで残る。

置いていない機体でも動く。日本語は内蔵の `EFontJA24` を 0.75 倍にしたものになり、
5 行 × 12 文字に減るだけ。`--chat-info` の `vlw` がどちらの状態かを答える。

### デバイスの初期化

ファームウェア書き込みと upstream バンドルの配置は `cwc-makers` プラグインの `m5-onboard`
スキルが行う。本リポジトリはそこに依存しない。

ファームウェアの取得が途中で切れるときは、先に
`uv run python host/tools/src/fetch_firmware.py --device cardputer-adv` を走らせる。
`Range: bytes=N-` でレジュームし、スキルが読むのと同じキャッシュに置く。

## 使い方

```bash
PORT=/dev/cu.usbmodem101

# overlay を転送する (走っているアプリは Ctrl-C で REPL に戻される)
# 転送後はアプリが起動して喋る。喋らせないなら --no-speak
uv run python host/tools/src/buddy_deploy.py --port $PORT

# アプリを起動して状態を取得する (電源投入なら --start は要らない)
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

# 走っているアプリの中を覗く (止めない)
uv run python host/link/src/buddy_bridge.py --port $PORT --dbg mem
uv run python host/link/src/buddy_bridge.py --port $PORT --dbg eval --dbg-src 'chat.active'

# アプリを畳んで REPL に戻す (reboot しない。画面に REPL と出る)
uv run python host/link/src/buddy_bridge.py --port $PORT --interrupt
```

電源を入れるだけでアプリは立ち上がる。`/flash/main.py` が WiFi を上げてから
`claude_buddy` を import して `run()` を呼ぶ (import だけでは起動しない)。`--start` や `buddy_start_app` が要るのは、Ctrl-C で
REPL に落とした後に立ち上げ直すときだけ。

REPL を要求するもの (`buddy_deploy.py`、`provision_wifi.py`、`buddy_bridge.py --start`、
`probe_device.py`) は、走っているアプリを Ctrl-C で畳んでから入る。それでも応答しない
デバイスのために BtnRST 待ちのポーリングが残っている。待ち時間は `--wait` 秒 (既定 180、
0 で待たない)。MCP の `buddy_start_app` だけはツール呼び出しを長時間ブロックしないよう
既定 15 秒。

### 動作確認とデバッグ

アプリが上がっている間もデバイス側に REPL は無い。代わりに `dbg.*` verb が既存の
シリアル経路に乗る。デバイス側の `buddy.debug` は **使うまで import されない**ので、
覗いていない間の heap コストは実測 64 バイト。初回の `dbg.*` でデバイスが
「デバッグモードに入ったのだ」と喋る (`--dbg-silent` で黙る)。

アプリを止めたいときは Ctrl-C が効く。`micropython.kbd_intr(-1)` はもう掛けていない —
host が流すのは `json.dumps` の出力だけで、制御文字は `\uXXXX` に逃げるため。アプリは
`KeyboardInterrupt` を捕まえて reboot せずに REPL で止まる。

詳しくは `buddy-debug` skill にある。

### MCP 経由

MCP server は常駐 daemon で、セッションは `http://127.0.0.1:8787/mcp` へ繋ぐ。
起こし方は「[常駐 daemon を起こす](#常駐-daemon-を起こす)」にある。ホスト側のコードを
直したら `buddy-mcpd restart` だけでよく、セッションの再起動は要らない。

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
| `buddy_debug` | 走っているアプリの中を覗く (`mem` / `frag` / `gc` / `state` / `eval` / `exec` / `off`) |
| `buddy_interrupt` | Ctrl-C でアプリを畳んで REPL に戻す。reboot はしない |
| `buddy_chatter_start` / `_stop` / `_status` | 作業中の独り言 (下記) の開始・停止・様子見 |

`buddy_say` は分割したパートの間に既定で 2 秒空ける (`pace`)。画面は末尾 4 行しか映らないので、
まとめて送ると読む前に流れていく。誰も見ていないなら `pace=0` でよい。

`buddy_speak` は合成と再生の長さぶんブロックする。事前に `docker compose up -d` と、
一度だけ WiFi の provisioning が要る。

`ResidentLink` がバックグラウンドスレッドでポートを読み続けるため、ツール呼び出しの合間に
届いたメッセージも `buddy_events` で回収できる。

### 常駐 daemon を起こす

デバイスに触るには daemon が要る。`uv tool` で入れて、CLI で起こす。

```bash
uv tool install --force --editable ./host/mcp   # buddy-mcp / buddy-mcpd が入る
buddy-mcpd start
buddy-mcpd status                    # pid・serve している URL・log の場所
```

`--editable` を落とさないこと。これが無いと入るのはその時点のコピーで、`host/` を
直しても `restart` では反映されず毎回入れ直すことになる。

| コマンド | |
| --- | --- |
| `buddy-mcpd start` | 起こす。ポートを掴み、chatter が動き出す |
| `buddy-mcpd stop` | 止めてポートを解放する。`buddy_deploy.py` や `esptool` の前に |
| `buddy-mcpd restart` | ホスト側のコードを直したときはこれだけ |
| `buddy-mcpd status` | 動いているか、どの URL とどのシリアルポートを見ているか |

daemon の出力は `~/.local/state/buddy/buddy-mcpd.log` に落ちる。起きなかったときは
まずここを読む。

**1 プロセスだけがポートを掴む。** 複数セッションが同時に繋がっても、デバイスに
触るのはこの daemon 1 つ。chatter も 1 つで、どのセッションの hook で撃たれても
同じ口が喋る。

**設定は `~/.config/buddy/config.toml`。** キーは `BUDDY_*` 環境変数へそのまま写り、
優先順位は 環境変数 > config.toml > 既定値。

```toml
port = "/dev/cu.usbmodem101"
connect_on_start = true

[chatter]
model = "sonnet"
```

HTTP のポートは `127.0.0.1:8787` に固定してある。`mcp-servers.json` が静的な URL を
持つので、`http_port` を変えたら登録側も直す。ずれは `buddy-mcpd status` の `url` に出る。

### plugin として使う

このリポジトリのルートが Claude Code plugin のルートを兼ねる。skill・chatter の hook・
MCP の登録が plugin として付いてくるので、他のプロジェクトからも同じ daemon を使える。

```bash
# 他のプロジェクトから使う
/plugin marketplace add usadamasa/m5stack-sandbox
/plugin install buddy@buddy

# このリポジトリで開発するとき
claude --plugin-dir .
```

marketplace 経由で入れた plugin はキャッシュへコピーされるため、working tree の編集は
届かない。**このリポジトリを触るときは `--plugin-dir .` で起動する。**

daemon の実体は plugin には入っていない。plugin が持つのは skill と hook と HTTP 登録
だけで、デバイスに繋ぐのは checkout 側で `uv tool install` した `buddy-mcpd`。

**chatter を動かすには sandbox の許可が要る。** hook が書く socket は
`~/.local/state/buddy/chatter.sock` にあり、許可が無いと `sendto` が EPERM で落ちる。
hook は失敗しても黙って exit 0 するので、**気づけるのは「独り言を言わない」ことだけ**。
plugin は sandbox 設定を配れないため、入れる側の `.claude/settings.json`
(または `~/.claude/settings.json`) に足す。

```json
{
  "sandbox": {
    "network": { "allowUnixSockets": ["~/.local/state/buddy"] },
    "filesystem": { "allowWrite": ["~/.local/state/buddy"] }
  }
}
```

AF_UNIX への接続は Seatbelt では network の operation なので、`allowUnixSockets` の方が
本命。`allowWrite` は daemon 側が pid・log・socket を書くために要る。

`/mcp` に `buddy` が 1 つだけ出れば繋がっている。skill は `/skills` から確認できる。
plugin 経由なので tool 名は `mcp__plugin_buddy_buddy__*` になる。

### 作業中に喋らせる (chatter)

エージェントが作業している間、デバイスが勝手に独り言を言う。dog fooding のための機能で、
音声経路を「思い出したときに呼ぶ」から「常時使われる」に変えるのが目的。

```
Claude Code hooks ─datagram─> ~/.local/state/buddy/chatter.sock ─> daemon の worker thread
                                                                     └─ 台詞を生成してキャッシュ
                                                                     └─ ResidentLink で発話
```

**タスクを一切ブロックしない。** hook は `scripts/buddy_chatter_notify.py` が
datagram を 1 発投げて終わり (約 40ms、listener が居なくても exit 0)。合成と再生は
daemon 側のスレッドが自分の時間でやる。そのスレッドはデバイスのロックを
`blocking=False` でしか取らないので、本物のツール呼び出しを待たせることが無い。

**台詞は `claude -p` が書く。** API クライアントではなく CLI を 1 ターン起動する。
CLI はその機械に確実に入っていてログイン済みの唯一のもので、モデルも認証もユーザーの
設定をそのまま継承できる。

起動するターンは道具を持たない。`--safe-mode` で hook も MCP server も skill も
CLAUDE.md も読み込まず、`--tools ""` で構造化出力以外を落とし、cwd は空の一時ディレクトリ。
hook を読み込ませないのが特に効く — このリポジトリの hook は chatter へ datagram を投げるので、
読み込むと chatter が自分の生成から生成することになる。

**chatter は daemon に 1 つ。** どのセッションの hook で撃たれても同じ chatter が反応する。
複数セッションが同時に繋がっていても、喋る口は 1 つしかない。

**間隔は毎回引き直し、セッションの忙しさで動く。** 固定間隔はメトロノームに聞こえて数分で
気に障るため。さらに、hook イベントの流量を見て、大きく動いている間は間隔の短いほうから、
静かな間は長いほうから引く。長い間隔を引いた直後に作業が始まったら、進行中の待ちもその場で
縮む。今どのくらいの流量とみなされているかは `buddy_chatter_status` の `tempo`。

リンクが上がるまでは何も喋らない。chatter が自分からポートを開けることは無い
(`buddy_deploy.py` や `esptool` のため)。

**daemon が上がった時点でリンクも上がる。** ポートを保持するのが daemon の存在理由なので、
起動直後に一度だけポートを開く (`config.toml` の `connect_on_start = false` で切れる)。
デバイスは電源が入っていればアプリまで自分で立ち上がる (`device/main.py`) ので、これだけで
`buddy-mcpd start` の直後から独り言が始まる。

試行は一度きりで、失敗しても再試行しない。`buddy_disconnect` がポートの所有権について
最後の一言であり続けるため — deploy の前に手放したポートを、あとから勝手に取り返す経路は
無い。セッションの途中でデバイスを挿したときは `buddy_connect` を呼ぶ。開いたかどうかは
`buddy_chatter_status` の `connect_on_start` に出る。

**喋る内容を変えたいときは `host/mcp/src/chatter_prompt.md` を直す。** コードは触らなくてよい。

**モデルと effort は走らせたまま変えられる。** `buddy_chatter_start(model="haiku",
effort="high")` で、次のバッチから効く。既定は `sonnet` と `low` — 独り言を 1 行書くのに
大きいモデルは要らず、これはセッション中ずっと回るため。今どれで書いているかは
`buddy_chatter_status` の `model` / `effort` に出る。

間隔・音量の頻度・無効化といった調整は環境変数と `buddy_chatter_start` で行う。一覧は
`skills/buddy-chatter/SKILL.md` にある。

daemon は起動時にホストのコードを import 済みなので、`buddy_chatter.py` を直しても
走っている daemon には届かない。`buddy-mcpd restart` で拾わせる。

切り分けのために単体プロセスで動かす口もある。こちらはポートを自分で掴むので、
**先に `buddy-mcpd stop`。**

```bash
uv run python host/mcp/src/buddy_chatter.py --port $PORT --once   # 1 行喋って終わる
uv run python host/mcp/src/buddy_chatter.py --port $PORT          # 常駐する
```

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
- アプリが未捕捉例外で落ちると `machine.reset()` が走り、自動起動でまた同じところへ着く。
  起動直後の WiFi 接続中 (8 秒ほど) に Ctrl-C を打つと REPL に落ちて止まる。REPL を要求する
  ホスト側のツールは待ちながらこれを繰り返すので、放っておいても抜けられる

## ライセンス

[Apache-2.0](LICENSE)。
[moremas/build-with-claude](https://github.com/moremas/build-with-claude) 由来のコードが
一部にあり、その帰属は [NOTICE](NOTICE) に、変更点は各ファイルのヘッダにある。

## クレジット

- VOICEVOX:ずんだもん
- 「VOICEVOX」は廣芝和之の商標、「ずんだもん」は SSS 合同会社の商標

チャットパネルのフォントは Morisawa BIZ UDGothic (SIL Open Font License 1.1)。
本リポジトリは再配布せず、`make_vlw.py` が手元で VLW に変換してデバイスに置くだけ。
配布元とライセンス全文は
[googlefonts/morisawa-biz-ud-gothic](https://github.com/googlefonts/morisawa-biz-ud-gothic)。
