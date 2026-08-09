# CLAUDE.md

M5Stack Cardputer-Adv を USB シリアル経由で Claude Code から操作する実験リポジトリ。
経緯と設計判断は [README.md](README.md) にある。

## 言語

これから書くコメント・docstring、コミットメッセージ、PR の本文は日本語 (標準語) で書く。
既存の英語コメントは触らない。当面は混在する。

## コマンド

```bash
uv sync                        # .venv を作る (dev グループ込み)
uv run pytest                  # テスト
uv run pytest --cov            # カバレッジ付き
uv run ruff check              # lint
uv run ruff format             # format
uv run basedpyright            # 型検査
uv run python host/buddy_deploy.py --compile-only   # device/ が MicroPython で通るか
```

デバイス操作は必ず `uv run` を通す。

```bash
docker compose up -d                                       # VOICEVOX ENGINE
PORT=/dev/cu.usbmodem101
uv run python host/buddy_deploy.py --port $PORT            # 転送 + 起動 + 発話で確認
uv run python host/buddy_bridge.py --port $PORT --status   # 単発で叩く

# 声を出すなら、一度だけ WiFi を焼く (以降ブートごとの操作は不要)
export BUDDY_WIFI_PSK=...
uv run python host/provision_wifi.py --port $PORT --ssid <SSID> --verify

uv run python host/buddy_bridge.py --port $PORT --speak 'ずんだもんなのだ'
```

`sandbox` は loopback 宛でも `curl` を拒否する。エンジンの疎通確認は `uv run` 経由の
Python から行う (`tmp/voicevox_probe.py` が例)。

**`uv run` を経由する理由**: シリアルポートを開くには `tcsetattr` (ioctl) が要り、
Seatbelt はこれを拒否する。グローバル設定の `sandbox.excludedCommands` に `uv *` が
入っているため `uv run` は sandbox の外で走る。`.venv/bin/python` を直接叩く経路も
`.claude/settings.json` に登録してあるが、sandbox 設定はセッションを再起動するまで
反映されないため、設定を変えた直後は `uv run` を使う。

## 構成

| パス | 中身 |
| --- | --- |
| `device/buddy_serial.py` | デバイス側のシリアル transport (`BuddyBLE` を duck-typing) |
| `device/buddy_chat.py` | LCD 上のチャットパネル。`chat.*` コマンドを処理する |
| `device/buddy_speak.py` | VOICEVOX の WAV ストリームをブロックに割って `M5.Speaker` へ流す。`speak.*` |
| `device/buddy_tts.py` | VOICEVOX の呼び出しと WAV ヘッダの解析。WiFi は扱わない |
| `device/apps/claude_buddy.py` | upstream 派生。差分は transport 選択と chat / speak のルーティング |
| `device/main.py` | upstream の launcher の置き換え。WiFi を上げて REPL に落ちるだけ。ソースのまま置く |
| `host/buddy_bridge.py` | ホスト側クライアント。`BuddyLink` (単発) と `ResidentLink` (常駐) |
| `compose.yaml` | VOICEVOX ENGINE。`docker compose up -d` |
| `host/buddy_mcp.py` | MCP server |
| `host/device_repl.py` | `mpremote` の SerialTransport を掴むところ。BtnRST 待ちのループもここ |
| `host/buddy_deploy.py` | overlay を `.mpy` にして `/flash/` へ。upstream ピアの変換と launcher 差し替えもここ |
| `host/provision_wifi.py` | `/flash/wifi_event.py` の SSID/PASSWORD を書き換える。一度だけ |
| `host/probe_device.py` | 実機のフォント一覧・実測メトリクス・Speaker API を取る (read-only、JSON) |
| `vendor/device/` | デバイスから吸い出した upstream ソース。git 管理外、削除しない (再配布しないので他に控えが無い) |
| `host/fetch_firmware.py` | UIFlow 2.0 ファームウェアの取得 (Range レジューム付き) |
| `host/tests/` | 全て実機不要 |

`buddy_protocol.py` / `buddy_ui_cp.py` / `buddy_state.py` / `buddy_chars.py` は upstream の
ものがデバイスの `/flash/` に入っている。このリポジトリには置かない。`buddy_deploy.py` が
デバイスから読み出して `.mpy` にし、消す前に `vendor/device/` へ退避する。

## デバイスへ載るのは .mpy

