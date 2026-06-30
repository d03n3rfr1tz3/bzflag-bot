# bzflag-bot

Bots für BZFlag 2.4 – füllen den Server automatisch auf und machen Platz, wenn echte Spieler beitreten.

## Features

- Vollwertiger Spieler (`PLAYER_TYPE_TANK`), umgeht `-disableBots`; client-seitige Physik & Hit-Detection
- Kampf-KI: Zielauswahl (Radar/FOV), Vorhalt-Schießen, Ausweichen, taktische & defensive Sprünge
- Flag-Strategie: gute Flags nutzen, schlechte/neutrale ablegen, gezielt zu Bonus-Flags navigieren
- Flaggen-spezifisches Verhalten für (fast) alle BZFlag-Flaggen (Waffen, Wahrnehmung, Physik)
- Team- und Wahrnehmungs-Bewusstsein (eigenes Team schonen; ST/CL/SE/MQ/CB berücksichtigen)
- 3D-Navigation: A*-Pathfinding über NavGraph inkl. Sprünge auf/über Gebäude (Etagen-Wechsel)
- Automatischer Reconnect bei Rundenende (Zeit- oder Score-Limit)

## Anforderungen

- Python 3.8+
- Keine Pflichtabhängigkeiten
- Optional: `pyyaml` für YAML-Konfiguration (`pip install pyyaml`)
- Optional: `pytest` für Tests (`pip install pytest`)

## Schnellstart

### Manager starten (empfohlen)

```bash
# Mit Konfigurationsdatei:
python bot_manager.py --config config.yaml

# Oder direkt per Kommandozeile:
python bot_manager.py --host mein-server.de --max_bots 3 --min_bots 1
```

### Einzelnen Bot starten

```bash
python bzbot.py --host mein-server.de --callsign "Robo"
```

## Konfiguration (`config.yaml`)

| Schlüssel         | Default     | Beschreibung                                                                 |
|-------------------|-------------|------------------------------------------------------------------------------|
| `host`            | `localhost` | Hostname/IP des BZFlag-Servers                                               |
| `port`            | `5154`      | TCP-Port                                                                     |
| `max_bots`        | `3`         | Maximale gleichzeitige Bots                                                  |
| `min_bots`        | `0`         | Mindestanzahl (nie unterschritten)                                           |
| `bot_name_prefix` | `Bot_`      | Präfix, den **jeder** Bot erhält (dient zugleich der zuverlässigen Bot-Erkennung) |
| `bot_callsigns`   | `[]`        | **Basisnamen** der Bots (ohne Präfix, z.B. `["Zwiebel", "Tomate"]`); In-Game-Name = `bot_name_prefix` + Basisname. Leer → `bot_name_prefix` + Nummer (`Bot_01`, …) |
| `observer_callsign` | `[b0t] Observer` | Callsign des Fallback-Observers (verbindet nur, wenn kein Bot Spielerzahlen liefert) |
| `team`            | `65534`     | Team-ID: `0`=Rogue, `1`=Red, `2`=Green, `3`=Blue, `4`=Purple, `5`=Observer, `6`=Rabbit, `7`=Hunter, `65534`=auto |
| `motto`           | `""`        | Bot-Motto                                                                    |
| `token`           | `""`        | BZFlag-Auth-Token (leer = unregistriert)                                     |
| `world_half`      | `400.0`     | Halbe Weltgröße in Einheiten (Standard-Map = 800x800)                        |
| `check_interval`  | `5.0`       | Sekunden zwischen Rebalance-Prüfungen                                        |
| `reconnect_delay` | `10.0`      | Sekunden vor Reconnect des Fallback-Observers nach Verbindungsabbruch        |
| `log_level`       | `INFO`      | `DEBUG` / `INFO` / `WARNING` / `ERROR`                                       |

## Kommandozeilenargumente

### `bot_manager.py`

```
python bot_manager.py [Optionen]

  --config YAML           Pfad zur YAML-Konfigurationsdatei
  --host HOST             Server-Hostname
  --port PORT             Server-Port
  --max_bots N            Maximale Bot-Anzahl
  --min_bots N            Mindest-Bot-Anzahl
  --bot_name_prefix P     Callsign-Präfix für jeden Bot (auch zur Erkennung)
  --bot_callsigns NAMEN   Kommagetrennte Bot-Basisnamen (ohne Präfix)
  --observer_callsign C   Callsign des Fallback-Observers
  --team TEAM             Team für alle Bots
  --motto TEXT            Motto
  --token TOKEN           Auth-Token
  --world_half FLOAT      Halbe Weltgröße
  --check_interval S      Rebalance-Intervall in Sekunden
  --log_level LEVEL       Log-Level (DEBUG/INFO/WARNING/ERROR)
```

