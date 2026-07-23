"""Alle Spiel-Konstanten des Bots — die „Header-Datei" (W1/W8, FABLE-PLAN Teil 3).

Werte aus dem BZFlag-Quellcode verifiziert (global.cxx, PlayerState.cxx, Flag.cxx).

Server-Variablen-Tabelle (W8): Viele Konstanten sind nur die DEFAULTS von
Server-Variablen, die zur Laufzeit via MsgSetVar überschrieben werden. Solche
Konstanten tragen die Annotation ``↔ _serverVar ↔ self._attr``: Entscheidungs-
Code muss das Instanz-Attribut nutzen, NICHT die Konstante — sonst rechnet der
Bot auf Custom-Servern mit falschen Werten (Fehlerklasse F5–F7, FABLE-PLAN
Teil 2). Konstanten ohne Annotation sind echte Konstanten oder bewusste
Bot-Tuning-Knöpfe ohne Server-Pendant.
"""

import math
from typing import Final

from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE

# ── Tank-Physik & Geometrie (aus global.cxx + PlayerState.cxx verifiziert) ─
TANK_LENGTH: Final = 6.0                  # ↔ _tankLength ↔ self._tank_length
TANK_WIDTH: Final = 2.8                  # ↔ _tankWidth ↔ self._tank_width
TANK_HEIGHT: Final = 2.05                 # ↔ _tankHeight ↔ self._tank_height
MAX_BUMP_HEIGHT: Final = 0.33                 # ↔ _maxBumpHeight ↔ self._max_bump_height: max. Stufen-
                                          # höhe, die ein Tank direkt überfährt statt anzustoßen
TANK_RADIUS_FACTOR: Final = 0.72                 # _tankRadius = 0.72 * _tankLength (global.cxx)
TANK_RADIUS: Final = TANK_RADIUS_FACTOR * TANK_LENGTH   # 4.32 (_tankRadius; nicht nachgeführt —
                                           # an TANK_RADIUS hängen Modulkonstanten wie
                                           # HIT_RADIUS/DODGE_DIST/MUZZLE_FRONT und der
                                           # NavGrid-Pad, s. F7-Entscheidung im FABLE-PLAN)
TANK_HALF_LENGTH: Final = TANK_LENGTH / 2.0        # 3.0
TANK_HALF_DIAG: Final = math.sqrt(TANK_HALF_LENGTH ** 2 + (TANK_WIDTH / 2.0) ** 2)  # ≈3.31u
TANK_SPEED: Final = 25.0                 # ↔ _tankSpeed ↔ self._tank_speed
TANK_TURN_RATE: Final = 0.785398  # π/4 rad/s  ↔ _tankAngVel ↔ self._tank_turn_rate
JUMP_VELOCITY: Final = 19.0                 # ↔ _jumpVelocity ↔ self._jump_velocity
GRAVITY: Final = -9.8                 # ↔ _gravity ↔ self._gravity
WALL_HEIGHT_DEFAULT: Final = 3.0 * TANK_HEIGHT    # _wallHeight = 3*_tankHeight (global.cxx)
                                           # ↔ _wallHeight/_tankHeight ↔ self._wall_height
WORLD_HALF_DEFAULT: Final = 400.0                # ↔ _worldSize/2 ↔ self.world_half
MUZZLE_FRONT: Final = TANK_RADIUS + 0.1   # 4.42  ↔ _muzzleFront ↔ self._muzzle_front
MUZZLE_HEIGHT: Final = 1.57                #       ↔ _muzzleHeight ↔ self._muzzle_height
ON_TOP_EPS: Final = 0.1     # Toleranz (u), in der eine Bot-Basis als "bündig AUF einer Box"
                              # (= nicht innen) gilt — verhindert, dass ein fahrender Teleporter-
                              # Austritt auf der Mauer-Oberkante (z=Box-Top) fälschlich revertiert wird.

# ── Schuss ────────────────────────────────────────────────────────────────
SHOT_SPEED_DEFAULT: Final = 100.0                # ↔ _shotSpeed ↔ self._shot_speed
SHOT_RANGE: Final = 350.0                # ↔ _shotRange ↔ self._shot_range
SHOT_LIFETIME: Final = SHOT_RANGE / SHOT_SPEED_DEFAULT  # 3.5s (abgeleitet _shotRange/_shotSpeed)
                                           # ↔ self._shot_lifetime
MAX_SHOTS_DEFAULT: Final = 1                    # ↔ _maxShots / MsgGameSettings ↔ self._max_shots
SHOT_RADIUS: Final = 0.5                  # ↔ _shotRadius ↔ self._shot_radius
RELOAD_TIME_DEFAULT: Final = 3.5    # BZFlag-Default für _reloadTime (via MsgSetVar überschreibbar)
                             # ↔ _reloadTime ↔ self._reload_time

