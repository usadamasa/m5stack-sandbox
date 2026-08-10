---
name: buddy-device-code
description: device/ 配下のコードを書く・直すときに使う。MicroPython 1.27 (ESP32-S3) 向けに書く際の当リポジトリの決めごとと、LCD のチャットパネル (buddy_chat.py) のフォント・行数・描画の制約を扱う。
---

# device/ は MicroPython

CPython ではなく MicroPython 1.27 (ESP32-S3) で動く。言語・標準ライブラリの差は公式の
[MicroPython differences from CPython](https://docs.micropython.org/en/latest/genrst/index.html)
を見る。

このリポジトリで実際に踏んだ差は `device/tests/test_device_constraints.py` が AST で機械的に
弾いている。**制約を足すときはそのテストに足す。** どれも CPython でも ruff でも型検査でも
通ってしまい、実機で初めて落ちる類なので、レビューでは捕まらない。

`device/apps/claude_buddy.py` は upstream 派生なので `ruff format` の対象外にしてある。
差分を読める状態に保つのが目的で、import 順や E402 も同じ理由で無視している。

実機なしで検証できる:

```bash
uv run --directory device pytest
uv run python host/tools/src/buddy_deploy.py --compile-only   # MicroPython のパーサに通す
```

# チャットパネル (`buddy_chat.py`)

`chat.say` / `chat.clear` / `chat.info` は upstream の `buddy_protocol.py` が知らない verb。
知らない `cmd` は "unknown cmd" と印字して捨てられるので、`claude_buddy.py` の `on_line` で
先に横取りしてから proto へ流す。**ここが upstream ファイルを触らずに protocol を拡張できる
唯一の場所。**

画面まわりで踏みやすい点:

- 表示中は `y=0..110` を chat が占有する。`BuddyUI.update_footer()` は `y=96..110` を塗るので
  `chat.active` の間は呼ばない。`set_connection()` は main panel も塗るので、呼んだら
  `chat.render()` で描き直す
- **日本語フォントは 24px しかない** (`EFontJA24` / `AlibabaSansJA24`、`fontHeight()` は 27)。
  中身に幅広文字があるかでフォントを切り替えており、日本語だと 4 行 × 9 文字、ASCII だけなら
  `DejaVu12` で 6 行 × 17 文字
- ホスト側の分割上限 (`buddy_bridge.MAX_SAY_CHARS_WIDE` / `MAX_SAY_CHARS`) はこの実測値から
  来ている。**片方を変えたらもう片方も見る。** `device/tests/test_chat.py` が両側の定数を
  import して食い違いを検出する契約テストになっている
- `setFont` は sticky。計測も描画も `_push_font` / `_pop_font` で挟んで DejaVu9 に戻す。
  戻し忘れると `BuddyUI` の footer とヒント列まで 24px になる
