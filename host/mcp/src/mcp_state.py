"""MCP server の土台 — `server` 本体と、デバイスへのリンクを持つ状態。

tool を並べた 3 つのモジュール (`buddy_mcp` / `mcp_debug_tools` /
`mcp_chatter_tools`) と、それらを起動する `buddy_mcp_serve` の全部がここを
見る。依存は serve → tools → state の一方向で、ここから上のどれかを import
することは無い。

リンクは 1 本、ロックも 1 本。`link` と `chatter` と `startup_connect` は
代入で差し替わるので、**参照する側は `from mcp_state import link` ではなく
`mcp_state.link` とモジュール経由で読むこと**。from import は差し替え前の値の
写しを取るだけで、その後の代入が届かなくなる。テストが差し替えるのもここの
属性なので、写しを持つとテストがそのまま本物のシリアルポートを開きに行く。

`ResidentLink` をこのモジュールの名前として持っているのも同じ理由で、テストは
これを stub に差し替えて `get_link` の振る舞いを見る。
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from collections.abc import Generator, Mapping
from typing import Any

# server は任意の cwd からエージェントに起動されるので、隣のモジュールは
# 作業ディレクトリ頼みではなく絶対パスで import できるようにしておく。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer

import buddy_paths
from buddy_chatter import ChatterService
from chatter_core import ChatterConfig
from resident_link import ResidentLink

# macOS で Cardputer-Adv が現れるデバイスノード。定数にしてあるのは
# `buddy-mcpd status` も解決済みのポートを報告するからで、この文字列の写しが
# 2 つあると必ずずれる。
FALLBACK_PORT = "/dev/cu.usbmodem101"
DEFAULT_PORT = buddy_paths.environment().get("BUDDY_PORT") or FALLBACK_PORT

# 常駐 daemon が listen する先であり、`.mcp.json` が登録している先。登録は
# 静的な URL なので、この番号は好みではなく取り決めになる: `config.toml` で
# 変えたら登録側も変えることになる。ずれは `buddy-mcpd status` が印字する
# 実際の URL に出る。
DEFAULT_HTTP_PORT = 8787
HTTP_HOST = "127.0.0.1"
HTTP_PATH = "/mcp"

# uvicorn が開いている接続を待ってよい上限。
#
# 上限は要る。`Server._wait_tasks_to_complete` は `await server.wait_closed()`
# で終わるが、これには `force_exit` の逃げ道が無い — リクエストが飛んでいる
# 最中に届いた stop は、二度と戻らないかもしれないクライアントを待つことに
# なる。そのあと supervisor が daemon を SIGKILL するので、このプロセスの
# 誰もシリアルポートも socket も手放せない。
#
# `buddy_mcpd.TERM_GRACE` の中に `buddy_mcp_serve._shutdown` の分 (約 1 秒) の
# 余地を残すこと。さもないと上限を設けた意味が無い。
SHUTDOWN_TIMEOUT = 3

log = logging.getLogger("buddy.mcp")

server = MCPServer(
    name="buddy",
    version="0.1.0",
    instructions=(
        "Talks to an M5Stack Cardputer-Adv running the Claude Buddy app over "
        "USB serial. Call probe_serial first on a new machine or after a "
        "sandbox settings change; if it reports tcsetattr failure, no other "
        "tool here will work. The running app has no REPL of its own — use "
        "buddy_debug to inspect it in place, and buddy_interrupt to drop it "
        "back to a prompt without touching the board."
    ),
)

link: ResidentLink | None = None

# デバイスとの 1 往復 — 送信と、それに答える ack — の間ずっと、デバイスに
# 触る誰もがこれを握る。再入可能ではない: ここの tool は入れ子にならないし、
# chatter 側の `acquire(blocking=False)` が「誰かが request の途中だ」を
# 意味するには素のロックである必要がある。
device_lock = threading.Lock()

chatter: ChatterService | None = None

# ポートを持っているつもりかどうか。持っているつもりなら、その対象。
#
# bool ではなくポート文字列なのは、`mcp_supervisor` が開き直す先がここにしか
# 残らないため。`link` は死ぬと畳まれてしまうので、そこからは「何を開いて
# いたか」を取り返せない。
#
# 誰が書くか: `connect_on_start` は**試行の時点で**、`get_link` は開いた後に、
# `buddy_disconnect` は `None` を。試行の時点で書くのは、起動時にポートを
# 開けなかった run がそのまま最後まで黙っていたから — ボードを挿し直せば
# 直る類の失敗で、意図の方を残しておけば supervisor が拾える。
#
# `None` を書くのは `buddy_disconnect` だけ。それが「`buddy_disconnect` が
# 最後の言葉」を構造として守っている: 手放したポートを supervisor が取り返す
# 道が無い。
wanted: str | None = None


def get_link(port: str | None = None) -> ResidentLink:
    global link, wanted
    target = port or DEFAULT_PORT
    if link is not None and link.connected and link.port != target:
        link.disconnect()
        link = None
    if link is not None and link.dropped:
        # デバイスが下で reboot すると USB が再列挙され、握っている fd は
        # 以後 ENXIO しか返さない。`connected` は開いたつもりのまま True なので、
        # ここで畳まないと同じ死んだ handle を配り続けることになる。
        log.info("link dropped (device reset?); reopening %s", link.port)
        link.disconnect()
        link = None
    if link is None or not link.connected:
        link = ResidentLink(target)
        link.connect()
    wanted = link.port
    return link


@contextlib.contextmanager
def device(port: str | None = None) -> Generator[ResidentLink]:
    """tool 1 回ぶんデバイスを占有する。必要ならポートを開く。"""
    with device_lock:
        yield get_link(port)


def live_link() -> ResidentLink | None:
    """既にリンクが上がっていればそれを、無ければ None。ポートは開かない。

    chatter に渡すのはこちら。代わりに `get_link` を渡すと、台詞を思いつく
    たびに chatter がポートを取り返しに行くことになる。それこそが
    `buddy_deploy.py` と `esptool` にとって困ることで、ポートを誰が持つかは
    `buddy_disconnect` が最後の言葉であり続けなければならない。

    下の `connect_on_start` が唯一の例外だが、あれは起動時の 1 回きりの試行で
    あって、この関数から誘発できるものではない。

    **死んだリンクは渡さない。** デバイスが下で reboot すると reader スレッドが
    `dropped` を立てて降りるが、`connected` は開いたつもりのまま True になって
    いる。それを渡すと chatter は書くたびに ENXIO で失敗し、台詞ごとに WARNING
    を出し続ける (実機で 16 分そうなった)。ここで None を返せば chatter は
    「繋がっていない」として数える。

    ここで開き直さないのは、この関数がデバイスロックの外から呼ばれるため。
    開き直すのは `mcp_supervisor` の周期処理で、あちらはロックを取ってから開く。
    tool 呼び出し (`get_link`) と daemon の再起動もそうするが、待っていたのは
    それだけだった頃、誰も tool を呼ばない 4 時間まるごと黙ったことがある。
    """
    if link is None or not link.connected or link.dropped:
        return None
    return link


# `connect_on_start` がその 1 回の試行をどう思ったか。走らなかったなら None。
# `buddy_chatter_status` から報告される: ポートが一度も開かれなかったせいで
# 黙っている chatter は、`skipped_offline` からはセッション途中でデバイスを
# 抜かれた場合と見分けが付かない。
startup_connect: dict[str, Any] | None = None


def connect_on_start_wanted(env: Mapping[str, str], *, default: bool = False) -> bool:
    """起動と同時にポートを取りに行くべきかどうか。

    既定値がプロセスの種類で変わるので引数にしてある。エージェントに stdio で
    起動された server は客なので、頼まれるまでポートには触らない — 掴んで
    しまうとポートを空けておきたい `buddy_deploy.py` が驚く。常駐 daemon は
    逆で、ポートを持つことこそが存在理由であり、頼まれるまで待つ daemon は
    どこかのセッションがたまたま `buddy_connect` を呼ぶまでデバイスを黙らせて
    しまう。

    どちらにせよ `BUDDY_CONNECT_ON_START` (`config.toml` なら
    `connect_on_start`) が最後の言葉を持つ。
    """
    raw = env.get("BUDDY_CONNECT_ON_START", "")
    if not raw:
        return default
    return raw in ("1", "true", "yes")


def connect_on_start(port: str | None = None) -> dict[str, Any]:
    """chatter のためにポートを 1 回だけ開く。例外は投げない。

    セッション最初の tool 呼び出しと並んで別スレッドで走るので、他と同じく
    `device_lock` を取る。空いているポートに対する `get_link` は reader
    スレッドとハンドシェイクであってデバイスとの往復ではないが、その最中に
    落ちてきた tool 呼び出しは組み立て途中のリンクを掴むことになる。

    ここでの試行は 1 回だけ。失敗するのはボードが挿さっていないか別プロセスが
    ポートを持っているかで、どちらもその場での再試行では直らない。代わりに
    `wanted` へ意図を残すので、後で挿し直されたぶんは `mcp_supervisor` が
    拾い直す。この結果を待っている者は居ないので、例外は黙ってスレッドを
    終わらせるだけになる。だから記録する。
    """
    global startup_connect, wanted
    # 開けたかどうかではなく、開こうとしたことを残す。開けなかった run が
    # そのまま最後まで黙っていたのがこれが無かったときの姿。
    wanted = port or DEFAULT_PORT
    try:
        with device(port) as opened:
            startup_connect = {"ok": True, "port": opened.port}
            log.info("port opened: %s", opened.port)
    except Exception as exc:
        startup_connect = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        # error ではなく warning: デバイスが無くても daemon は役に立つし、
        # ボードが抜けているのは異常ではなく普通の状態でもある。ただしログに
        # は残す — さもないとポートが開かれなかったことを知る手段が tool に
        # 訊くことしか無くなる。
        log.warning("port not opened: %s", startup_connect["error"])
    return startup_connect


def chatter_service() -> ChatterService:
    global chatter
    if chatter is None:
        chatter = ChatterService(ChatterConfig.from_env(), live_link, device_lock)
    return chatter


def decode_logs(logs: list[bytes]) -> list[str]:
    return [line.decode("utf-8", errors="replace") for line in logs]


def http_port(env: Mapping[str, str]) -> int:
    """設定されたポート、または取り決めの既定値。

    読めない値は例外ではなくフォールバックにする: この server に届く登録は
    既定ポートを指す静的な URL なので、誰も変えたつもりの無い設定の書き損じで
    daemon が起動を拒むと、それだけで全体が止まる。
    """
    try:
        return int(env.get("BUDDY_HTTP_PORT", ""))
    except ValueError:
        return DEFAULT_HTTP_PORT
