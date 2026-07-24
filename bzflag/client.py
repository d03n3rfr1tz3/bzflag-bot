"""
BZFlag 2.4 TCP+UDP Client

Verifizierte Verbindungssequenz (aus ServerLink.cxx + bzfs.cxx + NetHandler.cxx):

TCP-Handshake:
  Client → "BZFLAG\\r\\n\\r\\n"
  Server → 8 Bytes Versionsstring + 1 Byte player_id (9 Bytes total)

Paket-Phase: [uint16 len][uint16 code][payload]
  Client → MsgEnter + MsgNegotiateFlags
  … Handshake-Automata (WantSettings, WantWHash, GetWorld) …
  Server → MsgAccept

UDP-Handshake (aus NetHandler.cxx + ServerLink.cxx):
  1. Client öffnet UDP-Socket (beliebiger Port, gleiche IP wie TCP)
  2. Client sendet MsgUDPLinkRequest via UDP zum Server
     Payload: 1 Byte = uint8 player_id
     (Server prüft: payload len==1, code==MsgUDPLinkRequest, IP stimmt mit TCP überein)
  3. Server speichert Client-UDP-Adresse (udpin=true)
  4. Server sendet via TCP:
       MsgUDPLinkEstablished (leer)  → Client setzt ulinkup=True
       MsgUDPLinkRequest     (leer)  → Client antwortet MsgUDPLinkEstablished via TCP
  5. Server setzt udpout=True → sendet fortan bulk-Nachrichten via UDP

Nach vollständigem UDP-Handshake:
  Client → MsgPlayerUpdate  via UDP (auch TCP fallback möglich)
  Client → MsgShotBegin     via UDP (PFLICHT: TCP-Shots → Server kickt mit "no UDP")
  Server → MsgPlayerUpdate/Small, MsgShotBegin, MsgGMUpdate  via UDP

Laufend:
  Server → MsgLagPing (0x7069 'pi') → Client echot sofort (TCP oder UDP)
"""

import socket
import struct
import threading
import time
import logging
from typing import Callable, Dict, List, Optional

from .protocol import (
    DEFAULT_PORT, PROTOCOL_VERSION,
    MsgEnter, MsgExit, MsgNegotiateFlags, MsgWantSettings, MsgWantWHash,
    MsgGetWorld, MsgAccept, MsgReject, MsgSuperKill, MsgGameSettings,
    MsgCacheURL, MsgLagPing,
    MsgUDPLinkRequest, MsgUDPLinkEstablished,
    MSG_INTERNAL_DISCONNECT,
    pack_msg, build_enter_payload, build_negotiate_flags_payload,
    STANDARD_FLAGS, PLAYER_TYPE_TANK, TEAM_AUTOMATIC,
    unpack_uint16, unpack_uint32,
)
from .world_parser import parse_world

logger = logging.getLogger(__name__)

# Codes die via UDP gesendet werden wenn UDP-Link up ist
# (aus ServerLink::send() in ServerLink.cxx)
_UDP_CODES = frozenset({
    0x7362,  # MsgShotBegin
    0x7365,  # MsgShotEnd
    0x7075,  # MsgPlayerUpdate
    0x7073,  # MsgPlayerUpdateSmall
    0x676d,  # MsgGMUpdate
    0x6f66,  # MsgUDPLinkRequest  (immer UDP)
    0x6f67,  # MsgUDPLinkEstablished
})


# Reject-Gründe (MsgReject) – aus Protocol.h (RejectBadRequest=0x0000 … RejectIDBanned=0x000B)
REJECT_REASON_NAMES = {
    0x0000: "BadRequest",  0x0001: "BadTeam",
    0x0002: "BadType",     0x0003: "BadMotto",
    0x0004: "TeamFull",    0x0005: "ServerFull",
    0x0006: "BadCallsign", 0x0007: "RepeatCallsign",
    0x0008: "RejoinWait",  0x0009: "IPBanned",
    0x000A: "HostBanned",  0x000B: "IDBanned",
}


def reject_reason_name(code: int) -> str:
    """Liefert den Klarnamen eines MsgReject-Codes (oder 'Code=0xXXXX')."""
    return REJECT_REASON_NAMES.get(code, f"Code=0x{code:04x}")