### `bzbot.py`

```
python bzbot.py [Optionen]

  --host HOST             BZFlag-Server-Hostname oder IP     (Default: localhost)
  --port PORT             Server-Port                        (Default: 5154)
  --callsign NAME         Callsign des Bots                  (Default: Bot)
  --team TEAM             Team-ID                            (Default: 65534)
  --motto TEXT            Motto
  --token TOKEN           BZFlag-Auth-Token
  --world-half FLOAT      Halbe Weltgröße                    (Default: 200.0)
  --bot-name-prefix P     Präfix zur Eigenbot-Erkennung      (Default: Bot_)
  --bot-callsigns NAMEN   Kommagetrennte Bot-Namen zur Eigenbot-Erkennung
  --log-level LEVEL       Log-Level                          (Default: INFO)
  --good-flags FLAGS      Kommagetrennte Flaggen-Kürzel, die der Bot behält und nutzt
                          (leer = eingebaute Standardliste)
  --bad-flags FLAGS       Kommagetrennte Flaggen-Kürzel, die der Bot sofort ablegt
                          (leer = eingebaute Standardliste; z.B. "MG,F")
  --limited-flags FLAGS   Kommagetrennte Flaggen-Kürzel mit Server-Schusslimit;
                          Bot unterdrückt Random-/Druckschüsse für diese Flaggen.
                          Limitierte Flaggen werden außerdem automatisch erkannt,
                          sobald der Server eine Shot-Limit-Nachricht sendet
                          (z.B. "GM,L").
  --dump-map PFAD         Schreibt nach dem Welt-Download ein ASCII-Grid der NavGraph-
                          Layer + Obstacle-Liste (Diagnose) und läuft normal weiter.
  --dump-raw PFAD         Schreibt die rohen Weltdaten als <PFAD>.bin + <PFAD>.meta
                          (für Karten-Test-Fixtures, siehe DEVELOPER.md).
```

> Hinweis: Bei Rundenende (Zeit- oder Score-Limit) trennt der Bot und verbindet sich
> nach 5 s automatisch neu (Reconnect-Schleife in `main()`).

## Projektstruktur

```
bzflag-bot/
├── bzflag/
│   ├── __init__.py            – Paket-Init
│   ├── protocol.py            – BZFlag 2.4 Protokoll-Konstanten und Hilfsfunktionen
│   ├── client.py              – TCP/UDP-Client mit Handshake und Message-Dispatch
│   ├── world_parser.py        – MsgGetWorld-Parser: zlib-Dekomprimierung, Obstacle-Parsing
│   ├── world_map.py           – Datenklassen: BoxObstacle, WorldMap, FlagInfo
│   ├── shot_physics.py        – Schuss-Physik: simulate_shot_path (Bounce-Simulation), _segment_hits_obb_3d
│   └── nav_graph.py           – NavGraph: A*-Pfadsuche, Layer-Verwaltung, Sprung-/Fall-Kanten
├── tests/                     – Unit-Tests (kein Server nötig; `pytest tests/ -v`)
│   ├── conftest.py            – Pytest-Fixtures (Bot-Mock, Karten-Fixture-Loader)
│   ├── test_geometry.py       – Geometrie-Hilfsfunktionen und Shot-Methoden
│   ├── test_kill_payloads.py  – MsgKilled-Payload für alle Waffenarten
│   ├── test_shot_parsing.py   – MsgShotBegin-Parsing, SW/Laser/Thief-Sofortcheck
│   ├── test_hit_detection.py  – Hit-Detection (SW, GM, Laser, SR, Obesity, Narrow-OBB)
│   ├── test_shooting.py       – Schieß-Logik: GM-Targeting, Ricochet-Aim, Burst-Intervalle
│   ├── test_targeting.py      – Zielauswahl (Radar/FOV, Stealth, Cloaking, Team)
│   ├── test_movement.py       – Bewegung (Waypoints, Schwerkraft, BY-Flag)
│   ├── test_dodge_and_jump.py – Ausweichen und Sprung-Fallback
│   ├── test_tactics.py        – Taktische Sprünge, Z-Attack, Landing-Shot, State-Machine
│   ├── test_flags.py          – Flag-Strategie (Grab/Drop, Klassifizierung, Effektiv-Stats)
│   ├── test_capability_checks.py – Flag-Fähigkeiten (FO/RO/LT/RT, NJ, OO, …)
│   ├── test_protocol.py       – MsgSetVar/GameSettings-Parsing, Limited-Flags
│   ├── test_world_parser.py   – MsgGetWorld-Parsing (zlib, Obstacles)
│   ├── test_nav_graph.py      – NavGraph A*/Layer (Karten-Fixtures, ggf. übersprungen)
│   ├── test_shot_physics.py   – Ricochet-Pfad-Simulation (Bounce, Normalen)
│   ├── test_bot_manager.py    – Bot-Manager (Rebalancing, Observer-Zählung)
│   └── test_performance.py    – Performance-/Timing-Checks (`pytest -m perf -s`)
├── bzbot.py                   – Einzelner Bot (direkt startbar)
├── bzbot_ai.py                – KI-Logik, Physik, State Machine, Kollision
├── bot_manager.py             – Manager für mehrere Bots
├── config.yaml                – Konfigurationsbeispiel
└── README.md
```

