# BZFlag-Bot – Developer Architecture Guide

Ein fremder Entwickler soll nach dem Lesen dieses Dokuments den Bot sicher
weiterentwickeln können, ohne die gesamte Codebasis zu lesen. Das Dokument erklärt
**WARUM** Entscheidungen so getroffen wurden, beschreibt **INVARIANTEN** die der Code
voraussetzt, und zeigt **WO** man bei typischen Bugs suchen muss.

Es spiegelt nicht die Docstrings im Code — wer das WHAT sucht, liest den Code selbst.

---

## 1. Architektur-Überblick

### Prozess-Modell

```
bot_manager.py          Steuer-Prozess (kein eigener Tank)
  ├── subprocess ×N     je ein bzbot.py --managed-Prozess pro Bot
  │     stdout: "@@BZMGR@@ {…}"  Bot→Manager (Menschenzahl/-liste)
  │     stdin:  {"type":"bots",…} Manager→Bot (aktive Peer-Callsigns)
  └── ServerObserver    Fallback-BZFlagClient (TankPlayer + ObserverTeam),
                        nur aktiv wenn kein Bot Spielerzahlen liefert

bzbot.py                Bot-Prozess (PLAYER_TYPE_TANK); Entry-Point → bot/-Paket
  ├── BZFlagClient      TCP + UDP, empfängt alle Broadcasts (bzflag/client.py)
  ├── BZBot             Spiellogik, Hit-Detection, Protokoll-Handler, IPC (bot/core.py)
  └── BZBotAI (Mixin)   State Machine, Physik, Navigation (bot/ai/)
```

Manager und Bots sind separate Prozesse, nicht Threads. Die Spielerzahl kommt primär
von den laufenden Bots (IPC über stdin/stdout); ein Observer ist nur Fallback. Der
Manager hat keinen eigenen Tank und sendet keine `MsgPlayerUpdate`-Pakete.

**Profiling-Modus** (`profile: true` in config.yaml): Der Manager startet jeden Bot als
`python -m cProfile -o <profile_dir>/bzbot_<name>_<id>_<zeit>.prof bzbot.py …`. Das Profil
wird nur bei regulärem Prozessende geschrieben — deshalb stoppt `BotProcess.stop()`
gestaffelt SIGINT → SIGTERM → SIGKILL (SIGINT löst `KeyboardInterrupt` aus und lässt
`main()` regulär auslaufen; SIGTERM würde den Interpreter ohne `-o`-Dump töten).

### Modul-Landkarte (Stand nach Struktur-Track 4)

Die frühere Zweiteilung `bzbot.py`/`bzbot_ai.py` ist in das Paket `bot/` zerlegt;
alle Methoden wurden dabei unverändert verschoben (Methodennamen sind stabil geblieben).
`bzbot.py` ist nur noch der startbare Entry-Point (CLI, `main`, Managed-stdin-Reader,
Karten-Dump), `bot_manager.py` startet ihn unverändert als Subprozess.

| Modul | Inhalt |
|---|---|
| `bot/constants.py` | Alle Spiel-Konstanten; pro nachgeführter Konstante die Zuordnung `↔ Server-Var ↔ self._attr` |
| `bot/models.py` | `Shot`, `PlayerInfo`, `FlagInfo`, `AIState` |
| `bot/util.py` | Zustandslose Geometrie-Helfer (`_angle_diff`, `_wrap`, `_segment_point_dist3d`) |
| `bot/core.py` | `BZBot`: `__init__` (gegliedert in `_init_*`-Blöcke), Start/Stop, 60-Hz-Game-Loop, `_send_update`, Spawn, Callsign-Verwaltung |
| `bot/handlers.py` | `HandlersMixin`: alle `_on_*`-Message-Handler; `_on_set_var` tabellen-getrieben (`_SETVAR_VARS`/`_SETVAR_SPECIAL`) |
| `bot/hit_detection.py` | `HitDetectionMixin`: `_resolve_incoming_shots`, Steamroller, Schuss-Cleanup, Hitbox-Helfer |
| `bot/ai/__init__.py` | `BZBotAI` = Sammelklasse der 9 Mixins (Methoden disjunkt → MRO verhaltensneutral) |
| `bot/ai/capabilities.py` | `_can_*`-Gates und `_effective_*`-Ableitungen aus Flagge + Server-Vars |
| `bot/ai/physics.py` | Eigene Tank-Physik: `_run_physics`, `_get_floor_z`, Hindernis-Kollision |
| `bot/ai/states.py` | State-Machine: `_update_movement`/`_dispatch_movement`, alle `_tick_*` außer COMBAT |
| `bot/ai/combat.py` | `_tick_combat`, `_execute_combat_move`, Bedrohungsreaktion/Dodge, Eskalation |
| `bot/ai/navigation.py` | A*-Planung (sync/async), Wegpunkt-Abfahren, NAV_JUMP/NAV_TELE, Teleporter-Querung |
| `bot/ai/perception.py` | FoV/LoS-Prädikate, Radar-Aufmerksamkeit, `_find_incoming_shot` |
| `bot/ai/targeting.py` | Zielwahl, Ziel-Validierung/Staleness, Flaggen-Route |
| `bot/ai/tactics.py` | Taktischer Übersprung, Z-Höhenangriff, Sprung-Ausführung |
| `bot/ai/shooting.py` | Zielpunkt-/Ricochet-Berechnung, Feuer-Gates, alle `_maybe_shoot_*`, `_send_shot` |

Die Engine-Schicht `bzflag/` (protocol, client, world_map, world_parser, obstacle_grid,
shot_physics, nav_graph) war bereits vor dem Umbau sauber geschnitten und ist unverändert.

### Warum `PLAYER_TYPE_TANK` statt `PLAYER_TYPE_COMPUTER`

BZFlag-Server können mit `-disableBots` gestellt werden. Diese Option deaktiviert alle
Verbindungen vom Typ `PLAYER_TYPE_COMPUTER`. `PLAYER_TYPE_TANK` ist der Typ eines echten
menschlichen Spielers — kein Server kann ihn gezielt sperren, ohne alle Spieler zu sperren.

Konsequenz: Der Server behandelt den Bot wie einen Menschen. Alle serverseitigen Regeln
(Shot-Validierung, Team-Regeln, Spawn-Logik) gelten wie für echte Spieler.

### Physik im Client vs. Server

BZFlag-Server akzeptieren **beliebige Positionen** ohne Kollisionsprüfung. Das Protokoll
ist "Client autoritativ" für Bewegung: Jeder Bot berechnet seine eigene Position und
sendet sie via `MsgPlayerUpdate`. Der Server broadcastet sie an alle anderen Spieler.

Das bedeutet:
- Kollisionsprüfung muss client-seitig implementiert werden (→ Sektion 6)
- Hit-Detection muss client-seitig implementiert werden (→ Sektion 7)  
- Der Server prüft nur Shot-IDs und Kill-Meldungen auf Plausibilität

---

## 2. Spielschleife und Timing

### Die zwei Frequenzen

```
_run_game_loop()-Tick läuft mit ~60 Hz (UPDATE_RATE_HZ=60, dt=1/60)
  │
  ├── _resolve_incoming_shots / _check_steamroller — immer, 60 Hz
  ├── _update_movement(dt, now, ai_tick)
  │     ├── _run_physics(dt, now)         — immer, 60 Hz
  │     ├── _apply_bounds(dt)             — immer, 60 Hz
  │     │     └── _apply_obstacle_bounds(dt)  — Kollision, 60 Hz
  │     └── if ai_tick:                   — nur jeder 6. Tick (10 Hz, AI_RATE_HZ=10)
  │           State-Machine-Dispatch (_tick_idle/_seeking/_combat)
  │
  ├── _maybe_shoot(now)            — immer, 60 Hz
  └── MsgPlayerUpdate via UDP      — gedrosselt auf 30 Hz (SERVER_UPDATE_RATE_HZ)
```

`ai_tick = (_tick_count % (UPDATE_RATE_HZ // AI_RATE_HZ) == 0)` → bei 60/10 jeder **6.** Tick.

Physik läuft mit 60 Hz weil der Tank sichtbar ruckelt wenn Schwerkraft und Kollision
mit nur 10 Hz berechnet werden (~2,5u-Schritte bei 25 u/s Tank-Geschwindigkeit), und 60 Hz
entspricht dem BZFlag-Standard-Client. `MsgPlayerUpdate` wird auf 30 Hz gedrosselt
(`_updateThrottleRate`), nicht jeder Physik-Tick erzeugt ein Netzwerk-Paket.

Die KI läuft mit 10 Hz weil State-Machine-Entscheidungen bei 60 Hz zu viel Chaos erzeugen
(Zielwechsel, Path-Replanning, Shoot-Checks alle 20ms) und es keine spielerische Relevanz
hat — menschliche Reaktionszeiten sind 150+ ms.

Die 30-Hz-Drosselung übernimmt `_maybe_send_server_update(now)` (N1a im FABLE-PLAN).
Wichtig ist der **Kadenz-Anker**: `_next_server_update` wird unmittelbar vor Schleifenstart
auf `time.monotonic()` gesetzt. Der frühere Startwert `0.0` lag um die komplette
System-Uptime in der Vergangenheit; der `+= interval`-Catch-up holte das nie auf, und der
Bot sendete faktisch mit Tick-Rate (~60 Hz) statt 30 Hz — auf einem lange laufenden Host
dauerhaft. Nach einem Stall setzt die Methode den Anker neu auf, statt Pakete
nachzuholen (Stall-Klemme, kein Burst); Tests: `test_update_cadence.py`.

Der Tick-Wait selbst ist ein `time.sleep` statt `Event.wait` (N3): ein einzelner Syscall
statt Condition/Lock-Maschinerie (~7% der aktiven CPU plus Futex-Wakeups). `stop()` greift
dadurch erst am nächsten Schleifenkopf — Latenz maximal eine Tick-Dauer (~16ms), bewusst
akzeptiert. Nur der einmalige 0,5s-Pre-Spawn-Wait bleibt unterbrechbar auf `Event.wait`.

### `dt`, `now`, `ai_tick`

```python
# In _run_game_loop() (bot/core.py):
now     = time.monotonic()
dt_r    = now - last_tick              # tatsächliche Zeit seit letztem Tick
ai_tick = (self._tick_count % (UPDATE_RATE_HZ // AI_RATE_HZ) == 0)   # 60/10 → jeder 6.
```

`now` ist immer `time.monotonic()` — nie `time.time()`. Monotonic-Clocks springen nicht
(z.B. beim Sommerzeitwechsel oder NTP-Sync), was für Timer-Logik kritisch ist.

`dt` ist die echte vergangene Zeit, nicht der ideale Tick-Abstand. Bei Systemlast kann
dt deutlich größer als 20ms sein. Alle Physik-Berechnungen benutzen `dt`, keine fixen Werte.
`dt_r` ist zentral auf **0,1s geklemmt** (F4): bei Stalls (GC, Container-Scheduling,
Netz-Hänger) läuft die Simulation kurz verlangsamt weiter, statt in einem Riesenschritt
durch Wände zu tunneln oder das GM-Steering zu überdrehen.

### Per-Tick-Memo (P4a)

`_has_los_to_enemy(pid)`, `_get_floor_z()` und `_muzzle_clear(az)` werden innerhalb
eines Ticks mehrfach mit identischen Eingaben aufgerufen (Combat-Move + Shoot-Check).
Die Ergebnisse sind pro Tick memoisiert; der Memo-Key von `_get_floor_z` enthält
zusätzlich die **Position**, damit Aufrufe nach einer `self.pos`-Mutation im selben Tick
(z.B. `_tick_jumping`) nicht den alten Wert sehen — dadurch ist das Memo ein reiner
Funktions-Cache und garantiert verhaltensidentisch. Tests: `test_tick_memo.py`.

### Server-Zeitstempel

`MsgPlayerUpdate` enthält einen Zeitstempel im Server-Format. Der Offset zwischen
`time.monotonic()` und Server-Zeit wird aus `MsgGameTime` berechnet:

```python
self._server_time_offset = server_s - time.monotonic()
# Nutzung: server_now = time.monotonic() + self._server_time_offset
```

### Threading-Modell und Dict-Snapshot-Konvention

Es gibt zwei dauerhafte Threads (plus im Managed-Modus einen stdin-Reader):

- **Recv-Thread** (`BZFlagClient`): ruft alle `_on_*`-Message-Handler auf. NUR hier
  werden `self.players` und `self.flags` strukturell mutiert (Keys hinzugefügt/entfernt).
- **Game-Loop-Thread** (Main): Physik, KI, Hit-Detection — liest diese Dicts nur.

