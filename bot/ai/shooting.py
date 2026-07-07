"""Schiessen: Zielpunkt-/Ricochet-Berechnung, Feuer-Gates, alle _maybe_shoot_*-Zweige und _send_shot (W4, FABLE-PLAN Teil 3)."""

import math
import random
import struct
import time
import logging
from typing import Optional, Tuple

from bzflag.protocol import MsgShotBegin
from bzflag.shot_physics import (simulate_shot_path, can_ricochet, _segment_hits_obb_3d)
from bot.constants import (
    THIEF_AD_RANGE,
    SHOOT_INTERVAL_RANDOM_MAX,
    MIN_BURST_INTERVAL,
    GM_BURST_INTERVAL,
    OPTIMAL_RANGE,
    RICO_AIM_CACHE_TTL,
    RICO_AIM_MAX,
    TELE_AIM_Z_TOL,
    INDIRECT_HOLD_S,
    HIT_RADIUS,
)
from bot.util import _angle_diff
from bot.models import AIState

logger = logging.getLogger("bzbot")


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class ShootingMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

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
                                self._landing_second_shot_at = None
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
        _fb = self._own_flag_bytes()
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
        ecz = enemy.pos[2] + self._tank_height * 0.5

        hw   = self._tank_width  / 2 + self._shot_radius
        hlen = self._tank_length / 2 + self._shot_radius
        hh   = self._tank_height / 2 + self._shot_radius

        flag_bytes = self._own_flag_bytes()
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
            ez = (info.pos[2] if info is not None else self.pos[2]) + self._tank_height * 0.5
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
            _fb = self._own_flag_bytes()
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
                and abs(info.pos[2] - self.pos[2]) > self._tank_height * 0.7):
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
            _fb = self._own_flag_bytes()
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
                and abs(info.pos[2] - self.pos[2]) > self._tank_height * 0.7):
            return
        self._send_shot(now, self.azimuth)
        self._set_next_shoot_after_fire(now)

    def _fire_gate_rad(self, dist: float) -> float:
        """Maximal erlaubte Abweichung zwischen Ziel- und Blickwinkel beim Feuern,
        distanzabhängig (F8): linear von 25° (Zieldistanz ≤10u) auf 5° (≥100u)
        verengt — max_dev_deg = 25 − 20 · clamp((dist − 10) / 90, 0, 1).
        Im Nahkampf ist Streuung okay (Ziel füllt den Winkel), auf Distanz ist
        ein 20°-Fehlschuss praktisch garantiert (Slot-Verschwendung). Gilt
        einheitlich für Direkt- wie Indirekt-Schüsse."""
        f = max(0.0, min(1.0, (dist - 10.0) / 90.0))
        return math.radians(25.0 - 20.0 * f)

    def _maybe_shoot_sb(
        self, now: float, ep, info, dx: float, dy: float, dist: float
    ) -> None:
        """Super Bullet: schießt durch Wände — kein LoS-Check."""
        aim_xy = self._compute_aim_point(ep, info, dx, dy, dist, now)
        if aim_xy is None:
            return
        aim_angle = math.atan2(aim_xy[1] - self.pos[1], aim_xy[0] - self.pos[0])
        if abs(_angle_diff(aim_angle, self.azimuth)) > self._fire_gate_rad(dist):
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
            _fb = self._own_flag_bytes()
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
        if abs(_angle_diff(aim_angle, self.azimuth)) > self._fire_gate_rad(dist):
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
            + self._own_flag_bytes()
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
