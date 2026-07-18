"""State-Machine: Zustandsuebergaenge, 60-Hz-Dispatch und alle _tick_*-Zustaende ausser COMBAT (W4, FABLE-PLAN Teil 3)."""

import math
import random
import time
import logging

from bot.constants import (
    LANDING_DOUBLE_SHOT_DELAY,
    NAV_TELE_TIMEOUT,
    NAV_TELE_COOLDOWN,
    NAV_TELE_OVERSHOOT,
    STUCK_WINDOW,
    STUCK_MIN_DIST,
    EVADE_CLEAR_GRACE,
    DODGE_REACT_DELAY,
    IB_REACT_MULTIPLIER,
    M_REACT_MULTIPLIER,
    CS_REACT_MULTIPLIER,
    COVER_PEEK_BACK_S,
    TANK_LENGTH,
)
from bot.util import _angle_diff, _wrap
from bot.models import AIState

logger = logging.getLogger("bzbot")


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class StateMachineMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

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

    def _update_movement(self, dt: float, now: float, ai_tick: bool = True) -> None:
        """Bewegungs-Wrapper (60 Hz): State-Dispatch + zentraler Teleporter-Querungs-Check.

        Der Crossing-Check läuft pathing-unabhängig in JEDEM Tick und für JEDEN State (wie die
        Hitbox-Detection) — auch wenn _dispatch_movement früh `return`t. So wird ein Teleporter
        auch per Direktpfad, Bounce oder TactJump-Sprung-Arc korrekt durchquert (P3-NAV-02)."""
        old = (self.pos_x, self.pos_y, self.pos_z)
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

    def _turn_toward_ramped(self, target_az: float, dt: float) -> float:
        """Wie _turn_toward, aber mit angularer Beschleunigungsklemme (P4-MOV-02a, via
        _ramp_azimuth_step). Gleicher Vertrag: gibt den Winkelabstand VOR der Drehung zurück.
        Ohne aktives -a-Limit identisch zu _turn_toward. Für 02a-Bodenfahren (Combat); die
        committed States nutzen weiter _turn_toward, bis sie in 02b einzeln umgestellt werden."""
        diff = _angle_diff(target_az, self.azimuth)
        self._ramp_azimuth_step(diff, dt, self._tank_turn_rate)
        return diff

    def _dispatch_movement(self, dt: float, now: float, ai_tick: bool = True) -> None:
        """Physik (60 Hz) + KI (10 Hz): State-Machine-Dispatch."""
        half = self.world_half

        # Grundphysik läuft immer
        self._run_physics(dt, now)

        # FALLING-Erkennung: Bodenstates merken nicht dass sie vom Dach gefallen sind.
        # Nur beim Abwärts-Fallen (vel[2] < -0.1) und tatsächlich in der Luft.
        _GROUND_STATES = (AIState.COMBAT, AIState.SEEKING, AIState.IDLE,
                          AIState.EVADING, AIState.LANDING_SHOT, AIState.COVER_HOLD)
        if (self._ai_state in _GROUND_STATES
                and not self._jumping
                and self.vel_z < -0.1
                and self.pos_z > self._get_floor_z() + 0.5):
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
                # Position halten; aktiv auf Landepunkt drehen. P4-MOV-02b: Ramp gegen 0 (sanftes
                # Abbremsen aus der Landegeschwindigkeit) statt Hart-Stopp; ohne -a/M instant.
                speed = self._ramp_linear_speed(0.0, dt)
                self.vel_x = math.cos(self.azimuth) * speed
                self.vel_y = math.sin(self.azimuth) * speed
                if self._landing_aim_pos is not None:
                    ax, ay = self._landing_aim_pos
                    self._turn_toward_ramped(
                        math.atan2(ay - self.pos_y, ax - self.pos_x), dt)
                else:
                    self._ramp_azimuth_step(0.0, dt, self._tank_turn_rate)
                # F1: Abbremsen bewegt den Bot jetzt real (Ausrollen wie im echten Client);
                # ohne -a/M ist vel=0 → No-Op.
                self._apply_bounds(dt, half)
            return

        if self._ai_state == AIState.COVER_HOLD:
            # P4-TAC-02: Entscheidungen (Ausgang/Peek-Start) im 10-Hz-Tick, Bewegung jeden Tick.
            if ai_tick:
                self._tick_cover_hold(now)
                if self._ai_state != AIState.COVER_HOLD:
                    return   # Tick hat den State gewechselt (Ausgang oder Dodge)
            if not self._jumping:
                ep = (self._get_enemy_pos(self.target_player)
                      if self.target_player is not None else None)
                if ep is not None:
                    # Ausrichtung für Ausbruch/Peek-Schuss: bei fehlendem LoS auf den gecachten
                    # Abprall-/Tor-Azimut (Rico-Drive), sonst direkt aufs Ziel (P4-TAC-05).
                    self._turn_toward_ramped(self._cover_hold_aim_az(self.target_player, ep), dt)
                else:
                    self._ramp_azimuth_step(0.0, dt, self._tank_turn_rate)
                # Peek-Zyklus: kurz vorfahren (Phase 1) und sofort rückwärts zurück (Phase 2).
                if self._cover_peek_phase == 1:
                    speed = self._tank_speed * 0.6
                    if now >= self._cover_peek_until:
                        self._cover_peek_phase = 2
                        self._cover_peek_until = now + COVER_PEEK_BACK_S
                elif self._cover_peek_phase == 2:
                    speed = -self._tank_speed * 0.6
                    if now >= self._cover_peek_until:
                        self._cover_peek_phase = 0
                else:
                    speed = 0.0   # halten
                # P4-MOV-02b: Peek ist Bodenfahrt → Beschleunigungsklemme (ohne -a/M instant)
                speed = self._ramp_linear_speed(speed, dt)
                self.vel_x = math.cos(self.azimuth) * speed
                self.vel_y = math.sin(self.azimuth) * speed
                # F2: Peek bewegt den Bot jetzt real (vorher nur vel gesetzt, Position
                # eingefroren); _apply_obstacle_bounds (Teil von _apply_bounds) verhindert
                # per Wall-Slide das Clippen in die Deckungsbox.
                self._apply_bounds(dt, half)
            return

        # IDLE / SEEKING / COMBAT (10 Hz KI-Tick)
        if ai_tick:
            # Fertige Async-Vollsuche (P4-INF-01) vor dem State-Tick übernehmen — nur in
            # navigierbaren Bodenstates (NAV_JUMP/NAV_TELE/FALLING returnen vorher).
            self._poll_async_plan()
            # B4: Rand-Bounce-Replan aus dem 60-Hz-Physik-Pfad (_apply_bounds) hierher
            # verlagert — kein synchroner A*-Lauf im Physik-Pfad, kein Ziel-Überschreiben
            # in committed States (EVADING etc. laufen hier gar nicht erst ein).
            if self._bounce_replan:
                self._bounce_replan = False
                # In COMBAT kein Zufalls-Ziel — der Combat-Tick replant ohnehin zum Gegner.
                if self._ai_state in (AIState.SEEKING, AIState.IDLE):
                    h = self.world_half
                    self._plan_path(random.uniform(-h * 0.85, h * 0.85),
                                    random.uniform(-h * 0.85, h * 0.85))
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
                                        AIState.LANDING_SHOT, AIState.NAV_JUMP_ALIGN,
                                        AIState.COVER_HOLD):
                if self._ai_state == AIState.IDLE and self.target_pos is None:
                    # IDLE geparkt: _move_to_target würde bei target_pos=None früh
                    # zurückkehren und Rest-Geschwindigkeit stehen lassen → explizit stoppen.
                    self.vel_x  = 0.0
                    self.vel_y  = 0.0
                    self.ang_vel = 0.0
                else:
                    self._move_to_target(dt, half)

    def _tick_jumping(self, dt: float, now: float) -> None:
        """Sprungphysik. Ohne WG-Luftsteuerung: keine Kontrolle (LocalPlayer.cxx Z. 364-368).

        Mit WG (_wings_air_control_active(), P4-MOV-03a): Bewegung ist strikt an ±Blickrichtung
        gekoppelt (_wings_air_steer) statt entkoppelt zu drehen (_jump_ang_vel wird dann NIE
        angewandt). Zielwahl-Priorität: WG-TactJump-Finte (_wg_feint_target, P4-MOV-03b —
        _wg_feint_tick) > expliziter Steuerziel-Azimuth (_wings_steer_az — aus
        Escape-/DODGE_JUMP-Drehwünschen, s. tactics.py/combat.py) > Gegner-Verfolgung
        (target_player + _has_presence, volle Speed) > Heading halten (aktueller signierter
        Horizontal-Speed, Projektion von vel auf azimuth). Die Finte kann pro Tick abbrechen
        (Gegner weg/tot, Rest-Sinkzeit zu knapp, Blick-Check negativ) — dann übernimmt im
        SELBEN Tick die nächste Priorität (_wg_feint_tick gibt False zurück)."""
        self.vel_z += self._effective_gravity() * dt
        self.pos_z += self.vel_z * dt
        if self._wings_air_control_active():
            if self._wg_feint_target is not None and self._wg_feint_tick(dt):
                pass
            elif self._wings_steer_az is not None:
                speed = self.vel_x * math.cos(self.azimuth) + self.vel_y * math.sin(self.azimuth)
                self._wings_air_steer(dt, self._wings_steer_az, speed)
            else:
                target_az = None
                if self.target_player is not None and self._has_presence():
                    ep = self._get_enemy_pos(self.target_player)
                    if ep is not None:
                        target_az = math.atan2(ep[1] - self.pos_y, ep[0] - self.pos_x)
                if target_az is not None:
                    self._wings_air_steer(dt, target_az, self._effective_tank_speed())
                else:
                    speed = self.vel_x * math.cos(self.azimuth) + self.vel_y * math.sin(self.azimuth)
                    self._wings_air_steer(dt, self.azimuth, speed)
        else:
            self.azimuth = _wrap(self.azimuth + self._jump_ang_vel * dt)
        if not self._can_drive_through_obstacles():
            self._apply_obstacle_bounds(dt)
        self.pos_x += self.vel_x * dt
        self.pos_y += self.vel_y * dt
        # Weltgrenzen-Clamp (kein Bounce im Sprung)
        half = self.world_half
        self.pos_x = max(-half, min(half, self.pos_x))
        self.pos_y = max(-half, min(half, self.pos_y))

        # WG: zusätzlicher Luftsprung beim Abwärtsbogen. Faithful zu doJump(): im Fallen wird die
        # Velocity nur additiv angehoben (v + vz, hier vz<0), kein voller neuer Bogen.
        if self.own_flag == "WG" and self.vel_z < 0 and self._can_jump(now):
            self.vel_z = self._jump_launch_vz(self.vel_z)
            self._wings_jumps_used += 1

        if self._is_landed():
            self.pos_z = self._get_floor_z()
            self.vel_z = 0.0
            self._jumping = False
            self._wings_jumps_used = 0
            self._wings_steer_az = None
            self._wg_feint_target = None   # P4-MOV-03b: Finte endet spätestens bei der Landung
            self._wg_feint_phase = 0
            self._last_jump_at = now
            self.ang_vel = 0.0
            if self.target_player is not None and self._has_presence():
                self._transition_to(AIState.COMBAT)
            elif self._has_presence():
                self._transition_to(AIState.SEEKING)
            else:
                self._transition_to(AIState.IDLE)

    def _wg_feint_tick(self, dt: float) -> bool:
        """Ein Steuer-Tick der WG-TactJump-Finte (P4-MOV-03b) — höchste Priorität im WG-Zweig
        von _tick_jumping, solange _wg_feint_target gesetzt ist (gesetzt vom Frontal-Zweig von
        _execute_jump, s. tactics.py).

        Phase 0 (vorwärts, _wg_feint_phase == 0): fliegt mit voller _effective_tank_speed()
        Richtung Gegner, bis er horizontal an ihm vorbei ist — Kriterium: Projektion von
        (bot_pos − enemy_pos) auf die aktuelle Flugrichtung (Einheitsvektor aus vel_x/vel_y,
        bei Speed≈0 ersatzweise azimuth) ≥ TANK_LENGTH. Am Umschaltpunkt wird die Blickrichtung
        des Gegners GENAU EINMAL geprüft (keine Flatter-Entscheidung pro Tick):
          - Gegner hat sich zum Bot gedreht (< 45° Abweichung) → Finte bestätigt:
            _wg_feint_phase = 1, ab jetzt Phase 1 (rückwärts) bis zur Landung.
          - Gegner hat sich NICHT gedreht → keine Finte: _wg_feint_target wird gelöscht und
            _wings_steer_az auf die aktuelle Blickrichtung fixiert (Heading halten statt
            Gegner-Verfolgung — sonst würde Priorität (2) in _tick_jumping die Flugbahn zurück
            zum Gegner krümmen), der Bot landet klassisch vorwärts hinter dem Gegner.

        Phase 1 (rückwärts, _wg_feint_phase == 1): _wings_air_steer mit Ziel-Azimuth Richtung
        Gegner und halber Rückwärts-Speed (Azimuth bleibt auf dem Gegner — Feuerfenster bei der
        Landung), kein erneuter Blick-Check.

        Fallback (in JEDER Phase): Gegner tot/verschwunden ODER Rest-Sinkzeit zu knapp
        (sinkend UND < 2u über dem Boden) → Finte abbrechen (_wg_feint_target = None).

        Rückgabe: True, wenn dieser Tick von der Finte gesteuert wurde (vel_x/vel_y/azimuth
        bereits gesetzt); False, wenn sie abgebrochen wurde — der Aufrufer (_tick_jumping)
        fällt dann im SELBEN Tick auf die nächste Priorität zurück."""
        info = self.players.get(self._wg_feint_target)
        if info is None or not info.alive:
            self._wg_feint_target = None
            self._wg_feint_phase = 0
            return False
        if self.vel_z < 0.0 and self.pos_z - self._get_floor_z() < 2.0:
            self._wg_feint_target = None
            self._wg_feint_phase = 0
            return False

        ex, ey = info.pos[0], info.pos[1]
        bx, by = self.pos_x - ex, self.pos_y - ey             # bot − Gegner
        az_to_enemy = math.atan2(-by, -bx)                    # Steuerziel: Blick zum Gegner

        if self._wg_feint_phase == 1:
            # Phase 1 (rückwärts): Umschaltpunkt bereits entschieden, kein erneuter Blick-Check.
            self._wings_air_steer(dt, az_to_enemy, -0.5 * self._effective_tank_speed())
            return True

        # Phase 0 (vorwärts): Flugrichtung als Einheitsvektor (bei ~Stillstand: Azimuth-Ausweiche)
        speed_now = math.hypot(self.vel_x, self.vel_y)
        if speed_now > 1e-3:
            fdir_x, fdir_y = self.vel_x / speed_now, self.vel_y / speed_now
        else:
            fdir_x, fdir_y = math.cos(self.azimuth), math.sin(self.azimuth)
        proj = bx * fdir_x + by * fdir_y
        if proj < TANK_LENGTH:
            self._wings_air_steer(dt, az_to_enemy, self._effective_tank_speed())
            return True

        # Umschaltpunkt erreicht: Gegner-Blickrichtung genau einmal prüfen.
        az_enemy_to_bot = math.atan2(by, bx)                  # Blick des Gegners zum Bot hin
        hat_gedreht = abs(_angle_diff(info.azimuth, az_enemy_to_bot)) < math.radians(45)
        if hat_gedreht:
            self._wg_feint_phase = 1
            self._wings_air_steer(dt, az_to_enemy, -0.5 * self._effective_tank_speed())
            return True
        # Gegner NICHT gedreht: keine Finte — Heading halten statt Gegner-Verfolgung.
        self._wg_feint_target = None
        self._wg_feint_phase = 0
        self._wings_steer_az = self.azimuth
        return False

    def _tick_explosion(self, dt: float) -> None:
        """Integriert den Explosions-Bogen des Wracks (tot, PS_EXPLODING) — spiegelt explodeTank:
        Aufwärts-Velocity unter Schwerkraft, Horizontal-Momentum bleibt; bei Bodenkontakt liegen
        bleiben (vel[2]=0). Die Explosion läuft optisch bis _exploding_until weiter."""
        floor_z = self._get_floor_z()
        self.vel_z += self._gravity * dt
        self.pos_z = max(self.pos_z + self.vel_z * dt, floor_z)
        if self.pos_z <= floor_z + 1e-6:
            self.pos_z = floor_z
            self.vel_z = 0.0
        self.pos_x += self.vel_x * dt
        self.pos_y += self.vel_y * dt
        half = self.world_half
        self.pos_x = max(-half, min(half, self.pos_x))
        self.pos_y = max(-half, min(half, self.pos_y))

    def _tick_nav_jump(self, dt: float, now: float) -> None:
        """Navigationssprung-Physik. Landet auf Ziel-Etage → return_state.

        Mit WG-Luftsteuerung (P4-MOV-03a): Kurskorrektur Richtung Lande-WP (nav_path[0]) +
        Speed-Nachführung aus der Rest-Sinkzeit (Lösung von z(t)=_nav_jump_target_z), statt der
        am Absprung fixierten Lande-Drehung (_jump_ang_vel wird dann NIE angewandt) — kein
        Land-Spin, Landeausrichtung = Flugrichtung; Nachdrehen übernimmt die Bodensteuerung nach
        der Landung."""
        self.vel_z += self._effective_gravity() * dt
        self.pos_z += self.vel_z * dt
        if self._wings_air_control_active() and self._nav_path:
            wp = self._nav_path[0]
            g_abs = abs(self._effective_gravity())
            disc = self.vel_z * self.vel_z - 2.0 * g_abs * (self._nav_jump_target_z - self.pos_z)
            if disc >= 0.0 and g_abs > 1e-6:
                t_rem = max((self.vel_z + math.sqrt(disc)) / g_abs, 0.01)
            else:
                t_rem = 0.01
            hdist_rem = math.hypot(wp[0] - self.pos_x, wp[1] - self.pos_y)
            target_az = math.atan2(wp[1] - self.pos_y, wp[0] - self.pos_x)
            speed = max(1.0, min(hdist_rem / t_rem, self._travel_tank_speed()))
            self._wings_air_steer(dt, target_az, speed)
        elif self._wings_air_control_active():
            speed = self.vel_x * math.cos(self.azimuth) + self.vel_y * math.sin(self.azimuth)
            self._wings_air_steer(dt, self.azimuth, speed)
        else:
            self.azimuth = _wrap(self.azimuth + self._jump_ang_vel * dt)   # Lande-Drehung (am Absprung fixiert)
        if not self._can_drive_through_obstacles():
            self._apply_obstacle_bounds(dt)
        self.pos_x += self.vel_x * dt
        self.pos_y += self.vel_y * dt
        half = self.world_half
        self.pos_x = max(-half, min(half, self.pos_x))
        self.pos_y = max(-half, min(half, self.pos_y))

        if self._is_landed():
            floor_z = self._get_floor_z()
            self.pos_z = floor_z
            self.vel_z = 0.0
            self._jumping = False
            self._wings_jumps_used = 0
            self._wings_steer_az = None
            self._wg_feint_target = None   # P4-MOV-03b: Finte endet spätestens bei der Landung
            self._wg_feint_phase = 0
            self._last_jump_at = now
            self.ang_vel = 0.0
            ret = self._nav_jump_return_state
            if ret in (AIState.NAV_JUMP, AIState.NAV_JUMP_ALIGN):
                ret = self._ground_state()   # nie auf sich selbst zurück (Endlosfalle)
            if abs(floor_z - self._nav_jump_target_z) > 1.5:
                # Falsche Etage gelandet → Route verwerfen und neu planen
                self._nav_path = []
                self._nav_goal = None
                self._transition_to(ret)
                self._new_target()
                return
            self._transition_to(ret)
            self._advance_path()

    def _tick_nav_jump_align(self, dt: float, now: float) -> None:
        """Richtet Bot auf Sprungziel-Azimuth aus; wechselt dann zu NAV_JUMP."""
        wp  = self._nav_jump_align_wp
        ret = self._nav_jump_align_return_state
        if ret in (AIState.NAV_JUMP, AIState.NAV_JUMP_ALIGN):
            ret = self._ground_state()   # nie auf sich selbst zurück (Endlosfalle)
        if wp is None:
            self._transition_to(ret)
            return
        if now - self._nav_jump_align_start > 5.0:
            wp_key = (round(wp[0]), round(wp[1]), wp[2])
            self._nav_jump_cooldowns[wp_key] = now + 30.0
            self._nav_jump_cooldowns = {k: v for k, v in self._nav_jump_cooldowns.items() if v > now}
            self._nav_path = []
            self._nav_goal = None
            self.target_pos = None
            self._transition_to(ret)
            return
        az_to_wp = math.atan2(wp[1] - self.pos_y, wp[0] - self.pos_x)
        diff = self._turn_toward(az_to_wp, dt)
        self.vel_x = 0.0
        self.vel_y = 0.0
        if abs(diff) <= math.pi / 36:
            self._initiate_nav_jump(wp)

    def _tick_nav_tele(self, dt: float, now: float) -> None:
        """Fährt das letzte kurze Stück direkt in die Teleporter-Mitte, bis der zentrale
        _check_teleport_crossing (im _update_movement-Wrapper, nach diesem Tick) quert — oder
        bis Timeout/Revert. Ersetzt das Anfahren des mittenseitigen Austritts-WP, an dem der Bot
        sonst (Reichweite erreicht) davor stehen blieb."""
        ret = self._nav_tele_return_state or self._ground_state()
        if ret in (AIState.NAV_TELE, AIState.NAV_JUMP, AIState.NAV_JUMP_ALIGN):
            ret = self._ground_state()
        center = self._nav_tele_center
        # Erfolg: der Wrapper-Crossing-Check hat im vorherigen Tick gewarpt (→ _teleporting_until).
        if now < self._teleporting_until:
            logger.info("[%s] NAV_TELE: Querung erfolgreich → %s", self.callsign, ret.name)
            self._nav_tele_center = None
            self._transition_to(ret)
            return
        # Abbruch: Timeout deckt auch den Revert ab (bei _is_inside_obstacle setzt der Crossing-
        # Check _teleporting_until NICHT → kein Erfolg → nach ≤NAV_TELE_TIMEOUT Abbruch).
        # P4-MOV-02b: Timeout um die Anfahr-Rampe nachführen (ohne -a/M +0 → wie bisher).
        if center is None or now - self._nav_tele_start > NAV_TELE_TIMEOUT + self._momentum_ramp_time(1.0):
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
        ddx, ddy = cx - self.pos_x, cy - self.pos_y
        d = math.hypot(ddx, ddy) or 1.0
        aim_x = cx + (ddx / d) * NAV_TELE_OVERSHOOT
        aim_y = cy + (ddy / d) * NAV_TELE_OVERSHOOT
        target_az = math.atan2(aim_y - self.pos_y, aim_x - self.pos_x)
        diff = self._turn_toward_ramped(target_az, dt)
        speed = self._effective_tank_speed() if abs(diff) < math.pi / 2 else 0.0
        # P4-MOV-02b: Endanflug ist Bodenfahrt → Beschleunigungsklemme (ohne -a/M instant)
        speed = self._ramp_linear_speed(speed, dt)
        self.vel_x = math.cos(self.azimuth) * speed
        self.vel_y = math.sin(self.azimuth) * speed
        self._apply_bounds(dt, self.world_half)
        if self._debug_log_tele:
            _t = time.monotonic()
            if _t - self._debug_nav_tele_t > 0.25:
                self._debug_nav_tele_t = _t
                logger.debug(
                    "[%s] NAV_TELE: pos=(%.1f,%.1f,%.1f) →Mitte(%.1f,%.1f) dist=%.1fu "
                    "spd=%.1f az=%.0f° innen=%s",
                    self.callsign, self.pos_x, self.pos_y, self.pos_z, cx, cy, d,
                    speed, math.degrees(self.azimuth), self._is_inside_obstacle())

    def _tick_z_attack(self, dt: float, now: float) -> None:
        """ZJ1-Sprungphysik. Nur aus COMBAT erreichbar; Landung → immer COMBAT.

        Mit WG-Luftsteuerung (P4-MOV-03a): kein entkoppelter Spin — Steuerziel ist der beim
        Absprung berechnete Ziel-Azimuth (_wings_steer_az), sonst der Live-Gegner, sonst Heading;
        die Horizontalgeschwindigkeit bleibt betragsgleich (Z-Attack übernimmt die Boden-vel)."""
        self.vel_z += self._effective_gravity() * dt
        self.pos_z += self.vel_z * dt
        if self._wings_air_control_active():
            target_az = self._wings_steer_az
            if target_az is None and self.target_player is not None:
                _ep_wg = self._get_enemy_pos(self.target_player)
                if _ep_wg is not None:
                    target_az = math.atan2(_ep_wg[1] - self.pos_y, _ep_wg[0] - self.pos_x)
            if target_az is None:
                target_az = self.azimuth
            speed = self.vel_x * math.cos(self.azimuth) + self.vel_y * math.sin(self.azimuth)
            self._wings_air_steer(dt, target_az, speed)
        else:
            self.azimuth = _wrap(self.azimuth + self._jump_ang_vel * dt)
        if not self._can_drive_through_obstacles():
            self._apply_obstacle_bounds(dt)
        self.pos_x += self.vel_x * dt
        self.pos_y += self.vel_y * dt
        half = self.world_half
        self.pos_x = max(-half, min(half, self.pos_x))
        self.pos_y = max(-half, min(half, self.pos_y))

        if self._z_attack_mode:
            if abs(self.pos_z - self._z_attack_fire_z) < 1.5:
                if self._next_shoot <= now and self._next_slot_ready(now):
                    _shoot = True
                    if self.target_player is not None:
                        _ep = self._get_enemy_pos(self.target_player)
                        if _ep is not None:
                            _az_to_enemy = math.atan2(_ep[1] - self.pos_y, _ep[0] - self.pos_x)
                            if abs(_angle_diff(self.azimuth, _az_to_enemy)) > math.radians(15):
                                _shoot = False
                    if _shoot and self._can_shoot():
                        self._send_shot(now, self.azimuth)
                        self._set_next_shoot_after_fire(now)
                        self._z_attack_mode = False  # nur nach gefeuertem Schuss deaktivieren
                    # schlechter Winkel → nächster Tick versucht erneut (Modus bleibt aktiv)

        if self._is_landed():
            self.pos_z = self._get_floor_z()
            self.vel_z = 0.0
            self._jumping = False
            self._wings_jumps_used = 0
            self._wings_steer_az = None
            self._wg_feint_target = None   # P4-MOV-03b: Finte endet spätestens bei der Landung
            self._wg_feint_phase = 0
            self._last_jump_at = now
            self._z_attack_mode = False
            self.ang_vel = 0.0
            self._transition_to(AIState.COMBAT)

    def _tick_falling(self, dt: float, now: float) -> None:
        """Fall-Physik für unkontrollierten Fall vom Dach (analog _tick_jumping).
        Ohne WG-Luftsteuerung: kein Lenken, vel[0]/vel[1] und azimuth bleiben committed
        (_jump_ang_vel wird nicht zurückgesetzt — bestehende Drehbewegung bleibt).
        Mit WG (P4-MOV-03a): steuerbar Richtung aktuellem Nav-WP (nav_path[0]), falls
        vorhanden, sonst Heading halten (aktueller signierter Horizontal-Speed)."""
        self.vel_z += self._effective_gravity() * dt
        self.pos_z += self.vel_z * dt
        if self._wings_air_control_active():
            if self._nav_path:
                wp = self._nav_path[0]
                target_az = math.atan2(wp[1] - self.pos_y, wp[0] - self.pos_x)
            else:
                target_az = self.azimuth
            speed = self.vel_x * math.cos(self.azimuth) + self.vel_y * math.sin(self.azimuth)
            self._wings_air_steer(dt, target_az, speed)
        else:
            self.azimuth = _wrap(self.azimuth + self._jump_ang_vel * dt)
        if not self._can_drive_through_obstacles():
            self._apply_obstacle_bounds(dt)
        self.pos_x += self.vel_x * dt
        self.pos_y += self.vel_y * dt
        half = self.world_half
        self.pos_x = max(-half, min(half, self.pos_x))
        self.pos_y = max(-half, min(half, self.pos_y))

        if self._is_landed():
            self.pos_z = self._get_floor_z()
            self.vel_z = 0.0
            self._jumping = False
            self._wings_jumps_used = 0
            self._wings_steer_az = None
            self._wg_feint_target = None   # P4-MOV-03b: Finte endet spätestens bei der Landung
            self._wg_feint_phase = 0
            # _last_jump_at nicht setzen — kein echter Sprung, kein Cooldown
            self.ang_vel = 0.0
            self._transition_to(self._pre_fall_state)

    def _tick_committed(self, dt: float, now: float) -> None:
        """Führt aktiven Dodge aus. Keine neuen KI-Entscheidungen.

        JUMP_WINDUP: kein Abbruch bei Bedrohung. Nur Notschuss möglich,
        Sprung wird trotzdem ausgeführt (Entscheidung steht)."""
        half = self.world_half

        if self._ai_state == AIState.JUMP_WINDUP:
            incoming, _ = self._find_incoming_shot(now)
            if incoming is not None:
                t_impact = incoming.time_to_closest(self.pos_x, self.pos_y)
                if t_impact < 0.1 and self.client.udp_active and self.player_id is not None:
                    self._send_shot(now, self.azimuth)
                    if self._debug_log_shot:
                        logger.debug("[%s] Schuss: Notschuss während Wind-Up (t=%.2fs)",
                                     self.callsign, t_impact)

        # Fix E1/EV1: EVADING früh beenden nur wenn Schuss auch für alle typischen
        # Bewegungsrichtungen ungefährlich ist (verhindert Fehlausstieg durch Dodge-Velocity).
        if self._ai_state == AIState.EVADING:
            fwd_vx = math.cos(self.azimuth) * self._tank_speed
            fwd_vy = math.sin(self.azimuth) * self._tank_speed
            # P3: ein Scan über alle Schüsse statt vier separater _find_incoming_shot-Aufrufe
            # (je ein Shots-Lock + voller Schuss-/Ricochet-Scan) mit identischer Bedrohungslogik.
            if not self._any_incoming_threat(now, (
                    (self.vel_x, self.vel_y), (0.0, 0.0),
                    (fwd_vx, fwd_vy), (-fwd_vx, -fwd_vy))):
                self._dodging = False
                self._dodge_forward = False
                self._dodge_reverse = False
                
                # Fix EV2: Per-Schuss-Grace — denselben Schuss 1 s ignorieren damit nach
                # dem Early-Exit weder EVADING noch DODGE_JUMP neu ausgelöst werden.
                if self._last_threat_id is not None:
                    # B7: abgelaufene Einträge beim Einfügen mit ausfiltern (seltener Pfad) —
                    # sonst leakt das Dict über eine lange Session (nie sonst aufgeräumt).
                    self._evade_cleared_shots = {k: v for k, v in self._evade_cleared_shots.items() if v > now}
                    self._evade_cleared_shots[self._last_threat_id] = now + EVADE_CLEAR_GRACE
                self._last_threat_id = None

                if self._debug_log_dodge:
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
                    self._ramp_azimuth_step(0.0, dt, self._tank_turn_rate)
                    speed = -self._tank_speed * 0.5
            elif self._dodge_forward:
                self._ramp_azimuth_step(0.0, dt, self._tank_turn_rate)
                speed = self._tank_speed
            else:
                self._turn_toward_ramped(self._dodge_dir, dt)
                speed = self._tank_speed
            # P4-MOV-02b: EVADING/JUMP_WINDUP-Dodge ist Bodenfahrt → lineare Beschleunigungsklemme
            # (ohne -a wie bisher instant). time_to_dodge führt die Anfahr-Rampe im Trigger nach.
            speed = self._ramp_linear_speed(speed, dt)
            self.vel_x = math.cos(self.azimuth) * speed
            self.vel_y = math.sin(self.azimuth) * speed
            self._apply_bounds(dt, half)
            return

        # Timer abgelaufen
        self._dodging = False
        self._dodge_forward = False
        self._dodge_reverse = False

        # Fix EV2: Per-Schuss-Grace — denselben Schuss 1 s ignorieren damit nach
        # dem Early-Exit weder EVADING noch DODGE_JUMP neu ausgelöst werden.
        if self._last_threat_id is not None:
            # B7: abgelaufene Einträge beim Einfügen mit ausfiltern (siehe oben).
            self._evade_cleared_shots = {k: v for k, v in self._evade_cleared_shots.items() if v > now}
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

    def _tick_landing_shot(self, now: float) -> None:
        """KI während LANDING_SHOT: Azimuth auf Landepunkt; nur Bedrohung von anderen prüfen.
        Bewegung (vel=0) und Azimuth-Drehung werden in _update_movement (60Hz) gehandhabt."""
        # Menschlicher "Doppelklick"-Nachschuss: nach dem ersten Schuss wartet der Bot wenige
        # Ticks und feuert – falls noch ausgerichtet und ein Slot frei ist – ein zweites Mal.
        # Das deckt einen leicht zu früh berechneten ersten Schuss ab (siehe Plan/DEVELOPER).
        # Vor dem Timeout-Check, damit _landing_shot_until den Nachschuss nicht verschluckt.
        if self._landing_second_shot_at is not None:
            if now >= self._landing_second_shot_at:
                info = (self.players.get(self.target_player)
                        if self.target_player is not None else None)
                if (info is not None and self._landing_aim_pos is not None
                        and self._can_shoot() and self._next_slot_ready(now)):
                    _target_az = math.atan2(self._landing_aim_pos[1] - self.pos_y,
                                            self._landing_aim_pos[0] - self.pos_x)
                    if abs(_angle_diff(_target_az, self.azimuth)) <= math.radians(25):
                        self._send_shot(now, self.azimuth)
                        self._set_next_shoot_after_fire(now)
                        if self._debug_log_shot:
                            logger.debug("[%s] Schuss: LANDING_SHOT Nachschuss (Doppelklick)",
                                         self.callsign)
                self._landing_second_shot_at = None
                self._transition_to(
                    AIState.COMBAT if self.target_player is not None else AIState.SEEKING)
            return
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
                        self._landing_aim_pos[0] - self.pos_x,
                        self._landing_aim_pos[1] - self.pos_y)
                    _tof = _dist_aim / max(self._effective_shot_speed(), 1.0)
                    if _t_rem <= _tof + 0.15:
                        _target_az = math.atan2(
                            self._landing_aim_pos[1] - self.pos_y,
                            self._landing_aim_pos[0] - self.pos_x)
                        _aligned = abs(_angle_diff(_target_az, self.azimuth)) <= math.radians(25)
                        if (_aligned and self._can_shoot()
                                and now >= self._next_shoot and self._next_slot_ready(now)):
                            self._send_shot(now, self.azimuth)
                            self._set_next_shoot_after_fire(now)
                            if self._debug_log_shot:
                                logger.debug("[%s] Schuss: LANDING_SHOT (t_rem=%.2fs tof=%.2fs)",
                                             self.callsign, _t_rem, _tof)
                            # Doppelklick-Nachschuss nur einplanen, wenn bis dahin auch ein Slot
                            # frei wird (Reload-Zeitpunkt des nächsten Slots steht nach _send_shot
                            # bereits fest) – sonst wie bisher sofort weiter.
                            _ns = (self._shot_slot + 1) % self._max_shots
                            if self._slot_reload_at[_ns] <= now + LANDING_DOUBLE_SHOT_DELAY:
                                self._landing_second_shot_at = now + LANDING_DOUBLE_SHOT_DELAY
                                self._landing_shot_until = max(
                                    self._landing_shot_until,
                                    now + LANDING_DOUBLE_SHOT_DELAY + 0.05)
                                return
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
            t_impact = threat.time_to_closest(self.pos_x, self.pos_y)
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

    def _tick_idle(self, now: float) -> None:
        """IDLE: Passiv-Stillstand + Übergang zu SEEKING wenn Menschen/Zuschauer da.
        Kein Wandern mehr — ein evtl. Rest-Pfad (nach SEEKING→IDLE) wird zu Ende
        gefahren, danach bleibt der Bot stehen (CPU sparen). Bedrohungen werden auch
        im Passiv-Modus erkannt (Schuss kann jederzeit kommen)."""
        if self._handle_threat(now):
            return
        if self._has_presence():
            self._transition_to(AIState.SEEKING)
            return  # _tick_seeking übernimmt Navigation im nächsten Tick
        self._move_reverse = False

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
        # P4-MOV-02a: Stuck-Fenster um die Anfahr-Rampe nachführen (ohne -a: +0 → wie bisher),
        # damit die träge Anfahrt auf sehr niedrigen -a-Configs nicht als "festgefahren" gilt.
        if now - self._last_pos_check_time >= STUCK_WINDOW + self._momentum_ramp_time(1.0):
            d = math.hypot(self.pos_x - self._last_pos_check[0],
                           self.pos_y - self._last_pos_check[1])
            if d < STUCK_MIN_DIST and self.target_pos is not None:
                self._new_target()
            self._last_pos_check_time = now
            self._last_pos_check = [self.pos_x, self.pos_y]
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