SHOCK_IN_RADIUS: Final = 6.0     # _shockInRadius (BZFlag default = _tankLength ≈ 6u) ↔ self._shock_in_radius
SHOCK_OUT_RADIUS: Final = 60.0    # _shockOutRadius ↔ self._shock_out_radius
SHOCK_AD_LIFE: Final = 0.2     # _shockAdLife: Multiplikator auf _reloadTime für effektive SW-Lebensdauer
                           # ↔ self._shock_ad_life
SW_EXPAND_SPEED: Final = (SHOCK_OUT_RADIUS - SHOCK_IN_RADIUS) / (SHOT_LIFETIME * SHOCK_AD_LIFE)  # Default ≈ 77 u/s
                           # (abgeleitet; ↔ self._sw_expand_speed, neu berechnet bei jeder Zutat)

GM_TURN_RATE: Final = 0.628319  # rad/s — BZFlag _gmTurnAngle ↔ self._gm_turn_angle
GM_ACTIVATION_TIME: Final = 0.5      # _gmActivationTime (s bis GM aktiv) ↔ self._gm_activation_time
GM_AD_LIFE: Final = 0.95     # _gmAdLife (× shotRange = GM-Reichweite) ↔ self._gm_ad_life
GM_LOCK_ON_ANGLE: Final = 0.15     # _lockOnAngle (rad; GM-Lock-on-Toleranz) ↔ self._lock_on_angle
# GM-Geradeaus-/Homing-Grenze (_gmActivationTime × _shotSpeed) ist KEINE Konstante mehr, sondern der
# nachgeführte Instanzwert self._gm_min_range (s. _recompute_gm_min_range) — folgt geänderten Server-Vars.

# ── Flaggen-Physik (via _on_set_var überschreibbar) ───────────────────────
FLAG_RADIUS: Final = 2.5      # _flagRadius (Server-Aufnahme-Distanz) ↔ self._flag_radius
FLAG_GRAB_RADIUS: Final = TANK_RADIUS * 2   # ~8.64 u (Bot-eigener Grab-Anfahrtsradius)
VELOCITY_AD: Final = 1.5      # _velocityAd   (× tankSpeed bei V-Flagge) ↔ self._velocity_ad
AGILITY_AD_VEL: Final = 2.25     # _agilityAdVel (× tankSpeed bei A-Flagge) ↔ self._agility_ad_vel
LG_GRAVITY: Final = 12.7     # _lgGravity (Schwerkraft bei LG-Flagge) ↔ self._lg_gravity
BURROW_DEPTH: Final = -1.32    # _burrowDepth (Bot-Z bei BU-Flagge) ↔ self._burrow_depth
BURROW_SPEED_AD: Final = 0.8      # _burrowSpeedAd (× tankSpeed bei BU) ↔ self._burrow_speed_ad
BURROW_ANG_AD: Final = 0.55     # _burrowAngularAd (× tankAngVel bei BU) ↔ self._burrow_ang_ad
ANGULAR_AD: Final = 1.5      # _angularAd (× tankAngVel bei QT-Flagge) ↔ self._angular_ad
SHIELD_FLIGHT: Final = 2.7      # _shieldFlight (SH-Flagge Flugzeit in s) ↔ self._shield_flight
IDENTIFY_RANGE: Final = 50.0     # _identifyRange (ID-Flagge Erkennungsradius) ↔ self._identify_range

OBESITY_FACTOR: Final = 2.5      # _obeseFactor ↔ self._obese_factor
_TINY_FACTOR: Final = 0.4    # T-Flagge: Tank auf 40% skaliert ↔ _tinyFactor ↔ self._tiny_factor
THIEF_TINY_FACTOR: Final = 0.5    # TH-Flagge: Tank auf 50% skaliert (via MsgSetVar _thiefTinyFactor)
                             # ↔ self._thief_tiny_factor
THIEF_VEL_AD: Final = 1.67   # TH-Flagge: Geschwindigkeit 1,67× (via MsgSetVar _thiefVelAd)
                             # ↔ self._thief_vel_ad
THIEF_AD_SHOT_VEL: Final = 8.0    # TH-Flagge: Schussgeschwindigkeit-Multiplikator (via MsgSetVar _thiefAdShotVel)
                             # ↔ self._thief_ad_shot_vel
THIEF_AD_LIFE: Final = 0.05   # TH-Flagge: Schuss-Lifetime in Sekunden (via MsgSetVar _thiefAdLife)
                             # ↔ self._thief_ad_life
