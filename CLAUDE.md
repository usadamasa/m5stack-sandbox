# CLAUDE.md

M5Stack Cardputer-Adv を USB シリアル経由で Claude Code から操作する実験リポジトリ。
経緯と設計判断は [README.md](README.md) にある。

## コマンド

```bash
uv sync                        # .venv を作る (dev グループ込み)
uv run pytest                  # テスト
uv run pytest --cov            # カバレッジ付き
uv run ruff check              # lint
uv run ruff format             # format
uv run basedpyright            # 型検査
```

デバイス操作は必ず `uv run` を通す。

```bash
PORT=/dev/cu.usbmodem101
uv run python host/buddy_push.py --port $PORT              # overlay を転送
uv run python host/buddy_bridge.py --port $PORT --status   # 単発で叩く
```

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
| `device/buddy_speak.py` | ホストから流れてくる PCM を `M5.Speaker` へ流す。`speak.*` |
| `device/apps/claude_buddy.py` | upstream 派生。差分は transport 選択と chat / speak のルーティング |
| `host/buddy_bridge.py` | ホスト側クライアント。`BuddyLink` (単発) と `ResidentLink` (常駐) |
| `host/buddy_speech.py` | macOS `say` で 16kHz 16bit mono PCM を作る |
| `host/buddy_mcp.py` | MCP server (12 tools) |
| `host/buddy_push.py` | paste-mode REPL 経由で overlay を転送 |
| `host/probe_device.py` | 実機のフォント一覧・実測メトリクス・Speaker API を取る (read-only) |
| `host/fetch_firmware.py` | UIFlow 2.0 ファームウェアの取得 (Range レジューム付き) |
| `host/tests/` | 全て実機不要 |

`buddy_protocol.py` / `buddy_ui_cp.py` / `buddy_state.py` / `buddy_chars.py` は upstream の
ものがデバイスの `/flash/` に入っている。このリポジトリには置かない。

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

合成はホストの macOS `say` が行う。デバイス側は PCM を鳴らすだけ。Cardputer-Adv は
ESP32-S3 で、M5Stack の on-device TTS (StackFlow の MeloTTS) は別基板の Module LLM
(AX630C, Linux) が要る。Espressif の `esp-tts` は中国語のみ。

**`say` は sandbox の中では無音のファイル (4096 バイトのヘッダのみ) を exit 0 で吐く。**
`uv run` 経由なら sandbox の外なので通る。`buddy_speech.synthesize` はこのサイズを見て
エラーにしている (無音をそのまま流すと転送失敗と区別がつかなくなるため)。

### バルクモード

音声は JSON に乗せない。`speak.begin` で長さを宣言 → transport が bulk モードへ →
生バイトを流す、という経路になっている。理由:

- `poll()` の行読みは **1 バイトずつ**。実測 24.5 KiB/s で、16kHz 16bit の 32 KB/s に
  届かない。速くできない理由は、行は長さが分からないから
- `readinto` は **バッファが埋まるまでブロックする** (1024 バイトのバッファに 100 バイト
  だけ送ったら 41 秒待った)。長さが既知のときだけ安全に使える。実測 182 KiB/s

そのため送信側の制約が2つある。破ると BtnRST でしか戻れない:

- **必ずブロック単位で送る。** 端数はホスト側 (`pad_to_blocks`) が無音で埋める。
  半端なブロックはデバイスを `readinto` の中で待たせたまま固める
- **`speak.begin` の ack を待ってから書く。** ack がバルクモードへの切り替えそのもの

ブロックは 2048 バイト固定。40ms の tick で 1 ブロックずつしか読まないので、これより
小さいと再生が追いつかない (`_MIN_BLOCK`)。`speak.end` の `stalls` が 0 以外なら
供給が間に合っていない。

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

## デバイスを触るときの前提

- **ポートは 1 プロセスしか掴めない。** `buddy_push.py` や `esptool` を使う前に MCP の
  `buddy_disconnect` を呼ぶ
- **アプリ起動は片道。** transport が上がると `micropython.kbd_intr(-1)` で Ctrl-C が
  無効になる。REPL に戻すには本体背面の BtnRST を押してもらう
- **MCP server はセッション開始時に host のコードを import 済み。** `buddy_bridge.py` を
  直しても走っているサーバには反映されない。実機検証は `uv run` の別プロセスで行うか、
  セッションを再起動する
- `.mcp.json` と `.claude/settings.json` は絶対パスを持つ。別マシンでは書き換えが要る
