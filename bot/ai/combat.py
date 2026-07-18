"""Kampf: COMBAT-Tick, Bedrohungsreaktion/Dodge und Eskalation bei unerreichbaren Gegnern (W4, FABLE-PLAN Teil 3)."""

import math
import random
import time
import logging
from typing import Optional

from bot.constants import (
    NAV_CELL_SIZE,
    NAV_WALL_PROBE_DIST,
    COMBAT_STALL_WIN_MIN,
    COMBAT_STALL_WIN_MAX,
    COMBAT_STALL_MIN_DIST,
    COMBAT_STALL_REV_MIN,
    COMBAT_STALL_REV_MAX,
    COMBAT_STALL_WP_MIN,
    COMBAT_STALL_WP_MAX,
    COMBAT_STALL_TIMEOUT,
    COMBAT_REPLAN_RETRY,
    UNREACH_DIRECT_TIME,
    UNREACH_AVOID_TIME,
    UNREACH_REPOS_RADIUS,
    UNREACH_REPOS_TIMEOUT,
    COMBAT_DIST_DEADZONE,
    HIT_RADIUS,
    DODGE_REACT_DELAY,
    IB_REACT_MULTIPLIER,
    M_REACT_MULTIPLIER,
    CS_REACT_MULTIPLIER,
    PAUSE_WAIT_S,
    COVER_HOLD_MAX_S,
    COVER_HOLD_COOLDOWN_S,
    COVER_PEEK_CHANCE,
    COVER_PEEK_OUT_S,
    COVER_MAX_RANGE_FACTOR,
    COVER_BREAKOUT_MIN_WINDOW_S,
    COVER_CLOSE_EXIT_FRAC,
    RICO_AIM_MAX_COVER,
)
from bot.util import _angle_diff, _wrap
from bot.models import AIState

