# BZFlag-Bot – Functional Specification Document

Dieses Dokument beschreibt die **Funktionalität** des Bots aus Anwender-/Verhaltenssicht: was er
tut, wie er sich im Spiel verhält und welche Schnittstellen er zum Server bedient. Implementierungs-
details (interne Funktions-/Variablennamen, Konstanten, Formeln) gehören bewusst **nicht** hierher —
sie stehen im Quellcode und in `DEVELOPER.md`.

---

## Projektziel

Ein vollautomatischer BZFlag-2.4-Bot, der sich wie ein echter Spieler verhält: verbindet sich per
TCP/UDP, nimmt am Spiel teil, weicht Schüssen aus, navigiert die Karte, zielt und schießt. Ein
Manager-Prozess hält die Bot-Anzahl dynamisch auf dem konfigurierten Niveau und macht automatisch
Platz, wenn echte Spieler beitreten.

---

## Architektur-Überblick

**Zwei Prozesstypen:**
- **Manager** — verbindet sich als Observer, zählt menschliche Spieler und startet/stoppt die Bots
  als eigenständige Prozesse.
- **Bot** — ein Prozess je Bot, verbindet sich als Tank (TCP für Steuer-/Broadcast-Nachrichten,
  UDP für die eigenen Positions-Updates).

**Taktung der Spielschleife:** Physik und Kollision laufen mit **60 Hz**, die KI-Entscheidungslogik
mit **10 Hz** (jeder 6. Physiktakt), eigene Positions-Updates gehen gedrosselt mit **30 Hz** an den
Server. Pro Physiktakt (nur wenn der Bot lebt): eingehende Schüsse/Treffer auflösen → Überroll-Check
(Steamroller/Burrow) → Bewegung aktualisieren (KI nur im 10-Hz-Takt) → zielen und feuern →
Positions-Update senden (gedrosselt) → Lenkung eigener Guided Missiles und Flag-Drop-Logik.

---

## Protokoll-Schnittstelle (BZFlag 2.4)

### Verbindungsaufbau & Handshake

```
Client → Server:  "BZFLAG\r\n\r\n"               (Hallo-String)
Server → Client:  "BZFS0221\x00"                 (Protokollversion)

Danach: [uint16 length][uint16 code][payload]

1. Flag-Liste aushandeln (alle bekannten Flag-Kürzel)
2. Welt-Hash anfragen, Weltdaten chunked empfangen
3. Beitritt anfragen (Callsign, Team, Typ=TANK, Motto, Token, Version)
4. Beitritt bestätigt; bestehende Spieler + eigener Eintrag (mit Player-ID) empfangen
5. Spawn anfordern → Spawn-Position + Heading empfangen
```

### Wichtige Message-Typen

| Code | Name | Richtung | Beschreibung |
|--------|----------------------|---------------|------------------------------------------------------|
| `0x656e` | MsgEnter | → Server | Verbindungsantrag |
| `0x6163` | MsgAccept | ← Server | Verbindung akzeptiert |
| `0x726a` | MsgReject | ← Server | Verbindung abgelehnt (mit Grund-Code) |
| `0x616c` | MsgAlive | ↔ | Spawn-Anfrage / Spawn-Bestätigung mit Position |
| `0x6170` | MsgAddPlayer | ← Server | Neuer Spieler: ID, Callsign, Team, Typ, Flag |
| `0x7270` | MsgRemovePlayer | ← Server | Spieler hat den Server verlassen |
| `0x7075` | MsgPlayerUpdate | → Server | Eigene Position + Velocity + Status (via UDP) |
| `0x7073` | MsgPlayerUpdateSmall | ← Server | Andere Spieler: kompaktes Positions-Update |
| `0x7362` | MsgShotBegin | ↔ | Schuss abgefeuert: Pos, Vel, Flag, Lifetime, Shot-ID |
| `0x6b6c` | MsgKilled | ↔ | Spieler gestorben: Killer-ID, Reason, Shot-ID, Flag |
| `0x7376` | MsgSetVar | ← Server | Server-Variablen (Physik, Flag-Parameter, Timing) |
| `0x676d` | MsgGMUpdate | ↔ | Guided-Missile-Lenkung: Richtung + Ziel-ID (empfangen: fremde GM; gesendet: eigene GM) |
| `0x6675` | MsgFlagUpdate | ← Server | Flag aufgenommen / abgelegt / zurückgebracht |
| `0x6766` | MsgGrabFlag | → Server | Flag aufnehmen |
| `0x6466` | MsgDropFlag | → Server | Flag ablegen |
| `0x4e66` | MsgNearFlag | ← Server | Bot in Reichweite einer Flag (für Identify) |
| `0x7466` | MsgTransferFlag | ↔ | Flag via Thief-Schuss übertragen (empfangen + bei eigenem Treffer gesendet) |
| `0x7069` | MsgLagPing | ← Server | Lag-Ping (muss geechot werden) |
| `0x6774` | MsgGameTime | ← Server | Server-Keepalive + Zeitbasis |
| `0x6773` | MsgGameSettings | ← Server | Einmalig: worldSize, gameOptions (Ricochet-Bit), maxShots, Beschleunigung, shakeTimeout |
| `0x7365` | MsgShotEnd | ← Server | Schuss beendet |
| `0x6366` | MsgCaptureFlag | ← Server | Flag erobert |
| `0x746f` | MsgTimeUpdate | ← Server | Verbleibende Rundenzeit; ≤ 0 → Reconnect |
| `0x736f` | MsgScoreOver | ← Server | Rundenende durch Score-Limit/gameover → Reconnect |
| `0x6d67` | MsgMessage | ← Server | Chat/Server-Text; daraus wird u. a. das Schuss-Limit erkannt |
| `0x736b` | MsgSuperKill | ← Server | Server-Kick/Disconnect → Spielschleife beenden |

