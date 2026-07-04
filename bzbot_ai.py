"""Bewegungs- und KI-Logik für BZBot (Mixin).

Enthält AIState-Enum, BZBotAI-Mixin, Bewegungs- und Entscheidungsmethoden.
Die Spiel-Konstanten liegen in bot/constants.py und werden hier re-exportiert.

bzbot.py:  Protokoll, Netzwerk, Spielmechanik (Hit-Detection, Message-Handler)
bzbot_ai.py: KI-Strategie, State Machine, Bewegung
"""

import math
import random
import logging
import struct
import threading
import time
from typing import Optional, Tuple

from bzflag.protocol import MsgGrabFlag, MsgShotBegin
from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE, JUMP_EDGE_TOL
from bzflag.shot_physics import (simulate_shot_path, can_ricochet,
                                  ray_teleporter_crossing, teleport_through,
                                  _segment_hits_obb_3d)

logger = logging.getLogger("bzbot")

# Konstanten → bot/constants.py (Track 4/W1); Stern-Import hält alle bisherigen
# Namen (inkl. _TINY_FACTOR/_NARROW_HW via __all__) im Modul-Namespace.
from bot.constants import *  # noqa: F401,F403
# Winkel-Helfer + AIState → bot/util.py bzw. bot/models.py (Track 4/W2)
from bot.util import _angle_diff, _wrap  # noqa: F401
from bot.models import AIState
from bot.ai.tactics import TacticsMixin
from bot.ai.targeting import TargetingMixin
from bot.ai.shooting import ShootingMixin
from bot.ai.perception import PerceptionMixin
from bot.ai.physics import PhysicsMixin
from bot.ai.capabilities import CapabilityMixin


# ── State Machine ─────────────────────────────────────────────────────────

class BZBotAI(CapabilityMixin, PhysicsMixin, PerceptionMixin, ShootingMixin, TargetingMixin, TacticsMixin):
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

    # ── Physik-Block (60 Hz, immer) ───────────────────────────────────────

    # ── Landungs-Prüfung ──────────────────────────────────────────────────

    # ── Capability-Checks ─────────────────────────────────────────────────

    # ── Haupt-Dispatch (60 Hz) ────────────────────────────────────────────

    def _update_movement(self, dt: float, now: float, ai_tick: bool = True) -> None:
        """Bewegungs-Wrapper (60 Hz): State-Dispatch + zentraler Teleporter-Querungs-Check.

        Der Crossing-Check läuft pathing-unabhängig in JEDEM Tick und für JEDEN State (wie die
        Hitbox-Detection) — auch wenn _dispatch_movement früh `return`t. So wird ein Teleporter
        auch per Direktpfad, Bounce oder TactJump-Sprung-Arc korrekt durchquert (P3-NAV-02)."""
        old = (self.pos[0], self.pos[1], self.pos[2])
        self._dispatch_movement(dt, now, ai_tick)
        self._check_teleport_crossing(old, now)

    def _turn_toward(self, target_az: float, dt: float) -> float:
        """Dreht azimuth mit _tank_turn_rate Richtung target_az und setzt ang_vel
        konsistent (geklemmt, damit das gesendete PlayerUpdate zur realen Drehung
        passt). Gibt den Winkelabstand VOR der Drehung zurück (Aufrufer nutzen
        ihn für Speed-/Erreicht-Entscheidungen). F9: einzige Quelle für das
        vorher 5× duplizierte Dreh-Snippet; Varianten mit effektiver Drehrate
        (_eff_turn) oder Winkel-Cap (_compute_dodge_dir) bleiben bewusst eigen."""
        diff = _angle_diff(target_az, self.azimuth)
        self.ang_vel = math.copysign(
            min(abs(diff / max(dt, 1e-6)), self._tank_turn_rate), diff)
        self.azimuth = _wrap(
            self.azimuth + math.copysign(min(abs(diff), self._tank_turn_rate * dt), diff))
        return diff

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
                    self._turn_toward(
                        math.atan2(ay - self.pos[1], ax - self.pos[0]), dt)
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
        diff = self._turn_toward(az_to_wp, dt)
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
        diff = self._turn_toward(target_az, dt)
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
                self._turn_toward(self._dodge_dir, dt)
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
        # F5: Server-Basiswert (_shotRange) statt Konstante; ohne Flaggen-Multiplikatoren,
        # sonst würde z.B. Laser den Direktmodus kartenweit aktivieren (nie mehr A*-Nav).
        _dist_thresh = self._shot_range if _los_clear else _opt * 1.1
        _check1      = self.pos[2] + self._tank_height > _enemy_z
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
        replan_z   = abs(enemy_z - nav_goal_z) > self._tank_height * 2
        # Während einer Stuck-Episode verwaltet _combat_escalate das Planen allein (sonst würde
        # replan_xy den Reposition-Pfad überschreiben). Im Direktmodus wird kein Pfad gefahren →
        # gar nicht erst planen (spart A* und vermeidet ungenutzte Wegpunkt-Logs).
        if (replan_xy or replan_z) and not _stuck_active and not _skip_nav:
            if enemy_z > self._tank_height:
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
        self._turn_toward(target_az, dt)
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

    # ── Z-Höhen-Sprung (ZJ1) ─────────────────────────────────────────────

    # ── Flag-Pickup ───────────────────────────────────────────────────────

    # ── Schießen ─────────────────────────────────────────────────────────

    # ── Offensiver Ricochet-Aim ────────────────────────────────────────────

        # _send_shot setzt _slot_reload_at via _effective_reload_time() = reload/_rfire_ad_rate

