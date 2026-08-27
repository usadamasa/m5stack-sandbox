"""台詞を書く側 — バッチ生成と、その裏で走る `claude -p`。

`chatter_core` の `Event` / `ChatterConfig` / `LINES_SCHEMA` / `clean` /
`describe` だけを見て、喋る側 (`buddy_chatter`) のことは知らない。デバイスにも
socket にも触らないので、テストはプロセスを起こさずにここだけを回せる。
"""

from __future__ import annotations

import json
import logging
import random
import subprocess
import tempfile
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from chatter_core import (
    LINES_SCHEMA,
    SAID,
    ChatterConfig,
    Event,
    clean,
    describe,
)
from chatter_sessions import SessionRegistry

# 喋る側と同じ logger。読む側にとって「独り言が止まった」の原因は 1 つの
# 流れなので、生成の失敗とデバイスの失敗を同じ名前の下に並べる。
log = logging.getLogger("buddy.chatter")

# 2 つずつ引いてプロンプトへ貼る。イベントログはセッション中ずっと同じ形
# — `tool: Bash` の繰り返し — なので、これが無いとどのバッチもほとんど同じ
# 入力から生成されてほとんど同じものが返る。これは扱うべき話題ではなく、
# 前のバッチが落ち着いた場所から押し出すための小突きに過ぎない。
_ANGLES: tuple[str, ...] = (
    "目の前の画面で起きていること",
    "自分の体調や気分",
    "ずんだ餅や食べものの話",
    "外の天気や季節の想像",
    "むかしのことの思い出",
    "ふと浮かんだどうでもいい疑問",
    "退屈のまぎらわしかた",
    "プログラマの手つきの観察",
    "眠気やうとうとした感じ",
    "机の上やケーブルのようす",
    "自分が住んでいる小さい機械のこと",
    "数えたり、くらべたりしてみること",
)

# 生成が失敗したときに言うもの — CLI が無い、ログインしていない、
# ネットワークが無い。稀ではあるが、これが無いとセッションの残りをずっと
# 黙ったまま過ごし、その理由もどこにも見えないデバイスになる。
_FALLBACK_LINES = (
    "ぼくは元気にしているのだ",
    "ふぅん、そういうものなのだ",
    "見ているだけなのだ",
    "まだ終わらないのだ",
    "ちょっと眠たくなってきたのだ",
)