THIEF_AD_RANGE: Final = 120.0  # TH-Flagge: Reichweite (HIX hw=30→60u Gebäude × 2)
_NARROW_HW: Final = 0.30   # N-Flagge: reduzierte Halbbreite ↔ _narrowHW ↔ self._narrow_hw
SR_RADIUS_MULT: Final = 0.8    # ↔ _srRadiusMult ↔ self._sr_radius_mult

# ── Schuss-Typ-Multiplikatoren (via _on_set_var überschreibbar) ───────────
MGUN_AD_RATE: Final = 10.0     # _mGunAdRate  (reloadTime / MGUN_AD_RATE = MG-Reload) ↔ self._mgun_ad_rate
MGUN_AD_LIFE: Final = 0.1      # _mGunAdLife  (= 1/_mGunAdRate; × shotLifetime) ↔ self._mgun_ad_life
MGUN_AD_VEL: Final = 1.5      # _mGunAdVel   (× shotSpeed = MG-Schussgeschwindigkeit) ↔ self._mgun_ad_vel
RFIRE_AD_RATE: Final = 2.0      # _rFireAdRate (F- und TR-Reload-Divisor) ↔ self._rfire_ad_rate
RFIRE_AD_VEL: Final = 1.5      # _rFireAdVel  (× shotSpeed = TR-Schussgeschwindigkeit) ↔ self._rfire_ad_vel
RFIRE_AD_LIFE: Final = 0.5      # _rFireAdLife (= 1/_rFireAdRate; × shotRange) ↔ self._rfire_ad_life
LASER_AD_VEL: Final = 1000.0   # _laserAdVel  (× shotSpeed ≈ Laser-Länge) ↔ self._laser_ad_vel
LASER_AD_RATE: Final = 0.5      # _laserAdRate (× _rFireAdRate = Laser-Reload-Faktor) ↔ self._laser_ad_rate
LASER_AD_LIFE: Final = 0.1      # _laserAdLife (Laser-Lifetime in s) ↔ self._laser_ad_life

# ── Timing & Raten ────────────────────────────────────────────────────────
UPDATE_RATE_HZ: Final = 60
SERVER_UPDATE_RATE_HZ: Final = 30     # obere Grenze; ↔ _updateThrottleRate ↔ self._server_update_interval
AI_RATE_HZ: Final = 10
SHOOT_INTERVAL_RANDOM_MAX: Final = 10.0   # Obergrenze für zufälliges Random-Shot-Intervall
MIN_BURST_INTERVAL: Final = 1.0    # Mindestabstand zwischen zwei Schüssen im Burst
GM_BURST_INTERVAL: Final = 2.0    # GM: längere Pause, nur 1 Rakete gleichzeitig im Flug
LANDING_DOUBLE_SHOT_DELAY: Final = 0.15   # LANDING_SHOT: Abstand zum menschlichen "Doppelklick"-Nachschuss
RESPAWN_DELAY: Final = 3.0
EXPLODE_TIME: Final = 5.0    # BZFlag-Default für _explodeTime: Dauer der Explosions-Animation,
                               # Aufwärts-Flugzeit des Wracks (_explodeTime derzeit NICHT nachgeführt)
ROUND_END_LINGER: Final = 6.0    # s nach Rundenende verbunden bleiben, damit der Bot in der
                               # Endstand-Tabelle sichtbar bleibt, bevor er trennt/reconnectet
TELEPORT_TIME: Final = 1.0    # BZDB_TELEPORTTIME-Default: PS_TELEPORTING-Dauer + Re-Trigger-Sperre (P3-NAV-02)

# ── Navigation & A* ───────────────────────────────────────────────────────
WP_TIMEOUT_BASE: Final = 3.0             # Basiszeit für Drehen/Anfahren/Sicherheit
WP_TIMEOUT_SCALE: Final = 0.3             # s pro Einheit Distanz (≈3.3 u/s effektiv)
WP_TIMEOUT_JUMP_BONUS: Final = 2.0             # Aufschlag für NAV_JUMP-Anfahrt-WPs
NAV_JUMP_Z_TOL: Final = 2.5             # max. Z-Abweichung bei NAV_JUMP-Landung
NAV_TELE_TIMEOUT: Final = 2.0             # max. Sekunden Direktanflug in die Tor-Mitte vor Abbruch
NAV_TELE_ENGAGE_DIST: Final = NAV_CELL_SIZE * 5.0  # nur engagen, wenn Tor-Mitte so nah ist (~20u)
NAV_TELE_COOLDOWN: Final = 8.0             # Sperre eines Tors nach fehlgeschlagener Querung
NAV_TELE_OVERSHOOT: Final = 4.0             # u über die Mitte hinaus anzielen → Tor-Ebene sicher queren
# P4-MOV-01: Early-Advance (glatte WP-Übergänge / Ecken-Glättung)
EARLY_ADVANCE_LOOKAHEAD: Final = NAV_CELL_SIZE * 10.0  # ≈40u: nur nahe Ecken glätten — hält query_segment
                                                # klein (≤2×2 Grid-Zellen) und die Plan-Abweichung begrenzt
