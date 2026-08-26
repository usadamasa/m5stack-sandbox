# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 MAS Event + Design, LLC
# Copyright 2026 usadamasa
#
# moremas/build-with-claude からの派生。`apps/claude_buddy.py` の本体を
# そのまま移したもので、由来と upstream との差は向こうの docstring にある。
"""Claude Buddy の組み立てと main loop。

`apps/claude_buddy.py` は `/flash/apps/` に置く薄い起動口で、中身は
こちら。届いた 1 行の振り分け (`Router`) は `buddy/router.py`。

### ハードウェアの境界

ファームウェアのモジュールと upstream のピアを import するのは `run()`
の中だけ。このモジュールを import しただけでは何も要求しないので、
`run()` を呼ばない限りホストの CPython でも読める。テストが差し替えるの
もこの境界で、`sys.modules` へ fake を置いてから `run()` を呼ぶ
(`device/tests/test_app.py`)。

### 入力

無い。キーボードは読まず、入力はホストからのシリアルだけ。upstream は
Y / N / Q を approve / deny / quit に割り当てていたが、この build には
答えるべき permission prompt が無く (ホストが送らない)、戻る launcher も
無い。デバイス側の入力が戻ってくるのは issue #33 で、向きはキーではなく
音声。

dashboard の下端のヒントは今も Y/N/Q と読める。描いているのは
`buddy_ui_cp` で、あれは upstream のもので flash にしか無いので、文言を
直すにはあのモジュールを引き取ることになる。何と書くべきかを #33 が
決めるまで、そのままにしてある。

### 出口

Ctrl-C。下の main loop に KeyboardInterrupt として届き、トランスポートを
畳んで生きた REPL の上へ返る (`_shutdown` の `to_repl` の枝)。意図した出口は
これだけ。ループが終わるもう 1 つの道は捕まえ損ねた例外で、そちらは
reboot する: UIFlow 2.0 には launcher へ戻る API が無く、ユーザーアプリの
`run()` が終わっても launcher は描き直さないので、画面はアプリが最後に
描いたところで固まる。`machine.reset()` なら launcher まで戻れる。
"""

import gc
import sys
import time

from buddy.router import Router


# ---- battery stub
#
# Basic の buddy_app.py は IP5306 を I2C(0, sda=21, scl=22) 越しに読む。
# Cardputer-Adv の電源まわりはまるごと別物で、IP5306 は載っておらず、
# バッテリと USB の状態はここでは配線していないチップの中にある。protocol
# 層と UI 層が期待する形だけは返るよう、読み取りをスタブで塞いでおく。
# footer は "100%  USB" のまま動かない — 意図した嘘だが、害の無い嘘。
# 誰かが register map を掘り起こしたら、本物の AXP2101/AW9523 の読み取りへ
# 差し替えればいい。
def _stub_battery():
    # type: () -> dict[str, object]
    return {"pct": 100, "mV": 0, "mA": 0, "usb": True}


# stats の footer を描き直す間隔。
_FOOTER_INTERVAL = 3000


def _redraw_dashboard(ui, state):
    # type: (object, object) -> None
    """chat が panel を返してきた後の描き直し。

    chat は header まで覆っているので、dashboard には main panel だけで
    なく chrome の全面描き直しが要る。`_redraw_chrome` はまさにそのための
    upstream 自身の private なヘルパで、将来の bundle で名前が変わったら、
    header 以外を全部カバーする public な呼び出しへ落ちる。
    """
    redraw = getattr(ui, "_redraw_chrome", None)
    if redraw is not None:
        redraw()
    else:
        ui.update_heartbeat({})  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        ui.restore_button_hints()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
    ui.update_footer(state.stats(), _stub_battery())  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]


def _serve_ui(router, last_footer_ms):
    # type: (Router, int) -> int
    """溜めておいた UI の仕事を捌く。新しい footer の時刻を返す。

    main loop が LCD を触る唯一の場所。周期的な footer の描画と、protocol の
    イベントが起こす prompt の描画が交錯しないよう、書き込みはすべてここへ
    集める。とくに set_connection は header の帯をまるごと描き直し、これは
    SPI のトランザクション何本ぶんもある。

    描くのに要るものは全部 router が持っている (`ui` / `chat` / `state` /
    `speech`) ので、渡すのは router と時計だけ。tick ごとに増える呼び出しを
    この 1 つに抑えるため、描画の順も条件も 1 つの関数に畳んである。
    """
    # `Router` はこれらを `object` として持っている (トランスポートより先に
    # 組み立つので具体型が置けない)。呼び出しを行単位で無視しているのは
    # そのためで、事情は `buddy/router.py` の側にも書いてある。
    ui = router.ui
    chat = router.chat
    state = router.state

    new_state = router.pending_state
    if new_state is not None:
        router.pending_state = None
        ui.set_connection(new_state)  # pyright: ignore[reportUnknownMemberType]
        if chat.active:  # pyright: ignore[reportUnknownMemberType]
            # set_connection は main panel を描き直す。transcript が
            # 出ているあいだ、そこは chat のもの。
            chat.render()  # pyright: ignore[reportUnknownMemberType]

    if router.chat_dirty:
        router.chat_dirty = False
        if chat.active:  # pyright: ignore[reportUnknownMemberType]
            chat.render()  # pyright: ignore[reportUnknownMemberType]
        else:
            # chat.clear が panel を返してきた。
            _redraw_dashboard(ui, state)

    now = time.ticks_ms()
    if time.ticks_diff(now, last_footer_ms) < _FOOTER_INTERVAL:
        return last_footer_ms
    state.tick_nap()  # pyright: ignore[reportUnknownMemberType]
    # stats の footer は y=96..110 に描く。そこは chat の領域の内側で、
    # これを飛ばすことが transcript を 3 秒ごとに打ち抜かないでいる理由に
    # なっている。SPI のトランザクション何本ぶんもあり、再生中の tick には
    # 余裕が無い — underrun は耳に付くが、3 秒古いバッテリ表示は付かない。
    if not chat.active and not router.speech.active:  # pyright: ignore[reportUnknownMemberType]
        ui.update_footer(state.stats(), _stub_battery())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return now