class BatchedLineSource:
    """生成器のためのバッチ化・キャッシュ・後始末・失敗処理。

    発話ごとに 1 行ずつ生成するのは、デバイスが口を開くたびに往復すると
    いうこと。バッチなら数分をまかなえるし、それを作るレイテンシは見えない
    — 待っているスレッドが chatter 自身のものだけだから。

    バッチは、それが空になった時点でたまたま現在だったコンテキストから
    埋められるので、バッチの後ろの方の台詞は今の状況から遅れる。これは
    意図して選んだ取引で、これは実況ではなく独り言だから。

    サブクラスは `_generate` を用意する。それが投げうるものは全てここで
    捕まえて数える: 黙る chatter は許せるが、スレッドごと道連れにする
    chatter は許せない。
    """

    # どのモデル系統が答えたか。「デバイスが変なことを言っている」を
    # バックエンドまで辿れるように `status()` で報告する。
    backend = "none"

    def __init__(
        self,
        cfg: ChatterConfig,
        rng: random.Random | None = None,
        sessions: SessionRegistry | None = None,
    ) -> None:
        self._cfg = cfg
        self._rng = rng or random.Random()
        # 台帳は喋る側が持ち、ここは覗くだけ。どのセッションが動いているかを
        # 知っているのは hook のイベントを受けている側で、ここが知るのは
        # バッチを作る瞬間にそれがどうなっているか、だけ。
        self._sessions = sessions
        self._cache: deque[str] = deque()
        self._prompt: str | None = None
        self.generated = 0
        self.failures = 0
        self.last_error = ""

    @property
    def model(self) -> str:
        """報告用の、これが尋ねるモデル。空でもよい。"""
        return ""

    def _system_prompt(self) -> str:
        """`cfg.prompt_path` から一度だけ読むペルソナ。

        import 時ではなく遅延して読む: プロンプトが無い・読めないのは他と
        同じ生成の失敗であって、呼び出し側が既に数えてフォールバックする
        ものであり、MCP server の読み込みを止めるものではない。
        """
        if self._prompt is None:
            self._prompt = self._cfg.prompt_path.read_text(encoding="utf-8")
        return self._prompt

    def _angle(self) -> str:
        """`_ANGLES` から、バッチごとに引き直したヒントを 2 つ。

        1 つではなく 2 つなのは、ヒントが 1 つだとそれについて話せという
        指示に読めるから。これは独り言なので、どちら側に寄ってもよいペアに
        しておけば、モデルには両方を無視する余地も残る。
        """
        first, second = self._rng.sample(_ANGLES, 2)
        return f"{first}と{second}"

    def _sessions_note(self) -> str:
        """今このマシンで動いているセッションが何をしているか。無ければ空。

        イベントログへ混ぜずに節を分ける理由は、過去の台詞のときと同じ。
        出来事は「何が起きたか」で、これは「誰が何をしているか」— 読み方が
        違うものを 1 つの箇条書きにすると、どちらも薄まる。

        動いているものが無いときに「ありません」と書かないのは、それ自体が
        話題になってしまうから。デバイスに孤独を実況させたいわけではない。
        """
        if self._sessions is None:
            return ""
        notes = [note.describe() for note in self._sessions.recent()]
        if not notes:
            return ""
        listed = "\n".join(f"- {note}" for note in notes if note)
        return f"今このマシンで動いているセッション:\n{listed}"

    def _user_prompt(self, context: Sequence[Event]) -> str:
        """何が起きたか、既に何を言ったか、そして次にどこを見るか。

        節は 1 つのイベントログとしてまとめずに分けてある。読まれ方が
        違うから: 出来事は何について話すかで、過去の台詞は二度と言わない
        ものだ。
        """
        spoken = [ev.detail for ev in context if ev.kind == SAID and ev.detail]
        happened = [ev for ev in context if ev.kind != SAID]
        parts = [f"直近の出来事:\n{describe(happened)}"]
        running = self._sessions_note()
        if running:
            parts.append(running)
        if spoken:
            said = "\n".join(f"- {line}" for line in spoken)
            parts.append(f"すでに言ったこと。話題も言い回しも繰り返さない:\n{said}")
        parts.append(f"今回は{self._angle()}のあたりから。独り言を {self._cfg.batch} 個。")
        return "\n\n".join(parts)

    def next_line(self, context: Sequence[Event]) -> str | None:
        if not self._cache:
            self._fill(context)
        if not self._cache:
            return clean(self._rng.choice(_FALLBACK_LINES), self._cfg.max_chars)
        return self._cache.popleft()

    def _fill(self, context: Sequence[Event]) -> None:
        try:
            lines = self._generate(context)
        except Exception as exc:  # chatter は劣化する。投げはしない
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            # 缶詰の台詞に落ちたことは、デバイスを見ていても分からない。
            # CLI が入っていない・ログインが切れたはここに出る。
            log.warning("generation failed (%d so far): %s", self.failures, self.last_error)
            return

        # プロンプトは新しいものを求めている。ここはその求めに部分的にしか
        # 応じてもらえなかったときの処理。完全一致だけを見る — 2 つの違う
        # 文が「似すぎている」と判定するには閾値が要り、閾値を間違えると
        # 良い台詞を黙って食い潰す。
        seen = {ev.detail for ev in context if ev.kind == SAID}
        for raw in lines:
            cleaned = clean(raw, self._cfg.max_chars)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            self._cache.append(cleaned)
            self.generated += 1

    def _generate(self, context: Sequence[Event]) -> Sequence[object]:
        """生の台詞 1 バッチ。投げてよい。`_fill` がそれを数える。"""
        raise NotImplementedError