EARLY_ADVANCE_FLOOR_STEP: Final = 2.0             # Abtast-Abstand Kanten-/Absturz-Check. Spalten < 2×Overhang
                                                # (~2.8u) überfährt der Tank physisch (Pixel-on); ab Breite
                                                # 2×Overhang + STEP ist ein Abtast-Treffer garantiert →
                                                # 2.0 hält das Blindfenster klein (max. ~16 Punktabfragen)
# P4-INF-01: Asynchrone Pfadplanung. Der Haupt-Thread plant schnell (Defaults 5k/125ms, ggf.
# Best-Effort-Teilpfad); dauerte das länger als NAV_ASYNC_TRIGGER_MS, läuft parallel in einem
# Zweit-Thread eine Vollsuche mit großen Limits, deren bessere Route (inkl. Treppen-Sprüngen)
# übernommen wird, sobald sie fertig ist — ohne die 60-Hz-Schleife zu blockieren.
NAV_ASYNC_TRIGGER_MS     = 100.0   # Haupt-Thread-Plan teurer als das → Zweit-Thread-Vollsuche starten
                                    # (bewusst NICHT Final — von tests/test_async_plan.py gepatcht, s. DEVELOPER.md)
NAV_ASYNC_MAX_EXPANSIONS: Final = 50000   # Expansionslimit der Hintergrund-Vollsuche
NAV_ASYNC_MAX_MS: Final = 5000.0  # Wall-Clock-Limit der Hintergrund-Vollsuche
NAV_ASYNC_RESYNC_TOL: Final = NAV_CELL_SIZE * 4.0  # Max. Abstand Bot ↔ Route, um das Async-Ergebnis noch zu übernehmen

# Proaktive Wand-Vorausschau im COMBAT-Direktmodus: trifft die Fahrtrichtung eine solide Wand in
# steilem Winkel (Einfallswinkel zur Oberfläche > NAV_WALL_STEEP_DEG), fährt der Bot nicht stur
# dagegen (Wall-Slide nullt dann den Vortrieb), sondern bevorzugt A*-Navigation um die Wand bzw.
# dreht auf die Wand-Tangente ab. Bis ~60° bleibt genug Tangentialanteil zum Entlanggleiten.
NAV_WALL_PROBE_DIST: Final = 20.0   # u Vorausschau entlang der Fahrtrichtung
NAV_WALL_STEEP_DEG: Final = 60.0   # Einfallswinkel zur Wandfläche darüber = "steil" → kein Gleiten mehr

STUCK_WINDOW: Final = 1.5
STUCK_MIN_DIST: Final = 3.0

# COMBAT-Stall-Watchdog: Direktmodus ohne Sicht (dünne Wand, kein Ricochet-Pfad) darf nicht ewig
# stehen. RANDOMISIERTES Fenster, damit zwei Bots sich nicht spiegeln und synchron festfrieren.
COMBAT_STALL_WIN_MIN: Final = 5.0    # s, Untergrenze des zufälligen Beobachtungsfensters
COMBAT_STALL_WIN_MAX: Final = 10.0   # s, Obergrenze
COMBAT_STALL_MIN_DIST: Final = 5.0    # u, weniger Netto-Bewegung im Fenster = Stall
COMBAT_STALL_REV_MIN: Final = 20.0   # u, minimale Rückwärts-Distanz des Unstick-Manövers
COMBAT_STALL_REV_MAX: Final = 60.0   # u, maximale Rückwärts-Distanz
COMBAT_STALL_WP_MIN: Final = 10     # min. WP-Distanz des Zufallspunkt-Manövers (× NAV_CELL_SIZE)
COMBAT_STALL_WP_MAX: Final = 20     # max. WP-Distanz (× NAV_CELL_SIZE = 4 → 20–40 u)
COMBAT_STALL_TIMEOUT: Final = 5.0    # s, Sicherheits-Timeout je Unstick-Manöver