**`.py` を push してはいけない。** import 機構は各 `sys.path` エントリで `foo.py` を
`foo.mpy` より先に探すので、ソースを置くとバイトコードが読まれなくなる。デバイスは
import のたびに構文木とバイトコードの両方を GC heap に作り、`gc.mem_free()` が 55280
あっても `import buddy_ui_cp` が 776 バイト取れずに落ちる (総量ではなく連続領域)。
`.mpy` 化で clean GC heap 55280 → 101120、ESP-IDF DATA の最大連続 17408 → 51200。

だから転送の入口は `host/buddy_deploy.py` だけ。`.py` を push する経路はリポジトリに無い。

```bash
uv run python host/buddy_deploy.py --port $PORT       # 転送 (BtnRST 待ちを含む)
uv run python host/buddy_deploy.py --port $PORT --no-speak   # 発話確認を省き REPL に残す
uv run python host/buddy_deploy.py --compile-only     # 実機なし。CI の mpy-build と同じ
```

- **転送の最後にアプリを起動して喋らせる。** ファイルが載ったことはバンドルが動くことを
  意味せず、import 失敗も engine 不達も speaker の沈黙もディレクトリ一覧からは区別できない。
  `verify_by_speech` が import・継承した WiFi link・VOICEVOX 往復・`M5.Speaker` を一度に
  通す。engine の URL はランチ前に解決する (後で分かってもポートはもう返らない)。
  失敗は exit 1 で、断った層を名指しする
- **したがってデプロイの後、デバイスはアプリを走らせたままになる。** 次のデプロイは
  BtnRST から始まる。REPL に残したいときだけ `--no-speak`

- **`mpy-cross` は `==1.27.0.post2` 固定。** バイトコードは同じ `.mpy` ABI の中でしか
  通用せず、デバイス (MicroPython 1.27) が読むのは v6。ずれたときの症状はデバイス側の
  素の ImportError だけになるので、転送前に `sys.implementation._mpy` と突き合わせている。
  ピンは `pyproject.toml` と `buddy_deploy.MPY_CROSS_ABI` の 2 箇所で、テストが一致を見る
- **消す前に必ず `vendor/device/` へ退避する。** upstream のピアは本リポジトリに無く
  (NOTICE のとおり再配布しない)、`.py` を消した後はそこが唯一の控えになる。
  `vendor/` は `.gitignore` に入っているが `tmp/` とは違って消してはいけない
- **`main.py` だけはソースのまま。** MicroPython は `/flash/main.py` を実行し、
  `main.mpy` を探さない。`--compile-only` はパーサに通すためだけにコンパイルし、
  結果は push 対象と別のディレクトリに落とす
- **タイムアウトはスクリプトの中。** `mpremote` はポートを `timeout=None` で開き、
  `raw_paste_write` は素の `serial.read(1)` で待つので、応答が止まると永久にブロックする。
  `bash` の `timeout` で包まない。ポートへ有限の read timeout を掛け、`--timeout` の予算を
  ステップ間で見て、落ちたステップ名を出す

## チャットパネル

`chat.say` / `chat.clear` / `chat.info` は upstream の `buddy_protocol.py` が知らない verb で、
知らない `cmd` は "unknown cmd" と印字して捨てられる。だから `claude_buddy.py` の `on_line`
で先に横取りしてから proto へ流す。ここが upstream ファイルを触らずに protocol を拡張できる
唯一の場所。

画面まわりで踏みやすい点:

- 表示中は `y=0..110` を chat が占有する。`BuddyUI.update_footer()` は `y=96..110` を塗るので
  `chat.active` の間は呼ばない。`set_connection()` は main panel も塗るので、呼んだら
  `chat.render()` で描き直す
- **日本語フォントは 24px しかない** (`EFontJA24` / `AlibabaSansJA24`、`fontHeight()` は 27)。
  中身に幅広文字があるかでフォントを切り替えており、日本語だと 4 行 × 9 文字、ASCII だけなら
  `DejaVu12` で 6 行 × 17 文字。ホスト側の分割上限 (`MAX_SAY_CHARS_WIDE` / `MAX_SAY_CHARS`) は
  この実測値から来ているので、片方を変えたらもう片方も見る
- `setFont` は sticky。計測も描画も `_push_font` / `_pop_font` で挟んで DejaVu9 に戻す。
  戻し忘れると `BuddyUI` の footer とヒント列まで 24px になる
