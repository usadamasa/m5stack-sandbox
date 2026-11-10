---
name: buddy-module-split
description: host/ の大きいモジュールを割る、関数を別モジュールへ移すときに使う。
---

# モジュールを割る

割ったときに壊れるのは、テストの差し替え先と、daemon に載ってから初めて出る import の誤り。
どちらもテストが緑のまま起きうる。

## mock の patch 先を呼ぶ側へ向け直す

`mock.patch.object(元モジュール, "名前")` は、名前が移動先へ移ると呼ぶ側の呼び出しを**差し替えなくなる**。
呼ぶ側が引くのは移動先モジュールのグローバルで、元モジュールの属性ではない。
例外にはならず、呼ぶ側が本物を呼ぶ (`subprocess` なら本物の `claude -p`、HTTP なら本物の取得)。
sandbox の許可を通れば外へ出ていき、テストは assert のずれとしてしか落ちない。

- patch 先は、その名前を**呼ぶ側のモジュール**にする。
  例: `buddy_chatter.subprocess` ではなく `chatter_lines.subprocess`
- 移した名前の import は元モジュールから**消す**。re-export を残すと、呼ぶ側へ向け直していない patch が
  黙って効かなくなる。消せば `AttributeError` で落ちて見つかる
- 割った後は pytest の所要時間を前と比べる。patch が効いていなければ秒単位で伸びる

## モジュールをまたぐ名前は public にする

basedpyright は `strict` で、`src` 側では `reportPrivateUsage` が効く (無効なのは
`tests` の execution environment だけ)。別モジュールから呼ばれるようになった `_` 付きの
関数は、割るときに `_` を外す。

## chatter を割ったら実機で 2 段に確かめる

daemon は起動時に import 済みのコードで動くので、分割の import の誤りは restart するまで
出ない。PR を出す前に 1 → 2 の順で回し、結果を PR 本文に書く。手順の土台
(`chatter_cli.py`、hook の手打ち、log の読み方) は [buddy-chatter](../buddy-chatter/SKILL.md) にある。

1. **単体**: `buddy-mcpd stop` してから
   `uv run python host/mcp/src/chatter_cli.py --port $PORT --once`。`spoken: 1` と
   `generated` の件数が出れば、生成から `chat.say` / speak までが新しい import で通っている
2. **daemon**: `buddy-mcpd restart` してから
   `echo '{"hook_event_name":"Stop"}' | python3 scripts/buddy_chatter_notify.py` を流し、
   次の発話 (`next_gap_s` 秒後) を待つ。確かめるのは log:

   ```bash
   rg 'buddy.chatter:' ~/.local/state/buddy/buddy-mcpd.log
   ```

   `said "…" (voice=yes, next 131s)` の行が出れば喋っている

1 で `no 'chat.say' ack within 5.0s` が出ても、bridge の `--speak` が通るならデバイスとリンクは
生きている。もう一度回す。

daemon が見るのは本体 checkout なので、worktree の変更は 2 に載らない
([CLAUDE.md](../../CLAUDE.md) の「常駐 daemon」)。worktree では 1 だけを回し、2 はマージ後に本体で回す。
`project.scripts` を変えたときだけ `uv tool install --force --editable ./host/mcp` で入れ直す。

### log で足りないとき

`spoken` の数を数えたいときは `buddy_chatter_status` を HTTP で呼ぶ。
`uv run --directory host/mcp python <script>` で実行すれば、script は sandbox の外で動くので loopback に届く。
API の形は `uv.lock` が固定する mcp のバージョンのもので、上げたら `.venv` の mcp で確かめ直す。

- client は `mcp.client.streamable_http.streamable_http_client(url)` で、
  `(read, write)` の 2 要素を返す
- 結果の構造化された中身は `result.structured_content`