def _shutdown(router, buddy_ui, m5, to_repl):
    # type: (Router, object, object, bool) -> None
    """main loop を抜けた後の畳み方。reboot するかどうかは呼び出し側。

    順は buddy_app.py に倣う: まずトランスポート、次に画面を黒で塗り潰し、
    それから制御を返す。トランスポートを落とす前に stop(): あれはトランス
    ポート越しの転送を切り、キューに 1 秒ぶんの音声を抱えたままのスピーカーを
    黙らせる唯一のもの。
    """
    try:
        router.speech.stop()  # pyright: ignore[reportUnknownMemberType]
    except Exception as e:
        print("claude_buddy: speech stop warning:", e)
    try:
        router.ble.deinit()  # pyright: ignore[reportUnknownMemberType]
    except Exception as e:
        print("claude_buddy: deinit warning:", e)
    try:
        m5.Lcd.fillScreen(buddy_ui.BLACK)  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
    except Exception as e:
        print("claude_buddy: screen-clear warning:", e)
    if to_repl:
        # 黒い画面で reboot もしないのは、机の向こうから見れば文鎮化と
        # 区別が付かない。どちらなのかを言うには一語で足りる。
        try:
            m5.Lcd.setTextColor(buddy_ui.GRAY_DIM, buddy_ui.BLACK)  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            m5.Lcd.drawString("REPL", 8, 8)  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        except Exception as e:
            print("claude_buddy: repl banner warning:", e)
    # 短く待つのは、末尾の log 行が USB コンソールの上で行の途中から
    # 切られないようにするため。
    time.sleep_ms(200)
    if to_repl:
        # deinit() が Ctrl-C を戻し、トランスポートが握っていた stdin も
        # 手放しているので、run() から返れば生きたプロンプトの上に着き、
        # アプリのオブジェクトは回収できる状態になる。それが interrupt の
        # 目的そのもので、ここで reset したらキーを 1 つ押して辿り着いた
        # REPL を捨てることになる。
        print("claude_buddy: at the REPL. machine.reset() to restart.")


