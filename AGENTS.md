# AGENTS.md

M5Stack Cardputer-Adv を USB シリアル経由でコーディングエージェントから操作する実験
リポジトリ。Claude Code と Codex の両方から使える。
何ができるかと使い方は [README.md](README.md) にある。

## エージェント設定の正本

この `AGENTS.md` と `.agents/` を Claude Code / Codex 共通の正本とする。

- `CLAUDE.md` はこのファイルへのシンボリックリンク
- `.claude/skills` と `.claude/hooks` は `.agents/` 配下へのシンボリック
- スキルと hook 実装は `.agents/` 側だけを編集する
- `.claude/settings.json` / `.mcp.json` と `.codex/` は、製品ごとの設定形式を
  共通実装に接続する薄いアダプター

## 言語

これから書くコメント・docstring、コミットメッセージ、PR の本文は日本語 (標準語) で書く。
既存の英語コメントは触らない。当面は混在する。

## コマンド

```bash
uv sync --all-packages         # .venv を作る (全 member + dev グループ)
uv run ruff check              # lint。ルートから 1 回で全 member を見る
uv run ruff format             # format。同上
uv run python host/tools/src/buddy_deploy.py --compile-only   # device/ が MicroPython で通るか
uv run poe license                                            # 依存のライセンスを trivy の分類で検査する
```

タスクランナーは poethepoet。定義はルートの `pyproject.toml` の `[tool.poe.tasks]`、一覧は
`uv run poe --help`。CI に書くのはタスク名だけにして、コマンドと根拠は pyproject 側に置く。

`poe license` は `aqua exec` 越しに `trivy` を呼ぶ。バージョンは `aqua.yaml` が持つ。
`aqua exec` を挟むのは PATH に依存しないためで、`.envrc` が通す PATH は direnv を hook した
対話シェルにしか効かない (Claude Code の Bash からは見えない)。trivy は `.venv` の
site-packages を読むため、`uv sync` の後でないと何も検出しない。

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
Seatbelt はこれを拒否する。Claude Code 側は `.claude/settings.json` の
`sandbox.excludedCommands` に `uv *` を登録しているため、`uv run` は sandbox の外で
走る。sandbox 設定はセッションを再起動するまで反映されない。

## 構成

