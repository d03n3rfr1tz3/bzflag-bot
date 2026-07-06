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

from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE

# ── Tank-Physik & Geometrie (aus global.cxx + PlayerState.cxx verifiziert) ─
TANK_LENGTH         = 6.0                  # ↔ _tankLength ↔ self._tank_length
TANK_WIDTH          = 2.8                  # ↔ _tankWidth ↔ self._tank_width
TANK_HEIGHT         = 2.05                 # ↔ _tankHeight ↔ self._tank_height
TANK_RADIUS_FACTOR  = 0.72                 # _tankRadius = 0.72 * _tankLength (global.cxx)
TANK_RADIUS         = TANK_RADIUS_FACTOR * TANK_LENGTH   # 4.32 (_tankRadius; nicht nachgeführt —
                                           # an TANK_RADIUS hängen Modulkonstanten wie
                                           # HIT_RADIUS/DODGE_DIST/MUZZLE_FRONT und der
                                           # NavGrid-Pad, s. F7-Entscheidung im FABLE-PLAN)
TANK_HALF_LENGTH    = TANK_LENGTH / 2.0        # 3.0
TANK_HALF_DIAG      = math.sqrt(TANK_HALF_LENGTH ** 2 + (TANK_WIDTH / 2.0) ** 2)  # ≈3.31u
TANK_SPEED          = 25.0                 # ↔ _tankSpeed ↔ self._tank_speed
TANK_TURN_RATE      = 0.785398  # π/4 rad/s  ↔ _tankAngVel ↔ self._tank_turn_rate
JUMP_VELOCITY       = 19.0                 # ↔ _jumpVelocity ↔ self._jump_velocity
GRAVITY             = -9.8                 # ↔ _gravity ↔ self._gravity
WALL_HEIGHT_DEFAULT = 3.0 * TANK_HEIGHT    # _wallHeight = 3*_tankHeight (global.cxx)
                                           # ↔ _wallHeight/_tankHeight ↔ self._wall_height
WORLD_HALF_DEFAULT  = 400.0                # ↔ _worldSize/2 ↔ self.world_half
MUZZLE_FRONT  = TANK_RADIUS + 0.1   # 4.42  ↔ _muzzleFront ↔ self._muzzle_front
MUZZLE_HEIGHT = 1.57                #       ↔ _muzzleHeight ↔ self._muzzle_height
ON_TOP_EPS          = 0.1     # Toleranz (u), in der eine Bot-Basis als "bündig AUF einer Box"
                              # (= nicht innen) gilt — verhindert, dass ein fahrender Teleporter-
                              # Austritt auf der Mauer-Oberkante (z=Box-Top) fälschlich revertiert wird.

# ── Schuss ────────────────────────────────────────────────────────────────
SHOT_SPEED_DEFAULT  = 100.0                # ↔ _shotSpeed ↔ self._shot_speed
SHOT_RANGE          = 350.0                # ↔ _shotRange ↔ self._shot_range
SHOT_LIFETIME       = SHOT_RANGE / SHOT_SPEED_DEFAULT  # 3.5s (abgeleitet _shotRange/_shotSpeed)
                                           # ↔ self._shot_lifetime
MAX_SHOTS_DEFAULT   = 1                    # ↔ _maxShots / MsgGameSettings ↔ self._max_shots
SHOT_RADIUS         = 0.5                  # ↔ _shotRadius ↔ self._shot_radius
RELOAD_TIME_DEFAULT = 3.5    # BZFlag-Default für _reloadTime (via MsgSetVar überschreibbar)
                             # ↔ _reloadTime ↔ self._reload_time

SHOCK_IN_RADIUS  = 6.0     # _shockInRadius (BZFlag default = _tankLength ≈ 6u) ↔ self._shock_in_radius
SHOCK_OUT_RADIUS = 60.0    # _shockOutRadius ↔ self._shock_out_radius
SHOCK_AD_LIFE    = 0.2     # _shockAdLife: Multiplikator auf _reloadTime für effektive SW-Lebensdauer
                           # ↔ self._shock_ad_life
SW_EXPAND_SPEED  = (SHOCK_OUT_RADIUS - SHOCK_IN_RADIUS) / (SHOT_LIFETIME * SHOCK_AD_LIFE)  # Default ≈ 77 u/s
                           # (abgeleitet; ↔ self._sw_expand_speed, neu berechnet bei jeder Zutat)

