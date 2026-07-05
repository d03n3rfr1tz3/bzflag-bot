"""
BZFlag 2.4 Protokoll-Konstanten und Pack/Unpack-Hilfsfunktionen.

Alle Codes und Paket-Layouts wurden gegen die Originalquellen verifiziert:
  include/Protocol.h  – alle Msg*-Konstanten
  include/global.h    – Längen-Konstanten
  src/common/PlayerState.cxx – MsgPlayerUpdate / MsgPlayerUpdateSmall
  src/bzfs/bzfs.cxx   – Serverseite aller Nachrichten
  src/bzflag/ServerLink.cxx  – Clientseite (sendEnter, sendPlayerUpdate, …)
  src/bzfs/GameKeeper.cxx    – packPlayerUpdate (MsgAddPlayer)
  src/game/PlayerInfo.cxx    – unpackEnter, packUpdate, packId
  src/bzfs/Score.cxx         – Score::pack
"""

import struct

# ---------------------------------------------------------------------------
# Verbindungsparameter
# ---------------------------------------------------------------------------
DEFAULT_PORT     = 5154
PROTOCOL_VERSION = b"0221"   # BZFlag 2.4

# Client-Versionsstring – muss als "major.minor.rev" parsebar sein
CLIENT_VERSION_STRING = "2.4.24"

# ---------------------------------------------------------------------------
# Nachrichtencodes  –  aus include/Protocol.h (alle verifiziert)
# ---------------------------------------------------------------------------

# Client → Server
MsgEnter              = 0x656e  # 'en'
MsgExit               = 0x6578  # 'ex'
MsgAlive              = 0x616c  # 'al'  (Client→Server: leer; Server→Client: pos)
MsgKilled             = 0x6b6c  # 'kl'
MsgNegotiateFlags     = 0x6e66  # 'nf'
MsgWantSettings       = 0x7773  # 'ws'
MsgWantWHash          = 0x7768  # 'wh'
MsgGetWorld           = 0x6777  # 'gw'
MsgQueryGame          = 0x7167  # 'qg'
MsgQueryPlayers       = 0x7170  # 'qp'
MsgShotBegin          = 0x7362  # 'sb'
MsgShotEnd            = 0x7365  # 'se'
MsgGrabFlag           = 0x6766  # 'gf'
MsgDropFlag           = 0x6466  # 'df'
MsgCaptureFlag        = 0x6366  # 'cf'
MsgTeleport           = 0x7470  # 'tp'
MsgTransferFlag       = 0x7466  # 'tf'
MsgMessage            = 0x6d67  # 'mg'
MsgPause              = 0x7061  # 'pa'
MsgAutoPilot          = 0x6175  # 'au'
MsgNewRabbit          = 0x6e52  # 'nR'
MsgUDPLinkRequest     = 0x6f66  # 'of'
MsgUDPLinkEstablished = 0x6f67  # 'og'

# Positions-Update – Client sendet 'pu', empfängt 'ps' von anderen
MsgPlayerUpdate       = 0x7075  # 'pu'  ← Client → Server
MsgPlayerUpdateSmall  = 0x7073  # 'ps'  ← Server → Client (andere Spieler)

# Server → Client
MsgAccept             = 0x6163  # 'ac'
MsgReject             = 0x726a  # 'rj'
MsgSuperKill          = 0x736b  # 'sk'
MsgAddPlayer          = 0x6170  # 'ap'
MsgRemovePlayer       = 0x7270  # 'rp'
MsgGameSettings       = 0x6773  # 'gs'
MsgCacheURL           = 0x6375  # 'cu'
MsgTeamUpdate         = 0x7475  # 'tu'
MsgFlagUpdate         = 0x6675  # 'fu'
MsgFlagType           = 0x6674  # 'ft'
MsgScore              = 0x7363  # 'sc'
MsgScoreOver          = 0x736f  # 'so'
MsgTimeUpdate         = 0x746f  # 'to'
MsgGameTime           = 0x6774  # 'gt'  (Server-Keepalive)
MsgPlayerInfo         = 0x7062  # 'pb'  (Spieler-Status: registered/admin)
MsgLagPing            = 0x7069  # 'pi'  (Lag-Ping, muss geechot werden!)
MsgAdminInfo          = 0x6169  # 'ai'
MsgSetVar             = 0x7376  # 'sv'
MsgHandicap           = 0x6863  # 'hc'
MsgLagState           = 0x6c73  # 'ls'
MsgGMUpdate           = 0x676d  # 'gm'
MsgFetchResources     = 0x6672  # 'fr'
MsgNearFlag           = 0x4e66  # 'Nf'
MsgCustomSound        = 0x6373  # 'cs'
MsgReplayReset        = 0x7272  # 'rr'

