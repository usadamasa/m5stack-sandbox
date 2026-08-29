"""届いた 1 行と状態変化の行き先。

`buddy/router.py` は `run()` の中のクロージャだったもので、LCD にもトランス
ポートにも触らない — 呼ぶのは渡された相手だけ。だから何を fake にするかを
考えずに、どの入力がどこへ行くかだけを固定できる。

`dbg.*` の経路は本物の `buddy.debug` を呼ぶ。あれを覗きたくなるのは既に
何かがおかしいときなので、間に fake を挟まない方が意味がある。
"""

import contextlib
import importlib
import io
import sys
import unittest
from unittest import mock

from buddy import router as buddy_router
from device_fakes import FakeBle, FakeChat, FakeGc, FakeProto, FakeSpeech, make_router


class RouterLineTest(unittest.TestCase):
    """どの前置きを持つ行がどこへ行くか。"""

    def test_a_chat_verb_goes_to_the_chat_panel(self) -> None:
        chat, ble, proto = FakeChat(), FakeBle(), FakeProto()
        router = make_router(chat=chat, ble=ble, proto=proto)
        router.on_line(b'{"cmd":"chat.say","text":"hi"}')
        self.assertEqual(chat.names(), ["handle"])
        self.assertEqual(ble.acks(), [{"ack": "chat.say", "ok": True}])
        # 描くのは main loop。ここで立つのは印だけ。
        self.assertTrue(router.chat_dirty)
        self.assertEqual(proto.names(), [])

    def test_a_chat_verb_the_panel_does_not_claim_falls_through(self) -> None:
        chat, proto = FakeChat(), FakeProto()
        chat.ack = None
        router = make_router(chat=chat, proto=proto)
        router.on_line(b'{"cmd":"chat.nope"}')
        self.assertEqual(proto.names(), ["on_line"])
        self.assertFalse(router.chat_dirty)

    def test_a_speak_verb_goes_to_the_player(self) -> None:
        speech, ble, chat = FakeSpeech(), FakeBle(), FakeChat()
        router = make_router(speech=speech, ble=ble, chat=chat)
        router.on_line(b'{"cmd":"speak.say","text":"hi"}')
        self.assertEqual(speech.names(), ["handle"])
        self.assertEqual(ble.acks(), [{"ack": "speak.say", "ok": True}])
        # speech は LCD を触らないので、chat の描き直しは要らない。
        self.assertFalse(router.chat_dirty)
        self.assertEqual(chat.names(), [])

    def test_a_speak_verb_without_a_player_falls_through(self) -> None:
        # `speech` はトランスポートの後に組み立つ。その手前で届いた行は
        # protocol 層へ流れる。
        proto = FakeProto()
        router = make_router(proto=proto)
        router.speech = None
        router.on_line(b'{"cmd":"speak.say"}')
        self.assertEqual(proto.names(), ["on_line"])

    def test_an_ordinary_line_goes_to_the_protocol(self) -> None:
        proto, chat, speech = FakeProto(), FakeChat(), FakeSpeech()
        router = make_router(proto=proto, chat=chat, speech=speech)
        router.on_line(b'{"cmd":"status"}')
        self.assertEqual(proto.calls, [("on_line", (b'{"cmd":"status"}',))])
        self.assertEqual(chat.names(), [])
        self.assertEqual(speech.names(), [])

    def test_nothing_is_intercepted_before_the_transport_exists(self) -> None:
        # ack を返す先がまだ無いので、握れる行でも握らない。
        chat, proto = FakeChat(), FakeProto()
        router = make_router(chat=chat, proto=proto)
        router.ble = None
        router.on_line(b'{"cmd":"chat.say"}')
        self.assertEqual(chat.names(), [])
        self.assertEqual(proto.names(), ["on_line"])

    def test_a_line_with_no_protocol_yet_is_dropped_quietly(self) -> None:
        router = make_router()
        router.proto = None
        router.on_line(b'{"cmd":"status"}')

    def test_the_ack_goes_out_as_one_compact_line(self) -> None:
        # 詰めて書くのは、この 1 行がそのままシリアルに乗るため。
        ble = FakeBle()
        router = make_router(ble=ble)
        router.on_line(b'{"cmd":"chat.say"}')
        self.assertEqual(ble.lines, [b'{"ack":"chat.say","ok":true}'])


