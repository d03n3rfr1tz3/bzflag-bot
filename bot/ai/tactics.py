"""Taktische Manoever: Sprung-Ausfuehrung, taktischer Uebersprung, Z-Hoehenangriff, Rueckwaertsfahrt-Entscheid (W4, FABLE-PLAN Teil 3)."""

import math
import random
import logging

from bot.constants import (
    NAV_CELL_SIZE,
    NAV_JUMP_Z_TOL,
    OPTIMAL_RANGE,
    TACT_JUMP_CLEARANCE,
    TACT_JUMP_REACTION_S,
    HIT_RADIUS,
)
from bot.util import _angle_diff, _wrap
from bot.models import AIState

logger = logging.getLogger("bzbot")


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class TacticsMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

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

    def _execute_jump(self) -> None:
        """Führt Sprung nach Wind-Up-Ende aus (JUMP_WINDUP → JUMPING)."""
        if self._escape_jump_ang_vel is not None:
            self._jump_ang_vel = self._escape_jump_ang_vel
            self._escape_jump_ang_vel = None
            if self._debug_log_dodge:
                logger.debug("[%s] Ausweichen: Escape-Sprung ang_vel=%.2f az=%.1f°",
                             self.callsign, self._jump_ang_vel, math.degrees(self.azimuth))
        else:
            if self.target_player is not None:
                _ep2 = self._get_enemy_pos(self.target_player)
                if _ep2 is not None:
                    self.azimuth = math.atan2(
                        _ep2[1] - self.pos[1], _ep2[0] - self.pos[0])
            self._jump_ang_vel = self.ang_vel
            if self._debug_log_dodge:
                logger.debug("[%s] Ausweichen: Frontal-Sprung ang_vel=%.2f az=%.1f°",
                             self.callsign, self._jump_ang_vel, math.degrees(self.azimuth))
        self.vel[0] = math.cos(self.azimuth) * self._tank_speed
        self.vel[1] = math.sin(self.azimuth) * self._tank_speed
        self.vel[2] = self._jump_launch_vz(self.vel[2])
        self._jumping = True
        self._jump_pending = False
        self._transition_to(AIState.JUMPING)

    def _check_advance_path(self) -> bool:
        """WP-Erreichen prüfen; True wenn Aufrufer sofort return soll."""
        if self.target_pos is not None:
            tx, ty = self.target_pos
            if math.hypot(tx - self.pos[0], ty - self.pos[1]) < self._wp_reach_radius():
                nav_path = self._nav_path
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
        nav_path = self._nav_path
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