# Intern (kein echtes Netzwerkpaket)
MSG_INTERNAL_DISCONNECT = -1

# Bot ↔ Bot-Manager IPC (KEIN Netzwerkpaket): der Bot schreibt im --managed-Modus
# eine getaggte Statuszeile auf stdout, der Manager erkennt sie an diesem Präfix.
# Manager→Bot läuft über stdin als reine JSON-Zeilen (kein Präfix nötig).
MGR_STATUS_PREFIX = "@@BZMGR@@"

# Prozess-Exit-Code, mit dem sich ein --managed-Bot beendet, wenn der Server den Beitritt
# ablehnt (z.B. Server/Team voll, Callsign belegt). So kann der Manager eine erwartete
# Kapazitäts-Ablehnung von einem echten Absturz (Exit-Code 1) unterscheiden.
BOT_EXIT_REJECTED = 2

# Prozess-Exit-Code, mit dem sich ein --managed-Bot beendet, wenn die Runde regulär endet
# ("Game Over"). Statt sich in-process unsynchronisiert neu zu verbinden, beendet sich der Bot
# und überlässt dem Manager das koordinierte Leave-and-Rejoin (kurzer count()==0-Moment →
# neuer Zeit-Countdown). Eindeutig von Reject (2) und echtem Crash (1) unterscheidbar.
BOT_EXIT_ROUND_OVER = 3

# Prozess-Exit-Code für einen unerwarteten Verbindungsverlust NACH erfolgreichem Beitritt
# (Server schließt/resettet die TCP-Verbindung, MsgSuperKill o.ä.) — kein Absturz, sondern
# ein Netz-/Server-Ereignis. So kann der Manager es sauber beschriften (statt "abgestürzt")
# und trotzdem den Ausgabe-Tail mit dem konkreten Grund zeigen. Ein echter Fehlerpfad
# (Exception) bleibt Exit-Code 1 mit Traceback → weiterhin als Absturz erkennbar.
BOT_EXIT_CONN_LOST = 4

# Pause (Sekunden) zwischen "alle Bots getrennt" und "Bots verbinden wieder" beim Rundenende.
# Gibt dem Server Zeit, die Trennungen zu registrieren und count()==0 zu erreichen, sodass
# checkGameOn() beim ersten Rejoin einen neuen Zeit-Countdown startet. Genutzt vom Manager
# (koordiniertes Rejoin) UND vom Standalone-Bot (Gap vor In-Prozess-Reconnect).
ROUND_RESTART_GAP_S = 5.0

# ---------------------------------------------------------------------------
# Player-Typen  –  aus enum PlayerType (global.h): NUR TankPlayer=0, ComputerPlayer=1.
# Ein Observer ist KEIN eigener Typ, sondern ein Team (ObserverTeam) – Mensch UND
# Observer treten als TankPlayer bei (vgl. ServerLink::sendEnter im echten Client).
# WICHTIG: ComputerPlayer (1) wird bei -disableBots mit RejectBadType abgelehnt!
# ---------------------------------------------------------------------------
PLAYER_TYPE_TANK     = 0   # TankPlayer (Mensch und Observer)
PLAYER_TYPE_COMPUTER = 1   # ComputerPlayer – nur Client-Robots; bei -disableBots abgelehnt

# ---------------------------------------------------------------------------
# Teams  –  aus TeamColor enum
# ---------------------------------------------------------------------------
TEAM_ROGUE     = 0
TEAM_RED       = 1
TEAM_GREEN     = 2
TEAM_BLUE      = 3
TEAM_PURPLE    = 4
TEAM_OBSERVER  = 5        # ObserverTeam (Server liest Team als int16; 0xFFFF wäre NoTeam!)
TEAM_RABBIT    = 6
TEAM_HUNTER    = 7
TEAM_AUTOMATIC = 0xFFFE   # = int16(-2) = AutomaticTeam: Server wählt Team

# ---------------------------------------------------------------------------
# PlayerState Status-Flags  –  aus PlayerState.h
# ---------------------------------------------------------------------------
PS_ALIVE        = (1 << 0)
PS_PAUSED       = (1 << 1)
PS_EXPLODING    = (1 << 2)
PS_TELEPORTING  = (1 << 3)
PS_FLAG_ACTIVE  = (1 << 4)
PS_CROSSING     = (1 << 5)
PS_FALLING      = (1 << 6)
PS_ON_DRIVER    = (1 << 7)
PS_USER_INPUTS  = (1 << 8)
PS_JUMP_JETS    = (1 << 9)
PS_PLAY_SOUND   = (1 << 10)

