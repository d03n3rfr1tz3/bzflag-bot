"""Bewegungs- und KI-Logik für BZBot (Mixin).

Enthält AIState-Enum, BZBotAI-Mixin, Bewegungs- und Entscheidungsmethoden
sowie alle Spiel-Konstanten.

bzbot.py:  Protokoll, Netzwerk, Spielmechanik (Hit-Detection, Message-Handler)
bzbot_ai.py: KI-Strategie, State Machine, Bewegung
"""

import math
import random
import logging
import struct
import threading
import time
from enum import Enum, auto
from typing import Optional, Tuple

from bzflag.protocol import MsgGrabFlag, MsgShotBegin
from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE, JUMP_EDGE_TOL
from bzflag.shot_physics import (simulate_shot_path, can_ricochet,
                                  ray_teleporter_crossing, teleport_through,
                                  _segment_hits_obb_3d)

logger = logging.getLogger("bzbot")

# ── Konstanten (aus global.cxx + PlayerState.cxx verifiziert) ────────────
TANK_LENGTH         = 6.0
TANK_RADIUS         = 0.72 * TANK_LENGTH   # 4.32
TANK_SPEED          = 25.0
TANK_TURN_RATE      = 0.785398  # π/4 rad/s
FLAG_GRAB_RADIUS    = TANK_RADIUS * 2   # ~8.64 u
SHOT_SPEED_DEFAULT  = 100.0
SHOT_RANGE          = 350.0
SHOT_LIFETIME       = SHOT_RANGE / SHOT_SPEED_DEFAULT  # 3.5s
MAX_SHOTS_DEFAULT   = 1

JUMP_VELOCITY       = 19.0
GRAVITY             = -9.8

MUZZLE_FRONT  = TANK_RADIUS + 0.1   # 4.42
MUZZLE_HEIGHT = 1.57

SHOCK_IN_RADIUS  = 6.0     # _shockInRadius (BZFlag default = _tankLength ≈ 6u)
SHOCK_OUT_RADIUS = 60.0    # _shockOutRadius
SHOCK_AD_LIFE    = 0.2     # _shockAdLife: Multiplikator auf _reloadTime für effektive SW-Lebensdauer
SW_EXPAND_SPEED  = (SHOCK_OUT_RADIUS - SHOCK_IN_RADIUS) / (SHOT_LIFETIME * SHOCK_AD_LIFE)  # Default ≈ 77 u/s

OPTIMAL_RANGE  = 60.0
JUMP_COOLDOWN  = 4.0
TACT_JUMP_CLEARANCE  = 1.5   # TactJump muss so weit tragen, dass der Bot 1.5× hinter dem Gegner landet
TACT_JUMP_REACTION_S = 0.5   # Reaktionszeit des GEGNERS auf den Sprung (nicht DODGE_REACT_DELAY, das ist
                             # die Reaktion des Bots auf Schüsse): so lange wird fortgesetzte Annäherung
                             # noch gutgeschrieben — danach kann der Gegner gebremst/zurückgesetzt haben
# GM-Geradeaus-/Homing-Grenze (_gmActivationTime × _shotSpeed) ist KEINE Konstante mehr, sondern der
# nachgeführte Instanzwert self._gm_min_range (s. _recompute_gm_min_range) — folgt geänderten Server-Vars.
TELEPORT_TIME  = 1.0    # BZDB_TELEPORTTIME-Default: PS_TELEPORTING-Dauer + Re-Trigger-Sperre (P3-NAV-02)

UPDATE_RATE_HZ        = 60
SERVER_UPDATE_RATE_HZ = 30
AI_RATE_HZ            = 10
SHOOT_INTERVAL_RANDOM_MAX = 10.0   # Obergrenze für zufälliges Random-Shot-Intervall
MIN_BURST_INTERVAL    = 1.0    # Mindestabstand zwischen zwei Schüssen im Burst
GM_BURST_INTERVAL     = 2.0    # GM: längere Pause, nur 1 Rakete gleichzeitig im Flug
RELOAD_TIME_DEFAULT   = 3.5    # BZFlag-Default für _reloadTime (via MsgSetVar überschreibbar)
RESPAWN_DELAY         = 3.0
EXPLODE_TIME          = 5.0    # BZFlag-Default für _explodeTime: Dauer der Explosions-Animation,
                               # Aufwärts-Flugzeit des Wracks (via MsgSetVar überschreibbar)
ROUND_END_LINGER      = 6.0    # s nach Rundenende verbunden bleiben, damit der Bot in der
                               # Endstand-Tabelle sichtbar bleibt, bevor er trennt/reconnectet
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
NAV_ASYNC_TRIGGER_MS     = 50.0    # Haupt-Thread-Plan teurer als das → Zweit-Thread-Vollsuche starten
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
STUCK_MIN_DIST        = 2.0
WORLD_HALF_DEFAULT    = 400.0
ON_TOP_EPS            = 0.1     # Toleranz (u), in der eine Bot-Basis als "bündig AUF einer Box"
                               # (= nicht innen) gilt — verhindert, dass ein fahrender Teleporter-
                               # Austritt auf der Mauer-Oberkante (z=Box-Top) fälschlich revertiert wird.

# COMBAT-Eskalation, wenn ein per Sprung unerreichbarer (zu hoher) Gegner ohne A*-Pfad
# verfolgt wird — verhindert blindes Rammen der Wand und Einfrieren (Zyklus mit Früh-Ausstieg).
COMBAT_REPLAN_RETRY   = 1.0    # s, gedrosselter Hintergrund-Replan-Versuch zum Gegner
UNREACH_DIRECT_TIME   = 30.0   # s, Direktmodus-Fenster (Prio 2), bevor repositioniert wird
UNREACH_AVOID_TIME    = 30.0   # s, Re-Target deprioritisiert den unerreichbaren Gegner
UNREACH_AVOID_PENALTY = 100.0  # Score-Multiplikator für gemiedene Ziele (weich, kein Hard-Skip)
UNREACH_REPOS_RADIUS  = 100.0  # u, Reposition-Distanz (Prio 3), frischer A*-Start
UNREACH_REPOS_TIMEOUT = 8.0    # s, Sicherheits-Timeout fürs Abfahren der Reposition

SMALL_SCALE     = 32766.0
SMALL_MAX_DIST  = 0.02 * SMALL_SCALE
SMALL_MAX_VEL   = 0.01 * SMALL_SCALE
SMALL_MAX_ANGV  = 0.001 * SMALL_SCALE

DODGE_DIST          = TANK_RADIUS * 4.0   # 17.28
EVADE_CLEAR_GRACE   = 1.0                 # Sekunden, die ein als "sicher" eingestufter Schuss ignoriert wird
RICO_DODGE_LOOKAHEAD = 2.0               # Maximaler Lookahead (s) für Ricochet-Bedrohungen
RICO_AIM_CACHE_TTL   = 2.0               # Cache-Gültigkeit (s) für offensiven Ricochet-Azimut
RICO_AIM_MAX       = 45                  # Maximaler Winkel für Suche nach Ricochet-Schüssen
TELE_AIM_Z_TOL     = 1.0                 # Z-Spielraum (u) NUR für Tor-Schüsse — gleicht die
                                         # Höhen-Skalierung der Tor-Transform aus; reiner Tuning-Knopf.
INDIRECT_HOLD_S    = 5.0                 # max. Halt (s) zum Zielen eines Indirekt-Schusses
                                         # im Kletter-Fall (Rumpf-Drehung aufs Tor + 1–2 Schüsse).

TANK_HEIGHT         = 2.05
WALL_HEIGHT_DEFAULT = 3.0 * TANK_HEIGHT   # _wallHeight = 3*_tankHeight (global.cxx)
TANK_WIDTH          = 2.8
TANK_HALF_LENGTH    = TANK_LENGTH / 2.0        # 3.0
TANK_HALF_DIAG      = math.sqrt(TANK_HALF_LENGTH ** 2 + (TANK_WIDTH / 2.0) ** 2)  # ≈3.31u
_TINY_FACTOR        = 0.4    # T-Flagge: Tank auf 40% skaliert
THIEF_TINY_FACTOR   = 0.5    # TH-Flagge: Tank auf 50% skaliert (via MsgSetVar _thiefTinyFactor)
THIEF_VEL_AD        = 1.67   # TH-Flagge: Geschwindigkeit 1,67× (via MsgSetVar _thiefVelAd)
THIEF_AD_SHOT_VEL   = 8.0    # TH-Flagge: Schussgeschwindigkeit-Multiplikator (via MsgSetVar _thiefAdShotVel)
THIEF_AD_LIFE       = 0.05   # TH-Flagge: Schuss-Lifetime in Sekunden (via MsgSetVar _thiefAdLife)
THIEF_AD_RANGE      = 120.0  # TH-Flagge: Reichweite (HIX hw=30→60u Gebäude × 2)
_NARROW_HW          = 0.30   # N-Flagge: reduzierte Halbbreite
GM_TURN_RATE        = 0.628319  # rad/s — BZFlag _gmTurnAngle
GM_ACTIVATION_TIME  = 0.5      # _gmActivationTime (s bis GM aktiv)
GM_AD_LIFE          = 0.95     # _gmAdLife (× shotRange = GM-Reichweite)
GM_LOCK_ON_ANGLE    = 0.15     # _lockOnAngle (rad; GM-Lock-on-Toleranz)
SR_RADIUS_MULT      = 0.8
KILL_REASON_SHOT      = 1
KILL_REASON_RUNOVER   = 2
KILL_REASON_GENOCIDED = 3
OBESITY_FACTOR      = 2.5      # _obeseFactor
AHEAD_HALF_ANGLE    = math.pi / 2  # ±90° — Geometrie „liegt vor mir" (kein Sicht-FoV, s. _is_ahead)
# Radar-Aufmerksamkeit: ohne direkten Sichtkontakt (FoV/LoS) bemerkt der Bot Bewegungen nur verzögert —
# pro Tick fällt der „Radar-Blick" mit dieser Wahrscheinlichkeit aus, danach für einen Cooldown ganz.
RADAR_SKIP_DEFAULT     = 0.33
RADAR_SKIP_CL          = 0.66  # CL: out-the-window unsichtbar + Schussgefahr → öfter weggeschaut
RADAR_COOLDOWN_DEFAULT = 0.25  # s, nach einem Fehlschlag ganz „weggeschaut" (keine Radar-Updates)
RADAR_COOLDOWN_CL      = 0.5
ENEMY_STALE_S          = 10.0  # so lange ungesehen → Gegner gilt als verloren (kein Ziel-Re-Lock)
PAUSE_WAIT_S           = 12.0  # so lange wartet der Bot auf Rückkehr eines pausierten Ziels, dann SEEKING

SHOT_RADIUS         = 0.5
HIT_RADIUS          = TANK_RADIUS * 1.3   # ~5.62u
DODGE_REACT_DELAY   = 0.2
IB_REACT_MULTIPLIER = 1.1
M_REACT_MULTIPLIER  = 1.5
CS_REACT_MULTIPLIER = 2.0   # Cloaked Shot (Gegenstück zu IB): out-the-window unsichtbar, aber auf
                            # Radar sichtbar → nur visuelle Bestätigung fehlt, daher kleiner als IB
ST_GM_PENALTY       = 4.0   # ST-Spieler bei GM: 4× schlechtere Priorität (kein Homing)
RADAR_RANGE        = WORLD_HALF_DEFAULT
TARGET_FOV         = math.pi * 75 / 180  # 75°, ±37.5°
WIDE_ANGLE_ANG     = 1.745329             # ~100°, _wideAngleAng

# Flaggen-Physik (via _on_set_var überschreibbar)
FLAG_RADIUS         = 2.5      # _flagRadius (Server-Aufnahme-Distanz)
VELOCITY_AD         = 1.5      # _velocityAd   (× tankSpeed bei V-Flagge)
AGILITY_AD_VEL      = 2.25     # _agilityAdVel (× tankSpeed bei A-Flagge)
LG_GRAVITY          = 12.7     # _lgGravity (Schwerkraft bei LG-Flagge)
BURROW_DEPTH        = -1.32    # _burrowDepth (Bot-Z bei BU-Flagge)
BURROW_SPEED_AD     = 0.8      # _burrowSpeedAd (× tankSpeed bei BU)
BURROW_ANG_AD       = 0.55     # _burrowAngularAd (× tankAngVel bei BU)
ANGULAR_AD          = 1.5      # _angularAd (× tankAngVel bei QT-Flagge)
SHIELD_FLIGHT       = 2.7      # _shieldFlight (SH-Flagge Flugzeit in s)
IDENTIFY_RANGE      = 50.0     # _identifyRange (ID-Flagge Erkennungsradius)

# Schuss-Typ-Multiplikatoren (via _on_set_var überschreibbar)
MGUN_AD_RATE        = 10.0     # _mGunAdRate  (reloadTime / MGUN_AD_RATE = MG-Reload)
MGUN_AD_LIFE        = 1.5      # _mGunAdLife  (× shotRange = MG-Reichweite)
MGUN_AD_VEL         = 0.1      # _mGunAdVel   (= 1/_mGunAdRate; × shotSpeed)
RFIRE_AD_RATE       = 2.0      # _rFireAdRate (F- und TR-Reload-Divisor)
RFIRE_AD_VEL        = 1.5      # _rFireAdVel  (× shotSpeed = TR-Schussgeschwindigkeit)
RFIRE_AD_LIFE       = 0.5      # _rFireAdLife (= 1/_rFireAdRate; × shotRange)
LASER_AD_VEL        = 1000.0   # _laserAdVel  (× shotSpeed ≈ Laser-Länge)
LASER_AD_RATE       = 0.5      # _laserAdRate (× _rFireAdRate = Laser-Reload-Faktor)
LASER_AD_LIFE       = 0.1      # _laserAdLife (Laser-Lifetime in s)

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


# ── Winkel-Hilfsfunktionen ────────────────────────────────────────────────

def _angle_diff(target: float, current: float) -> float:
    """Kürzeste Winkeldifferenz in (-π, π]: Richtung von current nach target.
    Genau 180° gibt -π zurück (CW bevorzugt bei Halbkreis-Grenzfall)."""
    d = (target - current) % (2 * math.pi)
    return d - 2 * math.pi if d >= math.pi else d


def _wrap(a: float) -> float:
    """Normalisiert Winkel auf [-π, π]."""
    while a >  math.pi: a -= 2 * math.pi
    while a < -math.pi: a += 2 * math.pi
    return a


# ── State Machine ─────────────────────────────────────────────────────────

class AIState(Enum):
    DEAD         = auto()  # tot, wartet auf Spawn
    IDLE         = auto()  # passiv — keine Menschen auf Server
    SEEKING      = auto()  # aktiv — Ziel oder Flaggen suchen
    COMBAT       = auto()  # Kampf — Abstandshaltung + Schießen
    EVADING      = auto()  # Ausweichen — Schuss im Anflug (timer-basiert)
    JUMP_WINDUP  = auto()  # Übersprung-Wind-Up (committed, ~80–120ms)
    JUMPING      = auto()  # in der Luft (Physik-committed, kein AI-Block)
    Z_ATTACK     = auto()  # ZJ1-Höhenangriff: nur COMBAT→Z_ATTACK→COMBAT
    DODGE_JUMP   = auto()  # Ausweichsprung — defensiver Sprung gegen eingehenden Schuss
    LANDING_SHOT = auto()  # springenden Gegner auf Landepunkt anvisieren
    NAV_JUMP     = auto()  # Navigationssprung — auf/über Gebäude (Pfad-Ausführung)
    NAV_JUMP_ALIGN = auto()  # Vor NAV_JUMP: Tank auf Sprungziel-Azimuth ausrichten
    NAV_TELE     = auto()  # Endanflug: direkt in die Teleporter-Mitte fahren, bis Querung/Revert
    FALLING      = auto()  # Unkontrollierter Fall vom Dach (kein Lenken erlaubt)


