# bzflag-bot

Bots für BZFlag 2.4 – füllen den Server automatisch auf und machen Platz, wenn echte Spieler beitreten.

## Features

- Vollwertiger Spieler (`PLAYER_TYPE_TANK`), umgeht `-disableBots`; client-seitige Physik & Hit-Detection
- Kampf-KI: Zielauswahl (Radar/FOV), Vorhalt-Schießen, Ausweichen, taktische & defensive Sprünge
- Flag-Strategie: gute Flags nutzen, schlechte/neutrale ablegen, beobachtete Best-Flaggen (GM/L/SW) gezielt priorisiert ansteuern
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
| `max_bots`        | `3`         | Größe des aktiven Bot-Pools bei echter Präsenz (Spieler/Zuschauer)          |
| `min_bots`        | `0`         | Grundstock (nie unterschritten); allein aktiv, wenn niemand da ist          |
| `bot_name_prefix` | `Bot_`      | Präfix, den **jeder** Bot erhält (dient zugleich der zuverlässigen Bot-Erkennung) |
| `bot_callsigns`   | `[]`        | **Basisnamen** der Bots (ohne Präfix, z.B. `["Zwiebel", "Tomate"]`); In-Game-Name = `bot_name_prefix` + Basisname. Leer → `bot_name_prefix` + Nummer (`Bot_01`, …) |
| `observer_callsign` | `Bot-Manager` | Callsign des Fallback-Observers (verbindet nur, wenn kein Bot Spielerzahlen liefert) |
| `team`            | `65534`     | Team-ID: `0`=Rogue, `1`=Red, `2`=Green, `3`=Blue, `4`=Purple, `5`=Observer, `6`=Rabbit, `7`=Hunter, `65534`=auto |
| `motto`           | `""`        | Bot-Motto                                                                    |
| `token`           | `""`        | BZFlag-Auth-Token (leer = unregistriert)                                     |
| `world_half`      | `400.0`     | Halbe Weltgröße in Einheiten (Standard-Map = 800x800)                        |
| `check_interval`  | `5.0`       | Sekunden zwischen Rebalance-Prüfungen                                        |
| `reconnect_delay` | `10.0`      | Sekunden vor Reconnect des Fallback-Observers nach Verbindungsabbruch        |
| `idle_cleanup_delay` | `300.0`  | Sekunden ohne echte Präsenz, bis auf `min_bots` abgeräumt wird (`0` = sofort) |
| `good_flags`      | eingebaute Liste | Flaggen-Kürzel, die die Bots behalten und nutzen                        |
| `bad_flags`       | eingebaute Liste | Flaggen-Kürzel, die die Bots sofort ablegen                             |
| `best_flags`      | `GM,L,SW`        | Besonders begehrte Flaggen, bevorzugt angesteuert; implizit zu `good_flags` ergänzt |
| `bot_lifetime_min` | `900`      | Minimale Bot-Lebensdauer in Sekunden; danach Ersatz durch neuen Bot (Namens-/Statistik-Rotation) |
| `bot_lifetime_max` | `7200`     | Maximale Bot-Lebensdauer in Sekunden                                         |
| `log_level`       | `INFO`      | `DEBUG` / `INFO` / `WARNING` / `ERROR`                                       |
| `profile`         | `false`     | Bots unter cProfile starten; Profil wird beim regulären Bot-Ende (Stop/Rotation/Rundenende) als `.prof` geschrieben |
| `profile_dir`     | `/tmp`      | Zielordner für die `.prof`-Dateien (Auswertung: `python -m pstats …`)        |

## Kommandozeilenargumente

### `bot_manager.py`