# ---------------------------------------------------------------------------
# String-Längen  –  aus include/global.h
# ---------------------------------------------------------------------------
CallSignLen = 32
MottoLen    = 128
TokenLen    = 22
VersionLen  = 60
MessageLen  = 128

# ---------------------------------------------------------------------------
# Flag-Kürzel für MsgNegotiateFlags (Client → Server)
# Format: uint16 count + count×2-Byte-Abkürzungen
# ---------------------------------------------------------------------------
STANDARD_FLAGS = [
    b"A\x00", b"BU", b"CL", b"F\x00", b"G\x00", b"GM",
    b"ID",    b"IB", b"JP", b"L\x00", b"LT",    b"MG",
    b"MQ",    b"N\x00", b"O\x00", b"PZ", b"QT",  b"RC",
    b"RF",    b"RO", b"SE", b"SH",   b"SB",    b"ST",
    b"SW",    b"TH", b"TR", b"US",   b"V\x00", b"WG",
    b"B\x00", b"BY", b"CB", b"FO",   b"LG",    b"M\x00",
    b"NJ",    b"OO", b"PW", b"R\x00", b"RG",   b"WA",
    b"SR",    b"T\x00", b"JM",
]

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def pack_string(s: str, length: int) -> bytes:
    """Null-terminierter, auf length Bytes aufgefüllter Strings für Protokoll-Felder."""
    enc = s.encode("utf-8")[: length - 1]
    return enc + b"\x00" * (length - len(enc))


def pack_msg(code: int, payload: bytes) -> bytes:
    """BZFlag-Paket: uint16 len + uint16 code + payload."""
    return struct.pack(">HH", len(payload), code) + payload


def unpack_uint8(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">B", data, offset)[0]


def unpack_int16(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">h", data, offset)[0]


def unpack_uint16(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def unpack_uint32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def unpack_float(data: bytes, offset: int = 0) -> float:
    return struct.unpack_from(">f", data, offset)[0]


def unpack_vec3(data: bytes, offset: int = 0):
    return struct.unpack_from(">fff", data, offset)


def unpack_string(data: bytes, offset: int, length: int) -> str:
    """Dekodiert einen null-terminierten Fixlength-String aus Paket-Daten."""
    raw = data[offset: offset + length]
    null = raw.find(b"\x00")
    return raw[:null].decode("utf-8", errors="replace") if null >= 0 \
        else raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Paket-Builder
# ---------------------------------------------------------------------------

def build_enter_payload(callsign: str, player_type: int = PLAYER_TYPE_TANK,
                        team: int = TEAM_AUTOMATIC, motto: str = "",
                        token: str = "") -> bytes:
    """
    MsgEnter Payload  –  aus ServerLink::sendEnter + PlayerInfo::unpackEnter:
      uint16  type
      uint16  team      (als uint16 gepackt, auch wenn team-Enum signed ist)
      char[32]  callsign
      char[128] motto
      char[22]  token
      char[60]  version
    """
    return (
        struct.pack(">HH", player_type, team & 0xFFFF)
        + pack_string(callsign, CallSignLen)
        + pack_string(motto,    MottoLen)
        + pack_string(token,    TokenLen)
        + pack_string(CLIENT_VERSION_STRING, VersionLen)
    )


def build_negotiate_flags_payload() -> bytes:
    """
    MsgNegotiateFlags (Client→Server)  –  aus bzfs.cxx MsgNegotiateFlags-Handler:
      uint16 count  +  count × 2-Byte-Abkürzungen
    """
    return struct.pack(">H", len(STANDARD_FLAGS)) + b"".join(STANDARD_FLAGS)


def build_player_update(player_id: int, order: int, status: int,
                        pos: tuple, vel: tuple,
                        azimuth: float, ang_vel: float,
                        timestamp: float) -> bytes:
    """
    MsgPlayerUpdate Payload  –  aus ServerLink::sendPlayerUpdate + PlayerState::pack:
      float32  timestamp   (Ticks seit Serverstart – wir nutzen monotone Zeit)
      uint8    player_id
      int32    order       (Sequenznummer, zählt hoch)
      int16    status      (PS_ALIVE etc.)
      float[3] pos
      float[3] velocity
      float32  azimuth
      float32  angVel
    Gesamt: 4+1+4+2+12+12+4+4 = 43 Bytes
    """
    return (
        struct.pack(">f",  timestamp)
        + struct.pack(">B",  player_id)
        + struct.pack(">i",  order)
        + struct.pack(">h",  status)
        + struct.pack(">fff", *pos)
        + struct.pack(">fff", *vel)
        + struct.pack(">f",  azimuth)
        + struct.pack(">f",  ang_vel)
    )