# COMBAT-Eskalation, wenn ein per Sprung unerreichbarer (zu hoher) Gegner ohne A*-Pfad
# verfolgt wird — verhindert blindes Rammen der Wand und Einfrieren (Zyklus mit Früh-Ausstieg).
COMBAT_REPLAN_RETRY: Final = 1.0    # s, gedrosselter Hintergrund-Replan-Versuch zum Gegner
UNREACH_DIRECT_TIME: Final = 30.0   # s, Direktmodus-Fenster (Prio 2), bevor repositioniert wird
UNREACH_AVOID_TIME: Final = 30.0   # s, Re-Target deprioritisiert den unerreichbaren Gegner
UNREACH_AVOID_PENALTY: Final = 100.0  # Score-Multiplikator für gemiedene Ziele (weich, kein Hard-Skip)
UNREACH_REPOS_RADIUS: Final = 100.0  # u, Reposition-Distanz (Prio 3), frischer A*-Start
UNREACH_REPOS_TIMEOUT: Final = 8.0    # s, Sicherheits-Timeout fürs Abfahren der Reposition

# ── Kampf, Ausweichen & Schießen ─────────────────────────────────────────
OPTIMAL_RANGE: Final = 60.0
OPTIMAL_RANGE_MG: Final = 25.0   # MG-Schüsse laufen nach ~87u ab; aggressiver Nahkampf nötig
OPTIMAL_RANGE_SW: Final = 20.0   # SW-Killzone beginnt bei 6u; nahe heranfahren, dann zünden
OPTIMAL_RANGE_GM: Final = 100.0  # GM-Schüsse fliegen erst kurz geradeaus, bevor die Zielsuche beginnt. Abstand halten!

# COMBAT-Optimaldistanz-Deadzone (Controller-Deadzone-Analogie): innerhalb ±dieser Spanne um die
# Optimaldistanz NICHT vor/zurück regeln, sonst zittern zwei exakt distanzgleiche Bots umeinander.
COMBAT_DIST_DEADZONE: Final = 1.0    # u

JUMP_COOLDOWN: Final = 4.0
TACT_JUMP_CLEARANCE: Final = 1.5   # TactJump muss so weit tragen, dass der Bot 1.5× hinter dem Gegner landet
TACT_JUMP_REACTION_S: Final = 0.5   # Reaktionszeit des GEGNERS auf den Sprung (nicht DODGE_REACT_DELAY, das ist
                             # die Reaktion des Bots auf Schüsse): so lange wird fortgesetzte Annäherung
                             # noch gutgeschrieben — danach kann der Gegner gebremst/zurückgesetzt haben

DODGE_DIST: Final = TANK_RADIUS * 4.0   # 17.28
EVADE_CLEAR_GRACE: Final = 1.0                 # Sekunden, die ein als "sicher" eingestufter Schuss ignoriert wird
RICO_DODGE_LOOKAHEAD: Final = 2.0               # Maximaler Lookahead (s) für Ricochet-Bedrohungen
RICO_AIM_CACHE_TTL: Final = 1.0               # Cache-Gültigkeit (s) für offensiven Ricochet-Azimut
                                         # (P6: coarse-to-fine-Sweep ist deutlich schneller als der
                                         # alte 1°-Vollsweep → häufigeres Neuberechnen bezahlbar,
                                         # liefert aktuellere Zielwinkel bei bewegten Gegnern)
RICO_AIM_MAX: Final = 45                  # Maximaler Winkel für Suche nach Ricochet-Schüssen
TELE_AIM_Z_TOL     = 1.0                 # Z-Spielraum (u) NUR für Tor-Schüsse — gleicht die
                                         # Höhen-Skalierung der Tor-Transform aus; reiner Tuning-Knopf.
                                         # (bewusst NICHT Final — von tests/test_teleporter.py gepatcht)
INDIRECT_HOLD_S: Final = 5.0                 # max. Halt (s) zum Zielen eines Indirekt-Schusses
                                         # im Kletter-Fall (Rumpf-Drehung aufs Tor + 1–2 Schüsse).

HIT_RADIUS: Final = TANK_RADIUS * 1.3   # ~5.62u
DODGE_REACT_DELAY: Final = 0.2
IB_REACT_MULTIPLIER: Final = 1.1
M_REACT_MULTIPLIER: Final = 1.5
CS_REACT_MULTIPLIER: Final = 2.0   # Cloaked Shot (Gegenstück zu IB): out-the-window unsichtbar, aber auf
                            # Radar sichtbar → nur visuelle Bestätigung fehlt, daher kleiner als IB
ST_GM_PENALTY: Final = 4.0   # ST-Spieler bei GM: 4× schlechtere Priorität (kein Homing)

# ── Deckung (P4-TAC-02) ───────────────────────────────────────────────────
# Der Bot erkennt, dass er JETZT in Deckung steht, eine Tanklänge in Bewegungsrichtung aber
# nicht mehr (Deckungskante). Statt blind in den offenen Kampf zu fahren, hält er im State
# COVER_HOLD kurz und peekt gelegentlich. Kein aktives Anfahren, keine Pfadplanung.
COVER_EDGE_PROBE_DIST: Final = TANK_LENGTH   # Kanten-Probe: 1 Tanklänge voraus (6u)
COVER_HOLD_MAX_S: Final = 10.0   # max. Haltezeit als Fallback-Notausgang. Der kluge frühe Ausgang
                                 # ("Gegner-Slots leer", P4-TAC-05) greift meist deutlich vorher.