GM_TURN_RATE        = 0.628319  # rad/s — BZFlag _gmTurnAngle ↔ self._gm_turn_angle
GM_ACTIVATION_TIME  = 0.5      # _gmActivationTime (s bis GM aktiv) ↔ self._gm_activation_time
GM_AD_LIFE          = 0.95     # _gmAdLife (× shotRange = GM-Reichweite) ↔ self._gm_ad_life
GM_LOCK_ON_ANGLE    = 0.15     # _lockOnAngle (rad; GM-Lock-on-Toleranz) ↔ self._lock_on_angle
# GM-Geradeaus-/Homing-Grenze (_gmActivationTime × _shotSpeed) ist KEINE Konstante mehr, sondern der
# nachgeführte Instanzwert self._gm_min_range (s. _recompute_gm_min_range) — folgt geänderten Server-Vars.

# ── Flaggen-Physik (via _on_set_var überschreibbar) ───────────────────────
FLAG_RADIUS         = 2.5      # _flagRadius (Server-Aufnahme-Distanz) ↔ self._flag_radius
FLAG_GRAB_RADIUS    = TANK_RADIUS * 2   # ~8.64 u (Bot-eigener Grab-Anfahrtsradius)
VELOCITY_AD         = 1.5      # _velocityAd   (× tankSpeed bei V-Flagge) ↔ self._velocity_ad
AGILITY_AD_VEL      = 2.25     # _agilityAdVel (× tankSpeed bei A-Flagge) ↔ self._agility_ad_vel
LG_GRAVITY          = 12.7     # _lgGravity (Schwerkraft bei LG-Flagge) ↔ self._lg_gravity
BURROW_DEPTH        = -1.32    # _burrowDepth (Bot-Z bei BU-Flagge) ↔ self._burrow_depth
BURROW_SPEED_AD     = 0.8      # _burrowSpeedAd (× tankSpeed bei BU) ↔ self._burrow_speed_ad
BURROW_ANG_AD       = 0.55     # _burrowAngularAd (× tankAngVel bei BU) ↔ self._burrow_ang_ad
ANGULAR_AD          = 1.5      # _angularAd (× tankAngVel bei QT-Flagge) ↔ self._angular_ad
SHIELD_FLIGHT       = 2.7      # _shieldFlight (SH-Flagge Flugzeit in s) ↔ self._shield_flight
IDENTIFY_RANGE      = 50.0     # _identifyRange (ID-Flagge Erkennungsradius) ↔ self._identify_range

OBESITY_FACTOR      = 2.5      # _obeseFactor ↔ self._obese_factor
_TINY_FACTOR        = 0.4    # T-Flagge: Tank auf 40% skaliert ↔ _tinyFactor ↔ self._tiny_factor
THIEF_TINY_FACTOR   = 0.5    # TH-Flagge: Tank auf 50% skaliert (via MsgSetVar _thiefTinyFactor)
                             # ↔ self._thief_tiny_factor
THIEF_VEL_AD        = 1.67   # TH-Flagge: Geschwindigkeit 1,67× (via MsgSetVar _thiefVelAd)
                             # ↔ self._thief_vel_ad
THIEF_AD_SHOT_VEL   = 8.0    # TH-Flagge: Schussgeschwindigkeit-Multiplikator (via MsgSetVar _thiefAdShotVel)
                             # ↔ self._thief_ad_shot_vel
THIEF_AD_LIFE       = 0.05   # TH-Flagge: Schuss-Lifetime in Sekunden (via MsgSetVar _thiefAdLife)
                             # ↔ self._thief_ad_life
THIEF_AD_RANGE      = 120.0  # TH-Flagge: Reichweite (HIX hw=30→60u Gebäude × 2)
_NARROW_HW          = 0.30   # N-Flagge: reduzierte Halbbreite ↔ _narrowHW ↔ self._narrow_hw
SR_RADIUS_MULT      = 0.8    # ↔ _srRadiusMult ↔ self._sr_radius_mult