class RouterDecodeTest(unittest.TestCase):
    """1 行を JSON にするのはここだけ。verb 層が受けるのは解けた後の dict。"""

    def test_one_line_is_decoded_once_for_every_verb_layer(self) -> None:
        # 前置きを 2 つ持つ行。chat が握らなければ speech へ回るが、
        # そのとき渡るのは chat が見たのと同じ dict でなければならない。
        chat, speech = FakeChat(), FakeSpeech()
        chat.ack = None
        router = make_router(chat=chat, speech=speech, ble=FakeBle())
        router.on_line(b'{"cmd":"speak.say","text":"chat.say"}')
        self.assertEqual(chat.names(), ["handle"])
        self.assertEqual(speech.names(), ["handle"])
        self.assertIs(chat.calls[0][1][0], speech.calls[0][1][0])

    def test_a_line_that_is_not_json_falls_through(self) -> None:
        # 壊れた行の報告は buddy_protocol の持ち場。ここは黙って流す。
        chat, proto = FakeChat(), FakeProto()
        router = make_router(chat=chat, proto=proto)
        router.on_line(b'not json, but it says "chat. anyway')
        self.assertEqual(chat.names(), [])
        self.assertEqual(proto.names(), ["on_line"])

    def test_json_that_is_not_an_object_falls_through(self) -> None:
        chat, proto = FakeChat(), FakeProto()
        router = make_router(chat=chat, proto=proto)
        router.on_line(b'["chat.say"]')
        router.on_line(b'"chat.say"')
        self.assertEqual(chat.names(), [])
        self.assertEqual(proto.names(), ["on_line", "on_line"])


