"""MCP server の起動口 — ログの設定、transport の選択、後始末。

コンソールスクリプト `buddy-mcp` と `python -m buddy_mcp_serve` が着地する
先。tool を並べた 3 つのモジュールをここが import することで、serve に入る
時点では `mcp_state.server` への登録が全部済んでいる。

依存は serve → tools → state の一方向。下の 2 層はここを import しない。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

# server は任意の cwd からエージェントに起動されるので、隣のモジュールは
# 作業ディレクトリ頼みではなく絶対パスで import できるようにしておく。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import buddy_mcp
import buddy_paths
import mcp_chatter_tools
import mcp_debug_tools
import mcp_state
from mcp_state import DEFAULT_PORT, HTTP_HOST, HTTP_PATH, SHUTDOWN_TIMEOUT, server

# tool を並べた 3 つのモジュール。import すること自体が `server` への登録を
# 兼ねるので、`serve_http` や `server.run` に入る時点では 17 個すべてが載って
# いる。使っていない import ではなく起動手順の一部なので、こうして名前を
# 束ねて静的解析にもそう伝える。
TOOL_MODULES = (buddy_mcp, mcp_chatter_tools, mcp_debug_tools)

# ----- logging
#
# daemon は誰も見ていないファイルへ書き、そのファイルは再起動をまたいで追記
# される。ここから 2 つのことが従う。行にはすべてタイムスタンプが要る —
# さもないと行をどの run に帰属させられないし、このファイルに最初に訊かれる
# のは常に「いつ止まったのか」だから。そして run ごとに自分が何者かを言う
# 必要がある。`Started server process [19259]` だけでは、どのデバイスの
# どの transport なのかが分からない。

LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

log = mcp_state.log


def configure_logging() -> None:
    """uvicorn のものも含めて、全ての行にタイムスタンプを付ける。

    uvicorn の既定は日付の無い `INFO:     message` で、
    `run_streamable_http_async` には `log_config` を通す道が無い — なので
    server を組み立てる前に、同梱の設定辞書をその場で書き換える。冪等にして
    あるのは、2 回前置きするくらいなら 1 回も前置きしない方がましだから。

    `basicConfig` は uvicorn 以外の全部を賄う: MCP SDK はセッションの生存期間
    を root logger 経由の INFO で吐いていて、その行にも同じ扱いが要る。
    `force=True` なのは、root logger に handler が 1 つでも付いていると
    `basicConfig` が何もしないから。ここが走る頃には大抵何かが handler を
    入れていて、実際そのせいで最初の版は uvicorn の行だけタイムスタンプ付き、
    他は素のまま、という形で出荷された。
    """
    from uvicorn.config import LOGGING_CONFIG

    logging.basicConfig(format=LOG_FORMAT, datefmt=LOG_DATEFMT, level=logging.INFO, force=True)
    formatters = cast("dict[str, dict[str, Any]]", LOGGING_CONFIG["formatters"])
    for spec in formatters.values():
        fmt = str(spec["fmt"])
        if "%(asctime)s" not in fmt:
            spec["fmt"] = f"%(asctime)s {fmt}"
        spec["datefmt"] = LOG_DATEFMT


# ----- entry point


Transport = Literal["stdio", "streamable-http"]


def transport_options(
    argv: Sequence[str], env: Mapping[str, str]
) -> tuple[Transport, dict[str, Any]]:
    """どの transport で listen するか、そしてどう listen するか。

    起動を伴わずに読めるよう `main` から切り出してある。

    既定は `stdio` のまま: このプロセスを spawn したエージェントがそれを所有
    していて、クライアントは構造上 1 つしか居ない。`--http` は常駐 daemon が
    要求するもので、複数のセッションが 1 本のシリアルポートを共有できる唯一の
    道でもある。

    stateless は意図的。streamable HTTP は放っておくと server 側にセッション
    id を持つので、daemon の再起動 — この仕掛けが安く済ませようとしている
    まさにそれ — が、既に繋がっているクライアントを全部 404 にしてしまう。
    """
    parser = argparse.ArgumentParser(prog="buddy-mcp", description=buddy_mcp.__doc__)
    parser.add_argument(
        "--http", action="store_true", help="listen on streamable HTTP instead of stdio"
    )
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default 8787)")
    args = parser.parse_args(list(argv))
    if not args.http:
        return "stdio", {}
    port = args.port if args.port is not None else mcp_state.http_port(env)
    return "streamable-http", {
        "host": HTTP_HOST,
        "port": port,
        "stateless_http": True,
    }


def serve_http(options: Mapping[str, Any]) -> None:
    """streamable HTTP で serve する。graceful shutdown に上限を付けて。

    `server.run("streamable-http")` 経由ではなくここで組み立てているのは、
    あちらが host・port・log level だけから自前の `uvicorn.Config` を作るから。
    `timeout_graceful_shutdown` はそこからは届かず、これが無いと stop が
    飛んでいる 1 本のリクエストで無限に固まりうる。`SHUTDOWN_TIMEOUT` を参照。
    """
    import uvicorn

    app = server.streamable_http_app(
        streamable_http_path=HTTP_PATH,
        stateless_http=bool(options["stateless_http"]),
        host=str(options["host"]),
    )
    config = uvicorn.Config(
        app,
        host=str(options["host"]),
        port=int(options["port"]),
        log_level=server.settings.log_level.lower(),
        timeout_graceful_shutdown=SHUTDOWN_TIMEOUT,
    )
    uvicorn.Server(config).run()


def main(argv: Sequence[str] | None = None) -> int:
    """server を走らせる。コンソールスクリプト `buddy-mcp` がここに着地する。"""
    env = buddy_paths.environment()
    transport, options = transport_options(sys.argv[1:] if argv is None else argv, env)
    configure_logging()
    # 追記されていくログの中でのこの run の見出し: どの transport で、どこで
    # listen し、どのデバイスへ手を伸ばすのか。
    where = f" on {HTTP_HOST}:{options['port']}" if transport != "stdio" else ""
    log.info("starting: transport=%s%s device=%s", transport, where, DEFAULT_PORT)
    # import 時ではなくここで始める: このモジュールの import が socket を
    # bind したりスレッドを起こしたりしてはいけない。さもないとテスト (と、
    # 単に server を覗くだけの道具) が生きた server と競合する。
    mcp_state.chatter_service().start()
    if mcp_state.connect_on_start_wanted(env, default=transport == "streamable-http"):
        # 別スレッドで: ポートを開くのは reader とハンドシェイクなので、
        # エージェントの `initialize` をその後ろに並ばせない。
        threading.Thread(
            target=mcp_state.connect_on_start, name="buddy-connect", daemon=True
        ).start()
    install_shutdown_handlers()
    try:
        if transport == "stdio":
            server.run("stdio")
        else:
            serve_http(options)
    finally:
        _shutdown()
        log.info("stopped")
    return 0


def _shutdown() -> None:
    """socket とポートを手放す。例外は投げず、何度呼んでもよい。

    これが無いと stop がデータグラム socket を置き去りにし、`buddy-mcpd stop`
    が「殺された daemon」と見分けが付かなくなっていた。

    socket よりポートの方が重い: 次にそれを欲しがるのは大抵
    `buddy_deploy.py` で、`buddy-mcpd stop` はまさにそれを渡すために在る。
    """
    if mcp_state.chatter is not None:
        with contextlib.suppress(Exception):
            mcp_state.chatter.stop()
    with contextlib.suppress(Exception):
        buddy_mcp.buddy_disconnect()


def shutdown_on_signal(signum: int, _frame: object = None) -> None:
    """後始末をしてから、送り主が求めたとおりの死に方をする。

    `sys.exit` ではなく既定へ戻して投げ直すのは、supervisor が見る終了
    ステータスが「返り値 0」ではなく「SIGTERM で終了」であるべきだから。
    `_shutdown` は 2 回走っても安全で、それは `main` の `finally` もそこへ
    到達しうるから重要になる。
    """
    _shutdown()
    signal.signal(signum, signal.SIG_DFL)
    signal.raise_signal(signum)


def install_shutdown_handlers() -> None:
    """uvicorn が借りる前に SIGTERM と SIGINT を自分のものにする。

    uvicorn は `serve()` の間この 2 つを捕まえ、抜けるときに事前に入って
    いたものを復元して捕まえた分を投げ直す — プロセスが送り主の求めたとおりに
    終わるように。SIGTERM の既定の動作はその場で死ぬことで、それが `main` の
    `finally` が走らず、綺麗に止めた daemon が socket を置き去りにしていた
    理由だった。先にこれを入れておけば、uvicorn が復元するのはこの handler に
    なる。

    lifespan の shutdown は代わりにならない: `force_exit` が立っていると
    uvicorn はそれを丸ごと飛ばすし、それこそセッションが繋がったままの
    `buddy-mcpd stop` が踏むケースそのものだから。
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown_on_signal)


if __name__ == "__main__":
    raise SystemExit(main())