Bekannt, aber bewusst ignoriert: MsgPlayerInfo, MsgTeamUpdate, MsgScore, MsgHandicap, MsgLagState.

**Player-Status-Bits** (in den Positions-Updates): lebt, springt/fällt (airborne), Flag aktiv
(zusammen mit PhantomZone-Flag → „zoned").

### Shot-ID-Schema

Shot-IDs folgen dem Schema `(generation << 8) | slot`: `slot` zählt zyklisch von `0` bis
`maxShots − 1`, `generation` erhöht sich beim Überlauf. Der Server verwirft Schüsse mit
`slot ≥ maxShots` — die korrekte Vergabe ist daher zwingend.

### MsgKilled-Layout

```
Offset  Typ     Feld
0       uint8   killer_id      (0 = World / Welt-Kill)
1       uint16  reason         (1 = GotShot, 2 = GotRunOver, 3 = GotGenocided)
3       int16   shot_id        (−1 wenn kein Schuss)
5       2 Bytes flag_abbr      (bestimmt die Server-Chat-Meldung)
```

### Bekannte Protokoll-Abweichungen

Dokumentierte Abweichungen vom BZFlag-2.4-Protokoll und ihr Stand (✅ behoben/umgesetzt, 📋 offen).

| ID | Abweichung | Schwere | Status |
|---|---|---|---|
| PRO-01 | Physikalische Konstanten (Schuss-/Tank-/Sprung-Geschwindigkeit, Schwerkraft, Drehrate, Schussreichweite) werden aus den Server-Variablen gelesen statt fest verdrahtet | Hoch | ✅ |
| PRO-02 | Positions-Update-Zeitstempel werden mit der Server-Zeit synchronisiert (statt lokaler Uhr) | Mittel | ✅ |
| PRO-03 | UDP-Asymmetrie: eigene Positions-Updates werden per UDP gesendet, Server-Broadcasts aber per TCP empfangen (zuverlässiger; bewusster Kompromiss, bidirektionales UDP ist Roadmap) | Mittel | 📋 |
| PRO-04 | Flag-Liste im Handshake vollständig gegen BZFlag 2.4 abgeglichen | Mittel | ✅ |
| PRO-05 | Flag-Aktiv-Statusbit in den Positions-Updates beim Flag-Tragen gesetzt | Niedrig | ✅ |
| PRO-06 | Veraltete Positions-Pakete (niedrigere Order-Nummer) werden erkannt und verworfen | Niedrig | ✅ |

---

## Funktionale Fähigkeiten

Die folgenden Tabellen führen die Fähigkeiten je Bereich mit Status (✅ umgesetzt, 📋 offen) und
Abhängigkeiten. Die Spalte „Feature" beschreibt die *beobachtbare* Funktionalität; Implementierungs-
details stehen im Quellcode. Über-granulare Einträge sind zu Fähigkeits-Zeilen zusammengefasst (die
ID-Spalte trägt dann einen ID-Bereich).

### Phase 1: Abgeschlossen ✅

**Manager (P1-MGR)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-MGR-01 | Observer-Verbindung zum Mitzählen menschlicher Spieler | ✅ | — |
| P1-MGR-02 | Dynamische Bot-Anzahl: hält `max_bots − Menschen` zwischen Min/Max | ✅ | — |
| P1-MGR-03 | Bots als eigenständige Subprozesse starten/stoppen/überwachen | ✅ | — |
| P1-MGR-04 | Konfiguration per YAML-Datei mit CLI-Override | ✅ | — |

**Bot-Kern (P1-BOT)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-BOT-01 | TCP+UDP-Verbindung, vollständiger BZFlag-2.4-Handshake | ✅ | — |
| P1-BOT-02 | Beitritt als Tank (umgeht ein evtl. Bot-Verbot des Servers) | ✅ | — |
| P1-BOT-03 | Korrekte Shot-ID-Vergabe (Slot/Generation-Schema) | ✅ | — |
| P1-BOT-04 | Spawn/Respawn mit kurzer Verzögerung und Retry bei ausbleibender Antwort | ✅ | — |
| P1-BOT-05 | Flag-Grundregel: aufgesammelte Flaggen zunächst sofort ablegen (in P2/P3 verfeinert) | ✅ | — |

**Bewegung (P1-MOV)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-MOV-01 | Zufalls-Wegpunkte im Passivmodus (niemand auf dem Server) | ✅ | — |
| P1-MOV-02 | Zielverfolgung im Aktivmodus (mind. ein Mensch) | ✅ | — |
| P1-MOV-03 | Physik: Schwerkraft, Sprung ohne Luftsteuerung (Absprungbewegung bleibt erhalten) | ✅ | — |
| P1-MOV-04 | Abprall an den Weltgrenzen | ✅ | — |
| P1-MOV-05 | Stuck-Erkennung → neuer Wegpunkt + Richtungsumkehr | ✅ | — |
| P1-MOV-06 | Bounce-Flag (BY): automatischer Dauer-Sprung | ✅ | — |

**Zielauswahl (P1-TGT)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-TGT-01 | Radar-Sichtweite (150u, richtungsunabhängig) | ✅ | — |
| P1-TGT-02 | FOV-Sichtweite (Sichtkegel 75°, bis 350u) | ✅ | — |
| P1-TGT-03 | Stealth (ST): nur per Sicht erfassbar, nicht auf Radar | ✅ | — |
| P1-TGT-04 | Cloak (CL): nur per Radar erfassbar, nicht per Sicht | ✅ | — |
| P1-TGT-05 | Mensch-Bevorzugung in der Zielwahl | ✅ | — |

**Schießen (P1-SHT)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-SHT-01 | Schuss erst innerhalb der Ausrichtungstoleranz (~25°) | ✅ | — |
| P1-SHT-02 | Zufallsschüsse ohne Ziel (Passivmodus) | ✅ | — |
| P1-SHT-03 | Reload-Zeit nach Server-Vorgabe | ✅ | — |
| P1-SHT-04 | TR-Flag: Dauerfeuer (Rapid-Fire) | ✅ | — |

**Hit-Detection (P1-HIT)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-HIT-01 | Normalschuss: 3D-Abstands-Treffer (schmale Hitbox bei N-Flag, größere bei O) | ✅ | — |
| P1-HIT-02 | Shockwave (SW): wachsende radiale Killzone, zeit- und sofortbasierte Trefferprüfung | ✅ | — |
| P1-HIT-03 | Guided Missile (GM): fortlaufende Homing-Simulation aller aktiven Raketen | ✅ | — |
| P1-HIT-04 | Laser (L): Sofort-Treffer entlang der Laserachse | ✅ | — |
| P1-HIT-05 | Steamroller (SR): Treffer per Kontaktnähe | ✅ | — |
| P1-HIT-06 | Obesity (O): vergrößerte eigene Hitbox | ✅ | — |

**Ausweichen & Springen (P1-DDG)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-DDG-01 | Bedrohungserkennung über den dichtesten Annäherungsabstand der Schussbahn | ✅ | — |
| P1-DDG-02 | Menschliche Reaktionsverzögerung (~150 ms) | ✅ | — |
| P1-DDG-03 | Invisible Bullet (IB): verstärkte Reaktionsverzögerung (radar-unsichtbar) | ✅ | — |
| P1-DDG-04 | Ausweichrichtung senkrecht zur Schussbahn, weg vom Schuss | ✅ | — |
| P1-DDG-05 | Ausweichsprung-Fallback, wenn reguläres Ausweichen zeitlich nicht reicht (Landung Richtung Gegner) | ✅ | — |
| P1-DDG-06 | No-Jump-Flag (NJ): Ausweichsprung gesperrt | ✅ | — |

**Kill-Pakete (P1-KIL)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-KIL-01 | Korrekte Flag-Kennung im Kill-Paket (für die Server-Chat-Meldung) | ✅ | — |
| P1-KIL-02 | Korrekte Todesursache-Codes (Schuss / Überrollt / Genocide) | ✅ | — |
| P1-KIL-03 | Steamroller-Kill korrekt als Überroll-Tod gemeldet | ✅ | — |

**Tests (P1-TST)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P1-TST-01 | Umfangreiche Unit-Tests ohne laufenden BZFlag-Server | ✅ | — |

---

### Phase 2: Abgeschlossen ✅

**Protokoll-Compliance (P2-PRO)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P2-PRO-01 | Alle physikalischen Server-Variablen aus dem Server lesen | ✅ | — |
| P2-PRO-02 | Positions-Update-Zeitstempel mit der Server-Zeit synchronisieren | ✅ | — |
| P2-PRO-03 | Flag-Aktiv-Statusbit beim Flag-Tragen setzen | ✅ | — |
| P2-PRO-04 | Flag-Liste im Handshake gegen den Server abgleichen | ✅ | — |
| P2-PRO-05 | Erweiterte Server-Variablen lesen (Schockwellen-, GM-, Flag-Physik- und Schusstyp-Parameter) | ✅ | P2-PRO-01 |
| P2-PRO-06 | Veraltete Positions-Pakete (niedrigere Order-Nummer) überspringen | ✅ | — |

**Schießen (P2-SHT)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P2-SHT-01 | Schuss-Vorhalt: auf die vorhergesagte Gegnerposition zielen | ✅ | — |
| P2-SHT-02 | Eigene Flagge beim Schießen berücksichtigen (korrekter Schuss-Typ) | ✅ | P2-FLG-01 |
| P2-SHT-03 | Strengere Ausrichtungstoleranz für Laser | ✅ | — |
| P2-SHT-04 | Z-Achsen-Schießlogik: Laser/Thief bei großem Höhenunterschied blockieren, GM/SW höhenunabhängig | ✅ | — |
| P2-SHT-05 | Schockwelle korrekt mit Null-Velocity senden (Servervorgabe) | ✅ | P2-FLG-01 |
| P2-SHT-06 | Flag-spezifischer Schuss-Dispatcher mit gemeinsamer Zielpunkt-Berechnung | ✅ | P2-SHT-02 |

**Flaggen (P2-FLG)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P2-FLG-01 | Gute Flaggen nutzen statt ablegen | ✅ | — |
| P2-FLG-02 | Schlechte Flaggen nach Drop-Cooldown ablegen, neutrale sofort | ✅ | — |
| P2-FLG-03 | GM-Zielerfassung: Rakete nach Aktivierung auf das Ziel lenken; gegen Stealth nur mit Sichtlinie/enger Ausrichtung (kein Homing); Mindestpause zwischen GM-Schüssen | ✅ | P2-SHT-02 |
| P2-FLG-04 | Flag-Positionen aus den Server-Updates verfolgen | ✅ | — |
| P2-FLG-05 | Opportunistisches Flag-Aufsammeln im Aktivmodus | ✅ | P2-FLG-04 |
| P2-FLG-06 | Drop-Cooldown nach Server-Vorgabe | ✅ | — |
| P2-FLG-07 | Aufsammeln nur im Sichtfeld (kein Rückwärts-Umweg) | ✅ | — |
| P2-FLG-08 | Burrow (BU): reduzierte Radar-Reichweite am Boden, Sprung gesperrt | ✅ | — |

**Ausweichen (P2-DDG)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P2-DDG-01 | Dynamische Ausweichdauer je nach Zeit bis zum Einschlag | ✅ | — |
| P2-DDG-02 | Taktischer Übersprung: Frontalsprung über den Gegner bzw. Flucht-Sprung mit Wende (committed Wind-Up) | ✅ | — |
| P2-DDG-03 | Vorwärts-/Rückwärts-Ausweichen statt Drehen, wenn die Blickrichtung passt | ✅ | — |

**Bewegung (P2-MOV)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P2-MOV-01 | Keine Drehung im Stillstand (Mindest-Richtgeschwindigkeit) | ✅ | — |
| P2-MOV-02 | Stabile Abstandshaltung (kein Kreisfahren) | ✅ | — |
| P2-MOV-03 | Im Nahbereich rückwärts fahren, um den Gegner im Blick zu behalten | ✅ | — |
| P2-MOV-04 | Ausweichsprung dreht so, dass die Landung zum Gegner zeigt | ✅ | P1-DDG-05 |
| P2-MOV-05 | Taktischer Übersprung als Kampfbewegung (Frontalsprung / Flucht-Sprung) | ✅ | P2-DDG-02 |
| P2-MOV-06 | Landing-Shot: Position halten und auf den Landepunkt eines springenden Gegners zielen | ✅ | — |
| P2-MOV-07 | Z-Höhenangriff: gegen erhöhten Gegner in Sprungreichweite springen, auf die vorhergesagte Position ausrichten und auf Gegnerhöhe feuern (Auf- und Abstieg) | ✅ | P2-MOV-05 |

**Zielauswahl-Erweiterung (P2-TGT)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P2-TGT-06 | Sprung-Vorhalt: auf den vorhergesagten Landepunkt eines springenden Gegners zielen | ✅ | — |

**Pathfinding & Kollision (P2-PTH)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P2-PTH-01…03 | Welt-Parsing (Gebäude, Pyramiden, Teleporter) und Aufbau eines 3D-Navigationsgraphen mit Boden-/Dach-Ebenen und A*-Pfadsuche inkl. Sprung-/Fall-Kanten | ✅ | — |
| P2-PTH-04…12, 14, 15 | Robuste 3D-Navigation: Etagenwechsel per Sprung (Ausrichtung vor dem Absprung, Anlauf-Wegpunkte, Clearance-Erkennung (seitlich am Ziel und an dazwischenliegenden Überhängen im Sprungbogen) und Fehllandungs-Erkennung mit Routen-Replan; Lande-Drehung am Absprung fixiert: zum nächsten Wegpunkt, bzw. zum Gegner wenn der Landepunkt auf Gegner-Höhe liegt), Wandgleiten und Decken-Kollision in allen Bewegungszuständen, Wegpunkt-Folge auch im Kampf mit Direktanflug bei Optimaldistanz, Graph-Cache pro Welt | ✅ | P2-PTH-01…03 |
| P2-PTH-13 | Physiktakt auf 60 Hz (BZFlag-Standard), Server-Updates 30 Hz | ✅ | — |

---

### Phase 3: Abgeschlossen ✅ (Produktionsreif)

Kritische Fähigkeiten für den produktiven Server-Betrieb.

**Navigation (P3-NAV)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P3-NAV-01 | Teleporter: Alle Schüsse erkennen | ✅ | — |
| P3-NAV-02 | Teleporter: Pfadplanung & Durchfahren (Posts als Fahr-Kollision, A*-Teleport-Kanten, Positions-Sprung abfangen) | ✅ | — |
| P3-NAV-03 | Teleporter: gegnerische Teleports erkennen (MsgTeleport) | ✅ | — |
| P3-NAV-04 | Teleporter: Eigene Schüsse zielen | ✅ | P3-NAV-01 |
| P3-NAV-05 | Ricochet: Alle Abpraller-Schüsse erkennen | ✅ | — |
| P3-NAV-06 | Ricochet: Eigene Abpraller-Schüsse zielen | ✅ | — |
| P3-NAV-07 | Ricochet: Eigene Abpraller werden als Selbsttreffer erkannt | ✅ | — |

**Schießen (P3-SHT)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P3-SHT-01 | Schuss-Sonderfall: Zufalls-/Druckschuss-Intervall | ✅ | P1-SHT-02 |
| P3-SHT-02 | Schuss-Sonderfall: Slot-/Burst-genaues Nachladen | ✅ | P1-SHT-02 |
| P3-SHT-03 | Schuss-Sonderfall: Super Bullet schießt durch Wände | ✅ | P1-SHT-02 |
| P3-SHT-04 | Schuss-Sonderfall: Kein gezielter Schuss gegen PhantomZone-Gegner (außer SW/SB) | ✅ | P1-SHT-02 |
| P3-SHT-05 | Schuss-Sonderfall: Reservierter Slot für den gezielten Schuss | ✅ | P1-SHT-02 |
| P3-SHT-06 | Laser/Thief als Sofort-Treffer modelliert (bewusste Vereinfachung) | ✅ | — |

**Wahrnehmung & Team (P3-PER)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P3-PER-01 | Freund/Feind- und Sicht-Mechaniken: Rogue/Colorblindness werten alle als Feind | ✅ | — |

| P3-PER-06 | Verstärkte Reaktionsverzögerung gegen schwer wahrnehmbare Schützen: IB ×3, Momentum ×1,5, Cloaked Shot ×3 | ✅ | — |
| P3-PER-07 | Ziel-Scoring (Distanz, Mensch-Bonus, PhantomZone-/Stealth-gegen-GM-Abwertung) | ✅ | — |

**Flaggen (P3-FLG)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P3-FLG-01 | Vollständige Flaggen-Strategie (alle Flaggen klassifiziert: nutzen/ablegen/vermeiden), außer PhantomZone | ✅ | P2-FLG-01 |
| P3-FLG-02 | Aktives Flaggen-Suchen (zu sichtbaren guten Flaggen navigieren) | ✅ | — |
| P3-FLG-03 | Freund/Feind- und Sicht-Mechaniken: Stealth nur per Sicht, Cloak nur per Radar | ✅ | — |
| P3-FLG-04 | Freund/Feind- und Sicht-Mechaniken: Eigene Blindness/Jamming kehren dies um | ✅ | — |
| P3-FLG-05 | Freund/Feind- und Sicht-Mechaniken: Seer sieht alle normal | ✅ | — |
| P3-FLG-06 | Freund/Feind- und Sicht-Mechaniken: Masquerade-Gegner gelten außer Sicht als Freund | ✅ | — |
| P3-FLG-07 | Genocide (G): nur bei Multikill-Chance behalten; stirbt ein Teamkollege mit Genocide, stirbt der Bot regelkonform mit | ✅ | — |
| P3-FLG-08 | Limited-Flags: Schuss-Limit unterdrückt Zufalls-/Druckschüsse (Auto-Erkennung aus Server-Text) | ✅ | — |
| P3-FLG-09 | Gerade gedroppte neutrale Flaggen nicht sofort wieder aufsammeln | ✅ | — |

**Taktik (P3-TAC)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P3-TAC-03 | Eigenes Team nicht angreifen (außer Rogue) | ✅ | — |

**Lebenszyklus (P3-LC)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P3-LC-01 | Reconnect am Rundenende (Restzeit ≤ 0 oder Score-Over): abmelden und nach kurzer Wartezeit neu verbinden | ✅ | — |
| P3-LC-02 | Falling-Zustand: unkontrollierter Fall vom Dach (kein Lenken), Rückkehr in den vorherigen Zustand bei Landung | ✅ | — |

---

### Phase 4: Roadmap (Allgemeine Verbesserungen)

Taktische und infrastrukturelle Verbesserungen ohne harte Produktionsrelevanz.

**Protokoll (P4-PRO)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P4-PRO-01 | Bidirektionales UDP evaluieren (aktuell nur Senden via UDP) | 📋 | — |

**Bewegung (P4-MOV)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P4-MOV-01 | Glatte Wegpunkt-Übergänge (Lookahead-Blending) | 📋 | P2-PTH-01…03 |
| P4-MOV-02 | Trägheitsmodell (lineare/angulare Beschleunigung) in der Bewegungssimulation nutzen | 📋 | — |

**Flaggen (P4-FLG)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P4-FLG-03 | PhantomZone regelkonform nutzen | 📋 | P3-NAV-02 |
| P4-FLG-04 | Vom Gegner fallengelassene gute Flaggen aufsammeln | 📋 | — |

**Taktik (P4-TAC)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P4-TAC-01 | Flankieren statt frontal | 📋 | — |
| P4-TAC-02 | Deckung hinter Gebäuden | 📋 | — |
| P4-TAC-04 | Gegner-Bewegungsmuster lernen | 📋 | — |
| P4-TAC-05 | Gegner-Schuss-Slots tracken und passend aus der Deckung kommen | 📋 | — |
| P4-TAC-06 | Landing-Shot nicht ausführen, wenn Landung > eigener Z | ✅ | — |
| P4-TAC-07 | Landing-Shot eher ausführen, wenn Landung < eigener Z | ✅ | — |

**Performance / Infrastruktur (P4P4-INF)**

| ID | Feature | Status | Abhängigkeiten |
|---|---|---|---|
| P4-INF-01 | Asynchrones Pathfinding (A* nebenläufig; Bot nutzt den letzten validen Pfad oder Direktweg weiter) | ✅ | — |
| P4-INF-02 | Asynchroner KI-Tick: zeitkritische Physik (60 Hz) von der KI-Logik (10 Hz) entkoppeln | 📋 | P4-INF-01 |

---

## Flaggen-Verhaltensmatrix

Je Flagge die Bot-Klassifizierung (**good** = behalten/nutzen, **bad** = nach Drop-Cooldown ablegen,
**neutral** = sofort ablegen) und die konkrete Wirkung beim Halten der Flagge bzw. — wo relevant —
als Gegner-Effekt auf den Bot. Server-Variablen können die Werte überschreiben.

### Waffen-Flaggen

| Abbr | Klasse | Bot-Verhalten |
|---|---|---|
| GM | good | Optimaldistanz ~85u; nach kurzer Aktivierungszeit lenkt der Bot die Rakete auf das Ziel; gegen Stealth-Ziele nur mit Sichtlinie und enger Ausrichtung (kein Homing); höhenunabhängig |
| L | good | Laser, als Sofort-Treffer modelliert (kein Vorhalt); enge Ausrichtung; blockiert bei größerem Höhenunterschied; ohne Sichtlinie Abpraller-Zielen, falls Ricochet aktiv |
| SW | good | Flächen-Schockwelle; feuert im Nahbereich (bis ~60u, 3D); keine Sichtlinien-/Höhenbeschränkung; radiale Killzone (auch Gegner auf/in Gebäuden) |
| SB | good | Schießt durch Wände (keine Sichtlinie); tötet auch PhantomZone-Gegner |
| MG | good | Kurze Reichweite, schnelles Nachladen; Optimaldistanz ~25u |
| F | good | Schnelleres Dauerfeuer |
| TR | **bad** | Tank schießt unkontrolliert weiter (Dauerfeuer ohne Zielprüfung) |
| TH | good | Diebstahl statt Kill (Flag-Transfer); nur Nahbereich (~120u); klein und schnell |
| R | good | Aktiviert Abpraller; ohne Sichtlinie offensives Abpraller-Zielen; eigene Abpraller werden als Selbsttreffer erkannt |

### Wahrnehmung / Tarnung

| Abbr | Klasse | Bot-Verhalten |
|---|---|---|
| ST | good | Träger nur per Sicht auffindbar (nicht auf Radar); als Ziel für GM benachteiligt |
| CL | good | Träger out-window unsichtbar, nur per Radar; Radar-Update gedrosselt |
| SE | good | Sieht Stealth/Cloak/Masquerade-Gegner normal |
| IB | good | Eigene Schüsse radar-unsichtbar; gegen IB-Schützen reagiert der Bot deutlich verzögert |
| CS | good | Eigene Schüsse out-window unsichtbar, aber auf Radar sichtbar (Gegenstück zu IB); gegen CS-Schützen reagiert der Bot etwas verzögert. *Vorausschauend — existiert in keinem aktuellen Server* |
| MQ | good | Erscheint als Teamkollege; Gegner-MQ außer Sicht gilt als Freund (außer Bot trägt SE) |
| ID | good | Identifiziert nahe Flaggen; navigiert zu guten Flaggen in Reichweite und legt ID ab |

### Bewegung / Physik

| Abbr | Klasse | Bot-Verhalten |
|---|---|---|
| V | good | Schneller |
| A | good | Kurzer Geschwindigkeitsschub aus dem (Fast-)Stillstand |
| QT | good | Schnelleres Drehen |
| BU | good | Eingegraben (sinkt unter den Boden), langsamer, reduzierte Radar-Reichweite, kein Sprung; nur SW/GM treffen, **von jedem Tank überrollbar**. *Als Gegner-Effekt:* der Bot rammt einen eingegrabenen BU-Träger (Kontaktdistanz), sofern er nicht selbst GM/SW trägt |
| JP | good | Sprung möglich (hebt ein vorheriges No-Jump auf) |
| WG | good | Zusätzliche Luftsprünge mit eigener Flug-Physik (eigene Schwerkraft/Sprung-Velocity) |
| OO | good | Fährt durch Gebäude; kein Schuss, solange im Gebäude |
| SH | good | Erster Treffer droppt nur die Flagge (Bot überlebt) |
| T | good | Kleine Hitbox |
| N | good | Schmale Hitbox (von vorn schwer zu treffen, von der Seite normal) |
| SR | good | Tötet per Kontakt; Optimaldistanz = Kontaktreichweite |
| G | good | Behalten nur bei Multikill-Chance; Genocide-Propagation |

### Behindernde Flaggen (bad → ablegen, aber während des Haltens wirksam)

| Abbr | Klasse | Bot-Verhalten |
|---|---|---|
| O | bad | Große Hitbox |
| BY | bad | Tank springt unkontrolliert weiter (in Fahrtrichtung) |
| NJ | bad | Sprung gesperrt |
| B | bad | Gegner nur per Radar erfassbar |
| JM | bad | Radar fällt aus (nur Sicht) |
| CB | bad | Alle als Feind gewertet (Teamkollegen-Risiko) |
| WA | bad | Weiteres Sichtfeld, leichte Ziel-Streuung |
| M | bad | Träge; gegen M-Schützen reagiert der Bot langsamer |
| FO/RO/LT/RT | bad | Fahr-/Dreh-Beschränkungen |
| RC | bad | Keine Sonderbehandlung — nur Drop |

### Neutrale Flaggen (sofort ablegen)

| Abbr | Klasse | Bot-Verhalten |
|---|---|---|
| PZ | neutral | Vom Bot nicht genutzt (Roadmap). *Als Gegner-Effekt:* nur SW/SB treffen/zielen |
| LG | neutral | v2.1+/v3.0-Flagge, im v2.4-Zielserver nicht vorhanden; Schwerkraft-Handling vorhanden aber dormant |
| US | neutral | Nutzlos |

---

## Schuss-Sonderfälle

| Flag | Aim-Toleranz | Sichtlinie nötig | Höhen-Block | Spezialwirkung | Opt.-Distanz |
|---|---|---|---|---|---|
| Standard | 25° | nein (Abpraller-Fallback) | bei großem Höhenunterschied | Vorhalt (tangential) | 60u |
| SW | — (Flächenwaffe) | nein | nein | radiale Killzone, feuert 6–60u (3D) | 20u |
| GM | eng (enger im Nahbereich; gegen Stealth strenger) | nur gegen Stealth-Ziel | nein | Lenkung der Rakete | 85u |
| L | sehr eng | nein (Abpraller-Fallback) | bei kleinem Höhenunterschied | Sofort-Treffer | 60u |
| TH | eng | nein (Abpraller-Fallback) | bei kleinem Höhenunterschied | Sofort, kein Kill → Diebstahl, nur ≤120u | — |
| SB | 25° | **nein (durch Wände)** | wie Standard | durchschlägt Wände, kein Abpraller | 60u |
| TR | keine | nein | nein | Dauerfeuer (slot-getaktet) | — |
| WA | 25° (+Streuung) | wie Standard | wie Standard | weiteres Sichtfeld | 60u |

Im Z-Höhenangriff und Landing-Shot gilt die jeweils eigene Feuerlogik. Ohne Ziel/im Passivmodus
fallen Schüsse auf das Zufalls-Intervall zurück; gute und limitierte Flaggen unterdrücken
Zufalls-/Druckschüsse.

---

## State Machine

Der Bot wird über eine Zustandsmaschine gesteuert; jeder Übergang wird protokolliert.

### Zustände

| State | Beschreibung | Bewegung |
|---|---|---|
| `DEAD` | Tot, wartet auf Spawn | — |
| `IDLE` | Passiv — niemand auf dem Server | Zufalls-Wegpunkte |
| `SEEKING` | Aktiv — Ziel/Flaggen suchen (Mensch oder Observer da) | Wegpunkt-Navigation |
| `COMBAT` | Kampf — Abstandshaltung + Schießen | Kampf-Bewegung |
| `EVADING` | Ausweichen — Schuss im Anflug (timer-basiert) | committed |
| `JUMP_WINDUP` | Taktischer Übersprung: Wind-Up (committed) | committed |
| `JUMPING` | Taktischer Sprung in der Luft (physik-committed) | committed |
| `Z_ATTACK` | Z-Höhenangriff: Rotation auf Gegner + Schuss auf Gegnerhöhe | committed |
| `DODGE_JUMP` | Defensiver Ausweichsprung | committed |
| `LANDING_SHOT` | Position halten, auf Landepunkt des springenden Gegners zielen | steht, dreht |
| `NAV_JUMP` | Navigations-Sprung zum Etagenwechsel | committed |
| `NAV_JUMP_ALIGN` | Vor NAV_JUMP: still auf das Sprungziel ausrichten | steht, dreht |
| `NAV_TELE` | Letztes Stück direkt in die Teleporter-Mitte fahren, bis Querung/Revert | fährt direkt |
| `FALLING` | Unkontrollierter Fall vom Dach (kein Lenken) | committed |

> **Präsenz:** IDLE↔SEEKING richten sich danach, ob jemand (Mensch **oder** Observer) auf dem Server
> ist. Echter Kampf (COMBAT) erfordert mindestens einen **Menschen** — bei nur Observern bleibt der
> Bot in SEEKING (sieht belebt aus, kämpft aber nicht).

### Valide Übergänge (Auslöser funktional)

| Von | Nach | Auslöser |
|---|---|---|
| `DEAD` | `IDLE`/`SEEKING` | Spawn — SEEKING wenn Menschen da, sonst IDLE |
| `IDLE` | `SEEKING` | jemand betritt den Server |
| `SEEKING` | `IDLE` | niemand mehr da |
| `SEEKING` | `COMBAT` | Ziel gefunden |
| `COMBAT` | `SEEKING`/`IDLE` | Ziel verloren bzw. keine Menschen mehr |
| `IDLE`/`SEEKING`/`COMBAT` | `EVADING` | Bedrohung erkannt, Ausweichen machbar |
| `IDLE`/`SEEKING`/`COMBAT` | `DODGE_JUMP` | Bedrohung erkannt, Ausweichen nicht machbar → Sprung |
| `COMBAT` | `JUMP_WINDUP` | Taktischer Übersprung ausgelöst (nicht gegen Schockwellen-Träger) |
| `COMBAT` | `Z_ATTACK` | Gegner in Sprung-Höhenreichweite |
| `COMBAT` | `LANDING_SHOT` | Gegner springt, Feuerfenster offen, Drehung machbar |
| `Z_ATTACK` | `COMBAT` | Landung (immer zurück in COMBAT) |
| `EVADING` | `COMBAT`/`SEEKING`/`IDLE` | Schuss vorbei oder Timer abgelaufen (je nach Präsenz/Ziel) |
| `JUMP_WINDUP` | `JUMPING` | Wind-Up abgelaufen → Absprung |
| `JUMPING`/`DODGE_JUMP` | `COMBAT`/`SEEKING`/`IDLE` | Landung (je nach Präsenz/Ziel) |
| `LANDING_SHOT` | `COMBAT` | Schuss abgefeuert oder Zeitfenster abgelaufen |
| `LANDING_SHOT` | `EVADING` | Bedrohung von einem anderen Gegner |
| beliebig | `NAV_JUMP_ALIGN` | Sprung geometrisch ok, aber noch nicht ausgerichtet |
| `NAV_JUMP_ALIGN` | `NAV_JUMP` | ausgerichtet → Absprung |
| `NAV_JUMP_ALIGN` | (Boden-State) | Timeout → Replan |
| `NAV_JUMP` | `COMBAT`/`SEEKING` | Landung (SEEKING bei fehlendem Ziel oder Fehllandung) |
| beliebig | `NAV_TELE` | Eingangs-WP erreicht, nächster WP = Teleporter-Austritt, Tor-Mitte ≤ Reichweite |
| `NAV_TELE` | (Boden-State) | Querung ausgeführt, oder Timeout/Revert → Replan |
| (Boden-States) | `FALLING` | vom Dach gefallen |
| `FALLING` | (Vor-Fall-State) | Landung |
| beliebig | `JUMPING` | Bouncy-Flag-Bounce |
| beliebig | `DEAD` | Tod (Schuss/Überrollen/Genocide/Rundenende) |

### Verhaltensregeln (Invarianten)

- `JUMP_WINDUP` und `EVADING` sind **committed** — keine neuen KI-Entscheidungen bis zum Ende.
- `JUMPING`/`DODGE_JUMP`/`NAV_JUMP`/`FALLING` sind **physik-committed** — die Horizontalbewegung aus
  dem Absprung/Fall bleibt konstant, nur der Azimuth dreht sich noch.
- `FALLING` setzt keinen Sprung-Cooldown (kein echter Sprung) und übernimmt die Boden-Drehrate.
- `NAV_JUMP_ALIGN` ist **positions-committed** — der Bot steht und dreht nur, bis er ausgerichtet
  ist; bei Timeout wird neu geplant.
- `NAV_TELE` ist **ziel-committed** — der Bot fährt das letzte kurze Stück direkt in die Tor-Mitte
  (ohne normale WP-Navigation), bis der zentrale Querungs-Check teleportiert oder der Austritt
  revertiert (`_is_inside_obstacle`); bei Timeout/Block folgt Cooldown + Replan.
- Nach dem Ausweichen wird derselbe Schuss kurzzeitig nicht erneut als Auslöser gewertet (kein
  sofortiger Re-Trigger).

---

## Design-Entscheidungen

| Entscheidung | Begründung |
|---|---|
| Beitritt als Tank statt Computer/Autopilot | Umgeht ein evtl. Bot-Verbot; verhält sich für den Server wie ein echter Spieler |
| Sofort-Check bei SW/Laser/Thief zusätzlich zum 60-Hz-Check | Diese Waffen wirken instantan, nicht erst im nächsten Physiktakt |
| Verstärkte Reaktionsverzögerung gegen IB/CS/M | Schwer wahrnehmbare Schüsse simulieren eine realistisch schlechtere menschliche Reaktion |
| UDP senden / TCP empfangen | TCP für empfangene Broadcasts ist zuverlässiger; bewusster Kompromiss |