```
python bot_manager.py [Optionen]

  --config YAML           Pfad zur YAML-Konfigurationsdatei
  --host HOST             Server-Hostname
  --port PORT             Server-Port
  --max_bots N            Größe des aktiven Bot-Pools bei Präsenz
  --min_bots N            Grundstock-Bot-Anzahl (nie unterschritten)
  --bot_name_prefix P     Callsign-Präfix für jeden Bot (auch zur Erkennung)
  --bot_callsigns NAMEN   Kommagetrennte Bot-Basisnamen (ohne Präfix)
  --observer_callsign C   Callsign des Fallback-Observers
  --team TEAM             Team für alle Bots
  --motto TEXT            Motto
  --token TOKEN           Auth-Token
  --world_half FLOAT      Halbe Weltgröße
  --check_interval S      Rebalance-Intervall in Sekunden
  --idle_cleanup_delay S  Sekunden ohne Präsenz bis Abräumen auf min_bots (0 = sofort)
  --good_flags FLAGS      Kommagetrennte Flaggen-Kürzel, die die Bots behalten
  --bad_flags FLAGS       Kommagetrennte Flaggen-Kürzel, die die Bots ablegen
  --best_flags FLAGS      Kommagetrennte, besonders begehrte Flaggen (bevorzugt angesteuert)
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
  --world-half FLOAT      Halbe Weltgröße                    (Default: 400.0)
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
├── bot/                       – Bot-Logik als Paket
│   ├── constants.py           – Spiel-Konstanten (+ Server-Var-Tabelle)
│   ├── core.py                – BZBot: Game-Loop, Server-Updates, Spawn
│   ├── handlers.py            – Message-Handler (_on_*, _on_set_var)
│   ├── hit_detection.py       – Hit-Detection, Steamroller, Schuss-Cleanup
│   ├── models.py              – Shot/PlayerInfo/FlagInfo/AIState
│   ├── util.py                – Geometrie-Helfer
│   └── ai/                    – BZBotAI als 9 Mixins
│       ├── __init__.py        – Mixin-Zusammensetzung von BZBotAI (MRO)
│       ├── capabilities.py    – _can_*-Gates und _effective_*-Ableitungen aus Flagge + Server-Variablen
│       ├── combat.py          – COMBAT-Tick, Bedrohungsreaktion/Dodge, Eskalation bei unerreichbaren Gegnern
│       ├── navigation.py      – A*-Planung (sync + async Worker), Wegpunkt-Abfahren, NAV_JUMP-Vorbereitung, Teleporter-Querung
│       ├── perception.py      – FoV/LoS-Prädikate, Radar-Aufmerksamkeit, Sichtbarkeits-Gates, Bedrohungserkennung eingehender Schüsse
│       ├── physics.py         – Lokale Physik-Simulation des eigenen Tanks: Integration, Boden-/Hindernis-Kollision
│       ├── shooting.py        – Zielpunkt-/Ricochet-Berechnung, Feuer-Gates, alle _maybe_shoot_*-Zweige, _send_shot
│       ├── states.py          – State-Machine: Zustandsübergänge, 60-Hz-Dispatch, alle _tick_*-Zustände außer COMBAT
│       ├── tactics.py         – Sprung-Ausführung, taktischer Übersprung, Z-Höhenangriff, Rückwärtsfahrt-Entscheid
│       └── targeting.py       – Gegner-Scoring, Ziel-Validierung/Staleness, Flaggen-Route
├── bzflag/
│   ├── __init__.py            – Paket-Init
│   ├── client.py              – TCP/UDP-Client mit Handshake und Message-Dispatch
│   ├── intersect.py           – Geometrie-Primitive (Port bzfs Intersect.cxx): OBB-OBB-/Ray×Box-/Segment×Box-Overlap
│   ├── nav_graph.py           – NavGraph: A*-Pfadsuche, Layer-Verwaltung, Sprung-/Fall-Kanten
│   ├── obstacle_grid.py       – ObstacleGrid: Broad-Phase-Beschleunigung (Zellen-Grid, DDA-Ray)
│   ├── protocol.py            – BZFlag 2.4 Protokoll-Konstanten und Hilfsfunktionen
│   ├── shot_physics.py        – Schuss-Physik: simulate_shot_path (Bounce-Simulation), Teleporter-Querung
│   ├── world_parser.py        – MsgGetWorld-Parser: zlib-Dekomprimierung, Obstacle-Parsing
│   └── world_map.py           – Datenklassen: BoxObstacle, WorldMap, FlagInfo
├── tests/                     – Unit-Tests (kein Server nötig; `pytest tests/ -v`)
│   ├── conftest.py            – Pytest-Fixtures (Bot-Mock, Karten-Fixture-Loader)
│   ├── test_async_plan.py     – Asynchrones Pathfinding (Worker, Prefix-Resync)
│   ├── test_bot_manager.py    – Bot-Manager (Rebalancing, Observer-Zählung, Profiling)
│   ├── test_bzbot_managed.py  – Managed-Modus (IPC-Statuszeilen, stdin-Kommandos)
│   ├── test_capability_checks.py – Flag-Fähigkeiten (FO/RO/LT/RT, NJ, OO, …)
│   ├── test_client_join.py    – Join-Handshake (Accept/Reject-Auswertung)
│   ├── test_client_udp_addr.py – UDP-Zieladresse (gecachte IP statt Hostname)
│   ├── test_dodge_and_jump.py – Ausweichen und Sprung-Fallback
│   ├── test_flags.py          – Flag-Strategie (Grab/Drop, Klassifizierung, Effektiv-Stats)
│   ├── test_geometry.py       – Geometrie-Hilfsfunktionen und Shot-Methoden
│   ├── test_hit_detection.py  – Hit-Detection (SW, GM, Laser, SR, Obesity, Narrow-OBB)
│   ├── test_idle_early_out.py – Leerlauf-Early-Outs bei leeren Shot-Dicts
│   ├── test_intersect.py      – OBB-OBB-Overlap (rect_rect_overlap) + Shim-Re-Export aus shot_physics
│   ├── test_kill_payloads.py  – MsgKilled-Payload für alle Waffenarten
│   ├── test_movement.py       – Bewegung (Waypoints, Schwerkraft, BY-Flag)
│   ├── test_nav_graph.py      – NavGraph A*/Layer (Karten-Fixtures, ggf. übersprungen)
│   ├── test_pause.py          – Pause-Behandlung (MsgPause: nicht beschießen, warten)
│   ├── test_performance.py    – Performance-/Timing-Checks (`pytest -m perf -s`)
│   ├── test_protocol.py       – MsgSetVar/GameSettings-Parsing, Limited-Flags
│   ├── test_rabbit.py         – Rabbit-Chase: MsgNewRabbit-Team-Umbelegung (Rabbit/Hunter, Freund/Feind)
│   ├── test_sb_hit.py         – SB-Treffer: Wand-Phasing, Längskapsel, Hit-Fenster
│   ├── test_setvar_snapshot.py – Snapshot-Test: alle Server-Variablen → Attribute
│   ├── test_shooting.py       – Schieß-Logik: GM-Targeting, Ricochet-Aim, Burst-Intervalle
│   ├── test_shot_parsing.py   – MsgShotBegin-Parsing, SW/Laser/Thief-Sofortcheck
│   ├── test_shot_physics.py   – Ricochet-Pfad-Simulation (Bounce, Normalen)
│   ├── test_tactics.py        – Taktische Sprünge, Z-Attack, Landing-Shot, State-Machine
│   ├── test_targeting.py      – Zielauswahl (Radar/FOV, Stealth, Cloaking, Team, LoS-Cache)
│   ├── test_teleporter.py     – Teleporter (Querung, Pfad-Resync, NAV_TELE)
│   ├── test_thin_wall_obb.py  – Dünne 135°-Wand: OBB-Kollision, kein Wand-Durchschuss (normal/GM)
│   ├── test_tick_memo.py      – Per-Tick-Memo (LoS/FloorZ/Muzzle)
│   ├── test_update_cadence.py – 30-Hz-Kadenz der Positions-Updates (Anker, Stall)
│   └── test_world_parser.py   – MsgGetWorld-Parsing (zlib, Obstacles)
├── bot_manager.py             – Manager für mehrere Bots
├── bzbot.py                   – Einzelner Bot (direkt startbar; Entry-Point)
├── config.yaml                – Konfigurationsbeispiel
└── README.md
```

