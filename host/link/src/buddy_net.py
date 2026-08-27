"""デバイスへ WiFi (TCP) で繋ぐ側。`buddy_wire.SerialPort` の顔をした socket。

`tcp://host[:port]` という target を `ResidentLink` / `BuddyLink` が受け取ると、
`serial.Serial` の代わりにここが開く。framing は USB と同じなので、上に載る
`LineDemux` も verb も変わらない。
"""

from __future__ import annotations

# デバイス側の `buddy.netlink.PORT` と同じ値 (`device/tests/test_netlink.py` が
# 突き合わせる)。
DEFAULT_PORT = 8788