class RouterDebugTest(unittest.TestCase):
    """`dbg.*` は届いてから `buddy.debug` を読み、`dbg.off` で落とす。"""

    def setUp(self) -> None:
        self.gc = FakeGc()
        self.addCleanup(setattr, buddy_router, "gc", buddy_router.gc)
        buddy_router.gc = self.gc
        # unload の経路は sys.modules と package の属性の両方を落とす。他の
        # テストが掴んでいるものを壊さないよう、どちらも戻す。
        self.package = importlib.import_module("buddy")
        module = importlib.import_module("buddy.debug")
        self.addCleanup(setattr, self.package, "debug", module)
        self.addCleanup(sys.modules.__setitem__, "buddy.debug", module)
        self.router = make_router()

    def test_the_first_frame_says_it_pulled_the_module_in(self) -> None:
        # ホストにはこれを自力で知る手立てが無い。起ち上がったばかりの CLI
        # には、前のプロセスが読み込ませたかどうかが分からない。
        ack = self.router.on_dbg({"cmd": "dbg.state"})
        assert ack is not None
        self.assertTrue(ack["entered"])
        self.assertIsNotNone(self.router.dbg)

    def test_a_later_frame_does_not(self) -> None:
        _ = self.router.on_dbg({"cmd": "dbg.state"})
        ack = self.router.on_dbg({"cmd": "dbg.state"})
        assert ack is not None
        self.assertNotIn("entered", ack)

    def test_dbg_state_sees_the_app_s_live_objects(self) -> None:
        # bind() のキーはホストの `dbg.eval` が名前で呼ぶもの。
        ack = self.router.on_dbg({"cmd": "dbg.state"})
        assert ack is not None
        self.assertEqual(ack["ble.advertised_name"], "Claude_serial")

    def test_dbg_off_drops_both_references(self) -> None:
        _ = self.router.on_dbg({"cmd": "dbg.state"})
        ack = self.router.on_dbg({"cmd": "dbg.off"})
        assert ack is not None
        self.assertTrue(ack["unload"])
        self.assertIsNone(self.router.dbg)
        self.assertNotIn("buddy.debug", sys.modules)
        # sys.modules だけでは足りない。MicroPython は submodule を package
        # の属性としても持っていて、そちらの方が長生きする。
        self.assertFalse(hasattr(self.package, "debug"))
        # 参照を落とし切ってから測る。でなければ、そのモジュールがまだ
        # 居るヒープに対して数字を取ることになる。
        self.assertEqual(ack["free"], self.gc.mem_free())
        self.assertGreaterEqual(self.gc.collects, 1)

    def test_an_off_that_had_to_import_first_did_not_enter_anything(self) -> None:
        ack = self.router.on_dbg({"cmd": "dbg.off"})
        assert ack is not None
        self.assertNotIn("entered", ack)

    def test_a_bundle_without_the_module_answers_instead_of_raising(self) -> None:
        # トランスポートのコールバックの中なので、ImportError を逃がすと
        # main loop ごと落ちる。
        delattr(self.package, "debug")
        with mock.patch.dict(sys.modules, {"buddy.debug": None}):
            ack = self.router.on_dbg({"cmd": "dbg.state"})
        assert ack is not None
        self.assertFalse(ack["ok"])
        self.assertIn("buddy.debug", str(ack["err"]))

    def test_a_dbg_line_arrives_through_on_line(self) -> None:
        # 1 行から ack まで、本物の `buddy.debug` を通す唯一の経路。
        # `handle` の名前と引数が router の期待とずれたら、ここが落ちる —
        # あのモジュールは dbg.* が届くまで import されないので、ずれを
        # 実機の前に捕まえる仕掛けは他に無い。
        ble = FakeBle()
        router = make_router(ble=ble)
        router.on_line(b'{"cmd":"dbg.state"}')
        self.assertEqual(len(ble.acks()), 1)
        self.assertTrue(ble.acks()[0]["entered"])


class RouterStateTest(unittest.TestCase):
    def test_connected_is_remapped_and_starts_the_protocol_talking(self) -> None:
        # BuddySerial には pairing の段が無いので、handshake の "connected"
        # が終点になる。UI が PAIR... のバッジを抜けるのはこの読み替え。
        proto = FakeProto()
        router = make_router(proto=proto)
        router.on_state("connected")
        self.assertEqual(router.pending_state, "encrypted")
        self.assertEqual(proto.names(), ["send_hello"])

    def test_another_state_is_only_posted(self) -> None:
        # 描くのは main loop。ここでもやはり印を立てるだけ。
        proto = FakeProto()
        router = make_router(proto=proto)
        router.on_state("disconnected")
        self.assertEqual(router.pending_state, "disconnected")
        self.assertEqual(proto.names(), [])

    def test_the_boot_note_is_said_when_the_host_connects(self) -> None:
        # boot 時の print は誰も聞いていない (host はまだポートを開き直して
        # いない)。reboot の理由が daemon の log に届くのは、繋がった瞬間に
        # もう一度言うから。閉じたときには言わない。
        router = make_router(proto=FakeProto())
        router.boot_note = lambda: "reset_cause=2 up=15035s"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            router.on_state("connected")
        self.assertIn("claude_buddy: boot reset_cause=2 up=15035s\n", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            router.on_state("disconnected")
        self.assertNotIn("boot", out.getvalue())

    def test_without_a_boot_note_the_connect_is_still_reported(self) -> None:
        # `boot_note` は `run()` が後から差す枠。差さっていなくても handshake
        # は進む。
        router = make_router(proto=FakeProto())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            router.on_state("connected")
        self.assertEqual(router.pending_state, "encrypted")
        self.assertNotIn("boot", out.getvalue())


if __name__ == "__main__":
    unittest.main()
