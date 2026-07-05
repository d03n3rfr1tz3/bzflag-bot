"""Lokale Physik-Simulation des eigenen Tanks: Integration, Boden-/Hindernis-Kollision (W4, FABLE-PLAN Teil 3)."""

import math
import random

from bot.constants import *  # noqa: F401,F403
from bot.models import AIState
from bzflag.intersect import rect_rect_overlap


class PhysicsMixin:
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

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
            # Tank als orientierte Box (physische Maße, ohne Schussradius): HL zählt, damit die
            # lange Achse nicht durch dünne Wände ragt (Kern des Bugfixes). Gate exakt (OBB-OBB,
            # wie bzfs testRectRect) → kein Über-Blocken an schrägen Durchfahrten.
            HL = self._tank_length / 2.0
            HW = self._effective_half_width()
            if not rect_rect_overlap(obs.cx, obs.cy, obs.angle, obs.half_w, obs.half_d,
                                     nx, ny, self.azimuth, HL, HW):
                continue
            cos_a = obs.cos_a
            sin_a = obs.sin_a
            dx, dy = nx - obs.cx, ny - obs.cy
            lnx = dx * cos_a + dy * sin_a
            lny = -dx * sin_a + dy * cos_a
            lvx = vx * cos_a + vy * sin_a
            lvy = -vx * sin_a + vy * cos_a
            # Glide-Achse ISOTROP wählen (Trennachse aus der Obstacle-Geometrie, nicht aus der
            # Tank-Orientierung): kleineres Overlap = Trennachse. Beim OBB-Gate steht das Zentrum an
            # der dünnen Achse ggf. schon außerhalb (Overlap negativ) — die Min-Auswahl trifft dann
            # weiterhin korrekt die Wand-Normale. NUR die Achsen-Wahl ist isotrop; das Eindringen
            # verhindert der OBB-Gate oben (Tank-Länge zählt).
            hw = obs.half_w + HW
            hd = obs.half_d + HW
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