- 実測のやり直しは `uv run python host/probe_device.py` (REPL が要る)

## 音声 (speak)

**デバイスが自分で VOICEVOX を叩く。** ホストからはテキストしか渡らない。声はずんだもん
(`speaker=3`)。合成はホストの macOS `say` が行っていたが、置き換えた。

```
Claude Code -MCP-> USB line {"cmd":"speak.say","text":...,"url":...}
  -> device/buddy_tts.py -HTTP-> VOICEVOX ENGINE (Mac の Docker)
  -> WAV ストリーム -> device/buddy_speak.py -> M5.Speaker
```

エンジンは `docker compose up -d` で立てる。**`-p` は `0.0.0.0` に bind すること。**
VOICEVOX の README の例は `127.0.0.1:50021:50021` だが、それだと Mac の loopback にしか
listen しないのでデバイスから届かない。`voicevox_url()` は loopback アドレスを渡されたら
エラーにする — デバイス側では接続タイムアウトとして数秒後に出るため、原因から遠い。

on-device TTS が無い事情は変わっていない。Cardputer-Adv は ESP32-S3 で、M5Stack の
on-device TTS (StackFlow の MeloTTS) は別基板の Module LLM (AX630C, Linux) が要る。
Espressif の `esp-tts` は中国語のみ。

### 実機の制約 (実測)

`host/probe_device.py` と `tmp/probe_*.py` で採った値。設計はここから来ている。

| 項目 | 実測 | 効いてくる場所 |
| --- | --- | --- |
| heap | `mem_free` 61248。`bytearray(200000)` は失敗 | WAV 全体を載せられない |
| PSRAM | **無し** (`ESP32-S3-FN8`) | 同上。ストリーミング必須 |
| HTTP | `urequests` は無く **`requests`** (1.20 で改名) | 参考記事は `urequests` で書かれている |
| `Response.raw` | ある (インスタンス属性なのでクラスの `dir()` には出ない) | ストリーミングの土台 |
| socket | `settimeout` / `readinto` あり | 40ms tick に載せられる |
| WAV | `fmt `→`data`、PCM は offset 44。1ch/16bit | — |
| 16000Hz 指定 | 2.56 秒で 81964 バイト (既定 24000 の 2/3) | 帯域と heap |

`M5.Speaker` には `playWav` / `playWavFile` もあるが、WAV 全体を渡す API なので heap に
載らない。`playRaw` にブロックを送り続ける。

**音量はファームウェアの既定の 4 倍にしてある** (`buddy_speak._VOLUME_GAIN`)。固定値では
なく `getVolume()` を読んで掛ける。既定は M5Unified のもので、ファームウェアと一緒に動くため。
実測の既定は 64 で、4 倍は 256 = byte の上限を超えるため 255 に丸まる。つまり現状は
master volume の最大で、これ以上は `_VOLUME_GAIN` を上げても変わらない。起動ログの
`buddy_speak: volume 64 -> 255` がその行。
掛けるのは `SpeechPlayer` を作るときの 1 回だけ = ブート 1 回につき 1 回で、リセットで
ファームウェアが自分の既定へ戻すので累積しない。master volume はアンプの手前で
サンプルを掛けるだけなので、既定が full scale の半分以下である限りクリップしない。

### ストリーミングの制約

`res.raw` は素のソケットで、**MicroPython のデフォルトはブロッキング**。何もしないと
`read()` がデータ待ちで止まり 40ms tick ごと固まる。`_StreamSource` が `settimeout(0.02)`
を掛けている (tick の半分)。

- **ブロックは 2048 バイト固定。** 40ms tick で 1 ブロックずつしか読まないので、これより
  小さいと再生が追いつかない
- **端数ブロックはデバイス側で無音パディングする。** ホストの `pad_to_blocks` がやっていた
  仕事が `_StreamSource.read_block` に移った。`playRaw` に短いブロックを渡すとクリックが鳴る
- **進捗が 3 秒止まったら諦める** (`_STALL_MS`)。EOF で足りない場合も同じ扱い。
  `Content-Length` で長さが分かっているので、途中で切れたのは異常
- `speak.end` の `stalls` が 0 以外なら供給が間に合っていない