**Invariante:** Wer `self.players` oder `self.flags` außerhalb des Recv-Threads
iteriert, MUSS über einen Snapshot gehen: `for pid, info in list(self.players.items())`.
Ohne Kopie crasht ein Join/Leave während der Iteration den Game-Loop mit
`RuntimeError: dictionary changed size during iteration` (sporadisch, µs-Fenster —
ein klassischer „läuft tagelang, stirbt einmal die Woche"-Bug). Innerhalb der
`_on_*`-Handler ist direkte Iteration erlaubt (gleicher Thread wie die Mutationen).
Die `list()`-Kopie ist dank GIL atomar genug; ein zusätzliches Lock ist nicht nötig.
`self._shots`/`self._ricochet_paths` haben dagegen ein echtes Lock (`_shots_lock`),
weil dort beide Threads schreiben.

---

## 3. State Machine

### States und ihre Invarianten

| State | Was passiert | Committed? |
|-------|-------------|------------|
| `DEAD` | Wartet auf `MsgAlive` vom Server | Ja (passiv) |
| `IDLE` | Zufalls-Waypoints, kein Schießen | Nein |
| `SEEKING` | Navigiert zu Flagge oder zufälligem Ziel | Nein |
| `COMBAT` | Abstandshaltung zum Ziel + Schießen | Nein |
| `EVADING` | Ausweich-Manöver läuft (`_tick_committed`) | **Ja** |
| `JUMP_WINDUP` | Wind-Up (~80–120ms) vor taktischem Sprung | **Ja** |
| `JUMPING` | In der Luft (taktischer Sprung) | **Physik** |
| `Z_ATTACK` | ZJ1-Höhenangriff (Rotation + Schuss-Warten) | Nein |
| `DODGE_JUMP` | Ausweichsprung in der Luft | **Physik** |
| `LANDING_SHOT` | Wartet auf springenden Gegner | Nein |
| `NAV_JUMP` | Navigationssprung (Etagen-Wechsel) | **Physik** |
| `NAV_JUMP_ALIGN` | Azimuth-Ausrichtung vor NAV_JUMP (`vel=0`) | **Position** |
| `FALLING` | Unkontrollierter Fall vom Dach (kein Sprung) | **Physik** |

**Committed** bedeutet: `_update_movement` ruft `_tick_committed()` auf statt
des normalen State-Dispatch. In `_tick_committed` werden keine neuen Entscheidungen
getroffen — der Timer (`_dodge_until`) läuft ab, dann kommt der Übergang.

**`FALLING`** ist physik-committed wie JUMPING, aber **kein Sprung**: Wenn ein Boden-State
(COMBAT/SEEKING/IDLE/EVADING/LANDING_SHOT) `vel[2] < −0.1` über dem Boden hat (z.B. von einer
Dachkante gefahren), merkt `_update_movement` den aktuellen State als `_pre_fall_state`, setzt
`_jumping=True` (verhindert Schwerkraft-Dopplung) und übernimmt die Boden-Drehrate in
`_jump_ang_vel`. Kein `_last_jump_at`-Cooldown (kein echter Sprung). Bei Landung → zurück zu
`_pre_fall_state`.

**Präsenz-Logik:** IDLE↔SEEKING richten sich nach `_has_presence()` (`human_count > 0` **oder**
`observer_count > 0`). COMBAT erfordert echte Menschen (`human_count > 0`); bei nur-Observer
bleibt der Bot in SEEKING.

**Physik-committed** bedeutet: `_tick_jumping()` wird direkt aufgerufen (vor dem
State-Dispatch). Horizontalgeschwindigkeit und `_jump_ang_vel` bleiben fix bis zur Landung.

### `_transition_to()` — warum zentralisiert

```python
def _transition_to(self, state: AIState) -> None:
    if self._ai_state == state:
        return
    # COMBAT/SEEKING/IDLE → SEEKING/IDLE: veraltete Route löschen
    _clear_on_exit = {AIState.COMBAT, AIState.SEEKING, AIState.IDLE}
    if self._ai_state in _clear_on_exit and state in (AIState.SEEKING, AIState.IDLE):
        self._nav_path = []
        self._nav_goal = None
    logger.info("[%s] State: %s → %s", self.callsign, self._ai_state.name, state.name)
    self._ai_state = state
```

Jeder State-Wechsel geht durch diese Methode. Das macht State-Übergänge im Log
nachvollziehbar. Direktes Setzen von `self._ai_state` ist verboten — immer
`_transition_to()` nutzen.

Beim Verlassen von COMBAT/SEEKING/IDLE in Richtung SEEKING/IDLE werden `_nav_path` und
`_nav_goal` gelöscht — der neue State soll keine veraltete Route aus dem alten State erben.

### Häufige State-Bugs

**Symptom: Endlos-Loop zwischen zwei States**
Prüfe ob der Übergangs-Trigger bidirektional ist (A→B und B→A in gleicher Tick-Kondition).

**Symptom: Bot reagiert nicht auf Bedrohung**
EVADING und JUMP_WINDUP sind committed — `_handle_threat()` gibt dort immer `False` zurück.
Das ist absichtlich: ein laufendes Ausweich-Manöver nicht unterbrechen.

**Symptom: Sofortige Action nach EVADING-Exit**
Nach beiden EVADING-Exit-Pfaden (Early-Exit und Timer-Ablauf) wird der Schuss mit 1s
Grace in `_evade_cleared_shots` gespeichert. Wenn ein neues "sofort"-Verhalten nach
EVADING auftritt, prüfe ob `_last_threat_id = None` irgendwo gesetzt wird ohne
gleichzeitig `_evade_cleared_shots[_last_threat_id] = now + EVADE_CLEAR_GRACE` zu setzen.

---

## 4. Physik-System

### `_run_physics(dt, now)` — bot/ai/physics.py

Läuft immer, unabhängig vom AI-State. Verantwortlich für:
1. BY-Flag Auto-Bounce (alle 0.2s automatischer Sprung)
2. Schwerkraft für Tanks über dem Boden

Schwerkraft-Logik:
```python
if not self._jumping and self.pos[2] > _floor_z + 1e-6:
    self.vel[2] = max(self.vel[2] + self._gravity * dt, -self._tank_speed)
    self.pos[2] = max(self.pos[2] + self.vel[2] * dt, _floor_z)
```

Die Schwelle `1e-6` statt `0.0` ist kein Zufall — bei `pos[2] = 0.0` erzeugen
Floating-Point-Operationen gelegentlich kleine negative Werte, die eine permanente
Schwerkraft-Aktivierung auslösen würden. `1e-6` ist groß genug um das zu verhindern,
klein genug um als "am Boden" zu gelten.

### `_is_landed()` — warum der vel-Check wichtig ist

```python
def _is_landed(self) -> bool:
    if self.vel[2] > 0.1:    # Aufstieg → nie gelandet
        return False
    return self.pos[2] <= self._get_floor_z() + 0.1
```

Ohne den `vel[2] > 0.1`-Check würde `_is_landed()` beim Sprung durch ein Gebäude
`True` zurückgeben, sobald `pos[2]` nah genug an einem Dach ist. Das führt zu
Frühlandung und gebrochener Sprung-State-Machine.

Die Logik: Während des Aufstiegs ist der Tank definitiv nicht gelandet, egal wo er
räumlich ist.

### `_get_floor_z()` — Pixel-on-Bodenauflage

`_get_floor_z()` (bot/ai/physics.py) ruft `nav.get_floor_z(x, y, z, overhang=self._effective_half_width())`.
Der `overhang` (≈ Tank-Halbbreite 1.4u) weitet den Box-Test in `_point_in_rotated_box` — der Bot
gilt als **getragen**, solange seine **Mitte** bis ~eine Tank-Halbbreite über die Plattformkante
hinausragt (Pixel-on: trägt, solange ein Pixel aufliegt). Folge: FALLING-Erkennung
(`_update_movement`), `_is_landed` und die Lande-Snaps werden alle Pixel-on — der Bot fällt
**nicht** schon, wenn seine Mitte die geometrische Kante überquert (das ließ ihn früher kurz vor
dem Sprung von der Kante fahren). `get_floor_z` zieht nur nach unten / rastet beim Landen und der
`roof_z > z + 2.0`-Filter begrenzt die Wirkung auf höhennahe Dächer — kein „Schweben" über hohe
Plattformen, kein Anheben. Default `overhang=0.0` lässt alle anderen Aufrufer (z.B.
`_insert_jump_runups`) exakt.

**Flaggen-Boden (zentral in `_get_floor_z`):** Burrow und Oscillation Overthruster wirken nur am
Boden, daher kapselt `_get_floor_z` ihren z-abhängigen Boden an EINER Stelle (genutzt von Physik,
`_is_landed`, Combat-Move-Checks):
- **OO** → immer `0.0`: phast durch Gebäude, landet/fällt stets auf den Weltboden (man kann mit OO
  **nicht** auf Dächern landen — der Dach-Navigations-Sprung in `_advance_path` schließt OO daher aus,
  neben `NJ`/`BU`). OO **darf** springen, nur die Landung ist auf z=0 geklemmt.
- **BU** → `BURROW_DEPTH` (−1.32) **nur wenn der reguläre Boden ≤ 0** ist; auf einem Dach trägt das
  Dach (kein Durchsacken). Passend dazu greifen die BU-Mali (Speed/Turn in `_effective_tank_speed`/
  `_effective_turn_rate`, Schuss-Immunität, Radar-Reduktion) erst bei `pos[2] < 0` — Burrow „wirkt"
  also nur eingegraben. Spiegelbild: `_effective_optimal_range` rammt einen BU-Gegner nur bei
  `tgt.pos[2] < 0` (auf einem Dach ist er normal trefbar).

### `_apply_bounds(dt)` — Weltgrenzen vor Kollision

```
_apply_bounds(dt):
  1. Weltgrenzen: pos[0/1] auf ±world_half + Bounce
  2. _apply_obstacle_bounds(dt): Gebäude-Kollision (→ Sektion 6)
  3. pos aktualisieren
```

Reihenfolge ist wichtig: Weltgrenzen zuerst, dann Gebäude. Sonst könnte ein Bot
in einem Gebäude an der Weltgrenze "stecken".

---

## 5. Navigationssystem (NavGraph)

Der NavGraph ist das Herzstück des Pathfinding und verdient besondere Aufmerksamkeit.

### Aufbau-Pipeline

```
BZFlagClient._deliver_world()
  └── parse_world(data)           — world_parser.py
        └── WorldMap(boxes, ...)  — world_map.py
              └── get_nav_graph(world_map)   — nav_graph.py
                    └── NavGraph.__init__()
                          ├── _build_ground_layer()
                          └── _build_roof_layers()
```

Der NavGraph wird einmalig gebaut und dann gecacht. Der Cache-Key ist `world_hash`
(MD5 des Karten-Binärblobs). Mehrere Bots auf demselben Server teilen sich denselben
NavGraph-Objekt.

### Ground-Layer

Der Boden-Layer ist ein 2D-Raster über `±world_half` mit Zellgröße `CELL_SIZE=4u`.

```python
n = max(1, int(2.0 * world_half / CELL_SIZE))    # Anzahl Zellen pro Achse
walkable = [[True] * n for _ in range(n)]
```

Zellen werden blockiert via `_mark_blocked(layer, obs, TANK_MARGIN=3.5)`. Diese Funktion
berechnet die AABB des Obstacles im Axis-Aligned-Koordinatensystem (nicht rotiert!) und
markiert alle Zellen innerhalb als blockiert. Das bedeutet, dass rotierende Gebäude einen
größeren Bereich blockieren als nötig — das ist der Preis für einfache und schnelle
Berechnung.

`TANK_MARGIN=3.5u` entspricht `ceil(TANK_HALF_DIAG ≈ 3.31u)`. Das ist der
Mindestabstand, den der Tank-Mittelpunkt von einer Gebäudewand haben muss.

### Roof-Layer

Für jedes Gebäude mit `roof_z ≤ MAX_ROOF_H=55u` wird ein separater Layer erzeugt.

```python
# Begehbare Dachfläche = Gebäude-AABB - TANK_MARGIN auf jeder Seite
w = ext_x - TANK_MARGIN    # ext_x = Gebäudebreite/2 im AABB-Koordinatensystem
d = ext_y - TANK_MARGIN
if w < CELL_SIZE or d < CELL_SIZE:
    continue  # zu schmales Gebäude → kein Dach-Layer
```

Roof-Layer haben `source_obstacle = obs` (das Gebäude). Das nutzt `find_layer_at()`
um zu prüfen ob der Bot auf diesem Dach steht.

### `find_layer_at()` — Etagen-Bestimmung

```python
def find_layer_at(self, x, y, z):
    for lid, layer in enumerate(self.layers):
        if layer.z > z + 0.5:           # Etage zu hoch
            continue
        if layer.source_obstacle is not None:
            if not _point_in_rotated_box(layer.source_obstacle, x, y):
                continue                 # Bot nicht auf diesem Dach
        if layer.z > best_z:
            best_lid = lid
    return best_lid
```

**Wichtiger Subtilität**: Der Boden-Layer (lid=0) hat `source_obstacle=None` und wird
nie durch den `_point_in_rotated_box`-Check ausgeschlossen. Das bedeutet: Wenn kein
Roof-Layer passt, fällt der Bot auf Layer 0 zurück — den Boden.

`_point_in_rotated_box()` testet den exakten rotierten Obstacle-AABB mit 0.5u Toleranz.
Das ist bewusst weiter als `layer.contains_xy()` (die die um TANK_MARGIN reduzierte
Dachfläche prüft): Ein Bot am Dachrand steht physisch noch auf dem Dach, auch wenn er
außerhalb der begehbaren Dach-Zellen ist.

### A*-Algorithmus

Knoten: `(layer_id, ix, iy)` — Layer-Index + Zell-Koordinaten

Zwei Typen von Kanten werden erzeugt:
1. **Horizontale Kanten** (gleiche Etage, 8-direktional): während des A*-Aufbaus
2. **Vertikale Kanten** (Sprung/Fall): on-the-fly via `_vertical_neighbors()` für
   jeden expandierten Knoten

On-the-fly bedeutet: Die Sprung-Kanten werden nicht vorab berechnet und gespeichert,
sondern erst wenn A* einen Knoten expandiert. Das spart erheblich Speicher bei großen
Karten.

### Sprung-Kanten (Physikalische Validierung)

`self._tank_speed = 25u/s` ist die **horizontale Fahrgeschwindigkeit**, nicht die
Sprunggeschwindigkeit (`v0 = 19u/s`). Der Bot springt und fährt gleichzeitig horizontal.

**Pixel-on-Physik (BZFlag):** Ein Tank trägt/landet, solange auch nur ein Pixel seiner
Hitbox auf einer Plattform liegt — der Mittelpunkt darf bis zu eine halbe Tankbreite
(`JUMP_EDGE_TOL = 1.4u`) über die Kante hinausragen. `_vertical_neighbors` modelliert das
für den **Sprung-rauf**-Zweig:

1. **Ziel-Footprint exakt (rotationskorrekt):** Der nächstgelegene Punkt (`np_x, np_y`,
   = Sprungrichtung **und** Landefläche) wird für rotierte Plattformen im lokalen Frame
   berechnet. So wird die **Spitze** einer 45°-gedrehten Plattform als Landefläche zugelassen
   (früher klammerte der achsenparallele `cx±half_w`-Kasten sie weg → die HIX-Randplattformen
   waren unerreichbar → „A\* Expansion-Limit"). Die zu überbrückende Strecke ist der
   **euklidische** Abstand `gap = dir_len` der Zellmitte zu `np` — **nicht** `aabb_dist`
   (Chebyshev-Abstand im gedrehten Frame), das bei Diamant-Spitzen die echte Sprungweite
   stark unterschätzt. `aabb_dist` dient nur noch dem groben `JUMP_RANGE`-Cull und dem
   `<0.1`-„direkt-darunter"-Schutz.
2. **Absprung-Überhang:** Der Tank rollt von der Margin-eingerückten Startzelle bis zum
   Quell-Plattformrand (+`JUMP_EDGE_TOL`) vor — analytischer Ray-Box-Exit. **Gedeckelt auf
   `_margin_for(src, layer.z) + JUMP_EDGE_TOL`** (nur der Margin-Inset der Startzelle + Pixel-on-
   Front), **nicht** auf `t_exit`/`aabb_dist`. Wichtig: Ohne diese Deckelung entspräche der
   Überhang dem Rollen quer über die **gesamte** Plattform bis zum Rand → Innenzellen bekämen
   unmöglich weite Sprungkanten (Absprung fällt zu kurz, falsch platzierte Run-ups). So gehen
   Sprungkanten nur noch von Zellen nahe der Plattformkante aus; Innenzellen erreichen die
   Kante über normale Geh-Edges.
3. **Front-Catch:** Der Tank landet, sobald seine **Front** die Zielkante erreicht →
   `eff_gap = gap - overhang - JUMP_EDGE_TOL`, geprüft gegen `tank_speed·t_desc·0.9`
   mit `t_desc = (v0 + √disc)/g`.
4. **Clearance:** Beim Erreichen der Zielkante muss der Bot schon hoch genug sein
   (`z ≥ dz-0.5`), sonst stößt er in die Flanke. Daraus folgt ein minimaler horizontaler
   Absprungabstand `wall_dist_min`; der **Überhang wird so gedeckelt**, dass dieser erhalten
   bleibt (Trade-off gap-Verkürzung ↔ Steig-Runway). Zu nah an einer hohen Plattform →
   kein Sprung.
5. `aabb_dist < 0.1` (direkt unter dem Dach) bleibt verboten — kein Hochspringen durch den
   eigenen Decken-Körper.

Der frühere ±30°-Korridor-Check (NAV-15) **entfällt**: Erreichbarkeit deckt `eff_gap` ab,
und seine „Überschuss bei Maximalreichweite"-Semantik war für Fast-Senkrecht-Sprünge auf
schmale Diagonalbalken falsch (der Bot wählt seine Absprunggeschwindigkeit passend zum
Landepunkt und schießt nicht hinaus).

**Konsistenz Ausführung ↔ Planung:** `_nav_jump_feasible` / `_nav_jump_geometry_ok` (bot/ai/navigation.py)
ziehen denselben Front-Catch ab (`hdist - JUMP_EDGE_TOL`, bestehender `·1.1`-Puffer), sonst
plant A\* einen Sprung, den der Ausführungs-Gate ablehnt → NAV_JUMP_ALIGN-Timeout/Replan.

**Sprung-Landewegpunkt = Eintrittspunkt `np`, nicht Zellmitte (NAV-17):** Der Eintrittspunkt
`np` (nächster Punkt auf dem ggf. rotierten Ziel-Footprint = Plattformkante/Diamant-Spitze) wird
zentral in `_entry_point(dst, wx, wy)` berechnet und von `_vertical_neighbors` **und** `plan_path`
genutzt. `plan_path` gibt für Sprung-Landungen (`layer.z` steigt) **`np` als Wegpunkt-XY** aus —
**nicht** die `cell_to_world`-Zellmitte. Grund: Bei 45°-Plattformen liegt die per
`world_to_cell(np)` gerasterte Zelle weit innen (das achsenparallele Raster reicht nicht bis zur
Diamant-Spitze), während die Erreichbarkeit gegen `np` (nah) geprüft wurde. Der Bot bekäme sonst
ein zu weit innen liegendes Sprungziel und verwürfe den Sprung am Feasibility-Rand (Marge +1.7u →
30-s-Cooldown → A*-Expansion-Limit → Direktmodus → **Sturz über die Kante**). Mit `np` als Ziel ist
die Marge wieder ~+11u. Die Landung selbst ist Pixel-on (ein Pixel der Tank-Hitbox auf der
Plattform genügt), daher ist die Spitze ein gültiger Landepunkt; der A*-**Knoten** bleibt die Zelle
(Graph-Konnektivität unverändert). `np ≤` Zellmitten-Abstand → Sprünge werden nie schwerer.
Abgesichert durch `tests/test_nav_graph.py::_executable_jump_margins` (spiegelt das Bot-Gate) —
die HIX-Tests prüfen **Ausführbarkeit** (Marge ≥ 5u), nicht nur Pfad-Existenz.

### Fall-Kanten

Nur von Randzeilen eines Dach-Layers (`ix == 0` oder `ix == n_x-1` etc.). Kosten
sind günstiger als Sprung-Kanten (×0.5 für die Höhendifferenz), weil Fallen passiv
ist und keine Sprung-Energie kostet.

### Weighted A* und Kosten-Kalibrierung

**`_ASTAR_WEIGHT = 1.5`** (Epsilon-optimaler A*):

Der klassische A* (w=1.0) findet den kürzesten Pfad, expandiert auf großen Karten
aber sehr viele Knoten. Mit w=1.5 darf der gefundene Pfad bis zu 50% teurer als das
globale Optimum sein — dafür expandiert der Algorithmus 2–4× weniger Knoten.
Bei w=2.0 wären 100% Umwege möglich, was sichtbar wäre. 1.5 ist der empirische
Sweet-Spot: In der Praxis liegt die Abweichung oft deutlich unter 50%.

**Expansion-Limit (`ASTAR_MAX_EXPANSIONS=5000` Knoten)**: Sicherheitsnetz gegen
pathologische Karten mit vielen Engpässen. Bei `CELL_SIZE=4u` und `world_half=400u`
gibt es ≈(200×200)=40.000 Bodenzellen; 5.000 entspricht ≈12,5% davon. Wenn das
Limit greift: kein leeres `[]`, sondern ein Best-Effort-Teilpfad zum bisher
zielnächsten expandierten Knoten (siehe Kommentar zu `ASTAR_MAX_EXPANSIONS` in
`bzflag/nav_graph.py`).

**Heuristik `_h()`**:

```python
h = hypot(gx - wx, gy - wy)        # 2D-Euklidische Distanz
h += max(0.0, goal_z - layer.z)     # nur Aufstiegskosten addieren
```

Warum nur Aufstieg (`max(0, ...)`): Abstieg kostet keine Energie (der Bot fährt
während des Falls horizontal weiter). Die Heuristik bleibt admissibel — sie
unterschätzt nie die echten Kosten — was Epsilon-Optimalität garantiert.

**Kostenstruktur:**

| Kanten-Typ | Formel | Begründung |
|---|---|---|
| Gerade (horizontal) | `CELL_SIZE × 1.0` | Basis |
| Diagonal (horizontal) | `CELL_SIZE × 1.414` | √2 für Diagonaldistanz |
| Sprung (vertikal) | `hdist × 1.5 + dz × 1.0` | 50%-Distanzaufschlag: Sprünge sind riskant (Fehlschlag = Replan) |
| Fall (vertikal) | `dz × 0.5 + hdist × 1.5 + 5.0` | 0.5× Höhe (passiv/gratis); +5.0 fixer Aufschlag wegen kurzfristigen Kontrollverlusts |

**Same-Layer-Präferenz** entsteht implizit: Horizontale Kanten sind günstiger als
Sprung/Fall-Kanten. A* wählt Mehretagen-Routen nur wenn der direkte Bodenweg
unmöglich ist.

### Cache und Invalidierung

```python
# nav_graph.py
_nav_cache: Dict[str, NavGraph] = {}    # world_hash → NavGraph

def get_nav_graph(world_map):
    key = world_map.world_hash          # MD5 des Karten-Binärblobs
    if key not in _nav_cache:
        _nav_cache[key] = NavGraph(world_map, max_jump_h)
    return _nav_cache[key]

def invalidate_nav_cache(world_hash):
    _nav_cache.pop(world_hash, None)
```

**Kritische Einschränkung**: Der Cache-Key ist nur der Karten-Hash, nicht
`world_half`. Wenn `_worldSize` per MsgSetVar ankommt (nach dem Welt-Download)
und `world_half` sich ändert, muss der Cache explizit invalidiert werden:

```python
# bot/handlers.py, _on_set_var:
if abs(new_half - old_half) > 0.1:
    invalidate_nav_cache(self._world_map.world_hash)
    self.client._deliver_world()    # NavGraph neu bauen mit korrektem world_half
```

### Pfad-Ausführung und NAV_JUMP

```
SEEKING/COMBAT/IDLE → _plan_path() → nav_path = [(wx, wy, lz), ...]
                                      _nav_path_fresh = True    ← kein FOV-Check für ersten WP
                    ↓
                  _check_advance_path() (60 Hz, in _move_to_target + _execute_combat_move)
                    ├── dist_to_wp < _wp_reach_radius()     → _advance_path(); _nav_path_fresh = False
                    ├── WP hinter dem Bot (_is_ahead ±90°)  → Route löschen; ggf. _new_target()
                    │   [nur wenn nicht _nav_path_fresh]
                    └── sonst: Bewegung fortsetzen
                  _advance_path()
                    ├── nächster WP auf gleicher Etage → target_pos aktualisieren
                    └── nächster WP auf anderer Etage  → NAV_JUMP
```

**`_wp_reach_radius()`**: gibt `NAV_CELL_SIZE = 4.0u` zurück wenn der nächste WP nach dem
aktuellen einen Höhenwechsel erfordert (NAV_JUMP-Anlauf-Präzision), sonst
`NAV_CELL_SIZE * 1.25 = 5.0u`.

**`_insert_jump_runups`** (nav_graph.py): Wird in `plan_path` aufgerufen. Fügt **vor** jede
Absprungzelle (Höhenwechsel > 1.5u) einen Run-up-WP ein — eine Zelle (CELL_SIZE) hinter der
Absprungzelle in Sprungrichtung. Reihenfolge: `… → Run-up → Absprungzelle → Sprung-Landung`.
Der Bot erreicht die Absprungzelle dadurch bereits in Sprungrichtung ausgerichtet und springt
**von ihr** ab (nicht vom Run-up dahinter) → kein Drehen/Zurücksetzen am Absprung. Zwei Schutz-
Bedingungen: (a) kein Run-up, wenn die Absprungzelle bereits der Start-WP ist (Bot richtet sich
vor Ort aus); (b) **Thin-Wall-Guard** (`_runup_crosses_thin_wall`) — kein Run-up, wenn Anfahrt
(`pred→runup`) oder Ausrichtung (`runup→Absprung`) eine dünne Wand der Ebene kreuzen würde
(verhindert ein Hängenbleiben an der diagonalen z=15-Trennwand).

**Rückwärts zum Anlaufpunkt (`_should_reverse_to_wp`)**: Liegt der Anlauf-WP **kurz hinter**
dem Bot (`|Winkel| > 100°`, Distanz ≤ `NAV_CELL_SIZE·2.5`) und ist der **nächste** WP ein
Sprung-rauf (`nav_path[1].z - nav_path[0].z > 1.5`), fährt der Bot das kurze Stück **rückwärts**
zum Anlaufpunkt statt zweimal voll zu drehen (erst zum Anlauf hin, dann in `NAV_JUMP_ALIGN`
wieder zurück). Rückwärts kommt er bereits grob in Sprungrichtung ausgerichtet an. Genutzt in
`_move_to_target` und `_execute_combat_move` (`reverse=`-Parameter von `_navigate_wp`); gesperrt
bei `FO`-Flag (`_can_move_backward`).

**NAV_JUMP-Fehlschlag-Erkennung**: `_initiate_nav_jump` speichert `_nav_jump_target_z = wp[2]`.
Nach der Landung vergleicht `_tick_nav_jump` `floor_z` mit `_nav_jump_target_z`. Differenz >1.5u
→ falscher Boden → Route verwerfen, `_new_target()`.

**Return-State-Auflösung (nie auf sich selbst, P3-NAV-13)**: `_initiate_nav_jump` löst den
`_nav_jump_return_state` über `NAV_JUMP_ALIGN` hinweg auf den echten Eigentümer auf — wird der
Sprung aus `NAV_JUMP_ALIGN` gestartet (Normalfall), erbt er dessen `_nav_jump_align_return_state`
(COMBAT/SEEKING), **nicht** `NAV_JUMP_ALIGN`. Zusätzlich mappen `_tick_nav_jump` und
`_tick_nav_jump_align` jeden `ret ∈ {NAV_JUMP, NAV_JUMP_ALIGN}` über `_ground_state()` auf einen
Boden-State. Sonst „steigt" der 5-s-Timeout via `_transition_to(NAV_JUMP_ALIGN)` als No-Op auf
sich selbst aus → Bot bleibt regungslos in NAV_JUMP_ALIGN hängen (war die Ursache des „46-s-
Standbild auf z=30"-Bugs).

**COMBAT-spezifische Invarianten:**
- Max. 8 WPs in `_nav_path` (nach jedem Replan auf 8 begrenzt, ≈32u bei CELL_SIZE=4u)
- `_skip_nav`-Bedingung (Direktziel-Modus): `_not_below_enemy = bot_z + TANK_HEIGHT > enemy_z`
  (Gegner nicht deutlich höher) **und** `dist < _dist_thresh`, wobei `_dist_thresh = SHOT_RANGE` bei
  freier LOS, sonst `optimal_range × 1.5`. Ist `_skip_nav` True → nav_path ignorieren, Direktanflug.
- `_skip_nav` wird **vor** dem Replan bestimmt: im Direktmodus läuft **keine** A\*-Planung
  (`_plan_path` würde sonst ungenutzte Pfade berechnen und „Pfad: N WPs"-Logs erzeugen). Dabei
  werden `_nav_path`/`_nav_goal` invalidiert, sodass beim Verlassen des Direktmodus frisch geplant
  wird (kein Folgen veralteter Pfade). `_too_high` (Gegner ≥ `max_jump_h` höher) impliziert
  `_not_below_enemy` False → `_skip_nav` False, kollidiert also nie mit dem Eskalations-Zweig.
- **Distanz-Deadzone**: die distanzbasierte Vor-/Zurück-Regelung im Direktmodus hat ein neutrales
  Band `±COMBAT_DIST_DEADZONE` um die Optimaldistanz (Speed 0). Ohne dieses Band kippt der Speed bei
  exakt Optimaldistanz zwischen Rückwärts (0,5×) und Langsam-Vorwärts (0,15×) — zwei distanzgleiche
  Bots zittern dann sichtbar umeinander.

**COMBAT-Stall-Watchdog (`_stall_watchdog`/`_stall_maneuver_tick`)**: Zwei gleich starke Bots frieren
bei Optimaldistanz ohne Sicht und ohne Abpraller-Schuss ein (Spiegel-Stall, typisch an der dünnen
diagonalen Trennwand der z=15-Plattformen): Feuer wird gehalten, `_skip_nav` bleibt True, und die
proaktive Wand-Vorausschau `_steep_wall_ahead` greift dort nicht (Wand > 20u Probe entfernt bzw.
flacher Einfallswinkel an der Diagonalen → korrektes `None`). Der Watchdog läuft nur im sichtlosen
Direktsteuerungs-Zweig: er armiert ein **randomisiertes** Fenster (`COMBAT_STALL_WIN_MIN..MAX`), misst
die Netto-Bewegung ab dem Anker und startet bei < `COMBAT_STALL_MIN_DIST` ein zufällig gewähltes
Unstick-Manöver — **REV** (Rückwärtsstück `COMBAT_STALL_REV_MIN..MAX`, **bewusst ohne Klippen-Guard**:
ein Sturz von der Plattform löst den Stall) oder **PATH** (A\*-Pfad zu einem Zufallspunkt
`NAV_CELL_SIZE × COMBAT_STALL_WP_MIN..MAX` entfernt; scheitert die Planung, fällt es auf REV zurück).
Jedes Manöver ist per `COMBAT_STALL_TIMEOUT` gedeckelt; danach re-armiert der Watchdog frisch. Die
Randomisierung ist essenziell: mit festen Timern würden sich zwei deterministische Bots spiegeln und
synchron erneut festfahren. Der Episode-Zustand liegt in den `_stall_*`-Feldern (Muster analog den
`_unreach_*`-Feldern der Eskalation), **kein** eigener `AIState` — LANDING_SHOT/EVADING/DODGE_JUMP
sind eigene States, die `_execute_combat_move` gar nicht erreichen.

**Eskalation bei unerreichbar hohem Gegner (`_combat_escalate`, P3-NAV-12)**: Steht der Gegner
per Sprung unerreichbar hoch (`enemy_z - bot_z ≥ _effective_jump_height()` — WG/LG-bewusst, siehe
Helfer-Tabelle) und findet A\* keinen Pfad, fährt der Bot **nicht**
blind in die Wand. Stattdessen läuft ein wiederholender Zyklus mit Früh-Ausstieg, gebunden an
`_unreach_target`:
1. **Re-Target** (`_unreach_phase 0`): aktuellen Gegner kurz meiden (`_combat_avoid`, weiche
   Score-Penalty `UNREACH_AVOID_PENALTY` in `_find_target_player` — kein Hard-Skip), anderes
   erreichbares Ziel wählen.
2. **Direktmodus** (Phase 1): `UNREACH_DIRECT_TIME=30s` aggressiv direkt fahren, dabei alle
   `COMBAT_REPLAN_RETRY=1s` ein Hintergrund-Replan zum Gegner — sobald ein Pfad da ist, sofort
   raus (navigieren).
3. **Reposition** (Phase 2/3): Pfad zu einem Punkt ~`UNREACH_REPOS_RADIUS=100u` (grob Richtung
   Gegner) für einen frischen A\*-Start, abgefahren via `_navigate_wp`.
4. **Replan** zum Gegner; sonst Zyklus von vorn.

Während einer Episode ist der Top-of-Tick-Replan in `_execute_combat_move` ausgesetzt
(`_stuck_active`), sonst würde `replan_xy` den Reposition-Pfad überschreiben. Sinkt der Gegner
unter die Sprunghöhe, endet die Episode sofort. Zeitbasis durchgehend `time.monotonic()`.

**Refactoring-Falle: `_apply_movement_caps()` und Azimuth-Ordering**

`_apply_movement_caps()` clampt `ang_vel` — aber `self.azimuth` wird in
`_navigate_wp()` **im selben Block vorher** direkt gesetzt. Der Azimuth-Cap greift
nicht nachträglich: Der Tank hat sich bereits physikalisch in die verbotene Richtung
gedreht. Für LT/RT-Flags muss deshalb `diff` (Winkeldifferenz zum Ziel) vor der
`self.azimuth`-Zuweisung geclampt werden. Gilt für beide Pfade (normal + reverse).
`_apply_movement_caps()` am Ende bleibt für Speed-Caps (RO/FO) nötig.

### Typische Debugging-Schritte für Pathfinding-Probleme

1. **`[PTH] NavGraph: N Etagen, M begehbare Zellen`**: N sollte > 1 sein (Boden + Dächer).
   M >> 0 ist nötig. N=1, M=kleine Zahl = NavGraph mit falschem world_half gebaut.

2. **`[PTH] Kein Pfad: Start non-walkable+isolated`**: Bot steht außerhalb des NavGraph-
   Bereichs (world_half zu klein) oder tief in einem Gebäude. Prüfe `world_half` in den Logs.

3. **`[DEBUG_PATH] 0 WPs`**: A* gibt leere Liste zurück. Ursachen: Start oder Ziel
   non-walkable, Karte komplett isoliert (alle Paths blockiert).

4. **NAV_JUMP wird nie ausgelöst**: Sprung-Kanten fehlen. Prüfe `_vertical_neighbors`
   Logs, prüfe ob Dach-Layer mit `MAX_ROOF_H` zu niedrig ausgeschlossen wird.

5. **Bot bricht Route nach einem Schritt ab**: FOV-Check greift direkt. Prüfe ob der erste
   WP hinter dem Bot liegt (möglicher NavGraph-Fehler) oder ob `_nav_path_fresh = False`
   zu früh gesetzt wird.

6. **Bot fährt wiederholt gegen Wand in der Mitte einer schmalen Plattform**: Thin-Wall-Bug
   (s. u.). Prüfe ob `[PTH] thin_blocked: N verbotene Wegpunkt-Paare vorberechnet` im Log
   erscheint und ob das Obstacle wirklich dünn ist: `min(half_w, half_d) * 2 < CELL_SIZE = 4u`.

### Wände durch Dachflächen, `_obstacle_blocks_layer` und `_thin_blocked`

**Eigentliche Ursache von "Bot fährt durch eine Wand auf einer Plattform"**: Eine Wand,
die **unter** einer Dachfläche beginnt und durch die Tank-Höhe nach oben stößt, muss die
Etage sperren — der Tank steckt sonst im Wand-Körper. Die HIX-Diagonalwände haben z.B.
`bottom_z=14, height=16` (z=14…30): Auf dem z=15-Dach steckt der Tank (z=15…17) mitten
in der Wand, auf z=30 fährt er über die Wand drüber (Oberkante == Dachhöhe).

Die alte Blockierbedingung in `_build_roof_layers` (`obs2.bottom_z >= roof_z - 0.1 …`)
erfasste **nur Aufbauten, die auf Dachhöhe beginnen**. Wände, die 1u darunter beginnen,
wurden übersehen → ihre Dachzellen blieben begehbar → A* fuhr frei hindurch.

**Die Lösung — `_obstacle_blocks_layer(obs, layer_z)`**: Ein einziger vertikaler
Überlappungstest, der an **allen drei** Stellen verwendet wird (`_build_ground_layer`,
`_build_roof_layers`, `_precompute_thin_wall_blocked`):

```python
obs.bottom_z < layer_z + TANK_HEIGHT and obs.bottom_z + obs.height > layer_z + 0.1
```

Das `+0.1` schließt das Dach-erzeugende Obstacle (Oberkante == `layer_z`) und Wände, deren
Oberkante genau auf Etagenhöhe endet (Tank fährt drüber), korrekt aus.

> **Kritische Invariante**: Build und Precompute **müssen** denselben Filter benutzen.
> Driften sie auseinander (z.B. Precompute betrachtet eine Wand auf einer Etage, auf der
> der Grid sie gar nicht blockiert), erzeugt `_thin_blocked` Paare für Kanten, die A*
> dringend braucht → bis zu ~75% der Etagen-Kanten fallen weg → 10.000-Knoten-
> Expansionslimit. Genau dieser Fehler ist beim ersten Lösungsversuch passiert.

**Seitlich befahrbar — `THIN_WALL_MARGIN` (`_margin_for`), Ebenen-Split**: Volle
`TANK_MARGIN = 3.5u` auf beiden Seiten einer mittig auf einem schmalen Laufsteg liegenden
Wand ließe keinen Platz → der Laufsteg verschwände komplett. Dünne Wände
(`min(half_w, half_d) * 2 < CELL_SIZE`) bekommen daher beim `_mark_blocked` den reduzierten
`THIN_WALL_MARGIN = 1.4u` (= Tank-Halbbreite). Damit überlebt links/rechts der Wand ein
befahrbarer Streifen (bewusst engerer Sicherheitsabstand, akzeptiertes Rest-Kollisionsrisiko).

`_margin_for(obs, layer_z)` ist **layer-bewusst**: Der reduzierte Margin gilt **nur auf
Dach-/Plattform-Layern** (`layer_z > 0.5`). Auf dem **Boden-Layer** (z=0) bekommen auch
dünne Obstacles den vollen `TANK_MARGIN` — dort ist meist Platz, und der reduzierte Rand ließ
den Bot an den kleinen Kreuz-Obstacles auf z=0 hängen bleiben bzw. kurz gegen Wände fahren.
Dicke Obstacles behalten überall `TANK_MARGIN`.

**`_thin_blocked` + Glättung + A*-Guard**: Durch den reduzierten Margin liegen jetzt
**benachbarte** begehbare Zellen links/rechts der Wand — eine Diagonale zwischen ihnen
würde die Wand schneiden. `_precompute_thin_wall_blocked()` berechnet beim Build alle
solchen verbotenen Zellpaare (2D-Slab-Test `_segment_crosses_thin_obs`) und legt sie als
`(wx1, wy1, wx2, wy2, layer_z)` in beide Richtungen in `self._thin_blocked` ab. Zwei
O(1)-Absicherungen nutzen das Set:
- **A*-Kantengenerierung** (`_astar`): horizontale Nachbarkante wird verworfen, wenn
  `(wx, wy, nwx, nwy, layer.z)` in `_thin_blocked` liegt → der Rohpfad kreuzt keine Wand.
- **`_smooth_path`**: vor dem Entfernen eines Wegpunkts B wird geprüft, ob `result[-1]` →
  `nxt` ein verbotenes Paar ist (verhindert Shortcuts über das Wandende hinweg).

`_smooth_path` ist korrekt implementiert: `prev` für den Guard ist `result[-1]` (zuletzt
behaltener Punkt), nicht `waypoints[i-1]`.

**Log bei Verbindungsaufbau**: `[PTH] thin_blocked: N verbotene Wegpunkt-Paare vorberechnet`.
Fehlt diese Zeile auf einer Karte mit dünnen Wänden (z.B. HIX), ist entweder kein Obstacle
als dünn klassifiziert oder der NavGraph stammt aus dem Cache eines alten Builds.

---

## 6. Kollisionssystem

### `_apply_obstacle_bounds(dt)` — bot/ai/physics.py

Wird in `_apply_bounds()` aufgerufen, bevor `pos` aktualisiert wird.

```
Für jedes Gebäude auf der aktuellen Fahrebene:
  1. Decken-Kollision (Z-Achse): pz + TANK_HEIGHT > bottom_z + height?
     → vel[2] = 0, pos[2] = roof_z - TANK_HEIGHT (Decken-Stop)
  2. Wand-Kollision (XY): neue Position im lokalen Koordinatensystem prüfen
     → Überlapp berechnen, nur Normalkomponente nullen (Wall-Sliding)
```

**Decken-Kollision** prüft ob der Kopf des Tanks (`pos[2] + TANK_HEIGHT`) höher als
der Boden des Gebäudes wäre. Bedingung: `pz < bottom_z <= pz + TANK_HEIGHT`. Das stellt
sicher dass Boden-Level-Gebäude nur Tanks stoppen die unterhalb hineindringen, nicht
Tanks die darüberfahren.

**Wall-Sliding** (nicht-bouncy Wandkollision): Das rotierte Koordinatensystem des
Gebäudes wird genutzt um die Normalrichtung der nächsten Wand zu berechnen. Nur der
Geschwindigkeitsanteil senkrecht zur Wand wird genullt — der Anteil parallel zur Wand
bleibt erhalten. Das ermöglicht das "Entlangfahren" an Wänden.

**Einheitliches OBB-Form-Modell (Tank als orientierte Box).** Der Überlapp-Gate von Wand-
und Decken-Kollision UND `_is_inside_obstacle` nutzen dieselbe Primitive
`rect_rect_overlap` (`bzflag/intersect.py`, Port von bzfs `testRectRect`/`testOrigRectRect`).
**Warum:** der Tank ist mit `TANK_LENGTH=6.0` deutlich **länger** als dünne Wände dick sind
(z.B. HIX-Trennwand 1u). Der frühere Kreis-Test (Radius = Halb-*Breite* 1,4u) ließ die lange
Tank-Achse durch dünne, oft gedrehte (135°) Wände **ragen** — Folge: der Tank wurde durch die
Wand beschossen und schien „durch die Wand zu zielen". Der OBB-Gate hält die ganze orientierte
Box draußen (senkrecht an eine dünne Wand → Zentrum bleibt ~Halb-Länge 3,0 + Wand-Halb-Breite
draußen). Die **Glide-Achsen-Wahl** bleibt bewusst isotrop (Trennachse aus der Obstacle-Geometrie,
nicht aus der Tank-Orientierung) — nur der *Gate* wurde OBB, damit lange Tanks nicht an kleinen
Hindernissen (z.B. Teleporter-Posts) in die falsche Achse gleiten. Maße: Kollision/Innen nutzen
den **physischen** Tank (`_tank_length/2`, `_effective_half_width()`), die Treffer-Hitbox
(`_segment_hits_obb_3d`, `_hitbox_half_dims`) zusätzlich `+_shot_radius` — bewusst, keine
Inkonsistenz. **Pfadplanung** (NavGraph-Clearance) bleibt bewusst grob und nutzt das OBB NICHT.

### `_can_drive_through_obstacles()` — wann Kollision deaktiviert ist

```python
def _can_drive_through_obstacles(self) -> bool:
    return self.own_flag in ("OO",)    # Oscillation Overthruster
```

Mit OO-Flag fährt der Bot durch Gebäude. `_apply_obstacle_bounds` wird dann nicht
aufgerufen. Das ist also die Ausnahme, nicht die Regel.

Zusätzlich gibt es `_is_inside_obstacle()`: Prüft per OBB-Overlap (`rect_rect_overlap`,
einheitliches Form-Modell), ob **irgendein Teil** der Tank-Box in einem Gebäude steckt (nicht
mehr nur das Zentrum). Nutzer: Teleport-Exit-Revert (`_check_teleport_crossing`), OO-Dodge-Gate,
Debug-Logging. Mit der OBB-Wandkollision ist das im Normalbetrieb nie wahr (Berühren zählt strikt
nicht) → nur nach Teleport/Spawn/Durchdringung. Gibt `False` zurück wenn OO aktiv (ohne `include_oo`).

### Wichtige Größen

| Variable | Wert | Bedeutung |
|----------|------|-----------|
| `TANK_HEIGHT` | 2.05u | Tankhöhe für Decken-Check |
| `TANK_WIDTH` | 2.8u | Tankbreite (Halb-Breite = OBB-Querachse) |
| `TANK_LENGTH` | 6.0u | Tanklänge (Halb-Länge = OBB-Längsachse, verhindert Nase-durch-dünne-Wand) |
| `_effective_half_width()` | Normal=1.4u, T=0.56u, N=0.3u | Flag-abhängige Kollisionsbreite |
| `GRID_PAD` | `0.5 + hypot(HL,HW) + 0.01` ≈ 3,82u | Broad-Phase-Polster ≥ Tank-Eck-Radius, sonst verpasst die Grid-Query eine Wand, die die Tank-Nase erreicht |

### Broad-Phase-Grids (ObstacleGrid)

Alle heißen Geometrie-Abfragen laufen über `bzflag/obstacle_grid.py` (W6-Split aus
nav_graph.py, damit auch shot_physics es ohne Importzyklus nutzen kann). Ein
`ObstacleGrid` ist ein 2D-Zellen-Index über die Obstacle-AABBs; drei Instanzen:

| Grid | Abfrage | Nutzer |
|---|---|---|
| Solid-Grid | Zellen-Lookup | `_get_floor_z`, `_apply_obstacle_bounds` (Kollision) |
| LoS-Grid | `query_ray` (DDA, Amanatides-Woo) | `_segment_clear` → alle LoS-/Sicht-Checks |
| Shot-Grid | `query_ray` pro Bounce | `simulate_shot_path` (`obs_grid`-Parameter, P1) |

Kerninvariante: Das Grid verkleinert nur die **Kandidatenmenge** — die Narrow-Phase
(exakte Ray-Tests, min-über-Kandidaten) bleibt unverändert, das Ergebnis ist deshalb
exakt identisch zum linearen Scan. Das Zellen-`pad` garantiert, dass kein Kandidat
verloren geht (keine False Negatives); abgesichert durch Äquivalenztests
(`TestGetFloorZGridEquivalence`, `TestLosRayGridEquivalence`, P1-Fuzz „0 Mismatch").
Ohne Grid (Tests, Fallback) läuft überall weiterhin der lineare Pfad.

---

## 7. Hit-Detection

### Konzept: Client-seitig, Opfer meldet Tod

BZFlag-Protokoll: Das getroffene Opfer berechnet selbst ob ein Schuss trifft und sendet
dann `MsgKilled`. Der Server vertraut dieser Meldung und broadcastet den Tod an alle.

Das bedeutet: Ein Bot der `_resolve_incoming_shots` nie aufruft, stirbt nie — er kann dauerhaft
getroffen werden ohne es zu merken. Ebenso kann ein falsches `_resolve_incoming_shots` den Bot
unbeabsichtigt sterben lassen.

**Leerlauf und Lock-Disziplin:** `_resolve_incoming_shots`, `_cleanup_shots` und
`_find_incoming_shot` steigen bei komplett leeren Shot-Dicts sofort aus (N2 — der Check
ist ein GIL-sicherer Lesezugriff, dafür braucht es kein Lock; 60 Hz × leere Iteration
plus Lock-Overhead waren messbarer Idle-Posten). Und `_on_shot_begin` führt die
Schusspfad-Simulation (`simulate_shot_path`, bis zu 100 Bounces) **vor** dem
`_shots_lock` aus (P3): sie liest nur unveränderliche Weltdaten; gelockt wird nur das
gemeinsame Eintragen von Shot + Ricochet-Pfad (Atomicität bleibt, Haltezeit µs statt ms —
sonst blockiert der Recv-Thread die 60-Hz-Schleife bei Schuss-Bursts).

### Client-treue Nachjustierung eingehender Schüsse (AdVel/AdLife)

`MsgShotBegin` transportiert **Basis**-Velocity und **Basis**-Lifetime; der echte Client
multipliziert beide lokal im Strategy-Konstruktor nach (`SegmentedShotStrategy.cxx`:
`f.shot.vel[i] *= <flag>AdVel` — der ganze Vektor inkl. Tank-Anteil — und
`f.lifetime *= <flag>AdLife`). Der Bot spiegelt das in `_on_shot_begin` direkt nach dem
Parsen über zwei Helfer, bevor Shot-Objekt, Pfad-Simulation und Sofort-Check die Werte sehen:

| Helfer | Flaggen | Multiplikatoren (Defaults) |
|---|---|---|
| `_incoming_shot_velocity` | L, TH, MG, F | ×1000 / ×8 / ×1,5 / ×1,5 |
| `_incoming_shot_lifetime` | SW, GM, MG, F, L, TH | `_shockAdLife` usw.; L ×0,1, TH ×0,05 |

GM bleibt bei beidem außen vor bzw. nur AdLife: die Rakete hat kein AdVel und wird live
über `MsgGMUpdate` nachgeführt. Ohne diese Nachjustierung wäre die simulierte
Laser-Reichweite 100× zu kurz (350u statt 35 000u) — der Sofort-Check sah dann nur
Segmente für Direkttreffer bzw. einen nahen Abpraller, Mehrfach-Abpraller gingen wirkungslos
durch den Bot (historischer Bug; Regression: `test_laser_multibounce.py`).

### Schuss-Segment und Normschuss

Jeder Schuss hat eine `position_at(t)` Methode:

```python
def position_at(self, t):
    dt = t - self.fire_time
    return (pos[0] + vel[0]*dt, pos[1] + vel[1]*dt, pos[2] + vel[2]*dt)
```

Für den normalen Treffer-Check wird ein Segment `[A, B]` über das **Prüf-Fenster** definiert:
- `A = shot.position_at(prev_t)` mit `prev_t = max(fire_time, win_start)`
- `B = shot.position_at(now)`

`win_start = min(_last_hit_check_t, now − dt)`: mindestens der aktuelle Tick, zusätzlich
alles seit dem letzten `_resolve_incoming_shots`-Lauf. Die Abdeckung hängt damit **nicht**
am 0,1-s-Stall-Clamp der Hauptschleife — auch ein langer Tick (GC, Container-Scheduling,
historisch: synchrone A*-Freezes) verliert keinen Schusspfad; Überlappung ist harmlos
(idempotenter Segment-Test), Lücken nicht. Beim Spawn wird die Referenz zurückgesetzt
(`_on_alive`), sonst würde das Totliege-Fenster gegen die neue Position getestet
(Geister-Treffer beim Einspawnen).

**Relativ-Sweep (Eigenbewegung):** Der Startpunkt `A` wird um die eigene Bewegung seit
`prev_t` in den Tank-Frame von `now` verschoben (Client-Äquivalent: `relativeRay` in
`checkHit`) — ein Tank, der während des Fensters selbst durch die Schussbahn fährt, wird
getroffen. Guard: unplausibel großer eigener Sprung (Teleport; `> 2·tank_speed·Fenster + 5`)
→ Korrektur entfällt, statischer Test.

Getestet wird das Segment gegen die Tank-**OBB** (`_hitbox_half_dims`: Tank-Halbmaße +
`_shotRadius`-Aufschlag je Dimension; Flaggen-Skalierung O/T/TH, N-Sonderfall) via
`_segment_hits_obb_3d` — geometrisch eine Kapsel mit Radius `_shotRadius` um die Flugbahn.
Die OBB rekonstruiert BZFlags `checkHit`-Verhalten: Breitphasen-AABB-Gate (echte
Panzer-Maße, erklärt BU-Immunität und laterale Enge) vor der `0.99·tankRadius`-Kugel.

Die Kugel-Variante (`_segment_point_dist3d < _effective_hit_radius`) nutzt nur noch der
GM-Zweig. `_effective_hit_radius` ist `TANK_RADIUS * scale * 0.99`:
- Normal: `4.32 * 1.0 * 0.99 = 4.28u`
- Tiny (T): `4.32 * 0.4 * 0.99 = 1.71u`
- Obesity (O): `4.32 * 2.5 * 0.99 = 10.69u`

Der `0.99`-Faktor ist aus `SegmentedShotStrategy.cxx` von BZFlag — die originale
Implementierung verwendet exakt diesen Wert.

### N-Flag (Narrow): OBB statt Kugel

```python
# bot/hit_detection.py, _resolve_incoming_shots:
if self.own_flag == "N":
    hit = _segment_hits_obb_3d(A, B,
        center=tank_center,
        half_len=3.5,   # Längsachse (Tank ist lang und schmal)
        half_w=1.0,     # Querachse (schmal durch N-Flag)
        half_h=1.5,
        angle=self.azimuth
    )
```

Mit N-Flag ist der Tank schmal. Die Kugel-Approximation mit kleinem Radius reicht
nicht — ein Schuss von vorne/hinten würde den Tank fast nie treffen. Die OBB bildet
die tatsächliche Tank-Form besser ab: lang (3.5u), schmal (1.0u), normalhohe (1.5u).

### Shot-Typen und ihre Besonderheiten

**Shockwave (SW):**
- Kein normales Segment-Tracking
- Die "Welle" expandiert mit 60u/s: innere Grenze=6u, äußere Grenze=6+elapsed×60u
- Hit wenn `SHOCK_IN < dist < outer_radius` zum Abschuss-Zeitpunkt
- Zusätzlicher Sofort-Check bei `MsgShotBegin` (nötig falls Bot im Radius beim Abschuss)

**Guided Missile (GM):**
- Homing-Simulation: dreht sich mit `GM_TURN_RATE=0.628319 rad/s` pro Sekunde
- Richtung wird bei `MsgGMUpdate` aktualisiert — KEIN periodisches Update in der Simulation
- Zwischen zwei MsgGMUpdate-Paketen fährt das GM geradeaus; wenn Update-Pakete fehlen,
  kennt der Bot das neue Ziel nicht
- **Wand-Occlusion (Sicherheitsnetz):** die per-Tick-Homing-Simulation hat KEINEN Segment-
  Cache (der wäre für eine gelenkte Rakete falsch) und kennt daher keine Wände. Der Treffer-
  Check zählt deshalb nur, wenn `_segment_clear(Rakete → Tank)` frei ist — sonst würde die
  Rakete durch eine solide Wand „treffen" (im Rennfenster, bevor der Server das Schussende
  meldet). Rundet die Rakete die Wand später, greift der Treffer korrekt. Ohne NavGraph liefert
  `_segment_clear` `True` → unverändertes Verhalten.

**Laser (L):**
- **Instant** (bewusste Vereinfachung, kein Bug → BUGS/FSD P3-SHT-06): Sofort-Check bei
  `MsgShotBegin` über die volle Distanz; abgeprallter Laser prüft alle Ricochet-Segmente
- Segment-Prüfung: `A = laser_origin`, `B = laser_origin + direction × range`

**Super Bullet (SB):**
- Geometrisch identisch zu Normal (Client: `SuperBulletStrategy` erbt `checkHit`
  unverändert), aber `makeSegments(Through)`: Wände und Weltgrenzen stoppen den Schuss
  nicht — **Teleporter greifen trotzdem** (der Teleporter-Lookup in `makeSegments` läuft
  unabhängig vom ObstacleEffect; live verifiziert). Auf Teleporter-Karten laufen SB- und
  PZ-Schüsse deshalb durch `simulate_shot_path(..., phase_walls=True)` und landen im
  Segment-Cache (`_ricochet_paths`) — ohne Teleporter bleibt SB der gerade else-Zweig.
- **Längskapsel (bewusste, kleine Abweichung vom Client):** Das getestete Segment wird
  beidseitig um `_shotRadius` verlängert (`_extend_segment`) → Längsreichweite
  vorn/hinten `2×_shotRadius` (Default 1,0u), seitlich/vertikal unverändert. Bildet die
  optische Bolt-Länge ab; der away-skip bleibt aktiv (vorbei/wegteleportiert = weg).

**Thief (TH):**
- **Instant** wie Laser; Sofort-Check bei `MsgShotBegin` (direkt + Ricochet-Segmente)
- **Kein Kill** — ein TH-Treffer auf den Bot löst **Flaggendiebstahl** aus: hält der Bot eine
  Flagge, sendet er `MsgTransferFlag(player_id, shooter)` und verliert sie; ohne Flagge passiert
  nichts. `is_thief`-Schüsse werden in `_resolve_incoming_shots` ohne Kill verworfen.

**PhantomZone (PZ) — Phantom-Schüsse:**
- Wire-Flag „PZ" = der Schütze war beim Feuern **gezoned** (sein Client nullt das Flag sonst,
  `ShotPath.cxx:46`). Phantom-Schüsse phasen durch Wände, treffen aber **nur ebenfalls gezonede
  Ziele** (`LocalPlayer::checkHit`: „zoned shots only kill zoned tanks").
- Der Bot zoned sich nie selbst (P4-FLG-03 offen) → `_phantom_shot_harmless()` filtert PZ-Schüsse
  in `_resolve_incoming_shots` und `_find_incoming_shot` (direkt + Phase-Pfad-Cache): kein Treffer,
  kein Ausweichen. Der Schuss bleibt bis zum Ablauf in `_shots` (MsgShotEnd-Buchhaltung). Bei einer
  FLG-03-Umsetzung (Selbst-Zoning) muss der Filter den eigenen Zoned-Status prüfen.

**Steamroller (SR) — `_check_steamroller()`:**
- Proximity-Check, kein Segment; 3D-Abstand mit doppelter Z-Gewichtung:
  `sqrt(hypot(dx,dy)² + (2·dz)²) < TANK_RADIUS × (1 + SR_RADIUS_MULT)` (= 3× ≈ 12,96u)
- Greift, wenn der **andere** Spieler SR trägt — **oder** wenn der **Bot selbst BU** trägt
  (eingegrabener Tank wird von **jedem** Tank überrollt, nicht nur von SR).
- Nutzt `last_seen < 1.0s` (sonst Position veraltet).

**Guided Missile (GM) — zwei Pfade:**
- Per-Tick-Homing-Simulation in `_resolve_incoming_shots` (s.o.).
- **Zusätzlich** direkter Treffer-Check in `_on_gm_update`: kommt ein `MsgGMUpdate` mit
  `dist3d < HIT_RADIUS`, gilt der Bot sofort als getroffen.

**Eingegrabener BU-Bot (`pos[2] < 0`):** In `_resolve_incoming_shots`/`_find_incoming_shot` treffen ihn
**nur SW und GM** — normale Schüsse werden übersprungen.

**Shield (SH):** Ist beim Treffer SH aktiv, **stirbt der Bot nicht**, sondern droppt die Flagge
(`_try_drop_flag`) und überlebt (`_resolve_incoming_shots`, vor `_report_killed`).

**Past-closest-approach-Skip:** Bewegt sich ein nicht-ricochierender Fremdschuss bereits vom Bot
**weg** (`shot.vel · (B − tank) > 0`), wird er übersprungen — bleibt aber in `_shots`, falls er als
Ricochet zurückkommt.

### Eigene Schüsse und Ricochet-Eigentreffer

**Eigentreffer-Schutz** erfordert Guards in jedem spezialisierten Branch, nicht nur im
allgemeinen:

```python
# In _resolve_incoming_shots() — jeder spezialisierte Branch braucht eigenen Guard:
if shot.is_sw:
    if shot.shooter_id == self.player_id:
        continue     # eigene SW → nie Selbsttreffer
    ...
if shot.is_gm:
    if shot.shooter_id == self.player_id:
        continue     # eigene GM → nie Selbsttreffer
    ...
# Allgemeiner Non-Ricochet-Branch NACHHER:
if not shot.can_ricochet and shot.shooter_id == self.player_id:
    continue
```

SW und GM werden vor dem allgemeinen Guard abgefangen — ohne eigene Guards würden
eigene SW/GM-Schüsse den Bot töten.

**Ricochet-Eigentreffer**: Eigene Schüsse mit Ricochet-Flag (`shot.can_ricochet`)
können nach dem Abprallen auf den eigenen Bot zurückfliegen und sollen als Hit gelten.
`simulate_shot_path()` aus `bzflag/shot_physics.py` berechnet alle Bounce-Segmente.
Segment 0 (direkter Weg vor Bounce) wird für eigene Schüsse übersprungen; Segmente 1+
(nach mindestens einem Bounce) werden normal geprüft.

---

## 8. Schieß-Dispatcher und Aim-Berechnung

### Dispatcher-Pattern: `_maybe_shoot()`

`_maybe_shoot()` delegiert an waffentypische Submethoden:

```python
if self.own_flag == "TR" and self._can_shoot(): self._maybe_shoot_tr(now); return
if not self._can_shoot():                        return
if self.own_flag == "OO" and inside_obstacle:    return   # OO: kein Schuss im Gebäude
if now < self._next_shoot or not self._next_slot_ready(now): return
if self._ai_state in (Z_ATTACK, LANDING_SHOT):   return   # eigene Feuerlogik
if target and human_count > 0 and (ep := enemy_pos):
    pz_active = info.is_phantom_zoned and own_flag not in ("SW","SB")
    if not pz_active:
        if   own_flag == "SW": self._maybe_shoot_sw(...)
        elif own_flag == "GM": self._maybe_shoot_gm(...)
        elif own_flag == "SB": self._maybe_shoot_sb(...)
        elif own_flag == "L":  self._maybe_shoot_l(...)
        elif own_flag == "TH": self._maybe_shoot_th(...)
        else:                  self._maybe_shoot_standard(...)
        return
    # pz_active: Fall-through → Random-Schuss (PZ-Gegner nur per SW/SB sicher treffbar)
# kein Ziel / pz_active → Random-Schuss, aber NICHT bei good_flags/limited_flags
```

**PZ-aware:** Gegen einen PhantomZone-Gegner (`is_phantom_zoned`) bringt ein gezielter Schuss
nichts (er ist nur per SW/SB treffbar). Mit SW/SB feuert der Dispatcher normal, sonst fällt er
zu Random-Schüssen durch (Druck/Verwirrung).

Warum separate Methoden statt einer einzelnen Dispatch-Funktion:

- **TR (Rapid Fire)**: Sonderfall — wird vor allen anderen Checks behandelt (kein
  Random-Delay, kein Z-Block, kein `target_player`-Check). TR schießt wann immer
  ein Slot geladen ist.
- **SW (Shockwave)**: Kein Ziel nötig (Flächenwaffe), kein Z-Block, eigene
  Radius-Logik (`_maybe_shoot_sw` berechnet Gegner-in-Radius selbst).
- **GM (Guided Missile)**: Kein Z-Block (GM lenkt dem Ziel nach); schickt
  `MsgGMUpdate` nach Abschuss (→ Sektion 10, GM-Targeting-Flow).
- **L (Laser)**: **instant** (kein Lead); schärferer Winkel-Schwellwert (5° statt 25°);
  Z-Block bei `|dz| > TANK_HEIGHT × 0.7 ≈ 1.4u`.
- **TH (Thief)**: **instant**; nur `dist ≤ 120u`; 10°-Schwelle; Ricochet-Aim wenn kein LoS;
  Treffer = Diebstahl, kein Kill (→ §7).
- **SB (Super Bullet)**: schießt **durch Wände** (kein LoS-Check), 25°-Schwelle; eigener Z-/
  Warnschuss-Pfad wie Standard.
- **Standard**: Z-Block, 25°-Winkel-Check, Random-/Warnschuss-Fallback wenn kein Ziel/LoS.

**Z-Achsen-Block**: Standard unterdrückt bei `|dz| > HIT_RADIUS ≈ 5.6u`, Laser/TH strenger bei
`|dz| > TANK_HEIGHT × 0.7 ≈ 1.4u`. GM und SW überspringen den Check — GM weil es nachlenkt,
SW weil es sich radial ausdehnt. **LANDING_SHOT ist ebenfalls ausgenommen**, feuert aber nicht
über diesen Pfad: der Schuss kommt aus `_tick_landing_shot` (s. §8, „Gefeuert wird aus dem
eigenen Tick"), der gezielt auf die Boden-/Mündungshöhe des fallenden Gegners abgibt.

**Z-Angriffs-Sprung (ZJ1) — Feuerhöhe relativ vs. absolut**: `_check_z_attack_jump` rechnet die
Feuerhöhe `fire_rel = min(z_diff + 1.0, max_jump_h − 0.5)` **relativ** zum Absprungpunkt
(`z_diff = enemy_z − bot_z`); die Kinematik (`disc = v0² − 2·g·fire_rel`,
`t_fire = (v0 − √disc)/g`) ist relativ zum Absprung. Im Tick (`_tick_z_attack`) wird dagegen gegen
den **absoluten** `pos[2]` verglichen — daher speichert `_check_z_attack_jump`
`_z_attack_fire_z = bot_z + fire_rel` **absolut**. Beim Absprung vom Boden (`bot_z = 0`) fallen
beide zusammen; von erhöhten Plattformen **nicht** — die Vermischung war ZJ-02 (Bot sprang, feuerte
aber nie). `_z_attack_feasible` nutzt denselben relativen `fire_rel` für die Machbarkeit.

### Warnschuss-Mechanik (`_maybe_shoot_standard` / `_maybe_shoot_sb`)

Wenn kein LoS besteht **oder** der Z-Unterschied in der ZJ1-Zone liegt (`HIT_RADIUS < dz <
max_jump_h`), feuert der Bot keinen sicheren gezielten Schuss. Statt gar nichts zu tun, gibt er
mit gewisser Wahrscheinlichkeit einen **Druck-/Warnschuss** ab:
- kein LoS: 15 % (sofern kein Ricochet-Aim möglich), Z-Differenz: 30 %.
- Bei `_max_shots > 1` wird der **letzte freie Slot** reserviert (nicht für Warnschuss verbraucht),
  damit ein gezielter Schuss möglich bleibt.
- Nach einem Warnschuss wird **voller Reload** gesetzt (`_next_shoot = now + reload`), kein Burst.
- `dz ≥ max_jump_h` (unerreichbar hoch) → gar kein Schuss.

### Slot-/Burst-Reload-Modell

BZFlag erlaubt `_maxShots` gleichzeitige Schüsse. Der Bot trackt pro Slot die Reload-Zeit in
`_slot_reload_at[]`:
- `_next_slot_ready(now)`: ist der **nächste** Slot (zyklisch) reloadet?
- Nach einem Schuss setzt `_set_next_shoot_after_fire` `_next_shoot` auf das Minimum aus Reload
  und Burst-Intervall (`MIN_BURST_INTERVAL = 1s`, GM `GM_BURST_INTERVAL = 2s`) — so kann der Bot
  bei mehreren Slots schnelle Bursts feuern, ohne den Server-Slot-Check zu verletzen.
- `_send_shot` belegt den Slot (`_slot_reload_at[slot] = now + effective_reload`).

### Genocide-Flagge (G)

`_genocide_multikill_possible()` prüft, ob ein Feind-Team mehr als einen lebenden Spieler hat —
nur dann lohnt G und wird behalten (sonst Drop in der Game-Loop). Stirbt ein **Teamkollege** mit
G-Flagge, stirbt der Bot mit (Genocide-Propagation in `_on_killed`, eigenes `MsgKilled`
reason=GotGenocided).

### TH-Flagge: Ricochet-Aim-Vorberechnung

`_maybe_shoot_th()` ist der Dispatcher für die Thief-Flagge (TH). TH-Schüsse prallen
ab — der Bot berechnet einen Zielwinkel via `_compute_ricochet_aim()`:

```
_compute_ricochet_aim(now, ep, dist)
  └── _rico_aim_cache: {target_id: (azimuth, expires)} — ~1s Gültigkeit
  └── _find_ricochet_aim_angle(ep)
        └── brute-force Scan: az_deg in range(-60, 61)  # ±60°
              └── simulate_shot_path(origin, az, ...) → [(x,y,z), ...]
              └── Wenn Endpunkt < HIT_RADIUS von ep → Winkel gefunden
```

**Warum brute-force:** BZFlag-Ricochet-Physik ist deterministisch (perfekte Reflexion),
aber analytisch schwer lösbar bei mehreren Bounces. Der ±60°-Scan in 1°-Schritten
kostet ~120 Simulationen à ≤5 Bounces — unter 1ms.

**Cache-Design:** Der Aim-Winkel ändert sich langsam wenn der Gegner sich bewegt.
1s Cache verhindert 10-Hz-Neuberechnung pro KI-Tick. Bei Target-Wechsel veralten
Einträge automatisch — kein explizites Löschen nötig.

**TH vs. Standard-Dispatcher:**
- Kein Z-Block (Abpraller können über/unter dem Ziel beginnen)
- Ricochet-Aim statt Lead-Aim
- Schärferer 10°-Winkel-Threshold (Abpraller tolerieren weniger Winkelabweichung)

### `_compute_aim_point()` — Lead-Vorhalt und LANDING_SHOT-Aktivierung

Zentrale Hilfsmethode, von allen `_maybe_shoot_*`-Methoden (außer SW) aufgerufen.
Gibt `(aim_x, aim_y)` oder `None` zurück.

**Gegner am Boden:**

Nur der **tangentiale** Anteil von `enemy_vel` (senkrecht zur Schusslinie) wird als
Vorhalt addiert. Reines `pos + vel × (dist/speed)` überschießt bei radial
zukommenden Gegnern (Gegner fährt direkt auf den Bot zu): Die Radialkomponente
ändert die Distanz, nicht die Winkelposition — der korrekte Schusswinkel bleibt
gleich, nur die Zeitschätzung ist leicht falsch.

**Gegner springt — Ausgabewege:**

| Zustand | Bedingung | Verhalten |
|---|---|---|
| Fenster offen | `tof < t_aim − 0.15` und Drehzeit machbar | Aim auf Zielpunkt → LANDING_SHOT aktivieren (`return None`) |
| Fenster gut | `t_aim − 0.15 ≤ tof ≤ t_aim + 0.2` | Aim auf Zielpunkt, sofort schießen (kein LANDING_SHOT) |
| Schuss käme zu spät | `tof > t_aim + 0.2` | Normale Lead-Berechnung auf aktuelle Pos |
| Nicht machbar / unerreichbar | `not can_aim` (Drehzeit/Reichweite/P4-TAC-06) | Normale Lead-Berechnung; Z-Block hält das Feuer |

**Z-Bewusstsein (P4-TAC-06/07):** Der Flachschuss läuft auf Mündungshöhe `z_ref = bot_z +
_muzzle_height`. Mit `landing_z = nav.get_floor_z(aim_x, aim_y, …)` und `tol = HIT_RADIUS`:

- **P4-TAC-06** (`landing_z − bot_z > tol`, Gegner landet höher): `z_reachable = False` →
  `can_aim = False` → normaler Vorhalt; im COMBAT hält der reguläre Z-Block das Feuer. Kein
  aussichtsloser LANDING_SHOT.
- **P4-TAC-07** (`bot_z − landing_z > tol`, Gegner landet tiefer): statt der Bodenlandung wird
  der Moment abgefangen, in dem der Gegner **fallend** die Mündungshöhe kreuzt —
  `t_aim = (−vz − √(vz² − 2g·(z0 − z_ref)))/g < t_land`. Da das Fenster mit `t_aim` (nicht
  `t_land`) verglichen wird, kippt es früher → früher feuern.
- **Gleiche Ebene**: `t_aim = t_land`, Verhalten unverändert.

`_landing_hit_z` (= `z_ref` bei P4-TAC-07, sonst `0.0`) wird beim Entry gespeichert; der Tick
rechnet `t_rem` als Fallzeit bis zu dieser Höhe.

**GM-Homing-Ausnahme (Fix 3):** Hält der Bot GM und der Gegner **kein ST**, sucht die Rakete den
Gegner selbst — der ganze Landepunkt-Block wird übersprungen (`gm_homing = own_flag == "GM" and
info.flag != "ST"`), es wird direkt auf den (luftigen) Gegner vorgehalten und gefeuert. Nur gegen
ST (GM kann nicht zielsuchen) bleibt der LANDING_SHOT.

**Gefeuert wird aus dem eigenen Tick:** Wie `Z_ATTACK` schießt `_tick_landing_shot` **selbst**
(`_send_shot` + `_set_next_shoot_after_fire`), sobald `t_rem ≤ tof + 0.15` und Ausrichtung/Reload
stimmen. Die globale Sperre in `_maybe_shoot` (`if state in (Z_ATTACK, LANDING_SHOT): return`)
hält den normalen Feuerpfad — und damit den Z-Block — vom LANDING_SHOT fern, sodass die Resthöhe
des fallenden Gegners den Schuss nicht unterdrückt (sonst käme er erst bei Landung).

**Menschlicher Doppelschuss:** Der `t_rem ≤ tof + 0.15`-Trigger feuert am *ersten* Tick, an dem
er greift — der Schuss kann dadurch bis zu ~0.15 s zu früh kommen und unter dem noch fallenden
Gegner durchgehen (verstärkt durch Distanz: `_dist_aim` ab `bot_pos` statt Mündung, `tof` mit
`_effective_shot_speed()` vs. Basis-`_shot_speed` in `_send_shot`). Statt die Trigger-Mathematik
zu verfeinern, ahmt der Bot echte Spieler nach, die bei Unsicherheit einfach mehrfach klicken: nach
dem ersten Schuss gibt er im Abstand `LANDING_DOUBLE_SHOT_DELAY = 0.15 s` (~Doppelklick) einen
zweiten ab. Der Zweitschuss wird **nur eingeplant**, wenn nach `_send_shot` feststeht, dass bis
dahin ein Slot frei wird (`_slot_reload_at[(_shot_slot+1) % _max_shots] ≤ now + delay`) — sonst
transitioniert der Bot sofort weiter (Early-Out; auf `maxShots=1`-Servern gibt es damit nie einen
Nachschuss, wie bei einem Menschen auch). Der Pending-Zweig (`_landing_second_shot_at`, geprüft
noch **vor** dem `_landing_shot_until`-Timeout) feuert den zweiten Schuss nur bei erneut geprüfter
Ausrichtung (±25° auf `_landing_aim_pos`) und freiem Slot, danach → COMBAT/SEEKING. Beim Einplanen
wird `_landing_shot_until` auf mind. `now + delay + 0.05` nachgezogen, damit der Timeout den
Nachschuss nicht abschneidet.

**Warum LANDING_SHOT-Aktivierung in `_compute_aim_point` statt in der State-Machine:**
Nur der Schieß-Code kennt `tof` (Flugzeit des Schusses), den Reload-Status und den
genauen Landepunkt. Der Movement-Code hätte keinen sinnvollen Zugriff darauf.
`_landing_shot_until = now + t_aim + 0.2s` (0.2s Puffer für Physik-Ungenauigkeiten).

---

## 9. Bedrohungserkennung und Ausweichen

### `_find_incoming_shot()` — Wann ein Schuss gefährlich ist

```python
for shot in self.active_shots.values():
    t_rel = shot.time_to_closest(bot_x, bot_y)    # Zeit bis nächste Annäherung
    if t_rel < 0:
        continue    # Schuss entfernt sich bereits
    if shot.is_expired(now + t_rel):
        continue    # Schuss läuft ab bevor er nah kommt
    dist = shot.closest_approach_dist(bot_x, bot_y)
    if dist < DODGE_DIST:   # 17.28u = TANK_RADIUS × 4.0
        # → Bedrohung erkannt
```

`DODGE_DIST = 17.28u`: Dieser Wert erscheint groß (fast 4 Tankdurchmesser). Das ist
gewollt — der Bot braucht Reaktionszeit (150ms) plus Ausweichzeit. Bei 25u/s Tank-
Geschwindigkeit legt der Bot in 150ms nur 3.75u zurück. Der Puffer von 17.28u gibt
genug Spielraum.

Der obige Code ist **vereinfacht**. `_find_incoming_shot` macht real zusätzlich:
- **Relativgeschwindigkeit**: rechnet mit `shot.vel − bot_vel` (optional hypothetisches `bot_vel`),
  damit `_tick_committed` prüfen kann, ob ein Schuss den Bot trotz Ausweichbewegung noch trifft.
- **SW-Sonderfall**: SW hat `vel≈0` → Bedrohung über expandierende Front `(_sw_dist − shockIn)/
  SW_EXPAND_SPEED − elapsed` statt d/t (s. BUGS **SW-EXP-01**: SW_EXPAND_SPEED ist zu niedrig).
- **Ricochet-Pfade**: gecachte Segmente werden separat geprüft (Lookahead `_RICO_DODGE_LOOKAHEAD`).
- **BU-Eingrabung**: bei `pos[2] < 0` nur GM als Bedrohung.
- **Z-Etagen-Check**: `|shot_z − bot_z| > HIT_RADIUS*2` → andere Etage → keine Bedrohung.

### `_handle_threat()` — EVADING vs. DODGE_JUMP

```python
# time_to_impact: verbleibende Zeit ab jetzt (Fix J1a: minus bereits verstrichene Schusszeit),
#                 für Ricochet-Schüsse aus dem Segment-Cache (threat_t).
time_to_impact = max(0.0, shot.time_to_closest(bot) - elapsed_since_fire)
# time_to_dodge: Fahrweg (1.3 Trefferradien) + 30% der Drehzeit zur Ausweichrichtung
time_to_dodge  = HIT_RADIUS*1.3 / tank_speed + turn_rad / tank_turn_rate * 0.3

if time_to_dodge * 1.1 <= time_to_impact:
    → EVADING (seitliches Ausweichen)
elif landed and own_flag not in ("NJ","BU") and not evade_cleared:
    → DODGE_JUMP (defensiver Sprung)
else:
    → Notschuss (falls Slot bereit), kein Ausweichen mehr möglich
```

Die 10%-Puffer (`1.1`): Wenn die Ausweichzeit und die Einschlagszeit fast gleich sind,
wäre seitliches Ausweichen zu knapp. Der Bot springt stattdessen — oder gibt einen Notschuss
ab, wenn auch der Sprung nicht mehr geht.

`DODGE_REACT_DELAY=0.15s`: Simuliert menschliche Reaktionszeit. `_handle_threat` gibt
erst nach 150ms `True` zurück (d.h. erst 150ms nach der ersten Bedrohungserkennung
beginnt das Ausweich-Manöver).

`IB_REACT_MULTIPLIER=3.0`: Invisible Bullet macht Schüsse auf dem Radar unsichtbar.
Der Bot reagiert erst nach `0.15 × 3.0 = 0.45s` auf IB-Schüsse — simuliert das
schlechtere Situationsbewusstsein.

### EVADING Early-Exit und Grace Period (Fix EV2 / Fix 31e)

**Das Problem**: EVADING hat zwei Exit-Pfade die `_last_threat_id = None` setzen:
- **Early-Exit**: Alle 4 Ausweich-Velocities stufen den Schuss als "sicher" ein.
- **Timer-Ablauf**: `_dodge_until` ist abgelaufen (normales Ende).

In beiden Fällen behandelt der nächste `_handle_threat`-Aufruf denselben Schuss als
neue Bedrohung → nach 150ms feuert DODGE_JUMP.

**Die Lösung**: `_evade_cleared_shots` Dict

```python
# In _tick_committed (beide Exit-Pfade, VOR _last_threat_id = None):
if self._last_threat_id is not None:
    self._evade_cleared_shots[self._last_threat_id] = now + EVADE_CLEAR_GRACE

# In _handle_threat (DODGE_JUMP-Zweig):
if self._evade_cleared_shots.get(threat_key, 0.0) > now:
    return False    # Grace aktiv → kein DODGE_JUMP
```

**Lifecycle**: Vollständig geleert in `_on_alive()` (Respawn). Einträge eines Spielers
werden in `_on_remove_player()` entfernt (`k[0] == pid`). Kein Lazy-Cleanup nötig —
das Dict wächst maximal auf die Anzahl gefährlicher Schüsse in der gesamten Bot-Lebensdauer.

**Warum sicher**: `EVADE_CLEAR_GRACE = 1.0s`. Nach Ablauf liefert
`.get(threat_key, 0.0) > now` → `False`. Anderer Schuss → kein Eintrag → kein Einfluss.

---

## 9b. Team-, Wahrnehmungs- und Effektiv-Stat-Logik

### `_is_foe(info, in_sight)` — wen darf der Bot angreifen

Modelliert die Wahrnehmung wie ein echter Spieler:
- Eigenes Team Rogue/Automatic (`0`/`0xFFFE`/`0xFFFF` → 0) → **alle** sind Feinde.
- Eigene Flagge **CB** (Colorblindness) → der Bot kann Teamfarben nicht unterscheiden →
  **alle** gelten als Feind (Teamkollegen-Beschuss-Risiko, regelkonform).
- Gegner mit **MQ** (Masquerade), den der Bot **nicht in Sicht** hat, gilt als **Freund** —
  außer der Bot trägt **SE** (Seer).
- Sonst: Feind, wenn `my_team != info.team`.

### `_should_update_player(info, …)` — Sichtbarkeit fremder Spieler

Bestimmt, ob ein Positions-Update übernommen wird (= ob der Bot den Spieler „sieht"). Zwei zentrale
Sicht-Prädikate kapseln die Flaggen-Logik: `_enemy_visible_radar` (nur **ST** radar-unsichtbar; eigenes
**JM** = Radar tot; eigenes **SE** sieht alles) und `_enemy_visible_window` (nur **CL** fenster-unsichtbar;
eigenes **B** = blind; **SE** sieht alles). Ablauf:
- **Fenster-Sichtkontakt** (`_sees_in_window` = Flagge + FOV + LoS) → Update immer.
  Der LoS-Raycast ist dabei pro Spieler für `PLAYER_LOS_TTL_S` (1,5s) gecacht (P7):
  `_should_update_player` läuft pro eingehendem `MsgPlayerUpdate` (30 Hz × Spieler) im
  Recv-Thread — ohne Cache ein Raycast pro Paket. Nur dieser Update-Pfad übergibt `now`
  und aktiviert den Cache; Flag- und FOV-Check bleiben exakt (billig, drehen sich mit dem
  Bot), und Targeting-Aufrufer von `_sees_in_window` (ohne `now`) rechnen weiterhin exakt.
  Die Staleness wirkt praktisch nur auf ST-Träger — radar-sichtbare Gegner laufen bei
  LoS-Fehlschlag über den Radar-Pfad weiter. Design analog zur Radar-Aufmerksamkeit,
  TTL bewusst länger (User-Entscheid 1–2s statt 0,1s-Originalvorschlag).
- Sonst nur **Radar** → **Radar-Aufmerksamkeit**: pro Tick fällt der „Radar-Blick" mit `RADAR_SKIP_DEFAULT`
  (CL `RADAR_SKIP_CL`) aus; bei Fehlschlag für `RADAR_COOLDOWN_DEFAULT` (0.25s) / `RADAR_COOLDOWN_CL`
  (0.5s) ganz weggeschaut (`info.radar_blind_until`). Modelliert, dass man nicht ständig aufs Radar starrt.
- Weder Fenster noch Radar (ST verdeckt / eigenes JM) → kein Update → Position friert ein.

Ein **wahrnehmbarer Schuss** verrät zusätzlich die Schützen-Position (erzwungenes Einmal-Update auf den
Schuss-Ursprung, `_shot_reveals_shooter` in `_on_shot_begin`): Schuss-Sichtbarkeit ist das Spiegelbild
der Tank-Sichtbarkeit (**IB**-Schuss radar-unsichtbar wie ST-Tank, **CS**-Schuss fenster-unsichtbar wie
CL-Tank; SE betrifft nur Tanks).

Analog filtert `_find_target_player` Ziele (ST = kein Radar, CL = kein FOV, außer SE; Fenster-Sicht
zusätzlich LoS-gegated) und
gewichtet: `score = dist × (0.8 Mensch) × PZ-Penalty(5, außer SB/SW) × ST-GM-Penalty(4)`.
**Staleness-Konsistenz (kein Geist-Lock):** `_find_target_player` überspringt Gegner, die seit
`> ENEMY_STALE_S` (10s) nicht wahrgenommen wurden — dieselbe Schwelle, ab der `_get_enemy_pos` die
Position als verloren `None` liefert. Ohne dieses Gate re-akquirierte der Reichweiten-Check einen
eingefrorenen Radar-Geist sofort über die rohe `info.pos`, sodass `_tick_combat` nie nach SEEKING
zurückfiel und `_execute_combat_move` (ep=None) regungslos stehen blieb. Jetzt: Ziel veraltet →
`target_player=None` → COMBAT→SEEKING → Patrouille/Neu-Akquise sobald ein frisches Update eintrifft.
**Sicht-FoV (vereinheitlicht):** EIN Fenster-Sichtkegel `_effective_fov()` (Halbwinkel 37,5°, WA
`_wideAngleAng/2` ~50°) für JEDE echte Sicht — Wahrnehmung (`_in_fov`→`_sees_in_window`/ST/CL/
Positions-Updates/Schuss-Reveal) UND Ziel-Erfassung. Der frühere großzügige 180°-Wahrnehmungskegel
ist entfallen. **Ziel-Halten** läuft NICHT über den FoV, sondern distanzbasiert (`_in_r = d <
radar_range` in `_validate_and_find_target`) — so verliert der Bot sein Ziel nicht, wenn er beim
Ausweichen wegdreht (auch ST, da `_in_r` reine Distanz prüft). `_is_ahead()` (±90°,
`AHEAD_HALF_ANGLE`) ist KEIN Sicht-FoV, sondern die Geometrie „liegt vor mir" für Nav-WP-Skip und
Flag-Grab.
**Pause:** `_on_pause` (MsgPause `[pid][paused]`) setzt `PlayerInfo.paused`. Pausierte sind
unverwundbar → `_maybe_shoot` feuert nicht, `_find_target_player` lockt nicht neu, `_get_enemy_pos`
liefert die bekannte Position (kein Staleness-Geist). `_tick_combat` wartet `PAUSE_WAIT_S` (12s) auf
Rückkehr, dann `target_player=None` → SEEKING.

### Effektiv-Stat-Helfer

Alle Flag-Modifikatoren laufen über `self._*`-Werte (aus `_on_set_var`), nicht über die
Default-Konstanten:
| Helfer | Flaggen-Effekt |
|---|---|
| `_effective_tank_speed()` | V ×`_velocityAd`, TH ×`_thiefVelAd`, A ×`_agilityAdVel` (nur ~Stillstand), BU ×`_burrowSpeedAd` |
| `_effective_turn_rate()` | QT ×`_angularAd`, BU ×`_burrowAngularAd` |
| `_effective_gravity()` | WG `_wingsGravity` (sofern Server-Override), LG `gravity × (_lgGravity/100)` (LG ungetestet → BUGS **LG-01**) |
| `_effective_jump_velocity()` | WG `_wingsJumpVelocity` (sofern Server-Override), sonst `_jumpVelocity` |
| `_effective_jump_height()` | Einzelsprung-Höhe `v0²/(2·|g|)` aus den beiden obigen — **eine Quelle der Wahrheit** für alle Sprunghöhen-Checks (COMBAT `_too_high`, Z-Attack, Schuss-Z-Blocks). NavGraph-Bau bleibt bewusst auf der Normalsprung-Basislinie (gecacht, ohne Flaggenkontext) |
| `_effective_reload_time()` | MG `reload/_mGunAdRate`, F `reload/_rFireAdRate` |
| `_effective_hit_radius()` | O ×`_obeseFactor`, T ×`_tinyFactor`; N → OBB (gibt 0.0) |
| `_effective_radar_range()` | BU eingegraben → 25 % |
| `_effective_fov()` | Sicht-Halbwinkel: WA `_wideAngleAng/2`, sonst `TARGET_FOV/2` (37,5°) — einziger Sicht-FoV |
| `_effective_optimal_range()` | **Ziel-bewusst**: BU-Gegner (eingegraben, nur GM/SW treffen) ohne eigenes GM/SW → Ramm-Kontakt `TANK_RADIUS·SR_RADIUS_MULT`; sonst eigen-flaggen-basiert: MG 25u, SW 20u, SR Kontakt, GM 85u, sonst 60u |

Reaktionsmultiplikatoren (`_handle_threat`): IB-Schütze ×1,5, M-Schütze ×1,5, CS-Schütze ×3 auf
`DODGE_REACT_DELAY` (250ms).

**M (Momentum) — bewusst NICHT modelliert:** M limitiert in BZFlag nur die *Beschleunigung* (Inertie:
lin ≤ 20·`_momentumLinAcc`, ang ≤ `_momentumAngAcc`), **nicht** Top-Speed/Drehrate. Der Bot rechnet
Velocity instantan (für alle Tanks) und wirft M (bad-Flag) nach ~`shakeTimeout` (~1s) wieder ab → der
Inertie-Effekt ist vernachlässigbar; `_effective_tank_speed`/`_effective_turn_rate` ignorieren M.

Auch die **Gesamtflugzeit** `2·v0/|g|` (DODGE_JUMP-Rotations-Pacing, `_check_tactical_jump`-
Anschluss-Machbarkeit `t_jump`) nutzt `_effective_jump_velocity()`/`_effective_gravity()` — für
nicht-WG/LG identisch zur Rohformel, für WG/LG konsistent flaggen-bewusst.

---

## 10. Protokoll-Schicht

### Message-Dispatch

```python
# bzflag/client.py:
self._handlers: Dict[int, Callable] = {
    MsgAddPlayer:      self._on_add_player,
    MsgShotBegin:      self._on_shot_begin,
    MsgSetVar:         self._on_set_var,
    # ...
}
```

Der Dispatch ist ein Dict, kein Switch/Case. Das ermöglicht es, neue Handler ohne
Modifikation des Dispatch-Codes hinzuzufügen — einfach `handlers[CODE] = fn` setzen.

### UDP-Asymmetrie

```
Bot sendet:     MsgPlayerUpdate   → UDP (unzuverlässig aber schnell)
Bot empfängt:   alle anderen Msgs ← TCP (zuverlässig, Server broadcastet TCP)
```

Das ist absichtlich so. `MsgPlayerUpdate` sendet der Bot mit 30 Hz
(`_maybe_send_server_update`, Sektion 2). Verlust einzelner Pakete ist akzeptabel — das
nächste kommt in ~33ms. Alle anderen Nachrichten (Shot-Events, Player-Events, Kills)
müssen zuverlässig ankommen → TCP.

**UDP-Zieladresse (N1b):** Nach erfolgreichem TCP-Connect cached der Client die
Server-IP (`getpeername()`) und sendet alle UDP-Pakete an dieses numerische IP-Tupel.
Mit einem Hostnamen im Tupel macht CPython sonst **pro Paket** ein getaddrinfo im
C-Code von `sendto` (für cProfile unsichtbar; im Container mit Docker-DNS ~1,3ms/Paket
statt ~0,1ms). Bewusst kein `udp.connect()` — das würde Empfangsfilterung und
ICMP-Fehlersemantik ändern. Tests: `test_client_udp_addr.py`.

**Bidirektionales UDP: evaluiert und bewusst verworfen** (ehem. P4-PRO-01). Der Server sendet
Broadcasts erst dann per UDP, wenn der Client `MsgUDPLinkEstablished` über UDP schickt
(`NetHandler.cxx`: erst das setzt `udpout`; die `pwrite`-Whitelist umfasst u. a.
PlayerUpdate/ShotBegin/ShotEnd/GMUpdate). Der Bot unterlässt das absichtlich
(`_h_udp_link_request` antwortet nicht): die client-seitige Hit-Detection braucht zuverlässige
`MsgShotBegin` — ein verlorenes Paket hieße weder ausweichen noch sterben (Cheater-Wirkung) —
bei ~0 Latenzgewinn im Docker-Netz. FSD: PRO-03 ist eine dauerhafte Design-Entscheidung.

### Welt-Download-Flow

```
1. Client → MsgWantWHash   (Hash der gecachten Welt anfragen)
2. Server → MsgGetWorld reply (wenn Hash neu/leer: Weltdaten, sonst: "du hast sie schon")
3. Client → MsgGetWorld (Schleife: chunks downloaden bis bytes_remaining == 0)
4. parse_world(data) → WorldMap
5. get_nav_graph(world_map) → NavGraph
6. on_world_ready() → Bot kann spawnen
```

`_deliver_world()` führt Schritte 4–6 aus. Es ist idempotent: mehrfacher Aufruf mit
gleichen Daten gibt denselben NavGraph zurück (aus Cache).

### `_worldSize`-Timing-Problem

BZFlag 2.4 sendet `MsgSetVar _worldSize` **nach** dem Welt-Download. Das bedeutet:

1. `_deliver_world()` wird mit `world_half = 200.0` (Default) aufgerufen
2. NavGraph wird mit `±200u` gebaut, Cache-Eintrag für `world_hash` angelegt
3. `_worldSize=800` kommt per MsgSetVar → `world_half = 400.0`
4. `_deliver_world()` wird erneut aufgerufen — **aber gleicher `world_hash`** → Cache-Hit
   → NavGraph mit `±200u` wird weitergenutzt

**Fix**: `invalidate_nav_cache(world_hash)` vor `_deliver_world()` löscht den alten Eintrag.

Erkennbar in den Logs: `[callsign] _worldSize=800 → NavGraph-Rebuild (vorher 200u, jetzt 400u)`.
Wenn dieser Log-Eintrag fehlt aber die Karte nicht 400×400 ist → MsgSetVar wird nicht
empfangen oder `world_half` ist bereits korrekt.

### Variablen-Katalog: `_on_set_var`

Der `_on_set_var`-Handler in `bot/handlers.py` liest alle physikalischen und
spielmechanischen Parameter vom Server. Hardcodierte Konstanten in `bot/constants.py`
(z.B. `LG_GRAVITY = 12.7`) sind nur Defaults bis der Server sie überschreibt.
Code der direkt auf Konstanten statt auf `self._lg_gravity` etc. zugreift,
ignoriert Server-Konfiguration — das ist ein Bug.

| Gruppe | Variablen | Genutzt von |
|---|---|---|
| Kern-Physik | `_tankSpeed`, `_tankAngVel`, `_jumpVelocity`, `_gravity`, `_maxShots`, `_reloadTime` | `_update_movement`, `_run_physics`, `_maybe_shoot` |
| Schuss-Physik | `_shotSpeed`, `_shotRange`, `_gmTurnAngle`, `_gmActivationTime`, `_gmAdLife`, `_lockOnAngle` | `_resolve_incoming_shots`, GM-Tracking |
| Shockwave | `_shockInRadius`, `_shockOutRadius`, `_shockAdLife` | SW-Donut in `_resolve_incoming_shots` |
| Flag-Physik | `_obeseFactor`, `_agilityAdVel`, `_lgGravity`, `_burrowDepth/_speedAd/_angularAd`, `_shieldFlight`, `_identifyRange`, `_srRadiusMult` | Hitbox-Skalierung, Radar-Radius, Steamroller-Check |
| Waffen-Rate | `_mGunAdRate/_AdLife/_AdVel`, `_rFireAdRate/_AdVel` | `_effective_reload_time()` |
| Welt | `_worldSize`, `_dropBadFlagDelay`, `_flagRadius` | NavGraph-Rebuild, Flag-Drop-Timing, Radar-Reichweite (halbe Weltgröße, F6) |
| Netz/Timing | `_updateThrottleRate` | `_maybe_send_server_update` (30-Hz-Kadenz, Sektion 2) |

**MsgSetVar vs. MsgGameSettings:**
- `MsgGameSettings` kommt einmalig beim Connect: enthält `shakeTimeout`,
  `linearAcceleration`, `angularAcceleration` (letztere noch nicht in
  Bewegungssimulation aktiv, → P4-MOV-02a; Analyse-Notizen in Sektion 13)
- `MsgSetVar` kommt nach Welt-Download und kann wiederholt kommen
  (Server-Admin ändert Werte live)
- Einzige MsgSetVar-Variable mit Seiteneffekt auf den NavGraph: `_worldSize`
  (→ `_worldSize`-Timing-Problem oben)
- `MsgGameSettings`-Felder: `worldSize` (Offset 0), `gameOptions` (Offset 6 — Bit `0x0020` =
  RicochetGameStyle → `self._server_ricochet`), `maxShots` (Offset 10),
  `linear/angularAcceleration` (14/18, noch ungenutzt → P4-MOV-02a), `shakeTimeout` (Offset 22,
  1/10 s → `_drop_bad_flag_delay`).

### Rundenende und Reconnect

Zwei Ereignisse beenden eine Runde:
- `MsgTimeUpdate` mit `timeLeft ≤ 0` (Zeitlimit),
- `MsgScoreOver` (Score-Limit / `/gameover`).

In beiden Fällen sendet der Bot ein `MsgKilled` (sauberes Ausscheiden), setzt
`_reconnect_needed = True` und stoppt die Spielschleife. Die `main()`-Schleife verbindet danach
nach 5 s neu (frischer `BZBot`). `MsgSuperKill` (Kick) beendet ebenfalls, aber **ohne** Reconnect.

### Schuss-Limit aus Chat (`MsgMessage`)

Server senden Limits als Chat-Text „N shots left". `_on_message` parst das (Quelle 255),
fügt die aktuelle Flagge zu `_limited_flags` hinzu und unterdrückt damit Random-/Druckschüsse
für diese Flagge (Schüsse für gezielte Treffer aufsparen).

### Shot-ID-Schema

```python
shot_id = (generation << 8) | slot
# slot: 0 bis maxShots-1 (zyklisch)
# generation: wird inkrementiert wenn slot überläuft
```

Warum: BZFlag-Server validieren `slot < maxShots`. Bei naiver fortlaufender Nummerierung
(`1, 2, 3, ...`) würde `slot` schnell `maxShots` (oft = 1) übersteigen → Server verwirft
alle Schüsse. Das Generation-Schema stellt sicher dass `slot` immer im gültigen Bereich ist.

---

## 11. Tests

### conftest-Pattern

```python
# tests/conftest.py
@pytest.fixture
def bot():
    b = BZBot.__new__(BZBot)
    BZBotAI.__init__(b)
    # ... minimale Attribute setzen
    return b
```

Der Bot wird ohne echten BZFlagClient instanziiert. `BZBot.__new__` überspringt
`__init__`, dann initialisiert `BZBotAI.__init__` nur die KI-Attribute. Netzwerk-
Verbindung und Server-Kommunikation passieren in Tests nie.

### Test-Kategorien

| Datei | Testet |
|-------|--------|
| `test_geometry.py` | `_angle_diff`, `_wrap`, `position_at`, `closest_approach_dist` |
| `test_kill_payloads.py` | `MsgKilled`-Payload-Aufbau für alle Waffentypen |
| `test_shot_parsing.py` | `MsgShotBegin`-Parsing, SW/Laser/Thief Sofort-Check bei Spawn |
| `test_hit_detection.py` | `_resolve_incoming_shots` für SW, GM, Laser, SR, Obesity, Narrow-OBB, PZ-Phantom |
| `test_sb_hit.py` | SB-Treffer: Wand-Phasing (phase_walls), Längskapsel, Hit-Fenster |
| `test_laser_multibounce.py` | L/TH-Mehrfach-Abpraller E2E: AdVel/AdLife-Nachjustierung, Korridor-Geometrie, Reichweiten-Grenzen |
| `test_targeting.py` | `_find_target_player` mit Radar/FOV, Stealth, Cloaking, Team; P7-LoS-Cache |
| `test_movement.py` | Waypoint-Navigation, Schwerkraft, BY-Flag, `_is_landed` |
| `test_dodge_and_jump.py` | EVADING-Trigger, DODGE_JUMP, Grace Period (EV2) |
| `test_tactics.py` | Taktische Sprünge, Z_ATTACK, LANDING_SHOT, State-Übergänge |
| `test_shooting.py` | GM-Targeting, Ricochet-Aim, Warnschuss, Burst/Slot-Reload |
| `test_flags.py` | Flag-Strategie (Grab/Drop, Klassifizierung, Effektiv-Stats) |
| `test_capability_checks.py` | Flag-Fähigkeiten (FO/RO/LT/RT, NJ, OO, …) |
| `test_pause.py` | MsgPause: pausierte Ziele nicht beschießen, `PAUSE_WAIT_S`-Wartefenster |
| `test_protocol.py` | MsgSetVar/GameSettings-Parsing, `_limited_flags`, `_updateThrottleRate`, `_wingsJumpCount` |
| `test_setvar_snapshot.py` | Snapshot: alle `_on_set_var`-Variablen → resultierende Attribute (W3-Absicherung) |
| `test_update_cadence.py` | 30-Hz-Kadenz von `_maybe_send_server_update` (Anker, Stall-Klemme, N1a) |
| `test_client_udp_addr.py` | Gecachte UDP-Zieladresse nach TCP-Connect (N1b) |
| `test_client_join.py` | `join_game`: Accept-/Reject-Auswertung (`_ev_rejected` vor `_ev_accepted`) |
| `test_idle_early_out.py` | Leerlauf-Early-Outs bei leeren Shot-Dicts (N2) |
| `test_tick_memo.py` | Per-Tick-Memo für LoS/FloorZ/Muzzle (P4a) |
| `test_world_parser.py` | `parse_world` (zlib, BoxObstacle/Pyramid/Teleporter) |
| `test_nav_graph.py` | NavGraph A*/Layer; Karten-Fixtures (ggf. via `pytest.skip`) |
| `test_async_plan.py` | Asynchrones Pathfinding: Worker, Relevanz-Gates, Prefix-Resync (P4-INF-01) |
| `test_teleporter.py` | Teleporter-Querung, Pfad-Resync, NAV_TELE |
| `test_shot_physics.py` | `simulate_shot_path` (Bounce, Box-/Pyramid-Normalen, Grid-Äquivalenz) |
| `test_bot_manager.py` | Rebalancing-Formel, Observer-/Menschen-Zählung, Profiling-Start/-Stop |
| `test_bzbot_managed.py` | Managed-Modus: `@@BZMGR@@`-Status, stdin-Kommandos |
| `test_performance.py` | Perf-/Timing-Ausgaben (`pytest -m perf -s`, kein Assert) |

### Was Tests NICHT prüfen

- Protokoll-Level-Verhalten (kein echter Server, kein TCP/UDP)
- NavGraph-Aufbau auf echten Kartendaten (zu langsam für Unit-Tests)
- State-Machine-Übergänge über mehrere Ticks (nutze manuelle Testruns)
- Rendering oder visuelle Korrektheit (kein BZFlag-Client)

### Neue Tests hinzufügen

**State-Machine-Test** (Beispiel: neuer Threat-Handler):
```python
def test_new_threat_behavior(bot):
    bot._ai_state = AIState.COMBAT
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # Shot aufbauen:
    shot = Shot(shooter_id=1, shot_id=1, pos=[50.0, 0.0, 0.0],
                vel=[-100.0, 0.0, 0.0], fire_time=time.monotonic(),
                lifetime=3.5, team=1)
    bot.active_shots[1] = shot
    bot._tick_combat(dt=0.1, now=time.monotonic())
    assert bot._ai_state == AIState.EVADING
```

**Physik-Test** (Beispiel: Decken-Kollision):
```python
def test_ceiling_collision(bot):
    # Bot aufsteigend unter einem Gebäude (self.pos/self.vel sind seit Track 5 skalare
    # Attribute — pos_x/pos_y/pos_z, vel_x/vel_y/vel_z — statt Listen; s. Abschnitt 12)
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 5.0
    bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 10.0
    obs = BoxObstacle(cx=0, cy=0, bottom_z=7.0, height=5.0, ...)
    bot._world_map = mock_world_map(boxes=[obs])
    bot._apply_obstacle_bounds(dt=0.02)
    assert bot.vel_z == 0.0    # Decken-Kollision gestoppt
```

**Wichtig**: Vor dem Test `time.monotonic()` in einer Variable speichern und
konsistent nutzen. Floating-Point-Vergleiche mit `pytest.approx(value, abs=0.01)`.

---

## Navigations-Test-Fixtures erzeugen

Für kartenspezifische Navigations-Integrationstests (z.B. `TestNavHix`) wird ein
Binary-Dump der Karte benötigt, der mit einem laufenden Server erzeugt wird.

### Schritt 1 – Dump erzeugen

```
python bzbot.py --host <server-ip> --callsign DumpBot --dump-raw tests/fixtures/hix
```

Dieser Befehl verbindet sich mit dem Server, lädt die Karte herunter und schreibt:

- `tests/fixtures/hix.bin` — rohe Weltdaten (MsgGetWorld-Stream, binär)
- `tests/fixtures/hix.meta` — Metadaten, z.B.: `world_half=200.0`

Der Bot kann nach dem Dump mit `Ctrl+C` beendet werden.

### Schritt 2 – Tests ausführen

```
pytest tests/test_nav_graph.py::TestNavHix -v
```

Tests in `TestNavHix` überspringen sich automatisch (`pytest.skip`) wenn das Fixture
fehlt — `pytest tests/` bricht deshalb nicht ab.

### Schritt 3 – Neue Karte hinzufügen

```python
# In tests/test_nav_graph.py:
class TestNavMeinekarte:
    @pytest.fixture(scope="class")
    def nav(self):
        from tests.conftest import load_map_fixture
        from bzflag.nav_graph import NavGraph
        wm = load_map_fixture("meinekarte")
        if wm is None:
            pytest.skip("Fixture fehlt: --dump-raw tests/fixtures/meinekarte")
        return NavGraph(wm, max_jump_h=18.4)

    def test_beispiel(self, nav):
        path = nav.plan_path(0.0, 0.0, 0.0, 50.0, 50.0)
        assert path
```

Das Fixture `load_map_fixture(name)` ist in `tests/conftest.py` definiert.

---

## 12. Track 5: mypyc-Kompilierung

Der Bot wird mit `mypyc --namespace-packages bot bzflag` zu nativen Erweiterungsmodulen
kompiliert. mypyc kompiliert nur, was es beweisbar sauber typisieren kann — die folgenden
Regeln haben sich dabei als notwendig erwiesen und gelten für neuen Code in `bot/`/`bzflag/`.

**Harte Laufzeit-Abhängigkeit `mypy-extensions`:** `from mypy_extensions import trait` läuft
beim Import in `bot/_bot_base.py` und allen Mixins — **auch unkompiliert**. (`pip install mypy`
zieht es als Dependency mit; in schlanken Runtime-Umgebungen muss es explizit installiert
werden, sonst ImportError beim Start.)

### `getattr(self, "x", default)` vermeiden

`getattr` mit Default erzwingt unter mypyc den generischen Objekt-Pfad statt des nativen
Attribut-Slots und verhindert Typinferenz — IMMER direkten Attributzugriff (`self.x`)
verwenden. Voraussetzung: das Attribut muss in `_bot_base.py::BZBotBase` deklariert UND in
einer `bot/core.py::_init_*`-Methode garantiert gesetzt sein (sonst wirft der Direktzugriff
unter mypyc `AttributeError`, wo `getattr` früher still den Default lieferte). Neue lazy
erzeugte Attribute also immer gleich in `_init_*` initialisieren, nicht erst bei erster
Verwendung. Ausnahme bleibt dynamischer Dispatch mit berechnetem Namen (Tabellen-Dispatch,
z.B. `getattr(self, hook)()` in `bot/handlers.py::_apply_set_var`) — das ist kein
Default-getattr und unter mypyc unproblematisch.

Dieselbe Regel gilt für `getattr` auf fremde Objekte (z.B. `NavGraph`, `FloorLayer`): nur
durch Direktzugriff ersetzen, wenn die Klasse das Attribut IMMER in `__init__` setzt.
Fehlt es dann in einem Test-Stub, den Stub ergänzen statt den Produktionscode wieder
aufzuweichen.

### `Final`-Konstanten

mypyc faltet mit `Final` annotierte Modul-Konstanten zur Compile-Zeit in den generierten
Code — sie sind danach **nicht mehr per `monkeypatch.setattr("modul.NAME", ...)`
patchbar** (der native Code liest den eingebrannten Wert, nicht das Modul-Attribut).
Deshalb: heiße, nie von Tests gepatchte Konstanten (Geometrie-/Physik-/Timing-Werte in
`bzflag/nav_graph.py`, `bzflag/obstacle_grid.py`, `bzflag/shot_physics.py`,
`bot/constants.py`) sind mit `Final` annotiert (`CELL_SIZE: Final = 4`). Konstanten, die
Tests aktiv patchen — aktuell `ASTAR_MAX_EXPANSIONS`/`ASTAR_MAX_MS`
(`bzflag/nav_graph.py`, s. `tests/test_nav_graph.py`), `NAV_ASYNC_TRIGGER_MS`
(`bot/constants.py`, s. `tests/test_async_plan.py`) und `TELE_AIM_Z_TOL`
(`bot/constants.py`, s. `tests/test_teleporter.py`) — bleiben bewusst OHNE `Final`. Neue
heiße Konstanten defaulten auf `Final`; wird eine später testseitig patchbar gebraucht,
`Final` explizit weglassen und hier ergänzen.

### GIL-Yield in langen nativen Schleifen

Als interpretierter Bytecode gibt Python an Loop-Grenzen automatisch das GIL frei; als
mypyc-natives Modul entfallen diese impliziten Yield-Punkte. Lange Schleifen in
Zweit-Threads (z.B. die A*-Vollsuche des asynchronen Planers, `_astar` in
`bzflag/nav_graph.py`) können dadurch den 60-Hz-Game-Loop-Thread verhungern lassen. Abhilfe:
alle ~1024 Iterationen (nicht öfter — `time.sleep`/`perf_counter` sind nicht gratis) ein
explizites `time.sleep(0)` einstreuen; das gibt das GIL frei, verhält sich interpretiert wie
kompiliert identisch und ist im Schnellplan (selten >1024 Iterationen) vernachlässigbar.

### Typisierung statt `Any` (M2a/M2b)

`bot/_bot_base.py::BZBotBase` deklariert die über die Mixins geteilten Attribute. Ein
Attribut bekommt einen konkreten Typ (`bool`/`int`/`str`/Container), NUR wenn es über
ALLE Zuweisungen im Code hinweg repräsentationskonfliktfrei ist (grep-geprüft) — mypyc kann
dann native Slots statt des generischen Objekt-Pfads erzeugen. Bei echtem
Mischtyp (Optional/Union, Threads/Locks/Callbacks) bleibt das Attribut `Any`.

**`float` ist in `@trait`-Klassen tabu** (Stand mypy/mypyc 2.2.0): unboxed float-Attribute
brauchen Bitmap-Definedness-Tracking, das mypyc für Traits nicht unterstützt — der Codegen
crasht mit `ValueError: value is not in list` (emitfunc.get_attr_expr), sobald eine
Trait-Methode ein solches Attribut schreibt (Minimal-Repro: @trait mit `x: float` +
Trait-Methode mit `self.x = …`). Float-Attribute in `BZBotBase` bleiben deshalb bewusst
`Any` (boxed); `bool`/`int`/`str`/Container haben Sentinel-Werte und sind nicht betroffen.
In NICHT-Trait-Klassen (`FloorLayer`, `NavGraph`, `Shot`, …) sind float-Attribute
unproblematisch und erwünscht.

### Exception-Variablen: ein Name pro Typ

mypyc typisiert eine `except … as exc`-Variable funktionsweit mit dem Typ ihres ERSTEN
Handlers. Zwei Handler unterschiedlichen Typs mit demselben Variablennamen in einer
Funktion (`except _ParseError as exc: … except Exception as exc: …`) kompilieren, aber der
zweite Handler stirbt zur Laufzeit mit `TypeError: … object expected` — genau dort, wo er
graceful degradieren sollte (so geschehen in `bzflag/world_parser.py::parse_world`).
Regel: pro except-Zweig ein eigener Variablenname, wenn die Typen differieren.

### Stand der Suite gegen den kompilierten Build

`mypyc --namespace-packages bot bzflag` kompiliert vollständig; der Kern der Suite läuft
auch kompiliert (Stand Track 5: 1111 von 1207 Tests grün). Die verbleibenden Failures sind
KEINE Produktionsfehler, sondern Test-Infrastruktur: Tests, die Methoden auf `BZBot`-
Instanzen monkeypatchen (`bot._tick_combat = …`), scheitern am read-only-Attribut nativer
Klassen. Wer die Suite vollständig gegen das Kompilat fahren will, muss diese Patterns auf
Callback-/Subclass-Muster umstellen (eigener Track, bisher bewusst nicht gemacht). Der eigene
Tank-Zustand (`self.pos`/`self.vel`, ehemals 3-elementige Listen) ist aus demselben Grund
in sechs skalare Attribute aufgelöst (`pos_x/pos_y/pos_z`, `vel_x/vel_y/vel_z`) — das
betrifft NUR den eigenen Bot; `PlayerInfo.pos`/`Shot.pos` (andere Spieler/Schüsse,
`bot/models.py`) bleiben unverändert Listen.

---

## 13. Roadmap-Notizen (FSD Phase 4)

Analyse-Ergebnisse der FSD-Bereinigung (2026-07-10), damit die Umsetzungs-Sessions direkt
aufsetzen können.

**P4-MOV-01 — Glatte Wegpunkt-Übergänge: Ansatz „Early Advance mit Korridor-Check".**
Nicht das Aim verbiegen (ein früherer Aim-Blending-/„Scandinavian-Flick"-Versuch erzeugte
Regressionen beim WP-Abfahren), sondern den aktuellen WP früher weiterschalten, wenn:
Folge-WP auf gleicher Ebene; weder aktueller noch nächster WP ein
Sprung-Anlauf-/Teleporter-/z-Wechsel-WP; direkter Korridor Bot→nächster WP frei
(`query_segment`); kein Rückwärtsmodus. Die Steering-Formel bleibt unverändert — die
vorhandene Kurvendrosselung rundet die Übergänge dann natürlich. Pure-Pursuit
(Lookahead-Punkt auf dem Pfad-Polygon) wäre eine optionale zweite Stufe. Beim Umsetzen
ersetzen: die zwei geskippten Vertrags-Tests (`tests/test_movement.py`,
`TestLookaheadSmoothing`) und der veraltete „Lookahead"-Docstring in
`bot/ai/navigation.py::_navigate_wp`.

**P4-MOV-02a–c — Trägheitsmodell: verifizierte Fakten.** Der Zielserver setzt `-a 50 38`
(→ MsgGameSettings `linear/angularAcceleration`). Der echte Client klemmt in
`LocalPlayer::doMomentum` linear auf **20 × linearAcceleration** (= 1000 u/s² → 0→25 u/s in
~25 ms ≈ 1,5 Physik-Ticks) und angular direkt auf den Wert (38 rad/s² → volle Drehrate in
~21 ms, Umkehr ~41 ms). Die reale Rampe beträgt also nur ~1–3 Ticks — kleiner sichtbarer
Effekt, im Gegenzug begrenztes Risiko für die Vorberechnungen (t_fire, turn_time,
needed_hspeed). bzfs überspringt bei aktivem `-a` seinen Highspeed-Cheat-Check
(`bzfs.cxx:5396`). Ein `doMomentum`-Äquivalent macht das M-Flag (bisher bewusst nicht
modelliert) fast gratis (`_momentumLinAcc/_momentumAngAcc`, gleiche Klemme). Hinweis:
`_inertiaLinear/_inertiaAngular` sind 3.0-BZDB-Variablen und existieren in 2.4 nicht —
2.4 nutzt ausschließlich die `-a`-Option.

**P4-FLG-04/05 — Best-Flags-Wissen: Wahrnehmungs-Gate.** Protokoll-seitig wäre der Bot
allwissend: die `flag_id` ist über Drops stabil, `MsgFlagUpdate` liefert die exakte
Bodenposition, und getragene Flaggen kommen mit echtem Kürzel durch (nur liegende sind
„PZ"-Platzhalter → `""`). Damit er menschlich bleibt: Typ-Wissen `flag_id → abbr` nur
übernehmen, wenn Träger/Drop wahrnehmbar war — Sicht (`_enemy_visible_window`) unverändert,
der Radar-Pfad (`_enemy_visible_radar`) **zusätzlich distanz-begrenzt** (Radar-Reichweite =
halbe Weltgröße wäre zu großzügig; Wert beim Umsetzen festlegen) — oder per ID-Flag
(`MsgNearFlag`) bzw. eigenem Grab/Drop. Einmal Gewusstes bleibt gemerkt; Invalidierung bei
Flag-Reset (Status 0; via `_maxFlagGrabs` kann die Flagge Ort und Typ wechseln).

**P4-TAC-05 → P4-TAC-02 — Schuss-Slots & Deckung.** Slot eines Fremdschusses =
`shot_id & 0xFF`; `maxShots`/`_reloadTime` sind serverweit bekannt, `MsgShotBegin` kommt
zuverlässig per TCP → per-Gegner-Slot-Cooldowns als neues Feld an `PlayerInfo`, befüllt in
`_on_shot_begin` (Flag-Modifikatoren des Gegners analog `_effective_reload_time`). Der Wert
liegt im Peek-Timing für die Deckung (TAC-02): „beide Slots gerade leergeschossen → ~3 s
Fenster zum Rauskommen". Für TAC-02 existieren die Geometrie-Primitive bereits
(`_segment_clear`, LoS-Ray-Grid `query_ray`, `query_segment`, Punkt-Sampler in
`bot/ai/combat.py`); offene Verhaltensfragen: Hysterese gegen Deckung↔Angriff-Oszillation,
Scope auf die stärkste Bedrohung, Ausnahmen SB (durchschlägt Wände) / SW (radial) / GM
(Deckung bricht den Lock).