def run():
    # type: () -> None
    """アプリを起動する。戻るのは Ctrl-C で REPL へ抜けたときだけ。"""
    # ファームウェア自身のモジュールと、flash にあってこのリポジトリには
    # 無い upstream のピア。後者は `buddy_deploy.py` が基板から読む。
    # `/flash` と `/flash/apps` を sys.path へ入れるのは呼び出し側の仕事で、
    # ここへ来る時点で済んでいる (`apps/claude_buddy.py` を見ること)。
    import buddy_chars
    import buddy_protocol
    import buddy_state
    import buddy_ui_cp as buddy_ui
    import M5
    import machine

    # こちらは device/buddy/ の下、このリポジトリのもの。
    from buddy import chat as buddy_chat
    from buddy import serial as buddy_serial
    from buddy import speak as buddy_speak

    # 段ごとに print するのは、init の途中のハードフォールト (例えば LCD
    # ドライバの crash) が、どの段で落ちたかを指すパンくずをシリアルコン
    # ソールへ残すため。C レベルの crash は try/except をすべて素通りして
    # チップを reboot させるので、reboot の直前の print だけが手がかりになる。
    print("claude_buddy: run() start")

    ui = buddy_ui.BuddyUI()
    print("claude_buddy: ui ready")
    # transcript が出ているあいだ、main panel (y=22..110) はこちらのもの。
    # header とヒントの帯は BuddyUI が持ったままだが、あちらの footer は
    # こちらと重なるので、下のループでは update_footer を `chat.active` で
    # 抑えている。
    chat = buddy_chat.ChatPanel()
    print("claude_buddy: chat ready", chat.info())
    state = buddy_state.BuddyState()
    print("claude_buddy: state ready")
    ui.update_identity(state.name, state.owner)

    buddy_chars.sweep_partials()
    chars = buddy_chars.CharReceiver()
    print("claude_buddy: chars ready")

    # コールバックが読むものはすべてここに集まる。`ble` / `speech` / `proto`
    # は組み立ててから差す — protocol はトランスポートの handle (disconnect /
    # forget_bonds のため) を要り、トランスポートは `on_line` を要り、その
    # `on_line` が protocol を要る、という循環をこれで解く。
    router = Router(ui, chat, state, chars)

    # トランスポートと speech player がバッファを確保する前に、一度通しで
    # 集める。ここまでの UI・chat panel のフォント・state ファイルがヒープを
    # かき混ぜていて、断片を返せる最後の静かな瞬間がここ。
    gc.collect()
    print("claude_buddy: gc done, free=", gc.mem_free())
    # `ble` はトランスポートに対する upstream の名前で、`dbg.eval` がそれを
    # 束ねる名前でもある。ここで入っているのは BuddySerial。
    ble = buddy_serial.BuddySerial(on_line=router.on_line, on_state=router.on_state)
    router.ble = ble
    print("claude_buddy: transport ready")

    # speech は WiFi 越しに engine へ届く。その WiFi は main.py が boot で
    # 上げたもの。トランスポートが絡むのは ack だけ。
    speech = buddy_speak.SpeechPlayer(ble)
    router.speech = speech
    print("claude_buddy: speech ready")

    proto = buddy_protocol.BuddyProtocol(
        state=state,
        ui=ui,
        chars=chars,
        ble=ble,
        battery_reader=_stub_battery,
    )
    router.proto = proto

    ui.update_footer(state.stats(), _stub_battery())

    # 大きいものはここまでで確保し終えているので、遅くではなく早めに集めろ
    # と collector へ言うならこの瞬間。既定は確保に失敗してから集めるという
    # もので、これだけ断片化したヒープの上ではその時点でもう手遅れになる —
    # `import buddy_ui_cp` を落とした MemoryError のときは 55 KB 空いていた。
    # ドキュメントのレシピ: 空きヒープの 4 分の 1 を配ったら一度集める。
    gc.collect()
    gc.threshold(gc.mem_alloc() + gc.mem_free() // 4)
    print("claude_buddy: gc threshold set, free=", gc.mem_free())
    print("Claude Buddy up as", ble.advertised_name)

    last_footer_ms = time.ticks_ms()

    # 下の Ctrl-C ハンドラが立てる。reboot しない唯一の出口で、押す目的は
    # REPL を得ることなのに、reset すると同じプロンプトを返すまでに WiFi の
    # 上げ直しで 10 秒使うことになる。
    to_repl = False

    try:
        while True:
            # このトランスポートは自前のコールバックを持たない。stdin を
            # 汲んで、行が揃ったものを on_line の呼び出しに変えるのはここ。
            ble.poll()

            # トランスポートを汲んだ直後、描くもの全部より前に置く: 2 KiB の
            # ブロック 1 つが 64 ms の音声で、読むのに ~11 ms かかる。40 ms の
            # tick が再生に先行していられるのは、遅いものが前に入らない
            # あいだだけ。
            speech.pump()

            # 溜めておいた UI の仕事をここで捌く。tick ごとに増える呼び出し
            # はこの 1 つだけで、LCD を触る場所も引き続きここ 1 箇所。
            last_footer_ms = _serve_ui(router, last_footer_ms)

            # 40 ms は buddy_app.py に合わせてある。上の speech の pump が
            # これを基準に寸法を取っている: 2 KiB のブロック 1 つが 64 ms の
            # 音声なので、この長さの tick なら再生に先行していられる。
            time.sleep_ms(40)
    except KeyboardInterrupt:
        # また届くようになった。`buddy.serial` は以前 Ctrl-C を殺していた —
        # JSON の payload が 0x03 を運びうるため。ホストが制御バイトを
        # escape するようになり、生のチャネルを要求していたバイナリの bulk
        # モードも無くなったので、interrupt は戻ってきて、ここへ落ちてくる。
        to_repl = True
        print("claude_buddy: interrupted")
    except Exception as e:
        # 下の `finally` は reboot する。そして reboot はコンソールより速い:
        # これが無いと crash を説明する traceback は行の途中で切れるか、
        # そもそも出ない。投げ直さずに握るのは、どのみち reset がプロセスを
        # 終わらせるからで、投げ直しても 2 つ目の印字できない例外を招くだけ。
        print("claude_buddy: unhandled exception in the main loop")
        # MicroPython にしか無い。CPython の本物の `sys` (basedpyright が
        # ここで見ているのは typeshed のそれ) には無いので、属性そのものが
        # basedpyright には Unknown に見える。
        sys.print_exception(e)  # pyright: ignore[reportUnknownMemberType]
    finally:
        _shutdown(router, buddy_ui, M5, to_repl)
        if not to_repl:
            # UIFlow には launcher へ戻る API が無く、App List へ帰る道は
            # machine.reset() だけ。hello_cardputer.py も同じやり方をしている。
            machine.reset()