合成中 (`audio_query` → `synthesis` の 2 回の POST) は **UI が数秒止まる**。`_thread` は
GIL 付きなので逃げ場がない。再生が始まってからは 1 tick 1 ブロックで進むので UI は動く。

### WiFi

**デバイスもホストも、実行時には WiFi を一切扱わない。** `/flash/main.py` がブート時に
`/flash/wifi_event.py` の認証情報で接続し、アプリはその link を継承するだけ。
認証情報は `host/provision_wifi.py` が一度だけ書き込む。

なぜアプリ側で繋げないか (実測):

- アプリ稼働中の `connect()` は受理されるが association が完了しない。15 秒後も
  `status()` は "connecting"。ランチャーだけ載った状態で ESP-IDF heap の最大領域が
  ~12 KB しかなく、link を上げるのに DRAM が足りない
- したがって radio はアプリ起動前に上がっている必要がある。**アプリは link を継承できるが、
  作れない**

なぜ NVS ではなく `wifi_event.py` か (実測):

- UIFlow の startup は `uiflow/ssid0` / `uiflow/pswd0` を読む。キーは**存在する**が空文字
  (以前ここに「`ESP_ERR_NVS_NOT_FOUND`」と書いてあったのは誤り。`get_blob` で読んでいた
  ための空振り。実際は `get_str`)
- ただし `uiflow/boot_option` が **2** ("user app mode") で、UIFlow のフレームワーク自体を
  迂回して `/flash/main.py` を直接走らせている。**NVS を読む経路が通らない**
- `/flash/wifi_event.py` の docstring 自身が「他所で使うなら SSID / PASSWORD を
  差し替えろ」と案内している。想定された拡張点

`/flash/wifi_event.py` に元から焼かれている `cardputer` / `cardconnect` は M5Stack の
展示会場の AP で、会場で配られる公開パスワード。ファイル冒頭にそう明記されている。

**代償**: PSK が `/flash/wifi_event.py` に平文で残る。またブート時から radio が上がる分、
アプリが読み込まれる時点の空き heap が落ちる。`.py` を載せていた頃はここが **41040** まで
下がって発話が通らなかった。`.mpy` 化と launcher 差し替えの後は **69920**
(いずれも `tmp/launch_probe.py`、reboot 直後の計測)。`buddy_status` が返す `sys.heap` は
transport と UI を上げた後の値なので、これより 1 万ほど小さく出る。

`claude_buddy.py` の WiFi 停止処理は `_TRANSPORT == "ble"` のときだけ走る。ESP32 の
WiFi/BLE 共存クラッシュ回避が目的で、serial では NimBLE を触らないので不要。むしろ
speech が radio を使う。なお `buddy_ble` はデバイスから外してあり (NimBLE が speech の
ソケット分の ESP-IDF heap を押さえるため)、BLE 分岐は upstream との diff を読める形に
保つためだけに残っている。

### provisioning の手順で踏んだこと

- **reset 直後に REPL を掴みに行ってはいけない。** `machine.reset()` の直後から poll すると
  90 秒経っても raw REPL に入れない。25 秒待ってから 1 回試すと 0.1 秒で入る。ブート中の
  ハンドシェイク試行がデバイスを戻らなくするらしい。`_SETTLE_S` はこれ
- **reset 後の `repl.close()` は必ず失敗する。** mpremote が RTS を落としに行き、消えた
  デバイスの fd に ioctl して `OSError: [Errno 6] Device not configured`。異常ではない
- `main.py` が動いている間でも BtnRST 無しで raw REPL に入れる (落ち着いた後なら 0.1 秒)。
  そこで割り込んでも既に完了した association は落ちない

## device/ は MicroPython

CPython ではなく MicroPython 1.27 (ESP32-S3) で動く。以下は CPython でも ruff でも
型検査でも通ってしまい、実機で初めて落ちる。`host/tests/test_device_constraints.py` が
AST で機械的に弾いているので、追加するときはそちらも見る。

- `typing` と `__future__` は存在しない。`from __future__ import annotations` を書くと
  起動時 ImportError になり、症状は「アプリが起動しない」だけになる
- 関数注釈は使ってよいが、**組み込み型の名前だけ**。それ以上は PEP 484 の
  `# type:` コメントで書く (パーサが見ないため安全)
- `bytearray` のスライス削除 (`del buf[:n]`) は `TypeError`。末尾への再束縛
  (`buf = buf[n:]`) を使う