## Tests ausführen

```bash
pip install pytest
pytest tests/ -v
```

Einzelne Kategorien testen:

```bash
pytest tests/ -v -k "dodge"   # Ausweich- und Sprung-Tests
pytest tests/ -v -k "sw"      # Shockwave-Tests
pytest tests/ -v -k "targeting"  # Zielauswahl-Tests
```

### Performance-Checks

Misst die Laufzeit der rechenintensiven Funktionen (nav_graph-Aufbau/A\*/Bogencheck,
Schuss-Simulation mit Ricochet+Teleporter, Ray-Kernels, Hitbox-Detection). Kein Assert —
nur `[PERF]`-/`[TIMING]`-Ausgaben:

```bash
pytest -m perf -s -v
```

Diese Tests sind mit `@pytest.mark.perf` markiert und werden im Normallauf (`pytest tests/`)
automatisch übersprungen, damit die Unit-Suite schnell bleibt.

## Architektur

### Protokoll-Handshake (BZFlag 2.4)

```
Client → Server:  "BZFLAG\r\n\r\n"
Server → Client:  "BZFS0221\x00"   (9 Bytes)

Danach Standard-Pakete:  [uint16 length][uint16 code][payload]

Client sendet:    MsgNegotiateFlags
                  MsgWantWHash
                  MsgGetWorld (Schleife bis bytes_remaining == 0)
                  MsgEnter (Callsign, Team, Type=TANK, Motto, Token, Version)
Server antwortet: MsgAccept (oder MsgReject)
                  MsgAddPlayer (für jeden existierenden Spieler)
                  MsgAddPlayer (für den neuen Spieler selbst → Player-ID)

Bot sendet:       MsgAlive  (Spawn-Anfrage)
                  MsgPlayerUpdate (30×/s via UDP: Position + Velocity)
                  MsgShotBegin  (periodisches Schießen)
                  MsgKilled / MsgAlive (Tod und Respawn)
```

### Manager-Logik

```
Spielerzahl-Quelle:
  Primär melden die laufenden Bots ihre Sicht (Menschenzahl/-liste) per IPC
  (stdout-Zeile "@@BZMGR@@ {…}") an den Manager. Nur wenn gerade kein Bot
  verbunden ist, verbindet sich ein Fallback-Observer (TankPlayer + ObserverTeam,
  observer_callsign) und zählt selbst – sobald ein Bot meldet, trennt er wieder.

Peer-Erkennung:
  Der Manager pusht jedem Bot per stdin die Liste aktiver Bot-Callsigns
  ({"type":"bots",…}); jeder Bot erkennt seine Peers (Präfix + Liste) und
  zählt sie nicht als Menschen.
```

## Bekannte Einschränkungen

1. **Protokollversionen**: Getestet gegen BZFlag 2.4.24. Bei abweichenden Versionen
   kann der Handshake fehlschlagen; die Protokollversion (`PROTOCOL_VERSION = b"0221"`)
   in `bzflag/protocol.py` anpassen.

## Lizenz

MIT – freie Verwendung, Weitergabe und Modifikation.
