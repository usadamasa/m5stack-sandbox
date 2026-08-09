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
| `device/apps/claude_buddy.py` | upstream 派生。差分は transport 選択のみ |
| `host/buddy_bridge.py` | ホスト側クライアント。`BuddyLink` (単発) と `ResidentLink` (常駐) |
| `host/buddy_mcp.py` | MCP server (8 tools) |
| `host/buddy_push.py` | paste-mode REPL 経由で overlay を転送 |
| `host/fetch_firmware.py` | UIFlow 2.0 ファームウェアの取得 (Range レジューム付き) |
| `host/tests/` | 全て実機不要 |

`buddy_protocol.py` / `buddy_ui_cp.py` / `buddy_state.py` / `buddy_chars.py` は upstream の
ものがデバイスの `/flash/` に入っている。このリポジトリには置かない。

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