uv workspace の 4 member: `device/` (デバイスの上で動く overlay)、`host/link` (REPL transport
とクライアント)、`host/mcp` (MCP server)、`host/tools` (デプロイ・provisioning・実測・
チャットパネル用フォントの生成)。
overlay が何に何を重ねているかは [README の「overlay とは」](README.md#overlay-とは)。
ルートの `pyproject.toml` はパッケージではなく、member の列挙とツール設定だけを持つ。

- **lockfile も `.venv` も 1 つ。** 分かれるのは依存宣言とツール設定であって環境ではない。
  member 単体で足りることは CI の `isolation` ジョブが見る
- host の 3 つは `package = true`。member 間の import を workspace の依存として宣言するには
  installable である必要がある。`device` だけ `package = false`
- `host/link` は責務ごとの flat module。`buddy_wire` (framing と encode/decode)、
  `buddy_text` (パネルに載る形へ潰す。I/O を持たない)、`buddy_verbs` (chat / speech / debug の
  verb)、`buddy_link` (`BuddyLink` / `ResidentLink` / `launch_app`)、`device_repl`
  (raw REPL)。`buddy_bridge` はこれらを束ねる CLI だけ
- `vendor/device/` はデバイスから吸い出した upstream ソース。git 管理外だが、`tmp/` とは違って
  **消してはいけない** (再配布しないので他に控えが無い)
- `device/buddy/` は本リポジトリがデバイスへ載せるモジュールの package。flash では
  `/flash/buddy/` に落ちる。`buddy_protocol.py` / `buddy_ui_cp.py` / `buddy_state.py` /
  `buddy_chars.py` は upstream のものがデバイスの `/flash/` 直下にあり、本リポジトリには
  置かない。flash の階層がその境界になっている (地図は README の「overlay とは」)
- テストは全て実機不要。`device` の dev グループに host-link が入っているのは、
  `device/tests/test_chat.py` と `test_speak.py` が両側の定数を突き合わせる契約テストだから
- 依存を足したら `poe license` が弾くことがある。copyleft や分類不能のライセンスが来たら、
  除外に足す前にそれを使ってよいかを先に決める。GPL-2.0+ の `esptool` だけは既に除外済みで、
  理由は `[tool.poe.tasks.license]` の上に書いてある

## デバイスを触るときの前提

- **ポートは 1 プロセスしか掴めない。** `buddy_deploy.py` や `esptool` を使う前に MCP の
  `buddy_disconnect` を呼ぶ。このリポジトリの MCP server は `BUDDY_CONNECT_ON_START=1` で
  起動直後にポートを開く (chatter をセッションの最初から動かすため) ので、掴んでいる前提で
  考える。一度手放したポートを取り返す経路は無い
- **アプリ起動は片道ではない。** Ctrl-C は有効なままで、アプリがそれを捕まえて reboot せずに
  REPL で止まる。MCP なら `buddy_interrupt`、CLI なら `--interrupt`。REPL を要求するツールは
  自分で Ctrl-C を打ってから入る。BtnRST は、それでも応答しないときの最後の手段
- **走っているアプリは `buddy_debug` で覗ける。** `dbg.*` verb が既存のシリアル経路に乗る。
  デバイス側のモジュールは使うまで import されないので、覗いていない間の heap は減らない。
  初回の `dbg.*` でデバイスが喋る。詳細は `buddy-debug` skill
- **大きい出力は ack ではなく log に出る。** `dbg.frag` のヒープマップも traceback も
  `print()` 経由。ack の `ok: true` だけ見て終わらせない
- **MCP server はセッション開始時に host のコードを import 済み。** `host/` の下を
  直しても走っているサーバには反映されない。実機検証は `uv run` の別プロセスで行うか、
  セッションを再起動する
- **WiFi は provisioning 済みなら何もしなくてよい。** 繋がらないときは
  `host/tools/src/provision_wifi.py --verify` がどの層で切れているかを言う
- **チャットパネルの日本語フォントは flash に置いた VLW。** `/flash/buddy-ja.vlw` で、
  デプロイでは触らない (930KB あって毎回送る意味がない)。`loadFont` は失敗しても黙るので、
  効いているかは `--chat-info` の `vlw` で見る。無くても内蔵の 24px にフォールバックして
  動く。置き直しは `host/tools/src/make_vlw.py --port`。詳細は `buddy-device-code` skill
- **アプリを起動し直すには実際に reboot する。** REPL から re-import すると
  `MemoryError: memory allocation failed` で落ちる。`enter_raw_repl(soft_reset=False)` を
  使っているため前のインスタンスが residual に残る
- **chatter がデバイスを喋らせている。** MCP server の worker thread が hook 起点で独り言を
  言う。セッションを始めた時点でリンクが上がるので、何も呼ばなくても喋り出す。デバイスに
  触る tool は全て `_device_lock` を握ること — 握らずに request を出すと ack が入れ違う。
  詳細は `buddy-chatter` skill
- **接続元が Claude Code か Codex かで台詞を書く LLM が変わる。** tool は全て共通で、
  分かれるのは chatter の生成器だけ (`buddy_agent.py` / `RoutingLineSource`)。判定は MCP の
  `clientInfo` と hook の `--agent` から実行時に取る。デプロイ時に固定しない
- MCP と hook の実装は `.agents/` に共通化されている。製品別の登録は
  `.mcp.json` / `.claude/settings.json` / `.codex/` に残す。パスは Git ルートから解決するため、
  別の checkout で書き換える必要はない

## クレジット

VOICEVOX の利用規約に従い表記する。

- VOICEVOX:ずんだもん
- 「VOICEVOX」は廣芝和之の商標、「ずんだもん」は SSS 合同会社の商標
