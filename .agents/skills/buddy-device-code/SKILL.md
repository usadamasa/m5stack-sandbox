---
name: buddy-device-code
description: device/ 配下のコードを書く・直すときに使う。MicroPython 1.27 (ESP32-S3) 向けに書く際の当リポジトリの決めごとと、LCD のチャットパネル (buddy/chat.py) のフォント・行数・描画の制約を扱う。
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

# チャットパネル (`buddy/chat.py`)

`chat.say` / `chat.clear` / `chat.info` は upstream の `buddy_protocol.py` が知らない verb。
知らない `cmd` は "unknown cmd" と印字して捨てられるので、`claude_buddy.py` の `on_line` で
先に横取りしてから proto へ流す。**ここが upstream ファイルを触らずに protocol を拡張できる
唯一の場所。**

画面まわりで踏みやすい点:

- 表示中は `y=0..110` を chat が占有する。`BuddyUI.update_footer()` は `y=96..110` を塗るので
  `chat.active` の間は呼ばない。`set_connection()` は main panel も塗るので、呼んだら
  `chat.render()` で描き直す
- **日本語は firmware 内蔵のフォントでは足りない。** このビルドが持つ日本語フォントは 24px の
  `EFontJA24` / `AlibabaSansJA24` だけ (`fontHeight()` は 27) で、110px のパネルに 4 行 × 9 文字
  しか入らない。`EFontCN24` / `AlibabaPuHuiTiCN24` / `AlibabaSansKR24` は「漢」が半角幅を返す、
  つまり日本語グリフを持っていない
- **そこで VLW を外から与えている。** `host/tools/src/make_vlw.py` が TTF から VLW を作り、
  `/flash/buddy-ja.vlw` に置く。現物は BIZ UDGothic 16px、JIS 第 1 水準まで 3476 グリフで
  930KB。実測で `loadFont` 57ms・ヒープ 19.5KB・**6 行 × 13 文字**。M5GFX はグリフ属性の配列
  だけを常駐させてビットマップは描画のたびにファイルから読むので、ヒープ 99KB でも載る
- **`loadFont` は失敗しても何も言わない。** 存在しないパスでも空の bytes でも例外を投げず、
  前の書体が選ばれたまま。だから `_resolve_vlw` が構築時に `os.stat` で存在を確かめる。
  ack の `vlw` フィールドはこの判定結果で、`font: vlw` でなければ内蔵にフォールバックしている
- **`setTextSize` は float を取る。** 内蔵書体はこれで 0.75 に縮めて使う (`_WIDE_SCALE` /
  `_NARROW_SCALE`)。ただし最近傍で画素行を間引くだけなので、画数の多い漢字は潰れる。
  VLW を入れるまでの繋ぎと割り切ること。VLW 側は生成時が最終サイズなので 1:1
- 書体は中身で切り替える。日本語なら VLW、ASCII だけなら `DejaVu12` @0.75 で 9 行 × 24 文字。
  ASCII は VLW より DejaVu のほうが詰まる
- ホスト側の分割上限 (`buddy_bridge.MAX_SAY_CHARS_WIDE` / `MAX_SAY_CHARS`) はこの実測値から
  来ている。**片方を変えたらもう片方も見る。** `device/tests/test_chat.py` が両側の定数を
  import して食い違いを検出する契約テストになっている
- `setFont` も `setTextSize` も、読み込んだ VLW も sticky。計測も描画も `_push_font` /
  `_pop_font` で挟んで DejaVu9 の 1:1 に戻す。戻し忘れると `BuddyUI` の footer とヒント列まで
  巻き添えになる。base font に戻すと VLW は外れるので、次の `_push_font` が読み直す
- 実測を採り直すのは `host/tools/src/probe_device.py`。フォント一覧・各書体のメトリクス・
  ヒープをまとめて吐く
