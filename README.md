# m5stack-sandbox

M5Stack Cardputer-Adv を Claude Code から双方向に操作するための実験リポジトリ。

Claude Buddy の BLE transport を USB シリアルに差し替えることで、Claude Desktop の
Hardware Buddy を経由せずに Claude Code (Vertex AI backend) からデバイスと通信する。

## 現在の状況

| フェーズ | 状態 |
| --- | --- |
| デバイスのプロビジョニング (UIFlow2.0 + buddy バンドル) | 完了 |
| Phase 1 — device 側 serial transport | 完了 |
| Phase 2 — host 側ブリッジ (MCP 非依存) | 完了 |
| Phase 3 — MCP server 化 | 未着手 |

動作確認済みの経路:

```
Claude Code ──Bash──> host/buddy_bridge.py ──USB CDC──> Cardputer-Adv
                                                         └ claude_buddy.py
                                                           └ buddy_serial.py
```

`status` / `name` / `owner` のラウンドトリップと、デバイス発の `hello` の受信を実機で確認済み。

未解決:

- **MCP server が Claude Code の sandbox 内で起動するかは未実測。** 公式ドキュメントは
  sandbox を Bash tool のものとしてのみ記述しており、MCP への言及がない。内側だった場合は
  `tcsetattr` が `EPERM` で落ちるため、Phase 3 の最初にここを確認する。
- ファイル push (`char_begin` / `file` / `chunk` 系) は使えない。`buddy_chars.py:136-141` が
  transport によらず無条件で拒否する。

## 構成

```
device/                     デバイスへ push する overlay
├── buddy_serial.py         シリアル transport (BuddyBLE を duck-typing)
└── apps/claude_buddy.py    upstream からの派生。差分は transport 選択のみ (46 行)
host/
├── buddy_bridge.py         ホスト側クライアント + CLI
└── tests/test_framing.py   フレーミングの単体テスト
PLAN.md                     フェーズ管理と、実装中に踏んだ罠の記録
```

`buddy_protocol.py` / `buddy_ui_cp.py` / `buddy_state.py` / `buddy_chars.py` は無改造で
upstream のものをそのまま使う。

## 前提環境

### upstream クローン

`build-with-claude/` に [moremas/build-with-claude](https://github.com/moremas/build-with-claude)
をクローンする。gitignore 済みで、submodule ではない。

```bash
git clone https://github.com/moremas/build-with-claude.git
```

このクローンには `buddy/scripts/push.py` (デバイスへの転送) と m5-onboard スキルが入っている。

> `.claude/skills/m5-onboard/scripts/fetch_firmware.py` にローカルパッチ (+112/-27) が
> 当たっている。sandbox の CONNECT proxy が大きな転送を切るため、`Range: bytes=N-` で
> レジュームするようにしたもの。`git pull` すると失われる。

### Python

```bash
uv venv tmp/m5venv && uv pip install --python tmp/m5venv/bin/python esptool pyserial
```

`pip` は使わないこと。sandbox 内では truststore が `trustd` に到達できず
`SSLCertVerificationError OSStatus -26276` で失敗する。`uv` は `excludedCommands` に
入っているので動く。

### sandbox 設定

`.claude/settings.json` を追跡しているのは、この設定がないとデバイスに触れないため。

| キー | 用途 |
| --- | --- |
| `sandbox.excludedCommands` | venv の python を sandbox 外で実行する。`tcsetattr` (ioctl) が Seatbelt で拒否されるため必須 |
| `sandbox.filesystem.allowWrite` | シリアルデバイスノードとファームウェアキャッシュ |
| `sandbox.network.allowedDomains` | M5Burner のマニフェストと CDN |

パスが絶対パスで書かれているため、別のマシンでは書き換えが要る。

## 使い方

```bash
PY=tmp/m5venv/bin/python
PORT=/dev/cu.usbmodem101

# デバイスへ overlay を転送
$PY build-with-claude/buddy/scripts/push.py --port $PORT \
    --src device --files buddy_serial.py apps/claude_buddy.py --no-reset

# アプリを起動して状態を取得
$PY host/buddy_bridge.py --port $PORT --start --status

# 走っているアプリへコマンドを送る
$PY host/buddy_bridge.py --port $PORT --name Mikawa --owner usadamasa --watch 5

# テスト
$PY -m unittest discover -s host/tests
```

`--start` は片道。アプリは transport 起動時に `micropython.kbd_intr(-1)` で Ctrl-C を
無効化するため、REPL に戻るには本体背面の BtnRST を押す。

## 既知の制約

- デバイスはバッテリー駆動で、USB を抜くと電源が落ちる。挿し直しただけでは起動しない
  ことがあるため、側面の電源ボタンを押す。
- `/dev/cu.*` が現れないときは、USB バス上に居るかを先に確認する。`ioreg -p IOUSB` に
  出ていて `IOUSBHostInterface` が 0 個なら、列挙はしているがインタフェースが構成されて
  いない中間状態で、電源の入れ直しで解消する。