# ── Schuss-Typ-Multiplikatoren (via _on_set_var überschreibbar) ───────────
MGUN_AD_RATE        = 10.0     # _mGunAdRate  (reloadTime / MGUN_AD_RATE = MG-Reload) ↔ self._mgun_ad_rate
MGUN_AD_LIFE        = 1.5      # _mGunAdLife  (× shotRange = MG-Reichweite) ↔ self._mgun_ad_life
MGUN_AD_VEL         = 0.1      # _mGunAdVel   (= 1/_mGunAdRate; × shotSpeed) ↔ self._mgun_ad_vel
RFIRE_AD_RATE       = 2.0      # _rFireAdRate (F- und TR-Reload-Divisor) ↔ self._rfire_ad_rate
RFIRE_AD_VEL        = 1.5      # _rFireAdVel  (× shotSpeed = TR-Schussgeschwindigkeit) ↔ self._rfire_ad_vel
RFIRE_AD_LIFE       = 0.5      # _rFireAdLife (= 1/_rFireAdRate; × shotRange) ↔ self._rfire_ad_life
LASER_AD_VEL        = 1000.0   # _laserAdVel  (× shotSpeed ≈ Laser-Länge) ↔ self._laser_ad_vel
LASER_AD_RATE       = 0.5      # _laserAdRate (× _rFireAdRate = Laser-Reload-Faktor) ↔ self._laser_ad_rate
LASER_AD_LIFE       = 0.1      # _laserAdLife (Laser-Lifetime in s) ↔ self._laser_ad_life

# ── Timing & Raten ────────────────────────────────────────────────────────
UPDATE_RATE_HZ        = 60
SERVER_UPDATE_RATE_HZ = 30     # obere Grenze; ↔ _updateThrottleRate ↔ self._server_update_interval
AI_RATE_HZ            = 10
SHOOT_INTERVAL_RANDOM_MAX = 10.0   # Obergrenze für zufälliges Random-Shot-Intervall
MIN_BURST_INTERVAL    = 1.0    # Mindestabstand zwischen zwei Schüssen im Burst
GM_BURST_INTERVAL     = 2.0    # GM: längere Pause, nur 1 Rakete gleichzeitig im Flug
RESPAWN_DELAY         = 3.0
EXPLODE_TIME          = 5.0    # BZFlag-Default für _explodeTime: Dauer der Explosions-Animation,
                               # Aufwärts-Flugzeit des Wracks (_explodeTime derzeit NICHT nachgeführt)
ROUND_END_LINGER      = 6.0    # s nach Rundenende verbunden bleiben, damit der Bot in der
                               # Endstand-Tabelle sichtbar bleibt, bevor er trennt/reconnectet
TELEPORT_TIME  = 1.0    # BZDB_TELEPORTTIME-Default: PS_TELEPORTING-Dauer + Re-Trigger-Sperre (P3-NAV-02)

# ── Navigation & A* ───────────────────────────────────────────────────────
WP_TIMEOUT_BASE       = 3.0             # Basiszeit für Drehen/Anfahren/Sicherheit
WP_TIMEOUT_SCALE      = 0.3             # s pro Einheit Distanz (≈3.3 u/s effektiv)
WP_TIMEOUT_JUMP_BONUS = 2.0             # Aufschlag für NAV_JUMP-Anfahrt-WPs
NAV_JUMP_Z_TOL        = 2.5             # max. Z-Abweichung bei NAV_JUMP-Landung
NAV_TELE_TIMEOUT      = 2.0             # max. Sekunden Direktanflug in die Tor-Mitte vor Abbruch
NAV_TELE_ENGAGE_DIST  = NAV_CELL_SIZE * 5.0  # nur engagen, wenn Tor-Mitte so nah ist (~20u)
NAV_TELE_COOLDOWN     = 8.0             # Sperre eines Tors nach fehlgeschlagener Querung
NAV_TELE_OVERSHOOT    = 4.0             # u über die Mitte hinaus anzielen → Tor-Ebene sicher queren
# P4-INF-01: Asynchrone Pfadplanung. Der Haupt-Thread plant schnell (Defaults 5k/125ms, ggf.
# Best-Effort-Teilpfad); dauerte das länger als NAV_ASYNC_TRIGGER_MS, läuft parallel in einem
# Zweit-Thread eine Vollsuche mit großen Limits, deren bessere Route (inkl. Treppen-Sprüngen)
# übernommen wird, sobald sie fertig ist — ohne die 60-Hz-Schleife zu blockieren.
NAV_ASYNC_TRIGGER_MS     = 100.0   # Haupt-Thread-Plan teurer als das → Zweit-Thread-Vollsuche starten
NAV_ASYNC_MAX_EXPANSIONS = 50000   # Expansionslimit der Hintergrund-Vollsuche
NAV_ASYNC_MAX_MS         = 5000.0  # Wall-Clock-Limit der Hintergrund-Vollsuche
NAV_ASYNC_RESYNC_TOL     = NAV_CELL_SIZE * 4.0  # Max. Abstand Bot ↔ Route, um das Async-Ergebnis noch zu übernehmen

