"""Navigation: A*-Planung (sync + async Worker), Wegpunkt-Abfahren, NAV_JUMP-Vorbereitung und Teleporter-Querung (W4, FABLE-PLAN Teil 3)."""

import math
import threading
import time
import logging
from typing import Tuple

from bzflag.nav_graph import JUMP_EDGE_TOL
from bzflag.shot_physics import (ray_teleporter_crossing, teleport_through)
from bot.constants import (
    NAV_CELL_SIZE,
    EARLY_ADVANCE_FLOOR_STEP,
    TELEPORT_TIME,
    WP_TIMEOUT_BASE,
    WP_TIMEOUT_SCALE,
    NAV_JUMP_Z_TOL,
    NAV_TELE_ENGAGE_DIST,
    NAV_ASYNC_TRIGGER_MS,
    NAV_ASYNC_MAX_EXPANSIONS,
    NAV_ASYNC_MAX_MS,
    NAV_ASYNC_RESYNC_TOL,
)
from bot.util import _angle_diff, _wrap
from bot.models import AIState

logger = logging.getLogger("bzbot")


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class NavigationMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

    def _wp_reach_radius(self) -> float:
        """Engerer Radius direkt vor NAV_JUMP-Anlauf-WP (aufwärts), sonst NAV_CELL_SIZE."""
        nav_path = self._nav_path
        if len(nav_path) >= 2 and nav_path[1][2] - self._get_floor_z() > 1.5:
            return NAV_CELL_SIZE
        return NAV_CELL_SIZE * 1.25

    def _corridor_clear(self, ex: float, ey: float) -> bool:
        """P4-MOV-01: True, wenn die Gerade (pos_x,pos_y)→(ex,ey) frei von soliden
        (nicht-drive_through) Boxen ist. Spiegelt die reaktive Wall-Slide-Broadphase
        (_apply_obstacle_bounds): _solid_grid.query_segment + drive_through-Filter + Z-Band +
        Slab-Test des Segments gegen die um die Tank-Halbbreite aufgeblähte Box.
        Ohne NavGraph → True (unverändertes Verhalten)."""
        nav = self._nav_graph
        if nav is None:
            return True
        px = self.pos_x
        py = self.pos_y
        pz = self.pos_z
        tank_top = pz + self._tank_height
        hw_pad = self._effective_half_width()
        grid = nav._solid_grid
        cands = grid.query_segment(px, py, ex, ey) if grid is not None else nav._obs
        ddx = ex - px
        ddy = ey - py
        for obs in cands:
            if obs.drive_through:
                continue
            # Z-Band: Box über dem Kopf oder Oberkante ≤ _maxBumpHeight über den Ketten
            # (überfahrbar) → kein Blocker. MUSS dieselbe Schwelle wie die Wall-Slide-Kollision
            # nutzen, sonst gälten Stufen im Korridor als frei, die die Physik real blockt
            # (Early-Advance in eine Sackgasse bis zum WP-Timeout).
            if tank_top <= obs.bottom_z or pz >= obs.bottom_z + obs.height - self._max_bump_height:
                continue
            # Slab-Test Segment vs. rotierte Box, Footprint um hw_pad aufgebläht (Logik wie
            # nav_graph._segment_crosses_thin_obs, hier inline mit Inflation; Track 5/mypyc:
            # zwei explizite Achsen-Blöcke statt Tupel-Loop).
            cos_a = obs.cos_a
            sin_a = obs.sin_a
            ox =  (px - obs.cx) * cos_a + (py - obs.cy) * sin_a
            oy = -(px - obs.cx) * sin_a + (py - obs.cy) * cos_a
            dx =  ddx * cos_a + ddy * sin_a
            dy = -ddx * sin_a + ddy * cos_a
            half_w = obs.half_w + hw_pad
            half_d = obs.half_d + hw_pad
            t_min = 0.0
            t_max = 1.0
            crosses = True
            # Achse w (lokal, Box-Breite)
            if abs(dx) < 1e-9:
                if abs(ox) > half_w:
                    crosses = False
            else:
                ta = (-half_w - ox) / dx
                tb = ( half_w - ox) / dx
                if ta > tb:
                    ta, tb = tb, ta
                if ta > t_min:
                    t_min = ta
                if tb < t_max:
                    t_max = tb
                if t_min > t_max:
                    crosses = False
            # Achse d (lokal, Box-Tiefe)
            if crosses:
                if abs(dy) < 1e-9:
                    if abs(oy) > half_d:
                        crosses = False
                else:
                    ta = (-half_d - oy) / dy
                    tb = ( half_d - oy) / dy
                    if ta > tb:
                        ta, tb = tb, ta
                    if ta > t_min:
                        t_min = ta
                    if tb < t_max:
                        t_max = tb
                    if t_min > t_max:
                        crosses = False
            if crosses:
                return False
        return True

    def _corridor_no_dropoff(self, ex: float, ey: float, ref_z: float) -> bool:
        """P4-MOV-01: True, wenn der Boden entlang (pos_x,pos_y)→(ex,ey) nirgends mehr als
        _maxBumpHeight unter ref_z abfällt. Verhindert, dass der Early-Advance den Bot über
        eine Plattformkante abkürzen und abstürzen lässt (z.B. HIX nahe der Diagonalwand).
        Abtastung mit Pixel-on-Overhang (wie _get_floor_z). Ohne NavGraph → True."""
        nav = self._nav_graph
        if nav is None:
            return True
        sx = self.pos_x
        sy = self.pos_y
        ddx = ex - sx
        ddy = ey - sy
        dist = math.hypot(ddx, ddy)
        if dist < 1e-6:
            return True
        overhang = self._effective_half_width()
        min_floor = ref_z - self._max_bump_height
        probe_z = ref_z + 1.0
        n = int(dist / EARLY_ADVANCE_FLOOR_STEP)
        if n < 1:
            n = 1
        # n innere Stützstellen + Endpunkt: i = 1..n+1, t = i/(n+1) (i=n+1 → t=1.0 = Ziel-WP)
        i = 1
        while i <= n + 1:
            t = i / (n + 1)
            if nav.get_floor_z(sx + ddx * t, sy + ddy * t, probe_z, overhang) < min_floor:
                return False
            i += 1
        return True

    def _nav_jump_geometry_ok(self, target_wp) -> bool:
        """True wenn Sprung geometrisch möglich ist (Höhe + Reichweite), ohne Azimuth.

        Front-Catch (Pixel-on): der Tank landet, sobald seine Front die Zielkante erreicht —
        die effektiv zu überbrückende Strecke ist daher hdist - JUMP_EDGE_TOL (deckungsgleich
        mit der Sprungkanten-Planung in nav_graph._vertical_neighbors)."""
        dx, dy = target_wp[0] - self.pos_x, target_wp[1] - self.pos_y
        dz = target_wp[2] - self.pos_z
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
        dx, dy = target_wp[0] - self.pos_x, target_wp[1] - self.pos_y
        hdist  = math.hypot(dx, dy)
        dz     = target_wp[2] - self.pos_z
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
        """Gemeinsamer WP-Navigations-Kern: Timeout, Advance, Early-Advance (P4-MOV-01), Drehen,
        Geschwindigkeit. Gibt True zurück wenn der Tick vollständig behandelt wurde."""
        if self.target_pos is None:
            return False
        nav_path = self._nav_path
        now = time.monotonic()
        if (nav_path
                and self._wp_start_time is not None
                and now - self._wp_start_time > self._wp_timeout):
            self._wp_fail_count += 1
            _wp_z = nav_path[0][2]
            _d2d  = math.hypot(self.target_pos[0] - self.pos_x,
                               self.target_pos[1] - self.pos_y)
            if self._debug_log_path:
                logger.debug(
                    "[%s] Pfad: WP-Timeout #%d Bot=(%.1f,%.1f z=%.2f floor=%.2f)"
                    " WP=(%.1f,%.1f z=%.2f) dist2d=%.2f dz=%.2f r=%.1f",
                    self.callsign, self._wp_fail_count,
                    self.pos_x, self.pos_y, self.pos_z, self._get_floor_z(),
                    self.target_pos[0], self.target_pos[1], _wp_z,
                    _d2d, self.pos_z - _wp_z, self._wp_reach_radius())
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
        if not reverse and self._should_early_advance():
            # P4-MOV-01: Ecke früher schneiden — kein return, der Tick fällt zum Steering durch
            # und lenkt sofort auf den neuen WP (pop(0) mutiert nav_path in-place).
            self._advance_path()
        if nav_path:
            wp_x, wp_y, wp_z = nav_path[0][0], nav_path[0][1], nav_path[0][2]
        else:
            wp_x, wp_y = self.target_pos
            wp_z = self.pos_z
        aim_x, aim_y = wp_x, wp_y
        r = self._wp_reach_radius()
        dist_to_wp = math.hypot(wp_x - self.pos_x, wp_y - self.pos_y)
        dx, dy = aim_x - self.pos_x, aim_y - self.pos_y
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
            if self._debug_log_path and dist_to_wp < r * 3.0:
                _t = time.monotonic()
                if _t - self._debug_wp_near_t > 0.5:
                    self._debug_wp_near_t = _t
                    logger.debug(
                        "[%s] Pfad: Nahe WP (%.1f,%.1f,%.1f) Bot=(%.1f,%.1f,%.1f)"
                        " dist=%.2f r=%.1f spd=%.1f diff=%.0f° az=%.0f°",
                        self.callsign, wp_x, wp_y, wp_z,
                        self.pos_x, self.pos_y, self.pos_z,
                        dist_to_wp, r, speed,
                        math.degrees(diff), math.degrees(self.azimuth))
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
        world_map = self._world_map
        if world_map is None or not world_map.teleporters:
            return
        # PhantomZone togglet zoned statt zu teleportieren (P4-FLG-03) → hier nicht teleportieren.
        if self.own_flag == "PZ":
            return
        if now < self._teleporting_until:
            return  # Re-Trigger-Sperre (mirror isTeleporting())
        ox, oy, oz = old
        dx, dy, dz = self.pos_x - ox, self.pos_y - oy, self.pos_z - oz
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
        link_map = self._link_map or {}
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
            self.pos_x, self.pos_y, self.pos_z,
            self.vel_x, self.vel_y, self.vel_z,
            src_tele, best_face, dst_tele, exit_face)
        # Exit-Validierung: liegt der Austritt in einem Hindernis → Teleport verwerfen (revert wie
        # LocalPlayer: Position zurück, Horizontal-Velocity 0, vel[2] behalten).
        self.pos_x, self.pos_y, self.pos_z = npx, npy, npz
        if self._is_inside_obstacle():
            self.pos_x, self.pos_y, self.pos_z = ox, oy, oz
            self.vel_x = self.vel_y = 0.0
            return
        self.vel_x, self.vel_y, self.vel_z = nvx, nvy, nvz
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
        if self._debug_log_tele:
            _planned = bool(self._nav_path)
            logger.debug(
                "[%s] Tele-Detail: (%.1f,%.1f,%.1f)→(%.1f,%.1f,%.1f) Δz=%+.1f vz=%.1f "
                "Δaz=%+.1f° %s%s",
                self.callsign, ox, oy, oz, npx, npy, npz, npz - oz, self.vel_z,
                math.degrees(radians2 - radians1),
                "geplant" if _planned else "ungeplant",
                "" if self.vel_z == 0.0 else " (im Sprung/Fall)")

    def _resync_path_after_teleport(self, ox: float, oy: float,
                                    nx: float, ny: float) -> None:
        """Nach dem Teleport: Wegpunkte verwerfen, die nun „hinter" uns liegen (näher an der
        Eintritts- als an der Austrittsseite) — verhindert Zurückfahren zum Eingang. Führte ein
        geplanter Pfad durch den Teleporter, bleibt der Austritts-WP Ziel (kein Replan); war der
        Teleport ungewollt, leert sich der Pfad → der nächste Boden-Tick plant neu (deferred)."""
        nav_path = self._nav_path
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
        tp = self.target_pos
        if tp is not None and (math.hypot(tp[0] - ox, tp[1] - oy)
                               < math.hypot(tp[0] - nx, tp[1] - ny)):
            self._nav_goal = None
            self._wp_start_time = None
            self.target_pos = None

    def _plan_path(self, goal_x: float, goal_y: float,
                   goal_z: float | None = None, *, cap_wps: int | None = None) -> None:
        """Plant A*-Pfad zu (goal_x, goal_y); fällt auf Direktpfad zurück.

        cap_wps deckelt die WP-Anzahl (COMBAT: 8). Dauerte die Haupt-Thread-Suche länger als
        NAV_ASYNC_TRIGGER_MS, wird zusätzlich eine Hintergrund-Vollsuche angestoßen (P4-INF-01)."""
        nav = self._nav_graph
        self._nav_goal = (goal_x, goal_y)
        # Jeder Plan-Request invalidiert ältere (in-flight/fertige) Async-Ergebnisse.
        self._plan_gen += 1

        # Kartenrand-Escape: nahe am Rand kann A* nicht planen (kein gültiger Start).
        # Nur die Achse(n) halbieren, die zu nah am Rand sind.
        _EDGE_MARGIN = 15.0
        _half = self.world_half
        if abs(self.pos_x) > _half - _EDGE_MARGIN or abs(self.pos_y) > _half - _EDGE_MARGIN:
            ex = (((_half / 2.0) * (1.0 if self.pos_x > 0.0 else -1.0))
                  if abs(self.pos_x) > _half - _EDGE_MARGIN else self.pos_x)
            ey = (((_half / 2.0) * (1.0 if self.pos_y > 0.0 else -1.0))
                  if abs(self.pos_y) > _half - _EDGE_MARGIN else self.pos_y)
            self._nav_path  = []
            self.target_pos = (ex, ey)
            self._wp_start_time = time.monotonic()
            self._wp_fail_count = 0
            self._wp_timeout    = (WP_TIMEOUT_BASE
                                   + math.hypot(ex - self.pos_x, ey - self.pos_y)
                                   * WP_TIMEOUT_SCALE)
            return

        if nav is None or self._can_drive_through_obstacles():
            if self._debug_log_path:
                logger.debug("[%s] Pfad: Direktpfad (%s) → (%.0f,%.0f)",
                             self.callsign,
                             "Flagge" if self._can_drive_through_obstacles() else "kein NavGraph",
                             goal_x, goal_y)
            self.target_pos = (goal_x, goal_y)
            self._nav_path  = []
            return
        blocked = {k for k, v in self._nav_jump_cooldowns.items() if v > time.monotonic()}
        sx, sy, sz = self.pos_x, self.pos_y, self.pos_z
        # Reisegeschwindigkeit einmal snapshotten (Flaggen-Boost → weitere Sprünge planbar,
        # deckungsgleich zum reaktiven Executor). Plain-Value → reentrant an Sync- und Async-Plan.
        ts = self._travel_tank_speed()
        t0 = time.perf_counter()
        path = nav.plan_path(sx, sy, sz, goal_x, goal_y,
                             blocked_jump_wps=blocked, goal_z=goal_z,
                             label="Schnellplan", partial_level=logging.DEBUG,
                             tank_speed=ts)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if self._debug_log_path:
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
                                + math.hypot(path[0][0] - self.pos_x,
                                             path[0][1] - self.pos_y)
                                * WP_TIMEOUT_SCALE)
        else:
            self._nav_path  = []
            self.target_pos = (goal_x, goal_y)
            self._wp_start_time = time.monotonic()
            self._wp_fail_count = 0
            self._wp_timeout = (WP_TIMEOUT_BASE
                                + math.hypot(goal_x - self.pos_x,
                                             goal_y - self.pos_y)
                                * WP_TIMEOUT_SCALE)

    def _submit_async_plan(self, sx: float, sy: float, sz: float,
                           goal_x: float, goal_y: float, goal_z: float | None,
                           blocked: set, cap_wps: int | None, gen: int,
                           tank_speed: float | None = None) -> None:
        """Startet (höchstens einen) Hintergrund-Thread mit großen A*-Limits (P4-INF-01).

        Der NavGraph ist reentrant; der Worker bekommt nur Plain-Value-Snapshots und liest nie
        self.*-Mutables. Läuft bereits eine Suche, wird sie nur bei Ziel-Wechsel kooperativ
        abgebrochen (sonst weiterlaufen lassen) und KEINE zweite gestartet."""
        nav = self._nav_graph
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
        px, py, pz = self.pos_x, self.pos_y, self.pos_z
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
        if self._debug_log_path:
            logger.debug("[%s] Pfad: Async-Vollsuche übernommen (%d WPs, %d Prefix gedroppt, d=%.1f) "
                         "→ (%.0f,%.0f)", self.callsign, len(path), dropped, route_d, gx, gy)
        self._apply_planned_path(path, gx, gy, cap_wps)

    def _advance_path(self, *, timed_out: bool = False) -> None:
        """Rückt im Pfad vor; löst NAV_JUMP aus wenn nächster WP auf anderer Etage."""
        if not timed_out:
            self._wp_fail_count = 0
        nav_path = self._nav_path
        if nav_path:
            _reached_wp   = nav_path[0]
            _reached_dist = math.hypot(_reached_wp[0] - self.pos_x,
                                       _reached_wp[1] - self.pos_y)
            nav_path.pop(0)
        else:
            _reached_wp, _reached_dist = None, 0.0
        if nav_path:
            wp = nav_path[0]
            if self._debug_log_path:
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
            _nav = self._nav_graph
            _is_tele_exit = (_nav is not None
                             and (round(wp[0], 1), round(wp[1], 1)) in _nav._tele_exit_wps)
            # OO ausgeschlossen: man kann mit OO nicht auf einem Dach landen (fällt zurück durch) →
            # ein Sprung dorthin wäre sinnlos und würde sich endlos wiederholen; WP wird am Boden phasend angefahren.
            if wp[2] - floor_z > 1.5 and self.own_flag not in ("NJ", "BU", "OO") and not _is_tele_exit:
                if self._nav_jump_feasible(wp):
                    self._wp_start_time = None
                    self._initiate_nav_jump(wp)
                    return
                if not self._nav_jump_geometry_ok(wp):
                    wp_key = (round(wp[0]), round(wp[1]), wp[2])
                    _now = time.monotonic()
                    # B7: abgelaufene Einträge beim Einfügen mit ausfiltern (Muster wie in
                    # states.py._tick_nav_jump_align — verhindert Leak über lange Sessions).
                    self._nav_jump_cooldowns = {k: v for k, v in self._nav_jump_cooldowns.items() if v > _now}
                    self._nav_jump_cooldowns[wp_key] = _now + 30.0
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
                az_to_wp = math.atan2(wp[1] - self.pos_y, wp[0] - self.pos_x)
                self._nav_jump_align_wp = wp
                self._nav_jump_align_start = time.monotonic()
                self._nav_jump_align_return_state = self._ai_state
                self._transition_to(AIState.NAV_JUMP_ALIGN)
                return
            # Tor-Austritts-WP erreicht (Eingang gerade abgehakt) → letztes Stück direkt in die
            # Tor-Mitte fahren (NAV_TELE), statt am mittenseitigen Exit-WP davor zu stoppen.
            if _is_tele_exit:
                center = _nav._tele_cross_centers.get(
                    (round(wp[0], 1), round(wp[1], 1)))
                if center is not None and self._try_engage_nav_tele(center):
                    return
            self.target_pos = (wp[0], wp[1])
            self._wp_start_time = time.monotonic()
            self._wp_timeout = (WP_TIMEOUT_BASE
                                + math.hypot(wp[0] - self.pos_x,
                                             wp[1] - self.pos_y)
                                * WP_TIMEOUT_SCALE)
        else:
            if self._debug_log_path:
                logger.debug("[%s] Pfad: Fertig → Neuziel", self.callsign)
            self._wp_start_time = None
            if self._ai_state == AIState.COMBAT and self.target_player is not None:
                # Im COMBAT kein _new_target() — _nav_goal auf aktuelle Enemy-XY setzen
                # damit _execute_combat_move nicht sofort replant (dist(ep, ep_now) ≈ 0).
                _ep_now = self._get_enemy_pos(self.target_player)
                self.target_pos = None
                self._nav_path  = []
                self._nav_goal  = (_ep_now[0], _ep_now[1]) if _ep_now is not None else None
            elif self._ai_state == AIState.IDLE:
                # IDLE: Pfad abgefahren → parken statt Wandern (CPU sparen). Der Bot
                # bleibt stehen, bis _has_presence() den Wechsel nach SEEKING auslöst.
                self.target_pos = None
                self._nav_path  = []
                self._nav_goal  = None
                self.vel_x  = 0.0
                self.vel_y  = 0.0
                self.ang_vel = 0.0
            else:
                self._new_target()

    def _nav_jump_land_spin(self, wp, v0: float, g_abs: float) -> float:
        """Im Sprung fixe Drehrate, sodass der Bot ausgerichtet landet: auf den Gegner, wenn der
        Landepunkt auf Gegner-Höhe liegt, sonst auf den nächsten Wegpunkt. Fallback 0.0 (WG:
        bestehende Rate). Auf _tank_turn_rate gedeckelt — BZFlag: am Absprung fixiert, in der Luft
        unveränderlich. vel[0/1] müssen vor dem Aufruf gesetzt sein."""
        fallback = self._jump_ang_vel if self.own_flag == "WG" else 0.0
        disc = v0 * v0 - 2.0 * g_abs * (wp[2] - self.pos_z)
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
            nav_path = self._nav_path          # Invariante: nav_path[0] == wp
            if len(nav_path) >= 2:
                target = (nav_path[1][0], nav_path[1][1])
        if target is None:
            return fallback
        lx = self.pos_x + self.vel_x * t_flight              # voraussichtl. Landepunkt (ballistisch)
        ly = self.pos_y + self.vel_y * t_flight
        delta = _angle_diff(math.atan2(target[1] - ly, target[0] - lx), self.azimuth)
        return math.copysign(min(abs(delta / t_flight), self._tank_turn_rate), delta)

    def _initiate_nav_jump(self, wp) -> None:
        """Startet Navigationssprung zu Wegpunkt wp = (wx, wy, layer_z)."""
        self._nav_jump_target_z = wp[2]
        v0 = self._effective_jump_velocity()
        g_abs = abs(self._effective_gravity())
        self.vel_z = self._jump_launch_vz(self.vel_z)
        dz       = wp[2] - self.pos_z
        hdist    = math.hypot(wp[0] - self.pos_x, wp[1] - self.pos_y)
        az_to_wp = math.atan2(wp[1] - self.pos_y, wp[0] - self.pos_x)
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
        self.vel_x       = math.cos(self.azimuth) * needed_hspeed
        self.vel_y       = math.sin(self.azimuth) * needed_hspeed
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
            _owner = self._nav_jump_align_return_state
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
        dist = math.hypot(cx - self.pos_x, cy - self.pos_y)
        key = (round(cx), round(cy))
        if dist > NAV_TELE_ENGAGE_DIST or self._nav_tele_cooldowns.get(key, 0) > now:
            if self._debug_log_tele:
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
                    self.callsign, cx, cy, self.pos_x, self.pos_y, dist)
        return True
