"""serial と net を 1 つのトランスポートに見せる。

Router も upstream の `buddy_protocol` も `ble` を 1 つしか持たない。USB と
WiFi の両方で待つには、2 本を束ねて同じ面を出すものが要る。それがここ。

- `poll()` は全員に回す
- `send_line()` は繋がっている全員へ。`BuddySerial` はホストが 1 行喋るまで
  `False` を返す (REPL の人に ack を撒かない) ので、USB に誰も居なければ
  自然と net だけへ届く。短絡しない — 先頭が断っても後ろへ渡す
- state は「誰か 1 人でも繋がっていれば connected」。2 本目が上がっても
  2 度目の "connected" は出さず、最後の 1 本が切れたときだけ "disconnected"。
  それ以外の state (upstream が知る "encrypted" など) は素通し

組み立ての順に注意: 子は `on_state=mux.child_state` で作るので、mux が先。
`buddy/app.py` の `run()` にその形がある。

MicroPython: `typing` も `__future__` も無い。annotation は組み込みの名前だけ。
"""

# 型検査だけの import。デバイスの上では `False` なので走らない。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import StateCallback, Transport  # noqa: F401


def _noop_state(_st):
    # type: (str) -> None
    return None


class TransportMux:
    """One transport surface over several legs."""

    pairing_supported = False
    encrypted = False

    def __init__(self, on_state=None):
        # type: (StateCallback | None) -> None
        self._on_state = on_state or _noop_state
        self._legs = []  # type: list[Transport]
        self._up = False

    def add(self, leg):
        # type: (Transport) -> None
        self._legs.append(leg)

    # ----- transport surface

    @property
    def advertised_name(self) -> str:
        return "+".join(leg.advertised_name for leg in self._legs)

    @property
    def connected(self) -> bool:
        return any(leg.connected for leg in self._legs)

    def poll(self) -> None:
        for leg in self._legs:
            leg.poll()

    def send_line(self, payload):
        # type: (bytes) -> bool
        taken = False
        for leg in self._legs:
            if leg.send_line(payload):
                taken = True
        return taken

    def disconnect(self) -> None:
        for leg in self._legs:
            leg.disconnect()

    def forget_bonds(self) -> None:
        for leg in self._legs:
            leg.forget_bonds()

    def deinit(self) -> None:
        for leg in self._legs:
            try:
                leg.deinit()
            except Exception as e:
                # 1 本が畳めなくても残りは畳む。ここは去り際で、投げ直す先が無い。
                print("buddy.mux: deinit warning:", e)

    # ----- 子からの state

    def child_state(self, state: str) -> None:
        """子の `on_state`。集約して、変わったときだけ上へ渡す。"""
        if state not in ("connected", "disconnected"):
            self._on_state(state)
            return
        up = self.connected
        if up == self._up:
            return
        self._up = up
        self._on_state("connected" if up else "disconnected")
