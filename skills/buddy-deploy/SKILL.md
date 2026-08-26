---
name: buddy-deploy
description: Cardputer-Adv へコードを転送する、REPL でデバイスを操作する、WiFi を provisioning するときに使う。.mpy が必須である理由と mpy-cross の ABI ピン、mpremote を使う raw REPL の作法、reset 前後のタイミングの罠を扱う。buddy_deploy.py / provision_wifi.py / device_repl.py を触るとき、デプロイが失敗したとき、デバイスが REPL に戻らないときに参照する。
---

# デバイスへ載せる / REPL で操作する

## 転送の入口は 1 つ

```bash
PORT=/dev/cu.usbmodem101
uv run python host/tools/src/buddy_deploy.py --port $PORT              # 転送 + 起動 + 発話で確認
uv run python host/tools/src/buddy_deploy.py --port $PORT --no-speak   # 発話を省き REPL に残す
uv run python host/tools/src/buddy_deploy.py --compile-only            # 実機なし。CI の mpy-build と同じ
```

`.py` を push する経路はリポジトリに無い。増やしてもいけない。

**何を上書きし、何を残し、何を消すのか**は README の
[overlay とは](../../../README.md#overlay-とは) に表がある。以下はその境界を保つための
運用のきまり。

## 転送で守ること

- **自前のモジュールは `/flash/buddy/` に置く。** flash 直下は firmware と upstream のもの
  (`buddy_protocol` / `buddy_ui_cp` / `buddy_state` / `buddy_chars`) で、階層がその境界に
  なっている。`buddy/__init__.mpy` は中身が無くても必ず push する — MicroPython に
  namespace package は無く、無ければ `/flash/buddy` はただのディレクトリで
  `from buddy import ...` が全部 ImportError になる
- **MicroPython は submodule を親 package の属性にも入れる。** `sys.modules` から消すだけでは
  そちらに参照が残り、モジュールは heap に居座る。`delattr(sys.modules["buddy"], "debug")`
  まで要る (実測。module オブジェクトへの `delattr` は効き、次の `from buddy import debug` は
  flash を読み直す)。`device/tests/test_debug.py` の `CallerUnloadTest` が両方を固定している
- **レイアウトを変えたら flash の置き土産を消す。** import されないバイトコードが残るし、
  古い `sys.path` から解決されうる。`buddy_deploy.STALE` がその一覧で、`REMOVE` と違って
  `vendor/` へは退避しない (自前のモジュールなので git に控えがある)
- **`.py` を置かない。** import 機構は各 `sys.path` エントリで `foo.py` を `foo.mpy` より
  先に探すので、ソースを置くとバイトコードが読まれなくなる。デバイスは import のたびに
  構文木とバイトコードの両方を GC heap に作り、`gc.mem_free()` が 55280 あっても
  `import buddy_ui_cp` が 776 バイトを取れずに落ちる (総量ではなく連続領域)。`.mpy` 化で
  clean GC heap 55280 → 101120、ESP-IDF DATA の最大連続 17408 → 51200。`.py` で push すると
  黙って元に戻り、症状は数日後の合成失敗として出る
- **`mpy-cross` は `==1.27.0.post2` 固定。** バイトコードは同じ `.mpy` ABI の中でしか通用せず、
  デバイス (MicroPython 1.27) が読むのは v6。ずれたときの症状はデバイス側の素の ImportError
  だけになるので、転送前に `sys.implementation._mpy` と突き合わせている。ピンは
  `host/tools/pyproject.toml` と `buddy_deploy.MPY_CROSS_ABI` の 2 箇所にあり、テストが一致を見る
- **消す前に必ず `vendor/device/` へ退避する。** upstream のピア
  (`buddy_protocol` / `buddy_ui_cp` / `buddy_state` / `buddy_chars`) は本リポジトリに無く
  (NOTICE のとおり再配布しない)、`.py` を消した後はそこが唯一の控えになる。`vendor/` は
  `.gitignore` に入っているが `tmp/` とは違って消してはいけない
- **`main.py` だけはソースのまま。** MicroPython は `/flash/main.py` を実行し、`main.mpy` を
  探さない。`--compile-only` はパーサに通すためだけにコンパイルし、結果は push 対象と別の
  ディレクトリに落とす
- **`main.py` はブート時にアプリを起動する。** WiFi を上げてから `claude_buddy` を import
  して `run()` を呼ぶところまでが `/flash/main.py` の仕事で、`buddy_link.LAUNCH_SOURCE` と
  同じ 4 手 (`sys.path` へ `/flash` と `/flash/apps`、`gc.collect()`、import、`run()`) を
  踏む。import しただけでは起動しない — あれは `buddy/app.py` へ渡す起動口。両者が揃って
  いることは `device/tests/test_boot.py` が見る。だから REPL を要求する側は、デバイスが
  REPL に居ることを前提にしてはいけない — ハンドシェイクの Ctrl-C で取り返す
- **タイムアウトはスクリプトの中。** `mpremote` はポートを `timeout=None` で開き、
  `raw_paste_write` は素の `serial.read(1)` で待つので、応答が止まると永久にブロックする。
  `bash` の `timeout` で包まない。ポートへ有限の read timeout を掛け、`--timeout` の予算を
  ステップ間で見て、落ちたステップ名を出す
- **転送の最後にアプリを起動して喋らせる。** ファイルが載ったことはバンドルが動くことを
  意味しない。import 失敗も engine 不達も speaker の沈黙もディレクトリ一覧からは区別できない。
  `verify_by_speech` が import・継承した WiFi link・VOICEVOX 往復・`M5.Speaker` を一度に通す。
  engine の URL はランチ前に解決する (後で分かってもポートはもう返らない)。失敗は exit 1 で、
  断った層を名指しする
- **デプロイ後、デバイスはアプリを走らせたままになる。** 次のデプロイは Ctrl-C で
  そのアプリを畳んでから始まる。転送せず REPL に残したいときだけ `--no-speak`

## REPL は mpremote に任せる

ファイル転送・コード実行・値の読み出しは MicroPython 公式の `mpremote` を
**ライブラリとして**使う。CLI は叩かない。入口は `host/link/src/device_repl.py` の
`connect_repl()` だけで、`Repl` protocol が使っている API の範囲を記述している。

**paste mode (Ctrl-E / Ctrl-D) を自前で駆動しない。** 以前そうしていて踏んだこと:

- paste mode にはフロー制御が無い。長い転送がデバイスの rx を追い越して**無言で切れる**。
  固定チャンク長と 1 行ごとの `sleep` はその場しのぎだった
- paste mode はソースをエコーバックする。結果を読むのに自分が送った `print(...)` を
  正規表現の negative lookahead で除外する羽目になっていた

raw REPL ならどちらも無い。`repl.exec()` は完走するか例外を投げるかで、`repl.eval()` は
Python の値をそのまま返す (デバイス側で `print(repr(...))` してホストで `literal_eval`)。

- **`enter_raw_repl(soft_reset=False)` を使う。** 既定の `soft_reset=True` は
  boot.py / main.py を走らせ直す。`main.py` はアプリを起動するので、せっかく Ctrl-C で
  取り返した REPL をその場で手放すことになる
- **待ちのループだけは自前。** `mpremote` の `wait=` は `open()` の失敗しかリトライしない。
  ポートは開くのに応答しない状態 — アプリの teardown 中、あるいは Python の下で刺さった
  デバイス — を待てない。かつてはアプリが `kbd_intr(-1)` で Ctrl-C を殺していたのでこれが
  常態だったが、今は Ctrl-C が効くので通常はハンドシェイクだけで REPL に入る

## アプリの起動 (`launch_app`)

アプリは起動するとコンソールを乗っ取る (同じ線で sentinel protocol を喋る)。
だから **REPL のポートをそのままリンクへ渡す**。

```
connect_repl() -> run_and_release(repl, LAUNCH_SOURCE, read_timeout) -> SerialPort
  -> BuddyLink.open(adopt=...) / ResidentLink.connect(adopt=...)
```

- `run_and_release` は `exec_raw_no_follow` を使う。`exec` は戻り値を待つが、アプリは
  戻ってこない。`mpremote repl` の ctrl-k (スクリプト注入) と同じ手順
- 閉じて開き直さないのは、その隙間にデバイスが喋る内容 — import 失敗のトレースバック —
  を落とさないため
- **起動後にポートへ何も書かない。** paste mode の頃は Ctrl-D の後に `\r\n` を足す必要が
  あり、忘れると次の frame の先頭に紛れて sentinel が行頭でなくなり、「起動直後の
  1 リクエストだけ timeout」になっていた。raw-paste は自分の terminator を ack してから
  実行に入るので後始末は要らない

## provisioning で踏んだこと

- **reset 直後に REPL を掴みに行ってはいけない。** `machine.reset()` の直後から poll すると
  90 秒経っても raw REPL に入れない。25 秒待ってから 1 回試すと 0.1 秒で入る。ブート中の
  ハンドシェイク試行がデバイスを戻らなくするらしい。`_SETTLE_S` はこれ
- **reset 後の `repl.close()` は必ず失敗する。** mpremote が RTS を落としに行き、消えた
  デバイスの fd に ioctl して `OSError: [Errno 6] Device not configured`。異常ではない
- `main.py` が動いている間でも BtnRST 無しで raw REPL に入れる (落ち着いた後なら 0.1 秒)。
  そこで割り込んでも既に完了した association は落ちない