# Proaktive Wand-Vorausschau im COMBAT-Direktmodus: trifft die Fahrtrichtung eine solide Wand in
# steilem Winkel (Einfallswinkel zur Oberfläche > NAV_WALL_STEEP_DEG), fährt der Bot nicht stur
# dagegen (Wall-Slide nullt dann den Vortrieb), sondern bevorzugt A*-Navigation um die Wand bzw.
# dreht auf die Wand-Tangente ab. Bis ~60° bleibt genug Tangentialanteil zum Entlanggleiten.
NAV_WALL_PROBE_DIST   = 20.0   # u Vorausschau entlang der Fahrtrichtung
NAV_WALL_STEEP_DEG    = 60.0   # Einfallswinkel zur Wandfläche darüber = "steil" → kein Gleiten mehr

STUCK_WINDOW          = 1.5
STUCK_MIN_DIST        = 3.0

# COMBAT-Stall-Watchdog: Direktmodus ohne Sicht (dünne Wand, kein Ricochet-Pfad) darf nicht ewig
# stehen. RANDOMISIERTES Fenster, damit zwei Bots sich nicht spiegeln und synchron festfrieren.
COMBAT_STALL_WIN_MIN  = 5.0    # s, Untergrenze des zufälligen Beobachtungsfensters
COMBAT_STALL_WIN_MAX  = 10.0   # s, Obergrenze
COMBAT_STALL_MIN_DIST = 5.0    # u, weniger Netto-Bewegung im Fenster = Stall
COMBAT_STALL_REV_MIN  = 20.0   # u, minimale Rückwärts-Distanz des Unstick-Manövers
COMBAT_STALL_REV_MAX  = 60.0   # u, maximale Rückwärts-Distanz
COMBAT_STALL_WP_MIN   = 10     # min. WP-Distanz des Zufallspunkt-Manövers (× NAV_CELL_SIZE)
COMBAT_STALL_WP_MAX   = 20     # max. WP-Distanz (× NAV_CELL_SIZE = 4 → 20–40 u)
COMBAT_STALL_TIMEOUT  = 5.0    # s, Sicherheits-Timeout je Unstick-Manöver

# COMBAT-Eskalation, wenn ein per Sprung unerreichbarer (zu hoher) Gegner ohne A*-Pfad
# verfolgt wird — verhindert blindes Rammen der Wand und Einfrieren (Zyklus mit Früh-Ausstieg).
COMBAT_REPLAN_RETRY   = 1.0    # s, gedrosselter Hintergrund-Replan-Versuch zum Gegner
UNREACH_DIRECT_TIME   = 30.0   # s, Direktmodus-Fenster (Prio 2), bevor repositioniert wird
UNREACH_AVOID_TIME    = 30.0   # s, Re-Target deprioritisiert den unerreichbaren Gegner
UNREACH_AVOID_PENALTY = 100.0  # Score-Multiplikator für gemiedene Ziele (weich, kein Hard-Skip)
UNREACH_REPOS_RADIUS  = 100.0  # u, Reposition-Distanz (Prio 3), frischer A*-Start
UNREACH_REPOS_TIMEOUT = 8.0    # s, Sicherheits-Timeout fürs Abfahren der Reposition

# ── Kampf, Ausweichen & Schießen ─────────────────────────────────────────
OPTIMAL_RANGE  = 60.0
OPTIMAL_RANGE_MG  = 25.0   # MG-Schüsse laufen nach ~87u ab; aggressiver Nahkampf nötig
OPTIMAL_RANGE_SW  = 20.0   # SW-Killzone beginnt bei 6u; nahe heranfahren, dann zünden
OPTIMAL_RANGE_GM  = 100.0  # GM-Schüsse fliegen erst kurz geradeaus, bevor die Zielsuche beginnt. Abstand halten!

# COMBAT-Optimaldistanz-Deadzone (Controller-Deadzone-Analogie): innerhalb ±dieser Spanne um die
# Optimaldistanz NICHT vor/zurück regeln, sonst zittern zwei exakt distanzgleiche Bots umeinander.
COMBAT_DIST_DEADZONE  = 1.0    # u

JUMP_COOLDOWN  = 4.0
TACT_JUMP_CLEARANCE  = 1.5   # TactJump muss so weit tragen, dass der Bot 1.5× hinter dem Gegner landet
TACT_JUMP_REACTION_S = 0.5   # Reaktionszeit des GEGNERS auf den Sprung (nicht DODGE_REACT_DELAY, das ist
                             # die Reaktion des Bots auf Schüsse): so lange wird fortgesetzte Annäherung
                             # noch gutgeschrieben — danach kann der Gegner gebremst/zurückgesetzt haben

