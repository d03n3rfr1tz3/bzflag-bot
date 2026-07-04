"""Tests für den UDP-Adress-Cache (N1b, FABLE-PLAN.md Teil 1b).

Der historische Kostenpunkt: sendto() mit Hostname-Tupel macht in CPython pro
Paket ein getaddrinfo (im Container via Docker-DNS ~1ms/Paket). connect()
cached deshalb die tatsächliche Peer-IP der TCP-Verbindung in _server_addr;
alle UDP-Sends nutzen das numerische Tupel (inet_pton-Fastpath).
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_client():
    from bzflag.client import BZFlagClient
    return BZFlagClient("some-docker-service", 5154)


def test_connect_caches_resolved_peer_ip():
    """Nach connect() ist _server_addr die getpeername()-IP der TCP-Verbindung."""
    client = _make_client()

    fake_sock = MagicMock()
    fake_sock.getpeername.return_value = ("172.18.0.2", 5154)
    fake_sock.recv.side_effect = OSError("closed")  # Recv-Thread endet sofort sauber

    with patch("bzflag.client.socket.socket", return_value=fake_sock), \
         patch("bzflag.client._recv_exactly",
               return_value=b"BZFS" + b"0221" + bytes([7])):
        assert client.connect() is True

    assert client._server_addr == ("172.18.0.2", 5154)
    assert client.player_id == 7


def test_udp_send_uses_cached_addr():
    """send() mit UDP-Code schickt an das gecachte IP-Tupel, nicht an den Hostnamen."""
    from bzflag.protocol import MsgPlayerUpdate
    client = _make_client()
    client.connected = True
    client.udp_active = True
    client.player_id = 7
    client._server_addr = ("172.18.0.2", 5154)
    client._udp_sock = MagicMock()

    client.send(MsgPlayerUpdate, b"\x00" * 8)

    (data, addr), _ = client._udp_sock.sendto.call_args
    assert addr == ("172.18.0.2", 5154)


def test_udp_link_request_uses_cached_addr():
    """Auch der UDP-Handshake nutzt das gecachte Tupel."""
    client = _make_client()
    client.player_id = 7
    client._server_addr = ("172.18.0.2", 5154)
    client._udp_sock = MagicMock()

    client._send_udp_link_request()

    (data, addr), _ = client._udp_sock.sendto.call_args
    assert addr == ("172.18.0.2", 5154)


def test_udp_send_falls_back_to_host_tuple():
    """Ohne gecachte Adresse (defensiv, sollte nach connect() nie eintreten):
    Fallback auf das (host, port)-Tupel wie vor N1b."""
    from bzflag.protocol import MsgPlayerUpdate
    client = _make_client()
    client.connected = True
    client.udp_active = True
    client.player_id = 7
    client._server_addr = None
    client._udp_sock = MagicMock()

    client.send(MsgPlayerUpdate, b"\x00" * 8)

    (data, addr), _ = client._udp_sock.sendto.call_args
    assert addr == ("some-docker-service", 5154)
