---
name: buddy-chatter-variety
description: Use when chatter の台詞や LLM が生成する行が画一的 (同じ語尾、同じ文型、話題が周回) なとき、chatter_prompt.md や chatter_lines.py のプロンプトの組み立てを直すとき、「多様に」「繰り返さない」と書いても効かなかったとき。mode collapse、monotonous、homogeneous な出力にも。
---

# 台詞の画一化を崩す

「多様に書け」は効かない。整列済みモデルは open-ended な問いに対して同じ答えへ収束する
(Artificial Hivemind, NeurIPS 2025)。効くのは**構造の側**にある手で、どれを引くかは
実際の出力を読んで決める。プロンプトを眺めても分からない。chatter なら
`~/.local/state/buddy/buddy-mcpd.log` の `buddy.chatter: said` を並べて、語尾・文型・
話題の周期を先に数える。

| 実際の出力に見える症状 | 手 |
|------|------|
| 同じ語尾・決まり文句が並ぶ | [語句の禁止リスト](#語句の禁止リスト) |
| 毎行が同じ文型 | [出力ごとの仕様](#出力ごとの仕様) |
| 話題が一定周期で戻ってくる | [履歴の窓を周期より長く](#履歴の窓を周期より長く) |
| どの行も「いかにも」で平板 | [確率を言わせて低い方を取る](#確率を言わせて低い方を取る) |
| 「多様に」と書いたが変わらない | 上のどれかへ移る。指示文の言い換えでは動かない |

## 注入する場所で届き方が決まる

ランダム性をどこへ入れるかで出力への届き方が桁で変わる ([arXiv:2606.10302](https://arxiv.org/abs/2606.10302))。

| 段 | 例 | 出力への伝達 |
|---|---|---|
| 0 | temperature を上げる | 語彙が揺れるだけ |
| 1 | バッチの頭にランダムな概念を貼る (「今回は天気のあたりから」) | ~0.003 |
| 2 | 出力 1 つごとに tone / form / perspective を指定する | ~0.5 |

段 1 の候補プールを広げても段 1 のまま。平板なら段 2 へ上げる。chatter は以前が段 1
(`_angle()`) で、今は段 2 (`chatter_lines.py` の `_specs()`)。

## 出力ごとの仕様

バッチで N 行作らせるなら、N 行それぞれに「見るもの / 形 / 気分」のような軸を引いて
番号付きで渡す。形の軸は**バッチ内で非復元**に引く。同じ型の文が隣り合うのが
いちばん耳につく。

```text
独り言を 6 個。各行 30 文字以内。行ごとの指定:
1. 見るもの: 机の上 / 形: 数え言葉 / 気分: だるい
2. 見るもの: 外の天気 / 形: 途中でやめる / 気分: ごきげん
...
```

形の軸は `chatter_lines.py` の `_FORMS`、名前の説明は `chatter_prompt.md` の「形の名前」。
軸を足すときは両方に足す。説明の無い形の名前はモデルが好きに解釈する。

下流で切り詰める上限 (`max_chars`) は**プロンプトに数値で書く**。書かないと長くなる形が
上限を越え、語尾が落ちた行のほうが画一的な行より耳につく。

## 語句の禁止リスト

「言い回しを変えろ」ではなく、実際の出力から拾った語尾・定型句を名指しで禁じる
(Antislop, [arXiv:2510.15061](https://arxiv.org/abs/2510.15061))。構造の定型 (「X が Y だ」の
一文だけで終わる形) も同じく名指しする。chatter では `chatter_prompt.md` の
「使い古した言い回し」。log で新しい口癖が育ったら、そこへ足す。

## 履歴の窓を周期より長く

「すでに言ったこと」として渡す行数 (`buddy_chatter.py` の `_SAID_DEPTH`) は、**観測した
周期より長く**する。同じ話題が 1 時間おきに戻るなら、窓は 1 時間ぶん以上。バッチ 1.5 回分の
窓では前のバッチしか見えない。受け取る側では完全一致だけ落とす。類似度の閾値は良い行を
黙って食う。

## 確率を言わせて低い方を取る

「候補を K 個、それぞれの確率つきで」と頼み、確率の低いものを採る (Verbalized Sampling,
[arXiv:2510.01171](https://arxiv.org/abs/2510.01171))。mode collapse の原因は preference data の
typicality bias で、確率を言語化させると分布の裾が出る。閾値をプロンプトに書けば
(「確率 0.2 未満のものを」) 散り方を調整できる。`LINES_SCHEMA` の変更が要るので、
出力ごとの仕様で足りないときの次の手。

## Common Mistakes

- **プロンプトだけ読んで直す**: 症状は出力にしか無い。log から語尾・文型・話題の
  周期を先に数える
- **few-shot で良い例を見せる**: 例に収束する。独白や persona では例の型が出力の型になる
- **切り口のプールを広げる**: 段 1 のまま。届かない
- **類似度で重複を弾く**: 閾値が当たらないと良い行が消え、消えたことが見えない
- **直しても喋りが変わらない**: daemon は import 済みのコードで動く。`buddy-mcpd restart`

## 文献

- [Artificial Hivemind: The Open-Ended Homogeneity of Language Models](https://proceedings.neurips.cc/paper_files/paper/2025/hash/754d5a526a5ee5a47220664a0eb92751-Abstract-Datasets_and_Benchmarks_Track.html) (NeurIPS 2025 Best Paper): 同一モデルの反復でも異なるモデル間でも同じ答えへ収束する
- [Where You Inject Diversity Matters](https://arxiv.org/abs/2606.10302): 注入の段と伝達率
- [Verbalized Sampling](https://arxiv.org/abs/2510.01171) (Zhang, Yu, Chong, Sicilia, Tomz, Manning, Shi 2025): 確率を言語化させて mode collapse を崩す
- [Antislop](https://arxiv.org/abs/2510.15061) (Paech, Roush, Goldfeder, Shwartz-Ziv 2025): 語句と構造の定型を名指しで除く
- [A Diversity-Promoting Objective Function for Neural Conversation Models](https://arxiv.org/abs/1510.03055) (Li, Galley, Brockett, Gao, Dolan, NAACL 2016): 対話モデルの「安全で平凡な応答」問題の原典。distinct-n の出所