DODGE_DIST          = TANK_RADIUS * 4.0   # 17.28
EVADE_CLEAR_GRACE   = 1.0                 # Sekunden, die ein als "sicher" eingestufter Schuss ignoriert wird
RICO_DODGE_LOOKAHEAD = 2.0               # Maximaler Lookahead (s) für Ricochet-Bedrohungen
RICO_AIM_CACHE_TTL   = 2.0               # Cache-Gültigkeit (s) für offensiven Ricochet-Azimut
RICO_AIM_MAX       = 45                  # Maximaler Winkel für Suche nach Ricochet-Schüssen
TELE_AIM_Z_TOL     = 1.0                 # Z-Spielraum (u) NUR für Tor-Schüsse — gleicht die
                                         # Höhen-Skalierung der Tor-Transform aus; reiner Tuning-Knopf.
INDIRECT_HOLD_S    = 5.0                 # max. Halt (s) zum Zielen eines Indirekt-Schusses
                                         # im Kletter-Fall (Rumpf-Drehung aufs Tor + 1–2 Schüsse).

HIT_RADIUS          = TANK_RADIUS * 1.3   # ~5.62u
DODGE_REACT_DELAY   = 0.2
IB_REACT_MULTIPLIER = 1.1
M_REACT_MULTIPLIER  = 1.5
CS_REACT_MULTIPLIER = 2.0   # Cloaked Shot (Gegenstück zu IB): out-the-window unsichtbar, aber auf
                            # Radar sichtbar → nur visuelle Bestätigung fehlt, daher kleiner als IB
ST_GM_PENALTY       = 4.0   # ST-Spieler bei GM: 4× schlechtere Priorität (kein Homing)

# ── Wahrnehmung ───────────────────────────────────────────────────────────
AHEAD_HALF_ANGLE    = math.pi / 2  # ±90° — Geometrie „liegt vor mir" (kein Sicht-FoV, s. _is_ahead)
# Radar-Aufmerksamkeit: ohne direkten Sichtkontakt (FoV/LoS) bemerkt der Bot Bewegungen nur verzögert —
# pro Tick fällt der „Radar-Blick" mit dieser Wahrscheinlichkeit aus, danach für einen Cooldown ganz.
RADAR_SKIP_DEFAULT     = 0.33
RADAR_SKIP_CL          = 0.66  # CL: out-the-window unsichtbar + Schussgefahr → öfter weggeschaut
RADAR_COOLDOWN_DEFAULT = 0.25  # s, nach einem Fehlschlag ganz „weggeschaut" (keine Radar-Updates)
RADAR_COOLDOWN_CL      = 0.75
# P7: LoS-Ergebnis pro Spieler im Update-Pfad cachen (statt Raycast pro MsgPlayerUpdate, 30 Hz ×
# Spieler). Bewusst deutlich länger als der Radar-Cooldown: reines Wahrnehmungs-Gate, die Staleness
# wirkt praktisch nur auf die Fenster-Sicht (ST-Träger) — radar-sichtbare Gegner werden bei LoS-Fehlschlag
# weiterhin über den Radar-Pfad aktualisiert.
PLAYER_LOS_TTL_S       = 0.75
ENEMY_STALE_S          = 10.0  # so lange ungesehen → Gegner gilt als verloren (kein Ziel-Re-Lock)
PAUSE_WAIT_S           = 12.0  # so lange wartet der Bot auf Rückkehr eines pausierten Ziels, dann SEEKING

RADAR_RANGE        = WORLD_HALF_DEFAULT   # bewusst HALBE Weltgröße (Fairness-Limit, s. F6 im
                                          # FABLE-PLAN); folgt _worldSize via _effective_radar_range
TARGET_FOV         = math.pi * 75 / 180  # 75°, ±37.5°
WIDE_ANGLE_ANG     = 1.745329             # ~100°, _wideAngleAng ↔ self._wide_angle_ang

# ── Protokoll-Encoding (MsgPlayerUpdateSmall-Skalierung) ─────────────────
SMALL_SCALE     = 32766.0
SMALL_MAX_DIST  = 0.02 * SMALL_SCALE
SMALL_MAX_VEL   = 0.01 * SMALL_SCALE
SMALL_MAX_ANGV  = 0.001 * SMALL_SCALE

# ── Kill-Reasons (MsgKilled) ──────────────────────────────────────────────
KILL_REASON_SHOT      = 1
KILL_REASON_RUNOVER   = 2
KILL_REASON_GENOCIDED = 3

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
__all__ = [_n for _n in list(globals()) if _n.isupper()]
