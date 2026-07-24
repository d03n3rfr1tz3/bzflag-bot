"""Tests für das UDP-Datagramm-Bündel-Parsing (_dispatch_udp_datagram).

bzfs bündelt mehrere Nachrichten pro UDP-Datagramm (NetHandler::pwrite sammelt
bis MaxPacketLen=1024); der echte Client (ServerLink::read) leert das ganze
Datagramm, bevor er das nächste empfängt. Der Bot muss deshalb ALLE Nachrichten
eines Datagramms dispatchen — nur die erste zu parsen verliert alle dahinter
gebündelten (Livetest: ~25 von 29 MsgShotBegin verschluckt).
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bzflag.protocol import MsgShotBegin, MsgPlayerUpdate  # noqa: E402


def _make_client():
    from bzflag.client import BZFlagClient
    return BZFlagClient("localhost", 5154)


def _msg(code: int, payload: bytes) -> bytes:
    """[uint16 len][uint16 code][payload] — Wire-Format wie pack_msg."""
    return struct.pack(">HH", len(payload), code) + payload


def _collect_dispatches(client):
    """Fängt alle _dispatch-Aufrufe als (code, payload)-Liste ab."""
    seen = []
    client._dispatch = lambda code, payload: seen.append((code, payload))
    return seen


class TestDispatchUdpDatagram:
    def test_single_message_unchanged(self):
        """Ein-Nachricht-Datagramm läuft wie bisher durch."""
        client = _make_client()
        seen = _collect_dispatches(client)
        client._dispatch_udp_datagram(_msg(MsgShotBegin, b"\x01" * 43))
        assert seen == [(MsgShotBegin, b"\x01" * 43)]

    def test_bundled_messages_all_dispatched(self):
        """Drei gebündelte Nachrichten → alle drei Handler laufen, in Reihenfolge."""
        client = _make_client()
        seen = _collect_dispatches(client)
        data = (_msg(MsgPlayerUpdate, b"\xaa" * 12)
                + _msg(MsgShotBegin, b"\xbb" * 43)
                + _msg(MsgShotBegin, b"\xcc" * 43))
        client._dispatch_udp_datagram(data)
        assert seen == [(MsgPlayerUpdate, b"\xaa" * 12),
                        (MsgShotBegin, b"\xbb" * 43),
                        (MsgShotBegin, b"\xcc" * 43)]

    def test_truncated_tail_dropped_front_kept(self):
        """Abgeschnittenes Datagramm-Ende: vordere Nachrichten werden trotzdem
        verarbeitet, der verstümmelte Rest sauber ignoriert."""
        client = _make_client()
        seen = _collect_dispatches(client)
        tail = _msg(MsgShotBegin, b"\xdd" * 43)[:20]  # mitten im Payload gekappt
        client._dispatch_udp_datagram(_msg(MsgPlayerUpdate, b"\xaa" * 12) + tail)
        assert seen == [(MsgPlayerUpdate, b"\xaa" * 12)]

    def test_short_garbage_ignored(self):
        """<4 Bytes: kein Header lesbar → nichts dispatcht, kein Fehler."""
        client = _make_client()
        seen = _collect_dispatches(client)
        client._dispatch_udp_datagram(b"\x00\x01")
        client._dispatch_udp_datagram(b"")
        assert seen == []

    def test_zero_length_payload(self):
        """Nachricht mit leerem Payload (len=0) wird korrekt übersprungen und
        blockiert nachfolgende Nachrichten nicht."""
        client = _make_client()
        seen = _collect_dispatches(client)
        data = _msg(MsgShotBegin, b"") + _msg(MsgPlayerUpdate, b"\xee" * 8)
        client._dispatch_udp_datagram(data)
        assert seen == [(MsgShotBegin, b""), (MsgPlayerUpdate, b"\xee" * 8)]