class ClaudeCliLineSource(BatchedLineSource):
    """`claude -p` を 1 ターン走らせて台詞を作る。Claude Code のそれ。

    ### なぜ SDK ではなく CLI か

    Codex 側と同じ理由: 資格情報は CLI のものだから。Claude Code は、それを
    走らせている人が認証したとおりに認証する — キーチェーンのサブスクリプ
    ション、API キー、サードパーティのプロバイダ — し、その解決をここで
    再現するのは、実装し直したうえで追従し続けるということになる。CLI を
    起こせばそれを構造として引き継げる。Claude Code を走らせられるマシンは
    これも走らせられる。二重に設定するものは何も無い。

    ### なぜターンを削ぎ落とすのか

    `claude -p` はエージェント一式で、こちらが欲しいのは 1 文だから。

    - `--safe-mode` は hook・MCP server・skill・CLAUDE.md の探索を切る。
      実害があるのは hook だ: このリポジトリは chatter へデータグラムを
      送る hook を登録しているので、それを読み込んだターンは、自分の生成の
      ために chatter を生成させることになる。
    - `--tools ""` は structured output のツールだけを残すので、ターンは
      何も読めず何も書けない。
    - `--no-session-persistence` は、独り言 1 回ごとにトランスクリプトが
      ディスクへ溜まるのを防ぐ。
    - cwd は空の一時ディレクトリなので、その上には何のプロジェクト設定も
      無い。

    `--json-schema` へは Codex 経路が求めるのと同じ `{"lines": [...]}` を
    渡すので、どちらの側も同じようにパースできる。
    """

    backend = "claude-cli"

    def __init__(
        self,
        cfg: ChatterConfig,
        rng: random.Random | None = None,
        run: Callable[[str, str], str] | None = None,
        sessions: SessionRegistry | None = None,
    ) -> None:
        super().__init__(cfg, rng, sessions)
        # テストが CLI を起こさずに済むように、そしてパースに触らずに別の
        # 起動方法を差し込めるように注入可能にしてある。
        self._run = run if run is not None else self._run_claude

    @property
    def model(self) -> str:
        return self._cfg.model

    def _generate(self, context: Sequence[Event]) -> Sequence[object]:
        stdout = self._run(self._system_prompt(), self._user_prompt(context))
        return cast("Sequence[object]", _cli_answer(stdout)["lines"])

    def _run_claude(self, system: str, prompt: str) -> str:
        """1 ターン走らせて stdout を返す。まずいことがあれば投げる。"""
        argv = [
            self._cfg.claude_bin,
            "-p",
            "--model",
            self._cfg.model,
            "--safe-mode",
            "--no-session-persistence",
            "--tools",
            "",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(LINES_SCHEMA),
            "--system-prompt",
            system,
        ]
        if self._cfg.effort:
            argv += ["--effort", self._cfg.effort]
        with tempfile.TemporaryDirectory(prefix="buddy-chatter-") as tmp:
            # プロンプトは引数の列ではなく stdin へ載せる: 出来事も過去の
            # 台詞もセッションとともに伸びていくため。
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._cfg.claude_timeout,
                # サーバーが起動された場所ではなく空のディレクトリ。その上に
                # あるものがターンを誘導しないように。
                cwd=tmp,
                check=False,
            )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            # インストール漏れも、期限切れのログインも、間違ったフラグも
            # 全部 stderr に出る。これは status のフィールドに入るので
            # 切り詰める。
            detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
            raise RuntimeError(f"claude -p wrote no answer (rc {proc.returncode}): {detail}")
        return proc.stdout


def _as_object(value: object) -> Mapping[str, Any] | None:
    """デコードされた JSON オブジェクト。それ以外になったなら None。"""
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else None


def _cli_answer(stdout: str) -> Mapping[str, Any]:
    """`claude -p --output-format json` から構造化された答えを取り出す。

    2 つの形を受け付けるのは、2 つの形を実際に見たから: ドキュメントどおりの
    単一の result オブジェクトと、ストリーム全体をリストにして最後に result
    が来るもの。最後の `result` エントリを取れば、入っている CLI がどちらを
    するかを知らずに両方をまかなえる。

    `result` より `structured_output` を優先する。後者は同じ JSON をテキスト
    にしたもので、前者を埋めない CLI のためのフォールバック。
    """
    parsed: object = json.loads(stdout)
    if isinstance(parsed, list):
        objects = [_as_object(m) for m in cast("list[object]", parsed)]
        results = [obj for obj in objects if obj is not None and obj.get("type") == "result"]
        if not results:
            raise RuntimeError("claude -p emitted no result message")
        result = results[-1]
    else:
        found = _as_object(parsed)
        if found is None:
            raise TypeError(f"claude -p answered with {type(parsed).__name__}, not an object")
        result = found
    if result.get("is_error"):
        detail = " ".join(str(result.get("result", "")).split())[-300:]
        raise RuntimeError(f"claude -p reported an error: {detail}")
    structured = result.get("structured_output")
    if isinstance(structured, dict):
        return cast("Mapping[str, Any]", structured)
    return cast("Mapping[str, Any]", json.loads(result["result"]))