logger = logging.getLogger("bzbot")


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class CombatMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

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
        # P4-TAC-02: an einer Deckungskante kurz halten statt offen anzugreifen (defensiver State).
        if self._should_hold_in_cover(now):
            self._cover_hold_until = now + COVER_HOLD_MAX_S
            self._cover_peek_phase = 0
            self._transition_to(AIState.COVER_HOLD)
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
        time_to_impact = max(0.0, threat.time_to_closest(self.pos_x, self.pos_y) - _elapsed)
        # Für Ricochet-Schüsse: Segment-basierte Zeit statt linearer Anfangsgeschwindigkeit
        if threat_key in self._ricochet_paths:
            time_to_impact = max(0.0, threat_t)
        dodge_dir, orig_diff = self._compute_dodge_dir(threat, now)
        turn_rad = abs(_angle_diff(dodge_dir, self.azimuth))
        # Wie viel Zeit braucht der Bot zum Ausweichen:
        # Fahrweg (einen Trefferradius) + 30% der Drehzeit bis zur Ausweichrichtung.
        # P4-MOV-02b: + Anfahr-Rampe (_momentum_ramp_time), da der Dodge jetzt beschleunigt statt
        # instant loszufahren. Ohne -a/M = 0 (unverändert); bei -a 50 ~0,05 s, bei trägen Configs/M
        # groß genug, um rechtzeitig auf DODGE_JUMP umzuschwenken statt einen zu langsamen Dodge zu
        # starten (_accel_limits berücksichtigt dabei M).
        time_to_dodge = (HIT_RADIUS * 1.3 / max(self._tank_speed, 1e-6)
                         + turn_rad / max(self._tank_turn_rate, 1e-6) * 0.3
                         + self._momentum_ramp_time(1.0))
        # Wenn Ausweichen noch möglich (10% Puffer gegen knappe Situationen)
        if time_to_dodge * 1.1 <= time_to_impact:
            self._setup_dodge(threat, now, time_to_impact, dodge_dir, orig_diff)
            self._transition_to(AIState.EVADING)
        elif (not self._jumping and self._is_landed()
              and self.own_flag not in ("NJ", "BU")
              and (self._server_jumping or self.own_flag in ("WG", "BY", "JP"))
              and not self._debug_no_jump):
            # Fix EV2: Per-Schuss-Grace — Schuss der beim Early-Exit als ungefährlich
            # eingestuft wurde für 1 s ignorieren (verhindert sofortigen DODGE_JUMP).
            if self._evade_cleared_shots.get(threat_key, 0.0) > now:
                return False
            # Fix E3: DODGE_JUMP — defensiver Sprung, minimale Rotation.
            # P4-MOV-02b: setzt bewusst KEIN vel_x/vel_y — die Horizontalgeschwindigkeit bleibt, was
            # die Boden-Rampe hinterlassen hat (entspricht exakt doJump: alte vel übernommen, kein
            # doMomentum). Damit ist der Dodge-Sprung ohne weitere Änderung bereits ramp-korrekt.
            self.vel_z = self._jump_launch_vz(self.vel_z)
            self._jumping = True
            # P4-MOV-03a: mit WG-Luftsteuerung wird der Drehwunsch NIE mehr in _jump_ang_vel
            # geschrieben (entkoppeltes Drehen ist mit WG physikalisch unmöglich) — die Korrektur
            # wird stattdessen zum Steuerziel-Azimuth _wings_steer_az (direkt der Gegner-Azimuth,
            # da hier — anders als beim Escape-Spin — ein echtes Ziel statt einer Rate vorliegt).
            wg_air = self._wings_air_control_active()
            if wg_air:
                self._wings_steer_az = None
                if self.target_player is not None:
                    ep = self._get_enemy_pos(self.target_player)
                    if ep is not None:
                        enemy_az = math.atan2(ep[1] - self.pos_y, ep[0] - self.pos_x)
                        needed = _angle_diff(enemy_az, self.azimuth)
                        if abs(needed) > math.radians(135):
                            # Nur korrigieren wenn Rücken zum Gegner → Steuerziel = Gegner-Azimuth
                            self._wings_steer_az = enemy_az
                            if self._debug_log_dodge:
                                logger.debug("[%s] Ausweichen: Dodge-Sprung mit Korrektur (%.0f°)",
                                             self.callsign, math.degrees(needed))
            else:
                jump_time = 2.0 * self._effective_jump_velocity() / max(abs(self._effective_gravity()), 0.001)
                if self.own_flag != "WG":
                    self._jump_ang_vel = 0.0
                if self.target_player is not None:
                    ep = self._get_enemy_pos(self.target_player)
                    if ep is not None:
                        enemy_az = math.atan2(ep[1] - self.pos_y, ep[0] - self.pos_x)
                        needed = _angle_diff(enemy_az, self.azimuth)
                        if abs(needed) > math.radians(135):
                            # Nur korrigieren wenn Rücken zum Gegner → sanfte Rotation
                            self._jump_ang_vel = math.copysign(
                                min(abs(needed / max(jump_time, 0.001)) * 0.5,
                                    self._tank_turn_rate * 0.5), needed)
                            if self._debug_log_dodge:
                                logger.debug("[%s] Ausweichen: Dodge-Sprung mit Korrektur (%.0f°)",
                                             self.callsign, math.degrees(needed))
            if self._debug_log_dodge:
                logger.debug("[%s] Ausweichen: Dodge-Sprung statt Ausweichen (Zeit zu knapp)", self.callsign)
            # analog zu _initiate_nav_jump; bei WG-Luftsteuerung überschreibt der nächste
            # _tick_jumping-Tick (_wings_air_steer) ang_vel ohnehin sofort mit dem echten Wert.
            self.ang_vel = 0.0 if wg_air else self._jump_ang_vel
            self._transition_to(AIState.DODGE_JUMP)
        else:
            if self._debug_log_shot:
                logger.debug("[%s] Schuss: Notschuss – jumping=%s z=%.1f landed=%s flag=%s t_imp=%.3f",
                             self.callsign, self._jumping, self.pos_z, self._is_landed(),
                             self.own_flag, time_to_impact)
            if (self.client.udp_active
                    and self._last_notschuss_threat != threat_key
                    and now >= self._next_shoot
                    and self._next_slot_ready(now)):
                self._last_notschuss_threat = threat_key
                self._send_shot(now, self.azimuth)
                self._set_next_shoot_after_fire(now)
                if self._debug_log_shot:
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
        dot_r = ((self.pos_x - sx) * math.cos(perp_r)
                 + (self.pos_y - sy) * math.sin(perp_r))
        best_perp = perp_r if dot_r > 0 else perp_l
        diff = _angle_diff(best_perp, self.azimuth)
        capped = _wrap(self.azimuth + math.copysign(min(abs(diff), math.radians(60)), diff))
        return capped, diff

    def _should_hold_in_cover(self, now: float) -> bool:
        """Eingangs-Entscheidung für COVER_HOLD (P4-TAC-02): steht der Bot an einer Deckungskante und
        sollte defensiv halten statt offen anzugreifen?"""
        if now < self._cover_cooldown_until:
            return False
        pid = self.target_player
        if pid is None:
            return False
        info = self.players.get(pid)
        if info is None or not info.alive:
            return False
        # SB durchschlägt Wände, SW wirkt radial → Deckung ist gegen beide wertlos. GM erwünscht
        # (Deckung bricht den Lock), daher keine Ausnahme.
        if info.flag in ("SB", "SW"):
            return False
        # P4-TAC-05: Gegner leergeschossen und Nachlade-Fenster groß genug → gar nicht erst verstecken,
        # sondern die Blöße des Gegners zum Angriff nutzen.
        if (self._enemy_slots_empty(info, now)
                and self._enemy_next_slot_ready_in(info, now) >= COVER_BREAKOUT_MIN_WINDOW_S):
            return False
        ep = self._get_enemy_pos(pid)
        if ep is None:
            return False
        if math.hypot(ep[0] - self.pos_x, ep[1] - self.pos_y) > self._shot_range * COVER_MAX_RANGE_FACTOR:
            return False
        # Nur im Direktmodus greifen (dort leert _execute_combat_move den Pfad) — A*-Navigation nicht
        # unterbrechen (bewusste Design-Entscheidung: Kämpfe laufen zumeist im Direktmodus).
        if self._nav_path:
            return False
        return self._cover_edge_ahead(pid, now)

    def _cover_hold_exit(self, now: float) -> None:
        """Verlässt COVER_HOLD zurück in den Boden-State und setzt den Wieder-Eintritts-Cooldown."""
        self._cover_cooldown_until = now + COVER_HOLD_COOLDOWN_S
        self._cover_peek_phase = 0
        self._transition_to(self._ground_state())

    def _cover_hold_aim_az(self, pid: int, ep) -> float:
        """Ziel-Azimut für die Rumpf-Ausrichtung in COVER_HOLD: ohne LoS zum Gegner der gecachte
        Abprall-/Tor-Azimut (Rico-Drive-Muster aus _execute_combat_move — nur so passiert der Rumpf
        das Fire-Gate und _maybe_shoot feuert Indirekt-Schüsse aus der Deckung); sonst direkt aufs Ziel."""
        _cache = self._rico_aim_cache
        if (_cache is not None and _cache[1] == pid and _cache[2] is not None
                and not self._has_los_to_enemy(pid)):
            return _cache[2][0]
        return math.atan2(ep[1] - self.pos_y, ep[0] - self.pos_x)

    def _tick_cover_hold(self, now: float) -> None:
        """COVER_HOLD-Entscheidungen (10 Hz): Dodge hat Vorrang, sonst Ausgangsbedingungen prüfen und
        gelegentlich einen kurzen Peek starten. Die 60-Hz-Ausführung liegt in _dispatch_movement."""
        # Akuter Schuss gewinnt (Dodge/EVADING/DODGE_JUMP wie in COMBAT).
        if self._handle_threat(now):
            return
        pid = self.target_player
        info = self.players.get(pid) if pid is not None else None
        # Ausgang: Ziel weg/tot/pausiert oder Gegner jetzt SB/SW → Deckung wertlos.
        if info is None or not info.alive or info.paused or info.flag in ("SB", "SW"):
            self._cover_hold_exit(now)
            return
        # P4-TAC-05: Gegner leergeschossen + Fenster groß genug + eigener Schuss bereit → raus und
        # angreifen, statt den Notausgang-Timeout abzuwarten.
        if (self._enemy_slots_empty(info, now)
                and self._enemy_next_slot_ready_in(info, now) >= COVER_BREAKOUT_MIN_WINDOW_S
                and self._next_slot_ready(now)):
            self._cover_hold_exit(now)
            return
        # Ausgang: Timeout (Fallback-Notausgang, falls das Slot-Fenster nie groß genug wird).
        if now >= self._cover_hold_until:
            self._cover_hold_exit(now)
            return
        ep = self._get_enemy_pos(pid)
        if ep is None:
            self._cover_hold_exit(now)
            return
        dist = math.hypot(ep[0] - self.pos_x, ep[1] - self.pos_y)
        # Ausgang: außer Reichweite, Deckung ungültig (Gegner umrundet die Box), oder Gegner so nah,
        # dass COMBAT wieder Abstand halten soll (Point-Blank lohnt kein Verstecken).
        if (dist > self._shot_range * COVER_MAX_RANGE_FACTOR
                or dist < self._effective_optimal_range() * COVER_CLOSE_EXIT_FRAC
                or not self._covered_from(pid, now)):
            self._cover_hold_exit(now)
            return
        # Gelegentlich kurz herausschauen — aber nur, wenn der Gegner überhaupt schießen kann (sonst
        # provoziert der Peek nichts; das Slot-leer-Fenster bricht ohnehin aktiv aus, s. o.).
        if (self._cover_peek_phase == 0 and not self._enemy_slots_empty(info, now)
                and random.random() < COVER_PEEK_CHANCE):
            self._cover_peek_phase = 1
            self._cover_peek_until = now + COVER_PEEK_OUT_S
        # Abpraller-Ziel in Deckung frisch halten (breiter Sweep) — ohne LoS würde der Cache sonst
        # verfallen und das Fire-Gate ließe keinen Indirekt-Schuss zu (s. COVER_HOLD-Dispatch).
        if not self._has_los_to_enemy(pid):
            self._find_ricochet_aim_angle(pid, None, RICO_AIM_MAX_COVER)

    def _execute_combat_move(self, dt: float, half: float, now: float = 0.0) -> None:
        """COMBAT 60Hz: dreht auf vorhergesagte Zielposition, fährt distanzbasiert."""
        if self.target_player is None:
            return
        ep = self._get_enemy_pos(self.target_player)
        if ep is None:
            return
        info = self.players.get(self.target_player)
        dx = ep[0] - self.pos_x
        dy = ep[1] - self.pos_y
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return
        # Aktives Unstick-Manöver (Stall-Watchdog) steuert den Tick allein
        if self._stall_maneuver_tick(dt, half, now):
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
        nav_goal   = self._nav_goal
        nav_goal_z = self._nav_goal_z
        enemy_z    = info.pos[2] if info is not None else 0.0
        # Gegner per Sprung unerreichbar (zu hoch)? → ggf. Eskalations-Zyklus (s.u.)
        _max_jump_h = self._effective_jump_height()
        _too_high   = (enemy_z - self.pos_z) >= _max_jump_h
        _stuck_active = (self._unreach_target is not None
                         and self._unreach_target == self.target_player)
        if not _too_high and _stuck_active:
            self._unreach_target = None     # Gegner nicht mehr zu hoch → Episode beenden
            _stuck_active = False
        # Direktziel-Modus? (innerhalb Optimaldistanz + Gegner nicht deutlich höher). Wird hier
        # VOR dem Replan bestimmt, damit im Direktmodus gar keine (ungenutzte) A*-Planung läuft.
        _opt = self._effective_optimal_range()
        _enemy_z = info.pos[2] if info is not None else self.pos_z
        _los_clear   = self._has_los_to_enemy(self.target_player)
        # F5: Server-Basiswert (_shotRange) statt Konstante; ohne Flaggen-Multiplikatoren,
        # sonst würde z.B. Laser den Direktmodus kartenweit aktivieren (nie mehr A*-Nav).
        _dist_thresh = self._shot_range if _los_clear else _opt * 1.1
        # Gegner nicht deutlich höher als der Bot (Bot-Oberkante über Gegner-Fußpunkt)?
        _not_below_enemy = self.pos_z + self._tank_height > _enemy_z
        # C: Bot unter erhöhtem Gegner mit verfügbarem Indirekt-Schuss → stehen & aufs Tor zielen
        # statt hochzuklettern, zeitlich gedeckelt (kein ewiges Festkleben). sobald die
        # Navigation durch Tore routet, wird genau diese Bedingung zur traverse-vs-shoot-Entscheidung.
        _hold_indirect = self._update_indirect_hold(
            now, (not _not_below_enemy) and self._indirect_shot_available(self.target_player))
        _skip_nav    = (_not_below_enemy and dist < _dist_thresh) or _hold_indirect
        # Proaktive Wand-Vorausschau: würde der Direktmodus den Bot ohne Sicht steil in eine Wand
        # fahren (z.B. dünne Trennwand auf einer Plattform), nicht stur rammen, sondern A* um die
        # Wand routen lassen. Nur den Nahkampf-Direktmodus aufbrechen, nicht den Indirekt-Halt.
        if _skip_nav and not _hold_indirect and not _los_clear and self._steep_wall_ahead(
                math.atan2(aim_y - self.pos_y, aim_x - self.pos_x),
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
        nav_path = self._nav_path
        if nav_path and not _skip_nav:
            self._navigate_wp(dt, half, reverse=self._should_reverse_to_wp())
            return
        # Gegner per Sprung unerreichbar und (noch) kein A*-Pfad → nicht blind die Wand rammen,
        # sondern Eskalations-Zyklus (Re-Target → Direktmodus → Reposition → Replan).
        if _too_high and not nav_path and not _hold_indirect:   # während Halt nicht eskalieren
            if self._combat_escalate(dt, half, ep, aim_x, aim_y, enemy_z):
                return                                       # Reposition wird abgefahren
            nav_path = self._nav_path        # Replan evtl. erfolgreich
            if nav_path:
                self._navigate_wp(dt, half, reverse=self._should_reverse_to_wp())
                return
            # sonst: Direktmodus (Prio 2) fällt unten durch
        # Stall-Watchdog: Direktsteuerung ohne Sicht (auch der Fall "kein A*-Pfad, Gegner höher aber
        # springbar") darf nicht dauerhaft auf der Stelle stehen (Spiegel-Stall an dünner Wand).
        if self._stall_watchdog(now, _los_clear, _hold_indirect, _stuck_active):
            return
        # Direktziel-Modus: distanzbasiert (Rückwärts / langsam / voll)
        target_az = math.atan2(aim_y - self.pos_y, aim_x - self.pos_x)
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
        self._turn_toward_ramped(target_az, dt)   # P4-MOV-02a: angulare Beschleunigungsklemme
        if dist < _opt - COMBAT_DIST_DEADZONE:
            speed = -self._tank_speed * 0.5
            _nav = self._nav_graph
            if _nav is not None and self._get_floor_z() > 0.5:
                _nx = self.pos_x + math.cos(self.azimuth) * speed * dt
                _ny = self.pos_y + math.sin(self.azimuth) * speed * dt
                if _nav.get_floor_z(_nx, _ny, self.pos_z + 0.1) < self._get_floor_z() - 1.0:
                    speed = 0.0
        elif dist > _opt * 2:
            speed = self._tank_speed
        elif dist > _opt + COMBAT_DIST_DEADZONE:
            speed = self._tank_speed * 0.15
        else:
            speed = 0.0   # Deadzone um die Optimaldistanz: kein Zittern bei minimalen Distanzänderungen
        if speed > 0 and abs(self.ang_vel) > self._tank_turn_rate * 0.5:
            _nav = self._nav_graph
            if _nav is not None and self._get_floor_z() > 0.5:
                _nx = self.pos_x + math.cos(self.azimuth) * speed * dt
                _ny = self.pos_y + math.sin(self.azimuth) * speed * dt
                if _nav.get_floor_z(_nx, _ny, self.pos_z + 0.1) < self._get_floor_z() - 1.0:
                    speed = 0.0
        speed = self._ramp_linear_speed(speed, dt)   # P4-MOV-02a: lineare Beschleunigungsklemme
        speed, self.ang_vel = self._apply_movement_caps(speed, self.ang_vel)
        self.vel_x = math.cos(self.azimuth) * speed
        self.vel_y = math.sin(self.azimuth) * speed
        self._apply_bounds(dt, half)
        if self._debug_log_path and self._is_inside_obstacle():
            _t = time.monotonic()
            if _t - self._debug_obstacle_logged > 1.0:
                self._debug_obstacle_logged = _t
                logger.debug("[%s] Pfad: Kollision bei (%.0f,%.0f) Ziel:%s",
                             self.callsign, self.pos_x, self.pos_y, self.target_pos)

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
            # B7: abgelaufene Einträge beim Einfügen mit ausfiltern (seltener Pfad, verhindert
            # Leak über lange Sessions — analog _evade_cleared_shots/_nav_jump_cooldowns).
            self._combat_avoid = {k: v for k, v in self._combat_avoid.items() if v > now}
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
                if self._nav_path:
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
            if self._nav_path:
                self._unreach_target = None
                return False
            self._unreach_phase = 0                   # immer noch nichts → Zyklus neu
        return False

    def _pick_reposition_point(self, ep) -> tuple:
        """Reposition-Zielpunkt ~UNREACH_REPOS_RADIUS entfernt, Winkel grob in Gegnerrichtung
        (±120°), auf Weltgrenzen geklemmt. Frischer A*-Start, ohne den Kampf zu verlassen."""
        base_az = math.atan2(ep[1] - self.pos_y, ep[0] - self.pos_x)
        ang = base_az + random.uniform(-math.radians(120), math.radians(120))
        rx = self.pos_x + math.cos(ang) * UNREACH_REPOS_RADIUS
        ry = self.pos_y + math.sin(ang) * UNREACH_REPOS_RADIUS
        _m = self.world_half - 5.0
        return (max(-_m, min(_m, rx)), max(-_m, min(_m, ry)))

    # ── COMBAT-Stall-Watchdog ────────────────────────────────────────────────
    # Zwei gleich starke Bots frieren bei Optimaldistanz ohne Sicht/Ricochet ein (Spiegel-Stall,
    # v.a. an dünnen Trennwänden). Der Watchdog erkennt Null-Bewegung im Direktmodus und löst mit
    # einem RANDOMISIERTEN Manöver auf — Randomisierung, damit sich zwei Bots nicht spiegeln.

    def _stall_watchdog(self, now: float, los_clear: bool, hold_indirect: bool,
                        stuck_active: bool) -> bool:
        """Armiert ein ZUFÄLLIGES Fenster (10–15 s), misst Netto-Bewegung und startet bei < 2 u
        ein zufälliges Unstick-Manöver. True = Manöver gestartet (Caller soll return)."""
        if los_clear or hold_indirect or stuck_active:
            self._stall_check_at = None          # legitime Halte-Situation → entschärfen
            return False
        if self._stall_check_at is None:         # frisch armieren
            self._stall_check_at = now + random.uniform(COMBAT_STALL_WIN_MIN, COMBAT_STALL_WIN_MAX)
            self._stall_anchor = [self.pos_x, self.pos_y]
            return False
        if now < self._stall_check_at:
            return False
        moved = math.hypot(self.pos_x - self._stall_anchor[0], self.pos_y - self._stall_anchor[1])
        if moved >= COMBAT_STALL_MIN_DIST:       # Fortschritt → frisches Fenster, kein Stall
            self._stall_check_at = now + random.uniform(COMBAT_STALL_WIN_MIN, COMBAT_STALL_WIN_MAX)
            self._stall_anchor = [self.pos_x, self.pos_y]
            return False
        self._stall_check_at = None              # Stall → Manöver (re-armiert nach dessen Ende)
        return self._stall_fire(now)

    def _stall_fire(self, now: float) -> bool:
        """Startet zufällig REV oder PATH; scheitert PATH, wird REV genommen (REV gelingt immer)."""
        first = random.choice(("REV", "PATH"))
        for mode in (first, "PATH" if first == "REV" else "REV"):
            if mode == "REV" and self._stall_try_rev(now):
                return True
            if mode == "PATH" and self._stall_try_path(now):
                return True
        return False

    def _stall_try_rev(self, now: float) -> bool:
        # KEIN Klippen-Check: von der Plattform zu fallen löst den Stall (erwünscht).
        self._stall_mode = "REV"
        self._stall_rev_dist = random.uniform(COMBAT_STALL_REV_MIN, COMBAT_STALL_REV_MAX)
        self._stall_rev_start = [self.pos_x, self.pos_y]
        self._stall_until = now + COMBAT_STALL_TIMEOUT
        return True

    def _stall_try_path(self, now: float) -> bool:
        ang = random.uniform(0.0, 2.0 * math.pi)
        r = NAV_CELL_SIZE * random.uniform(COMBAT_STALL_WP_MIN, COMBAT_STALL_WP_MAX)
        _m = self.world_half - 5.0
        rx = max(-_m, min(_m, self.pos_x + math.cos(ang) * r))
        ry = max(-_m, min(_m, self.pos_y + math.sin(ang) * r))
        self._plan_path(rx, ry, cap_wps=COMBAT_STALL_WP_MAX)
        if not self._nav_path:
            return False                          # kein Pfad → Fallback auf REV (s. _stall_fire)
        self._stall_mode = "PATH"
        self._stall_until = now + COMBAT_STALL_TIMEOUT
        return True

    def _stall_maneuver_tick(self, dt: float, half: float, now: float) -> bool:
        """Fährt ein laufendes Unstick-Manöver ab. True = Tick vollständig behandelt.
        Nach Abschluss/Timeout: Modus beenden → normaler COMBAT-Fluss, Watchdog re-armiert neu."""
        if self._stall_mode is None:
            return False
        if self._stall_mode == "REV":
            driven = math.hypot(self.pos_x - self._stall_rev_start[0],
                                self.pos_y - self._stall_rev_start[1])
            if driven >= self._stall_rev_dist or now >= self._stall_until:
                self._stall_end()
                return False
            # KEIN Klippen-Guard: Absturz ist hier gewollt (bricht den Spiegel-Stall).
            # Rückwärts-Cap 0,5× der FLAGGEN-effektiven Speed (wie _navigate_wp): BZFlag klemmt
            # fracOfMaxSpeed clientseitig auf -0,5×maxSpeed (LocalPlayer.cxx) — maxSpeed enthält
            # den Flaggen-Modifikator (V/TH/A/BU). Volle Speed triggert den Server-Speedcheck.
            speed = -self._effective_tank_speed() * 0.5
            # P4-MOV-02a: ang_vel auf 0 und Rückwärts-Speed mit Beschleunigungsklemme (ohne -a wie bisher)
            self._ramp_azimuth_step(0.0, dt, self._tank_turn_rate)
            speed = self._ramp_linear_speed(speed, dt)
            speed, self.ang_vel = self._apply_movement_caps(speed, self.ang_vel)
            self.vel_x = math.cos(self.azimuth) * speed
            self.vel_y = math.sin(self.azimuth) * speed
            self._apply_bounds(dt, half)
            return True
        # PATH-Modus
        if not self._nav_path or now >= self._stall_until:
            self._stall_end()
            return False
        self._navigate_wp(dt, half)
        return True

    def _stall_end(self) -> None:
        """Manöver beenden: Pfad/Ziel verwerfen (frischer Replan Richtung Gegner)."""
        self._stall_mode = None
        self._stall_check_at = None
        self._nav_path = []
        self._nav_goal = None

    def _setup_dodge(self, threat, now: float, time_to_impact: float,
                     dodge_dir: float, orig_diff: Optional[float] = None) -> None:
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
        if self._debug_log_dodge:
            logger.debug("[%s] Ausweichen: Vor Shot [%d/%d] für %.2fs",
                         self.callsign, threat.shooter_id, threat.shot_id, dodge_duration)
