"""chatter の共有物 — イベント、設定、リンクの形。

`chatter_lines` (台詞を書く側) と `buddy_chatter` (喋る側) の両方が参照する
ものだけを置く。依存は service → lines → core の一方向で、ここから上の 2 つを
import することは無い。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import buddy_paths
from buddy_verbs import DEFAULT_RATE, ZUNDAMON
from buddy_wire import Message

# daemon は任意のディレクトリから起動され、hook は任意のプロジェクトから
# 飛んでくるので、どちらも自分のファイルの置き場ではなく環境からこれを
# 計算する。buddy_paths を参照。
DEFAULT_SOCKET = buddy_paths.socket_path()

# Claude Code のセッション transcript の置き場。読む側は `chatter_sessions`。
DEFAULT_PROJECTS = buddy_paths.projects_dir()

# hook が報告できるイベント。それ以外はモデルへ渡さずに捨てる。見知らぬ
# 送り主にモデルを操られないようにするため。
KINDS = frozenset({"tool", "error", "stop", "notify", "prompt", "session", "idle"})

# デバイス自身の過去の発話。生成器がそれとは違うものを出せるように戻して
# やる。あえて `KINDS` には入れていない: socket はこのマシンの誰にでも開いて
# いるので、`said` を偽装できる送り主はデバイスの自己記憶を書き換えられる
# ことになる。
SAID = "said"

# ペルソナはこのモジュールではなくファイルに置く。デバイスの喋り方を変える
# のは散文を編集する作業で、文字列リテラルの中の散文は誰にも編集を促さない
# — 別の性格が欲しいだけで Python を触りたくない人にはなおさら。
DEFAULT_PROMPT_PATH = Path(__file__).with_name("chatter_prompt.md")


class ChatLink(Protocol):
    """chatter が必要とする `ResidentLink` の断面。

    テストがスタブを差せるように、そしてこのモジュールがリンクの開き方や
    閉じ方に依存しないように、狭く取ってある — ここは借りるだけ。
    """

    @property
    def connected(self) -> bool: ...

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message: ...

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message: ...


# hook が名乗るセッションの識別子として受け付ける形。Claude Code の
# transcript は `<session_id>.jsonl` という名前で置かれるので、この値は後で
# glob のパターンに埋まる。socket はこのマシンの誰にでも開いているため、
# ここが「送られてきた文字列がパスの部品になる」唯一の場所になる — だから
# UUID そのものだけを通す。`..` もセパレータもワイルドカードもこの形には
# 収まらない。
SESSION_ID = re.compile(r"\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


@dataclass(frozen=True, slots=True)
class Event:
    """hook が見たものを、台詞を組み立てられる形まで削ったもの。

    `session` はそれを送ってきた Claude Code のセッション。空でもよい —
    この項目を知らない古い hook からも届くし、`SAID` のように chatter が
    自分で作るイベントには送り主が居ない。
    """

    kind: str
    detail: str = ""
    session: str = ""


class LineSource(Protocol):
    """次に言うことがどこから来るか。"""

    def next_line(self, context: Sequence[Event]) -> str | None: ...


# どちらの生成器も自分のモデルにこの形を求める。オブジェクト 1 つと文字列の
# 配列 1 つ — どちらのバックエンドの structured output でも強制できる程度に
# 小さく、それがパースを自明にしている。
LINES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
    "required": ["lines"],
    "additionalProperties": False,
}


def _float_env(env: Mapping[str, str], name: str, fallback: float) -> float:
    """float を読む。解釈できない値は指定が無かったものとして扱う。

    環境変数のタイプミスでサーバーが起動しなくなってはいけない。chatter は
    飾りであって、その設定も同じように劣化するべき。
    """
    try:
        return float(env[name])
    except (KeyError, ValueError):
        return fallback


def _int_env(env: Mapping[str, str], name: str, fallback: int) -> int:
    try:
        return int(env[name])
    except (KeyError, ValueError):
        return fallback


@dataclass(frozen=True)
class ChatterConfig:
    """調整できるもの全て。起動時に一度だけ解決する。"""

    socket_path: Path = DEFAULT_SOCKET
    # ペルソナの書き場所。別の性格にするならここを他所へ向ける。中身を読む
    # のは生成器だけ。
    prompt_path: Path = DEFAULT_PROMPT_PATH
    enabled: bool = True
    # 発話の間隔 (秒)。毎回この範囲から引き直す。範囲のどこで引くかは
    # セッションの忙しさが決めるので、この上下限は両端であって普段の値では
    # ない。
    gap_min: float = 40.0
    gap_max: float = 150.0
    # セッションが「完全に忙しい」と見なされ、間隔が短い端に寄る hook
    # イベントの毎分あたりの数。ツール呼び出し 1 回は Pre と Post の両方の
    # hook で数えられるので、これはおよそ毎分 6 回 — 実際に働いている
    # セッションなら超える速さで、ビルドを待っている間には遠く及ばない
    # 速さ。
    busy_rate: float = 12.0
    # この時間だけ黙っていたらデバイスが自分から何か言う。これも刻まない
    # ように引き直す。
    idle_min: float = 60.0
    idle_max: float = 180.0
    # N 回に 1 回だけ声に出す。残りはパネルにだけ出す。
    voice_every: int = 1
    # ----- よそのセッション
    #
    # hook を飛ばしてきたセッションの transcript を読んで、何をしている
    # セッションなのかを台詞の材料に加えるか。切れるようにしてあるのは、
    # ここで拾ったものはパネルに出て読み上げられるから — 人が見ている前で
    # 他所の作業を口にさせたくない場面のために、chatter ごと止めずに済む
    # ノブが要る。
    sessions: bool = True
    projects_path: Path = DEFAULT_PROJECTS
    # プロンプトへ載せるセッションの数。多くしても喋れる量は増えないので、
    # これは話題の幅ではなく、1 回の生成で読む transcript の数になる。
    session_limit: int = 3
    # これだけ hook が来なければ、そのセッションは動いていないものとする。
    # 昨日の作業を今の話として喋られても困る。
    session_ttl: float = 900.0
    # 1 回の生成で作る台詞の数。1 回の呼び出しで数分をまかなえるので、
    # これが安さの理由になっている。
    batch: int = 6
    # ----- 台詞を書く Claude CLI
    #
    # 固定の id ではなくエイリアスにして、その時点の Sonnet に追従させる。
    # 独り言を 1 行書くのは最大のモデルを要する仕事ではないし、これは
    # セッション中ずっと走る。
    claude_bin: str = "claude"
    model: str = "sonnet"
    # `--effort` へ渡す。空なら CLI 自身の既定値をそのままにする。
    effort: str = "low"
    # ツール無しの `claude -p` の 1 ターンは数秒で終わる。これはレイテンシの
    # 予算ではなく、プロセスが刺さったときの保険。これを待つのは chatter
    # 自身のスレッドだけ。
    claude_timeout: float = 120.0
    speaker: int = ZUNDAMON
    rate: int = DEFAULT_RATE
    engine: str = ""
    # 日本語のパネル 1 枚は 32 文字。1 行が 2 回の送信に分かれることが決して
    # 無いように少し余裕を残す。
    max_chars: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ChatterConfig:
        """プロセスの環境から設定を組み立てる。

        どのノブにも環境変数があるので、セッションの調整はこれを編集する
        のではなくサーバーを再起動して行える。そのうちセッションの途中で
        変える価値があるもの (`model`、`effort`、`batch`、ペーシング) は
        `buddy_chatter_start` の引数にもなっている。
        """
        # `os.environ` ではなく `environment()`: 明示的な env は呼び出し側
        # (やテスト) が読むものを正確に指定しているということで、env を
        # 渡さないのは「このマシンの設定に従う」という意味になり、そこには
        # `config.toml` も含まれる。
        env = buddy_paths.environment() if env is None else env
        raw_prompt = env.get("BUDDY_CHATTER_PROMPT", "")
        return cls(
            socket_path=buddy_paths.socket_path(env),
            prompt_path=Path(raw_prompt) if raw_prompt else DEFAULT_PROMPT_PATH,
            enabled=env.get("BUDDY_CHATTER", "1") not in ("0", "false", "no"),
            gap_min=_float_env(env, "BUDDY_CHATTER_GAP_MIN", 40.0),
            gap_max=_float_env(env, "BUDDY_CHATTER_GAP_MAX", 150.0),
            idle_min=_float_env(env, "BUDDY_CHATTER_IDLE_MIN", 60.0),
            idle_max=_float_env(env, "BUDDY_CHATTER_IDLE_MAX", 180.0),
            busy_rate=_float_env(env, "BUDDY_CHATTER_BUSY_RATE", 12.0),
            voice_every=max(1, _int_env(env, "BUDDY_CHATTER_VOICE_EVERY", 1)),
            sessions=env.get("BUDDY_CHATTER_SESSIONS", "1") not in ("0", "false", "no"),
            projects_path=buddy_paths.projects_dir(env),
            session_limit=max(1, _int_env(env, "BUDDY_CHATTER_SESSION_LIMIT", 3)),
            session_ttl=_float_env(env, "BUDDY_CHATTER_SESSION_TTL", 900.0),
            batch=max(1, _int_env(env, "BUDDY_CHATTER_BATCH", 6)),
            claude_bin=env.get("BUDDY_CHATTER_CLAUDE_BIN", "claude"),
            model=env.get("BUDDY_CHATTER_MODEL", "sonnet"),
            effort=env.get("BUDDY_CHATTER_EFFORT", "low"),
            claude_timeout=_float_env(env, "BUDDY_CHATTER_CLAUDE_TIMEOUT", 120.0),
            speaker=_int_env(env, "BUDDY_CHATTER_SPEAKER", ZUNDAMON),
            rate=_int_env(env, "BUDDY_CHATTER_RATE", DEFAULT_RATE),
            engine=env.get("VOICEVOX_URL", ""),
        )


def parse_event(payload: bytes) -> Event | None:
    """データグラム 1 つをイベントにする。イベントでなければ None を返す。

    壊れているものは黙って捨てる。送り主は失敗を伝えようが無い hook で、
    しかも失敗を知るために遅くされてはいけないので、エラーの行き先が無い。
    """
    try:
        raw: Any = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    # `isinstance` だけでは `raw` は `dict[Unknown, Unknown]` にしか絞られ
    # ない。cast は下の `.get` に具体的な値型を与えるためのもの。
    obj = cast("dict[str, Any]", raw)
    kind = obj.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        return None
    detail = obj.get("detail", "")
    if not isinstance(detail, str):
        detail = ""
    # detail はプロンプトへ貼り付けられるので、送り主が常識的であることを
    # 信じるのではなくここで刈り込む。
    return Event(kind, " ".join(detail.split())[:120], _session_id(obj.get("session")))


def _session_id(value: object) -> str:
    """名乗られたセッション。UUID でなければ、名乗らなかったものとして扱う。

    形が合わないときにイベントごと捨てないのは、出来事そのものは実際に
    起きているから。送り主として一番ありそうなのは攻撃者ではなく、この
    項目をまだ送らない古い hook で、そちらのイベントは今までどおり使える。
    """
    if not isinstance(value, str):
        return ""
    lowered = value.lower()
    return lowered if SESSION_ID.match(lowered) else ""


def clean(line: object, limit: int) -> str:
    """生成された台詞 1 行を、パネルが載せられる形まで削る。

    `object` を取るのはモデルの出力が JSON だから: スキーマは文字列を求めて
    いるが、それでも来てしまった文字列以外の値は、背景スレッドでの
    TypeError ではなく短い台詞になるべき。
    """
    text = " ".join(str(line).split())
    return text[:limit]


def describe(context: Sequence[Event]) -> str:
    """直近の出来事を、プロンプトから見た「今の状況」として描く。"""
    if not context:
        return "まだ何も起きていない。"
    return "\n".join(f"- {ev.kind}: {ev.detail}" if ev.detail else f"- {ev.kind}" for ev in context)
