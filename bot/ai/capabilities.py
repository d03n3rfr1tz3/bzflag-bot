"""Faehigkeiten und effektive Werte: _can_*-Gates und _effective_*-Ableitungen aus Flagge + Server-Variablen (W4, FABLE-PLAN Teil 3)."""

import math

from bot.constants import (
    TANK_WIDTH,
    TANK_RADIUS_FACTOR,
    TANK_RADIUS,
    _TINY_FACTOR,
    _NARROW_HW,
    OPTIMAL_RANGE,
    OPTIMAL_RANGE_MG,
    OPTIMAL_RANGE_SW,
    OPTIMAL_RANGE_GM,
    JUMP_COOLDOWN,
)


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class CapabilityMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

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

    def _effective_tank_radius(self) -> float:
        """Aktueller Tank-Radius aus der nachgeführten Server-Variable (_tankRadius = 0.72 * _tankLength)."""
        return TANK_RADIUS_FACTOR * self._tank_length

    def _effective_optimal_range(self) -> float:
        """Optimale Kampfdistanz je nach eigener Flagge UND Gegner-Flagge.
        Eingegrabener BU-Gegner ist immun gegen normale Schüsse (nur GM/SW treffen) und von
        jedem Tank überrollbar → ohne eigenes GM/SW auf Kontaktdistanz rammen (wie SR).
        MG und SW profitieren von Nahkampf: MG wegen kurzer Schuss-Reichweite,
        SW wegen Donut-Killzone (zu weit → treffen nur noch die äußere Grenze)."""
        tgt = self.players.get(self.target_player) if self.target_player is not None else None
        if (tgt is not None and tgt.flag == "BU" and tgt.pos[2] < 0.0
                and self.own_flag not in ("GM", "SW")):
            return self._effective_tank_radius() / 2 * self._sr_radius_mult   # eingegrabener Gegner (z<0): Ramm-Kontaktdistanz
        if self.own_flag == "MG":
            return OPTIMAL_RANGE_MG
        if self.own_flag == "SW":
            return OPTIMAL_RANGE_SW
        if self.own_flag == "SR":
            return self._effective_tank_radius() / 2 * self._sr_radius_mult  # Kontaktdistanz für Ramm-Kill
        if self.own_flag == "GM":
            return OPTIMAL_RANGE_GM
        return OPTIMAL_RANGE

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

    def _effective_reload_time(self) -> float:
        """Reload-Zeit je nach aktiver Flagge."""
        if self.own_flag == "MG":
            return self._reload_time / max(self._mgun_ad_rate, 1.0)
        if self.own_flag == "F":
            return self._reload_time / max(self._rfire_ad_rate, 1.0)
        return self._reload_time

    def _own_flag_bytes(self) -> bytes:
        """2-Byte-Wire-Encoding der eigenen Flagge (Protokoll-FlagAbbr mit
        Null-Padding; keine Flagge → b'\\x00\\x00'). F9: einzige Quelle —
        vorher 6× inline dupliziert."""
        return (self.own_flag.encode('ascii') + b'\x00\x00')[:2]

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

    def _next_slot_ready(self, now: float) -> bool:
        """True wenn der nächste Slot (Zyklus-Reihenfolge) seinen Reload abgewartet hat."""
        while len(self._slot_reload_at) < self._max_shots:
            self._slot_reload_at.append(0.0)
        return now >= self._slot_reload_at[(self._shot_slot + 1) % self._max_shots]

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
