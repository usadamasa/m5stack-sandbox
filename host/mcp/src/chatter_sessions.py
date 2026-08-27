"""読む側 — Claude Code のセッション transcript から、そのセッションの今を拾う。

hook が線に載せられるのは `kind` と 100 文字の `detail` だけで、それは
`tool: Bash: uv run pytest` のような行にしかならない。何をやっているセッション
なのかはそこに残らないし、daemon には複数のセッションから同じ socket へ届くので、
イベント列は混ざったまま持ち主を名乗らない。

Claude Code は同じことを別の場所に、もっと良い形で書いている。
`~/.claude/projects/<project>/<session_id>.jsonl` には `ai-title` (セッションを
1 行にした要約) と `last-prompt` (最後にユーザーが言ったこと) が入っていて、
どちらもセッションが進むたびに書き直される。この 2 つと cwd / ブランチがあれば、
「よそで何が起きているか」は足りる。

### パスは線を越えない

socket はこのマシンの誰にでも開いている。だから `transcript_path` は受け取らない
— hook の payload には入っているが、それを信じて開けば、送り主が読ませたい
どんなファイルの断片でもプロンプトへ貼られ、デバイスが読み上げることになる。
線に載るのは `session_id` だけで、`chatter_core.parse_event` がそれを UUID として
検め、ここが自分の知っている置き場から glob で引く。ここでも検め直すのは、この
registry を呼ぶ経路が増えたときに検問が抜けないようにするため。

### 本文は読まない

`assistant` と `user` のメッセージには手を付けない。ここで拾ったものはパネルに
出て VOICEVOX が読み上げるので、他プロジェクトの会話がそのまま部屋の音になる
経路を作らない。タイトルと最後の指示までなら、話題としては十分で、漏れるものは
最小になる。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from chatter_core import SESSION_ID

# 末尾から読む量。本物の transcript は数 MB あり、独り言 1 行のために毎バッチ
# 全部をパースする理由が無い。`ai-title` と `last-prompt` はどちらも数十行
# おきに書き直されるので、この幅があれば実測でどちらも入る。
TAIL_BYTES = 128 * 1024

# プロンプトへ貼る前に切る幅。向こう側の `clean` も切るが、切られる前に
# 4000 字がプロンプトに乗る理由は無い。
_MAX_TITLE = 60
_MAX_PROMPT = 80
_MAX_NAME = 60

# transcript の中で見る行。ここに無い種別は読まない。
_TITLE = "ai-title"
_PROMPT = "last-prompt"


@dataclass(frozen=True, slots=True)
class SessionNote:
    """1 つのセッションの、今のところの姿。"""

    session: str
    title: str = ""
    prompt: str = ""
    project: str = ""
    branch: str = ""

    def describe(self) -> str:
        """プロンプトへ貼る 1 行。空の項目は書かない。

        揃わないことは普通にある — 始まったばかりのセッションにはまだ
        タイトルが無いし、git の外で動いているセッションにはブランチが無い。
        埋まっていない項目を書くと、モデルはそれを「そういう状態である」
        こととして読んでしまう。
        """
        where = self.project
        if where and self.branch and self.branch != "main":
            where = f"{where} の {self.branch}"
        parts = [p for p in (where, self.title) if p]
        line = "、".join(parts)
        if self.prompt:
            said = f"直前の指示は「{self.prompt}」"
            line = f"{line} ({said})" if line else said
        return line


def read_note(path: Path, session: str, limit: int = TAIL_BYTES) -> SessionNote | None:
    """transcript 1 本の末尾を読む。何も拾えなければ None。

    後ろから見て、種別ごとに最初に見つかったものを採る。transcript は追記
    されるだけなので、それがそのセッションの最新になる。

    読めないファイルは「そのセッションのことは分からない」であって、失敗
    ではない — 消えている途中かもしれないし、書き込みの最中かもしれない。
    chatter がそれで止まる価値は無い。
    """
    found: dict[str, str] = {}
    for entry in _tail_entries(path, limit):
        _absorb(entry, found)
        if found.keys() >= _WANTED:
            break
    if not found:
        return None
    return SessionNote(
        session,
        found.get("title", ""),
        found.get("prompt", ""),
        found.get("project", ""),
        found.get("branch", ""),
    )


# 揃った時点で読むのをやめてよい項目。`branch` は入れない — git の外で
# 動いているセッションには最後まで来ないので、待つと必ず末尾まで読む。
_WANTED = frozenset({"title", "prompt", "project"})


def _absorb(entry: Mapping[str, Any], found: dict[str, str]) -> None:
    """行 1 つから、まだ埋まっていない項目を採る。

    後ろから読んでいるので、先に入ったものが新しい。上書きはしない。
    """
    kind = entry.get("type")
    if kind == _TITLE:
        _put(found, "title", _flat(entry.get("aiTitle"), _MAX_TITLE))
    elif kind == _PROMPT:
        _put(found, "prompt", _flat(entry.get("lastPrompt"), _MAX_PROMPT))
    elif "project" not in found:
        # `cwd` と `gitBranch` はどの user / assistant 行にも付いている。
        # 種別で選ばずに、持っている行から採る。
        _put(found, "project", _project(_flat(entry.get("cwd"), _MAX_NAME)))
        _put(found, "branch", _flat(entry.get("gitBranch"), _MAX_NAME))


def _put(found: dict[str, str], key: str, value: str) -> None:
    """空でない値を、まだ無いときだけ入れる。

    空を入れないのは、それが「その行には無かった」と「そのセッションには
    無い」の区別になっているから。前者ならもっと前の行に載っている。
    """
    if value and key not in found:
        found[key] = value


def _tail_entries(path: Path, limit: int) -> Iterator[Mapping[str, Any]]:
    """末尾 `limit` バイトの中の JSON オブジェクトを、後ろの行から順に。"""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - limit)
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return
    if start:
        # seek した先はほぼ確実に行の途中。その断片は壊れた JSON になる
        # だけとは限らず、運が悪ければ別の何かとして読めてしまう。
        _, _, blob = blob.partition(b"\n")
    for raw in reversed(blob.split(b"\n")):
        if not raw.strip():
            continue
        try:
            parsed: object = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(parsed, dict):
            yield cast("Mapping[str, Any]", parsed)


def _flat(value: object, limit: int) -> str:
    """transcript の値 1 つを、1 行に潰して切る。文字列でなければ空。"""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _project(cwd: str) -> str:
    """cwd が指しているリポジトリの名前。

    worktree の cwd は `<repo>/.claude/worktrees/<branch>` で終わるので、
    末尾を採るとブランチ名を 2 回言うことになり、どのリポジトリの話かが
    消える。
    """
    parts = Path(cwd).parts
    if ".claude" in parts:
        index = parts.index(".claude")
        return parts[index - 1] if index else ""
    return parts[-1] if parts else ""


class SessionRegistry:
    """どのセッションが今も動いているかの台帳と、その要約のキャッシュ。

    hook のイベントが来るたびに `note_activity` が呼ばれ、台詞を作るときに
    `recent` が呼ばれる。読み取りは後者の側にある: イベントは毎秒来うるが
    生成は数分に 1 回で、transcript を読むのはその頻度でよい。

    daemon はセッションより長く生きるので、`ttl` を過ぎたものは台帳から
    落ちる。落とさなければ、一日動かした daemon の台帳は、その日に開いた
    セッション全部になる。
    """

    def __init__(
        self,
        root: Path,
        ttl: float = 900.0,
        limit: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = root
        self._ttl = ttl
        self._limit = limit
        self._clock = clock
        self._seen: dict[str, float] = {}
        self._cache: dict[str, tuple[float, SessionNote]] = {}

    @property
    def tracked(self) -> int:
        """台帳に載っているセッションの数。`status()` に出る。"""
        return len(self._seen)

    def note_activity(self, session: str) -> None:
        """このセッションから hook が飛んできた。UUID でなければ何もしない。"""
        if not SESSION_ID.match(session):
            return
        self._seen[session] = self._clock()

    def recent(self) -> list[SessionNote]:
        """今も動いているセッションの要約を、新しく動いた順に。"""
        now = self._clock()
        self._seen = {s: at for s, at in self._seen.items() if now - at <= self._ttl}
        self._cache = {s: c for s, c in self._cache.items() if s in self._seen}
        ordered = sorted(self._seen, key=lambda s: self._seen[s], reverse=True)
        notes: list[SessionNote] = []
        for session in ordered:
            note = self._note(session)
            if note is not None:
                notes.append(note)
            if len(notes) >= self._limit:
                break
        return notes

    def _note(self, session: str) -> SessionNote | None:
        """要約 1 つ。transcript が動いていなければキャッシュから返す。"""
        path = self._transcript(session)
        if path is None:
            return None
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return None
        cached = self._cache.get(session)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        note = read_note(path, session)
        if note is None:
            return None
        self._cache[session] = (stamp, note)
        return note

    def _transcript(self, session: str) -> Path | None:
        """このセッションの transcript。どのプロジェクトの下かは知らない。

        `session` は `note_activity` が UUID として検めたものだけが台帳に
        載るので、ここで glob のパターンに埋まる文字列にワイルドカードも
        セパレータも入らない。
        """
        return next(iter(sorted(self._root.glob(f"*/{session}.jsonl"))), None)