# Wiederholungsabstand für den UDP-Handshake (MsgUDPLinkRequest), solange udp_active=False.
# Der Stock-Client sendet den Request nur EINMAL (ServerLink::sendUDPlinkRequest); geht das
# eine UDP-Paket verloren, bleibt der Link die ganze Session aus → der Bot schießt nie (Gate
# in _can_shoot). Wir wiederholen ihn deshalb; der Server triggert bei JEDEM empfangenen
# MsgUDPLinkRequest erneut sendUDPupdate() → MsgUDPLinkEstablished (bzfs.cxx).
UDP_LINK_RETRY_INTERVAL = 2.0
# Nach so vielen erfolglosen Versuchen einmalig WARNING loggen (dauerhaft blockierter UDP-Pfad).
_UDP_LINK_WARN_AFTER = 5


def _code_str(code: int) -> str:
    """Gibt den 2-Byte-Nachrichtencode als lesbaren ASCII-String zurück (z.B. 0x7362 → 'sb')."""
    raw = code.to_bytes(2, "big")
    return "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02x}" for b in raw)


class BZFlagClient:
    """
    BZFlag 2.4 Client mit TCP + UDP Unterstützung.

    Nach connect() + join_game() wird der UDP-Handshake automatisch
    durch initiate_udp() gestartet. Sobald udp_active=True, werden
    MsgPlayerUpdate und MsgShotBegin via UDP gesendet.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port

        # TCP
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._recv_thread: Optional[threading.Thread] = None

        # UDP
        self._udp_sock:   Optional[socket.socket] = None
        self._udp_thread: Optional[threading.Thread] = None
        # Aufgelöste Server-Adresse (IP, Port) für UDP-Sends. WICHTIG: numerische IP,
        # nicht Hostname — sendto() mit Hostname-Tupel macht in CPython pro Paket ein
        # getaddrinfo (im Container via Docker-DNS ~1ms/Paket, war 46% der aktiven CPU;
        # FABLE-PLAN.md Teil 1b, N1b). Gesetzt in connect() aus getpeername() der
        # TCP-Verbindung → garantiert dieselbe Server-IP wie TCP.
        self._server_addr: Optional[tuple] = None
        self.udp_active = False   # True sobald UDP-Handshake abgeschlossen
        self._udp_link_sent_at = 0.0   # monotonic des letzten MsgUDPLinkRequest (Retry-Throttle)
        self._udp_link_attempts = 0    # Anzahl gesendeter MsgUDPLinkRequest (für WARNING-Schwelle)

        self.connected  = False
        self.running    = False
        self.player_id: Optional[int] = None

        # Loggt unbehandelte Nachrichten-Codes (DEBUG). Ein voller Spiel-Client
        # (Bot) handhabt fast alles und sieht selten Unbehandeltes; ein schlanker
        # Beobachter (nur Add/Remove) würde dagegen den 60-Hz-Verkehr fluten –
        # daher abschaltbar (ServerObserver setzt False).
        self.log_unhandled = True

        self._handlers: Dict[int, List[Callable]] = {}
        self._on_game_settings: Callable[..., None] | None = None

        # Join-Synchronisation
        self._ev_accepted = threading.Event()
        self._ev_rejected = threading.Event()
        self._reject_reason = -1
        self._world_bytes  = 0
        self._world_buf:  bytearray = bytearray()
        self._world_hash: str = ""
        self._world_half_cache: float = 0.0

        # Callback: on_world_ready(WorldMap) — gesetzt von bzbot.py
        self.on_world_ready: Callable[..., None] | None = None

        # Eingebaute Handler
        self.add_handler(MsgAccept,           self._h_accept)
        self.add_handler(MsgReject,           self._h_reject)
        self.add_handler(MsgSuperKill,        self._h_super_kill)
        self.add_handler(MsgNegotiateFlags,   self._h_negotiate_flags)
        self.add_handler(MsgGameSettings,     self._h_game_settings)
        self.add_handler(MsgWantWHash,        self._h_want_whash)
        self.add_handler(MsgGetWorld,         self._h_get_world)
        self.add_handler(MsgCacheURL,         self._h_cache_url)
        self.add_handler(MsgLagPing,          self._h_lag_ping)
        self.add_handler(MsgUDPLinkRequest,   self._h_udp_link_request)
        self.add_handler(MsgUDPLinkEstablished, self._h_udp_link_established)

    # ── Öffentliche API ───────────────────────────────────────────────────

    def add_handler(self, code: int, handler: Callable) -> None:
        """Registriert eine Handler-Funktion für einen Nachrichtencode (mehrere möglich)."""
        self._handlers.setdefault(code, []).append(handler)

    def connect(self) -> bool:
        """TCP-Verbindung + BZFlag-Handshake. Player-ID aus dem 9. Hello-Byte."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15.0)
            sock.connect((self.host, self.port))
            sock.sendall(b"BZFLAG\r\n\r\n")

            hello = _recv_exactly(sock, 9)
            if not hello or not hello.startswith(b"BZFS"):
                logger.error("Ungültiges Server-Hello: %r", hello)
                sock.close()
                return False

            srv_ver = hello[4:8]
            pid     = hello[8]

            logger.info("Server-Version: %s  |  zugewiesene Player-ID: %d",
                        srv_ver.decode("ascii", errors="?"), pid)

            if srv_ver != PROTOCOL_VERSION:
                logger.warning("Protokollversion-Mismatch: Server=%r erwartet=%r",
                               srv_ver, PROTOCOL_VERSION)
            if pid == 0xFF:
                logger.error("Server ist voll (player_id=0xFF)")
                sock.close()
                return False

            self.player_id    = pid
            # N1b: tatsächliche Peer-IP der TCP-Verbindung cachen (inet_pton-Fastpath
            # in sendto, kein getaddrinfo pro UDP-Paket mehr)
            self._server_addr = (sock.getpeername()[0], self.port)
            self._sock        = sock
            self._sock.settimeout(None)
            self.connected    = True

            self.running = True
            self._recv_thread = threading.Thread(
                target=self._recv_loop_tcp, daemon=True, name="bzflag-tcp-recv")
            self._recv_thread.start()
            return True

        except OSError as exc:
            logger.error("Verbindung fehlgeschlagen: %s", exc)
            return False

    @property
    def last_reject_reason(self) -> int:
        """MsgReject-Code des letzten Join-Versuchs (>= 0 = Server hat abgelehnt,
        -1 = keine Ablehnung). Erlaubt dem Aufrufer, eine Kapazitäts-Ablehnung von
        einem Verbindungsfehler/Timeout zu unterscheiden."""
        return self._reject_reason

    def join_game(self, callsign: str, player_type: int = PLAYER_TYPE_TANK,
                  team: int = TEAM_AUTOMATIC, motto: str = "", token: str = "",
                  timeout: float = 60.0) -> bool:
        """
        Tritt dem Server bei (TCP-Handshake).
        UDP-Handshake wird danach mit initiate_udp() gestartet.
        """
        if not self.connected:
            return False

        self._ev_accepted.clear()
        self._ev_rejected.clear()
        self._reject_reason = -1
        self._world_bytes   = 0

        logger.debug("Sende MsgEnter als '%s' (id=%d)", callsign, self.player_id)
        self.send(MsgEnter, build_enter_payload(
            callsign=callsign, player_type=player_type,
            team=team, motto=motto, token=token))

        logger.debug("Sende MsgNegotiateFlags (%d Flags)", len(STANDARD_FLAGS))
        self.send(MsgNegotiateFlags, build_negotiate_flags_payload())

        # _ev_accepted wird auch bei Reject/Superkill gesetzt (damit der Wait sofort
        # zurückkehrt), darf also NICHT als alleiniges Erfolgssignal dienen. Nach dem
        # Wait der Reihe nach prüfen: zuerst Ablehnung, dann Verbindungsverlust, dann
        # echter Timeout, sonst Erfolg.
        self._ev_accepted.wait(timeout=timeout)
        if self._ev_rejected.is_set():
            logger.error("Server hat abgelehnt: %s",
                         reject_reason_name(self._reject_reason))
            return False
        if not self.connected:
            logger.error("Verbindung während Join verloren")
            return False
        if not self._ev_accepted.is_set():
            logger.error("Timeout (%ds) – kein MsgAccept", int(timeout))
            return False

        logger.info("Beigetreten als '%s' (player_id=%d)", callsign, self.player_id)
        return True

    def initiate_udp(self) -> bool:
        """
        Startet den UDP-Handshake (aus ServerLink::sendUDPupdate):

        1. UDP-Socket öffnen (beliebiger lokaler Port)
        2. MsgUDPLinkRequest via UDP senden: [uint16 1][uint16 0x6f66][uint8 player_id]
           Server matcht via IP-Adresse + player_id
        3. UDP-Empfangs-Thread starten
        4. MsgUDPLinkEstablished und MsgUDPLinkRequest kommen via TCP zurück
           (werden durch Handler _h_udp_link_established / _h_udp_link_request verarbeitet)
        5. Nach Handshake: udp_active=True → MsgPlayerUpdate + MsgShotBegin via UDP

        Gibt True zurück wenn Socket geöffnet, False bei Fehler.
        """
        if self._udp_sock is not None:
            return True  # already open

        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.settimeout(0.5)
            # Bind auf 0 (OS wählt Port) – selbe lokale IP wie TCP
            udp.bind(("", 0))
            self._udp_sock = udp

            # UDP-Empfangs-Thread
            self._udp_thread = threading.Thread(
                target=self._recv_loop_udp, daemon=True, name="bzflag-udp-recv")
            self._udp_thread.start()

            self._send_udp_link_request()
            return True

        except OSError as exc:
            logger.error("UDP-Socket-Fehler: %s", exc)
            return False

    def _send_udp_link_request(self) -> None:
        """Sendet ein MsgUDPLinkRequest via UDP-Socket und merkt sich Zeitpunkt/Zähler.
        Payload: 1 Byte player_id (NetHandler.cxx: "if (id==-1 && len==1 && code==MsgUDPLinkRequest)").
        Der Server matcht via IP + player_id und antwortet mit MsgUDPLinkEstablished via TCP."""
        if self._udp_sock is None or self.player_id is None:
            return
        pkt = pack_msg(MsgUDPLinkRequest, struct.pack(">B", self.player_id))
        self._udp_sock.sendto(pkt, self._server_addr or (self.host, self.port))
        self._udp_link_sent_at = time.monotonic()
        self._udp_link_attempts += 1
        logger.debug("MsgUDPLinkRequest via UDP gesendet (player_id=%d, Versuch %d)",
                     self.player_id, self._udp_link_attempts)

    def retry_udp_link(self) -> None:
        """Wiederholt den UDP-Handshake, solange udp_active=False. Selbst-gedrosselt
        (UDP_LINK_RETRY_INTERVAL) → kann gefahrlos jeden Frame aufgerufen werden.

        Der Stock-Client sendet MsgUDPLinkRequest nur einmal; geht es verloren, schießt der
        Bot die ganze Session nicht (Gate in _can_shoot, weil TCP-Shots gekickt werden). Jeder
        erneut empfangene Request lässt den Server sendUDPupdate() wiederholen (bzfs.cxx)."""
        if self.udp_active or self._udp_sock is None or self.player_id is None:
            return
        if time.monotonic() - self._udp_link_sent_at < UDP_LINK_RETRY_INTERVAL:
            return
        try:
            self._send_udp_link_request()
        except OSError as exc:
            logger.error("UDP-Link-Retry Sendefehler: %s", exc)
            return
        if self._udp_link_attempts == _UDP_LINK_WARN_AFTER:
            logger.warning("UDP-Handshake nach %d Versuchen noch offen – Bot kann nicht "
                           "schießen, bis udp_active. UDP-Pfad (NAT/Firewall?) prüfen.",
                           self._udp_link_attempts)

    def send(self, code: int, payload: bytes) -> None:
        """
        Sendet ein BZFlag-Paket.
        Routing (aus ServerLink::send()):
          - MsgUDPLinkRequest: immer via UDP
          - MsgPlayerUpdate, MsgShotBegin, MsgShotEnd, MsgGMUpdate: via UDP wenn udp_active
          - Alles andere: via TCP
        """
        if not self.connected:
            return
        use_udp = (
            self.udp_active and self._udp_sock is not None and code in _UDP_CODES
        ) or code == MsgUDPLinkRequest

        data = pack_msg(code, payload)

        if use_udp:
            try:
                assert self._udp_sock is not None  # use_udp impliziert _udp_sock gesetzt
                self._udp_sock.sendto(data, self._server_addr or (self.host, self.port))
            except OSError as exc:
                logger.error("UDP-Sendefehler 0x%04x: %s", code, exc)
        else:
            with self._send_lock:
                try:
                    assert self._sock is not None  # TCP-Pfad läuft nur nach connect()
                    # B8: kein setblocking(True) mehr pro Send — Send und Recv teilen sich
                    # dasselbe Socket-Objekt, das frühere setblocking(True) hat das Recv-
                    # Timeout ohnehin nach dem ersten Send deaktiviert (deshalb jetzt auch
                    # in _recv_loop_tcp entfernt, siehe dort). Socket bleibt blocking wie
                    # nach connect().
                    self._sock.sendall(data)
                except OSError as exc:
                    logger.error("TCP-Sendefehler 0x%04x: %s", code, exc)
                    self.connected = False

    def disconnect(self) -> None:
        """Sendet MsgExit und schließt TCP- und UDP-Sockets."""
        if not self.connected:
            return
        self.running = False
        try:
            self.send(MsgExit, b"")
        except Exception:
            pass
        self.connected  = False
        self.udp_active = False
        if self._udp_sock:
            try: self._udp_sock.close()
            except Exception: pass
            self._udp_sock = None
        if self._sock:
            try: self._sock.close()
            except Exception: pass
            self._sock = None

    # ── TCP-Empfangs-Thread ───────────────────────────────────────────────

    def _recv_loop_tcp(self) -> None:
        """TCP-Empfangs-Thread: liest Pakete aus dem Stream und dispatcht sie."""
        # bytearray + Lese-Cursor statt buf += data / buf = buf[total:]: Letzteres kopiert
        # bei Broadcast-lastigem Verkehr (viele PlayerUpdates) den kompletten Restpuffer bei
        # JEDEM verarbeiteten Paket um → O(n²) über die Lebensdauer der Verbindung. Der
        # Cursor verbraucht nur einen Index; das bereits gelesene Präfix wird einmal pro
        # recv()-Runde (nicht pro Paket) via del buf[:pos] abgeschnitten.
        buf = bytearray()
        pos = 0
        assert self._sock is not None  # Thread startet erst nach erfolgreichem connect()
        # B8: kein settimeout(1.0)/socket.timeout-Polling mehr — Socket bleibt blocking wie
        # nach connect() (das war es faktisch ohnehin, da send() bislang setblocking(True) pro
        # TCP-Send setzte und damit dieses Timeout nach dem ersten Send deaktivierte). Der
        # Loop-Exit läuft wie bisher über den Socket-Close in disconnect() → recv() liefert
        # dann OSError, der break unten greift. Der UDP-Socket behält sein eigenes Timeout.
        while self.running:
            try:
                data = self._sock.recv(8192)
                if not data:
                    logger.warning("Server hat TCP-Verbindung geschlossen")
                    break
                buf += data
            except OSError as exc:
                # Socket-Fehler (z.B. ECONNRESET) NICHT verschlucken – das ist oft der
                # einzige konkrete Grund eines Verbindungsverlusts.
                logger.warning("[recv] TCP-Verbindung abgebrochen: %s", exc)
                break

            # TCP ist ein Byte-Strom: ein recv() kann halbe Pakete liefern → Puffer zusammensetzen
            while len(buf) - pos >= 4:
                length, code = struct.unpack_from(">HH", buf, pos)
                total = 4 + length  # 4 Byte Header + Nutzlast
                if len(buf) - pos < total:
                    break  # noch nicht genug Bytes für dieses Paket → nächsten recv() abwarten
                payload = bytes(buf[pos + 4: pos + total])  # Handler slicen/unpacken auf bytes
                pos += total
                self._dispatch(code, payload)

            if pos:
                del buf[:pos]
                pos = 0

        self.connected = False
        self._dispatch(MSG_INTERNAL_DISCONNECT, b"")

    # ── UDP-Empfangs-Thread ───────────────────────────────────────────────

    def _recv_loop_udp(self) -> None:
        """
        Empfängt UDP-Pakete vom Server.
        Format: identisch TCP – [uint16 len][uint16 code][payload]
        Erlaubte Codes via UDP (aus handleCommand udp=true in bzfs.cxx):
          MsgShotBegin, MsgShotEnd, MsgPlayerUpdate, MsgPlayerUpdateSmall,
          MsgGMUpdate, MsgUDPLinkRequest, MsgUDPLinkEstablished
        """
        while self.running and self._udp_sock is not None:
            try:
                data, addr = self._udp_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            self._dispatch_udp_datagram(data)

    def _dispatch_udp_datagram(self, data: bytes) -> None:
        """Dispatcht ALLE Nachrichten eines UDP-Datagramms der Reihe nach.

        bzfs bündelt mehrere Nachrichten pro Datagramm (NetHandler::pwrite
        sammelt bis MaxPacketLen=1024 in udpOutputBuffer); der echte Client
        (ServerLink::read) leert das ganze Datagramm, bevor er das nächste
        empfängt. Nur die erste Nachricht zu parsen hieße, alle dahinter
        gebündelten zu verlieren (im Livetest: ~25 von 29 MsgShotBegin).
        Keine Nachricht überspannt zwei Datagramme (überlange Nachrichten
        sendet bzfs einzeln) → keine datagrammübergreifende Pufferung nötig.
        """
        off, n = 0, len(data)
        while n - off >= 4:
            length, code = struct.unpack_from(">HH", data, off)
            total = 4 + length
            if n - off < total:
                break  # abgeschnittenes/verstümmeltes Datagramm-Ende
            payload = data[off + 4: off + total]
            off += total
            self._dispatch(code, payload)

    def _dispatch(self, code: int, payload: bytes) -> None:
        """Leitet ein empfangenes Paket an alle registrierten Handler weiter."""
        handlers = self._handlers.get(code, [])
        if not handlers and code != MSG_INTERNAL_DISCONNECT and self.log_unhandled:
            logger.debug("Unbehandelt 0x%04x '%s' (%d B)",
                         code, _code_str(code), len(payload))
        for h in list(handlers):
            try:
                h(code, payload)
            except Exception as exc:
                logger.exception("Handler-Fehler 0x%04x: %s", code, exc)

    # ── Eingebaute Handler ────────────────────────────────────────────────

    def _h_accept(self, code: int, payload: bytes) -> None:
        """MsgAccept: bestätigt Player-ID und signalisiert erfolgreichen Join."""
        # MsgAccept payload: uint8 player_id (aus bzfs.cxx AddPlayer)
        if len(payload) >= 1:
            confirmed_id = payload[0]
            if self.player_id != confirmed_id:
                logger.warning("Player-ID Korrektur: Hello=%d Accept=%d → nehme %d",
                               self.player_id, confirmed_id, confirmed_id)
                self.player_id = confirmed_id
        logger.debug("MsgAccept empfangen (player_id=%s)", self.player_id)
        self._ev_accepted.set()

    def _h_reject(self, code: int, payload: bytes) -> None:
        """MsgReject: loggt Ablehnungsgrund und signalisiert Join-Fehler."""
        if len(payload) >= 2:
            self._reject_reason = unpack_uint16(payload, 0)
        logger.warning("MsgReject: %s (0x%04x)",
                       reject_reason_name(self._reject_reason), self._reject_reason)
        self._ev_rejected.set()
        # _ev_accepted ebenfalls setzen, damit join_game() sofort aus dem wait()
        # zurückkehrt; die Erfolgs-/Fehlerunterscheidung trifft join_game() selbst
        # anhand von _ev_rejected (NICHT anhand von _ev_accepted).
        self._ev_accepted.set()

    def _h_super_kill(self, code: int, payload: bytes) -> None:
        """MsgSuperKill: Server hat die Verbindung zwangsweise beendet."""
        logger.error("MsgSuperKill – Server hat Verbindung beendet")
        self.connected  = False
        self.udp_active = False
        self.running    = False
        self._ev_accepted.set()

    def _h_negotiate_flags(self, code: int, payload: bytes) -> None:
        """MsgNegotiateFlags: antwortet mit MsgWantSettings um den Handshake fortzuführen."""
        count = len(payload) // 2
        logger.debug("MsgNegotiateFlags vom Server: %d unbekannte Flags – sende MsgWantSettings", count)
        self.send(MsgWantSettings, b"")

    def _h_game_settings(self, code: int, payload: bytes) -> None:
        """MsgGameSettings: leitet Payload an optionalen Callback weiter; antwortet mit MsgWantWHash."""
        logger.debug("MsgGameSettings (%d B) – sende MsgWantWHash", len(payload))
        if self._on_game_settings is not None:
            self._on_game_settings(payload)
        self.send(MsgWantWHash, b"")

    def _h_want_whash(self, code: int, payload: bytes) -> None:
        """MsgWantWHash: empfängt Welt-Hash und startet den Welt-Download mit MsgGetWorld(0)."""
        if len(payload) >= 1:
            md5 = payload.rstrip(b"\x00").decode("ascii", errors="?")
            self._world_hash = md5
            logger.debug("Welt-Hash: %s", md5)
        self._world_bytes = 0
        self._world_buf   = bytearray()
        logger.debug("Sende MsgGetWorld(0) – starte Welt-Download")
        self.send(MsgGetWorld, struct.pack(">I", 0))

    def _h_cache_url(self, code: int, payload: bytes) -> None:
        """MsgCacheURL: Karten-Cache-URL vom Server (wird nicht verwendet)."""
        url = payload.rstrip(b"\x00").decode("utf-8", errors="?")
        logger.debug("MsgCacheURL: %s (ignoriert)", url)

    def _h_get_world(self, code: int, payload: bytes) -> None:
        """MsgGetWorld: empfängt Welt-Chunk; fordert weitere an bis bytes_remaining == 0."""
        if len(payload) < 4:
            return
        remaining = unpack_uint32(payload, 0)
        chunk     = payload[4:]
        self._world_buf  += chunk
        self._world_bytes = len(self._world_buf)
        if remaining == 0:
            logger.debug("Welt-Download abgeschlossen (%d Bytes)", self._world_bytes)
            self._deliver_world()
        else:
            self.send(MsgGetWorld, struct.pack(">I", self._world_bytes))

    def _deliver_world(self) -> None:
        """Parst _world_buf → WorldMap und ruft on_world_ready auf."""
        if self.on_world_ready is None:
            return
        world_half = self._world_half_cache
        wm = parse_world(
            bytes(self._world_buf),
            world_half=world_half,
            world_hash=self._world_hash,
        )
        try:
            self.on_world_ready(wm)
        except Exception as exc:
            logger.warning("[PTH] on_world_ready-Callback Fehler: %s", exc)

    def _h_lag_ping(self, code: int, payload: bytes) -> None:
        """MsgLagPing echo – aus playing.cxx: serverLink->send(MsgLagPing,2,msg)."""
        self.send(MsgLagPing, payload)

    def _h_udp_link_request(self, code: int, payload: bytes) -> None:
        """
        MsgUDPLinkRequest (Server→Client via TCP) – aus sendUDPupdate() in bzfs.cxx.

        Der Server erwartet, dass wir mit MsgUDPLinkEstablished via TCP antworten.
        Tun wir das, setzt der Server udpout=True und sendet ALLE Broadcasts
        (MsgShotBegin, MsgPlayerUpdate usw.) via UDP an uns – unzuverlässig!

        Wir antworten BEWUSST NICHT, damit server.udpout=False bleibt.
        Ergebnis:
          - Server→Bot: alle Broadcasts via TCP (zuverlässig, kein Paketverlust)
          - Bot→Server: MsgPlayerUpdate + MsgShotBegin via UDP (udp_active=True)
        Damit funktioniert Hit-Detection und Dodge-Detection zuverlässig.
        """
        logger.debug("MsgUDPLinkRequest vom Server empfangen – kein Acknowledge "
                     "(server.udpout bleibt False → Broadcasts kommen via TCP)")
        # Absichtlich keine Antwort: server.udpout bleibt False

    def _h_udp_link_established(self, code: int, payload: bytes) -> None:
        """
        MsgUDPLinkEstablished (Server→Client via TCP).
        Server bestätigt dass er unseren UDP-Pfad kennt.
        Aus ServerLink::enableOutboundUDP(): ulinkup=True.
        """
        self.udp_active = True
        logger.info("UDP-Link aktiv – MsgPlayerUpdate und MsgShotBegin gehen via UDP")


# ── Hilfsfunktion ─────────────────────────────────────────────────────────

def _recv_exactly(sock: socket.socket, n: int) -> Optional[bytes]:
    """Liest genau n Bytes aus sock; gibt None zurück bei Verbindungsabbruch."""
    data = b""
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        except OSError:
            return None
    return data
