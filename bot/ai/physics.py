"""Lokale Physik-Simulation des eigenen Tanks: Integration, Boden-/Hindernis-Kollision (W4, FABLE-PLAN Teil 3)."""

import math
import random

from bot.constants import (
    ON_TOP_EPS,
)
from bot.models import AIState
from bzflag.intersect import rect_rect_overlap


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class PhysicsMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

    def _run_physics(self, dt: float, now: float) -> None:
        """Grundlegende Spielphysik: Schwerkraft (off-ground) + Bounce-Flag (BY).
        Läuft jeden Tick unabhängig vom AI-State."""
        # BY: Auto-Bounce alle 0.2s
        if (self.own_flag == "BY" and not self._jumping
                and self.pos_z <= 0.1 and now >= self._bounce_next):
            self.vel_z = random.uniform(0.25, 1.0) * self._jump_velocity
            # BY-01: Horizontalrichtung aus aktuellem Azimuth — nicht aus altem vel[0/1]
            h_speed = math.hypot(self.vel_x, self.vel_y)
            if h_speed < 1.0:
                h_speed = self._tank_speed * 0.5
            self.vel_x = math.cos(self.azimuth) * h_speed
            self.vel_y = math.sin(self.azimuth) * h_speed
            self._jumping = True
            self._jump_ang_vel = 0.0  # BZFlag: keine Steuerung in der Luft
            self._bounce_next = now + 0.2
            self._transition_to(AIState.JUMPING)

        # Schwerkraft für nicht-springende Tanks über dem Boden.
        # _get_floor_z liefert den flaggen-korrekten Boden: 0.0 Weltboden / ≥0 Gebäudedach;
        # BU sinkt nur AM BODEN auf BURROW_DEPTH (−1.32u), nicht auf Dächern; OO → immer 0.0.
        # Schwelle 1e-6 statt 0: verhindert Dead-Zone durch Floating-Point-Artefakte.
        _floor_z = self._get_floor_z()
        if not self._jumping and self.pos_z > _floor_z + 1e-6:
            self.vel_z = max(self.vel_z + self._effective_gravity() * dt, -self._tank_speed)
            self.pos_z = max(self.pos_z + self.vel_z * dt, _floor_z)
            if self.pos_z <= _floor_z + 1e-6:
                self.pos_z = _floor_z
                self.vel_z = 0.0

    def _is_landed(self) -> bool:
        """True wenn Bot auf dem Boden (oder einer Gebäude-Oberfläche) steht.
        Nur beim Abstieg (vel[2] <= 0.1) prüfen — kein Früh-Landen beim Aufstieg."""
        if self.vel_z > 0.1:
            return False
        return self.pos_z <= self._get_floor_z() + 0.1

    def _get_floor_z(self) -> float:
        """Höchste Bodenfläche unterhalb des Bots; 0.0 wenn kein NavGraph.

        Pixel-on-Auflage: der Tank bleibt getragen, bis seine Mitte ~eine Tank-Halbbreite über
        die Kante hinaus ist (overhang). So fällt der Bot nicht schon, wenn die Mitte die Kante
        überquert — entscheidend für Sprung-Anläufe am Plattformrand.

        Flaggen-Boden zentral hier: OO phast durch Gebäude → landet/fällt immer auf den Weltboden
        (z=0). BU gräbt sich NUR am Boden ein (auf einem Dach trägt das Dach, also nur dort sinkt
        der Bot auf BURROW_DEPTH)."""
        if not self.alive:
            return 0.0
        if self.own_flag == "OO":
            return 0.0
        # P4a: Per-Tick-Memo (3–5 identische Aufrufe pro 60-Hz-Tick). Der Key
        # enthält Position+Flagge → Aufrufe NACH einer pos-Mutation im selben
        # Tick treffen einen neuen Key; Ergebnis bleibt verhaltensidentisch.
        memo = self._tick_memo
        key = ("floor", self.pos_x, self.pos_y, self.pos_z, self.own_flag)
        if memo is not None:
            cached = memo.get(key)
            if cached is not None:
                return cached
        nav = self._nav_graph
        floor = 0.0 if nav is None else nav.get_floor_z(
            self.pos_x, self.pos_y, self.pos_z, overhang=self._effective_half_width())
        if self.own_flag == "BU" and floor <= 0.0:
            floor = self._burrow_depth
        if memo is not None:
            memo[key] = floor
        return floor

    def _is_inside_obstacle(self, include_oo: bool = False) -> bool:
        """True wenn Bot physisch innerhalb eines Gebäudes steht (echte Geometrie, kein A*-Margin)."""
        if self._can_drive_through_obstacles() and not include_oo:
            return False
        world_map = self._world_map
        if world_map is None:
            return False
        px, py, pz = self.pos_x, self.pos_y, self.pos_z
        for obs in world_map.boxes:
            if obs.drive_through:
                continue
            tank_top = pz + self._tank_height
            # pz >= Box-Oberkante (− ON_TOP_EPS): der Bot steht bündig AUF der Box (nicht innen) —
            # z.B. ein FAHRENDER Teleporter-Austritt landet exakt auf der Mauer-Oberkante (z=Box-Top).
            # Mit strikt `>` würde das als "innen" gewertet und der Teleport revertiert (Bot steckt fest).
            if tank_top <= obs.bottom_z or pz >= obs.bottom_z + obs.height - ON_TOP_EPS:
                continue
            # OBB-Overlap (einheitliches Form-Modell): „irgendein Teil des Tanks steckt im Gebäude".
            # Mit der OBB-Wandkollision (W3) ist das im Normalbetrieb nie wahr (Berühren = strikt
            # nicht-innen) → nur nach Teleport/Spawn/Durchdringung, genau das wollen die Aufrufer
            # (Teleport-Exit-Revert, OO-Gate). Physische Halbmaße, linearer Scan (nicht hot).
            if rect_rect_overlap(obs.cx, obs.cy, obs.angle, obs.half_w, obs.half_d,
                                 px, py, self.azimuth,
                                 self._tank_length / 2.0, self._effective_half_width()):
                return True
        return False

    def _crossing_wall(self) -> bool:
        """True wenn der echte Client hier das PS_CROSSING-Bit setzen würde.

        Spiegelt die Client-Verzweigung (LocalPlayer.cxx:678-694): entweder ein OO-Tank
        durchquert eine Gebäudewand ODER (jede Flagge) straddlet eine Teleporter-
        Querungsebene. Für das Statusbit ist es ein ODER — die Client-Priorität (OO-Zweig
        vor Teleporter) spielt für den einen Bit-Wert keine Rolle."""
        if self.own_flag == "OO" and self._oo_crossing_wall():
            return True
        return self._crossing_teleporter()

    def _oo_crossing_wall(self) -> bool:
        """True wenn der OO-Tank gerade eine solide Box durchquert (→ PS_CROSSING).

        Grid-Broad-Phase wie _apply_obstacle_bounds (nicht der bewusst lineare
        _is_inside_obstacle) — läuft nur bei OO und mit 30-Hz-Sende-Kadenz.
        Approximation: True bei jedem Overlap (Straddle wie vollständig-innen); der
        Client-`isCrossing` nur beim Straddeln. Für den Effekt praktisch identisch."""
        world_map = self._world_map
        if world_map is None:
            return False
        px, py, pz = self.pos_x, self.pos_y, self.pos_z
        _solid = world_map.boxes + self._tele_solid_boxes
        _nav = self._nav_graph
        _grid = _nav._solid_grid if _nav is not None else None
        cands = _grid.query_point(px, py) if _grid is not None else _solid
        tank_top = pz + self._tank_height
        half_len = self._tank_length / 2.0
        half_w = self._effective_half_width()
        for obs in cands:
            if obs.drive_through:
                continue
            # z-Gate wie _is_inside_obstacle: auf/unter der Box zählt nicht als "durch die Wand".
            if tank_top <= obs.bottom_z or pz >= obs.bottom_z + obs.height - ON_TOP_EPS:
                continue
            if rect_rect_overlap(obs.cx, obs.cy, obs.angle, obs.half_w, obs.half_d,
                                 px, py, self.azimuth, half_len, half_w):
                return True
        return False

    def _crossing_teleporter(self) -> bool:
        """True wenn die Tank-OBB eine Teleporter-Querungsebene straddlet (→ PS_CROSSING).

        Für JEDE Flagge (wie der Client). Port von Teleporter::isCrossing: OBB gegen das
        Querungsfeld (getWidth × getBreadth-border) plus z-Gate. Linear über die wenigen
        Teleporter der Karte, Early-Out ohne Teleporter."""
        world_map = self._world_map
        if world_map is None or not world_map.teleporters:
            return False
        px, py, pz = self.pos_x, self.pos_y, self.pos_z
        half_len = self._tank_length / 2.0
        half_w = self._effective_half_width()
        for tele in world_map.teleporters:
            if pz < tele.bottom_z or pz > tele.bottom_z + tele.height - tele.border:
                continue
            if rect_rect_overlap(tele.cx, tele.cy, tele.angle,
                                 tele.half_w, max(0.0, tele.half_d - tele.border),
                                 px, py, self.azimuth, half_len, half_w):
                return True
        return False

    def _apply_obstacle_bounds(self, dt: float) -> None:
        """Wall-Sliding + Decken-Kollision: korrigiert self.vel_*/pos_* bei Gebäude-Kollision (60 Hz)."""
        if self._can_drive_through_obstacles():
            return
        world_map = self._world_map
        if world_map is None:
            return
        pz = self.pos_z
        px, py = self.pos_x, self.pos_y
        vx, vy = self.vel_x, self.vel_y
        # P3-NAV-02: Teleporter-Posts + Crossbar als solide Boxen mitprüfen (Decken-Kollision von
        # unten gegen den Crossbar, Wall-Slide an den Posts). Das Querungsfeld bleibt frei.
        _solid = world_map.boxes + self._tele_solid_boxes
        # Broad-Phase: bei vorhandenem NavGraph nur die Boxen der Bot-Zelle statt linear über alle.
        # nav._obs = non-drive_through world_map.boxes + dieselben Teleporter-Solidboxen → deckungs-
        # gleicher Kandidatensatz wie _solid nach dem drive_through-Skip. Ohne nav: linearer Fallback.
        _nav = self._nav_graph
        _grid = _nav._solid_grid if _nav is not None else None
        # ── Decken-Kollision: Bot-Kopf stößt von unten an Plattform-Boden ──────
        bot_top = pz + self._tank_height
        ceil_cands = _grid.query_point(px, py) if _grid is not None else _solid
        for obs in ceil_cands:
            if obs.drive_through:
                continue
            # pz < obs.bottom_z: Bot ist unterhalb — nicht bereits darin (OO-Flagge etc.)
            if not (pz < obs.bottom_z <= bot_top):
                continue
            # Horizontaler Footprint als OBB (einheitliches Form-Modell): die Tank-NASE unter einem
            # Plattform-Rand löst den Kopf-Anstoß korrekt aus statt erst die Mitte.
            if rect_rect_overlap(obs.cx, obs.cy, obs.angle, obs.half_w, obs.half_d,
                                 px, py, self.azimuth,
                                 self._tank_length / 2.0, self._effective_half_width()):
                self.vel_z = 0.0
                _floor_z = self._burrow_depth if self.own_flag == "BU" else self._get_floor_z()
                self.pos_z = max(obs.bottom_z - self._tank_height, _floor_z)
                pz = self.pos_z
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
            # Oberkante ≤ _maxBumpHeight über den Ketten → Tank fährt direkt drüber (Server-Var;
            # ersetzt das alte 0.5-Literal — deckungsgleich mit bzfs' Bump-Regel).
            if tank_top <= obs.bottom_z or pz >= obs.bottom_z + obs.height - self._max_bump_height:
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
        self.vel_x = vx
        self.vel_y = vy

    def _apply_bounds(self, dt: float, half: float) -> None:
        """Begrenzt Bot-Position auf Weltgrenzen; prallt von Wänden ab."""
        self._apply_obstacle_bounds(dt)
        nx = self.pos_x + self.vel_x * dt
        ny = self.pos_y + self.vel_y * dt
        bounced = False
        if not (-half < nx < half):
            self.vel_x = -self.vel_x
            nx = max(-half + 1, min(half - 1, nx))
            bounced = True
        if not (-half < ny < half):
            self.vel_y = -self.vel_y
            ny = max(-half + 1, min(half - 1, ny))
            bounced = True
        if bounced:
            # B4: Replan in den 10-Hz-KI-Tick verlagert (states._dispatch_movement) statt hier
            # synchron zu planen — _apply_bounds läuft im 60-Hz-Physik-Pfad aus JEDEM State
            # (auch EVADING/committed; in COMBAT würde ein sofortiger A*-Lauf hier _nav_goal
            # überschreiben). Nur noch ein Flag setzen, kein A*-Lauf im Physik-Pfad.
            self._bounce_replan = True
        self.pos_x = nx
        self.pos_y = ny