- `contextlib` は無いので `try/except: pass` のまま置く (ruff の SIM105 は device/ で無効)

`device/apps/claude_buddy.py` は upstream 派生なので `ruff format` の対象外にしてある。
差分を読める状態に保つのが目的で、import 順や E402 も同じ理由で無視している。

## REPL は mpremote に任せる

デバイスと REPL で喋る経路 (ファイル転送・コード実行・値の読み出し) は
MicroPython 公式の `mpremote` を**ライブラリとして**使う。CLI は叩かない。
入口は `host/device_repl.py` の `connect_repl()` だけで、`Repl` protocol が
使っている API の範囲を記述している。

**paste mode (Ctrl-E / Ctrl-D) を自前で駆動しない。** 以前そうしていて踏んだこと:

- paste mode にはフロー制御が無い。長い転送がデバイスの rx を追い越して**無言で切れる**。
  固定チャンク長と 1 行ごとの `sleep` はその場しのぎだった
- paste mode はソースをエコーバックする。結果を読むのに自分が送った `print(...)` を
  正規表現の negative lookahead で除外する羽目になっていた

raw REPL ならどちらも無い。`repl.exec()` は完走するか例外を投げるかで、`repl.eval()` は
Python の値をそのまま返す (デバイス側で `print(repr(...))` してホストで `literal_eval`)。

`enter_raw_repl(soft_reset=False)` を使うこと。既定の `soft_reset=True` は
boot.py / main.py を走らせ直すので、UIFlow のランチャーが再起動してしまう。

BtnRST 待ちのループだけは自前。`mpremote` の `wait=` は `open()` の失敗しか
リトライせず、アプリが `kbd_intr(-1)` で Ctrl-C を殺している状態 (ポートは開くが
応答しない) を待てないため。

### アプリの起動 (`launch_app`)

アプリは起動するとコンソールを乗っ取る (Ctrl-C 無効 + 同じ線で sentinel protocol)。
だから **REPL のポートをそのままリンクへ渡す**。

```
connect_repl() -> run_and_release(repl, LAUNCH_SOURCE, read_timeout) -> SerialPort
  -> BuddyLink.open(adopt=...) / ResidentLink.connect(adopt=...)
```

`run_and_release` は `exec_raw_no_follow` を使う。`exec` は戻り値を待つが、アプリは
戻ってこない。これは `mpremote repl` の ctrl-k (スクリプト注入) と同じ手順。

閉じて開き直さないのは、その隙間にデバイスが喋る内容 — import 失敗のトレースバック —
を落とすため。

**起動後にポートへ何も書かないこと。** paste mode の頃は Ctrl-D が改行を持たないせいで
末尾に `\r\n` を足す必要があり、忘れると次の frame の先頭に紛れて sentinel が行頭で
なくなり、デバイスが黙って捨てて「起動直後の 1 リクエストだけ timeout」になっていた。
raw-paste は自分の terminator を ack してから実行に入るので、後始末は要らない。

## デバイスを触るときの前提

- **ポートは 1 プロセスしか掴めない。** `buddy_deploy.py` や `esptool` を使う前に MCP の
  `buddy_disconnect` を呼ぶ
- **アプリ起動は片道。** transport が上がると `micropython.kbd_intr(-1)` で Ctrl-C が
  無効になる。REPL に戻すには本体背面の BtnRST を押してもらう
- **MCP server はセッション開始時に host のコードを import 済み。** `buddy_bridge.py` を
  直しても走っているサーバには反映されない。実機検証は `uv run` の別プロセスで行うか、
  セッションを再起動する
- **WiFi は provisioning 済みなら何もしなくてよい。** 繋がらないときは
  `host/provision_wifi.py --verify` が どの層で切れているかを言う
- **アプリを起動し直すには実際に reboot する。** REPL から re-import すると
  `MemoryError: memory allocation failed` で落ちる。`enter_raw_repl(soft_reset=False)` を
  使っているため前のインスタンスが residual に残る
- `.mcp.json` と `.claude/settings.json` は絶対パスを持つ。別マシンでは書き換えが要る

## クレジット

VOICEVOX の利用規約に従い表記する。

- VOICEVOX:ずんだもん
- 「VOICEVOX」は廣芝和之の商標、「ずんだもん」は SSS 合同会社の商標