COVER_HOLD_COOLDOWN_S: Final = 2.5   # Sperre gegen sofortigen Wieder-Eintritt (Hysterese gegen Oszillation)
COVER_PEEK_CHANCE: Final = 0.15   # Peek-Wahrscheinlichkeit pro AI-Tick (10 Hz) im Halten
COVER_PEEK_OUT_S: Final = 0.25   # kurz vorfahren …
COVER_PEEK_BACK_S: Final = 0.40   # … und sofort rückwärts zurück (länger = sicher wieder hinter der Kante)
COVER_MAX_RANGE_FACTOR: Final = 1.0   # Halten nur, wenn Gegner < _shot_range * Faktor entfernt
# P4-TAC-05: das Gegner-Nachlade-Fenster muss mind. so groß sein, um Ausbruch/Angriff aus der
# Deckung zu rechtfertigen (sonst lohnt sich das Herauskommen nicht).
COVER_BREAKOUT_MIN_WINDOW_S: Final = 1.2
# Zu-nah-Exit: kommt der Gegner näher als optimale Distanz × Faktor, regelt COMBAT wieder Abstand.
COVER_CLOSE_EXIT_FRAC: Final = 0.5
RICO_AIM_MAX_COVER: Final = 90   # breiterer Abpraller-Sweep (°) in Deckung — im Stand ist Zeit dafür

# ── Wahrnehmung ───────────────────────────────────────────────────────────
AHEAD_HALF_ANGLE: Final = math.pi / 2  # ±90° — Geometrie „liegt vor mir" (kein Sicht-FoV, s. _is_ahead)
# Radar-Aufmerksamkeit: ohne direkten Sichtkontakt (FoV/LoS) bemerkt der Bot Bewegungen nur verzögert —
# pro Tick fällt der „Radar-Blick" mit dieser Wahrscheinlichkeit aus, danach für einen Cooldown ganz.
RADAR_SKIP_DEFAULT: Final = 0.33
RADAR_SKIP_CL: Final = 0.66  # CL: out-the-window unsichtbar + Schussgefahr → öfter weggeschaut
RADAR_COOLDOWN_DEFAULT: Final = 0.25  # s, nach einem Fehlschlag ganz „weggeschaut" (keine Radar-Updates)
RADAR_COOLDOWN_CL: Final = 0.75
# P7: LoS-Ergebnis pro Spieler im Update-Pfad cachen (statt Raycast pro MsgPlayerUpdate, 30 Hz ×
# Spieler). Bewusst deutlich länger als der Radar-Cooldown: reines Wahrnehmungs-Gate, die Staleness
# wirkt praktisch nur auf die Fenster-Sicht (ST-Träger) — radar-sichtbare Gegner werden bei LoS-Fehlschlag
# weiterhin über den Radar-Pfad aktualisiert.
PLAYER_LOS_TTL_S: Final = 0.75
ENEMY_STALE_S: Final = 10.0  # so lange ungesehen → Gegner gilt als verloren (kein Ziel-Re-Lock)
PAUSE_WAIT_S: Final = 12.0  # so lange wartet der Bot auf Rückkehr eines pausierten Ziels, dann SEEKING

RADAR_RANGE: Final = WORLD_HALF_DEFAULT   # bewusst HALBE Weltgröße (Fairness-Limit, s. F6 im
                                          # FABLE-PLAN); folgt _worldSize via _effective_radar_range
TARGET_FOV: Final = math.pi * 75 / 180  # 75°, ±37.5°
WIDE_ANGLE_ANG: Final = 1.745329             # ~100°, _wideAngleAng ↔ self._wide_angle_ang

# ── Protokoll-Encoding (MsgPlayerUpdateSmall-Skalierung) ─────────────────
SMALL_SCALE: Final = 32766.0
SMALL_MAX_DIST: Final = 0.02 * SMALL_SCALE
SMALL_MAX_VEL: Final = 0.01 * SMALL_SCALE
SMALL_MAX_ANGV: Final = 0.001 * SMALL_SCALE

# ── Kill-Reasons (MsgKilled) ──────────────────────────────────────────────
KILL_REASON_SHOT: Final = 1
KILL_REASON_RUNOVER: Final = 2
KILL_REASON_GENOCIDED: Final = 3

