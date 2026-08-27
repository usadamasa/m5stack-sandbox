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

`device/apps/claude_buddy.py` は `sys.path` を整えて `device/buddy/app.py` へ渡すだけの
起動口。import しても起動せず、起動するのは `claude_buddy.run()` を呼んだとき。import が
冒頭に来ないので E402 を無視してある。アプリ本体は `buddy/app.py` (組み立てと main loop)
と `buddy/router.py` (届いた 1 行の振り分け)。

実機なしで検証できる:

```bash
uv run --directory device pytest
uv run python host/tools/src/buddy_deploy.py --compile-only   # MicroPython のパーサに通す
```

# チャットパネル (`buddy/chat.py`)

4 つに割れている。パネルの幾何・verb の振り分けと描画が `buddy/chat.py`、
書体の選択と読み込みと計測が `buddy/chat_font.py` (`ChatFont`)、transcript の保持が
`buddy/chat_log.py` (`Transcript`)、行の折り返しが `buddy/chat_wrap.py` (`wrap`)。
テストも同じ継ぎ目で `test_chat.py` / `test_chat_font.py` / `test_chat_log.py` /
`test_chat_wrap.py` に割れていて、fake の LCD は `device/tests/chat_fakes.py`。

`chat.say` / `chat.clear` / `chat.info` は upstream の `buddy_protocol.py` が知らない verb。
知らない `cmd` は "unknown cmd" と印字して捨てられるので、`buddy/router.py` の `on_line` で
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
  前の書体が選ばれたまま。だから `ChatFont._resolve_vlw` が構築時に `os.stat` で存在を
  確かめる。ack の `vlw` フィールドはこの判定結果で、`font: vlw` でなければ内蔵に
  フォールバックしている
- **`setTextSize` は float を取る。** 内蔵書体はこれで 0.75 に縮めて使う
  (`chat_font.WIDE_SCALE` / `chat_font.NARROW_SCALE`)。ただし最近傍で画素行を間引くだけ
  なので、画数の多い漢字は潰れる。
  VLW を入れるまでの繋ぎと割り切ること。VLW 側は生成時が最終サイズなので 1:1
- 書体は中身で切り替える。日本語なら VLW、ASCII だけなら `DejaVu12` @0.75 で 9 行 × 24 文字。
  ASCII は VLW より DejaVu のほうが詰まる
- ホスト側の分割上限 (`buddy_text.MAX_SAY_CHARS_WIDE` / `MAX_SAY_CHARS`) はこの実測値から
  来ている。**片方を変えたらもう片方も見る。** `device/tests/test_chat.py` が両側の定数を
  import して食い違いを検出する契約テストになっている
- `setFont` も `setTextSize` も、読み込んだ VLW も sticky。計測も描画も `ChatFont.push` /
  `ChatFont.pop` で挟んで DejaVu9 の 1:1 に戻す。戻し忘れると `BuddyUI` の footer と
  ヒント列まで巻き添えになる。base font に戻すと VLW は外れるので、次の `push` が読み直す
- 実測を採り直すのは `host/tools/src/probe_device.py`。フォント一覧・各書体のメトリクス・
  ヒープをまとめて吐く

# 発話 (`buddy/speak.py`)

デバイスは自分で音声を取りに行く。5 つに割れている。

| モジュール | 担当 |
| --- | --- |
| `buddy/speak.py` (`SpeechPlayer`) | 発話のライフサイクル。`speak.say` / `speak.stop` の振り分け、fetch、tick ごとの pump、`speak.end` の ack |
| `buddy/speak_out.py` (`SpeakerOut`) | `M5.Speaker` への出口。起こす・音量・枠の確認・`playRaw`・渡したブロックの保持 |
| `buddy/speak_stream.py` (`StreamSource`) | socket から届くバイト列を player が欲しがる大きさのブロックに均す |
| `buddy/tts.py` (`fetch_speech`) | Mac の VOICEVOX ENGINE との HTTP のやりとり |
| `buddy/wav.py` (`open_pcm` / `parse_wav_header` / `PrefixedStream`) | 返ってきた WAV を解いて samples の頭を見つける |

依存は `speak` -> `speak_out` / `speak_stream` と `speak` -> `tts` -> `wav` の一方向。
`speak_out` / `speak_stream` は player を知らず、`wav` はネットワークも speaker も知らない。

テストも同じ継ぎ目で `test_speak.py` / `test_speak_out.py` / `test_speak_stream.py` /
`test_tts.py` / `test_wav.py` に割れている。fake は `device/tests/speak_fakes.py` (時計と
speaker とストリーム) と `device/tests/wav_fakes.py` (engine が返すバイト列の組み立てと、
socket の形をした読み口)。ホスト側から見た `buddy_verbs.speak` の契約は
`test_speak_host.py` が両側の定数を突き合わせる。実機も engine も要らない。

音まわりで踏みやすい点:

- **合成はデバイスがやっていない。** ESP32-S3 には日本語の音声を置く flash も回す
  サイクルも無い。WiFi 越しに VOICEVOX engine を叩いて PCM を取ってくるのが
  `buddy/tts.py` で、`speak.say` の ack はその往復 (数秒) が終わってから出る
- **WiFi を繋ぐのはこの経路ではない。** `/flash/main.py` が boot で繋ぐ。アプリの中から
  `connect()` すると受け付けられたまま完了しない (ESP-IDF heap が足りない)。ラジオが
  落ちていれば最初の POST が OSError になり、その台詞は失われる — それが正しい結末
- **rate はヘッダから採る。** `outputSamplingRate` は engine が断れる要求で、24 kHz の
  samples を 16 kHz で鳴らすのは全編ずれた音程になる。`fetch_speech` が返す `rate` は
  頼んだ値ではなく WAV が名乗った値
- **ヘッダを探すと samples を読み過ぎる。** 読み過ぎたぶんは `PrefixedStream` が抱えて
  次の読み手へ先に返す。捨てると毎回の台詞の頭がクリックになる
- **チャンネルは 1 本に固定する。** `channel=-1` は空きチャンネルを探すので、ブロックが
  重なって鳴る (実測)。枠は 2 つ (再生中 + 次) で、渡す前に `isPlaying(ch)` を見る —
  満杯の `playRaw` は False を返さずに**待つ**ので、待たされた tick は UI が止まる
- **渡したブロックの参照を落とさない。** binding は buffer のポインタを渡すだけで複製
  しないので、GC がその領域を次の bytes に回すと鳴っている途中で中身が変わる。
  `SpeakerOut` が最後の数個を持ち続け、もう読まれないと分かったところで `release()` する
- 実測の根拠 (ブロック長、枠のタイミング、起動時のポップ) は `buddy/speak_out.py` の
  docstring の Timing 節、WiFi の省電力を切る理由は `buddy/speak_stream.py` の docstring
