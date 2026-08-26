# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 MAS Event + Design, LLC
# Copyright 2026 usadamasa
#
# Modified from moremas/build-with-claude: the USB-serial transport, the
# chat and speech verbs intercepted in `on_line`, and the removal of the
# BLE and keyboard paths. See the docstring.
"""Claude Buddy for the M5 Cardputer-Adv.

Derived from moremas/build-with-claude (Apache-2.0), `buddy/device/apps/
claude_buddy.py`. Two things differ. The transport is the USB CDC
console rather than a BLE peripheral, which is what lets Claude Code
drive the device instead of Claude Desktop's Hardware Buddy (see
`buddy/serial.py`). And the paths upstream drove from hardware buttons
— permission responses, unpair confirmation, quit-to-launcher — are
gone, along with the BLE branches that were kept beside them only so a
diff against upstream stayed readable.

This is a port of the Basic's `buddy_app.py` to a device with a 240x135
LCD instead of 320x240 and no accessible battery IC (Cardputer-Adv
ships with a different power rail that we don't bother reading here).
The wire protocol, persistent state, character-receive logic and the
dashboard itself are upstream and unmodified — `buddy_protocol`,
`buddy_state`, `buddy_chars` and `buddy_ui_cp` are read off flash and
never carried in this repository. What is local is the transport, the
chat panel, and speech.

### Install layout

UIFlow 2.0's default sys.path on this build is roughly:
  ['', '.frozen', '/lib', '/system', '/flash/libs']
Notably /flash itself is NOT on the path, even though that is where
main.py lives. The peer modules sit at /flash/ root and this file in
/flash/apps/, so both are prepended on entry. `buddy_deploy.py` is what
puts them there.

### ここには何も無い

アプリ本体は `/flash/buddy/` の下にある。組み立てと main loop が
`buddy/app.py`、届いた 1 行の振り分けが `buddy/router.py` で、このファイル
に残っているのは sys.path を整えることと、そこへ橋を渡すことだけ。

入力・出口・ハードウェアの境界がどうなっているかは `buddy/app.py` の
docstring にある。

### 起動

import しただけでは何も起きない。起動するのは `run()` を呼んだときで、
呼ぶのは `/flash/main.py` (power-on) と `buddy_link.LAUNCH_SOURCE`
(ホストから) の 2 つ。どちらも import してから `claude_buddy.run()` と
書く。以前はこのファイルの末尾が `run()` を呼んでいて、import そのものが
起動だった — CPython から中を覗くだけでアプリが走り出すので、テストが
書けない形でもあった。
"""

import sys

# 下の `from buddy import app` より前でなければならない。そうでないと
# 読み込みの時点で ImportError になり、それを行儀よく報告する道は無い。
# 何が既に path に載っているかは docstring の "Install layout" にある。
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from buddy import app

# `main.py` と `buddy_link.LAUNCH_SOURCE` がこの名前を呼ぶ。別名にして
# あるのは、`/flash/apps/claude_buddy.mpy` が起動口だという入口の形を、
# 本体をどこへ置くかと切り離しておくため。
run = app.run