## Kompilieren mit mypyc

Installation von mypy und genereller Test der Python-Typen:
```bash
pip install mypy
mypy --namespace-packages bot bzflag
```

Tatsächliches Kompilieren mit mypyc
```bash
mypyc --namespace-packages bot bzflag
```

**Wichtig:** `mypy-extensions` ist eine **Laufzeit-Abhängigkeit** — die `@trait`-Mixins
importieren es auch im unkompilierten Betrieb (Entwicklung/Tests). `pip install mypy` bringt
es automatisch mit; in Umgebungen ohne mypy (z. B. schlanke Runtime-Container) muss es
explizit installiert werden:
```bash
pip install mypy-extensions
```
Details zu den mypyc-Idiomen (Traits, `bot/_bot_base.py`, statisches `__all__`):
DEVELOPER.md, Sektion 12.

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

Der Manager ist ein eigener, langlebiger Prozess. Er spielt selbst nicht mit, sondern
sorgt dafür, dass auf dem Server stets eine sinnvolle Anzahl Bots läuft: genug, damit ein
beitretender Mensch sofort Gegner vorfindet, aber nie so viele, dass echte Spieler oder die
CPU des Hosts unnötig belastet werden. Dazu beobachtet er fortlaufend die **Präsenz** auf dem
Server (Spieler und Zuschauer) und startet oder beendet Bots als eigenständige Subprozesse.

