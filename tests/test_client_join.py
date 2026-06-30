"""
Tests für BZFlagClient.join_game(): korrekte Reject-Erkennung.

Regression: _h_reject setzt _ev_accepted (um den Wait zu lösen). Früher prüfte
join_game() `if not _ev_accepted.wait()` und meldete dadurch bei einer Ablehnung
fälschlich Erfolg (return True, "Beigetreten…"), der Klarname wurde nie geloggt.
"""
import logging
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_join_game_reject_returns_false_and_logs_clear_name(caplog):
    from bzflag.client import BZFlagClient
    from bzflag.protocol import MsgEnter, MsgReject, PLAYER_TYPE_TANK, TEAM_OBSERVER

    client = BZFlagClient("localhost", 5154)
    client.connected = True
    client.player_id = 7

    # send() ersetzen: beim MsgEnter sofort eine Server-Ablehnung (BadType=0x0002)
    # simulieren, wie sie der echte Server via MsgReject schickt.
    def fake_send(code, payload=b""):
        if code == MsgEnter:
            client._h_reject(MsgReject, struct.pack(">H", 0x0002))
    client.send = fake_send

    with caplog.at_level(logging.INFO, logger="bzflag.client"):
        ok = client.join_game(callsign="Obs", player_type=PLAYER_TYPE_TANK,
                              team=TEAM_OBSERVER, timeout=1.0)

    assert ok is False
    msgs = [r.getMessage() for r in caplog.records]
    # Klarname statt nur Code
    assert any("Server hat abgelehnt: BadType" in m for m in msgs), msgs
    # KEIN fälschlicher Erfolg
    assert not any("Beigetreten" in m for m in msgs), msgs


def test_join_game_timeout_returns_false(caplog):
    """Ohne Accept/Reject läuft der Wait ab → False + Timeout-Log (kein Crash)."""
    from bzflag.client import BZFlagClient
    from bzflag.protocol import PLAYER_TYPE_TANK, TEAM_AUTOMATIC

    client = BZFlagClient("localhost", 5154)
    client.connected = True
    client.player_id = 3
    client.send = lambda code, payload=b"": None  # nichts passiert → Timeout

    with caplog.at_level(logging.ERROR, logger="bzflag.client"):
        ok = client.join_game(callsign="Bot", player_type=PLAYER_TYPE_TANK,
                              team=TEAM_AUTOMATIC, timeout=0.2)

    assert ok is False
    assert any("Timeout" in r.getMessage() for r in caplog.records)


def test_last_reject_reason_set_on_reject():
    """Nach einer Ablehnung liefert last_reject_reason den MsgReject-Code (>= 0)."""
    from bzflag.client import BZFlagClient
    from bzflag.protocol import MsgEnter, MsgReject, PLAYER_TYPE_TANK, TEAM_OBSERVER

    client = BZFlagClient("localhost", 5154)
    client.connected = True
    client.player_id = 7
    client.send = lambda code, payload=b"": (
        client._h_reject(MsgReject, struct.pack(">H", 0x0005))  # ServerFull
        if code == MsgEnter else None)

    client.join_game(callsign="Obs", player_type=PLAYER_TYPE_TANK,
                     team=TEAM_OBSERVER, timeout=1.0)
    assert client.last_reject_reason == 0x0005


def test_last_reject_reason_minus_one_on_timeout():
    """Ohne Ablehnung bleibt last_reject_reason -1 (= kein Reject)."""
    from bzflag.client import BZFlagClient
    from bzflag.protocol import PLAYER_TYPE_TANK, TEAM_AUTOMATIC

    client = BZFlagClient("localhost", 5154)
    client.connected = True
    client.player_id = 3
    client.send = lambda code, payload=b"": None

    client.join_game(callsign="Bot", player_type=PLAYER_TYPE_TANK,
                     team=TEAM_AUTOMATIC, timeout=0.2)
    assert client.last_reject_reason == -1