# ── Flaggen-Mengen & Namens-Tabelle ───────────────────────────────────────
GOOD_FLAGS_DEFAULT = {"GM", "SW", "L", "SB", "MG", "V", "SE", "ID",
                      "A", "BU", "G", "IB", "N", "QT", "SH", "T", "F", "JP", "WG",
                      "OO", "MQ", "TH", "R", "CL", "ST", "SR", "CS"}
BAD_FLAGS_DEFAULT  = {"NJ", "B", "RC", "O", "JM", "BY",
                      "CB", "FO", "LT", "M", "RO", "RT", "TR", "WA"}

# Vollname → Abkürzung (aus BZFlag Flag.cxx); für MsgNearFlag-Auswertung
FLAG_NAME_TO_ABBR: dict[str, str] = {
    "High Speed": "V",              "Quick Turn": "QT",
    "Oscillation Overthruster": "OO", "Rapid Fire": "F",
    "Machine Gun": "MG",            "Guided Missile": "GM",
    "Laser": "L",                   "Ricochet": "R",
    "Super Bullet": "SB",           "Invisible Bullet": "IB",
    "Cloaked Shot": "CS",  # spekulativ (existiert noch nicht im Server) — Gegenstück zu IB

    "Stealth": "ST",                "Tiny": "T",
    "Narrow": "N",                  "Shield": "SH",
    "Steamroller": "SR",            "Shock Wave": "SW",
    "Phantom Zone": "PZ",           "Genocide": "G",
    "Jumping": "JP",                "Identify": "ID",
    "Cloaking": "CL",               "Useless": "US",
    "Masquerade": "MQ",             "Seer": "SE",
    "Thief": "TH",                  "Burrow": "BU",
    "Wings": "WG",                  "Agility": "A",
    "ReverseControls": "RC",        "Colorblindness": "CB",
    "Obesity": "O",                 "Left Turn Only": "LT",
    "Right Turn Only": "RT",        "Forward Only": "FO",
    "ReverseOnly": "RO",            "Momentum": "M",
    "Blindness": "B",               "Jamming": "JM",
    "Wide Angle": "WA",             "No Jumping": "NJ",
    "Trigger Happy": "TR",          "Bouncy": "BY",
    "Red Team": "R*",               "Green Team": "G*",
    "Blue Team": "B*",              "Purple Team": "P*",
}

