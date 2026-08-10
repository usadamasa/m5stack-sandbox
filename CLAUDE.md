# CLAUDE.md

M5Stack Cardputer-Adv を USB シリアル経由で Claude Code から操作する実験リポジトリ。
何ができるかと使い方は [README.md](README.md) にある。

## 言語

これから書くコメント・docstring、コミットメッセージ、PR の本文は日本語 (標準語) で書く。
既存の英語コメントは触らない。当面は混在する。

## コマンド

```bash
uv sync --all-packages         # .venv を作る (全 member + dev グループ)
uv run ruff check              # lint。ルートから 1 回で全 member を見る
uv run ruff format             # format。同上
uv run python host/tools/src/buddy_deploy.py --compile-only   # device/ が MicroPython で通るか
```

pytest と basedpyright は **member ごと**に回す。どちらも 1 プロセス 1 プロジェクトで、
member の設定を読むにはそこで動かすしかない (`device` / `host/link` / `host/mcp` /
`host/tools`)。

```bash
uv run --directory host/link pytest
uv run --directory host/link pytest --cov
uv run --directory host/link basedpyright
```

デバイス操作は必ず `uv run` を通す。

```bash
docker compose up -d                                            # VOICEVOX ENGINE
PORT=/dev/cu.usbmodem101
uv run python host/tools/src/buddy_deploy.py --port $PORT       # 転送 + 起動 + 発話で確認
uv run python host/link/src/buddy_bridge.py --port $PORT --status
uv run python host/link/src/buddy_bridge.py --port $PORT --speak 'ずんだもんなのだ'
```

**`uv run` を経由する理由**: シリアルポートを開くには `tcsetattr` (ioctl) が要り、
Seatbelt はこれを拒否する。グローバル設定の `sandbox.excludedCommands` に `uv *` が
入っているため `uv run` は sandbox の外で走る。`.venv/bin/python` を直接叩く経路も
`.claude/settings.json` に登録してあるが、sandbox 設定はセッションを再起動するまで
反映されないため、設定を変えた直後は `uv run` を使う。

## 構成

uv workspace の 4 member: `device/` (デバイスの上で動く overlay)、`host/link` (REPL transport
とクライアント)、`host/mcp` (MCP server)、`host/tools` (デプロイ・provisioning・実測)。
ルートの `pyproject.toml` はパッケージではなく、member の列挙とツール設定だけを持つ。

- **lockfile も `.venv` も 1 つ。** 分かれるのは依存宣言とツール設定であって環境ではない。
  member 単体で足りることは CI の `isolation` ジョブが見る
- host の 3 つは `package = true`。member 間の import を workspace の依存として宣言するには
  installable である必要がある。`device` だけ `package = false`
- `vendor/device/` はデバイスから吸い出した upstream ソース。git 管理外だが、`tmp/` とは違って
  **消してはいけない** (再配布しないので他に控えが無い)
- `buddy_protocol.py` / `buddy_ui_cp.py` / `buddy_state.py` / `buddy_chars.py` は upstream の
  ものがデバイスの `/flash/` にあり、本リポジトリには置かない
- テストは全て実機不要。`device` の dev グループに host-link が入っているのは、
  `device/tests/test_chat.py` と `test_speak.py` が両側の定数を突き合わせる契約テストだから

## デバイスを触るときの前提

- **ポートは 1 プロセスしか掴めない。** `buddy_deploy.py` や `esptool` を使う前に MCP の
  `buddy_disconnect` を呼ぶ
- **アプリ起動は片道ではない。** Ctrl-C は有効なままで、アプリがそれを捕まえて reboot せずに
  REPL で止まる。MCP なら `buddy_interrupt`、CLI なら `--interrupt`。REPL を要求するツールは
  自分で Ctrl-C を打ってから入る。BtnRST は、それでも応答しないときの最後の手段
- **走っているアプリは `buddy_debug` で覗ける。** `dbg.*` verb が既存のシリアル経路に乗る。
  デバイス側のモジュールは使うまで import されないので、覗いていない間の heap は減らない。
  初回の `dbg.*` でデバイスが喋る。詳細は `buddy-debug` skill
- **大きい出力は ack ではなく log に出る。** `dbg.frag` のヒープマップも traceback も
  `print()` 経由。ack の `ok: true` だけ見て終わらせない
- **MCP server はセッション開始時に host のコードを import 済み。** `buddy_bridge.py` を
  直しても走っているサーバには反映されない。実機検証は `uv run` の別プロセスで行うか、
  セッションを再起動する
- **WiFi は provisioning 済みなら何もしなくてよい。** 繋がらないときは
  `host/tools/src/provision_wifi.py --verify` がどの層で切れているかを言う
- **アプリを起動し直すには実際に reboot する。** REPL から re-import すると
  `MemoryError: memory allocation failed` で落ちる。`enter_raw_repl(soft_reset=False)` を
  使っているため前のインスタンスが residual に残る
- **chatter がデバイスを喋らせている。** MCP server の worker thread が hook 起点で独り言を
  言う。デバイスに触る tool は全て `_device_lock` を握ること — 握らずに request を出すと
  ack が入れ違う。詳細は `buddy-chatter` skill
- `.mcp.json` と `.claude/settings.json` は絶対パスを持つ。別マシンでは書き換えが要る

## クレジット

VOICEVOX の利用規約に従い表記する。

- VOICEVOX:ずんだもん
- 「VOICEVOX」は廣芝和之の商標、「ずんだもん」は SSS 合同会社の商標
