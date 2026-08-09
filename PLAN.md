# Cardputer-Adv ↔ Claude Code 双方向通信 (USB シリアル transport)

Claude Buddy の BLE transport を USB シリアルに差し替え、Claude Code (Vertex AI backend)
から双方向に通信できるようにする。

## 背景

`claude_buddy.py` の host 側は Claude Desktop の Hardware Buddy であり、Claude Code から
は利用できない。一方 `buddy_protocol.py` は transport に対して以下しか要求していないため、
BLE を差し替えれば protocol 層は無改造で再利用できる。

| 方向 | インタフェース |
| --- | --- |
| host → device | `on_line(raw: bytes)` コールバック |
| device → host | `send_line(payload: bytes) -> bool` |
| 付随 | `connected` / `encrypted` / `advertised_name` / `disconnect()` / `deinit()` |

## 方針

- 新規コードは本リポジトリ (m5stack-sandbox) に置く。`build-with-claude/` は upstream の
  クローンなので触らない。device 側の改変ファイルは overlay として持ち、`push.py` で流す。
- `buddy_protocol.py` / `buddy_ui_cp.py` / `buddy_state.py` / `buddy_chars.py` は無改造。
- 一番不確実な前提 (MCP が sandbox 外か) の検証はセッション再起動を伴うため最後に回し、
  それ以外を全て Bash 経由で検証してから着手する。

## 構成

```
device/                     デバイスへ push する overlay
├── buddy_serial.py         新規: シリアル transport
└── apps/claude_buddy.py    upstream からの派生 (~10行差分)
host/
├── buddy_bridge.py         ホスト側プロトコルクライアント (MCP 非依存)
├── buddy_mcp.py            MCP server ラッパー
└── tests/                  buddy_bridge のフレーミング単体テスト
```

## 設計上の決定事項

### 出力フレーミング

デバイスの stdout は `print()` によるデバッグログと共用されている。protocol 行は
sentinel 前置で区別する。host 側は前置のある行だけを protocol として解釈し、それ以外は
ログとして扱う。

### 入力と Ctrl-C

host → device は同じ USB CDC を使う。JSON 中の 0x03 が `KeyboardInterrupt` を誘発すると
アプリが落ちるため、transport 起動時に `micropython.kbd_intr(-1)` で無効化し、teardown で
復帰させる。この間 Ctrl-C による脱出は効かなくなる。

### connected / encrypted の意味論

`sec` フィールド (`buddy_protocol.py:258`) は `transport.encrypted` をそのまま流す。

**決定: `encrypted = False`。** `sec` が名指ししているのはリンクが暗号学的に保護されて
いるかであって、到達しにくいかどうかではない。USB CDC は平文であり、ケーブルの向こう側が
誰かをデバイスは認証できない。物理アクセスの要求は実在する障壁だが `sec` の意味とは別物で、
`True` を返すと host に対して `sec` が gate しているはずの挙動の緩和を許してしまう。

`connected` はホストからの最初の有効な sentinel 付き行で `True` になる。`pairing_supported`
を `False` にしてあるため、`claude_buddy.py:241` の既存分岐が `"connected"` を `"encrypted"`
へ読み替え、`send_hello()` まで到達する。BLE 非認証ビルド向けの経路をそのまま再利用する。

なお file push は `buddy_chars.py:136-141` が `encrypted` を見ずに**無条件で拒否**する。
transport を替えても自動では開かない。本 PLAN のスコープ外とする。

## フェーズ

### Phase 1 — device 側 transport

- [x] `device/buddy_serial.py` を実装 (`send_line` / `poll` / 各プロパティ)
- [x] `device/apps/claude_buddy.py` に transport 選択と `poll()` 呼び出しを追加
      (upstream からの差分 46 行)
- [x] `push.py` でデバイスへ流し込み、アプリが起動することを確認

### Phase 2 — host 側ブリッジ (MCP 非依存)

- [x] `host/tests/` にフレーミングの単体テストを先に書き、失敗を確認する
- [x] `host/buddy_bridge.py` を実装しテストを通す (12 件)
- [x] Bash 経由で実機ラウンドトリップ (`status` / `name` / `owner`) を確認

### Phase 3 — MCP server 化

- [x] `host/buddy_bridge.py` に `ResidentLink` を追加 (読み取り専用スレッド + バッファ)
      — MCP server は個々のツール呼び出しより長生きするため、呼び出しの合間に届く
      デバイス発のメッセージを取りこぼさない仕組みが要る
- [x] 偽シリアルによる `ResidentLink` の単体テスト (9 件)
- [x] `host/buddy_mcp.py` (8 tools) と `.mcp.json` を追加
- [ ] **セッション再起動後、`probe_serial` で `tcsetattr` が通るか実測する**
  - 通れば前提どおり。`EPERM` なら Bash 常駐ブリッジ + JSONL ファイル経由に切り替える

MCP SDK は v2.0.0 を使う。`mcp.server.fastmcp` は廃止されており、`MCPServer` を
`mcp.server.mcpserver` から import して `server.run("stdio")` で起動する。同期関数の
tool は `anyio.to_thread.run_sync` 経由で呼ばれる (`func_metadata.py:108`) ため、
ブロッキング I/O をそのまま書いてよい。

## 実装中に踏んだ罠

### MicroPython の bytearray はスライス削除ができない

`del buf[:n]` は CPython では通るが、MicroPython では
`TypeError: 'bytearray' object doesn't support item deletion` になる。ホスト側の
`ast.parse` も構文チェックも通ってしまうため、実機で初めて落ちる。末尾への再束縛
(`buf = buf[n:]`) で回避した。ホスト側の `LineDemux` は CPython なのでそのままでよい。

### `finally: machine.reset()` が診断を焼き払う

`claude_buddy.py` の `run()` は `finally` で `machine.reset()` を呼ぶ。メインループ内で
例外が出るとトレースバックが flush される前にリセットが走り、症状が「なぜか再起動する」に
しか見えない。診断するには `machine` を差し替える:

```python
import sys, machine as _real
class _Shim:
    def __getattr__(self, k): return getattr(_real, k)
    def reset(self): print('RESET-SUPPRESSED')
sys.modules['machine'] = _Shim()
import claude_buddy   # 例外がそのまま上がってくる
```

`machine` は frozen module なので `machine.reset = ...` は `AttributeError` になる。
`sys.modules` への差し込みが要る。

### バッファされたログは時系列を潰す

`buddy_serial: down` を起動直後の出力だと読み違えて、原因をアプリ起動時だと誤断した。
実際は `finally` の出力で、コマンド送信の後だった。まとめて dump するとこの取り違えが
起きるため、`start_app` はエコーを捨てず全て保持する実装にしてある。

## 検証済みの前提

- `tcsetattr` は `sandbox.excludedCommands` により Bash 経由で通る (本セッションで実測)
- REPL ラウンドトリップは成立する (MicroPython 1.27.0 / M5STACK_CardputerADV で実測)
- MCP が sandbox 外であることは**ドキュメントからの推論のみ**。Phase 3 で実測する
