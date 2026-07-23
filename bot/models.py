"""Daten-Klassen des Bots: Shot, PlayerInfo, FlagInfo, AIState (W2, FABLE-PLAN Teil 3)."""

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple


@dataclass
class Shot:
    """Zustand eines aktiven Schusses auf dem Spielfeld."""

    shooter_id: int
    shot_id:    int
    pos:        List[float]
    vel:        List[float]
    fire_time:  float
    lifetime:   float
    team:       int
    is_sw:      bool = False
    is_gm:      bool = False
    is_laser:   bool = False
    is_thief:   bool = False
    flag_abbr:  bytes = b"\x00\x00"
    gm_target_pid: Optional[int] = None
    last_gm_update: float = 0.0

    def is_expired(self, now: float) -> bool:
        return now - self.fire_time >= self.lifetime

    def position_at(self, t: float) -> Tuple[float, float, float]:
        dt = t - self.fire_time
        return (self.pos[0] + self.vel[0] * dt,
                self.pos[1] + self.vel[1] * dt,
                self.pos[2] + self.vel[2] * dt)

    def time_to_closest(self, px: float, py: float) -> float:
        """Zeit bis zur nächsten Annäherung an (px, py), ausgehend von self.pos."""
        rvx = self.vel[0]; rvy = self.vel[1]
        rx = self.pos[0] - px; ry = self.pos[1] - py
        denom = rvx * rvx + rvy * rvy
        # Schuss steht (quasi) still → kommt nie näher
        if denom < 1e-6:
            return float("inf")
        # Zeitpunkt des nächsten Annäherns: negativer Anteil des Schuss-Richtungsvektors
        # in Richtung des Abstands-Vektors (negativ → Schuss fährt auf Ziel zu)
        return max(0.0, -(rx * rvx + ry * rvy) / denom)

    def closest_approach_dist(self, px: float, py: float) -> float:
        """Minimaler Abstand des Schusses zu (px, py) über seine Lebenszeit."""
        t = self.time_to_closest(px, py)
        if t == float("inf"):
            return math.hypot(self.pos[0] - px, self.pos[1] - py)
        ex = self.pos[0] + self.vel[0] * t
        ey = self.pos[1] + self.vel[1] * t
        return math.hypot(ex - px, ey - py)


@dataclass
class PlayerInfo:
    """Zustand eines anderen Spielers."""
    callsign:   str
    team:       int
    is_human:   bool
    pos:        List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    vel:        List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    azimuth:    float = 0.0
    alive:      bool  = False
    flag:       str   = ""
    is_airborne: bool  = False  # aus PS_FALLING: True bei Sprung UND Fall (nicht nur Springen)
    last_seen:  float = 0.0
    last_order: int   = -1
    radar_blind_until: float = 0.0   # Radar-Aufmerksamkeit: bis dahin keine Radar-Updates (Cooldown)
    los_cache_until: float = 0.0     # P7: bis dahin gilt das gecachte LoS-Ergebnis (nur Update-Pfad)
    los_cache:       bool  = False   # P7: letztes LoS-Ergebnis zu diesem Spieler
    is_phantom_zoned: bool = False
    paused:     bool  = False  # aus MsgPause: pausiert = unverwundbar, nicht beschießen
    last_teleport: Optional[Tuple[float, int, int]] = None  # (zeit, from_face, to_face), letzter Teleport
    slot_reload_at: List[float] = field(default_factory=list)
    # P4-TAC-05: Ready-Zeitpunkt (monotonic) je Schuss-Slot dieses Gegners; Index = shot_id & 0xFF.
    # Befüllt in _on_shot_begin, Reset auf [] bei Tod/Respawn. Konvention: leere Liste bzw. ein
    # fehlender Index = Slot gilt als GELADEN (konservativ — ein nie gesehener Schuss heißt nicht,
    # dass der Slot leer ist).


@dataclass
class FlagInfo:
    """Eine Flag auf dem Spielfeld."""
    flag_id: int
    abbr:    str
    status:  int
    pos:     List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


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
    COVER_HOLD   = auto()  # P4-TAC-02: kurz an der Deckungskante halten + peeken statt offen anzugreifen