# Alles GROSSGESCHRIEBENE exportieren — inkl. der historisch _-präfixierten
# _TINY_FACTOR/_NARROW_HW (str.isupper ignoriert Unterstriche), die `import *`
# sonst überspringen würde. NAV_CELL_SIZE wird bewusst mit re-exportiert.
# STATISCHE Liste (früher dynamisch via `[_n for _n in globals() if _n.isupper()]`):
# mypy/mypyc können ein dynamisch berechnetes __all__ nicht auswerten, wodurch
# `from bot.constants import *` für die Typprüfung/Kompilierung leer bliebe. Beim
# Ergänzen einer neuen GROSSGESCHRIEBENEN Konstante hier mit aufnehmen.
__all__ = [
    'NAV_CELL_SIZE', 'TANK_LENGTH', 'TANK_WIDTH', 'TANK_HEIGHT', 'TANK_RADIUS_FACTOR',
    'TANK_RADIUS', 'TANK_HALF_LENGTH', 'TANK_HALF_DIAG', 'TANK_SPEED', 'TANK_TURN_RATE',
    'JUMP_VELOCITY', 'GRAVITY', 'WALL_HEIGHT_DEFAULT', 'WORLD_HALF_DEFAULT', 'MUZZLE_FRONT',
    'MUZZLE_HEIGHT', 'ON_TOP_EPS', 'SHOT_SPEED_DEFAULT', 'SHOT_RANGE', 'SHOT_LIFETIME',
    'MAX_SHOTS_DEFAULT', 'SHOT_RADIUS', 'RELOAD_TIME_DEFAULT', 'SHOCK_IN_RADIUS',
    'SHOCK_OUT_RADIUS', 'SHOCK_AD_LIFE', 'SW_EXPAND_SPEED', 'GM_TURN_RATE',
    'GM_ACTIVATION_TIME', 'GM_AD_LIFE', 'GM_LOCK_ON_ANGLE', 'FLAG_RADIUS', 'FLAG_GRAB_RADIUS',
    'VELOCITY_AD', 'AGILITY_AD_VEL', 'LG_GRAVITY', 'BURROW_DEPTH', 'BURROW_SPEED_AD',
    'BURROW_ANG_AD', 'ANGULAR_AD', 'SHIELD_FLIGHT', 'IDENTIFY_RANGE', 'OBESITY_FACTOR',
    '_TINY_FACTOR', 'THIEF_TINY_FACTOR', 'THIEF_VEL_AD', 'THIEF_AD_SHOT_VEL', 'THIEF_AD_LIFE',
    'THIEF_AD_RANGE', '_NARROW_HW', 'SR_RADIUS_MULT', 'MGUN_AD_RATE', 'MGUN_AD_LIFE',
    'MGUN_AD_VEL', 'RFIRE_AD_RATE', 'RFIRE_AD_VEL', 'RFIRE_AD_LIFE', 'LASER_AD_VEL',
    'LASER_AD_RATE', 'LASER_AD_LIFE', 'UPDATE_RATE_HZ', 'SERVER_UPDATE_RATE_HZ', 'AI_RATE_HZ',
    'SHOOT_INTERVAL_RANDOM_MAX', 'MIN_BURST_INTERVAL', 'GM_BURST_INTERVAL',
    'LANDING_DOUBLE_SHOT_DELAY', 'RESPAWN_DELAY', 'EXPLODE_TIME', 'ROUND_END_LINGER',
    'TELEPORT_TIME', 'WP_TIMEOUT_BASE', 'WP_TIMEOUT_SCALE', 'WP_TIMEOUT_JUMP_BONUS',
    'NAV_JUMP_Z_TOL', 'NAV_TELE_TIMEOUT', 'NAV_TELE_ENGAGE_DIST', 'NAV_TELE_COOLDOWN',
    'NAV_TELE_OVERSHOOT', 'NAV_ASYNC_TRIGGER_MS', 'NAV_ASYNC_MAX_EXPANSIONS',
    'NAV_ASYNC_MAX_MS', 'NAV_ASYNC_RESYNC_TOL', 'NAV_WALL_PROBE_DIST', 'NAV_WALL_STEEP_DEG',
    'STUCK_WINDOW', 'STUCK_MIN_DIST', 'COMBAT_STALL_WIN_MIN', 'COMBAT_STALL_WIN_MAX',
    'COMBAT_STALL_MIN_DIST', 'COMBAT_STALL_REV_MIN', 'COMBAT_STALL_REV_MAX',
    'COMBAT_STALL_WP_MIN', 'COMBAT_STALL_WP_MAX', 'COMBAT_STALL_TIMEOUT',
    'COMBAT_REPLAN_RETRY', 'UNREACH_DIRECT_TIME', 'UNREACH_AVOID_TIME',
    'UNREACH_AVOID_PENALTY', 'UNREACH_REPOS_RADIUS', 'UNREACH_REPOS_TIMEOUT', 'OPTIMAL_RANGE',
    'OPTIMAL_RANGE_MG', 'OPTIMAL_RANGE_SW', 'OPTIMAL_RANGE_GM', 'COMBAT_DIST_DEADZONE',
    'JUMP_COOLDOWN', 'TACT_JUMP_CLEARANCE', 'TACT_JUMP_REACTION_S', 'DODGE_DIST',
    'EVADE_CLEAR_GRACE', 'RICO_DODGE_LOOKAHEAD', 'RICO_AIM_CACHE_TTL', 'RICO_AIM_MAX',
    'TELE_AIM_Z_TOL', 'INDIRECT_HOLD_S', 'HIT_RADIUS', 'DODGE_REACT_DELAY',
    'IB_REACT_MULTIPLIER', 'M_REACT_MULTIPLIER', 'CS_REACT_MULTIPLIER', 'ST_GM_PENALTY',
    'COVER_EDGE_PROBE_DIST', 'COVER_HOLD_MAX_S', 'COVER_HOLD_COOLDOWN_S', 'COVER_PEEK_CHANCE',
    'COVER_PEEK_OUT_S', 'COVER_PEEK_BACK_S', 'COVER_MAX_RANGE_FACTOR',
    'COVER_BREAKOUT_MIN_WINDOW_S', 'COVER_CLOSE_EXIT_FRAC', 'RICO_AIM_MAX_COVER',
    'AHEAD_HALF_ANGLE', 'RADAR_SKIP_DEFAULT', 'RADAR_SKIP_CL', 'RADAR_COOLDOWN_DEFAULT',
    'RADAR_COOLDOWN_CL', 'PLAYER_LOS_TTL_S', 'ENEMY_STALE_S', 'PAUSE_WAIT_S', 'RADAR_RANGE',
    'TARGET_FOV', 'WIDE_ANGLE_ANG', 'SMALL_SCALE', 'SMALL_MAX_DIST', 'SMALL_MAX_VEL',
    'SMALL_MAX_ANGV', 'KILL_REASON_SHOT', 'KILL_REASON_RUNOVER', 'KILL_REASON_GENOCIDED',
    'GOOD_FLAGS_DEFAULT', 'BAD_FLAGS_DEFAULT', 'FLAG_NAME_TO_ABBR',
]
