"""Diagnose-Logging bei Verbindungsverlust/„Absturz": Der eigentliche Grund darf
nicht an Bot-, Client- oder Manager-Schicht verschluckt werden (siehe 59-s-Disconnects).
"""
import logging

import pytest

from bzflag.protocol import MsgMessage, MSG_INTERNAL_DISCONNECT, BOT_EXIT_CONN_LOST


def _server_msg_payload(text: str) -> bytes:
    """MsgMessage-Payload von ServerPlayer (src=253): [src][dst:2][text]."""
    return bytes([253, 0, 0]) + text.encode("utf-8")


class TestServerMessageLogging:
    def test_kick_reason_is_logged_as_warning(self, bot, caplog):
        """Eine ServerPlayer-Nachricht (z.B. Kick-Grund) wird als WARNING sichtbar gemacht."""
        with caplog.at_level(logging.WARNING, logger="bzbot"):
            bot._on_message(MsgMessage, _server_msg_payload(
                "You were kicked because you were idle too long"))
        assert any("Server-Nachricht" in r.getMessage()
                   and "idle too long" in r.getMessage()
                   and r.levelno == logging.WARNING
                   for r in caplog.records)

    def test_shots_left_stays_info_no_warning(self, bot, caplog):
        """'N shots left' bleibt INFO-Spezialfall und erzeugt KEINE Server-Nachricht-WARNING."""
        bot.own_flag = "L"
        with caplog.at_level(logging.DEBUG, logger="bzbot"):
            bot._on_message(MsgMessage, _server_msg_payload("3 shots left"))
        assert not any("Server-Nachricht" in r.getMessage() for r in caplog.records)


class TestTcpRecvErrorLogging:
    def test_oserror_in_recv_is_logged(self, caplog):
        """Ein Socket-Fehler im TCP-Recv wird geloggt statt stumm verschluckt."""
        from bzflag.client import BZFlagClient

        class _FakeSock:
            def settimeout(self, _t):
                pass

            def recv(self, _n):
                raise OSError("Connection reset by peer")

        client = BZFlagClient(host="localhost")
        client.running = True
        client._sock = _FakeSock()
        with caplog.at_level(logging.WARNING, logger="bzflag.client"):
            client._recv_loop_tcp()
        assert client.connected is False
        assert any("TCP-Verbindung abgebrochen" in r.getMessage()
                   and "Connection reset by peer" in r.getMessage()
                   for r in caplog.records)


class TestConnectionLostFlag:
    def test_on_disconnect_sets_flag_when_running(self, bot):
        """Unerwarteter Disconnect (Loop läuft) markiert _connection_lost."""
        bot._running = True
        bot._connection_lost = False
        bot._on_disconnect(MSG_INTERNAL_DISCONNECT, b"")
        assert bot._connection_lost is True

    def test_on_disconnect_no_flag_on_intentional_stop(self, bot):
        """Bewusster Stop (stop() setzt _running=False vorher) markiert NICHT als Verlust."""
        bot._running = False
        bot._connection_lost = False
        bot._on_disconnect(MSG_INTERNAL_DISCONNECT, b"")
        assert bot._connection_lost is False