Die Anzahl richtet sich nach der Präsenz. Ist niemand da, hält der Manager nur den Grundstock
`min_bots` – diese Bots bleiben passiv stehen und verbrauchen kaum Ressourcen. Sobald ein echter
Spieler oder Zuschauer verbunden ist, füllt er den aktiven Pool bis `min_bots + max_bots` auf und
zieht für jeden **aktiv spielenden** Menschen einen Bot ab (Zuschauer wecken die Bots, belegen aber
keinen Spielplatz). Verlässt der letzte Mensch den Server, wird nicht sofort abgeräumt, sondern erst
nach `idle_cleanup_delay` wieder auf `min_bots` zurückgefahren – das überbrückt kurze Verbindungs­abbrüche
und Rundenwechsel.

Seine Sicht auf die Präsenz bezieht der Manager primär von den Bots selbst: Jeder Bot meldet die von ihm
wahrgenommene Spieler-/Zuschauerzahl per IPC (getaggte stdout-Zeile `@@BZMGR@@ {…}`). Läuft gerade kein
Bot, verbindet sich der Manager ersatzweise selbst als stiller Beobachter (TankPlayer + ObserverTeam,
`observer_callsign`) und zählt direkt – sobald wieder ein Bot meldet, trennt er diese Hilfsverbindung.
Damit die Bots einander nicht fälschlich für Menschen halten, verteilt der Manager außerdem die Liste
aller aktiven Bot-Callsigns an jeden Bot (`{"type":"bots",…}`).

Neben der Skalierung übernimmt der Manager die Betriebsstabilität: Er startet abgestürzte Bots neu
(mit exponentiellem Backoff gegen Restart-Schleifen bei dauerhafter Server-Ablehnung), rotiert Bots nach
einer zufälligen Lebensdauer für frische Namen/Statistiken und koordiniert bei Rundenende einen sauberen
Gesamt-Neustart.

## Bekannte Einschränkungen

1. **Protokollversionen**: Getestet gegen BZFlag 2.4.24. Bei abweichenden Versionen
   kann der Handshake fehlschlagen; die Protokollversion (`PROTOCOL_VERSION = b"0221"`)
   in `bzflag/protocol.py` anpassen.

## Lizenz

MIT – freie Verwendung, Weitergabe und Modifikation.