class BZBotAI:
    """Mixin: Bewegungs- und KI-Logik für BZBot.

    Erlaubte Übergänge:
        DEAD         → IDLE / SEEKING      (Spawn-Event, via bzbot.py)
        IDLE         → SEEKING             (_has_presence: Mensch oder Observer da)
        IDLE         → EVADING             (_handle_threat: Bedrohung erkannt)
        IDLE         → DODGE_JUMP          (_handle_threat: Dodge nicht machbar)
        SEEKING      → IDLE                (not _has_presence: kein Mensch, kein Observer)
        SEEKING      → COMBAT              (Ziel vorhanden)
        SEEKING      → EVADING             (_handle_threat: Bedrohung erkannt)
        SEEKING      → DODGE_JUMP          (_handle_threat: Dodge nicht machbar)
        COMBAT       → SEEKING             (Ziel verloren)
        COMBAT       → EVADING             (_handle_threat: Bedrohung erkannt)
        COMBAT       → JUMP_WINDUP         (taktischer Übersprung, Wind-Up)
        COMBAT       → DODGE_JUMP          (_handle_threat: Dodge nicht machbar)
        COMBAT       → LANDING_SHOT        (Gegner springt, Fenster offen)
        COMBAT       → Z_ATTACK            (_check_z_attack_jump: Höhenangriff)
        EVADING      → COMBAT / SEEKING / IDLE  (Schuss vorbei oder dodge_until abgelaufen)
        JUMP_WINDUP  → JUMPING             (Wind-Up abgelaufen → _execute_jump)
        JUMPING      → COMBAT / SEEKING / IDLE  (_is_landed())
        Z_ATTACK     → COMBAT              (_is_landed() — immer COMBAT)
        DODGE_JUMP   → COMBAT / SEEKING / IDLE  (_is_landed())
        LANDING_SHOT → COMBAT              (Schuss abgefeuert / Fenster zu)
        LANDING_SHOT → EVADING             (Bedrohung von anderem Gegner)
        NAV_JUMP     → ANY                 (_is_landed() → _nav_jump_return_state)
        NAV_JUMP_ALIGN → NAV_JUMP          (Azimuth ≤5° ausgerichtet → _initiate_nav_jump)
        NAV_JUMP_ALIGN → ANY               (Timeout 5s → return_state + replan)
        ANY          → NAV_JUMP            (_advance_path: nächster WP auf anderer Etage)
        ANY          → NAV_JUMP_ALIGN      (_advance_path: Geometrie OK, Azimuth zu weit)
        ANY          → NAV_TELE            (_advance_path: Eingangs-WP erreicht, nächster WP = Tor-Austritt)
        NAV_TELE     → ANY                 (Querung ausgeführt, oder Timeout/Revert → Replan)
        ANY          → JUMPING             (BY-Flag-Bounce, via _run_physics)
        ANY          → DEAD                (Tod-Event, via bzbot.py)
    """

    # ── Transition ────────────────────────────────────────────────────────

    def _transition_to(self, state: AIState) -> None:
        """Setzt neuen AI-State; loggt den Übergang."""
        if self._ai_state == state:
            return
        _clear_on_exit = {AIState.COMBAT, AIState.SEEKING, AIState.IDLE}
        if (self._ai_state in _clear_on_exit
                and state in (AIState.SEEKING, AIState.IDLE)):
            self._nav_path = []
            self._nav_goal = None
        logger.info("[%s] State: %s → %s", self.callsign,
                    self._ai_state.name, state.name)
        self._ai_state = state

    def _ground_state(self) -> AIState:
        """Realer Boden-State je nach Lage: COMBAT (Ziel + Mensch da), sonst SEEKING/IDLE.

        Dient als sicherer Return-State, damit NAV_JUMP/NAV_JUMP_ALIGN nie auf sich selbst
        „aussteigen" (sonst No-Op-Transition in _transition_to → Endlosfalle)."""
        if self.target_player is not None and self._has_presence():
            return AIState.COMBAT
        if self._has_presence():
            return AIState.SEEKING
        return AIState.IDLE

    # ── Tank-Dimensions-Hilfsmethoden ────────────────────────────────────

    def _effective_half_width(self) -> float:
        """Aktuelle Tank-Halbbreite je nach Flagge (T/N verkleinern den Tank)."""
        if self.own_flag == "T":
            return (TANK_WIDTH / 2.0) * _TINY_FACTOR
        if self.own_flag == "N":
            return _NARROW_HW
        return TANK_WIDTH / 2.0

    def _effective_hit_radius(self) -> float:
        """Trefferradius gemäß BZFlag: 0.99 * tankRadius * scale. N-Flagge → OBB, gibt 0.0 zurück."""
        if self.own_flag == "N":
            return 0.0  # N-Flagge: OBB-Check in _resolve_incoming_shots; diese Funktion nicht verwendet
        if self.own_flag == "O":   scale = self._obese_factor
        elif self.own_flag == "T": scale = _TINY_FACTOR
        else:                       scale = 1.0
        return TANK_RADIUS * scale * 0.99

    def _effective_optimal_range(self) -> float:
        """Optimale Kampfdistanz je nach eigener Flagge UND Gegner-Flagge.
        Eingegrabener BU-Gegner ist immun gegen normale Schüsse (nur GM/SW treffen) und von
        jedem Tank überrollbar → ohne eigenes GM/SW auf Kontaktdistanz rammen (wie SR).
        MG und SW profitieren von Nahkampf: MG wegen kurzer Schuss-Reichweite,
        SW wegen Donut-Killzone (zu weit → treffen nur noch die äußere Grenze)."""
        tgt = self.players.get(self.target_player) if self.target_player is not None else None
        if (tgt is not None and tgt.flag == "BU" and tgt.pos[2] < 0.0
                and self.own_flag not in ("GM", "SW")):
            return TANK_RADIUS * SR_RADIUS_MULT   # eingegrabener Gegner (z<0): Ramm-Kontaktdistanz
        if self.own_flag == "MG":
            return 25.0   # MG-Schüsse laufen nach ~87u ab; aggressiver Nahkampf nötig
        if self.own_flag == "SW":
            return 20.0   # SW-Killzone beginnt bei 6u; nahe heranfahren, dann zünden
        if self.own_flag == "SR":
            return TANK_RADIUS * SR_RADIUS_MULT  # Kontaktdistanz für Ramm-Kill
        if self.own_flag == "GM":
            return 85.0
        return OPTIMAL_RANGE

    # ── Physik-Block (60 Hz, immer) ───────────────────────────────────────

    def _run_physics(self, dt: float, now: float) -> None:
        """Grundlegende Spielphysik: Schwerkraft (off-ground) + Bounce-Flag (BY).
        Läuft jeden Tick unabhängig vom AI-State."""
        # BY: Auto-Bounce alle 0.2s
        if (self.own_flag == "BY" and not self._jumping
                and self.pos[2] <= 0.1 and now >= self._bounce_next):
            self.vel[2] = random.uniform(0.25, 1.0) * self._jump_velocity
            # BY-01: Horizontalrichtung aus aktuellem Azimuth — nicht aus altem vel[0/1]
            h_speed = math.hypot(self.vel[0], self.vel[1])
            if h_speed < 1.0:
                h_speed = self._tank_speed * 0.5
            self.vel[0] = math.cos(self.azimuth) * h_speed
            self.vel[1] = math.sin(self.azimuth) * h_speed
            self._jumping = True
            self._jump_ang_vel = 0.0  # BZFlag: keine Steuerung in der Luft
            self._bounce_next = now + 0.2
            self._transition_to(AIState.JUMPING)

        # Schwerkraft für nicht-springende Tanks über dem Boden.
        # _get_floor_z liefert den flaggen-korrekten Boden: 0.0 Weltboden / ≥0 Gebäudedach;
        # BU sinkt nur AM BODEN auf BURROW_DEPTH (−1.32u), nicht auf Dächern; OO → immer 0.0.
        # Schwelle 1e-6 statt 0: verhindert Dead-Zone durch Floating-Point-Artefakte.
        _floor_z = self._get_floor_z()
        if not self._jumping and self.pos[2] > _floor_z + 1e-6:
            self.vel[2] = max(self.vel[2] + self._effective_gravity() * dt, -self._tank_speed)
            self.pos[2] = max(self.pos[2] + self.vel[2] * dt, _floor_z)
            if self.pos[2] <= _floor_z + 1e-6:
                self.pos[2] = _floor_z
                self.vel[2] = 0.0

    # ── Landungs-Prüfung ──────────────────────────────────────────────────

    def _is_landed(self) -> bool:
        """True wenn Bot auf dem Boden (oder einer Gebäude-Oberfläche) steht.
        Nur beim Abstieg (vel[2] <= 0.1) prüfen — kein Früh-Landen beim Aufstieg."""
        if self.vel[2] > 0.1:
            return False
        return self.pos[2] <= self._get_floor_z() + 0.1

    def _get_floor_z(self) -> float:
        """Höchste Bodenfläche unterhalb des Bots; 0.0 wenn kein NavGraph.

        Pixel-on-Auflage: der Tank bleibt getragen, bis seine Mitte ~eine Tank-Halbbreite über
        die Kante hinaus ist (overhang). So fällt der Bot nicht schon, wenn die Mitte die Kante
        überquert — entscheidend für Sprung-Anläufe am Plattformrand.

        Flaggen-Boden zentral hier: OO phast durch Gebäude → landet/fällt immer auf den Weltboden
        (z=0). BU gräbt sich NUR am Boden ein (auf einem Dach trägt das Dach, also nur dort sinkt
        der Bot auf BURROW_DEPTH)."""
        if self.own_flag == "OO":
            return 0.0
        # P4a: Per-Tick-Memo (3–5 identische Aufrufe pro 60-Hz-Tick). Der Key
        # enthält Position+Flagge → Aufrufe NACH einer pos-Mutation im selben
        # Tick treffen einen neuen Key; Ergebnis bleibt verhaltensidentisch.
        memo = getattr(self, "_tick_memo", None)
        key = ("floor", self.pos[0], self.pos[1], self.pos[2], self.own_flag)
        if memo is not None:
            cached = memo.get(key)
            if cached is not None:
                return cached
        nav = getattr(self, "_nav_graph", None)
        floor = 0.0 if nav is None else nav.get_floor_z(
            self.pos[0], self.pos[1], self.pos[2], overhang=self._effective_half_width())
        if self.own_flag == "BU" and floor <= 0.0:
            floor = self._burrow_depth
        if memo is not None:
            memo[key] = floor
        return floor

    # ── Capability-Checks ─────────────────────────────────────────────────

    def _can_shoot(self) -> bool:
        """Basis-Voraussetzungen für Schuss: Netzwerk aktiv, eingeloggt, Debug-Flag."""
        if getattr(self, '_debug_no_shoot', False): return False
        if not self.client.udp_active:              return False
        if self.player_id is None:                  return False
        return True

    def _can_jump(self, now: float) -> bool:
        """Prüft alle Sprung-Voraussetzungen: physikalisch, Flagge, Cooldown, Debug-Flag."""
        if getattr(self, '_debug_no_jump', False):   return False
        if self._dodging:                             return False
        if self._jumping:
            if self.own_flag != "WG":                return False
            return self._wings_jumps_used < self._wings_jump_count - 1
        if not self._is_landed():                     return False
        if self.own_flag in ("NJ", "BU"):            return False
        if not self._server_jumping and self.own_flag not in ("WG", "BY", "JP"):
            return False
        if now - self._last_jump_at < JUMP_COOLDOWN: return False
        return True

    def _can_move_forward(self)  -> bool: return self.own_flag != "RO"
    def _can_move_backward(self) -> bool: return self.own_flag != "FO"
    def _can_turn_left(self)     -> bool: return self.own_flag != "RT"
    def _can_turn_right(self)    -> bool: return self.own_flag != "LT"

    def _is_foe(self, info: "PlayerInfo", in_sight: bool) -> bool:
        """Wahrnehmungsbasierte Feind-Erkennung — respektiert CB und MQ wie ein echter Spieler."""
        my = self.team if self.team not in (0xFFFF, 0xFFFE) else 0
        if my == 0:
            return True
        if self.own_flag == "CB":
            return True
        if not in_sight and info.flag == "MQ" and self.own_flag != "SE":
            return False
        return my != info.team

    def _genocide_multikill_possible(self) -> bool:
        """True wenn min. ein Feind-Team > 1 lebenden Spieler hat (G-Flagge lohnt sich)."""
        team_alive: dict = {}
        # list()-Snapshot: players/flags werden im Recv-Thread mutiert (Join/Leave,
        # Flag-Updates), die KI läuft im Game-Loop-Thread → Iterationen über diese
        # Dicts hier und im Rest der Datei immer über eine Kopie (Konvention s. DEVELOPER.md).
        for pid, info in list(self.players.items()):
            if pid == self.player_id or not info.alive:
                continue
            if not self._is_foe(info, True):
                continue
            team_alive[info.team] = team_alive.get(info.team, 0) + 1
        return any(c > 1 for c in team_alive.values())

    def _effective_reload_time(self) -> float:
        """Reload-Zeit je nach aktiver Flagge."""
        if self.own_flag == "MG":
            return self._reload_time / max(self._mgun_ad_rate, 1.0)
        if self.own_flag == "F":
            return self._reload_time / max(self._rfire_ad_rate, 1.0)
        return self._reload_time

    def _effective_shot_speed(self) -> float:
        """Schussgeschwindigkeit (u/s) der aktiven Flagge (BZFlag: vel *= AdVel)."""
        f = self.own_flag
        if f == "L":  return self._shot_speed * self._laser_ad_vel
        if f == "MG": return self._shot_speed * self._mgun_ad_vel
        if f == "F":  return self._shot_speed * self._rfire_ad_vel
        if f == "TH": return self._shot_speed * self._thief_ad_shot_vel
        return self._shot_speed                       # GM + Normal: Basis-Geschwindigkeit

    def _effective_shot_lifetime(self) -> float:
        """Schuss-Lebensdauer (s) der aktiven Flagge (BZFlag: lifetime *= AdLife)."""
        f = self.own_flag
        if f == "L":  return self._shot_lifetime * self._laser_ad_life
        if f == "MG": return self._shot_lifetime * self._mgun_ad_life
        if f == "F":  return self._shot_lifetime * self._rfire_ad_life
        if f == "TH": return self._shot_lifetime * self._thief_ad_life
        if f == "GM": return self._shot_lifetime * self._gm_ad_life
        return self._shot_lifetime

    def _effective_shot_range(self) -> float:
        """Maximale Schuss-Reichweite (u) der aktiven Flagge = eff_speed · eff_lifetime."""
        return self._effective_shot_speed() * self._effective_shot_lifetime()

    def _effective_tank_speed(self) -> float:
        # M (Momentum) bewusst NICHT modelliert — M ist Inertie (Beschleunigungs-Limit
        # lin≤20·_momentumLinAcc, ang≤_momentumAngAcc), nicht Top-Speed/Drehrate. Der Bot rechnet
        # Velocity instantan (für alle Tanks) und wirft M nach ~shakeTimeout (~1s) wieder ab.
        if self.own_flag == "V":   return self._tank_speed * self._velocity_ad
        if self.own_flag == "TH":  return self._tank_speed * self._thief_vel_ad
        if self.own_flag == "A" and math.hypot(self.vel[0], self.vel[1]) < 1.0:
            return self._tank_speed * self._agility_ad_vel
        if self.own_flag == "BU" and self.pos[2] < 0.0:  # Malus nur eingegraben (am Boden), nicht auf Dächern
            return self._tank_speed * self._burrow_speed_ad
        return self._tank_speed

    def _travel_tank_speed(self) -> float:
        """Nachhaltige Vorwärts-Reisegeschwindigkeit für Sprung-Planung UND -Ausführung.

        Nur dauerhaft während der Fahrt wirkende Flaggen (V/TH); A (nur Stillstand) und BU (nur
        eingegraben) werden bewusst ignoriert — beim Sprung-Anlauf fährt der Tank (vel>1) am Boden
        (z≥0), dort liefern sie ohnehin Basisgeschwindigkeit. Stabil (keine transienten Sprünge im
        Wert) → Planer (nav.plan_path(tank_speed=…)) und reaktiver Executor (needed_hspeed,
        _nav_jump_feasible/_geometry_ok) rechnen deckungsgleich: der Bot plant keinen Sprung, den er
        dann zu langsam ausführt. Siehe _effective_tank_speed für den (transienten) Live-Wert."""
        if self.own_flag == "V":  return self._tank_speed * self._velocity_ad
        if self.own_flag == "TH": return self._tank_speed * self._thief_vel_ad
        return self._tank_speed

    def _effective_turn_rate(self) -> float:
        # M nicht modelliert — siehe _effective_tank_speed (Inertie, nicht Drehrate; ~1s gehalten).
        if self.own_flag == "QT":  return self._tank_turn_rate * self._angular_ad
        if self.own_flag == "BU" and self.pos[2] < 0.0:  # Malus nur eingegraben (am Boden)
            return self._tank_turn_rate * self._burrow_ang_ad
        return self._tank_turn_rate

    def _effective_gravity(self) -> float:
        # WG: Wings nutzen im Flug _wingsGravity (BZFlag LocalPlayer.cxx). _wings_gravity is None
        # → kein Server-Override → BZDB-Default ist der Ausdruck "_gravity".
        if self.own_flag == "WG" and self._wings_gravity is not None:
            return self._wings_gravity
        # LG ist neutral (sofort gedroppt) → dieser Zweig ist praktisch toter Code; Formel
        # nicht gegen BZFlag verifiziert (LG im Quellcode nicht vorhanden). Siehe BUGS LG-01.
        if self.own_flag == "LG":
            return self._gravity * (self._lg_gravity / 100.0)
        return self._gravity

    def _effective_jump_velocity(self) -> float:
        # WG: Wings springen mit _wingsJumpVelocity (BZFlag doJump). _wings_jump_velocity is None
        # → kein Server-Override → BZDB-Default ist der Ausdruck "_jumpVelocity".
        if self.own_flag == "WG" and self._wings_jump_velocity is not None:
            return self._wings_jump_velocity
        return self._jump_velocity

    def _effective_jump_height(self) -> float:
        """Maximale Höhe eines Einzelsprungs unter der aktuell wirksamen Schwerkraft/Jump-Velocity
        (WG/LG-bewusst). Eine Quelle der Wahrheit für alle Sprunghöhen-Checks."""
        v = self._effective_jump_velocity()
        return v * v / (2.0 * abs(self._effective_gravity()))

    def _jump_launch_vz(self, cur_vz: float) -> float:
        """Vertikale Velocity direkt nach einem Sprung — faithful zu LocalPlayer.cxx doJump()
        (Z. 1449-1467). WG (Wings): additiv beim Fallen (bremst den Fall nur ab), behält höhere
        Steig-Velocity bei; alle anderen (Normal/JM): fester Wert. Eine Quelle der Wahrheit für
        jede Sprung-Velocity-Zuweisung."""
        v = self._effective_jump_velocity()
        if self.own_flag == "WG":
            if cur_vz < 0.0:
                return v + cur_vz
            if cur_vz > v:
                return cur_vz
        return v

    def _next_slot_ready(self, now: float) -> bool:
        """True wenn der nächste Slot (Zyklus-Reihenfolge) seinen Reload abgewartet hat."""
        while len(self._slot_reload_at) < self._max_shots:
            self._slot_reload_at.append(0.0)
        return now >= self._slot_reload_at[(self._shot_slot + 1) % self._max_shots]

    def _set_next_shoot_after_fire(self, now: float) -> None:
        """Setzt _next_shoot nach einem Schuss: kurzes Burst-Intervall wenn weiterer
        Slot bereit, sonst voller Reload."""
        _eff = self._effective_reload_time()
        _burst = GM_BURST_INTERVAL if self.own_flag == "GM" else MIN_BURST_INTERVAL
        _gap = min(_eff, _burst)
        while len(self._slot_reload_at) < self._max_shots:
            self._slot_reload_at.append(0.0)
        _ns = (self._shot_slot + 1) % self._max_shots
        if self._slot_reload_at[_ns] <= now:
            self._next_shoot = now + _gap
        else:
            self._next_shoot = self._slot_reload_at[_ns]

    def _apply_movement_caps(self, speed: float, ang_vel: float):
        """Wendet Fahrt- und Drehbeschränkungen durch Flaggen an (FO/RO/LT/RT)."""
        if not self._can_move_forward():  speed   = min(0.0, speed)
        if not self._can_move_backward(): speed   = max(0.0, speed)
        if not self._can_turn_left():     ang_vel = min(0.0, ang_vel)
        if not self._can_turn_right():    ang_vel = max(0.0, ang_vel)
        return speed, ang_vel

    def _has_presence(self) -> bool:
        """True, wenn mindestens ein MENSCH (Mitspieler ODER Zuschauer) anwesend ist.

        Leitet die Anwesenheit direkt aus der Spielerliste ab (robust gegen Zähler-Drift):
        jeder Eintrag, dessen Callsign KEIN Bot ist (eigener Name, Manager-Liste, Prefix),
        ist ein Mensch — egal ob aktiver Mitspieler oder reiner Zuschauer (Observer). Eigene
        Bots (Peer-Tanks, der Manager-Fallback-Observer) zählen NICHT als Anwesenheit; nur
        menschliche Anwesenheit lässt die Tanks aus dem IDLE-Modus wechseln."""
        return any(not self._is_bot_callsign(p.callsign) for p in list(self.players.values()))

    def _can_drive_through_obstacles(self) -> bool:
        """True wenn Bot mit aktueller Flagge durch Hindernisse fahren darf (OO u.a.)."""
        return self.own_flag in ("OO",)

    def _has_teleporters(self) -> bool:
        """True wenn die Karte Teleporter hat (→ indirekte Schüsse auch ohne Ricochet möglich)."""
        wm = getattr(self, "_world_map", None)
        return bool(wm and wm.teleporters)

    def _cross_floor_indirect(self, info) -> bool:
        """True, wenn der Gegner auf einer per Flachschuss unerreichbaren Etage steht und ein
        indirekter (Teleporter-/Abpraller-)Schuss in Frage kommt. Bewusst NICHT an die Sprunghöhe
        gekoppelt — ein Teleporter-Schuss überbrückt jede Etagen-Differenz (NAV-Vorgabe: sicherer
        als ein Z-Sprung)."""
        if info is None or self.own_flag in ("GM", "SW"):
            return False
        if abs(info.pos[2] - self.pos[2]) <= HIT_RADIUS:
            return False                       # gleiche Höhe → Direktschuss reicht
        return self._has_teleporters() or self._server_ricochet

    def _is_inside_obstacle(self, include_oo: bool = False) -> bool:
        """True wenn Bot physisch innerhalb eines Gebäudes steht (echte Geometrie, kein A*-Margin)."""
        if self._can_drive_through_obstacles() and not include_oo:
            return False
        world_map = getattr(self, '_world_map', None)
        if world_map is None:
            return False
        px, py, pz = self.pos[0], self.pos[1], self.pos[2]
        for obs in world_map.boxes:
            if obs.drive_through:
                continue
            tank_top = pz + self._tank_height
            # pz >= Box-Oberkante (− ON_TOP_EPS): der Bot steht bündig AUF der Box (nicht innen) —
            # z.B. ein FAHRENDER Teleporter-Austritt landet exakt auf der Mauer-Oberkante (z=Box-Top).
            # Mit strikt `>` würde das als "innen" gewertet und der Teleport revertiert (Bot steckt fest).
            if tank_top <= obs.bottom_z or pz >= obs.bottom_z + obs.height - ON_TOP_EPS:
                continue
            dx = px - obs.cx
            dy = py - obs.cy
            cos_a = obs.cos_a
            sin_a = obs.sin_a
            lx = dx * cos_a + dy * sin_a
            ly = -dx * sin_a + dy * cos_a
            if abs(lx) <= obs.half_w and abs(ly) <= obs.half_d:
                return True
        return False

    # ── Haupt-Dispatch (60 Hz) ────────────────────────────────────────────

    def _update_movement(self, dt: float, now: float, ai_tick: bool = True) -> None:
        """Bewegungs-Wrapper (60 Hz): State-Dispatch + zentraler Teleporter-Querungs-Check.

        Der Crossing-Check läuft pathing-unabhängig in JEDEM Tick und für JEDEN State (wie die
        Hitbox-Detection) — auch wenn _dispatch_movement früh `return`t. So wird ein Teleporter
        auch per Direktpfad, Bounce oder TactJump-Sprung-Arc korrekt durchquert (P3-NAV-02)."""
        old = (self.pos[0], self.pos[1], self.pos[2])
        self._dispatch_movement(dt, now, ai_tick)
        self._check_teleport_crossing(old, now)

    def _dispatch_movement(self, dt: float, now: float, ai_tick: bool = True) -> None:
        """Physik (60 Hz) + KI (10 Hz): State-Machine-Dispatch."""
        half = self.world_half

        # Grundphysik läuft immer
        self._run_physics(dt, now)

        # FALLING-Erkennung: Bodenstates merken nicht dass sie vom Dach gefallen sind.
        # Nur beim Abwärts-Fallen (vel[2] < -0.1) und tatsächlich in der Luft.
        _GROUND_STATES = (AIState.COMBAT, AIState.SEEKING, AIState.IDLE,
                          AIState.EVADING, AIState.LANDING_SHOT)
        if (self._ai_state in _GROUND_STATES
                and not self._jumping
                and self.vel[2] < -0.1
                and self.pos[2] > self._get_floor_z() + 0.5):
            self._pre_fall_state = self._ai_state
            self._jump_ang_vel = self.ang_vel   # Boden-Drehrate in Fall-Physik übertragen
            self._jumping = True   # verhindert Schwerkraft-Dopplung in _run_physics
            self._transition_to(AIState.FALLING)
            return  # diese Tick: Physik fertig, nächster Tick übernimmt _tick_falling

        if self._ai_state in (AIState.JUMPING, AIState.DODGE_JUMP):
            self._tick_jumping(dt, now)
            return

        if self._ai_state == AIState.NAV_JUMP:
            self._tick_nav_jump(dt, now)
            return

        if self._ai_state == AIState.NAV_JUMP_ALIGN:
            self._tick_nav_jump_align(dt, now)
            return

        if self._ai_state == AIState.NAV_TELE:
            self._tick_nav_tele(dt, now)
            return

        if self._ai_state == AIState.Z_ATTACK:
            self._tick_z_attack(dt, now)
            return

        if self._ai_state == AIState.FALLING:
            self._tick_falling(dt, now)
            return

        if self._ai_state in (AIState.EVADING, AIState.JUMP_WINDUP):
            self._tick_committed(dt, now)
            return

        if self._ai_state == AIState.LANDING_SHOT:
            if ai_tick:
                self._tick_landing_shot(now)
            if not self._jumping:
                # Position halten; aktiv auf Landepunkt drehen
                self.vel[0] = 0.0
                self.vel[1] = 0.0
                if self._landing_aim_pos is not None:
                    ax, ay = self._landing_aim_pos
                    target_az = math.atan2(ay - self.pos[1], ax - self.pos[0])
                    max_turn = self._tank_turn_rate * dt
                    diff = _angle_diff(target_az, self.azimuth)
                    self.ang_vel = math.copysign(
                        min(abs(diff / max(dt, 1e-6)), self._tank_turn_rate), diff)
                    self.azimuth = _wrap(
                        self.azimuth + math.copysign(min(abs(diff), max_turn), diff))
                else:
                    self.ang_vel = 0.0
            return

        # IDLE / SEEKING / COMBAT (10 Hz KI-Tick)
        if ai_tick:
            # Fertige Async-Vollsuche (P4-INF-01) vor dem State-Tick übernehmen — nur in
            # navigierbaren Bodenstates (NAV_JUMP/NAV_TELE/FALLING returnen vorher).
            self._poll_async_plan()
            if self._ai_state == AIState.IDLE:
                self._tick_idle(now)
            elif self._ai_state == AIState.SEEKING:
                self._tick_seeking(now)
            elif self._ai_state == AIState.COMBAT:
                self._tick_combat(now)

        # 60 Hz Bewegung
        if not self._jumping:
            if self._ai_state == AIState.COMBAT:
                self._execute_combat_move(dt, half, now)
            elif self._ai_state not in (AIState.JUMP_WINDUP, AIState.EVADING,
                                        AIState.JUMPING, AIState.DODGE_JUMP,
                                        AIState.LANDING_SHOT, AIState.NAV_JUMP_ALIGN):
                self._move_to_target(dt, half)

    # ── JUMPING-Tick ──────────────────────────────────────────────────────

    def _tick_jumping(self, dt: float, now: float) -> None:
        """Sprungphysik (BZFlag: in der Luft keine Steuerung). LocalPlayer.cxx Z. 364-368."""
        self.vel[2] += self._gravity * dt
        self.pos[2] += self.vel[2] * dt
        self.azimuth = _wrap(self.azimuth + self._jump_ang_vel * dt)
        if not self._can_drive_through_obstacles():
            self._apply_obstacle_bounds(dt)
        self.pos[0] += self.vel[0] * dt
        self.pos[1] += self.vel[1] * dt
        # Weltgrenzen-Clamp (kein Bounce im Sprung)
        half = self.world_half
        self.pos[0] = max(-half, min(half, self.pos[0]))
        self.pos[1] = max(-half, min(half, self.pos[1]))

        # WG: zusätzlicher Luftsprung beim Abwärtsbogen. Faithful zu doJump(): im Fallen wird die
        # Velocity nur additiv angehoben (v + vz, hier vz<0), kein voller neuer Bogen.
        if self.own_flag == "WG" and self.vel[2] < 0 and self._can_jump(now):
            self.vel[2] = self._jump_launch_vz(self.vel[2])
            self._wings_jumps_used += 1

        if self._is_landed():
            self.pos[2] = self._get_floor_z()
            self.vel[2] = 0.0
            self._jumping = False
            self._wings_jumps_used = 0
            self._last_jump_at = now
            self.ang_vel = 0.0
            if self.target_player is not None and self._has_presence():
                self._transition_to(AIState.COMBAT)
            elif self._has_presence():
                self._transition_to(AIState.SEEKING)
            else:
                self._transition_to(AIState.IDLE)

    # ── Explosions-Tick ──────────────────────────────────────────────────

    def _tick_explosion(self, dt: float) -> None:
        """Integriert den Explosions-Bogen des Wracks (tot, PS_EXPLODING) — spiegelt explodeTank:
        Aufwärts-Velocity unter Schwerkraft, Horizontal-Momentum bleibt; bei Bodenkontakt liegen
        bleiben (vel[2]=0). Die Explosion läuft optisch bis _exploding_until weiter."""
        floor_z = self._get_floor_z()
        self.vel[2] += self._gravity * dt
        self.pos[2] = max(self.pos[2] + self.vel[2] * dt, floor_z)
        if self.pos[2] <= floor_z + 1e-6:
            self.pos[2] = floor_z
            self.vel[2] = 0.0
        self.pos[0] += self.vel[0] * dt
        self.pos[1] += self.vel[1] * dt
        half = self.world_half
        self.pos[0] = max(-half, min(half, self.pos[0]))
        self.pos[1] = max(-half, min(half, self.pos[1]))

    # ── NAV_JUMP-Tick ────────────────────────────────────────────────────

    def _tick_nav_jump(self, dt: float, now: float) -> None:
        """Navigationssprung-Physik. Landet auf Ziel-Etage → return_state."""
        self.vel[2] += self._gravity * dt
        self.pos[2] += self.vel[2] * dt
        self.azimuth = _wrap(self.azimuth + self._jump_ang_vel * dt)   # Lande-Drehung (am Absprung fixiert)
        if not self._can_drive_through_obstacles():
            self._apply_obstacle_bounds(dt)
        self.pos[0] += self.vel[0] * dt
        self.pos[1] += self.vel[1] * dt
        half = self.world_half
        self.pos[0] = max(-half, min(half, self.pos[0]))
        self.pos[1] = max(-half, min(half, self.pos[1]))

        if self._is_landed():
            floor_z = self._get_floor_z()
            self.pos[2] = floor_z
            self.vel[2] = 0.0
            self._jumping = False
            self._wings_jumps_used = 0
            self._last_jump_at = now
            self.ang_vel = 0.0
            ret = getattr(self, "_nav_jump_return_state", AIState.SEEKING)
            if ret in (AIState.NAV_JUMP, AIState.NAV_JUMP_ALIGN):
                ret = self._ground_state()   # nie auf sich selbst zurück (Endlosfalle)
            if abs(floor_z - getattr(self, "_nav_jump_target_z", floor_z)) > 1.5:
                # Falsche Etage gelandet → Route verwerfen und neu planen
                self._nav_path = []
                self._nav_goal = None
                self._transition_to(ret)
                self._new_target()
                return
            self._transition_to(ret)
            self._advance_path()

    # ── NAV_JUMP_ALIGN-Tick ───────────────────────────────────────────────

    def _tick_nav_jump_align(self, dt: float, now: float) -> None:
        """Richtet Bot auf Sprungziel-Azimuth aus; wechselt dann zu NAV_JUMP."""
        wp  = getattr(self, '_nav_jump_align_wp', None)
        ret = getattr(self, '_nav_jump_align_return_state', AIState.SEEKING)
        if ret in (AIState.NAV_JUMP, AIState.NAV_JUMP_ALIGN):
            ret = self._ground_state()   # nie auf sich selbst zurück (Endlosfalle)
        if wp is None:
            self._transition_to(ret)
            return
        if now - getattr(self, '_nav_jump_align_start', now) > 5.0:
            wp_key = (round(wp[0]), round(wp[1]), wp[2])
            self._nav_jump_cooldowns[wp_key] = now + 30.0
            self._nav_jump_cooldowns = {k: v for k, v in self._nav_jump_cooldowns.items() if v > now}
            self._nav_path = []
            self._nav_goal = None
            self.target_pos = None
            self._transition_to(ret)
            return
        az_to_wp = math.atan2(wp[1] - self.pos[1], wp[0] - self.pos[0])
        diff = _angle_diff(az_to_wp, self.azimuth)
        max_turn = self._tank_turn_rate * dt
        self.ang_vel = math.copysign(
            min(abs(diff / max(dt, 1e-6)), self._tank_turn_rate), diff)
        self.azimuth = _wrap(self.azimuth + math.copysign(min(abs(diff), max_turn), diff))
        self.vel[0] = 0.0
        self.vel[1] = 0.0
        if abs(diff) <= math.pi / 36:
            self._initiate_nav_jump(wp)

    # ── NAV_TELE-Tick ─────────────────────────────────────────────────────

    def _tick_nav_tele(self, dt: float, now: float) -> None:
        """Fährt das letzte kurze Stück direkt in die Teleporter-Mitte, bis der zentrale
        _check_teleport_crossing (im _update_movement-Wrapper, nach diesem Tick) quert — oder
        bis Timeout/Revert. Ersetzt das Anfahren des mittenseitigen Austritts-WP, an dem der Bot
        sonst (Reichweite erreicht) davor stehen blieb."""
        ret = getattr(self, "_nav_tele_return_state", None) or self._ground_state()
        if ret in (AIState.NAV_TELE, AIState.NAV_JUMP, AIState.NAV_JUMP_ALIGN):
            ret = self._ground_state()
        center = getattr(self, "_nav_tele_center", None)
        # Erfolg: der Wrapper-Crossing-Check hat im vorherigen Tick gewarpt (→ _teleporting_until).
        if now < getattr(self, "_teleporting_until", 0.0):
            logger.info("[%s] NAV_TELE: Querung erfolgreich → %s", self.callsign, ret.name)
            self._nav_tele_center = None
            self._transition_to(ret)
            return
        # Abbruch: Timeout deckt auch den Revert ab (bei _is_inside_obstacle setzt der Crossing-
        # Check _teleporting_until NICHT → kein Erfolg → nach ≤NAV_TELE_TIMEOUT Abbruch).
        if center is None or now - getattr(self, "_nav_tele_start", now) > NAV_TELE_TIMEOUT:
            if center is not None:
                self._nav_tele_cooldowns[(round(center[0]), round(center[1]))] = now + NAV_TELE_COOLDOWN
            logger.info("[%s] NAV_TELE: Abbruch (Timeout/blockiert) → Cooldown + Replan", self.callsign)
            self._nav_tele_center = None
            self._nav_path = []
            self._nav_goal = None
            self.target_pos = None
            self._transition_to(ret)
            return
        # Direktfahrt: auf Mitte + Overshoot zielen (Overshoot → dünne Tor-Ebene sicher queren).
        cx, cy = center
        ddx, ddy = cx - self.pos[0], cy - self.pos[1]
        d = math.hypot(ddx, ddy) or 1.0
        aim_x = cx + (ddx / d) * NAV_TELE_OVERSHOOT
        aim_y = cy + (ddy / d) * NAV_TELE_OVERSHOOT
        target_az = math.atan2(aim_y - self.pos[1], aim_x - self.pos[0])
        diff = _angle_diff(target_az, self.azimuth)
        max_turn = self._tank_turn_rate * dt
        self.ang_vel = math.copysign(
            min(abs(diff / max(dt, 1e-6)), self._tank_turn_rate), diff)
        self.azimuth = _wrap(self.azimuth + math.copysign(min(abs(diff), max_turn), diff))
        speed = self._effective_tank_speed() if abs(diff) < math.pi / 2 else 0.0
        self.vel[0] = math.cos(self.azimuth) * speed
        self.vel[1] = math.sin(self.azimuth) * speed
        self._apply_bounds(dt, self.world_half)
        if getattr(self, "_debug_log_tele", False):
            _t = time.monotonic()
            if _t - getattr(self, "_debug_nav_tele_t", 0.0) > 0.25:
                self._debug_nav_tele_t = _t
                logger.debug(
                    "[%s] NAV_TELE: pos=(%.1f,%.1f,%.1f) →Mitte(%.1f,%.1f) dist=%.1fu "
                    "spd=%.1f az=%.0f° innen=%s",
                    self.callsign, self.pos[0], self.pos[1], self.pos[2], cx, cy, d,
                    speed, math.degrees(self.azimuth), self._is_inside_obstacle())

    # ── Z_ATTACK-Tick ─────────────────────────────────────────────────────

    def _tick_z_attack(self, dt: float, now: float) -> None:
        """ZJ1-Sprungphysik. Nur aus COMBAT erreichbar; Landung → immer COMBAT."""
        self.vel[2] += self._gravity * dt
        self.pos[2] += self.vel[2] * dt
        self.azimuth = _wrap(self.azimuth + self._jump_ang_vel * dt)
        if not self._can_drive_through_obstacles():
            self._apply_obstacle_bounds(dt)
        self.pos[0] += self.vel[0] * dt
        self.pos[1] += self.vel[1] * dt
        half = self.world_half
        self.pos[0] = max(-half, min(half, self.pos[0]))
        self.pos[1] = max(-half, min(half, self.pos[1]))

        if self._z_attack_mode:
            if abs(self.pos[2] - self._z_attack_fire_z) < 1.5:
                if self._next_shoot <= now and self._next_slot_ready(now):
                    _shoot = True
                    if self.target_player is not None:
                        _ep = self._get_enemy_pos(self.target_player)
                        if _ep is not None:
                            _az_to_enemy = math.atan2(_ep[1] - self.pos[1], _ep[0] - self.pos[0])
                            if abs(_angle_diff(self.azimuth, _az_to_enemy)) > math.radians(15):
                                _shoot = False
                    if _shoot and self._can_shoot():
                        self._send_shot(now, self.azimuth)
                        self._set_next_shoot_after_fire(now)
                        self._z_attack_mode = False  # nur nach gefeuertem Schuss deaktivieren
                    # schlechter Winkel → nächster Tick versucht erneut (Modus bleibt aktiv)

        if self._is_landed():
            self.pos[2] = self._get_floor_z()
            self.vel[2] = 0.0
            self._jumping = False
            self._wings_jumps_used = 0
            self._last_jump_at = now
            self._z_attack_mode = False
            self.ang_vel = 0.0
            self._transition_to(AIState.COMBAT)

    # ── FALLING-Tick ──────────────────────────────────────────────────────

    def _tick_falling(self, dt: float, now: float) -> None:
        """Fall-Physik für unkontrollierten Fall vom Dach (analog _tick_jumping).
        Kein Lenken: vel[0]/vel[1] und azimuth bleiben committed.
        _jump_ang_vel wird nicht zurückgesetzt — bestehende Drehbewegung bleibt."""
        self.vel[2] += self._gravity * dt
        self.pos[2] += self.vel[2] * dt
        self.azimuth = _wrap(self.azimuth + self._jump_ang_vel * dt)
        if not self._can_drive_through_obstacles():
            self._apply_obstacle_bounds(dt)
        self.pos[0] += self.vel[0] * dt
        self.pos[1] += self.vel[1] * dt
        half = self.world_half
        self.pos[0] = max(-half, min(half, self.pos[0]))
        self.pos[1] = max(-half, min(half, self.pos[1]))

        if self._is_landed():
            self.pos[2] = self._get_floor_z()
            self.vel[2] = 0.0
            self._jumping = False
            self._wings_jumps_used = 0
            # _last_jump_at nicht setzen — kein echter Sprung, kein Cooldown
            self.ang_vel = 0.0
            self._transition_to(self._pre_fall_state)

    # ── EVADING / JUMP_WINDUP-Tick ────────────────────────────────────────

    def _tick_committed(self, dt: float, now: float) -> None:
        """Führt aktiven Dodge aus. Keine neuen KI-Entscheidungen.

        JUMP_WINDUP: kein Abbruch bei Bedrohung. Nur Notschuss möglich,
        Sprung wird trotzdem ausgeführt (Entscheidung steht)."""
        half = self.world_half

        if self._ai_state == AIState.JUMP_WINDUP:
            incoming, _ = self._find_incoming_shot(now)
            if incoming is not None:
                t_impact = incoming.time_to_closest(self.pos[0], self.pos[1])
                if t_impact < 0.1 and self.client.udp_active and self.player_id is not None:
                    self._send_shot(now, self.azimuth)
                    if getattr(self, '_debug_log_shot', False):
                        logger.debug("[%s] Schuss: Notschuss während Wind-Up (t=%.2fs)",
                                     self.callsign, t_impact)

        # Fix E1/EV1: EVADING früh beenden nur wenn Schuss auch für alle typischen
        # Bewegungsrichtungen ungefährlich ist (verhindert Fehlausstieg durch Dodge-Velocity).
        if self._ai_state == AIState.EVADING:
            fwd_vx = math.cos(self.azimuth) * self._tank_speed
            fwd_vy = math.sin(self.azimuth) * self._tank_speed
            if (self._find_incoming_shot(now)[0] is None
                    and self._find_incoming_shot(now, bot_vel=(0.0, 0.0))[0] is None
                    and self._find_incoming_shot(now, bot_vel=(fwd_vx, fwd_vy))[0] is None
                    and self._find_incoming_shot(now, bot_vel=(-fwd_vx, -fwd_vy))[0] is None):
                self._dodging = False
                self._dodge_forward = False
                self._dodge_reverse = False
                
                # Fix EV2: Per-Schuss-Grace — denselben Schuss 1 s ignorieren damit nach
                # dem Early-Exit weder EVADING noch DODGE_JUMP neu ausgelöst werden.
                if self._last_threat_id is not None:
                    self._evade_cleared_shots[self._last_threat_id] = now + EVADE_CLEAR_GRACE
                self._last_threat_id = None
                
                if getattr(self, '_debug_log_dodge', False):
                    logger.debug("[%s] Ausweichen: Bedrohung vorbei – frühzeitiger EVADING-Exit", self.callsign)
                if self.target_player is not None and self._has_presence():
                    self._transition_to(AIState.COMBAT)
                elif self._has_presence():
                    self._transition_to(AIState.SEEKING)
                else:
                    self._transition_to(AIState.IDLE)
                return

        if self._dodging and now < self._dodge_until:
            if self._dodge_reverse:
                if self.own_flag == "OO" and self._is_inside_obstacle(include_oo=True):
                    speed = 0.0
                else:
                    self.ang_vel = 0.0
                    speed = -self._tank_speed * 0.5
            elif self._dodge_forward:
                self.ang_vel = 0.0
                speed = self._tank_speed
            else:
                diff = _angle_diff(self._dodge_dir, self.azimuth)
                max_turn = self._tank_turn_rate * dt
                self.ang_vel = math.copysign(
                    min(abs(diff / max(dt, 1e-6)), self._tank_turn_rate), diff)
                self.azimuth = _wrap(
                    self.azimuth + math.copysign(min(abs(diff), max_turn), diff))
                speed = self._tank_speed
            self.vel[0] = math.cos(self.azimuth) * speed
            self.vel[1] = math.sin(self.azimuth) * speed
            self._apply_bounds(dt, half)
            return

        # Timer abgelaufen
        self._dodging = False
        self._dodge_forward = False
        self._dodge_reverse = False

        # Fix EV2: Per-Schuss-Grace — denselben Schuss 1 s ignorieren damit nach
        # dem Early-Exit weder EVADING noch DODGE_JUMP neu ausgelöst werden.
        if self._last_threat_id is not None:
            self._evade_cleared_shots[self._last_threat_id] = now + EVADE_CLEAR_GRACE
        self._last_threat_id = None
        
        if self._ai_state == AIState.JUMP_WINDUP:
            if self._jump_pending:
                self._execute_jump()
        else:
            if self.target_player is not None and self._has_presence():
                self._transition_to(AIState.COMBAT)
            elif self._has_presence():
                self._transition_to(AIState.SEEKING)
            else:
                self._transition_to(AIState.IDLE)

    def _execute_jump(self) -> None:
        """Führt Sprung nach Wind-Up-Ende aus (JUMP_WINDUP → JUMPING)."""
        if self._escape_jump_ang_vel is not None:
            self._jump_ang_vel = self._escape_jump_ang_vel
            self._escape_jump_ang_vel = None
            if getattr(self, '_debug_log_dodge', False):
                logger.debug("[%s] Ausweichen: Escape-Sprung ang_vel=%.2f az=%.1f°",
                             self.callsign, self._jump_ang_vel, math.degrees(self.azimuth))
        else:
            if self.target_player is not None:
                _ep2 = self._get_enemy_pos(self.target_player)
                if _ep2 is not None:
                    self.azimuth = math.atan2(
                        _ep2[1] - self.pos[1], _ep2[0] - self.pos[0])
            self._jump_ang_vel = self.ang_vel
            if getattr(self, '_debug_log_dodge', False):
                logger.debug("[%s] Ausweichen: Frontal-Sprung ang_vel=%.2f az=%.1f°",
                             self.callsign, self._jump_ang_vel, math.degrees(self.azimuth))
        self.vel[0] = math.cos(self.azimuth) * self._tank_speed
        self.vel[1] = math.sin(self.azimuth) * self._tank_speed
        self.vel[2] = self._jump_launch_vz(self.vel[2])
        self._jumping = True
        self._jump_pending = False
        self._transition_to(AIState.JUMPING)

    # ── LANDING_SHOT-Tick ─────────────────────────────────────────────────

    def _tick_landing_shot(self, now: float) -> None:
        """KI während LANDING_SHOT: Azimuth auf Landepunkt; nur Bedrohung von anderen prüfen.
        Bewegung (vel=0) und Azimuth-Drehung werden in _update_movement (60Hz) gehandhabt."""
        if now > self._landing_shot_until:
            self._transition_to(
                AIState.COMBAT if self.target_player is not None else AIState.SEEKING)
            return
        info = (self.players.get(self.target_player)
                if self.target_player is not None else None)
        # Proaktiver Fire-Trigger: selbst feuern, sobald die Restfallzeit des Gegners ≈ Flugzeit
        # des Schusses ist. Feuern aus dem eigenen Tick (analog Z_ATTACK) hält den Schuss komplett
        # im LANDING_SHOT-Zustand → der Z-Block in _maybe_shoot_* wird nicht durchlaufen, und die
        # COMBAT-Bewegung stört das Aiming nicht.
        if (info is not None and info.is_airborne and info.pos[2] > 0.1
                and self._landing_aim_pos is not None):
            _g = self._gravity
            _dz = info.pos[2] - self._landing_hit_z   # Fallhöhe bis Interzeptionshöhe (Fix 2)
            _disc = info.vel[2] ** 2 - 2.0 * _g * _dz
            if _disc >= 0:
                _t_rem = (-info.vel[2] - math.sqrt(_disc)) / _g
                if _t_rem > 0:
                    _dist_aim = math.hypot(
                        self._landing_aim_pos[0] - self.pos[0],
                        self._landing_aim_pos[1] - self.pos[1])
                    _tof = _dist_aim / max(self._effective_shot_speed(), 1.0)
                    if _t_rem <= _tof + 0.15:
                        _target_az = math.atan2(
                            self._landing_aim_pos[1] - self.pos[1],
                            self._landing_aim_pos[0] - self.pos[0])
                        _aligned = abs(_angle_diff(_target_az, self.azimuth)) <= math.radians(25)
                        if (_aligned and self._can_shoot()
                                and now >= self._next_shoot and self._next_slot_ready(now)):
                            self._send_shot(now, self.azimuth)
                            self._set_next_shoot_after_fire(now)
                            if getattr(self, '_debug_log_shot', False):
                                logger.debug("[%s] Schuss: LANDING_SHOT (t_rem=%.2fs tof=%.2fs)",
                                             self.callsign, _t_rem, _tof)
                            self._transition_to(
                                AIState.COMBAT if self.target_player is not None
                                else AIState.SEEKING)
                            return
                        if _t_rem <= _tof:
                            # Fenster verstrichen (Reload/nicht ausgerichtet) → ohne Schuss aufgeben
                            self._transition_to(
                                AIState.COMBAT if self.target_player is not None
                                else AIState.SEEKING)
                            return
                        # sonst: noch im 0.15s-Puffer → nächster Tick versucht erneut
        if info is None or not info.is_airborne:
            self._transition_to(
                AIState.COMBAT if self.target_player is not None else AIState.SEEKING)
            return
        # Bedrohung von ANDEREM Gegner
        threat, threat_t = self._find_incoming_shot(now)
        if threat is not None and threat.shooter_id != self.target_player:
            t_impact = threat.time_to_closest(self.pos[0], self.pos[1])
            if (threat.shooter_id, threat.shot_id) in self._ricochet_paths:
                t_impact = threat_t
            if t_impact < 0.4:
                _sh = self.players.get(threat.shooter_id)
                if self._threat_unseen(_sh):
                    self._last_threat_id = None    # IB/ST ohne Sicht: ignorieren
                    return
                threat_key = (threat.shooter_id, threat.shot_id)
                if self._last_threat_id != threat_key:
                    self._last_threat_id = threat_key
                    self._threat_detected_at = now
                _react = DODGE_REACT_DELAY
                if _sh and _sh.flag == "IB":
                    _react = DODGE_REACT_DELAY * IB_REACT_MULTIPLIER
                elif _sh and _sh.flag == "M":
                    _react = DODGE_REACT_DELAY * M_REACT_MULTIPLIER
                elif _sh and _sh.flag == "CS":
                    _react = DODGE_REACT_DELAY * CS_REACT_MULTIPLIER
                if now - self._threat_detected_at >= _react:
                    dodge_dir, orig_diff = self._compute_dodge_dir(threat, now)
                    self._setup_dodge(threat, now, t_impact, dodge_dir, orig_diff)
                    self._transition_to(AIState.EVADING)
                    return

    # ── Per-State KI-Ticks (10 Hz) ───────────────────────────────────────

    def _tick_idle(self, now: float) -> None:
        """IDLE: Passiv-Wegpunkte + Übergang zu SEEKING wenn Menschen da.
        Bedrohungen werden auch im Passiv-Modus erkannt (Schuss kann jederzeit kommen)."""
        if self._handle_threat(now):
            return
        if self._has_presence():
            self._transition_to(AIState.SEEKING)
            return  # _tick_seeking übernimmt Navigation im nächsten Tick
        # Passiv-Modus: Stuck-Erkennung und Wegpunkte
        if now - self._last_pos_check_time >= STUCK_WINDOW:
            d = math.hypot(self.pos[0] - self._last_pos_check[0],
                           self.pos[1] - self._last_pos_check[1])
            if d < STUCK_MIN_DIST and self.target_pos is not None:
                self._new_target()
            self._last_pos_check_time = now
            self._last_pos_check = [self.pos[0], self.pos[1]]
        self._move_reverse = False
        if self.target_pos is None:
            self._new_target()

    def _tick_seeking(self, now: float) -> None:
        """SEEKING: Ziel suchen, Bedrohungen prüfen, zu COMBAT/IDLE wechseln."""
        if not self._has_presence():
            self._transition_to(AIState.IDLE)
            self._move_reverse = False
            if self.target_pos is None:
                self._new_target()
            return
        if self._handle_threat(now):
            return
        if now - self._last_pos_check_time >= STUCK_WINDOW:
            d = math.hypot(self.pos[0] - self._last_pos_check[0],
                           self.pos[1] - self._last_pos_check[1])
            if d < STUCK_MIN_DIST and self.target_pos is not None:
                self._new_target()
            self._last_pos_check_time = now
            self._last_pos_check = [self.pos[0], self.pos[1]]
        self._check_opportunistic_grab(now)
        _prev = self.target_player
        self._validate_and_find_target()
        if self._active_gm is not None and self.target_player != _prev:
            self._gm_need_update = True
        if self.target_player is not None:
            self._transition_to(AIState.COMBAT)
            return
        self._move_reverse = False
        if self.target_pos is None:
            self._new_target()

    def _tick_combat(self, now: float) -> None:
        """COMBAT: Abstandsmanagement, Schießen, taktische Sprünge."""
        if not self._has_presence():
            self._transition_to(AIState.IDLE)
            return
        if self._handle_threat(now):
            return
        self._check_opportunistic_grab(now)
        # Pausiertes Ziel: nicht beschießen (s. _maybe_shoot), kurz auf Rückkehr warten, dann aufgeben.
        tp = self.players.get(self.target_player) if self.target_player is not None else None
        if tp is not None and tp.paused:
            if self._target_paused_since is None:
                self._target_paused_since = now
            elif now - self._target_paused_since > PAUSE_WAIT_S:
                self.target_player = None          # zu lange pausiert → Ziel aufgeben
                self._target_paused_since = None
        else:
            self._target_paused_since = None
        _prev = self.target_player
        self._validate_and_find_target()
        if self._active_gm is not None and self.target_player != _prev:
            self._gm_need_update = True
        if self.target_player is None:
            self._transition_to(AIState.SEEKING)
            self._move_reverse = False
            if self.target_pos is None:
                self._new_target()
            return
        # Taktischer Übersprung, dann Z-Höhen-Sprung (ZJ1) als Fallback
        if not self._check_tactical_jump(now):
            self._check_z_attack_jump(now)

    def _validate_and_find_target(self) -> None:
        """Validiert target_player (Reichweite/Sicht), sucht neues Ziel falls nötig."""
        if self.target_player is not None:
            ep = self._get_enemy_pos(self.target_player)
            if ep is None:
                self.target_player = None
            else:
                _tx, _ty = ep
                _d = math.hypot(_tx - self.pos[0], _ty - self.pos[1])
                _in_r = _d < self._effective_radar_range()
                _in_s = False
                if _d < SHOT_RANGE:
                    _ang = math.atan2(_ty - self.pos[1], _tx - self.pos[0])
                    _in_s = abs(_angle_diff(_ang, self.azimuth)) < self._effective_fov()
                if not _in_r and not _in_s:
                    self.target_player = None
        if self.target_player is None:
            self.target_player = self._find_target_player()

    def _threat_unseen(self, shooter) -> bool:
        """IB (Geschoss radar-unsichtbar — auch mit SE) bzw. ST (Tank radar-unsichtbar; SE sieht ihn):
        solchen Bedrohungen nur ausweichen, wenn der Bot den Schützen wahrnimmt (Radar mit SE, sonst
        Fenster: FoV + Sicht-LoS)."""
        if shooter is None:
            return False
        if shooter.flag == "ST":
            return not (self._enemy_visible_radar(shooter)          # = SE → sieht Stealth-Tank
                        or self._sees_in_window(shooter, *shooter.pos))
        if shooter.flag == "IB":
            return not self._sees_in_window(shooter, *shooter.pos)  # Geschoss radar-unsichtbar, SE hilft nicht
        return False

    def _handle_threat(self, now: float) -> bool:
        """Bedrohungserkennung mit react-delay und Fix-B-Dodge-Feasibility.
        Gibt True zurück wenn eine Aktion ausgeführt wurde (Caller soll return)."""
        threat, threat_t = self._find_incoming_shot(now)
        if threat is None:
            self._last_threat_id = None
            return False
        _sh = self.players.get(threat.shooter_id)
        if self._threat_unseen(_sh):
            self._last_threat_id = None    # IB/ST ohne Sicht: nicht als 'erkannt' merken
            return False
        threat_key = (threat.shooter_id, threat.shot_id)
        if self._last_threat_id != threat_key:
            self._last_threat_id = threat_key
            self._threat_detected_at = now
        _react = DODGE_REACT_DELAY
        if _sh and _sh.flag == "IB":
            _react = DODGE_REACT_DELAY * IB_REACT_MULTIPLIER
        elif _sh and _sh.flag == "M":
            _react = DODGE_REACT_DELAY * M_REACT_MULTIPLIER
        elif _sh and _sh.flag == "CS":
            _react = DODGE_REACT_DELAY * CS_REACT_MULTIPLIER
        if now - self._threat_detected_at < _react:
            return False
        # Fix J1a: verbleibende Zeit = Zeit_ab_Abschuss − bereits_vergangene_Zeit.
        # Gilt nur für Schüsse mit pos=Abschussort — GM-pos ist bereits die aktuelle
        # Raketenposition, time_to_closest rechnet dort schon ab jetzt → nichts abziehen.
        _elapsed = 0.0 if threat.is_gm else max(0.0, now - threat.fire_time)
        time_to_impact = max(0.0, threat.time_to_closest(self.pos[0], self.pos[1]) - _elapsed)
        # Für Ricochet-Schüsse: Segment-basierte Zeit statt linearer Anfangsgeschwindigkeit
        if threat_key in self._ricochet_paths:
            time_to_impact = max(0.0, threat_t)
        dodge_dir, orig_diff = self._compute_dodge_dir(threat, now)
        turn_rad = abs(_angle_diff(dodge_dir, self.azimuth))
        # Wie viel Zeit braucht der Bot zum Ausweichen:
        # Fahrweg (einen Trefferradius) + 30% der Drehzeit bis zur Ausweichrichtung
        time_to_dodge = (HIT_RADIUS * 1.3 / max(self._tank_speed, 1e-6)
                         + turn_rad / max(self._tank_turn_rate, 1e-6) * 0.3)
        # Wenn Ausweichen noch möglich (10% Puffer gegen knappe Situationen)
        if time_to_dodge * 1.1 <= time_to_impact:
            self._setup_dodge(threat, now, time_to_impact, dodge_dir, orig_diff)
            self._transition_to(AIState.EVADING)
        elif (not self._jumping and self._is_landed()
              and self.own_flag not in ("NJ", "BU")
              and (self._server_jumping or self.own_flag in ("WG", "BY", "JP"))
              and not getattr(self, '_debug_no_jump', False)):
            # Fix EV2: Per-Schuss-Grace — Schuss der beim Early-Exit als ungefährlich
            # eingestuft wurde für 1 s ignorieren (verhindert sofortigen DODGE_JUMP).
            if self._evade_cleared_shots.get(threat_key, 0.0) > now:
                return False
            # Fix E3: DODGE_JUMP — defensiver Sprung, minimale Rotation
            self.vel[2] = self._jump_launch_vz(self.vel[2])
            self._jumping = True
            jump_time = 2.0 * self._effective_jump_velocity() / max(abs(self._effective_gravity()), 0.001)
            if self.own_flag != "WG":
                self._jump_ang_vel = 0.0
            if self.target_player is not None:
                ep = self._get_enemy_pos(self.target_player)
                if ep is not None:
                    enemy_az = math.atan2(ep[1] - self.pos[1], ep[0] - self.pos[0])
                    needed = _angle_diff(enemy_az, self.azimuth)
                    if abs(needed) > math.radians(135):
                        # Nur korrigieren wenn Rücken zum Gegner → sanfte Rotation
                        self._jump_ang_vel = math.copysign(
                            min(abs(needed / max(jump_time, 0.001)) * 0.5,
                                self._tank_turn_rate * 0.5), needed)
                        if getattr(self, '_debug_log_dodge', False):
                            logger.debug("[%s] Ausweichen: Dodge-Sprung mit Korrektur (%.0f°)",
                                         self.callsign, math.degrees(needed))
            if getattr(self, '_debug_log_dodge', False):
                logger.debug("[%s] Ausweichen: Dodge-Sprung statt Ausweichen (Zeit zu knapp)", self.callsign)
            self.ang_vel = self._jump_ang_vel  # analog zu _initiate_nav_jump
            self._transition_to(AIState.DODGE_JUMP)
        else:
            if getattr(self, '_debug_log_shot', False):
                logger.debug("[%s] Schuss: Notschuss – jumping=%s z=%.1f landed=%s flag=%s t_imp=%.3f",
                             self.callsign, self._jumping, self.pos[2], self._is_landed(),
                             self.own_flag, time_to_impact)
            if (self.client.udp_active
                    and self._last_notschuss_threat != threat_key
                    and now >= self._next_shoot
                    and self._next_slot_ready(now)):
                self._last_notschuss_threat = threat_key
                self._send_shot(now, self.azimuth)
                self._set_next_shoot_after_fire(now)
                if getattr(self, '_debug_log_shot', False):
                    logger.debug("[%s] Schuss: Notschuss abgefeuert", self.callsign)
        return True

    def _compute_dodge_dir(self, threat, now: float):
        """Berechnet optimale Ausweich-Richtung mit 60°-Cap vom aktuellen Azimuth.
        Gibt (capped_dir, orig_diff) zurück: orig_diff für vorwärts/rückwärts-Entscheidung."""
        # GM: pos ist bereits die aktuelle Raketenposition (s. _find_incoming_shot)
        sx, sy, _ = threat.pos if threat.is_gm else threat.position_at(now)
        shot_dir = math.atan2(threat.vel[1], threat.vel[0])
        perp_r = _wrap(shot_dir + math.pi / 2)
        perp_l = _wrap(shot_dir - math.pi / 2)
        dot_r = ((self.pos[0] - sx) * math.cos(perp_r)
                 + (self.pos[1] - sy) * math.sin(perp_r))
        best_perp = perp_r if dot_r > 0 else perp_l
        diff = _angle_diff(best_perp, self.azimuth)
        capped = _wrap(self.azimuth + math.copysign(min(abs(diff), math.radians(60)), diff))
        return capped, diff

    # ── COMBAT 60 Hz Bewegung ─────────────────────────────────────────────

    def _execute_combat_move(self, dt: float, half: float, now: float = 0.0) -> None:
        """COMBAT 60Hz: dreht auf vorhergesagte Zielposition, fährt distanzbasiert."""
        if self.target_player is None:
            return
        ep = self._get_enemy_pos(self.target_player)
        if ep is None:
            return
        info = self.players.get(self.target_player)
        dx = ep[0] - self.pos[0]
        dy = ep[1] - self.pos[1]
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return
        # Fix A: Vorhaltepunkt mit Radialgeschwindigkeits-Korrektur
        if info is not None:
            rdx, rdy = dx / dist, dy / dist
            radial_closing = -(info.vel[0] * rdx + info.vel[1] * rdy)
            tof = dist / max(self._effective_shot_speed() + radial_closing, 10.0)
            aim_x = ep[0] + info.vel[0] * tof
            aim_y = ep[1] + info.vel[1] * tof
        else:
            aim_x, aim_y = ep
        # Pfad zum Gegner planen/aktualisieren wenn nötig
        nav_goal   = getattr(self, "_nav_goal",   None)
        nav_goal_z = getattr(self, "_nav_goal_z", 0.0)
        enemy_z    = info.pos[2] if info is not None else 0.0
        # Gegner per Sprung unerreichbar (zu hoch)? → ggf. Eskalations-Zyklus (s.u.)
        _max_jump_h = self._effective_jump_height()
        _too_high   = (enemy_z - self.pos[2]) >= _max_jump_h
        _stuck_active = (self._unreach_target is not None
                         and self._unreach_target == self.target_player)
        if not _too_high and _stuck_active:
            self._unreach_target = None     # Gegner nicht mehr zu hoch → Episode beenden
            _stuck_active = False
        # Direktziel-Modus? (innerhalb Optimaldistanz + Gegner nicht deutlich höher). Wird hier
        # VOR dem Replan bestimmt, damit im Direktmodus gar keine (ungenutzte) A*-Planung läuft.
        _opt = self._effective_optimal_range()
        _enemy_z = info.pos[2] if info is not None else self.pos[2]
        _los_clear   = self._has_los_to_enemy(self.target_player)
        _dist_thresh = SHOT_RANGE if _los_clear else _opt * 1.1
        _check1      = self.pos[2] + TANK_HEIGHT > _enemy_z
        # C: Bot unter erhöhtem Gegner mit verfügbarem Indirekt-Schuss → stehen & aufs Tor zielen
        # statt hochzuklettern, zeitlich gedeckelt (kein ewiges Festkleben). sobald die
        # Navigation durch Tore routet, wird genau diese Bedingung zur traverse-vs-shoot-Entscheidung.
        _hold_indirect = self._update_indirect_hold(
            now, (not _check1) and self._indirect_shot_available(self.target_player))
        _skip_nav    = (_check1 and dist < _dist_thresh) or _hold_indirect
        # Proaktive Wand-Vorausschau: würde der Direktmodus den Bot ohne Sicht steil in eine Wand
        # fahren (z.B. dünne Trennwand auf einer Plattform), nicht stur rammen, sondern A* um die
        # Wand routen lassen. Nur den Nahkampf-Direktmodus aufbrechen, nicht den Indirekt-Halt.
        if _skip_nav and not _hold_indirect and not _los_clear and self._steep_wall_ahead(
                math.atan2(aim_y - self.pos[1], aim_x - self.pos[0]),
                min(dist, NAV_WALL_PROBE_DIST)) is not None:
            _skip_nav = False
        replan_xy  = (nav_goal is None or math.hypot(ep[0] - nav_goal[0],
                                                     ep[1] - nav_goal[1]) > 20.0)
        replan_z   = abs(enemy_z - nav_goal_z) > TANK_HEIGHT * 2
        # Während einer Stuck-Episode verwaltet _combat_escalate das Planen allein (sonst würde
        # replan_xy den Reposition-Pfad überschreiben). Im Direktmodus wird kein Pfad gefahren →
        # gar nicht erst planen (spart A* und vermeidet ungenutzte Wegpunkt-Logs).
        if (replan_xy or replan_z) and not _stuck_active and not _skip_nav:
            if enemy_z > TANK_HEIGHT:
                # Z_ATTACK möglich → 50% NAV_JUMP hoch; sonst (auch _too_high) → immer hoch
                _goal_z = enemy_z if (_too_high or not self._z_attack_feasible(now)
                                      or random.random() < 0.5) else None
            else:
                _goal_z = 0.0   # Bodengegner: A* auf Boden-Endpunkt zwingen
            self._plan_path(ep[0], ep[1], goal_z=_goal_z, cap_wps=8)  # COMBAT: max. 8 WPs (≈40u)
            self._nav_goal_z = enemy_z
        if _skip_nav:
            self._nav_path = []     # Direktmodus: keine Wegpunkte fahren
            self._nav_goal = None   # erzwingt frischen Replan beim Verlassen des Direktmodus
        nav_path = getattr(self, "_nav_path", [])
        if nav_path and not _skip_nav:
            self._navigate_wp(dt, half, reverse=self._should_reverse_to_wp())
            return
        # Gegner per Sprung unerreichbar und (noch) kein A*-Pfad → nicht blind die Wand rammen,
        # sondern Eskalations-Zyklus (Re-Target → Direktmodus → Reposition → Replan).
        if _too_high and not nav_path and not _hold_indirect:   # während Halt nicht eskalieren
            if self._combat_escalate(dt, half, ep, aim_x, aim_y, enemy_z):
                return                                       # Reposition wird abgefahren
            nav_path = getattr(self, "_nav_path", [])        # Replan evtl. erfolgreich
            if nav_path:
                self._navigate_wp(dt, half, reverse=self._should_reverse_to_wp())
                return
            # sonst: Direktmodus (Prio 2) fällt unten durch
        # Direktziel-Modus: distanzbasiert (Rückwärts / langsam / voll)
        target_az = math.atan2(aim_y - self.pos[1], aim_x - self.pos[0])
        _cache = self._rico_aim_cache
        _rico_drive = (_cache is not None
                       and _cache[1] == self.target_player
                       and _cache[2] is not None
                       and (not self._has_los_to_enemy(self.target_player)
                            or self._cross_floor_indirect(info)))
        if _rico_drive:
            target_az = _cache[2][0]
        elif not _los_clear:
            # Abdrehen (A* hat keinen Pfad geliefert): würde der Bot hier ohne Sicht steil in eine
            # Wand fahren, auf die Wand-Tangente drehen und entlanggleiten statt frontal zu drücken.
            _tan = self._steep_wall_ahead(target_az, min(dist, NAV_WALL_PROBE_DIST))
            if _tan is not None:
                target_az = _tan
        max_turn  = self._tank_turn_rate * dt
        diff = _angle_diff(target_az, self.azimuth)
        self.ang_vel = math.copysign(
            min(abs(diff / max(dt, 1e-6)), self._tank_turn_rate), diff)
        self.azimuth = _wrap(
            self.azimuth + math.copysign(min(abs(diff), max_turn), diff))
        if dist < _opt:
            speed = -self._tank_speed * 0.5
            _nav = getattr(self, "_nav_graph", None)
            if _nav is not None and self._get_floor_z() > 0.5:
                _nx = self.pos[0] + math.cos(self.azimuth) * speed * dt
                _ny = self.pos[1] + math.sin(self.azimuth) * speed * dt
                if _nav.get_floor_z(_nx, _ny, self.pos[2] + 0.1) < self._get_floor_z() - 1.0:
                    speed = 0.0
        elif dist > _opt * 2:
            speed = self._tank_speed
        else:
            speed = self._tank_speed * 0.15
        if speed > 0 and abs(self.ang_vel) > self._tank_turn_rate * 0.5:
            _nav = getattr(self, "_nav_graph", None)
            if _nav is not None and self._get_floor_z() > 0.5:
                _nx = self.pos[0] + math.cos(self.azimuth) * speed * dt
                _ny = self.pos[1] + math.sin(self.azimuth) * speed * dt
                if _nav.get_floor_z(_nx, _ny, self.pos[2] + 0.1) < self._get_floor_z() - 1.0:
                    speed = 0.0
        speed, self.ang_vel = self._apply_movement_caps(speed, self.ang_vel)
        self.vel[0] = math.cos(self.azimuth) * speed
        self.vel[1] = math.sin(self.azimuth) * speed
        self._apply_bounds(dt, half)
        if getattr(self, '_debug_log_path', False) and self._is_inside_obstacle():
            _t = time.monotonic()
            if _t - getattr(self, '_debug_obstacle_logged', 0.0) > 1.0:
                self._debug_obstacle_logged = _t
                logger.debug("[%s] Pfad: Kollision bei (%.0f,%.0f) Ziel:%s",
                             self.callsign, self.pos[0], self.pos[1], self.target_pos)

    # ── COMBAT-Eskalation: per Sprung unerreichbarer (zu hoher) Gegner ─────

    def _combat_escalate(self, dt: float, half: float, ep, aim_x: float,
                         aim_y: float, enemy_z: float) -> bool:
        """Eskalations-Zyklus, wenn der zu hohe Gegner per A* nicht erreichbar ist.

        Wiederholender Zyklus mit Früh-Ausstieg (User-Prio):
          1. anderes erreichbares Ziel suchen
          2. Direktmodus für UNREACH_DIRECT_TIME (mit gedrosseltem Hintergrund-Replan)
          3. ~UNREACH_REPOS_RADIUS Reposition (frischer A*-Start)
          4. erneut Pfad zum Gegner; sonst Zyklus neu

        Gibt True zurück, wenn der Tick komplett gesteuert wurde (Reposition-Fahrt); sonst False
        (Caller navigiert einen gefundenen Pfad oder fährt Direktmodus)."""
        now = time.monotonic()
        tgt = self.target_player
        # Episode an Ziel binden / bei Zielwechsel zurücksetzen
        if self._unreach_target != tgt:
            self._unreach_target = tgt
            self._unreach_phase = 0
            self._unreach_until = now
            self._unreach_replan_at = 0.0

        # ── Phase 0 — Prio 1: anderes erreichbares Ziel ───────────────────
        if self._unreach_phase == 0:
            self._combat_avoid[tgt] = now + UNREACH_AVOID_TIME
            alt = self._find_target_player()
            if alt is not None and alt != tgt:
                self.target_player = alt
                self._unreach_target = None          # Episode beenden → normaler COMBAT
                return False
            self._unreach_phase = 1                  # kein Alt-Ziel → Direktmodus
            self._unreach_until = now + UNREACH_DIRECT_TIME
            # Top-Replan dieses Ticks lief bereits → erster Hintergrund-Replan erst in 1 s
            self._unreach_replan_at = now + COMBAT_REPLAN_RETRY

        # ── Phase 1 — Prio 2: Direktmodus + Hintergrund-Replan (Früh-Aus) ─
        if self._unreach_phase == 1:
            if now >= self._unreach_replan_at:
                self._unreach_replan_at = now + COMBAT_REPLAN_RETRY
                self._plan_path(ep[0], ep[1], goal_z=enemy_z, cap_wps=8)
                self._nav_goal_z = enemy_z
                if getattr(self, "_nav_path", []):
                    self._unreach_target = None      # Pfad gefunden → raus
                    return False
            if now < self._unreach_until:
                return False                          # Direktmodus weiterlaufen lassen
            self._unreach_phase = 2                   # 30 s um → Reposition

        # ── Phase 2 — Prio 3: Reposition-Pfad einmalig planen ─────────────
        if self._unreach_phase == 2:
            rx, ry = self._pick_reposition_point(ep)
            self._plan_path(rx, ry)                   # Boden-Reposition (frischer A*-Start)
            self._unreach_phase = 3
            self._unreach_until = now + UNREACH_REPOS_TIMEOUT

        # ── Phase 3 — Reposition abfahren, dann Prio 4: Replan zum Gegner ─
        if self._unreach_phase == 3:
            if self.target_pos is not None and now < self._unreach_until:
                self._navigate_wp(dt, half)
                return True                           # Reposition abfahren
            # Reposition erreicht / Timeout → erneut zum Gegner
            self._plan_path(ep[0], ep[1], goal_z=enemy_z, cap_wps=8)
            self._nav_goal_z = enemy_z
            if getattr(self, "_nav_path", []):
                self._unreach_target = None
                return False
            self._unreach_phase = 0                   # immer noch nichts → Zyklus neu
        return False

    def _pick_reposition_point(self, ep) -> tuple:
        """Reposition-Zielpunkt ~UNREACH_REPOS_RADIUS entfernt, Winkel grob in Gegnerrichtung
        (±120°), auf Weltgrenzen geklemmt. Frischer A*-Start, ohne den Kampf zu verlassen."""
        base_az = math.atan2(ep[1] - self.pos[1], ep[0] - self.pos[0])
        ang = base_az + random.uniform(-math.radians(120), math.radians(120))
        rx = self.pos[0] + math.cos(ang) * UNREACH_REPOS_RADIUS
        ry = self.pos[1] + math.sin(ang) * UNREACH_REPOS_RADIUS
        _m = self.world_half - 5.0
        return (max(-_m, min(_m, rx)), max(-_m, min(_m, ry)))

    # ── Dodge-Setup ───────────────────────────────────────────────────────

    def _setup_dodge(self, threat, now: float, time_to_impact: float,
                     dodge_dir: float, orig_diff: float = None) -> None:
        """Setzt Dodge-Variablen mit vorberechneter Ausweich-Richtung (60°-gecapped).
        orig_diff: Winkel von best_perp zu azimuth vor dem Cap — bestimmt fwd/rev."""
        self._dodge_dir = dodge_dir
        # Fix E4: Entscheidung fwd/rev auf Basis der ursprünglichen Perpendikular-Richtung
        decision = abs(orig_diff) if orig_diff is not None else abs(_angle_diff(dodge_dir, self.azimuth))
        if decision < math.radians(45):
            self._dodge_forward, self._dodge_reverse = True, False
        elif decision > math.radians(135):
            self._dodge_forward, self._dodge_reverse = False, True
        else:
            self._dodge_forward, self._dodge_reverse = False, False
        dodge_duration = max(0.15, min(time_to_impact * 1.5, 0.8))
        self._dodge_until = now + dodge_duration
        self._dodging = True
        if getattr(self, '_debug_log_dodge', False):
            logger.debug("[%s] Ausweichen: Vor Shot [%d/%d] für %.2fs",
                         self.callsign, threat.shooter_id, threat.shot_id, dodge_duration)

    # ── Bewegungs-Methoden ────────────────────────────────────────────────

    def _wp_reach_radius(self) -> float:
        """Engerer Radius direkt vor NAV_JUMP-Anlauf-WP (aufwärts), sonst NAV_CELL_SIZE."""
        nav_path = getattr(self, "_nav_path", [])
        if len(nav_path) >= 2 and nav_path[1][2] - self._get_floor_z() > 1.5:
            return NAV_CELL_SIZE
        return NAV_CELL_SIZE * 1.25

    def _nav_jump_geometry_ok(self, target_wp) -> bool:
        """True wenn Sprung geometrisch möglich ist (Höhe + Reichweite), ohne Azimuth.

        Front-Catch (Pixel-on): der Tank landet, sobald seine Front die Zielkante erreicht —
        die effektiv zu überbrückende Strecke ist daher hdist - JUMP_EDGE_TOL (deckungsgleich
        mit der Sprungkanten-Planung in nav_graph._vertical_neighbors)."""
        dx, dy = target_wp[0] - self.pos[0], target_wp[1] - self.pos[1]
        dz = target_wp[2] - self.pos[2]
        v0 = self._effective_jump_velocity()
        g_abs = abs(self._effective_gravity())
        disc = v0 * v0 - 2.0 * g_abs * dz
        if disc < 0:
            return False
        t_desc = (v0 + math.sqrt(disc)) / g_abs
        eff = max(0.0, math.hypot(dx, dy) - JUMP_EDGE_TOL)
        return eff <= self._travel_tank_speed() * t_desc * 1.1

    def _nav_jump_feasible(self, target_wp) -> bool:
        """True wenn Bot Ziel-WP beim Abstieg physikalisch erreichen kann
        und der Bot bereits präzise in Sprungrichtung zeigt (±5°).
        Front-Catch wie in _nav_jump_geometry_ok (hdist - JUMP_EDGE_TOL)."""
        dx, dy = target_wp[0] - self.pos[0], target_wp[1] - self.pos[1]
        hdist  = math.hypot(dx, dy)
        dz     = target_wp[2] - self.pos[2]
        v0 = self._effective_jump_velocity()
        g_abs = abs(self._effective_gravity())
        disc = v0 * v0 - 2.0 * g_abs * dz
        if disc < 0:
            return False
        t_desc = (v0 + math.sqrt(disc)) / g_abs
        if max(0.0, hdist - JUMP_EDGE_TOL) > self._travel_tank_speed() * t_desc * 1.1:
            return False
        # Azimuth-Check — Bot muss präzise in Sprungrichtung zeigen (±5°)
        az_to_target = math.atan2(dy, dx)
        if abs(_angle_diff(az_to_target, self.azimuth)) > math.pi / 36:
            return False
        return True

    def _check_advance_path(self) -> bool:
        """WP-Erreichen prüfen; True wenn Aufrufer sofort return soll."""
        if self.target_pos is not None:
            tx, ty = self.target_pos
            if math.hypot(tx - self.pos[0], ty - self.pos[1]) < self._wp_reach_radius():
                nav_path = getattr(self, "_nav_path", [])
                # NAV_JUMP-Landekontrolle: zu großer Z-Unterschied = Fehlschlag
                if (nav_path
                        and nav_path[0][2] - self._get_floor_z() > 1.5
                        and abs(self.pos[2] - nav_path[0][2]) > NAV_JUMP_Z_TOL):
                    self._advance_path(timed_out=True)
                    return True
                self._wp_fail_count = 0
                self._advance_path()
                return True
        return False

    def _should_reverse_to_wp(self) -> bool:
        """Rückwärts zum NAV_JUMP-Anlauf-WP fahren, wenn dieser kurz hinter dem Bot liegt und
        der darauf folgende WP ein Sprung-rauf ist.

        Spart das doppelte ~180°-Drehen: ohne dies dreht der Bot voll um, fährt vorwärts zum
        Anlaufpunkt und dreht in NAV_JUMP_ALIGN erneut zurück. Rückwärts kommt er bereits grob
        in Sprungrichtung ausgerichtet am Anlaufpunkt an. Nur über kurze Strecken (sonst ist
        Vorwärtsfahren effizienter)."""
        if not self._can_move_backward():
            return False
        nav_path = getattr(self, "_nav_path", [])
        if len(nav_path) < 2 or self.target_pos is None:
            return False
        # WP nach dem aktuellen Ziel ist ein Sprung-rauf (aktuelles Ziel = Anlauf-WP)?
        if nav_path[1][2] - nav_path[0][2] <= 1.5:
            return False
        tx, ty = self.target_pos
        if math.hypot(tx - self.pos[0], ty - self.pos[1]) > NAV_CELL_SIZE * 2.5:
            return False  # nur ein kurzes Stück rückwärts, nicht über weite Strecken
        diff = _angle_diff(math.atan2(ty - self.pos[1], tx - self.pos[0]), self.azimuth)
        return abs(diff) > math.radians(135)   # Anlaufpunkt liegt klar hinter dem Bot

    def _navigate_wp(self, dt: float, half: float, reverse: bool = False) -> bool:
        """Gemeinsamer WP-Navigations-Kern: Timeout, Advance, Lookahead, Drehen, Geschwindigkeit.
        Gibt True zurück wenn der Tick vollständig behandelt wurde."""
        if self.target_pos is None:
            return False
        nav_path = self._nav_path
        now = time.monotonic()
        if (nav_path
                and self._wp_start_time is not None
                and now - self._wp_start_time > self._wp_timeout):
            self._wp_fail_count += 1
            _wp_z = nav_path[0][2]
            _d2d  = math.hypot(self.target_pos[0] - self.pos[0],
                               self.target_pos[1] - self.pos[1])
            if getattr(self, '_debug_log_path', False):
                logger.debug(
                    "[%s] Pfad: WP-Timeout #%d Bot=(%.1f,%.1f z=%.2f floor=%.2f)"
                    " WP=(%.1f,%.1f z=%.2f) dist2d=%.2f dz=%.2f r=%.1f",
                    self.callsign, self._wp_fail_count,
                    self.pos[0], self.pos[1], self.pos[2], self._get_floor_z(),
                    self.target_pos[0], self.target_pos[1], _wp_z,
                    _d2d, self.pos[2] - _wp_z, self._wp_reach_radius())
            if self._wp_fail_count >= 2:
                self._nav_path = []
                self._nav_goal = None
                self._wp_fail_count = 0
                self._wp_start_time = None
                self.target_pos = None
            else:
                self._advance_path(timed_out=True)
            return True
        if self._check_advance_path():
            return True
        if nav_path:
            wp_x, wp_y, wp_z = nav_path[0][0], nav_path[0][1], nav_path[0][2]
        else:
            wp_x, wp_y = self.target_pos
            wp_z = self.pos[2]
        aim_x, aim_y = wp_x, wp_y
        r = self._wp_reach_radius()
        dist_to_wp = math.hypot(wp_x - self.pos[0], wp_y - self.pos[1])
        dx, dy = aim_x - self.pos[0], aim_y - self.pos[1]
        if math.hypot(dx, dy) < 0.001:
            self._new_target()
            return True
        target_az = math.atan2(dy, dx)
        _eff_turn = self._effective_turn_rate()
        _eff_speed = self._effective_tank_speed()
        max_turn = _eff_turn * dt
        if reverse:
            enemy_facing = _wrap(target_az + math.pi)
            diff = _angle_diff(enemy_facing, self.azimuth)
            if not self._can_turn_left()  and diff > 0: diff = 0.0
            if not self._can_turn_right() and diff < 0: diff = 0.0
            self.ang_vel = math.copysign(
                min(abs(diff / max(dt, 1e-6)), _eff_turn), diff)
            self.azimuth = _wrap(
                self.azimuth + math.copysign(min(abs(diff), max_turn), diff))
            speed = -_eff_speed * 0.5 * max(0.05, math.cos(diff))
        else:
            diff = _angle_diff(target_az, self.azimuth)
            if not self._can_turn_left()  and diff > 0: diff = 0.0
            if not self._can_turn_right() and diff < 0: diff = 0.0
            self.ang_vel = math.copysign(
                min(abs(diff / max(dt, 1e-6)), _eff_turn), diff)
            self.azimuth = _wrap(
                self.azimuth + math.copysign(min(abs(diff), max_turn), diff))
            if abs(diff) >= math.pi / 2.0:
                speed = 0.0
            else:
                sin_d = max(math.sin(abs(diff)), 0.02)
                speed = min(_eff_speed,
                            _eff_turn * dist_to_wp / (2.0 * sin_d))
            if getattr(self, '_debug_log_path', False) and dist_to_wp < r * 3.0:
                _t = time.monotonic()
                if _t - getattr(self, '_debug_wp_near_t', 0.0) > 0.5:
                    self._debug_wp_near_t = _t
                    logger.debug(
                        "[%s] Pfad: Nahe WP (%.1f,%.1f,%.1f) Bot=(%.1f,%.1f,%.1f)"
                        " dist=%.2f r=%.1f spd=%.1f diff=%.0f° az=%.0f°",
                        self.callsign, wp_x, wp_y, wp_z,
                        self.pos[0], self.pos[1], self.pos[2],
                        dist_to_wp, r, speed,
                        math.degrees(diff), math.degrees(self.azimuth))
        speed, self.ang_vel = self._apply_movement_caps(speed, self.ang_vel)
        self.vel[0] = math.cos(self.azimuth) * speed
        self.vel[1] = math.sin(self.azimuth) * speed
        self._apply_bounds(dt, half)
        if getattr(self, '_debug_log_path', False) and self._is_inside_obstacle():
            _t = time.monotonic()
            if _t - getattr(self, '_debug_obstacle_logged', 0.0) > 1.0:
                self._debug_obstacle_logged = _t
                logger.debug("[%s] Pfad: Kollision bei (%.0f,%.0f) Ziel:%s",
                             self.callsign, self.pos[0], self.pos[1], self.target_pos)
        return True

    def _move_to_target(self, dt: float, half: float) -> None:
        """Dreht und fährt zum nächsten Pfad-Wegpunkt; nutzt _navigate_wp.

        Rückwärts, wenn ein NAV_JUMP-Anlaufpunkt kurz hinter dem Bot liegt
        (_should_reverse_to_wp), sonst gemäß _move_reverse."""
        if self.target_pos is None:
            return
        reverse = self._move_reverse or self._should_reverse_to_wp()
        self._navigate_wp(dt, half, reverse=reverse)

    def _apply_obstacle_bounds(self, dt: float) -> None:
        """Wall-Sliding + Decken-Kollision: korrigiert self.vel/pos bei Gebäude-Kollision (60 Hz)."""
        if self._can_drive_through_obstacles():
            return
        world_map = getattr(self, '_world_map', None)
        if world_map is None:
            return
        pz = self.pos[2]
        px, py = self.pos[0], self.pos[1]
        vx, vy = self.vel[0], self.vel[1]
        # P3-NAV-02: Teleporter-Posts + Crossbar als solide Boxen mitprüfen (Decken-Kollision von
        # unten gegen den Crossbar, Wall-Slide an den Posts). Das Querungsfeld bleibt frei.
        _solid = world_map.boxes + getattr(self, '_tele_solid_boxes', [])
        # Broad-Phase: bei vorhandenem NavGraph nur die Boxen der Bot-Zelle statt linear über alle.
        # nav._obs = non-drive_through world_map.boxes + dieselben Teleporter-Solidboxen → deckungs-
        # gleicher Kandidatensatz wie _solid nach dem drive_through-Skip. Ohne nav: linearer Fallback.
        _grid = getattr(getattr(self, '_nav_graph', None), '_solid_grid', None)
        # ── Decken-Kollision: Bot-Kopf stößt von unten an Plattform-Boden ──────
        bot_top = pz + self._tank_height
        ceil_cands = _grid.query_point(px, py) if _grid is not None else _solid
        for obs in ceil_cands:
            if obs.drive_through:
                continue
            # pz < obs.bottom_z: Bot ist unterhalb — nicht bereits darin (OO-Flagge etc.)
            if not (pz < obs.bottom_z <= bot_top):
                continue
            cos_a = obs.cos_a; sin_a = obs.sin_a
            dx, dy = px - obs.cx, py - obs.cy
            lx = dx * cos_a + dy * sin_a
            ly = -dx * sin_a + dy * cos_a
            hw = obs.half_w + self._effective_half_width()
            hd = obs.half_d + self._effective_half_width()
            if abs(lx) < hw and abs(ly) < hd:
                self.vel[2] = 0.0
                _floor_z = self._burrow_depth if self.own_flag == "BU" else self._get_floor_z()
                self.pos[2] = max(obs.bottom_z - self._tank_height, _floor_z)
                pz = self.pos[2]
                bot_top = pz + self._tank_height
                break
        # ── XY-Wall-Sliding ───────────────────────────────────────────────────
        # Broad-Phase über die Strecke Bot→prädizierter Punkt: der prädizierte Punkt (nx,ny) wandert
        # innerhalb der Schleife (vx/vy werden geklemmt), deshalb query_segment über die (sub-zellige)
        # Anfangsstrecke — deckt alle geprüften Zwischenpunkte ab, jede Box genau einmal.
        slide_cands = (_grid.query_segment(px, py, px + vx * dt, py + vy * dt)
                       if _grid is not None else _solid)
        for obs in slide_cands:
            if obs.drive_through:
                continue
            tank_top = pz + self._tank_height
            if tank_top <= obs.bottom_z or pz >= obs.bottom_z + obs.height - 0.5:
                continue
            nx = px + vx * dt
            ny = py + vy * dt
            cos_a = obs.cos_a
            sin_a = obs.sin_a
            dx, dy = nx - obs.cx, ny - obs.cy
            lnx = dx * cos_a + dy * sin_a
            lny = -dx * sin_a + dy * cos_a
            hw = obs.half_w + self._effective_half_width()
            hd = obs.half_d + self._effective_half_width()
            if abs(lnx) >= hw or abs(lny) >= hd:
                continue
            lvx = vx * cos_a + vy * sin_a
            lvy = -vx * sin_a + vy * cos_a
            overlap_x = hw - abs(lnx)
            overlap_y = hd - abs(lny)
            # Kleineres Overlap = Trennungsachse: Geschwindigkeit entlang dieser Achse auf 0
            # (Wandgleiten: Bot gleitet an der Wand entlang statt "stecken" zu bleiben)
            if overlap_x < overlap_y:
                if lnx * lvx < 0:  # Bot bewegt sich noch in die Wand → stoppen
                    lvx = 0.0
            else:
                if lny * lvy < 0:
                    lvy = 0.0
            # Rück-Rotation local→world (cos_a/sin_a = cos/sin(angle))
            vx = lvx * cos_a - lvy * sin_a
            vy = lvx * sin_a + lvy * cos_a
        self.vel[0] = vx
        self.vel[1] = vy

    def _apply_bounds(self, dt: float, half: float) -> None:
        """Begrenzt Bot-Position auf Weltgrenzen; prallt von Wänden ab."""
        self._apply_obstacle_bounds(dt)
        nx = self.pos[0] + self.vel[0] * dt
        ny = self.pos[1] + self.vel[1] * dt
        bounced = False
        if not (-half < nx < half):
            self.vel[0] = -self.vel[0]
            nx = max(-half + 1, min(half - 1, nx))
            bounced = True
        if not (-half < ny < half):
            self.vel[1] = -self.vel[1]
            ny = max(-half + 1, min(half - 1, ny))
            bounced = True
        if bounced:
            self._plan_path(
                random.uniform(-half * 0.85, half * 0.85),
                random.uniform(-half * 0.85, half * 0.85),
            )
        self.pos[0] = nx
        self.pos[1] = ny

    def _check_teleport_crossing(self, old: Tuple[float, float, float], now: float) -> None:
        """P3-NAV-02: Erkennt das Durchqueren eines Teleporter-Felds im letzten Bewegungssegment
        (old → self.pos) und führt den Positions-/Velocity-/Azimuth-Sprung aus + meldet MsgTeleport.

        Port von LocalPlayer::doUpdateMotion (crossesTeleporter → getPointWRT → sendTeleport).
        Läuft in jedem State (auch im Sprung/Fall): teleport_through erhält die Z-Höhe relativ zu
        bottom_z und reicht vel[2] unverändert durch — der AI-/Sprung-State bleibt unangetastet."""
        world_map = getattr(self, "_world_map", None)
        if world_map is None or not world_map.teleporters:
            return
        # PhantomZone togglet zoned statt zu teleportieren (P4-FLG-03) → hier nicht teleportieren.
        if self.own_flag == "PZ":
            return
        if now < getattr(self, "_teleporting_until", 0.0):
            return  # Re-Trigger-Sperre (mirror isTeleporting())
        ox, oy, oz = old
        dx, dy, dz = self.pos[0] - ox, self.pos[1] - oy, self.pos[2] - oz
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return  # keine Horizontalbewegung → keine Feld-Ebene gequert
        # Frühestes gequertes Feld (t ∈ [0,1]) bestimmen
        best_t, best_ti, best_face = 2.0, -1, -1
        for ti, tele in enumerate(world_map.teleporters):
            res = ray_teleporter_crossing(ox, oy, oz, dx, dy, dz, tele)
            if res is None:
                continue
            t, face = res
            if 0.0 <= t <= 1.0 and t < best_t:
                best_t, best_ti, best_face = t, ti, face
        if best_ti < 0:
            return
        link_map = getattr(self, "_link_map", None) or {}
        target = link_map.get(2 * best_ti + best_face)
        if target is None:
            return
        teles = world_map.teleporters
        exit_ti, exit_face = target // 2, target & 1
        if exit_ti >= len(teles):
            return
        src_tele, dst_tele = teles[best_ti], teles[exit_ti]
        # Position UND Velocity in einem Aufruf transformieren (Z relativ zu bottom_z bleibt
        # erhalten → Sprunghöhe; vel[2] unverändert → Sprung-/Fallbewegung läuft nahtlos weiter).
        npx, npy, npz, nvx, nvy, nvz = teleport_through(
            self.pos[0], self.pos[1], self.pos[2],
            self.vel[0], self.vel[1], self.vel[2],
            src_tele, best_face, dst_tele, exit_face)
        # Exit-Validierung: liegt der Austritt in einem Hindernis → Teleport verwerfen (revert wie
        # LocalPlayer: Position zurück, Horizontal-Velocity 0, vel[2] behalten).
        self.pos[0], self.pos[1], self.pos[2] = npx, npy, npz
        if self._is_inside_obstacle():
            self.pos[0], self.pos[1], self.pos[2] = ox, oy, oz
            self.vel[0] = self.vel[1] = 0.0
            return
        self.vel[0], self.vel[1], self.vel[2] = nvx, nvy, nvz
        radians1 = src_tele.angle + (0.0 if best_face == 0 else math.pi)
        radians2 = dst_tele.angle + (0.0 if exit_face == 1 else math.pi)
        self.azimuth = _wrap(self.azimuth + (radians2 - radians1))
        self._teleporting_until = now + TELEPORT_TIME
        self._resync_path_after_teleport(ox, oy, npx, npy)
        self._send_teleport(2 * best_ti + best_face, target)
        # ── Logging der Teleporter-Nutzung (P3-NAV-02) ──────────────────────
        # INFO ohne Details (Teleporte sind selten → nicht spammy), DEBUG mit Details via
        # --debug-log-tele. „geplant" = ein A*-Pfad führte hindurch und besteht nach dem Resync fort.
        logger.info("[%s] Teleporter genutzt (Tor %d→%d)",
                    self.callsign, 2 * best_ti + best_face, target)
        if getattr(self, "_debug_log_tele", False):
            _planned = bool(getattr(self, "_nav_path", None))
            logger.debug(
                "[%s] Tele-Detail: (%.1f,%.1f,%.1f)→(%.1f,%.1f,%.1f) Δz=%+.1f vz=%.1f "
                "Δaz=%+.1f° %s%s",
                self.callsign, ox, oy, oz, npx, npy, npz, npz - oz, self.vel[2],
                math.degrees(radians2 - radians1),
                "geplant" if _planned else "ungeplant",
                "" if self.vel[2] == 0.0 else " (im Sprung/Fall)")

    def _resync_path_after_teleport(self, ox: float, oy: float,
                                    nx: float, ny: float) -> None:
        """Nach dem Teleport: Wegpunkte verwerfen, die nun „hinter" uns liegen (näher an der
        Eintritts- als an der Austrittsseite) — verhindert Zurückfahren zum Eingang. Führte ein
        geplanter Pfad durch den Teleporter, bleibt der Austritts-WP Ziel (kein Replan); war der
        Teleport ungewollt, leert sich der Pfad → der nächste Boden-Tick plant neu (deferred)."""
        nav_path = getattr(self, "_nav_path", None)
        if nav_path:
            while nav_path and (math.hypot(nav_path[0][0] - ox, nav_path[0][1] - oy)
                                < math.hypot(nav_path[0][0] - nx, nav_path[0][1] - ny)):
                nav_path.pop(0)
            if nav_path:
                self.target_pos = (nav_path[0][0], nav_path[0][1])
                self._wp_start_time = time.monotonic()
                self._wp_fail_count = 0
            else:
                self._nav_goal = None
                self._wp_start_time = None
                self.target_pos = None
            return
        # Direktziel: nur invalidieren, wenn es jetzt hinter uns liegt
        tp = getattr(self, "target_pos", None)
        if tp is not None and (math.hypot(tp[0] - ox, tp[1] - oy)
                               < math.hypot(tp[0] - nx, tp[1] - ny)):
            self._nav_goal = None
            self._wp_start_time = None
            self.target_pos = None

    # ── Sicht- und Erkennungs-Methoden ────────────────────────────────────

    def _effective_fov(self) -> float:
        """Halbwinkel des Fenster-Sichtkegels (was der Spieler „out the window" sieht). WA verbreitert
        ihn (Server-Var _wideAngleAng). EINZIGER Sicht-FoV — Wahrnehmung UND Ziel-Erfassung nutzen ihn."""
        return (self._wide_angle_ang / 2.0) if self.own_flag == "WA" else (TARGET_FOV / 2.0)

    def _in_fov(self, px: float, py: float) -> bool:
        """True wenn (px, py) im Fenster-Sichtkegel des Bots liegt (Halbwinkel = _effective_fov())."""
        if math.hypot(px - self.pos[0], py - self.pos[1]) < 1.0:
            return True
        angle_to = math.atan2(py - self.pos[1], px - self.pos[0])
        return abs(_angle_diff(angle_to, self.azimuth)) < self._effective_fov()

    def _is_ahead(self, px: float, py: float) -> bool:
        """Geometrie „liegt vor mir" (±90°), KEIN Sicht-FoV: für Nav-WP-Skip (Startzelle hinter dem
        Bot) und Flag-Grab (nicht rückwärts greifen). Bewusst weiter als der Sicht-FoV."""
        if math.hypot(px - self.pos[0], py - self.pos[1]) < 1.0:
            return True
        angle_to = math.atan2(py - self.pos[1], px - self.pos[0])
        return abs(_angle_diff(angle_to, self.azimuth)) < AHEAD_HALF_ANGLE

    # ── Sichtbarkeit: zwei Kanäle (Radar / Fenster), zentral statt verstreut ──────
    def _enemy_visible_radar(self, info) -> bool:
        """Grundsätzliche Radar-Sicht auf den Gegner (Reichweite separat). Nur Stealth ist
        radar-unsichtbar; eigenes JM stört das eigene Radar komplett; eigenes SE deckt alles auf."""
        if self.own_flag == "SE": return True
        if self.own_flag == "JM": return False     # Radar gestört
        return info.flag != "ST"

    def _enemy_visible_window(self, info) -> bool:
        """Grundsätzliche Fenster-Sicht auf den Gegner (FoV/LoS separat). Nur Cloaking ist
        fenster-unsichtbar; eigenes B (Blind) macht fensterblind; eigenes SE deckt alles auf.
        (MQ ist sichtbar — nur die Team-Zugehörigkeit täuscht, s. _is_foe.)"""
        if self.own_flag == "SE": return True
        if self.own_flag == "B":  return False     # blind
        return info.flag != "CL"

    def _sees_in_window(self, info, x: float, y: float, z: float) -> bool:
        """Voller Fenster-Sichtkontakt: Flagge erlaubt Fenster-Sicht UND im FoV UND unverdeckt (LoS)."""
        return (self._enemy_visible_window(info)
                and self._in_fov(x, y)
                and self._has_los_to_point(x, y, z + TANK_HEIGHT * 0.5))

    # Schuss-Sichtbarkeit = Spiegelbild der Tank-Sichtbarkeit (SE betrifft nur Tanks, nicht Schüsse).
    def _shot_visible_radar(self, shooter) -> bool:
        if self.own_flag == "JM": return False     # eigenes Radar gestört → keine Schuss-Blips
        return shooter.flag != "IB"                # IB-Schüsse erscheinen nicht auf dem Radar

    def _shot_visible_window(self, shooter) -> bool:
        if self.own_flag == "B": return False      # blind
        return shooter.flag != "CS"                # CS-Schüsse sind out-the-window unsichtbar

    def _shot_reveals_shooter(self, shooter, ox: float, oy: float, oz: float) -> bool:
        """Ein wahrnehmbarer Schuss verrät die Schützen-Position: auf Radar ODER out-the-window
        (FoV + LoS zum Schuss-Ursprung)."""
        return (self._shot_visible_radar(shooter)
                or (self._shot_visible_window(shooter)
                    and self._in_fov(ox, oy) and self._has_los_to_point(ox, oy, oz)))

    def _should_update_player(self, info, px: float, py: float, pz: float, now: float) -> bool:
        """Übernimmt der Bot diese Gegnerposition jetzt?
        - Direkter Sichtkontakt (Fenster: FoV+LoS) → immer aktuell (man schaut ihn an).
        - Nur Radar → Radar-Aufmerksamkeit: pro Tick mit (1-skip) hinschauen; bei Fehlschlag für
          einen Cooldown ganz wegschauen (CL stärker). Weder Fenster noch Radar (ST/eigenes JM) → nie."""
        if self._sees_in_window(info, px, py, pz):
            return True
        if not self._enemy_visible_radar(info):
            return False
        if now < info.radar_blind_until:               # seit letztem Fehlschlag noch weggeschaut
            return False
        cl = info.flag == "CL" and self.own_flag != "SE"
        if random.random() >= (RADAR_SKIP_CL if cl else RADAR_SKIP_DEFAULT):
            return True                                # hingeschaut → Update
        info.radar_blind_until = now + (RADAR_COOLDOWN_CL if cl else RADAR_COOLDOWN_DEFAULT)
        return False                                   # weggeschaut → Cooldown

    def _find_incoming_shot(self, now: float, bot_vel=None):
        """Findet den gefährlichsten anfliegenden Schuss.
        Gibt (shot, t_threat) zurück; (None, inf) wenn kein Treffer.
        bot_vel: optionales (vx, vy)-Tupel für hypothetische Bot-Velocity (Standard: self.vel).
        Prüft sowohl direkte Schüsse als auch gecachte Ricochet-Pfade."""
        bvx = self.vel[0] if bot_vel is None else bot_vel[0]
        bvy = self.vel[1] if bot_vel is None else bot_vel[1]
        best = None
        best_t = float("inf")
        with self._shots_lock:
            for shot in self._shots.values():
                if shot.is_expired(now): continue
                if shot.shooter_id == self.player_id: continue
                # Ricochet-Schüsse: Richtung nach Bounce unklar → nur Segment-Cache prüfen
                if (shot.shooter_id, shot.shot_id) in self._ricochet_paths:
                    continue
                if shot.is_gm:
                    # GM: shot.pos wird laufend nachgeführt (Integration in
                    # _resolve_incoming_shots + MsgGMUpdate) — position_at() würde
                    # die bisherige Flugzeit ein ZWEITES Mal aufaddieren und die
                    # Rakete weit vor ihrer echten Position sehen (Phantom-Position,
                    # meist „entfernt sich" → GM wurde beim Ausweichen ignoriert).
                    sx, sy, sz = shot.pos
                else:
                    sx, sy, sz = shot.position_at(now)
                if shot.is_sw:
                    _sw_dist = math.hypot(sx - self.pos[0], sy - self.pos[1])
                    if self._shock_in_radius < _sw_dist < self._shock_out_radius:
                        sw_elapsed = max(0.0, now - shot.fire_time)
                        t = max(0.0,
                                (_sw_dist - self._shock_in_radius) / self._sw_expand_speed - sw_elapsed)
                        if t < best_t:
                            best_t = t; best = shot
                    continue  # SW hat vel≈0, normales d/t-Verfahren nicht anwendbar
                # Eingegrabener BU-Bot: nur SW und GM können ihn treffen
                if self.own_flag == "BU" and self.pos[2] < 0.0 and not shot.is_gm:
                    continue
                if abs(sz - self.pos[2]) > HIT_RADIUS * 2:
                    continue  # Schuss auf anderer Etage → keine Bedrohung
                # Relativgeschwindigkeit: Schuss minus Bot-Eigengeschwindigkeit
                # (ermöglicht Voraussage ob der Schuss den Bot trotz Ausweichen noch trifft)
                rvx = shot.vel[0] - bvx
                rvy = shot.vel[1] - bvy
                rx  = sx - self.pos[0]
                ry  = sy - self.pos[1]
                rel_spd_sq = rvx * rvx + rvy * rvy
                if rel_spd_sq > 1e-6:
                    t_rel_raw = -(rx * rvx + ry * rvy) / rel_spd_sq
                    if t_rel_raw < 0:
                        continue  # Schuss entfernt sich im rel. Bezugssystem → keine Bedrohung
                    t_rel = t_rel_raw
                    d = math.hypot(rx + rvx * t_rel, ry + rvy * t_rel)
                    t = t_rel
                else:
                    d = math.hypot(rx, ry)
                    t = 0.0
                if d < DODGE_DIST and t < best_t:
                    best_t = t; best = shot

            # --- Ricochet-Pfade: segmentweise prüfen ---
            for (pid, sid), segs in self._ricochet_paths.items():
                if pid == self.player_id:
                    continue
                shot = self._shots.get((pid, sid))
                if shot is None or shot.is_expired(now):
                    continue
                if self.own_flag == "BU" and self.pos[2] < 0.0:
                    continue
                for seg in segs:
                    if seg.t_end <= now:
                        continue  # Segment bereits abgelaufen
                    seg_dt = seg.t_end - seg.t_start
                    if seg_dt < 1.0e-9:
                        continue
                    t_from = max(seg.t_start, now)
                    frac = (t_from - seg.t_start) / seg_dt
                    sx = seg.px + (seg.ex - seg.px) * frac
                    sy = seg.py + (seg.ey - seg.py) * frac
                    sz = seg.pz + (seg.ez - seg.pz) * frac
                    if abs(sz - self.pos[2]) > HIT_RADIUS * 2:
                        continue
                    svx = (seg.ex - seg.px) / seg_dt
                    svy = (seg.ey - seg.py) / seg_dt
                    rvx = svx - bvx
                    rvy = svy - bvy
                    rx = sx - self.pos[0]
                    ry = sy - self.pos[1]
                    rel_spd_sq = rvx * rvx + rvy * rvy
                    seg_rem = seg.t_end - t_from
                    if rel_spd_sq > 1e-6:
                        t_rel = -(rx * rvx + ry * rvy) / rel_spd_sq
                        if t_rel < 0:
                            continue
                        t_rel = min(t_rel, seg_rem)
                        d = math.hypot(rx + rvx * t_rel, ry + rvy * t_rel)
                        t_threat = (t_from - now) + t_rel
                    else:
                        d = math.hypot(rx, ry)
                        t_threat = t_from - now
                    if d < DODGE_DIST and t_threat < best_t and t_threat < RICO_DODGE_LOOKAHEAD:
                        best_t = t_threat
                        best = shot
        return best, best_t

    def _effective_radar_range(self) -> float:
        """Liefert effektive Radar-Reichweite (25% wenn BU + pos[2] < 0)."""
        if self.own_flag == "BU" and self.pos[2] < 0.0:
            return RADAR_RANGE * 0.25
        return RADAR_RANGE

    def _find_target_player(self):
        """Wählt das nächste Ziel; None im Passivmodus."""
        # Kampf ist aktiv, sobald ein Mensch anwesend ist (Mitspieler ODER Zuschauer) — nicht nur
        # bei zielbaren Menschen. Peer-Bots auf Gegner-Teams sind gültige Ziele, damit die Bots
        # für Zuschauer lebendig wirken; ohne jeden Menschen (nur eigene Bots) bleibt es passiv.
        if not self._has_presence():
            return None
        best_id = None
        best_score = float("inf")
        _now = time.monotonic()
        for pid, info in list(self.players.items()):
            if pid == self.player_id or not info.alive:
                continue
            if info.paused:                             # pausiert = unverwundbar → kein Neu-Lock
                continue
            if _now - info.last_seen > ENEMY_STALE_S:   # zu lange nicht wahrgenommen → kein Re-Lock
                continue                                # (Gegenstück zu _get_enemy_pos: kein Geist-Lock)
            d = math.hypot(info.pos[0] - self.pos[0], info.pos[1] - self.pos[1])
            in_radar = d < self._effective_radar_range() and self._enemy_visible_radar(info)
            in_sight = False
            if d < SHOT_RANGE and self._enemy_visible_window(info):
                angle_to = math.atan2(
                    info.pos[1] - self.pos[1], info.pos[0] - self.pos[0])
                in_sight = (abs(_angle_diff(angle_to, self.azimuth)) < self._effective_fov()
                            and self._has_los_to_point(info.pos[0], info.pos[1],
                                                       info.pos[2] + TANK_HEIGHT * 0.5))
            if not in_radar and not in_sight:
                continue
            if not self._is_foe(info, in_sight):
                continue
            pz_penalty = 5.0 if info.is_phantom_zoned and self.own_flag not in ("SB", "SW") else 1.0
            st_gm_penalty = ST_GM_PENALTY if self.own_flag == "GM" and info.flag == "ST" else 1.0
            # Aktuell als unerreichbar gemiedener Gegner: weich deprioritisieren (nicht hart
            # überspringen) — ein erreichbarer Feind wird bevorzugt, der gemiedene aber weiter
            # gewählt, falls er der einzige ist.
            avoid_penalty = UNREACH_AVOID_PENALTY if self._combat_avoid.get(pid, 0.0) > _now else 1.0
            score = d * (0.8 if info.is_human else 1.0) * pz_penalty * st_gm_penalty * avoid_penalty
            if score < best_score:
                best_score = score; best_id = pid
        return best_id

    def _get_enemy_pos(self, pid: int):
        """Gibt (x, y) eines lebenden, kürzlich gesehenen Gegners zurück; sonst None."""
        info = self.players.get(pid)
        if info is None or not info.alive: return None
        if info.paused: return (info.pos[0], info.pos[1])  # Pausierte: Position bekannt → kein Geist-Drop
        if time.monotonic() - info.last_seen > ENEMY_STALE_S: return None
        return (info.pos[0], info.pos[1])

    def _dist_to_target(self) -> float:
        """Euklidische Distanz zum aktuellen Wegpunkt; inf wenn kein Wegpunkt."""
        if not self.target_pos: return float("inf")
        return math.hypot(self.target_pos[0] - self.pos[0],
                          self.target_pos[1] - self.pos[1])

    def _flags_on_route_all(self, gx: float, gy: float,
                            detour: float = 40.0) -> list[tuple[float, float]]:
        """Wie _flags_on_route, aber ohne good_flags-Filter (alle on-ground Flags).
        Für flagless-Modus mit breiterem Detour-Radius."""
        cx, cy = self.pos[0], self.pos[1]
        dx, dy = gx - cx, gy - cy
        dist2 = dx * dx + dy * dy
        if dist2 < 1.0:
            return []
        result: list[tuple[float, float, float]] = []
        for fi in list(self.flags.values()):
            if fi.status != 1:
                continue
            fx, fy = fi.pos[0], fi.pos[1]
            t = ((fx - cx) * dx + (fy - cy) * dy) / dist2
            if t <= 0.05 or t >= 0.95:
                continue
            proj_x = cx + t * dx
            proj_y = cy + t * dy
            if math.hypot(fx - proj_x, fy - proj_y) <= detour:
                result.append((t, fx, fy))
        result.sort()
        return [(fx, fy) for _, fx, fy in result]

    def _new_target(self) -> None:
        """Setzt Navigationsziel abhängig vom eigenen Flag-Zustand.
        Kein Flag: nächste on-ground Flag (Typ unbekannt).
        ID-Flag: gute Flags in IDENTIFY_RANGE bevorzugen, ID ggf. ablegen.
        Andere Flag: zufälliger Wegpunkt."""
        self.target_player = None

        # ── Fall A: Bot hat keine Flagge ─────────────────────────────────
        if self.own_flag == "":
            best_d: float = float("inf")
            best_pos = None
            _dropped = getattr(self, '_dropped_neutrals', ())
            _recent  = getattr(self, '_recent_flag_targets', ())
            for fi in list(self.flags.values()):
                if fi.status != 1:
                    continue
                if (round(fi.pos[0]), round(fi.pos[1])) in _recent:
                    continue
                if any(fi.abbr == a and math.hypot(fi.pos[0]-dx, fi.pos[1]-dy) < 20.0
                       for a, dx, dy in _dropped):
                    continue
                d = math.hypot(fi.pos[0] - self.pos[0], fi.pos[1] - self.pos[1])
                if d < best_d:
                    best_d = d
                    best_pos = (fi.pos[0], fi.pos[1], fi.pos[2])
            if best_pos is not None:
                self._recent_flag_targets.append((round(best_pos[0]), round(best_pos[1])))
                via = self._flags_on_route_all(best_pos[0], best_pos[1], detour=40.0)
                if via:
                    nav = getattr(self, "_nav_graph", None)
                    blocked = {k for k, v in self._nav_jump_cooldowns.items()
                               if v > time.monotonic()}
                    all_wps: list = []
                    px, py, pz = self.pos[0], self.pos[1], self.pos[2]
                    for fx, fy in via:
                        if nav:
                            seg = nav.plan_path(px, py, pz, fx, fy,
                                                blocked_jump_wps=blocked)
                            if seg:
                                all_wps.extend(seg)
                                px, py, pz = seg[-1][0], seg[-1][1], seg[-1][2]
                    if nav:
                        seg = nav.plan_path(px, py, pz,
                                            best_pos[0], best_pos[1],
                                            blocked_jump_wps=blocked,
                                            goal_z=best_pos[2])
                        if seg:
                            all_wps.extend(seg)
                    if all_wps:
                        if len(all_wps) > 1 and not self._is_ahead(all_wps[0][0], all_wps[0][1]):
                            all_wps.pop(0)
                        self._nav_path  = all_wps
                        self._nav_goal  = (best_pos[0], best_pos[1])
                        self.target_pos = (all_wps[0][0], all_wps[0][1])
                        self._wp_start_time = time.monotonic()
                        self._wp_fail_count = 0
                        self._wp_timeout = (WP_TIMEOUT_BASE
                                            + math.hypot(all_wps[0][0] - self.pos[0],
                                                         all_wps[0][1] - self.pos[1])
                                            * WP_TIMEOUT_SCALE)
                        return
                self._plan_path(best_pos[0], best_pos[1], best_pos[2])
                return

        # ── Fall B: Bot hat ID-Flagge ─────────────────────────────────────
        elif self.own_flag == "ID":
            _recent = getattr(self, '_recent_flag_targets', ())
            best_d_good: float = float("inf")
            best_pos_good = None
            for fi in list(self.flags.values()):
                if fi.status != 1:
                    continue
                d = math.hypot(fi.pos[0] - self.pos[0], fi.pos[1] - self.pos[1])
                if d < IDENTIFY_RANGE and fi.abbr in self.good_flags and d < best_d_good:
                    if getattr(self, '_debug_log_flag', False):
                        logger.debug("[%s] Flagge: ID-B1 – gute Flagge %r d=%.1fu (< %.0fu)",
                                     self.callsign, fi.abbr, d, IDENTIFY_RANGE)
                    best_d_good = d
                    best_pos_good = (fi.pos[0], fi.pos[1])
                elif fi.abbr:
                    if getattr(self, '_debug_log_flag', False):
                        logger.debug("[%s] Flagge: ID-B1 – keine gute Flagge %r d=%.1fu",
                                     self.callsign, fi.abbr, d)
            if best_pos_good is not None:
                d_to_good = math.hypot(best_pos_good[0] - self.pos[0],
                                       best_pos_good[1] - self.pos[1])
                if getattr(self, '_debug_log_flag', False):
                    logger.debug("[%s] Flagge: ID-B1 – Drop-Kandidat (%.0f,%.0f) d=%.1fu cooldown=%.1fs",
                                 self.callsign, best_pos_good[0], best_pos_good[1], d_to_good,
                                 time.monotonic() - self._last_drop_attempt)
                if time.monotonic() - self._last_drop_attempt > 1.0:
                    self._try_drop_flag()  # ID ablegen, damit gute Flag aufgesammelt werden kann
                if d_to_good > self._wp_reach_radius():
                    self._plan_path(best_pos_good[0], best_pos_good[1])
                # else: bereits am Ziel, warten bis Drop vom Server bestätigt wird
                return
            # Keine gute Flag in Erkennungsradius → nächste unbekannte Flag ansteuern
            if getattr(self, '_debug_log_flag', False):
                logger.debug("[%s] Flagge: ID-B2 – kein Ziel in %.0fu, scanne %d Flaggen (%d recent)",
                             self.callsign, IDENTIFY_RANGE, len(self.flags), len(_recent))
            best_d = float("inf")
            best_pos = None
            for fi in list(self.flags.values()):
                if fi.status != 1:
                    continue
                if (round(fi.pos[0]), round(fi.pos[1])) in _recent:
                    continue
                d = math.hypot(fi.pos[0] - self.pos[0], fi.pos[1] - self.pos[1])
                # Innerhalb IDENTIFY_RANGE bereits als nicht-gut erkannte Flags überspringen
                if d < IDENTIFY_RANGE and fi.abbr and fi.abbr not in self.good_flags:
                    continue
                if d < best_d:
                    best_d = d
                    best_pos = (fi.pos[0], fi.pos[1])
            if best_pos is not None:
                if getattr(self, '_debug_log_flag', False):
                    logger.debug("[%s] Flagge: ID-B2 – Ziel (%.0f,%.0f) d=%.1fu",
                                 self.callsign, best_pos[0], best_pos[1], best_d)
                self._recent_flag_targets.append((round(best_pos[0]), round(best_pos[1])))
                if best_d > self._wp_reach_radius():
                    self._plan_path(best_pos[0], best_pos[1])
                return

        # ── Fall C: Bot hat andere Flagge — zufälliger Wegpunkt ───────────
        h = self.world_half * 0.85
        best_gx = best_gy = 0.0
        best_score = -2.0
        for _ in range(5):
            cx_ = random.uniform(-h, h)
            cy_ = random.uniform(-h, h)
            cand_az = math.atan2(cy_ - self.pos[1], cx_ - self.pos[0])
            score = math.cos(abs(_angle_diff(cand_az, self.azimuth)))
            if score > best_score:
                best_score, best_gx, best_gy = score, cx_, cy_
        self._plan_path(best_gx, best_gy)

    def _plan_path(self, goal_x: float, goal_y: float,
                   goal_z: float | None = None, *, cap_wps: int | None = None) -> None:
        """Plant A*-Pfad zu (goal_x, goal_y); fällt auf Direktpfad zurück.

        cap_wps deckelt die WP-Anzahl (COMBAT: 8). Dauerte die Haupt-Thread-Suche länger als
        NAV_ASYNC_TRIGGER_MS, wird zusätzlich eine Hintergrund-Vollsuche angestoßen (P4-INF-01)."""
        nav = getattr(self, "_nav_graph", None)
        self._nav_goal = (goal_x, goal_y)
        # Jeder Plan-Request invalidiert ältere (in-flight/fertige) Async-Ergebnisse.
        self._plan_gen += 1

        # Kartenrand-Escape: nahe am Rand kann A* nicht planen (kein gültiger Start).
        # Nur die Achse(n) halbieren, die zu nah am Rand sind.
        _EDGE_MARGIN = 15.0
        _half = self.world_half
        if abs(self.pos[0]) > _half - _EDGE_MARGIN or abs(self.pos[1]) > _half - _EDGE_MARGIN:
            ex = (((_half / 2.0) * (1.0 if self.pos[0] > 0.0 else -1.0))
                  if abs(self.pos[0]) > _half - _EDGE_MARGIN else self.pos[0])
            ey = (((_half / 2.0) * (1.0 if self.pos[1] > 0.0 else -1.0))
                  if abs(self.pos[1]) > _half - _EDGE_MARGIN else self.pos[1])
            self._nav_path  = []
            self.target_pos = (ex, ey)
            self._wp_start_time = time.monotonic()
            self._wp_fail_count = 0
            self._wp_timeout    = (WP_TIMEOUT_BASE
                                   + math.hypot(ex - self.pos[0], ey - self.pos[1])
                                   * WP_TIMEOUT_SCALE)
            return

        if nav is None or self._can_drive_through_obstacles():
            if getattr(self, '_debug_log_path', False):
                logger.debug("[%s] Pfad: Direktpfad (%s) → (%.0f,%.0f)",
                             self.callsign,
                             "Flagge" if self._can_drive_through_obstacles() else "kein NavGraph",
                             goal_x, goal_y)
            self.target_pos = (goal_x, goal_y)
            self._nav_path  = []
            return
        blocked = {k for k, v in self._nav_jump_cooldowns.items() if v > time.monotonic()}
        sx, sy, sz = self.pos[0], self.pos[1], self.pos[2]
        # Reisegeschwindigkeit einmal snapshotten (Flaggen-Boost → weitere Sprünge planbar,
        # deckungsgleich zum reaktiven Executor). Plain-Value → reentrant an Sync- und Async-Plan.
        ts = self._travel_tank_speed()
        t0 = time.perf_counter()
        path = nav.plan_path(sx, sy, sz, goal_x, goal_y,
                             blocked_jump_wps=blocked, goal_z=goal_z,
                             label="Schnellplan", partial_level=logging.DEBUG,
                             tank_speed=ts)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if getattr(self, '_debug_log_path', False):
            logger.debug("[%s] Pfad: %d WPs von (%.0f,%.0f) → (%.0f,%.0f) [%.0fms]%s",
                         self.callsign, len(path), sx, sy, goal_x, goal_y, elapsed_ms,
                         f": {path[:2]}" if path else " → kein Pfad (Direktziel)")
        self._apply_planned_path(path, goal_x, goal_y, cap_wps)
        # War der Haupt-Thread-Plan teuer (Teilpfad-Verdacht), eine Vollsuche im Zweit-Thread
        # nachschieben — sie übernimmt später, falls sie eine bessere Route findet (P4-INF-01).
        if elapsed_ms > NAV_ASYNC_TRIGGER_MS:
            self._submit_async_plan(sx, sy, sz, goal_x, goal_y, goal_z,
                                    blocked, cap_wps, self._plan_gen, tank_speed=ts)

    def _apply_planned_path(self, path, goal_x: float, goal_y: float,
                            cap_wps: int | None = None) -> None:
        """Übernimmt einen A*-Pfad in den Navigations-State (Sync- UND Async-Pfad teilen das).
        Leerer Pfad → Direktziel auf (goal_x, goal_y). cap_wps deckelt die WP-Anzahl."""
        if path:
            # Start-Gitterzelle liegt ggf. leicht hinter dem Bot (world_to_cell-Trunkierung).
            # Einmalig überspringen wenn außerhalb ±90° — nie mehr als 1 WP entfernen.
            if len(path) > 1 and not self._is_ahead(path[0][0], path[0][1]):
                path = path[1:]
            if cap_wps is not None and len(path) > cap_wps:
                path = path[:cap_wps]      # COMBAT: max. cap_wps WPs (≈40u bei 8)
            self._nav_path = path
            self.target_pos = (path[0][0], path[0][1])
            self._wp_start_time = time.monotonic()
            self._wp_fail_count = 0
            self._wp_timeout = (WP_TIMEOUT_BASE
                                + math.hypot(path[0][0] - self.pos[0],
                                             path[0][1] - self.pos[1])
                                * WP_TIMEOUT_SCALE)
        else:
            self._nav_path  = []
            self.target_pos = (goal_x, goal_y)
            self._wp_start_time = time.monotonic()
            self._wp_fail_count = 0
            self._wp_timeout = (WP_TIMEOUT_BASE
                                + math.hypot(goal_x - self.pos[0],
                                             goal_y - self.pos[1])
                                * WP_TIMEOUT_SCALE)

    def _submit_async_plan(self, sx: float, sy: float, sz: float,
                           goal_x: float, goal_y: float, goal_z: float | None,
                           blocked: set, cap_wps: int | None, gen: int,
                           tank_speed: float | None = None) -> None:
        """Startet (höchstens einen) Hintergrund-Thread mit großen A*-Limits (P4-INF-01).

        Der NavGraph ist reentrant; der Worker bekommt nur Plain-Value-Snapshots und liest nie
        self.*-Mutables. Läuft bereits eine Suche, wird sie nur bei Ziel-Wechsel kooperativ
        abgebrochen (sonst weiterlaufen lassen) und KEINE zweite gestartet."""
        nav = getattr(self, "_nav_graph", None)
        if nav is None:
            return
        th = self._async_plan_thread
        if th is not None and th.is_alive():
            if self._async_plan_goal != (goal_x, goal_y):
                self._async_cancel.set()   # in-flight-Suche ist stale → schnell raus
            return
        self._async_cancel.clear()
        self._async_plan_goal = (goal_x, goal_y)
        cancel = self._async_cancel

        def _worker():
            try:
                p = nav.plan_path(sx, sy, sz, goal_x, goal_y,
                                  blocked_jump_wps=blocked, goal_z=goal_z,
                                  max_expansions=NAV_ASYNC_MAX_EXPANSIONS,
                                  max_ms=NAV_ASYNC_MAX_MS, cancel=cancel,
                                  label="Vollsuche", partial_level=logging.INFO,
                                  tank_speed=tank_speed)
            except Exception:
                logger.exception("[%s] Async-Pfadplanung fehlgeschlagen", self.callsign)
                p = None
            with self._async_plan_lock:
                self._async_plan_result = (gen, goal_x, goal_y, goal_z, cap_wps, sx, sy, p)

        th = threading.Thread(target=_worker, name=f"navplan-{self.callsign}", daemon=True)
        self._async_plan_thread = th
        th.start()

    def _trim_traversed_prefix(self, path):
        """Trimmt den bereits abgefahrenen Pfad-Prefix vor der Async-Übernahme.

        Findet das bot-nächste Pfad-Segment per 3D-Punkt-zu-Strecke-Projektion (robust gegen
        _smooth_path, das flache Geraden auf weit auseinanderliegende Endpunkte kürzt — reines
        Nächster-WP würde den Bot mittig auf langen Segmenten fälschlich „off-route" einstufen) und
        gibt den Rest ab dessen Startknoten zurück. So übernimmt der Bot die Hintergrund-Route an
        seinem aktuellen Fortschritt, statt zu WP0 zurückzudrehen. z fließt ein, damit eine
        xy-nahe Oberetage nicht fälschlich matcht.
        Rückgabe: (gedroppte WP-Anzahl, perpendikulärer Routen-Abstand, Rest-Pfad)."""
        px, py, pz = self.pos[0], self.pos[1], self.pos[2]
        if len(path) == 1:
            wx, wy, wz = path[0]
            return 0, math.hypot(wx - px, wy - py, wz - pz), path
        best_i, best_d = 0, math.inf
        for i in range(len(path) - 1):
            ax, ay, az = path[i]
            bx, by, bz = path[i + 1]
            dx, dy, dz = bx - ax, by - ay, bz - az
            seg2 = dx * dx + dy * dy + dz * dz
            if seg2 <= 1e-9:                       # degeneriertes Segment (z.B. reiner z-Sprung)
                d = math.hypot(ax - px, ay - py, az - pz)
            else:
                t = ((px - ax) * dx + (py - ay) * dy + (pz - az) * dz) / seg2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                d = math.hypot(ax + t * dx - px, ay + t * dy - py, az + t * dz - pz)
            if d < best_d:
                best_d, best_i = d, i
        return best_i, best_d, path[best_i:]

    def _poll_async_plan(self) -> None:
        """Übernimmt ein fertiges, noch relevantes Async-Ergebnis. O(1) wenn nichts ansteht.
        Pro KI-Tick aus _dispatch_movement (nur Bodenstates) aufgerufen."""
        with self._async_plan_lock:
            res = self._async_plan_result
            self._async_plan_result = None
        if res is None:
            return
        gen, gx, gy, gz, cap_wps, sx, sy, path = res
        # ── Relevanz: nur übernehmen, wenn der Request noch aktuell und brauchbar ist ──
        if gen != self._plan_gen:
            return                                   # neuerer Plan-Request seither → verworfen
        if not path:
            return                                   # nichts gefunden → Sync-Direktziel behalten
        if self._nav_goal != (gx, gy):
            return                                   # Ziel hat gewechselt
        if not self.alive:
            return                                   # tot/respawnt seither
        if self._can_drive_through_obstacles():
            return                                   # Flaggen-Direktmodus: keine A*-Route fahren
        # Statt bei reinem Vorfahren zu verwerfen: bereits abgefahrenen Prefix überspringen und die
        # Route an der aktuellen Bot-Position übernehmen. Nur bei echtem Off-Route (weggeschossen/
        # teleportiert/divergente Route) verwerfen — der nächste Sync-Plan deckt das ab.
        dropped, route_d, path = self._trim_traversed_prefix(path)
        if route_d > NAV_ASYNC_RESYNC_TOL:
            return                                   # Bot nicht (mehr) auf dieser Route
        if getattr(self, '_debug_log_path', False):
            logger.debug("[%s] Pfad: Async-Vollsuche übernommen (%d WPs, %d Prefix gedroppt, d=%.1f) "
                         "→ (%.0f,%.0f)", self.callsign, len(path), dropped, route_d, gx, gy)
        self._apply_planned_path(path, gx, gy, cap_wps)

    def _advance_path(self, *, timed_out: bool = False) -> None:
        """Rückt im Pfad vor; löst NAV_JUMP aus wenn nächster WP auf anderer Etage."""
        if not timed_out:
            self._wp_fail_count = 0
        nav_path = getattr(self, "_nav_path", [])
        if nav_path:
            _reached_wp   = nav_path[0]
            _reached_dist = math.hypot(_reached_wp[0] - self.pos[0],
                                       _reached_wp[1] - self.pos[1])
            nav_path.pop(0)
        else:
            _reached_wp, _reached_dist = None, 0.0
        if nav_path:
            wp = nav_path[0]
            if getattr(self, '_debug_log_path', False):
                logger.debug(
                    "[%s] Pfad: WP (%s dist=%.2f timed=%s) → (%.0f,%.0f,z=%.1f), %d verbleibend",
                    self.callsign,
                    f"{_reached_wp[0]:.0f},{_reached_wp[1]:.0f}" if _reached_wp else "?",
                    _reached_dist, timed_out,
                    wp[0], wp[1], wp[2], len(nav_path))
            floor_z = self._get_floor_z()
            # P3-NAV-02-Folgefix: Teleport-Exit-WP (z.B. z=30 am Ziel-Tor) wird NICHT angesprungen,
            # sondern durch das Tor angefahren — der reaktive _check_teleport_crossing warpt den Bot
            # samt Höhe. Sonst löste der z-Sprung des Exit-WP hier fälschlich NAV_JUMP aus.
            _nav = getattr(self, "_nav_graph", None)
            _is_tele_exit = (_nav is not None
                             and (round(wp[0], 1), round(wp[1], 1)) in getattr(_nav, "_tele_exit_wps", set()))
            # OO ausgeschlossen: man kann mit OO nicht auf einem Dach landen (fällt zurück durch) →
            # ein Sprung dorthin wäre sinnlos und würde sich endlos wiederholen; WP wird am Boden phasend angefahren.
            if wp[2] - floor_z > 1.5 and self.own_flag not in ("NJ", "BU", "OO") and not _is_tele_exit:
                if self._nav_jump_feasible(wp):
                    self._wp_start_time = None
                    self._initiate_nav_jump(wp)
                    return
                if not self._nav_jump_geometry_ok(wp):
                    wp_key = (round(wp[0]), round(wp[1]), wp[2])
                    self._nav_jump_cooldowns[wp_key] = time.monotonic() + 30.0
                    self._nav_path = []
                    self._nav_goal = None
                    self._wp_start_time = None
                    self.target_pos = None
                    return
                # Geometrie OK, Azimuth falsch → NAV_JUMP_ALIGN (aber erst Cooldown prüfen)
                wp_key = (round(wp[0]), round(wp[1]), wp[2])
                if self._nav_jump_cooldowns.get(wp_key, 0) > time.monotonic():
                    self._nav_path = []
                    self._nav_goal = None
                    self._wp_start_time = None
                    self.target_pos = None
                    return
                az_to_wp = math.atan2(wp[1] - self.pos[1], wp[0] - self.pos[0])
                self._nav_jump_align_wp = wp
                self._nav_jump_align_start = time.monotonic()
                self._nav_jump_align_return_state = self._ai_state
                self._transition_to(AIState.NAV_JUMP_ALIGN)
                return
            # Tor-Austritts-WP erreicht (Eingang gerade abgehakt) → letztes Stück direkt in die
            # Tor-Mitte fahren (NAV_TELE), statt am mittenseitigen Exit-WP davor zu stoppen.
            if _is_tele_exit:
                center = getattr(_nav, "_tele_cross_centers", {}).get(
                    (round(wp[0], 1), round(wp[1], 1)))
                if center is not None and self._try_engage_nav_tele(center):
                    return
            self.target_pos = (wp[0], wp[1])
            self._wp_start_time = time.monotonic()
            self._wp_timeout = (WP_TIMEOUT_BASE
                                + math.hypot(wp[0] - self.pos[0],
                                             wp[1] - self.pos[1])
                                * WP_TIMEOUT_SCALE)
        else:
            if getattr(self, '_debug_log_path', False):
                logger.debug("[%s] Pfad: Fertig → Neuziel", self.callsign)
            self._wp_start_time = None
            if self._ai_state == AIState.COMBAT and self.target_player is not None:
                # Im COMBAT kein _new_target() — _nav_goal auf aktuelle Enemy-XY setzen
                # damit _execute_combat_move nicht sofort replant (dist(ep, ep_now) ≈ 0).
                _ep_now = self._get_enemy_pos(self.target_player)
                self.target_pos = None
                self._nav_path  = []
                self._nav_goal  = (_ep_now[0], _ep_now[1]) if _ep_now is not None else None
            else:
                self._new_target()

    def _nav_jump_land_spin(self, wp, v0: float, g_abs: float) -> float:
        """Im Sprung fixe Drehrate, sodass der Bot ausgerichtet landet: auf den Gegner, wenn der
        Landepunkt auf Gegner-Höhe liegt, sonst auf den nächsten Wegpunkt. Fallback 0.0 (WG:
        bestehende Rate). Auf _tank_turn_rate gedeckelt — BZFlag: am Absprung fixiert, in der Luft
        unveränderlich. vel[0/1] müssen vor dem Aufruf gesetzt sein."""
        fallback = self._jump_ang_vel if self.own_flag == "WG" else 0.0
        disc = v0 * v0 - 2.0 * g_abs * (wp[2] - self.pos[2])
        if disc < 0:
            return fallback
        t_flight = (v0 + math.sqrt(disc)) / max(g_abs, 1e-6)   # absteigende Nulldurchgangszeit
        if t_flight < 1e-3:
            return fallback
        # Lande-Ziel wählen: Gegner nur, wenn der Landepunkt auf seiner Etage liegt; sonst nächster WP.
        target = None
        if self.target_player is not None:
            ep   = self._get_enemy_pos(self.target_player)     # lebend + <10s gesehen, sonst None
            info = self.players.get(self.target_player)
            if ep is not None and info is not None and abs(wp[2] - info.pos[2]) <= NAV_JUMP_Z_TOL:
                target = ep
        if target is None:
            nav_path = getattr(self, "_nav_path", [])          # Invariante: nav_path[0] == wp
            if len(nav_path) >= 2:
                target = (nav_path[1][0], nav_path[1][1])
        if target is None:
            return fallback
        lx = self.pos[0] + self.vel[0] * t_flight              # voraussichtl. Landepunkt (ballistisch)
        ly = self.pos[1] + self.vel[1] * t_flight
        delta = _angle_diff(math.atan2(target[1] - ly, target[0] - lx), self.azimuth)
        return math.copysign(min(abs(delta / t_flight), self._tank_turn_rate), delta)

    def _initiate_nav_jump(self, wp) -> None:
        """Startet Navigationssprung zu Wegpunkt wp = (wx, wy, layer_z)."""
        self._nav_jump_target_z = wp[2]
        v0 = self._effective_jump_velocity()
        g_abs = abs(self._effective_gravity())
        self.vel[2] = self._jump_launch_vz(self.vel[2])
        dz       = wp[2] - self.pos[2]
        hdist    = math.hypot(wp[0] - self.pos[0], wp[1] - self.pos[1])
        az_to_wp = math.atan2(wp[1] - self.pos[1], wp[0] - self.pos[0])
        disc     = v0 * v0 - 2.0 * g_abs * dz
        _ts      = self._travel_tank_speed()   # deckungsgleich zur Sprungkanten-Planung (Flaggen-Boost)
        needed_hspeed = _ts
        if disc >= 0 and hdist > 0.5:
            t_desc    = (v0 + math.sqrt(disc)) / g_abs
            # 4d+4b: hdist + 2.5u Überschuss kompensiert Versatz vom Absprung-WP
            hdist_aim = hdist + 2.5
            calc      = hdist_aim / max(t_desc, 0.01)
            if 1.0 < calc <= _ts:
                needed_hspeed = calc
        # Velocity in Blickrichtung (self.azimuth) — NAV_JUMP_ALIGN hat Ausrichtung sichergestellt
        self.vel[0]       = math.cos(self.azimuth) * needed_hspeed
        self.vel[1]       = math.sin(self.azimuth) * needed_hspeed
        self._jumping      = True
        # Lande-Drehung am Absprung fixieren (BZFlag: in der Luft unveränderlich, außer WG):
        # zum nächsten Wegpunkt, bzw. zum Gegner wenn der Landepunkt auf Gegner-Höhe liegt.
        self._jump_ang_vel = self._nav_jump_land_spin(wp, v0, g_abs)
        self.ang_vel       = 0.0
        # Return-State über NAV_JUMP_ALIGN hinweg auf den echten Eigentümer (COMBAT/SEEKING)
        # auflösen — sonst landet der Bot nach dem Sprung wieder in NAV_JUMP_ALIGN und der
        # 5-s-Timeout „steigt auf sich selbst aus" (No-Op) → Endlosfalle.
        _owner = self._ai_state
        if _owner == AIState.NAV_JUMP_ALIGN:
            _owner = getattr(self, "_nav_jump_align_return_state", AIState.SEEKING)
        self._nav_jump_return_state = _owner
        self._transition_to(AIState.NAV_JUMP)
        logger.info("[%s] NAV_JUMP → (%.1f, %.1f, z=%.1f) hdist=%.1fu hspeed=%.1f az=%.0f° ziel=%.0f°",
                    self.callsign, wp[0], wp[1], wp[2],
                    hdist, needed_hspeed,
                    math.degrees(self.azimuth), math.degrees(az_to_wp))

    def _try_engage_nav_tele(self, center) -> bool:
        """Startet NAV_TELE, wenn die Tor-Mitte nah genug und nicht gesperrt ist.

        Gibt True zurück, wenn der State gewechselt wurde. Bei False fährt der Aufrufer den
        (mittenseitigen) Austritts-WP wie bisher normal an — als Fallback, falls die Mitte noch
        zu weit weg ist (dann nähert sich der Bot und re-triggert beim nächsten Advance)."""
        cx, cy = center
        now = time.monotonic()
        self._nav_tele_cooldowns = {k: v for k, v in self._nav_tele_cooldowns.items() if v > now}
        dist = math.hypot(cx - self.pos[0], cy - self.pos[1])
        key = (round(cx), round(cy))
        if dist > NAV_TELE_ENGAGE_DIST or self._nav_tele_cooldowns.get(key, 0) > now:
            if getattr(self, "_debug_log_tele", False):
                logger.debug("[%s] NAV_TELE nicht engaged (dist=%.1fu cd=%s)",
                             self.callsign, dist, self._nav_tele_cooldowns.get(key, 0) > now)
            return False
        self._nav_tele_center = (cx, cy)
        self._nav_tele_start = now
        self._nav_tele_return_state = self._ground_state()
        self.target_pos = (cx, cy)
        self._wp_start_time = None
        self._transition_to(AIState.NAV_TELE)
        logger.info("[%s] NAV_TELE → Tor-Mitte (%.1f, %.1f) von (%.1f, %.1f) dist=%.1fu",
                    self.callsign, cx, cy, self.pos[0], self.pos[1], dist)
        return True

    # ── Taktischer Übersprung ─────────────────────────────────────────────

    def _check_tactical_jump(self, now: float) -> bool:
        """Taktischer Übersprung: Wind-Up (→ JUMP_WINDUP) dann Sprung (→ JUMPING).

        Szenario 4 (Frontalsprung): Gegner schaut auf Bot zu.
          - Bot schaut Gegner an (< 45°): Vorwärtssprung + 180°-Flip
          - Bot zeigt Rücken (> 135°):    Rückwärtssprung, kein Flip
        Szenario 5 (Escape-Jump): Gegner kommt schnell und nah.
        """
        if not self._can_jump(now):
            return False
        if now < self._tact_jump_retry_after:
            return False
        if self.target_player is None:
            return False
        ep = self._get_enemy_pos(self.target_player)
        if ep is None:
            return False
        info = self.players.get(self.target_player)
        if info is None or not info.alive:
            return False
        # TACT-02: Ziel trägt Schockwelle → kein TactJump (Sprung führt in die SW-Kuppel)
        if info.flag == "SW":
            return False
        z_diff = abs(info.pos[2] - self.pos[2])
        if z_diff > HIT_RADIUS:
            return False
        dx, dy = ep[0] - self.pos[0], ep[1] - self.pos[1]
        dist = math.hypot(dx, dy)
        if dist < 5.0 or dist > OPTIMAL_RANGE * 2:
            return False
        dir_x, dir_y = dx / dist, dy / dist
        raw_closing = -(info.vel[0] * dir_x + info.vel[1] * dir_y)
        enemy_closing = max(0.0, raw_closing)  # für Sz5 + faces-Fallback
        t_jump = 2.0 * self._effective_jump_velocity() / max(abs(self._effective_gravity()), 0.001)
        # Klärungs-Check: Bot muss TACT_JUMP_CLEARANCE-fach hinter dem Gegner landen.
        # Annäherung nur über die Reaktionszeit gutschreiben (Gegner kann nicht instant
        # bremsen/zurücksetzen); Rückzug über die volle Flugzeit (Gegner bewegt sich schon weg).
        t_closing = min(t_jump, TACT_JUMP_REACTION_S) if raw_closing > 0.0 else t_jump
        enemy_dist_at_land = dist - raw_closing * t_closing
        if self._tank_speed * t_jump < enemy_dist_at_land * TACT_JUMP_CLEARANCE:
            return False
        enemy_az = math.atan2(dy, dx)
        angle_to_enemy = abs(_angle_diff(self.azimuth, enemy_az))
        angle_enemy_to_bot = abs(_angle_diff(info.azimuth, _wrap(enemy_az + math.pi)))

        # TACT-01: Gegner mit Rücken zum Bot → kein TactJump (würde vor Gegner landen)
        if angle_enemy_to_bot > math.radians(120):
            return False

        # Szenario 4: Gegner muss Bot im Blickfeld haben (≤15°) oder schnell nähern
        enemy_faces_bot = (angle_enemy_to_bot < math.radians(15)) or (enemy_closing > 5.0)

        if enemy_faces_bot:
            if angle_to_enemy < math.radians(15):
                # Vorwärtssprung + 180°-Flip
                if random.random() >= 0.5:
                    self._tact_jump_retry_after = now + 2.0
                    return False
                turn_sign = math.copysign(1.0, _angle_diff(enemy_az, self.azimuth))
                self._escape_jump_ang_vel = turn_sign * self._tank_turn_rate
                self._dodge_dir   = enemy_az
                self._dodge_forward = True
                self._dodge_reverse = False
                self._dodging     = True
                self._dodge_until = now + 0.12
                self._jump_pending = True
                self._tactical_jump_until = now + 0.5
                self._transition_to(AIState.JUMP_WINDUP)
                logger.info("[%s] Übersprung-Vorwärts (dist=%.0f, az=%.0f°)",
                            self.callsign, dist, math.degrees(angle_to_enemy))
                return True
            elif angle_to_enemy > math.radians(135):
                # Rückwärtssprung, kein Flip — nur wenn Gegner sich nähert (>= 5 m/s)
                if enemy_closing < 5.0: return False
                if random.random() >= 0.5:
                    self._tact_jump_retry_after = now + 2.0
                    return False
                self._escape_jump_ang_vel = None
                self._dodge_dir   = enemy_az
                self._dodge_forward = False
                self._dodge_reverse = True
                self._dodging     = True
                self._dodge_until = now + 0.12
                self._jump_pending = True
                self._tactical_jump_until = now + 0.5
                self._transition_to(AIState.JUMP_WINDUP)
                logger.info("[%s] Übersprung-Rückwärts (dist=%.0f, az=%.0f°)",
                            self.callsign, dist, math.degrees(angle_to_enemy))
                return True
            # Zwischenwinkel (45°–135°): fällt zu Szenario 5 durch

        # Szenario 5: Escape-Jump (Bot nicht frontal zum Gegner, Gegner schließt schnell)
        if (enemy_closing > 10.0 and dist < OPTIMAL_RANGE
                and angle_to_enemy >= math.radians(45)):
            if random.random() >= 0.5:
                self._tact_jump_retry_after = now + 2.0
                return False
            turn_sign = math.copysign(1.0, _angle_diff(enemy_az, self.azimuth))
            self._escape_jump_ang_vel = turn_sign * self._tank_turn_rate
            self._dodge_dir    = self.azimuth
            self._dodge_until  = now + 0.08
            self._dodging      = True
            self._dodge_forward  = True
            self._dodge_reverse  = False
            self._jump_pending   = True
            self._tactical_jump_until = now + 0.4
            self._transition_to(AIState.JUMP_WINDUP)
            logger.info("[%s] Escape-Jump-Wind-Up (dist=%.0f, closing=%.1f)",
                        self.callsign, dist, enemy_closing)
            return True

        return False

    # ── Z-Höhen-Sprung (ZJ1) ─────────────────────────────────────────────

    def _segment_clear(self, ox: float, oy: float, oz: float,
                       ex: float, ey: float, ez: float) -> bool:
        """True, wenn keine solide Box (nav._los_obs, shoot_through=False) das Segment
        (ox,oy,oz)→(ex,ey,ez) schneidet. Generischer Slab-Test mit frei wählbarem Ursprung —
        Basis für _has_los_to_point (Bot-Auge) und das GM-Aktivierungspunkt-Gate (beliebige Punkte).
        Teleporter zählen bewusst NICHT als Blocker (Schuss-/Kurven-Routing s. _has_los_to_enemy)."""
        nav = getattr(self, "_nav_graph", None)
        if nav is None:
            return True
        dx = ex - ox; dy = ey - oy; dz = ez - oz
        # Broad-Phase: nur Boxen entlang des Strahls (DDA) statt linear über alle _los_obs.
        # Fallback (nav ohne _los_grid, z.B. Test-Stub) auf den linearen Scan.
        _grid = getattr(nav, "_los_grid", None)
        _boxes = _grid.query_ray(ox, oy, ex, ey) if _grid is not None else nav._los_obs
        for box in _boxes:
            cos_a = box.cos_a; sin_a = box.sin_a
            rx = ox - box.cx; ry = oy - box.cy
            lox =  rx * cos_a + ry * sin_a
            loy = -rx * sin_a + ry * cos_a
            ldx =  dx * cos_a + dy * sin_a
            ldy = -dx * sin_a + dy * cos_a
            t_min = 0.0; t_max = 1.0; hit = True
            for o_v, d_v, lo_v, hi_v in (
                (lox, ldx, -box.half_w,   box.half_w),
                (loy, ldy, -box.half_d,   box.half_d),
                (oz,  dz,   box.bottom_z, box.bottom_z + box.height),
            ):
                if abs(d_v) < 1e-9:
                    if o_v < lo_v or o_v > hi_v:
                        hit = False; break
                else:
                    t1 = (lo_v - o_v) / d_v; t2 = (hi_v - o_v) / d_v
                    t_min = max(t_min, min(t1, t2))
                    t_max = min(t_max, max(t1, t2))
            if hit and t_min <= t_max:
                return False
        return True

    def _steep_wall_ahead(self, az: float, max_dist: float) -> Optional[float]:
        """COMBAT-Direktmodus-Vorausschau: castet einen horizontalen Strahl der Länge max_dist von
        der Tank-Mitte (Augenhöhe) entlang az gegen die soliden LoS-Boxen. Trifft er die NÄCHSTE
        Wand in steilem Winkel (Einfallswinkel zur Oberfläche > NAV_WALL_STEEP_DEG → der Wall-Slide
        nullt dann fast den ganzen Vortrieb), liefert er die nach vorn gerichtete Wand-Tangente
        (Azimut) zum Entlanggleiten/Abdrehen. Sonst None: flacher Winkel (Gleiten ist ok) oder
        freie Bahn. Slab-Mathematik wie _segment_clear; Box-angle deckt gedrehte Wände ab."""
        nav = getattr(self, "_nav_graph", None)
        if nav is None or max_dist <= 0.0:
            return None
        ox = self.pos[0]; oy = self.pos[1]; oz = self.pos[2] + TANK_HEIGHT * 0.5
        dx = math.cos(az) * max_dist; dy = math.sin(az) * max_dist
        best_t = 2.0; best_axis = -1; best_box = None
        # Broad-Phase: nur Boxen entlang des Strahls (DDA); Fallback auf linearen Scan ohne _los_grid.
        _grid = getattr(nav, "_los_grid", None)
        _boxes = _grid.query_ray(ox, oy, ox + dx, oy + dy) if _grid is not None else nav._los_obs
        for box in _boxes:
            cos_a = box.cos_a; sin_a = box.sin_a
            rx = ox - box.cx; ry = oy - box.cy
            lox =  rx * cos_a + ry * sin_a
            loy = -rx * sin_a + ry * cos_a
            ldx =  dx * cos_a + dy * sin_a
            ldy = -dx * sin_a + dy * cos_a
            t_min = 0.0; t_max = 1.0; hit = True; t_min_axis = -1
            for ax, (o_v, d_v, lo_v, hi_v) in enumerate((
                (lox, ldx, -box.half_w,   box.half_w),
                (loy, ldy, -box.half_d,   box.half_d),
                (oz,  0.0,  box.bottom_z, box.bottom_z + box.height),
            )):
                if abs(d_v) < 1e-9:
                    if o_v < lo_v or o_v > hi_v:
                        hit = False; break
                else:
                    t1 = (lo_v - o_v) / d_v; t2 = (hi_v - o_v) / d_v
                    t_near = min(t1, t2)
                    if t_near > t_min:
                        t_min = t_near; t_min_axis = ax
                    t_max = min(t_max, max(t1, t2))
            if not hit or t_min > t_max or t_min >= best_t or t_min_axis not in (0, 1):
                continue   # kein Treffer / weiter weg / Eintritt über Z-Ebene (Dach/Boden)
            best_t = t_min; best_axis = t_min_axis; best_box = box
        if best_axis < 0 or best_box is None:
            return None
        # Einfallswinkel zur getroffenen Fläche: Normalkomponente der (normierten) Fahrtrichtung
        # entlang der Eintritts-Achse. dz=0 → |Richtung| == max_dist. Steil ⇔ Komponente > sin(60°).
        cos_a = best_box.cos_a; sin_a = best_box.sin_a
        ndx = ( dx * cos_a + dy * sin_a) / max_dist
        ndy = (-dx * sin_a + dy * cos_a) / max_dist
        normal_comp = abs(ndx) if best_axis == 0 else abs(ndy)
        if normal_comp <= math.sin(math.radians(NAV_WALL_STEEP_DEG)):
            return None   # flacher Winkel → der Bot gleitet sauber an der Wand entlang
        # Wand-Tangente in Weltkoordinaten (Fläche ⟂ Eintritts-Normale), nach vorn gerichtet.
        if best_axis == 0:      # x-Fläche getroffen → Tangente entlang lokaler y-Achse
            tx, ty = -sin_a, cos_a
        else:                   # y-Fläche getroffen → Tangente entlang lokaler x-Achse
            tx, ty = cos_a, sin_a
        if dx * tx + dy * ty < 0.0:
            tx, ty = -tx, -ty
        return math.atan2(ty, tx)

    def _has_los_to_point(self, ex: float, ey: float, ez: float) -> bool:
        """Reine Sicht-LoS: True, wenn keine solide Box zwischen Bot-Auge und (ex,ey,ez) liegt.
        Teleporter blockieren KEINE Sicht (das ist nur Schuss-LoS, s. _has_los_to_enemy)."""
        return self._segment_clear(self.pos[0], self.pos[1], self.pos[2] + TANK_HEIGHT * 0.5,
                                   ex, ey, ez)

    def _muzzle_clear(self, az: float) -> bool:
        """True, wenn die Mündung (pos + Richtung az * _muzzle_front, auf Mündungshöhe) NICHT
        hinter/in einer soliden Wand steckt. Der reale Schuss spawnt an der Mündung (s. _send_shot);
        liegt eine dünne Wand zwischen Tank-Mitte und Mündung, würde bzfs den Schuss serverseitig
        'fressen' bzw. er ginge unfair durch die Wand → solche Schüsse unterdrücken (s. _maybe_shoot)."""
        # P4a: Per-Tick-Memo — wird pro Tick mit identischem az mehrfach geprüft.
        memo = getattr(self, "_tick_memo", None)
        key = ("muzzle", az, self.pos[0], self.pos[1], self.pos[2])
        if memo is not None:
            cached = memo.get(key)
            if cached is not None:
                return cached
        mz = self.pos[2] + self._muzzle_height
        mx = self.pos[0] + math.cos(az) * self._muzzle_front
        my = self.pos[1] + math.sin(az) * self._muzzle_front
        result = self._segment_clear(self.pos[0], self.pos[1], mz, mx, my, mz)
        if memo is not None:
            memo[key] = result
        return result

    def _has_los_to_enemy(self, target_pid: int) -> bool:
        """True wenn weder eine undurchschießbare Box noch ein verlinktes Teleporter-Feld zwischen
        Bot und Gegner liegt (Schuss-LoS — ein Direktschuss durch ein Tor würde wegteleportiert)."""
        info = self.players.get(target_pid) if target_pid else None
        if info is None or not info.alive:
            return True
        ex = info.pos[0]; ey = info.pos[1]; ez = info.pos[2] + TANK_HEIGHT * 0.5
        # P4a: Per-Tick-Memo — bis zu 3× pro Tick identisch aufgerufen
        # (_execute_combat_move 2×, _maybe_shoot_standard 1×). Key enthält
        # beide Positionen → bewegt sich der Gegner mittendrin (Recv-Thread),
        # gibt es schlicht einen Miss statt eines stalen Treffers.
        memo = getattr(self, "_tick_memo", None)
        key = ("los", target_pid, self.pos[0], self.pos[1], self.pos[2], ex, ey, ez)
        if memo is not None:
            cached = memo.get(key)
            if cached is not None:
                return cached
        result = True
        if not self._has_los_to_point(ex, ey, ez):
            result = False
        else:
            # Teleporter-Feld zwischen Bot und Ziel → ein Direktschuss würde wegteleportiert,
            # also kein sauberer Direktschuss (der indirekte Aim-Sweep übernimmt dann, s. A4).
            wm = getattr(self, "_world_map", None)
            if wm and wm.teleporters:
                ox = self.pos[0]; oy = self.pos[1]; oz = self.pos[2] + TANK_HEIGHT * 0.5
                dx = ex - ox; dy = ey - oy; dz = ez - oz
                lmap = getattr(self, "_link_map", {})
                for ti, tele in enumerate(wm.teleporters):
                    res = ray_teleporter_crossing(ox, oy, oz, dx, dy, dz, tele)
                    if res is None:
                        continue
                    t_cross, face = res
                    if 0.0 < t_cross < 1.0 and (ti * 2 + face) in lmap:
                        result = False
                        break
        if memo is not None:
            memo[key] = result
        return result

    def _z_attack_feasible(self, now: float) -> bool:
        """Prüft ob Z_ATTACK grundsätzlich möglich ist (ohne Zufalls-Gate, kein Sprung)."""
        if not self._can_jump(now):
            return False
        info = self.players.get(self.target_player) if self.target_player else None
        if info is None or not info.alive or info.is_airborne:
            return False
        ep = self._get_enemy_pos(self.target_player)
        if ep is None:
            return False
        enemy_z = info.pos[2]
        z_diff = enemy_z - self.pos[2]
        max_jump_h = self._effective_jump_height()
        if z_diff <= HIT_RADIUS or z_diff >= max_jump_h:
            return False
        fire_rel = min(z_diff + 1.0, max_jump_h - 0.5)   # relativ zum Absprung (s. _check_z_attack_jump)
        v0 = self._effective_jump_velocity()
        g_abs = abs(self._effective_gravity())
        disc = v0 * v0 - 2.0 * g_abs * fire_rel
        if disc < 0:
            return False
        t_fire = (v0 - math.sqrt(disc)) / g_abs
        if t_fire <= 0.0:
            return False
        az_target = math.atan2(ep[1] - self.pos[1], ep[0] - self.pos[0])
        return abs(_angle_diff(az_target, self.azimuth)) <= self._tank_turn_rate * t_fire

    def _check_z_attack_jump(self, now: float) -> bool:
        """ZJ1: Springt auf die Höhe eines erhöhten Gegners wenn normaler Schuss
        die Z-Achse nicht überbrücken kann, aber der Sprung die Höhe erreicht."""
        if not self._can_jump(now):
            return False
        if self.target_player is None:
            return False
        ep = self._get_enemy_pos(self.target_player)
        if ep is None:
            return False
        info = self.players.get(self.target_player)
        if info is None or not info.alive:
            return False
        if info.is_airborne:
            return False

        enemy_z = info.pos[2]
        z_diff = enemy_z - self.pos[2]
        max_jump_h = self._effective_jump_height()

        if z_diff <= HIT_RADIUS:
            return False
        if z_diff >= max_jump_h:
            return False
        # NAV-Vorgabe: ein verfügbarer Teleporter-Schuss ist sicherer als der Z-Sprung →
        # am Boden bleiben, _maybe_shoot_* feuert den Teleporter-Schuss.
        if self._teleporter_shot_available(self.target_player):
            return False
        if now < self._z_attack_retry_after:
            return False
        if random.random() >= 0.5:
            self._z_attack_retry_after = now + 3.0
            return False

        # Feuerzeitpunkt berechnen: wann während des Aufstiegs ist Bot auf Feuer-Höhe?
        # (Herleitung → DEVELOPER.md §8 "Z-Angriffs-Sprung — Feuerhöhe relativ vs. absolut")
        # fire_rel = Feuer-Höhe RELATIV zum Absprungpunkt (Kinematik); +1u = Tankmittelpunkt-
        # Korrektur, cap hält disc >= 0. z_diff statt enemy_z, damit der Absprung von erhöhten
        # Plattformen korrekt ist (sonst wird fire_rel auf max_jump_h gedeckelt → falscher t_fire).
        fire_rel = min(z_diff + 1.0, max_jump_h - 0.5)
        v0 = self._effective_jump_velocity()
        g_abs = abs(self._effective_gravity())
        # disc < 0 wenn fire_rel höher als Sprung-Maximum → kein Schuss möglich
        disc = v0 * v0 - 2.0 * g_abs * fire_rel
        if disc < 0:
            return False
        # Kleinere Lösung = Aufstiegs-Zeitpunkt (größere = Abstieg)
        t_fire = (v0 - math.sqrt(disc)) / g_abs
        if t_fire <= 0.0:
            return False
        while len(self._slot_reload_at) < self._max_shots:
            self._slot_reload_at.append(0.0)
        _ns = (self._shot_slot + 1) % self._max_shots
        _earliest = max(self._next_shoot, self._slot_reload_at[_ns])
        if _earliest > now + t_fire - 0.2:  # 0.2s Puffer gegen Tick-Quantisierung
            return False

        # Gegnerposition zum Feuer-Zeitpunkt vorhersagen
        pred_ex = ep[0] + info.vel[0] * t_fire
        pred_ey = ep[1] + info.vel[1] * t_fire
        az_target = math.atan2(pred_ey - self.pos[1], pred_ex - self.pos[0])
        az_target += random.uniform(-math.radians(2.5), math.radians(2.5))
        ang_diff = _angle_diff(az_target, self.azimuth)
        if abs(ang_diff) > self._tank_turn_rate * t_fire:
            return False  # Bot kann in t_fire nicht ausreichend drehen
        self._jump_ang_vel = math.copysign(
            min(abs(ang_diff / max(t_fire, 0.001)), self._tank_turn_rate), ang_diff)

        # Sprung starten
        self.vel[2] = self._jump_launch_vz(self.vel[2])
        self._jumping = True
        self._z_attack_mode = True
        # ABSOLUTE Feuer-Höhe (Tick vergleicht gegen pos[2]); self.pos[2] ist die Absprunghöhe
        self._z_attack_fire_z = self.pos[2] + fire_rel
        self._transition_to(AIState.Z_ATTACK)
        logger.info("[%s] Z-Sprung", self.callsign)
        return True

    # ── Flag-Pickup ───────────────────────────────────────────────────────

    def _check_opportunistic_grab(self, now: float) -> None:
        """Sendet MsgGrabFlag wenn Bot nah an einer onGround-Flag ist."""
        if self.own_flag or self.player_id is None: return
        if now - self._last_grab_attempt < 0.5: return
        for fi in list(self.flags.values()):
            if fi.status != 1: continue
            if abs(fi.pos[2] - self.pos[2]) > 0.5: continue
            d = math.hypot(fi.pos[0] - self.pos[0], fi.pos[1] - self.pos[1])
            if d >= FLAG_GRAB_RADIUS: continue
            if d > TANK_RADIUS and not self._is_ahead(fi.pos[0], fi.pos[1]): continue
            self._last_grab_attempt = now
            self._try_grab_flag(fi.flag_id)
            return

    def _try_grab_flag(self, flag_id: int) -> None:
        """Sendet MsgGrabFlag."""
        if self.player_id is None: return
        self.client.send(MsgGrabFlag, struct.pack(">H", flag_id))
        if getattr(self, '_debug_log_flag', False):
            logger.debug("[%s] Flagge: MsgGrabFlag gesendet (flag_id=%d)", self.callsign, flag_id)

    # ── Schießen ─────────────────────────────────────────────────────────

    def _shot_quality(self, aim_diff: float, dist: float, z_diff: float) -> float:
        """Schussqualität 0.0–1.0 basierend auf Winkel, Distanz und Z-Achse.
        0.0 = Treffer geometrisch unmöglich (zu hoher Z-Unterschied)."""
        if z_diff > HIT_RADIUS:
            return 0.0
        if aim_diff <= math.radians(5):
            angle_f = 1.0
        elif aim_diff <= math.radians(15):
            angle_f = 0.8
        elif aim_diff <= math.radians(30):
            angle_f = 0.5
        else:
            angle_f = 0.1
        if dist < 40.0:
            dist_f = 1.0
        elif dist < OPTIMAL_RANGE:
            dist_f = 0.8
        elif dist < OPTIMAL_RANGE * 2:
            dist_f = 0.5
        else:
            dist_f = 0.2
        return angle_f * dist_f

    def _compute_aim_point(
        self, ep, info, dx: float, dy: float, dist: float, now: float
    ) -> tuple[float, float] | None:
        """Vorhaltepunkt berechnen; None wenn zu LANDING_SHOT gewechselt wird."""
        if info is not None:
            aim_x, aim_y = ep[0], ep[1]
            # Fix 3 (GM-Homing): Mit GM gegen einen Nicht-ST-Gegner sucht die Rakete den Gegner
            # selbst — kein LANDING_SHOT nötig. Gegen ST kann die GM nicht zielsuchen (vgl.
            # _maybe_shoot_gm), dort bleibt die Landepunkt-Vorhersage sinnvoll.
            gm_homing = self.own_flag == "GM" and info.flag != "ST"
            if info.is_airborne and info.pos[2] > 0.1 and not gm_homing:
                g = self._gravity
                z0, vz = info.pos[2], info.vel[2]
                # Berechnet wie lange bis der Gegner auf dem Boden landet
                # (Details → DEVELOPER.md §6 "Landepunkt-Vorhersage")
                disc = vz * vz - 2.0 * g * z0
                if disc >= 0:
                    # Lösung der Höhengleichung: Moment wenn z = 0 (Boden)
                    t_land = (-vz - math.sqrt(disc)) / g
                    if t_land > 0:
                        aim_x = ep[0] + info.vel[0] * t_land
                        aim_y = ep[1] + info.vel[1] * t_land
                        # P4-TAC-06/07 (Z-Bewusstsein): Der Flachschuss läuft auf Mündungshöhe.
                        # Landet der Gegner deutlich höher, ist er unerreichbar (kein LANDING_SHOT);
                        # landet er tiefer, wird der Schuss auf den Moment getimt, in dem der Gegner
                        # durch die Mündungshöhe fällt (Interzeption beim Fallen, t_aim < t_land).
                        nav = getattr(self, "_nav_graph", None)
                        landing_z = (0.0 if nav is None
                                     else nav.get_floor_z(aim_x, aim_y, info.pos[2]))
                        z_ref = self.pos[2] + self._muzzle_height
                        tol = HIT_RADIUS
                        t_aim = t_land
                        self._landing_hit_z = 0.0
                        z_reachable = True
                        if landing_z - self.pos[2] > tol:
                            z_reachable = False           # P4-TAC-06: landet höher → unerreichbar
                        elif self.pos[2] - landing_z > tol:
                            # P4-TAC-07: landet tiefer → Mündungshöhen-Durchgang abfangen
                            dz = info.pos[2] - z_ref
                            disc2 = vz * vz - 2.0 * g * dz
                            if disc2 >= 0:
                                t_cross = (-vz - math.sqrt(disc2)) / g
                                if t_cross > 0:
                                    t_aim = t_cross
                                    aim_x = ep[0] + info.vel[0] * t_aim
                                    aim_y = ep[1] + info.vel[1] * t_aim
                                    self._landing_hit_z = z_ref
                        landing_dist = math.hypot(
                            aim_x - self.pos[0], aim_y - self.pos[1])
                        tof_to_landing = landing_dist / max(self._effective_shot_speed(), 1.0)
                        # Fix D: Drehtzeit-Feasibility + Reichweitencheck
                        aim_az_land = math.atan2(
                            aim_y - self.pos[1], aim_x - self.pos[0])
                        turn_needed = abs(_angle_diff(aim_az_land, self.azimuth))
                        turn_time = turn_needed / max(self._tank_turn_rate, 1e-6)
                        can_aim = (z_reachable
                                   and turn_time + tof_to_landing < t_aim - 0.1
                                   and landing_dist <= OPTIMAL_RANGE * 3)
                        if not can_aim:
                            # Zu hoch/weit oder keine Zeit zum Drehen → normaler Schuss
                            rdx = dx / max(dist, 1e-6)
                            rdy = dy / max(dist, 1e-6)
                            rc = -(info.vel[0] * rdx + info.vel[1] * rdy)
                            tof = dist / max(self._effective_shot_speed() + rc, 10.0)
                            aim_x = ep[0] + info.vel[0] * tof
                            aim_y = ep[1] + info.vel[1] * tof
                        elif tof_to_landing < t_aim - 0.15:
                            # Zu früh: LANDING_SHOT-State aktivieren
                            if self._ai_state == AIState.COMBAT:
                                self._landing_aim_pos = (aim_x, aim_y)
                                self._landing_shot_until = now + t_aim + 0.2
                                self._transition_to(AIState.LANDING_SHOT)
                            return None
                        elif tof_to_landing > t_aim + 0.2:
                            # Schuss käme zu spät → normaler Vorhalteschuss
                            rdx = dx / max(dist, 1e-6)
                            rdy = dy / max(dist, 1e-6)
                            rc = -(info.vel[0] * rdx + info.vel[1] * rdy)
                            tof = dist / max(self._effective_shot_speed() + rc, 10.0)
                            aim_x = ep[0] + info.vel[0] * tof
                            aim_y = ep[1] + info.vel[1] * tof
                        # else: gutes Fenster → kein LANDING_SHOT, sofort auf Zielpunkt schießen
            else:
                if self._ai_state == AIState.LANDING_SHOT:
                    self._transition_to(AIState.COMBAT)
                # Fix A: tof mit Radialgeschwindigkeits-Korrektur
                rdx = dx / max(dist, 1e-6)
                rdy = dy / max(dist, 1e-6)
                radial_closing = -(info.vel[0] * rdx + info.vel[1] * rdy)
                tof = dist / max(self._effective_shot_speed() + radial_closing, 10.0)
                aim_x = ep[0] + info.vel[0] * tof
                aim_y = ep[1] + info.vel[1] * tof
            return aim_x, aim_y
        return ep[0], ep[1]

    # ── Offensiver Ricochet-Aim ────────────────────────────────────────────

    def _find_ricochet_aim_angle(
        self,
        target_pid: int,
        predicted_pos: Optional[Tuple[float, float]] = None,
    ) -> Optional[float]:
        now = time.monotonic()
        cache = self._rico_aim_cache
        if (cache is not None
                and cache[1] == target_pid
                and now - cache[0] < RICO_AIM_CACHE_TTL):
            return cache[2][0] if cache[2] is not None else None
        result = self._compute_ricochet_aim(target_pid, predicted_pos)
        self._rico_aim_cache = (now, target_pid, result)
        if result is not None:
            az, via_tele = result
            if via_tele:
                logger.debug("[%s] Tele: Schuss anvisiert az=%.1f°",
                             self.callsign, math.degrees(az))
            else:
                logger.debug("[%s] Rico: Abprallwinkel neu berechnet az=%.1f°",
                             self.callsign, math.degrees(az))
        return result[0] if result is not None else None

    def _teleporter_shot_available(self, target_pid: int) -> bool:
        """True, wenn die (gecachte) Indirekt-Zielsuche einen *Teleporter*-Schuss liefert.
        Nutzt den TTL-Cache von `_find_ricochet_aim_angle` (kein zusätzlicher Sweep)."""
        if target_pid is None or not self._has_teleporters():
            return False
        self._find_ricochet_aim_angle(target_pid, None)   # füllt/erneuert Cache (TTL)
        c = self._rico_aim_cache
        return bool(c and c[1] == target_pid and c[2] is not None and c[2][1])  # via_tele

    def _indirect_shot_available(self, target_pid: int) -> bool:
        """True, wenn die gecachte Zielsuche IRGENDEINEN Indirekt-Schuss (Abpraller oder Tor) liefert.
        Nutzt denselben 2s-Cache wie `_find_ricochet_aim_angle` (kein zusätzlicher Sweep)."""
        if target_pid is None:
            return False
        _fb = (self.own_flag.encode('ascii') + b'\x00\x00')[:2]
        if not (self._has_teleporters()
                or can_ricochet(_fb, self.own_flag == "GM", self.own_flag == "SW",
                                self._server_ricochet)):
            return False
        self._find_ricochet_aim_angle(target_pid, None)   # füllt/erneuert Cache (TTL)
        c = self._rico_aim_cache
        return bool(c and c[1] == target_pid and c[2] is not None)

    def _update_indirect_hold(self, now: float, in_hold_case: bool) -> bool:
        """Zeit-Cap fürs Indirekt-Schuss-Halten: armt beim Eintritt, läuft nach INDIRECT_HOLD_S ab,
        resettet beim Verlassen (kein sofortiges Re-Arm im selben Fall). Gibt zurück, ob aktuell
        gehalten werden soll."""
        if not in_hold_case:
            self._indirect_hold_until = None              # Fall verlassen → Cap zurücksetzen
            return False
        if self._indirect_hold_until is None:
            self._indirect_hold_until = now + INDIRECT_HOLD_S   # gerade eingetreten → armen
        return now < self._indirect_hold_until

    def _compute_ricochet_aim(
        self,
        target_pid: int,
        predicted_pos: Optional[Tuple[float, float]],
    ) -> Optional[Tuple[float, bool]]:
        enemy = self.players.get(target_pid)
        if not enemy or not enemy.alive:
            return None
        wmap = getattr(self, "_world_map", None)
        if wmap is None:
            return None

        bx, by, bz = self.pos[0], self.pos[1], self.pos[2]
        ecx = predicted_pos[0] if predicted_pos else enemy.pos[0]
        ecy = predicted_pos[1] if predicted_pos else enemy.pos[1]
        ecz = enemy.pos[2] + TANK_HEIGHT * 0.5

        hw   = self._tank_width  / 2 + self._shot_radius
        hlen = self._tank_length / 2 + self._shot_radius
        hh   = self._tank_height / 2 + self._shot_radius

        flag_bytes = (self.own_flag.encode('ascii') + b'\x00\x00')[:2]
        direct_az  = math.atan2(enemy.pos[1] - by, enemy.pos[0] - bx)

        eff_speed      = self._effective_shot_speed()
        sweep_lifetime = self._effective_shot_lifetime()
        max_range      = self._effective_shot_range()

        # Kandidaten-Azimute (absolute Grad): um die Gegner-Richtung (Abpraller / direkte Tor-Schüsse)
        # plus um die Richtung zu jedem erreichbaren, gegnerseitig verlinkten Tor-Eintritt.
        direct_az_deg = math.degrees(direct_az)
        cand_deg: set = set()
        for az_deg in range(-(RICO_AIM_MAX), RICO_AIM_MAX):
            if abs(az_deg) > 5:
                cand_deg.add(direct_az_deg + az_deg)

        bot_z    = bz + self._muzzle_height                        # Flachschuss-Höhe am Eintritt
        ez0, ez1 = enemy.pos[2], enemy.pos[2] + self._tank_height  # Gegner-Vertikalspanne
        teles    = wmap.teleporters or []
        lmap     = getattr(self, "_link_map", None) or {}
        for ti, t in enumerate(teles):
            if not (t.bottom_z <= bot_z <= t.bottom_z + t.height - t.border):
                continue                                           # (1) Eingang nicht auf Bot-Höhe
            for face in (0, 1):
                dst = lmap.get(ti * 2 + face)
                if dst is None:
                    continue
                ex = teles[dst // 2]                               # verlinktes Austritts-Tor
                if ex.bottom_z > ez1 or ex.bottom_z + ex.height - ex.border < ez0:
                    continue                                       # (2) Ausgang nicht auf Gegner-Höhe
                d_in = math.hypot(t.cx - bx, t.cy - by)
                if d_in + math.hypot(ecx - ex.cx, ecy - ex.cy) > max_range:
                    continue                                       # (3) Reichweite zu kurz
                ent_deg  = math.degrees(math.atan2(t.cy - by, t.cx - bx))
                half_deg = math.degrees(math.atan2(max(t.half_d - t.border, 0.1),
                                                   max(d_in, 0.1))) + 5.0
                # distanz-adaptives Fenster — nah breit, fern schmal (Tor schrumpft optisch)
                for d in range(-int(half_deg), int(half_deg) + 1):
                    cand_deg.add(ent_deg + d)

        best_az: Optional[float] = None
        best_via_tele: bool = False
        best_t: float = float("inf")
        _tl: list = []   # je Winkel wiederverwendet: Teleporter-Querungen des Pfades

        for deg in sorted(cand_deg):
            az = math.radians(deg)
            # F2: Sim-Start an der realen Mündung (deckt sich mit _send_shot)
            sx = bx + math.cos(az) * self._muzzle_front
            sy = by + math.sin(az) * self._muzzle_front
            sz = bz + self._muzzle_height
            vx = math.cos(az) * eff_speed
            vy = math.sin(az) * eff_speed
            _tl.clear()
            segs = simulate_shot_path(
                pos=(sx, sy, sz),
                vel=(vx, vy, 0.0),
                fire_time=0.0,
                lifetime=sweep_lifetime,
                flag_abbr=flag_bytes,
                obstacles=wmap.boxes,
                world_half=self.world_half,
                server_ricochet=self._server_ricochet,
                max_bounces=3,
                wall_height=self._wall_height,
                teleporters=wmap.teleporters,
                link_map=getattr(self, "_link_map", None),
                tele_log=_tl,
                solid_obs=wmap.solid_obstacles(),
                obs_grid=getattr(self, "_shot_grid", None),
            )
            if len(segs) <= 1:
                continue
            _hh = hh + (TELE_AIM_Z_TOL if _tl else 0.0)   # A: nur Tor-Schüsse bekommen Z-Spielraum
            for seg in segs[1:]:
                if _segment_hits_obb_3d(
                    seg.px, seg.py, seg.pz,
                    seg.ex, seg.ey, seg.ez,
                    ecx, ecy, ecz, 0.0,
                    hlen, hw, _hh,
                ):
                    if seg.t_start < best_t:
                        best_t = seg.t_start
                        best_az = az
                        best_via_tele = bool(_tl)
                    break

        return None if best_az is None else (best_az, best_via_tele)

    def _maybe_shoot_tr(self, now: float) -> None:
        if not self._next_slot_ready(now): return
        self._send_shot(now, self.azimuth)
        # _send_shot setzt _slot_reload_at via _effective_reload_time() = reload/_rfire_ad_rate

    def _maybe_shoot_sw(self, now: float, ep, info, dx: float, dy: float) -> None:
        dz_abs = abs(info.pos[2] - self.pos[2]) if info is not None else 0.0
        dist_3d = math.hypot(math.hypot(dx, dy), dz_abs)
        if self._shock_in_radius < dist_3d <= self._shock_out_radius:
            self._send_shot(now, self.azimuth)
            self._set_next_shoot_after_fire(now)

    def _maybe_shoot_gm(
        self, now: float, ep, info, dx: float, dy: float, dist: float
    ) -> None:
        aim_xy = self._compute_aim_point(ep, info, dx, dy, dist, now)
        if aim_xy is None:
            return
        aim_angle = math.atan2(aim_xy[1] - self.pos[1], aim_xy[0] - self.pos[0])
        st_target = info is not None and info.flag == "ST"
        if st_target:
            if not self._has_los_to_enemy(self.target_player):
                return
            aim_threshold = math.radians(10)
        else:
            aim_threshold = math.radians(4) if dist < self._gm_min_range else math.radians(20)
        if abs(_angle_diff(aim_angle, self.azimuth)) > aim_threshold:
            return
        # GM-Aktivierungspunkt-Gate (nur Normalziele): die Rakete fliegt _gm_min_range geradeaus,
        # bevor das Zielsuchen aktiv wird. Zwei Wand-LoS-Checks — Bot→Aktivierungspunkt und
        # Aktivierungspunkt→Gegner; schlägt einer fehl, würde die Rakete sehr wahrscheinlich in eine
        # Wand krachen → keinen Schuss verschwenden. (Teleporter zählen NICHT als Wand → Tor-/Kurven-
        # schüsse bleiben erlaubt. Gegen ST kann die GM nicht zielsuchen → dort gilt der direkte
        # LoS-Zwang oben, nicht dieses Gate.)
        if not st_target:
            az = self.azimuth
            mx = self.pos[0] + math.cos(az) * self._muzzle_front
            my = self.pos[1] + math.sin(az) * self._muzzle_front
            mz = self.pos[2] + self._muzzle_height
            ax = mx + math.cos(az) * self._gm_min_range
            ay = my + math.sin(az) * self._gm_min_range
            ez = (info.pos[2] if info is not None else self.pos[2]) + TANK_HEIGHT * 0.5
            if not (self._segment_clear(mx, my, mz, ax, ay, mz)
                    and self._segment_clear(ax, ay, mz, ep[0], ep[1], ez)):
                return
        self._send_shot(now, self.azimuth)
        if st_target:
            self._gm_need_update = False
        self._set_next_shoot_after_fire(now)

    def _maybe_shoot_l(
        self, now: float, ep, info, dx: float, dy: float, dist: float
    ) -> None:
        # Laser ist instant — kein _compute_aim_point(), direkt auf aktuelle Position
        aim_angle = math.atan2(ep[1] - self.pos[1], ep[0] - self.pos[0])
        _indirect = False
        if not self._has_los_to_enemy(self.target_player) or self._cross_floor_indirect(info):
            _fb = (self.own_flag.encode('ascii') + b'\x00\x00')[:2]
            if can_ricochet(_fb, False, False, self._server_ricochet) or self._has_teleporters():
                _rico_az = self._find_ricochet_aim_angle(
                    self.target_player, (ep[0], ep[1])
                )
                if _rico_az is not None:
                    aim_angle = _rico_az
                    _indirect = True
                    if getattr(self, '_debug_log_shot', False):
                        logger.debug("[%s] Schuss: Indirekt-Laser (Ricochet/Teleporter) az=%.1f°",
                                     self.callsign, math.degrees(aim_angle))
                else:
                    return
            else:
                return
        if abs(_angle_diff(aim_angle, self.azimuth)) > math.radians(5):
            return
        if (not _indirect and info is not None
                and abs(info.pos[2] - self.pos[2]) > TANK_HEIGHT * 0.7):
            return
        self._send_shot(now, self.azimuth)
        self._set_next_shoot_after_fire(now)

    def _maybe_shoot_th(
        self, now: float, ep, info, dx: float, dy: float, dist: float
    ) -> None:
        if dist > THIEF_AD_RANGE:
            return
        aim_angle = math.atan2(ep[1] - self.pos[1], ep[0] - self.pos[0])
        _indirect = False
        if not self._has_los_to_enemy(self.target_player) or self._cross_floor_indirect(info):
            _fb = (self.own_flag.encode('ascii') + b'\x00\x00')[:2]
            if can_ricochet(_fb, False, False, self._server_ricochet) or self._has_teleporters():
                _rico_az = self._find_ricochet_aim_angle(self.target_player, None)
                if _rico_az is not None:
                    aim_angle = _rico_az
                    _indirect = True
                    if getattr(self, '_debug_log_shot', False):
                        logger.debug("[%s] Schuss: Indirekt-TH (Ricochet/Teleporter) az=%.1f°",
                                     self.callsign, math.degrees(aim_angle))
                else:
                    return
            else:
                return
        if abs(_angle_diff(aim_angle, self.azimuth)) > math.radians(10):
            return
        if (not _indirect and info is not None
                and abs(info.pos[2] - self.pos[2]) > TANK_HEIGHT * 0.7):
            return
        self._send_shot(now, self.azimuth)
        self._set_next_shoot_after_fire(now)

    def _maybe_shoot_sb(
        self, now: float, ep, info, dx: float, dy: float, dist: float
    ) -> None:
        """Super Bullet: schießt durch Wände — kein LoS-Check."""
        aim_xy = self._compute_aim_point(ep, info, dx, dy, dist, now)
        if aim_xy is None:
            return
        aim_angle = math.atan2(aim_xy[1] - self.pos[1], aim_xy[0] - self.pos[0])
        if abs(_angle_diff(aim_angle, self.azimuth)) > math.radians(25):
            return
        _warning = False
        if self._ai_state != AIState.LANDING_SHOT and info is not None:
            z_diff = abs(info.pos[2] - self.pos[2])
            if z_diff > HIT_RADIUS:
                _max_jump_h = self._effective_jump_height()
                if z_diff >= _max_jump_h:
                    return
                if random.random() >= 0.3:
                    return
                if self._max_shots > 1:
                    while len(self._slot_reload_at) < self._max_shots: self._slot_reload_at.append(0.0)
                    if self._slot_reload_at[self._shot_slot] > now:
                        return
                _warning = True
        self._send_shot(now, self.azimuth)
        if _warning:
            self._next_shoot = now + self._effective_reload_time()
        else:
            self._set_next_shoot_after_fire(now)

    def _maybe_shoot_standard(
        self, now: float, ep, info, dx: float, dy: float, dist: float
    ) -> None:
        aim_xy = self._compute_aim_point(ep, info, dx, dy, dist, now)
        if aim_xy is None:
            return
        aim_angle = math.atan2(aim_xy[1] - self.pos[1], aim_xy[0] - self.pos[0])
        if self.own_flag == "WA":
            aim_angle += random.gauss(0, math.radians(4))
        _warning = False
        _indirect = False
        _no_los = not self._has_los_to_enemy(self.target_player)
        if _no_los or self._cross_floor_indirect(info):
            _fb = (self.own_flag.encode('ascii') + b'\x00\x00')[:2]
            _can_rico = can_ricochet(_fb, self.own_flag == "GM",
                                      self.own_flag == "SW", self._server_ricochet)
            # Teleporter routen auch normale Schüsse → Aim-Sweep auch ohne Ricochet versuchen.
            _rico_az = None
            if _can_rico or self._has_teleporters():
                _rico_az = self._find_ricochet_aim_angle(
                    self.target_player, (aim_xy[0], aim_xy[1])
                )
            if _rico_az is not None:
                aim_angle = _rico_az
                _indirect = True
                if getattr(self, '_debug_log_shot', False):
                    logger.debug("[%s] Schuss: Indirekt-Standard (Ricochet/Teleporter) az=%.1f°",
                                 self.callsign, math.degrees(aim_angle))
            elif _no_los:
                # Blinder Warnschuss nur ohne Sicht — nicht im reinen Cross-Floor-Fall.
                if random.random() > 0.15:
                    return
                if self._max_shots > 1:
                    while len(self._slot_reload_at) < self._max_shots: self._slot_reload_at.append(0.0)
                    if self._slot_reload_at[self._shot_slot] > now:
                        return
                _warning = True
            else:
                return   # Cross-Floor, aber kein indirekter Schuss gefunden → Feuer halten
        if abs(_angle_diff(aim_angle, self.azimuth)) > math.radians(25):
            return
        # SS1: Z-Achsen-Block — GM und LANDING_SHOT ausgenommen; indirekte (Teleporter-/Abpraller-)
        # Schüsse überbrücken die Etage (Simulation hat den Treffer bestätigt) → ausgenommen.
        if (self._ai_state != AIState.LANDING_SHOT
                and self.own_flag not in ("GM", "SW")
                and info is not None
                and not _indirect):
            z_diff = abs(info.pos[2] - self.pos[2])
            if z_diff > HIT_RADIUS:
                _max_jump_h = self._effective_jump_height()
                if z_diff >= _max_jump_h:
                    return  # zu hoch zum Springen: kein Schuss
                if random.random() >= 0.3:
                    return  # ZJ1-Zone: 30% Warnschuss
                if self._max_shots > 1:
                    while len(self._slot_reload_at) < self._max_shots: self._slot_reload_at.append(0.0)
                    if self._slot_reload_at[self._shot_slot] > now:
                        return  # letzter freier Slot → für gezielten Schuss aufsparen
                _warning = True
        self._send_shot(now, self.azimuth)
        if _warning:
            self._next_shoot = now + self._effective_reload_time()
        else:
            self._set_next_shoot_after_fire(now)

    def _maybe_shoot(self, now: float) -> None:
        """Schießt wenn Ziel ausgerichtet und Reload abgelaufen (TR: Dauerfeuer)."""
        if self.own_flag == "TR" and self._can_shoot():
            if self._muzzle_clear(self.azimuth):   # Mündung nicht hinter einer Wand → sonst gefressen
                self._maybe_shoot_tr(now)
            return
        if not self._can_shoot(): return
        if self.own_flag == "OO" and self._is_inside_obstacle(include_oo=True): return
        if now < self._next_shoot: return
        if not self._next_slot_ready(now): return
        if self._ai_state in (AIState.Z_ATTACK, AIState.LANDING_SHOT):
            return

        if self.target_player is not None and self._has_presence():
            ep = self._get_enemy_pos(self.target_player)
            if ep:
                info = self.players.get(self.target_player)
                if info is not None and info.paused:
                    return  # pausiertes Ziel ist unverwundbar → Schüsse sparen (Slots bereithalten)
                pz_active = bool(info and info.is_phantom_zoned
                                 and self.own_flag not in ("SW", "SB"))
                if not pz_active:
                    # Mündungs-Occlusion-Gate: der reale Schuss spawnt an der Mündung (4.42u
                    # voraus). Steckt die zwischen Tank-Mitte und sich selbst hinter einer dünnen
                    # Wand, würde bzfs ihn fressen bzw. er ginge unfair durch die Wand → nicht
                    # feuern. SW (radial) und SB (durchschlägt Wände) sind ausgenommen.
                    if self.own_flag not in ("SW", "SB") and not self._muzzle_clear(self.azimuth):
                        return
                    dx, dy = ep[0] - self.pos[0], ep[1] - self.pos[1]
                    dist = math.hypot(dx, dy)
                    if   self.own_flag == "SW": self._maybe_shoot_sw(now, ep, info, dx, dy)
                    elif self.own_flag == "GM": self._maybe_shoot_gm(now, ep, info, dx, dy, dist)
                    elif self.own_flag == "SB": self._maybe_shoot_sb(now, ep, info, dx, dy, dist)
                    elif self.own_flag == "L":  self._maybe_shoot_l(now, ep, info, dx, dy, dist)
                    elif self.own_flag == "TH": self._maybe_shoot_th(now, ep, info, dx, dy, dist)
                    else:                       self._maybe_shoot_standard(now, ep, info, dx, dy, dist)
                    return  # nur nach gezieltem Schuss zurück
                # pz_active: fall-through zu Random-Schüssen (Verwirrung)
            else:
                return  # kein ep bekannt

        if self.own_flag in self.good_flags:
            return
        if self.own_flag in self._limited_flags:
            return
        if not self._has_presence():
            self._next_shoot = now + random.uniform(self._effective_reload_time(), SHOOT_INTERVAL_RANDOM_MAX)
            return
        self._send_shot(now, self.azimuth)
        # Kein Burst für Random-Schüsse — zufälliges Intervall [reload_time, 10s]
        self._next_shoot = now + random.uniform(self._effective_reload_time(), SHOOT_INTERVAL_RANDOM_MAX)

    def _send_shot(self, now: float, az: float) -> None:
        """Sendet MsgShotBegin (43-Byte FiringInfo) via UDP."""
        if self.player_id is None: return
        # Shot-ID: low byte = Slot (0..maxShots-1), high byte = Generation
        # Jeder neue Schuss inkrementiert Slot; bei Überlauf neue Generation
        self._shot_slot = (self._shot_slot + 1) % self._max_shots
        if self._shot_slot == 0:
            self._shot_gen = (self._shot_gen + 1) & 0xFF
        shot_id = (self._shot_gen << 8) | self._shot_slot

        vx = math.cos(az) * self._shot_speed
        vy = math.sin(az) * self._shot_speed
        if self.own_flag == "SW":
            vx = vy = 0.0  # bzfs.cxx:4148 setzt shotSpeed=0; Non-Zero wird abgelehnt
        team_id = self.team if self.team not in (0xFFFF, 0xFFFE) else 0
        muzzle_x = self.pos[0] + math.cos(az) * self._muzzle_front
        muzzle_y = self.pos[1] + math.sin(az) * self._muzzle_front
        muzzle_z = self.pos[2] + self._muzzle_height
        if getattr(self, '_debug_log_shot', False):
            logger.debug("[%s] Schuss: Abgefeuert – muzzle=(%.3f,%.3f,%.3f) vel=(%.2f,%.2f) flag=%s",
                         self.callsign, muzzle_x, muzzle_y, muzzle_z, vx, vy, self.own_flag or "–")

        payload = (
            struct.pack(">f",  now)
            + struct.pack(">B", self.player_id)
            + struct.pack(">H", shot_id)
            + struct.pack(">fff", muzzle_x, muzzle_y, muzzle_z)
            + struct.pack(">fff", vx, vy, 0.0)
            + struct.pack(">f",  0.0)
            + struct.pack(">h",  team_id)
            + (self.own_flag.encode('ascii') + b'\x00\x00')[:2]
            + struct.pack(">f",  self._shot_lifetime)
        )
        assert len(payload) == 43
        self.client.send(MsgShotBegin, payload)
        # Slot-Reload-Tracking: diesen Slot als belegt markieren
        while len(self._slot_reload_at) < self._max_shots:
            self._slot_reload_at.append(0.0)
        self._slot_reload_at[self._shot_slot] = now + self._effective_reload_time()
        if self.own_flag == "GM":
            self._active_gm = {
                "shot_id":        shot_id,
                "fire_time":      now,
                "pos":            [muzzle_x, muzzle_y, muzzle_z],
                "vel":            [vx, vy, 0.0],
                "team":           team_id,
                "initial_target": self.target_player,
            }
            self._gm_need_update = True
            self._gm_send_at     = now + max(self._gm_activation_time - 0.005, 0.005)
            self._gm_resend_at   = None
