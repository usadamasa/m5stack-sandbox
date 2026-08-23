# CLAUDE.md

M5Stack Cardputer-Adv を USB シリアル経由でコーディングエージェントから操作する実験
リポジトリ。何ができるかと使い方は [README.md](README.md) にある。

## このリポジトリは plugin でもある

ルートが Claude Code plugin のルートを兼ねる。MCP の登録・chatter の hook・
デバイスを触る skill は plugin として配られ、常駐 daemon は
`uv tool install ./host/mcp` で入る別物として動く。

| 置き場 | 中身 |
| --- | --- |
| `.claude-plugin/plugin.json` | plugin のマニフェスト |
| `.claude-plugin/marketplace.json` | 配布用のカタログ |
| `skills/` | デバイスを触るときの skill |
| `hooks/hooks.json` | chatter へ作業イベントを流す hook の登録 |
| `scripts/buddy_chatter_notify.py` | その hook 本体 |
| `mcp-servers.json` | 常駐 daemon への HTTP 登録 |
| `.claude/settings.json` | sandbox と permission だけ |

**このリポジトリで開発するときは `claude --plugin-dir .` で起動する。**
marketplace 経由で入れた plugin はキャッシュへコピーされるため、
working tree の編集が届かない。

**`.claude/settings.json` に hook を書き足さない。** hook は plugin 側の
`hooks/hooks.json` が正本で、そこに置くから他のプロジェクトへ付いていく。

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
uv run poe lines                                              # 行数のラチェット。しきい値超えのファイルが増えていないか
uv run poe lines --update                                     # 縮んだぶんを baseline へ取り込む
uv run poe metrics                                            # 複雑度・凝集度・結合度・循環依存
```

`poe lines` のしきい値は 400 行。超えたファイルは `file-length-baseline.json` に
現在値で載り、そこから増やせない。減らしたぶんは `--update` が取り込んで baseline が
下がる。上げる方向へは動かないので、baseline に並ぶ行数がそのままリファクタリングの
backlog になる。新しく超えたファイルを意図して受け入れるときだけ `--adopt`。

`poe metrics` は関数ごとの循環的複雑度、モジュールの凝集度 (定義が参照でいくつの塊に
分かれるか)、コンポーネント間の結合度 (Ca/Ce/instability)、依存の循環を出す。落ちるのは
循環依存があるときだけで、残りは並べるだけ。判定を持っているのは ruff (C901) と
行数のラチェット。

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

**sandbox の外に出る必要がある理由**: シリアルポートを開くには `tcsetattr` (ioctl) が
要り、Seatbelt はこれを拒否する。`.claude/settings.json` の
`sandbox.excludedCommands` に `uv *` / `uvx *` / `buddy-mcpd *` を登録してあるため、
この 3 つは sandbox の外で走る。sandbox 設定はセッションを再起動するまで反映されない。

AF_UNIX socket の bind も同じく拒否されるので、**テストも `uv run` を通す**。
素の `pytest` だと chatter の socket テストだけが `PermissionError` で落ちる。

## 常駐 daemon

MCP server は 1 プロセスの常駐 daemon で、複数のセッションが HTTP で繋ぐ。
デバイスに繋がるのはこの daemon だけ。

```bash
uv tool install --force --editable ./host/mcp   # buddy-mcp / buddy-mcpd を入れる
buddy-mcpd start                     # 起こす。ポートを掴んで chatter が動き出す
buddy-mcpd status                    # pid・serve している URL・log の場所
buddy-mcpd restart                   # host/ を直したときはこれだけ
buddy-mcpd stop                      # deploy や esptool の前に
```

- **`--editable` を落とさない。** これが無いと `uv tool install` はその時点の
  コピーを入れるので、`host/` を直しても restart では反映されず、毎回入れ直す
  ことになる。editable なら working tree をそのまま見る
- **ホスト側のコードを直したら `buddy-mcpd restart`。** セッションの再起動は要らない。
  daemon は import 済みのコードで動き続けるので、restart しない限り反映されない
- **HTTP は `127.0.0.1:8787` 固定**。`mcp-servers.json` が静的な URL を持つので、
  `config.toml` で `http_port` を変えたら登録側も直す。ずれは `buddy-mcpd status` の
  `url` に出る
- 落ちた理由は `~/.local/state/buddy/buddy-mcpd.log` にある

## 設定と状態の置き場

XDG に従う。`$XDG_CONFIG_HOME/buddy/config.toml` (既定 `~/.config/buddy/`) が設定、
`$XDG_STATE_HOME/buddy/` (既定 `~/.local/state/buddy/`) に pid・log・chatter の socket。

`config.toml` のキーは `BUDDY_*` 環境変数へそのまま写る。優先順位は
**環境変数 > config.toml > 既定値**。

```toml
port = "/dev/cu.usbmodem101"   # BUDDY_PORT
connect_on_start = true        # BUDDY_CONNECT_ON_START。daemon では既定で on

