"""起動時の疏通確認 — どの層が死んでいるかを log 1 行ずつで言う。

### なぜログに出すのか

daemon の log に出ていたのは uvicorn の起動行と `starting` / `port opened` /
`stopped` だけだった。デバイスが黙っているときに疑うべき先はそれより多い —
VOICEVOX が落ちている、`claude` が PATH に無い、socket が bind できていない、
`config.toml` が読まれていない。どれも「独り言を言わない」という同じ症状に
なるので、log を見ただけで層が切り分けられなければ、結局 tool を叩いて
回ることになる。

### 1 項目 1 行

各項目は `Check` 1 つ、log 1 行。成功は INFO、失敗は WARNING。落ちても起動は
続ける: デバイスが刺さっていない daemon も、engine の無い daemon も、
できることが減るだけで役には立つ。

### なぜファイルにも書くのか

`buddy-mcpd status` は daemon とは別のプロセスで、シリアルポートを持って
いないので自分では確かめようがない。pid ファイルと同じ流儀で、daemon が
state ディレクトリへ結果を置き、supervisor はそれを読むだけにする。
`checked_at` を添えるのは、止まっている daemon の health がそのまま残るから。

依存は serve → health → state の一方向。`mcp_state` からここを import する
ことは無い。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import buddy_paths
import mcp_state
from buddy_chatter import ChatterService
from buddy_verbs import voicevox_url
from chatter_core import ChatterConfig

log = logging.getLogger("buddy.health")

# engine も CLI も、応答が無いこと自体が答えなので長く待たない。起動直後の
# 1 回きりの確認で、これを待っているのは health のスレッドだけ。
HTTP_TIMEOUT = 3.0
CLI_TIMEOUT = 10.0
# デバイスは開いているのにアプリが走っていない (REPL で止まっている) とき、
# ack は来ない。tool の既定 (8 秒) より短くしてあるのは、ここでの答えが
# 「返事が無い」で足りるから。
STATUS_TIMEOUT = 4.0

# `config.toml` で触れる設定のうち、起動時に出す価値のあるもの。表示名と、
# それが写る `BUDDY_*` の名前。
SETTING_NAMES: tuple[tuple[str, str], ...] = (
    ("port", "BUDDY_PORT"),
    ("connect_on_start", "BUDDY_CONNECT_ON_START"),
    ("chatter.gap_min", "BUDDY_CHATTER_GAP_MIN"),
    ("chatter.gap_max", "BUDDY_CHATTER_GAP_MAX"),
    ("chatter.model", "BUDDY_CHATTER_MODEL"),
    ("chatter.effort", "BUDDY_CHATTER_EFFORT"),
    ("chatter.voice_every", "BUDDY_CHATTER_VOICE_EVERY"),
)


@dataclass(frozen=True, slots=True)
class Check:
    """項目 1 つの結果。log の 1 行であり、status の 1 要素。"""

    name: str
    ok: bool
    detail: str
    # log には出さないが status には載せる行。ポートが開かなかったことは
    # `mcp_state.connect_on_start` が既に WARNING で言っているので、そこだけ
    # 二度言わない。
    quiet: bool = False

    def emit(self) -> None:
        if self.quiet:
            return
        (log.info if self.ok else log.warning)("%s: %s", self.name, self.detail)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


# ----- 設定


def origin(name: str, process_env: Mapping[str, str], file_env: Mapping[str, str]) -> str:
    """その設定がどこから来たか。env / config / default のどれか。

    合成済みの環境ではなく素の 2 つを見る: `buddy_paths.environment()` は
    優先順位を畳んだ後なので、値がどちらから来たかはもう残っていない。
    空文字を「指定なし」として扱うのは `merge_env` と同じ規則で、
    シェルで `FOO=` と書き損じたときに config の値が生きるのもそのため。
    """
    if process_env.get(name):
        return "env"
    if file_env.get(name):
        return "config"
    return "default"


def effective(cfg: ChatterConfig, port: str, *, connect: bool) -> dict[str, str]:
    """いま効いている値。既定値をここで解決し直さないための引き写し。

    `ChatterConfig` と `connect_on_start_wanted` が決めた結果を受け取って
    並べるだけにしてある。既定値の写しをこのモジュールが持つと、向こうが
    変わったときに黙ってずれる。
    """
    return {
        "port": port,
        "connect_on_start": "1" if connect else "0",
        "chatter.gap_min": f"{cfg.gap_min:g}",
        "chatter.gap_max": f"{cfg.gap_max:g}",
        "chatter.model": cfg.model,
        "chatter.effort": cfg.effort or "(CLI default)",
        "chatter.voice_every": str(cfg.voice_every),
    }


def config_checks(
    process_env: Mapping[str, str],
    file_env: Mapping[str, str],
    values: Mapping[str, str],
    path: Path,
) -> list[Check]:
    """読んだファイルと、有効になった値と、その出どころ。"""
    where = f"file={path}" if path.exists() else f"no file at {path} — defaults only"
    shown = " ".join(
        f"{label}={values[label]}({origin(var, process_env, file_env)})"
        for label, var in SETTING_NAMES
    )
    return [Check("config", True, where), Check("config", True, shown)]


# ----- シリアル


def describe_status(ack: Mapping[str, Any]) -> str:
    """status ack を 1 行にする。無い項目は黙って落とす。

    ファームウェアが何を載せてくるかはこのリポジトリの外で決まるので、
    形は当てにせず、あった項目だけを並べる。
    """
    sys_info = ack.get("sys")
    # `isinstance` だけでは `dict[Unknown, Unknown]` にしか絞られない。
    # 中身の型はデバイスが決めるので、読むときに名前を付ける。
    nested: Mapping[str, Any] = (
        cast("Mapping[str, Any]", sys_info) if isinstance(sys_info, dict) else {}
    )
    fields: tuple[tuple[str, object], ...] = (
        ("version", ack.get("version")),
        ("name", ack.get("name")),
        ("heap", nested.get("heap")),
    )
    parts = [f"{key}={value}" for key, value in fields if value is not None and value != ""]
    return " ".join(parts) if parts else "answered, but said nothing recognisable"


def serial_check(timeout: float = STATUS_TIMEOUT) -> Check:
    """ポートが開いたか、開いたならデバイスが何と答えるか。

    ここからポートを開き直すことはしない。`connect_on_start` の試行は 1 回
    きりで、それが失敗したということはボードが挿さっていないか他のプロセスが
    ポートを持っているかで、どちらも再試行では直らない。
    """
    attempt = mcp_state.startup_connect
    if attempt is None:
        return Check("serial", True, "not opened on start (connect_on_start off)")
    if not attempt.get("ok"):
        return Check("serial", False, f"port not opened: {attempt.get('error', '')}", quiet=True)

    port = str(attempt.get("port", ""))
    # tool と同じロックの下で訊く。握らずに request を出すと ack が入れ違う。
    with mcp_state.device_lock:
        link = mcp_state.live_link()
        if link is None:
            return Check("serial", False, f"{port}: link dropped before it answered")
        try:
            ack = link.request({"cmd": "status"}, "status", timeout=timeout)
        except Exception as exc:
            # 開いてはいるが答えない。アプリが走っておらず REPL で止まって
            # いるときの姿で、chatter から見れば喋れないのと同じ。
            return Check("serial", False, f"{port}: no status ack ({type(exc).__name__}: {exc})")
    return Check("serial", True, f"{port}: {describe_status(ack)}")


# ----- VOICEVOX


Fetch = Callable[[str, float], str]


def _fetch(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read(200).decode("utf-8", errors="replace").strip()


def voicevox_check(fetch: Fetch = _fetch) -> Check:
    """engine の URL と、そこに届くかどうか。

    デバイスが自分で取りに行く先なので、ここで届くことは十分条件ではない
    (デバイスは LAN 越しに同じ URL を叩く)。それでも必要条件ではあり、
    落ちている engine を「デバイスが喋らない」から切り分けられる。
    """
    try:
        url = voicevox_url()
    except ValueError as exc:
        return Check("voicevox", False, str(exc))
    try:
        version = fetch(f"{url}/version", HTTP_TIMEOUT)
    except Exception as exc:
        return Check("voicevox", False, f"{url} unreachable: {type(exc).__name__}: {exc}")
    return Check("voicevox", True, f"{url} version {version}")


# ----- claude CLI


Version = Callable[[str], tuple[int, str]]


def _version_of(binary: str) -> tuple[int, str]:
    proc = subprocess.run(
        [binary, "--version"],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
        check=False,
    )
    return proc.returncode, " ".join((proc.stdout or proc.stderr or "").split())[:120]


def claude_check(cfg: ChatterConfig, version: Version = _version_of) -> Check:
    """台詞を書く CLI が居るか。無ければ chatter は缶詰の台詞に落ちる。"""
    found = shutil.which(cfg.claude_bin)
    if found is None:
        return Check("claude", False, f"{cfg.claude_bin} not on PATH — canned lines only")
    try:
        code, output = version(found)
    except Exception as exc:
        return Check("claude", False, f"{found}: {type(exc).__name__}: {exc}")
    if code != 0:
        return Check("claude", False, f"{found}: --version exited {code}: {output}")
    return Check("claude", True, f"{found} ({output})")


# ----- chatter


def chatter_checks(service: ChatterService) -> list[Check]:
    """socket が bind できたか、worker が回っているか。

    `BUDDY_CHATTER=0` で切ってあるのは失敗ではないので INFO 1 行で済ませる。
    そこを WARNING にすると、意図して黙らせた daemon が毎回怒られる。
    """
    cfg = service.cfg
    if not cfg.enabled:
        return [Check("chatter", True, "disabled by BUDDY_CHATTER=0")]
    socket = Check(
        "socket",
        service.listening,
        f"{cfg.socket_path}" if service.listening else f"{cfg.socket_path}: not bound",
    )
    running = Check(
        "chatter",
        service.running,
        f"running (model {cfg.model}, gap {cfg.gap_min:g}-{cfg.gap_max:g}s)"
        if service.running
        else "not running",
    )
    return [socket, running]


# ----- 実行と保存


def startup_checks(
    env: Mapping[str, str], service: ChatterService, *, connect: bool
) -> list[Check]:
    """起動時に見る項目を、上から順に。

    シリアルが先頭なのは、ポートを開けるのがこの daemon の存在理由で、
    engine と CLI の確認をその前に置くとリンクが上がるのがそのぶん遅れる
    から。
    """
    if connect:
        mcp_state.connect_on_start()
    path = buddy_paths.config_path()
    port = env.get("BUDDY_PORT") or mcp_state.FALLBACK_PORT
    return [
        *config_checks(
            os.environ,
            buddy_paths.config_env(path),
            effective(service.cfg, port, connect=connect),
            path,
        ),
        serial_check(),
        *chatter_checks(service),
        voicevox_check(),
        claude_check(service.cfg),
    ]


def check_on_start(
    env: Mapping[str, str], service: ChatterService, *, connect: bool
) -> list[Check]:
    """確認して、log へ出して、`buddy-mcpd status` のために書き置く。

    daemon の起動スレッドから呼ばれる。ここで投げると起動そのものが止まる
    ので、個々の確認はどれも例外を返り値に畳んでいる。
    """
    checks = startup_checks(env, service, connect=connect)
    for check in checks:
        check.emit()
    save(checks, env)
    return checks


def save(checks: list[Check], env: Mapping[str, str] | None = None) -> None:
    """結果を state ディレクトリへ。書けなくても起動は続ける。

    書いてから差し替えるのは、`buddy-mcpd status` が書き途中のファイルを
    読みうるため。
    """
    path = buddy_paths.health_path(env)
    payload = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "checks": [check.as_dict() for check in checks],
    }
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("health not written to %s: %s", path, exc)


def load(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """最後に書かれた結果。無ければ None。

    `buddy-mcpd status` が読む側。壊れたファイルは無いものとして扱う —
    supervisor は daemon の代わりに確かめられないので、ここで例外を投げて
    status ごと落とすより、health だけ空にする方がまし。
    """
    try:
        raw = buddy_paths.health_path(env).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return None
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None