[chatter]
gap_min = 40.0                 # BUDDY_CHATTER_GAP_MIN
model = "sonnet"               # BUDDY_CHATTER_MODEL
```

socket のパスだけは `config.toml` に置かない。hook が system の `python3` で
毎回のツール呼び出しに乗るため、そこで TOML を読ませない。環境変数
(`BUDDY_CHATTER_SOCKET`) と XDG 既定値だけで解決する。

**socket を書くには sandbox の許可が要る。** `sandbox.network.allowUnixSockets` に
`~/.local/state/buddy` が無いと hook の `sendto` が EPERM で落ちる。AF_UNIX への接続は
Seatbelt では filesystem ではなく network の operation なので、`allowWrite` だけでは
通らない (`allowWrite` は daemon が pid・log・socket を書くために別途要る)。hook は
失敗を握り潰して exit 0 するので、症状は「独り言を言わない」だけになる。plugin は
sandbox 設定を配れないため、入れる側の `.claude/settings.json` に足す仕事になる。

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

- **ポートは 1 プロセスしか掴めない。** 掴んでいるのは常駐 daemon で、起動直後に
  ポートを開く (chatter を最初から動かすため)。`buddy_deploy.py` や `esptool` を使う前に
  MCP の `buddy_disconnect` を呼ぶか `buddy-mcpd stop`。一度手放したポートを
  取り返す経路は無い
- **アプリ起動は片道ではない。** Ctrl-C は有効なままで、アプリがそれを捕まえて reboot せずに
  REPL で止まる。MCP なら `buddy_interrupt`、CLI なら `--interrupt`。REPL を要求するツールは
  自分で Ctrl-C を打ってから入る。BtnRST は、それでも応答しないときの最後の手段
- **走っているアプリは `buddy_debug` で覗ける。** `dbg.*` verb が既存のシリアル経路に乗る。
  デバイス側のモジュールは使うまで import されないので、覗いていない間の heap は減らない。
  初回の `dbg.*` でデバイスが喋る。詳細は `buddy-debug` skill
- **大きい出力は ack ではなく log に出る。** `dbg.frag` のヒープマップも traceback も
  `print()` 経由。ack の `ok: true` だけ見て終わらせない
- **daemon は起動時に host のコードを import 済み。** `host/` の下を直しても
  走っている daemon には反映されない。`buddy-mcpd restart` する
  (セッションの再起動は要らない)
- **WiFi は provisioning 済みなら何もしなくてよい。** 繋がらないときは
  `host/tools/src/provision_wifi.py --verify` がどの層で切れているかを言う
- **チャットパネルの日本語フォントは flash に置いた VLW。** `/flash/buddy-ja.vlw` で、
  デプロイでは触らない (930KB あって毎回送る意味がない)。`loadFont` は失敗しても黙るので、
  効いているかは `--chat-info` の `vlw` で見る。無くても内蔵の 24px にフォールバックして
  動く。置き直しは `host/tools/src/make_vlw.py --port`。詳細は `buddy-device-code` skill
- **アプリを起動し直すには実際に reboot する。** REPL から re-import すると
  `MemoryError: memory allocation failed` で落ちる。`enter_raw_repl(soft_reset=False)` を
  使っているため前のインスタンスが residual に残る
- **chatter がデバイスを喋らせている。** daemon の worker thread が hook 起点で独り言を
  言う。daemon が上がった時点でリンクも上がるので、何も呼ばなくても喋り出す。デバイスに
  触る tool は全て `_device_lock` を握ること — 握らずに request を出すと ack が入れ違う。
  台詞は `claude -p` が書く。詳細は `buddy-chatter` skill
- **chatter は daemon に 1 つ。** どのセッションの hook で撃たれても同じ chatter が
  反応する。複数セッションが同時に繋がっているときも、喋る口は 1 つ

## クレジット

VOICEVOX の利用規約に従い表記する。

- VOICEVOX:ずんだもん
- 「VOICEVOX」は廣芝和之の商標、「ずんだもん」は SSS 合同会社の商標
