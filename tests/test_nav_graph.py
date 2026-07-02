"""
Tests für bzflag/nav_graph.py.

Testet NavGraph-Aufbau, A*-Pfadsuche und get_floor_z
anhand synthetischer WorldMaps.
"""

import logging
import math
import threading

import pytest

from bzflag.world_map import BoxObstacle, WorldMap
from bzflag.nav_graph import (
    NavGraph, FloorLayer, CELL_SIZE, TANK_MARGIN, TANK_HEIGHT, THIN_WALL_MARGIN,
    JUMP_EDGE_TOL, get_nav_graph,
    _mark_blocked, _nearest_walkable, _smooth_path, _clip_to_footprint, _h,
    _segment_crosses_thin_obs, _obstacle_blocks_layer, _margin_for,
    _insert_jump_runups, _runup_crosses_thin_wall,
    _point_in_rotated_box, ObstacleGrid,
)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_world(boxes=None, world_half=100.0):
    """Erstellt eine minimale WorldMap für Tests."""
    return WorldMap(
        boxes=boxes or [],
        teleporters=[],
        links=[],
        world_half=world_half,
        world_hash="test",
    )


def _make_box(cx, cy, bz, hw, hd, height, angle=0.0) -> BoxObstacle:
    return BoxObstacle(cx=cx, cy=cy, bottom_z=bz,
                       angle=angle, half_w=hw, half_d=hd, height=height)


# ---------------------------------------------------------------------------
# FloorLayer
# ---------------------------------------------------------------------------

class TestFloorLayer:
    def test_world_to_cell_center(self):
        half = 25.0
        n = max(1, int(2.0 * half / CELL_SIZE))
        layer = FloorLayer(z=0.0, cx=0.0, cy=0.0, half_w=half, half_d=half,
                           n_x=n, n_y=n, walkable=[[True]*n for _ in range(n)],
                           source_obstacle=None)
        ix, iy = layer.world_to_cell(0.0, 0.0)
        expected = int(half / CELL_SIZE)
        assert ix == expected and iy == expected

    def test_cell_to_world_round_trip(self):
        n = 8
        layer = FloorLayer(z=0.0, cx=0.0, cy=0.0, half_w=20.0, half_d=20.0,
                           n_x=n, n_y=n, walkable=[[True]*n for _ in range(n)],
                           source_obstacle=None)
        for ix in range(n):
            for iy in range(n):
                wx, wy = layer.cell_to_world(ix, iy)
                ix2, iy2 = layer.world_to_cell(wx, wy)
                assert ix2 == ix and iy2 == iy

    def test_contains_xy(self):
        n = 4
        layer = FloorLayer(z=0.0, cx=50.0, cy=50.0, half_w=10.0, half_d=10.0,
                           n_x=n, n_y=n, walkable=[[True]*n for _ in range(n)],
                           source_obstacle=None)
        assert layer.contains_xy(50.0, 50.0)
        assert layer.contains_xy(55.0, 55.0)
        assert not layer.contains_xy(65.0, 50.0)
        assert not layer.contains_xy(50.0, 65.0)


# ---------------------------------------------------------------------------
# NavGraph-Aufbau
# ---------------------------------------------------------------------------

class TestNavGraphBuild:
    def test_empty_world_has_ground_layer(self):
        wm = _make_world(world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        assert len(ng.layers) == 1
        assert ng.layers[0].z == pytest.approx(0.0)
        assert ng.layers[0].source_obstacle is None

    def test_ground_layer_size(self):
        wm = _make_world(world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        g = ng.layers[0]
        expected_n = int(100.0 / CELL_SIZE)
        assert g.n_x == expected_n
        assert g.n_y == expected_n

    def test_building_creates_roof_layer(self):
        # 30×30 building, height=10 → roof layer bei z=10
        box = _make_box(0.0, 0.0, 0.0, 15.0, 15.0, 10.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm, max_jump_h=18.4)
        assert len(ng.layers) == 2
        roof = ng.layers[1]
        assert roof.z == pytest.approx(10.0)
        assert roof.source_obstacle is box

    def test_too_small_building_no_roof_layer(self):
        # 8×8 building → walkable area = 8-5=3 < CELL_SIZE=5 → kein Layer
        box = _make_box(0.0, 0.0, 0.0, 4.0, 4.0, 5.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm, max_jump_h=18.4)
        assert len(ng.layers) == 1  # nur Boden, kein Dach

    def test_too_high_building_no_roof_layer(self):
        # Höhe > MAX_ROOF_H (55u) → kein Roof-Layer
        box = _make_box(0.0, 0.0, 0.0, 20.0, 20.0, 60.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm, max_jump_h=18.4)
        assert len(ng.layers) == 1

    def test_building_blocks_ground_cells(self):
        # Großes Gebäude in der Mitte → Zellen um Gebäude blockiert
        box = _make_box(0.0, 0.0, 0.0, 10.0, 10.0, 8.0)
        wm = _make_world(boxes=[box], world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        ground = ng.layers[0]
        # Mittelpunkt des Gebäudes soll blockiert sein
        ix, iy = ground.world_to_cell(0.0, 0.0)
        assert not ground.walkable[iy][ix]
        # Fernpunkte sollen begehbar sein
        ix2, iy2 = ground.world_to_cell(40.0, 40.0)
        ix2, iy2 = ground.clamp_cell(ix2, iy2)
        assert ground.walkable[iy2][ix2]

    def test_drive_through_building_not_blocked(self):
        box = BoxObstacle(cx=0.0, cy=0.0, bottom_z=0.0, angle=0.0,
                          half_w=10.0, half_d=10.0, height=8.0,
                          drive_through=True)
        wm = _make_world(boxes=[box], world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        ground = ng.layers[0]
        # drive-through → Zellen bleiben walkable
        ix, iy = ground.world_to_cell(0.0, 0.0)
        assert ground.walkable[iy][ix]


# ---------------------------------------------------------------------------
# get_floor_z
# ---------------------------------------------------------------------------

class TestGetFloorZ:
    def test_floor_z_at_ground_level(self):
        wm = _make_world()
        ng = NavGraph(wm)
        assert ng.get_floor_z(0.0, 0.0, 0.0) == pytest.approx(0.0, abs=0.01)

    def test_floor_z_above_building(self):
        box = _make_box(0.0, 0.0, 0.0, 10.0, 10.0, 8.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm)
        # Bot steht auf dem Dach bei z=8
        fz = ng.get_floor_z(0.0, 0.0, 8.1)
        assert fz == pytest.approx(8.0, abs=0.1)

    def test_floor_z_outside_building_is_zero(self):
        box = _make_box(0.0, 0.0, 0.0, 5.0, 5.0, 8.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm)
        # Weit außerhalb des Gebäudes
        fz = ng.get_floor_z(50.0, 50.0, 0.0)
        assert fz == pytest.approx(0.0, abs=0.01)

    def test_floor_z_at_ground_near_building(self):
        box = _make_box(20.0, 0.0, 0.0, 5.0, 5.0, 8.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm)
        # Am Boden neben Gebäude → floor_z = 0
        fz = ng.get_floor_z(0.0, 0.0, 0.0)
        assert fz == pytest.approx(0.0, abs=0.01)

    def test_floor_z_above_z_plus_2_ignored(self):
        # Dach bei z=10; Bot bei z=5 → Dach liegt 5 über Bot → ignoriert
        box = _make_box(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm)
        fz = ng.get_floor_z(0.0, 0.0, 5.0)
        assert fz == pytest.approx(0.0, abs=0.01)  # Bot IST innerhalb des Gebäudes


# ---------------------------------------------------------------------------
# ObstacleGrid / get_floor_z: Broad-Phase-Äquivalenz zur Brute-Force-Referenz
# ---------------------------------------------------------------------------

def _floor_z_bruteforce(ng, x, y, z, overhang):
    """Referenz: lineare Schleife über alle _obs (= get_floor_z vor der Grid-Optimierung)."""
    floor_z = 0.0
    for obs in ng._obs:
        roof_z = obs.roof_z
        if roof_z > z + 2.0:
            continue
        if _point_in_rotated_box(obs, x, y, margin=overhang):
            if roof_z > floor_z:
                floor_z = roof_z
    return floor_z


class TestGetFloorZGridEquivalence:
    """Das Grid ist nur eine gepolsterte Übermenge → get_floor_z muss bit-identisch zur linearen
    Brute-Force-Referenz sein, auch an Box-Kanten und bei rotierten/überlappenden Dächern."""

    def _world(self):
        boxes = [
            _make_box(0.0, 0.0, 0.0, 10.0, 10.0, 8.0),      # niedriges Dach
            _make_box(5.0, 5.0, 0.0, 6.0, 6.0, 15.0),       # überlappt, höheres Dach
            _make_box(-40.0, 20.0, 0.0, 8.0, 3.0, 12.0, angle=math.radians(30.0)),   # rotiert
            _make_box(40.0, -40.0, 0.0, 5.0, 20.0, 10.0, angle=math.radians(45.0)),  # rotiert, länglich
            _make_box(60.0, 60.0, 0.0, 4.0, 4.0, 6.0),      # isoliert
        ]
        return _make_world(boxes=boxes, world_half=100.0)

    def test_matches_bruteforce_over_raster(self):
        ng = NavGraph(self._world())
        coords = [c * 2.5 for c in range(-40, 41)]   # -100..100 in 2.5u-Schritten
        for overhang in (0.0, 1.4):
            for z in (0.0, 5.0, 8.1, 15.1):
                for x in coords:
                    for y in coords:
                        got = ng.get_floor_z(x, y, z, overhang=overhang)
                        want = _floor_z_bruteforce(ng, x, y, z, overhang)
                        assert got == want, (x, y, z, overhang, got, want)

    def test_query_point_no_false_negatives(self):
        """Jede Box, in deren gepolstertem Footprint ein Punkt liegt, muss Grid-Kandidat sein."""
        ng = NavGraph(self._world())
        grid = ng._solid_grid
        for x in (c * 5.0 for c in range(-20, 21)):
            for y in (c * 5.0 for c in range(-20, 21)):
                cands = set(id(b) for b in grid.query_point(x, y))
                for obs in ng._obs:
                    if _point_in_rotated_box(obs, x, y, margin=1.4):
                        assert id(obs) in cands, (x, y, obs)


# ---------------------------------------------------------------------------
# ObstacleGrid.query_ray: LoS-Broad-Phase-Äquivalenz zum linearen Slab-Test
# ---------------------------------------------------------------------------

def _segment_blocked_ref(boxes, ox, oy, oz, ex, ey, ez):
    """Referenz: 3D-Slab-Test über eine gegebene Box-Liste (= _segment_clear-Narrow-Phase, invertiert:
    True wenn IRGENDEINE Box das Segment schneidet)."""
    dx = ex - ox; dy = ey - oy; dz = ez - oz
    for box in boxes:
        cos_a = box.cos_a; sin_a = box.sin_a
        rx = ox - box.cx; ry = oy - box.cy
        lox =  rx * cos_a + ry * sin_a
        loy = -rx * sin_a + ry * cos_a
        ldx =  dx * cos_a + dy * sin_a
        ldy = -dx * sin_a + dy * cos_a
        t_min = 0.0; t_max = 1.0; hit = True
        for o_v, d_v, lo_v, hi_v in (
            (lox, ldx, -box.half_w, box.half_w),
            (loy, ldy, -box.half_d, box.half_d),
            (oz,  dz,   box.bottom_z, box.bottom_z + box.height),
        ):
            if abs(d_v) < 1e-9:
                if o_v < lo_v or o_v > hi_v:
                    hit = False; break
            else:
                t1 = (lo_v - o_v) / d_v; t2 = (hi_v - o_v) / d_v
                t_min = max(t_min, min(t1, t2))
                t_max = min(t_max, max(t1, t2))
        if hit and t_min <= t_max:
            return True
    return False


class TestLosRayGridEquivalence:
    """query_ray ist nur eine gepolsterte Übermenge entlang des Strahls → der Slab-Test über die
    Grid-Kandidaten muss dasselbe Blockiert/Frei liefern wie über ALLE _los_obs, auch bei rotierten
    Boxen und langen Diagonal-Strahlen."""

    def _nav(self):
        boxes = [
            _make_box(0.0, 0.0, 0.0, 8.0, 8.0, 10.0),
            _make_box(30.0, 10.0, 0.0, 5.0, 5.0, 6.0),
            _make_box(-25.0, -30.0, 0.0, 10.0, 3.0, 12.0, angle=math.radians(40.0)),
            _make_box(50.0, -20.0, 0.0, 4.0, 25.0, 10.0, angle=math.radians(20.0)),
            _make_box(-60.0, 40.0, 5.0, 6.0, 6.0, 8.0),   # schwebend (bottom_z>0)
            _make_box(70.0, 70.0, 0.0, 5.0, 5.0, 15.0),
        ]
        return NavGraph(_make_world(boxes=boxes, world_half=120.0))

    def test_segment_clear_matches_bruteforce_random(self):
        import random
        rnd = random.Random(20260701)
        ng = self._nav()
        grid = ng._los_grid
        for _ in range(4000):
            ox = rnd.uniform(-120, 120); oy = rnd.uniform(-120, 120); oz = rnd.uniform(0.0, 14.0)
            ex = rnd.uniform(-120, 120); ey = rnd.uniform(-120, 120); ez = rnd.uniform(0.0, 14.0)
            cands = grid.query_ray(ox, oy, ex, ey)
            got = _segment_blocked_ref(cands, ox, oy, oz, ex, ey, ez)
            want = _segment_blocked_ref(ng._los_obs, ox, oy, oz, ex, ey, ez)
            assert got == want, (ox, oy, oz, ex, ey, ez, got, want)

    def test_query_ray_degenerate(self):
        """Achsenparallele, Null-Längen- und Einzelzell-Strahlen dürfen keinen Kandidaten verschlucken."""
        ng = self._nav()
        grid = ng._los_grid
        # Null-Länge in einer Box → deren Zelle liefert die Box
        assert any(b.cx == 0.0 for b in grid.query_ray(0.0, 0.0, 0.0, 0.0))
        # exakt horizontal quer durch die Mittel-Box
        want = _segment_blocked_ref(ng._los_obs, -120.0, 0.0, 5.0, 120.0, 0.0, 5.0)
        got = _segment_blocked_ref(grid.query_ray(-120.0, 0.0, 120.0, 0.0), -120.0, 0.0, 5.0, 120.0, 0.0, 5.0)
        assert got == want is True
        # exakt vertikal (in y)
        w2 = _segment_blocked_ref(ng._los_obs, 30.0, -120.0, 3.0, 30.0, 120.0, 3.0)
        g2 = _segment_blocked_ref(grid.query_ray(30.0, -120.0, 30.0, 120.0), 30.0, -120.0, 3.0, 30.0, 120.0, 3.0)
        assert g2 == w2


# ---------------------------------------------------------------------------
# A*-Pfadsuche
# ---------------------------------------------------------------------------

class TestPlanPath:
    def test_empty_world_straight_path(self):
        wm = _make_world(world_half=100.0)
        ng = NavGraph(wm)
        path = ng.plan_path(0.0, 0.0, 0.0, 50.0, 50.0)
        assert len(path) >= 1
        last = path[-1]
        # Letzter WP nahe Ziel
        assert math.hypot(last[0] - 50.0, last[1] - 50.0) < CELL_SIZE * 2

    def test_obstacle_between_start_and_goal(self):
        # Großes Gebäude direkt zwischen Start (−40,0) und Ziel (40,0)
        box = _make_box(0.0, 0.0, 0.0, 8.0, 8.0, 10.0)
        wm = _make_world(boxes=[box], world_half=80.0)
        ng = NavGraph(wm)
        path = ng.plan_path(-40.0, 0.0, 0.0, 40.0, 0.0)
        assert len(path) >= 1
        # Keiner der Wegpunkte darf innerhalb des Gebäudes liegen
        for wx, wy, _ in path:
            # Mit TANK_MARGIN 5u → Bot darf nicht näher als 5u an Gebäudekante
            dist_to_center = math.hypot(wx - 0.0, wy - 0.0)
            assert dist_to_center > box.half_w - 0.5, \
                f"Wegpunkt ({wx:.1f},{wy:.1f}) innerhalb Gebäude"

    def test_path_reaches_goal(self):
        wm = _make_world(world_half=100.0)
        ng = NavGraph(wm)
        gx, gy = 70.0, -30.0
        path = ng.plan_path(-70.0, 30.0, 0.0, gx, gy)
        assert len(path) > 0
        last = path[-1]
        assert math.hypot(last[0] - gx, last[1] - gy) < CELL_SIZE * 3

    def test_start_equals_goal_returns_empty_or_trivial(self):
        wm = _make_world()
        ng = NavGraph(wm)
        path = ng.plan_path(0.0, 0.0, 0.0, 0.0, 0.0)
        # Entweder leer (Start=Ziel) oder trivial kurz
        assert len(path) <= 2

    def test_path_all_same_z_on_ground(self):
        wm = _make_world(world_half=80.0)
        ng = NavGraph(wm)
        path = ng.plan_path(-40.0, -40.0, 0.0, 40.0, 40.0)
        for _, _, z in path:
            assert z == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# Roof-Zugang (Sprung-Kanten via A*)
# ---------------------------------------------------------------------------

class TestRoofAccess:
    def test_roof_layer_created_for_large_building(self):
        # 40×40 Gebäude → Roof-Layer vorhanden (half_w=20 - TANK_MARGIN=5 = 15 > CELL_SIZE)
        box = _make_box(0.0, 0.0, 0.0, 20.0, 20.0, 10.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm)
        assert any(l.source_obstacle is box for l in ng.layers)

    def test_roof_layer_z_correct(self):
        box = _make_box(0.0, 0.0, 0.0, 20.0, 20.0, 12.5)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm)
        roof = next(l for l in ng.layers if l.source_obstacle is box)
        assert roof.z == pytest.approx(12.5, abs=0.01)


# ---------------------------------------------------------------------------
# Shared Cache
# ---------------------------------------------------------------------------

class TestSharedCache:
    def test_same_hash_returns_same_instance(self):
        wm1 = _make_world(world_half=100.0)
        wm2 = _make_world(world_half=100.0)  # gleicher Hash "test"
        ng1 = get_nav_graph(wm1, max_jump_h=18.4)
        ng2 = get_nav_graph(wm2, max_jump_h=18.4)
        assert ng1 is ng2  # identische Instanz

    def test_different_hash_returns_different_instance(self):
        wm1 = WorldMap(boxes=[], teleporters=[], links=[],
                       world_half=100.0, world_hash="hash_a")
        wm2 = WorldMap(boxes=[], teleporters=[], links=[],
                       world_half=100.0, world_hash="hash_b")
        ng1 = get_nav_graph(wm1)
        ng2 = get_nav_graph(wm2)
        assert ng1 is not ng2


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_smooth_path_removes_collinear(self):
        # 5 Punkte auf gerader Linie → nur Start und Ende bleiben
        pts = [(float(i * 5), 0.0, 0.0) for i in range(5)]
        smoothed = _smooth_path(pts)
        assert len(smoothed) == 2

    def test_smooth_path_keeps_turns(self):
        # Richtungswechsel: (0,0)→(5,0) geht nach Osten, dann (5,0)→(5,5)→(5,10) nach Norden
        # Die 90°-Kurve liegt bei (5,0): Ost→Nord → muss behalten werden
        pts = [
            (0.0, 0.0, 0.0),
            (5.0, 0.0, 0.0),   # 90°-Kurve hier (Ost→Nord)
            (5.0, 5.0, 0.0),
            (5.0, 10.0, 0.0),
        ]
        smoothed = _smooth_path(pts)
        # (5,0) ist die Kurve → muss im Ergebnis sein
        assert any(abs(p[0] - 5.0) < 0.1 and abs(p[1] - 0.0) < 0.1
                   for p in smoothed)

    def test_smooth_path_keeps_z_changes(self):
        pts = [(0.0, 0.0, 0.0), (5.0, 0.0, 0.0), (10.0, 0.0, 10.0)]
        smoothed = _smooth_path(pts)
        # Z-Wechsel-Punkt soll erhalten bleiben
        assert len(smoothed) == 3

    def test_smooth_path_thin_blocked_preserves_waypoint(self):
        # Fast kollineare Punkte: Winkeländerung ≈ 2° < 15° → B wird normalerweise entfernt
        pts = [(0.0, 0.0, 0.0), (5.0, 0.1, 0.0), (10.0, 0.0, 0.0)]
        # Ohne thin_blocked: nur Start und Ende
        assert len(_smooth_path(pts)) == 2
        # Mit thin_blocked (A→C verboten): B muss erhalten bleiben
        blocked = {(0.0, 0.0, 10.0, 0.0, 0.0)}
        smoothed = _smooth_path(pts, blocked)
        assert len(smoothed) == 3
        assert any(abs(p[0] - 5.0) < 0.1 for p in smoothed)

    def test_smooth_path_thin_blocked_noop_when_pair_not_blocked(self):
        # Gleiche Punkte, aber thin_blocked enthält dieses Paar nicht
        pts = [(0.0, 0.0, 0.0), (5.0, 0.1, 0.0), (10.0, 0.0, 0.0)]
        blocked = {(99.0, 99.0, 88.0, 88.0, 0.0)}
        smoothed = _smooth_path(pts, blocked)
        # B wird trotzdem entfernt — kein fälschlicher Guard
        assert len(smoothed) == 2


# ---------------------------------------------------------------------------
# NAV-03: max_hdist = tank_speed * t_land (nicht v0 * t_land)
# ---------------------------------------------------------------------------

class TestNAV03JumpPhysics:
    """NAV-03: Sprung-Kanten benutzen horizontale Fahrgeschwindigkeit (25 u/s),
    nicht die vertikale Sprung-Anfangsgeschwindigkeit (19 u/s)."""

    def test_tank_speed_attribute(self):
        """_tank_speed=25 und _v0=19 sind unabhängig korrekt initialisiert."""
        wm = _make_world(world_half=100.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        assert ng._tank_speed == pytest.approx(25.0)
        assert ng._v0         == pytest.approx(19.0)

    def test_jump_edge_accepted_with_tank_speed_rejected_with_v0(self):
        """Gebäude (hw=30, hd=15, h=10) bei cx=0; Bot bei wx=-95 (aabb_dist=65u).
        Abstiegsformel: t_desc=(19+√165)/9.8≈3.25s.
        Mit tank_speed=25: raw_land=-95+81.25=-13.75 ∈ [-30,30] → Sprungkante akzeptiert.
        Mit tank_speed=19: raw_land=-95+61.75=-33.25 ∉ [-30,30] → keine Sprungkante."""
        box = _make_box(0.0, 0.0, 0.0, 30.0, 15.0, 10.0)
        wm  = _make_world(boxes=[box], world_half=97.5)
        ng  = NavGraph(wm, max_jump_h=18.4)

        ground = ng.layers[0]
        # world_half=97.5 → erste Zellmitte exakt bei -95.0
        ix, iy = ground.world_to_cell(-95.0, 0.0)
        ix, iy = ground.clamp_cell(ix, iy)
        wx, wy = ground.cell_to_world(ix, iy)

        # Mit tank_speed=25: raw_land=-13.75 liegt auf dem Dach → Sprungkante
        edges_25 = ng._vertical_neighbors(0, ix, iy, wx, wy, ground)
        assert len(edges_25) > 0, \
            "Sprungkante soll mit tank_speed=25 existieren (raw_land im Dachbereich)"

        # Mit tank_speed=19: raw_land=-33.25 verfehlt das Dach → keine Sprungkante (NAV-03)
        ng._tank_speed = 19.0
        edges_19 = ng._vertical_neighbors(0, ix, iy, wx, wy, ground)
        ng._tank_speed = 25.0
        assert len(edges_19) == 0, \
            "Mit tank_speed=19 darf keine Sprungkante existieren (NAV-03)"

    def test_jump_reach_scales_with_tank_speed_param(self):
        """Der ``tank_speed``-Parameter (flaggenbedingte Reisegeschwindigkeit) hebt die
        Sprungreichweite: derselbe Absprung ist bei Basis 25 NICHT, bei 40 (Velocity) DOCH eine
        Sprungkante. Bot bei wx=-115 → aabb_dist=85u < JUMP_RANGE (Kandidat überlebt den Cull),
        aber jenseits der 25er-Reichweite (~73u)."""
        box = _make_box(0.0, 0.0, 0.0, 30.0, 15.0, 10.0)   # Dach z=10, x∈[-30,30]
        wm  = _make_world(boxes=[box], world_half=117.5)     # erste Zellmitte bei -115
        ng  = NavGraph(wm, max_jump_h=18.4)
        g = ng.layers[0]
        ix, iy = g.clamp_cell(*g.world_to_cell(-115.0, 0.0))
        wx, wy = g.cell_to_world(ix, iy)

        def up(ts):
            return [n for n, c in ng._vertical_neighbors(0, ix, iy, wx, wy, g, tank_speed=ts)
                    if ng.layers[n[0]].z > g.z]

        assert not up(25.0), "Bei Basis 25 darf der weite Sprung NICHT machbar sein"
        assert up(40.0),     "Bei 40 (Velocity) muss der Sprung machbar werden"

    def test_nav_jump_up_penalty_only_on_teleporter_maps(self):
        """Sicherheits-Strafe: Sprung-hoch-Kanten kosten auf Teleporter-Karten NAV_JUMP_UP_PENALTY
        mehr (Bot soll lieber das sichere Tor nehmen). Ohne Teleporter unverändert (keine Regression)."""
        from bzflag.nav_graph import NAV_JUMP_UP_PENALTY
        box = _make_box(0.0, 0.0, 0.0, 30.0, 15.0, 10.0)
        ng  = NavGraph(_make_world(boxes=[box], world_half=97.5), max_jump_h=18.4)
        g = ng.layers[0]
        ix, iy = g.clamp_cell(*g.world_to_cell(-95.0, 0.0))
        wx, wy = g.cell_to_world(ix, iy)
        up = lambda: [c for n, c in ng._vertical_neighbors(0, ix, iy, wx, wy, g)
                      if ng.layers[n[0]].z > g.z]
        c0 = up()
        assert c0, "Test-Setup: Sprungkante erwartet"
        ng._teleporters = [object()]              # Karte „hat jetzt Teleporter"
        ng._reset_vertical_cache()                # _teleporters nicht im Cache-Key → neu berechnen
        c1 = up()
        assert c1[0] - c0[0] == pytest.approx(NAV_JUMP_UP_PENALTY)


# ---------------------------------------------------------------------------
# NAV-19: Sprungbogen-Kopfstoß an dazwischenliegendem Überhang
# ---------------------------------------------------------------------------

def _head_cross_x(wx, hspeed, z0, bz, v0=19.0, g=9.8):
    """x, an dem der Bogen-Kopf (z+TANK_HEIGHT) in der Steigphase die Unterkante bz kreuzt —
    dieselbe geschlossene Lösung wie _arc_clears_overhangs (Bewegung in +x)."""
    t = (v0 - math.sqrt(v0 * v0 - 2.0 * g * (bz - TANK_HEIGHT - z0))) / g
    return wx + hspeed * t


class TestNAV19ArcOverhang:
    """NAV-19: _vertical_neighbors verwirft Sprungkanten, deren Bogen die Unterkante eines
    DAZWISCHEN liegenden Hindernisses streift (Kopfstoß). Quell-/Ziel-Obstacle ausgenommen."""

    def _ng_with(self, *extra):
        target = _make_box(0.0, 0.0, 0.0, 40.0, 40.0, 14.0)        # Ziel-Dach z=14
        ng = NavGraph(_make_world(boxes=[target, *extra], world_half=100.0), max_jump_h=18.4)
        ground = ng.layers[0]
        roof = next(l for l in ng.layers if l.source_obstacle is target)
        return ng, ground, roof, target

    def test_overhang_in_arc_blocks(self):
        wx, hspeed, bz = -50.0, 20.0, 12.0
        x = _head_cross_x(wx, hspeed, 0.0, bz)                     # ≈ -37.5
        over = _make_box(x, 0.0, bz, 5.0, 5.0, 3.0)               # schwebender Überhang im Bogen
        ng, ground, roof, _t = self._ng_with(over)
        assert ng._arc_clears_overhangs(wx, 0.0, ground, roof, 1.0, 0.0, hspeed) is False

    def test_free_arc_ok(self):
        ng, ground, roof, _t = self._ng_with()                    # nur Ziel, kein Überhang
        assert ng._arc_clears_overhangs(-50.0, 0.0, ground, roof, 1.0, 0.0, 20.0) is True

    def test_target_obstacle_excluded(self):
        # Gleicher Bogen + Überhang wie test_overhang_in_arc_blocks (dort False), hier ist der
        # Überhang aber selbst das ZIEL → seine Unterseite ist Landung, kein Hindernis. Sonst
        # würde der eigene Aufbau dünner Ziele (z. B. HIX-Querbalken) fälschlich gecullt.
        wx, hspeed, bz = -50.0, 20.0, 12.0
        x = _head_cross_x(wx, hspeed, 0.0, bz)
        over = _make_box(x, 0.0, bz, 5.0, 5.0, 3.0)
        ng, ground, _roof, _t = self._ng_with(over)
        dst = FloorLayer(z=bz + 3.0, cx=x, cy=0.0, half_w=5.0, half_d=5.0,
                         n_x=1, n_y=1, walkable=[[True]], source_obstacle=over)
        assert ng._arc_clears_overhangs(wx, 0.0, ground, dst, 1.0, 0.0, hspeed) is True

    def test_overhang_above_head_apex_ignored(self):
        # Unterkante über der maximalen Kopfhöhe (z0+apex+TANK ≈ 20.45) → unerreichbar, ignoriert
        over = _make_box(-37.5, 0.0, 25.0, 8.0, 8.0, 3.0)
        ng, ground, roof, _t = self._ng_with(over)
        assert ng._arc_clears_overhangs(-50.0, 0.0, ground, roof, 1.0, 0.0, 20.0) is True

    def test_vertical_neighbors_culls_jump_under_overhang(self):
        """End-to-End: dieselbe Geometrie wie NAV-03 (Dach z=10, Absprung bei -95), zusätzlich ein
        breiter schwebender Überhang im Bogen → die z=10-Sprungkante verschwindet."""
        box  = _make_box(0.0, 0.0, 0.0, 30.0, 15.0, 10.0)         # Ziel-Dach z=10
        wall = _make_box(-65.0, 0.0, 12.0, 25.0, 10.0, 2.0)       # Überhang x∈[-90,-40], bz=12

        def z10_edges(boxes):
            ng = NavGraph(_make_world(boxes=boxes, world_half=97.5), max_jump_h=18.4)
            g = ng.layers[0]
            ix, iy = g.clamp_cell(*g.world_to_cell(-95.0, 0.0))
            wx, wy = g.cell_to_world(ix, iy)
            return [e for e, _ in ng._vertical_neighbors(0, ix, iy, wx, wy, g)
                    if ng.layers[e[0]].z == pytest.approx(10.0)]

        assert z10_edges([box]), "Basis: z=10-Sprungkante muss ohne Überhang existieren"
        assert not z10_edges([box, wall]), \
            "Überhang im Bogen muss die z=10-Sprungkante verwerfen (NAV-19)"


# ---------------------------------------------------------------------------
# _clip_to_footprint: Zellen außerhalb des rotierten Footprints blockieren
# ---------------------------------------------------------------------------

class TestClipToFootprint:
    def _make_roof(self, obs: BoxObstacle) -> FloorLayer:
        """Erstellt einen AABB-basierten Roof-Layer für obs (alle Zellen walkable)."""
        cos_a = abs(math.cos(obs.angle))
        sin_a = abs(math.sin(obs.angle))
        ext_x = obs.half_w * cos_a + obs.half_d * sin_a
        ext_y = obs.half_w * sin_a + obs.half_d * cos_a
        w = ext_x - TANK_MARGIN
        d = ext_y - TANK_MARGIN
        n_x = max(1, int(2.0 * w / CELL_SIZE))
        n_y = max(1, int(2.0 * d / CELL_SIZE))
        walkable = [[True] * n_x for _ in range(n_y)]
        return FloorLayer(z=obs.bottom_z + obs.height,
                          cx=obs.cx, cy=obs.cy,
                          half_w=w, half_d=d,
                          n_x=n_x, n_y=n_y, walkable=walkable,
                          source_obstacle=obs)

    def test_diagonal_strip_clip_reduces_walkable(self):
        """Diagonalstreifen hw=4, hd=550, angle=45°: clip reduziert walkable drastisch.
        Mit CELL_SIZE=4 trifft die Mittellinie (lx≈0.34u ≤ 0.5u) → wenige Zellen begehbar,
        aber weit unter dem originalen Bug-Wert (~24001). hw=4 > TANK_MARGIN=3.5 → physikalisch OK."""
        obs = _make_box(0.0, 0.0, 29.0, 4.0, 550.0, 1.0, angle=math.pi / 4)
        roof = self._make_roof(obs)
        total_before = sum(sum(1 for w in row if w) for row in roof.walkable)
        assert roof.n_x > 100 and roof.n_y > 100, "Layer sollte AABB-groß sein"
        _clip_to_footprint(roof, obs, TANK_MARGIN)
        walkable_after = sum(sum(1 for w in row if w) for row in roof.walkable)
        assert walkable_after < total_before * 0.02, \
            f"clip muss >98% blockieren; war {total_before}, nach clip {walkable_after}"
        assert walkable_after < 400, \
            f"Nur wenige Mittellinienzellen erwartet, got {walkable_after}"

    def test_diagonal_strip_roof_layer_is_small(self):
        """NavGraph-Roof-Layer für Diagonalstreifen enthält nur Mittellinienzellen (keine 24001)."""
        obs = _make_box(0.0, 0.0, 29.0, 4.0, 550.0, 1.0, angle=math.pi / 4)
        wm = _make_world(boxes=[obs], world_half=400.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        roof_layers = [l for l in ng.layers if l.source_obstacle is obs]
        # Ggf. kein Layer (wenn grid-Alignment keine Zelle trifft) oder ein kleiner Layer
        if roof_layers:
            count = sum(sum(1 for w in row if w) for row in roof_layers[0].walkable)
            assert count < 400, \
                f"Roof-Layer sollte nur wenige Mittellinienzellen haben, hat {count}"

    def test_clip_preserves_wide_rotated_box(self):
        """Breite quadratische Box hw=35, hd=35, angle=45°: Footprint 31.5u →
        mindestens eine Zelle bleibt walkable nach clip."""
        obs = _make_box(0.0, 0.0, 26.0, 35.0, 35.0, 4.0, angle=math.pi / 4)
        roof = self._make_roof(obs)
        _clip_to_footprint(roof, obs, TANK_MARGIN)
        assert roof.has_any_walkable(), \
            "Breite rotierte Box sollte walkable Zellen behalten"

    def test_clip_axis_aligned_is_noop(self):
        """Achsenparallele Box (angle=0): AABB-Grenze = Footprint → clip ist No-Op."""
        obs = _make_box(0.0, 0.0, 0.0, 20.0, 20.0, 10.0, angle=0.0)
        roof = self._make_roof(obs)
        before = sum(sum(1 for w in row if w) for row in roof.walkable)
        _clip_to_footprint(roof, obs, TANK_MARGIN)
        after = sum(sum(1 for w in row if w) for row in roof.walkable)
        assert before == after, "Clip bei angle=0 darf keine Zellen entfernen"


# ---------------------------------------------------------------------------
# Superstruktur-Blocking: Headroom-Prüfung
# ---------------------------------------------------------------------------

class TestHeadroomBlocking:
    """Testet, dass Obstacles weit über dem Dach keine Zellen blockieren."""

    def test_superstructure_far_above_does_not_block(self):
        """Obstacle 14u über dem Dach (> TANK_HEIGHT=2.05) darf Zellen nicht blockieren.
        Entspricht dem HIX-Bug: Diagonalstreifen z=29 über z=15-Dach."""
        base = _make_box(0.0, 0.0, 0.0, 20.0, 20.0, 15.0)   # Dach bei z=15
        high = _make_box(0.0, 0.0, 29.0, 10.0, 10.0, 1.0)    # Obstacle bei z=29 (14u Abstand)
        wm = _make_world(boxes=[base, high], world_half=100.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        roof = next(l for l in ng.layers if l.source_obstacle is base)
        # Zellen unter 'high' im Zentrum (0,0) müssen walkable sein
        ix, iy = roof.world_to_cell(0.0, 0.0)
        assert roof.walkable[iy][ix], \
            "Obstacle 14u über Dach hat genug Kopffreiheit — Zelle muss walkable sein"

    def test_superstructure_close_above_blocks(self):
        """Obstacle 1u über dem Dach (< TANK_HEIGHT=2.05) muss Zellen blockieren."""
        base = _make_box(0.0, 0.0, 0.0, 20.0, 20.0, 15.0)   # Dach bei z=15
        low  = _make_box(0.0, 0.0, 16.0, 10.0, 10.0, 5.0)   # Obstacle bei z=16 (1u Abstand)
        wm = _make_world(boxes=[base, low], world_half=100.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        roof = next(l for l in ng.layers if l.source_obstacle is base)
        # Zellen direkt unter 'low' müssen blockiert sein (Tank passt nicht drunter)
        ix, iy = roof.world_to_cell(0.0, 0.0)
        assert not roof.walkable[iy][ix], \
            "Obstacle 1u über Dach: Tank passt nicht drunter — Zelle muss blockiert sein"


class TestGoalZ:
    """plan_path(goal_z=...) soll Zielknoten auf die angegebene Höhenzone filtern."""

    def test_without_goal_z_routes_to_ground(self):
        """Ohne goal_z landet der letzte Wegpunkt auf z=0 (billigster Weg)."""
        base = _make_box(0.0, 0.0, 0.0, 20.0, 20.0, 15.0)
        wm = _make_world(boxes=[base], world_half=100.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        path = ng.plan_path(-30.0, 0.0, 0.0, 0.0, 0.0)
        assert path, "Pfad muss gefunden werden"
        assert path[-1][2] == pytest.approx(0.0), \
            f"Ohne goal_z erwartet z=0, bekommen z={path[-1][2]}"

    def test_with_goal_z_routes_to_roof(self):
        """Mit goal_z=15 landet der letzte Wegpunkt auf dem z=15-Dach."""
        base = _make_box(0.0, 0.0, 0.0, 20.0, 20.0, 15.0)
        wm = _make_world(boxes=[base], world_half=100.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        path = ng.plan_path(-30.0, 0.0, 0.0, 0.0, 0.0, goal_z=15.0)
        assert path, "Pfad muss gefunden werden"
        assert path[-1][2] == pytest.approx(15.0), \
            f"Mit goal_z=15 erwartet z=15, bekommen z={path[-1][2]}"

    def test_goal_z_no_matching_layer_returns_empty(self):
        """goal_z ohne passende Layer → [] zurück, kein irreführender z=0-Pfad."""
        wm = _make_world(boxes=[], world_half=100.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        path = ng.plan_path(-30.0, 0.0, 0.0, 0.0, 0.0, goal_z=99.0)
        # Kein Dach bei z=99 → kein Fallback auf z=0, leerer Pfad
        assert path == []


# ---------------------------------------------------------------------------
# A*-Heuristik Z-Komponente
# ---------------------------------------------------------------------------

class TestAstarHeuristic3D:

    def _make_layer(self, z: float) -> FloorLayer:
        n = 4
        return FloorLayer(z=z, cx=0.0, cy=0.0, half_w=20.0, half_d=20.0,
                          n_x=n, n_y=n, walkable=[[True]*n for _ in range(n)],
                          source_obstacle=None)

    def test_heuristic_z_component_adds_z_gap(self):
        """_h mit goal_z=30 und layer.z=0 → h enthält +30 Z-Aufstiegsterm."""
        layer = self._make_layer(z=0.0)
        ix, iy = 2, 2
        wx, wy = layer.cell_to_world(ix, iy)
        h_no_z  = _h(layer, ix, iy, wx, wy)
        h_with_z = _h(layer, ix, iy, wx, wy, goal_z=30.0)
        assert h_with_z == pytest.approx(h_no_z + 30.0)

    def test_heuristic_no_z_component_when_already_at_goal_z(self):
        """_h mit goal_z=15 und layer.z=15 → Z-Term = 0, h gleich wie ohne goal_z."""
        layer = self._make_layer(z=15.0)
        ix, iy = 2, 2
        wx, wy = layer.cell_to_world(ix, iy)
        h_no_z   = _h(layer, ix, iy, wx, wy)
        h_with_z = _h(layer, ix, iy, wx, wy, goal_z=15.0)
        assert h_with_z == pytest.approx(h_no_z)

    def test_heuristic_no_negative_z_term(self):
        """_h mit layer.z > goal_z → Z-Term = 0 (max(0, goal_z - layer.z) = 0)."""
        layer = self._make_layer(z=30.0)
        ix, iy = 2, 2
        wx, wy = layer.cell_to_world(ix, iy)
        h_no_z   = _h(layer, ix, iy, wx, wy)
        h_with_z = _h(layer, ix, iy, wx, wy, goal_z=10.0)
        assert h_with_z == pytest.approx(h_no_z)

    def test_expansion_limit_returns_empty(self):
        """Ziel außerhalb des Grids → Open-Set erschöpft (kein goal_set-Match) → leere Liste.

        (Trifft NICHT den Limit-Zweig — die kleine Map hat nur ~625 Knoten; testet den
        regulären „kein Pfad"-Ausgang.)"""
        wm = _make_world(boxes=[], world_half=50.0)
        ng = NavGraph(wm, max_jump_h=0.0)
        result = ng._astar(
            (0, 0, 0),
            {(0, 999, 999)},  # Zelle außerhalb der Layer-Grenzen → nie erreichbar
            0.0, 0.0
        )
        assert result == []

    def test_expansion_limit_returns_best_effort_partial_path(self, monkeypatch):
        """Limit-Treffer bei erreichbarem, aber weitem Ziel → Best-Effort-Teilpfad (nicht []).

        Der Teilpfad startet am Start und endet am dem Ziel nächsten expandierten Knoten."""
        wm = _make_world(boxes=[], world_half=100.0)
        ng = NavGraph(wm, max_jump_h=0.0)
        layer = ng.layers[0]
        start = (0, 0, 0)                       # Ecke (~-98,-98)
        gix, giy = layer.clamp_cell(*layer.world_to_cell(90.0, 90.0))
        goal = (0, gix, giy)                    # gegenüberliegende Ecke, erreichbar aber weit
        gx, gy = layer.cell_to_world(gix, giy)
        monkeypatch.setattr("bzflag.nav_graph.ASTAR_MAX_EXPANSIONS", 5)
        path = ng._astar(start, {goal}, gx, gy)
        assert path, "Best-Effort-Teilpfad erwartet, nicht []"
        assert path[0] == start
        assert goal not in path                 # Ziel wegen Limit nicht erreicht
        # Fortschritt: letzter Teilpfad-Knoten ist näher am Ziel als der Start
        def _hh(node):
            return _h(ng.layers[node[0]], node[1], node[2], gx, gy)
        assert _hh(path[-1]) < _hh(start)

    def test_expansion_limit_no_progress_returns_empty(self, monkeypatch):
        """Limit greift schon nach dem ersten Expand (nur Start) → kein Fortschritt → []."""
        wm = _make_world(boxes=[], world_half=100.0)
        ng = NavGraph(wm, max_jump_h=0.0)
        layer = ng.layers[0]
        start = (0, 0, 0)
        gix, giy = layer.clamp_cell(*layer.world_to_cell(90.0, 90.0))
        gx, gy = layer.cell_to_world(gix, giy)
        monkeypatch.setattr("bzflag.nav_graph.ASTAR_MAX_EXPANSIONS", 1)
        assert ng._astar(start, {(0, gix, giy)}, gx, gy) == []

    def test_time_budget_returns_best_effort_partial_path(self, monkeypatch):
        """Wall-Clock-Budget (ASTAR_MAX_MS, alle 1024 Expansionen geprüft) → Best-Effort-Teilpfad.

        ASTAR_MAX_MS=0 → Deadline sofort überschritten; das Ziel liegt außerhalb des Grids (kein
        goal-Match), die Welt hat >1024 Knoten → der Budget-Zweig greift bei der 1024er-Expansion
        und liefert den Teilpfad zum zielnächsten Knoten statt der vollständigen Suche."""
        wm = _make_world(boxes=[], world_half=100.0)   # 50×50 = 2500 Knoten > 1024
        ng = NavGraph(wm, max_jump_h=0.0)
        start = (0, 0, 0)
        gx, gy = 95.0, 95.0
        monkeypatch.setattr("bzflag.nav_graph.ASTAR_MAX_MS", 0.0)
        path = ng._astar(start, {(0, 999, 999)}, gx, gy)
        assert path, "Best-Effort-Teilpfad erwartet (Zeitbudget), nicht []"
        assert path[0] == start
        def _hh(node):
            return _h(ng.layers[node[0]], node[1], node[2], gx, gy)
        assert _hh(path[-1]) < _hh(start)        # Fortschritt Richtung Ziel


# ---------------------------------------------------------------------------
# P4-INF-01: Reentranz (Per-Call-Limits, Cancel, paralleles plan_path)
# ---------------------------------------------------------------------------

class TestAstarReentrancy:
    """plan_path/_astar halten keinen veränderlichen Zustand mehr auf self (blocked_jump_wps,
    Limits via Parameter) → derselbe gecachte Graph ist parallel beplanbar (P4-INF-01)."""

    def _far_world(self):
        wm = _make_world(boxes=[], world_half=100.0)
        ng = NavGraph(wm, max_jump_h=0.0)
        layer = ng.layers[0]
        gix, giy = layer.clamp_cell(*layer.world_to_cell(90.0, 90.0))
        gx, gy = layer.cell_to_world(gix, giy)
        return ng, (0, 0, 0), {(0, gix, giy)}, gx, gy, (0, gix, giy)

    def test_per_call_max_expansions_overrides_module_constant(self, monkeypatch):
        """`max_expansions`-Parameter hat Vorrang vor der Modulkonstante — in beide Richtungen."""
        ng, start, goal_set, gx, gy, goal = self._far_world()
        # Modulkonstante winzig, Per-Call groß → Ziel wird trotzdem erreicht.
        monkeypatch.setattr("bzflag.nav_graph.ASTAR_MAX_EXPANSIONS", 1)
        full = ng._astar(start, goal_set, gx, gy, max_expansions=50000)
        assert full and full[-1] == goal
        # Modulkonstante groß, Per-Call winzig → nur Best-Effort-Teilpfad (Ziel nicht erreicht).
        monkeypatch.setattr("bzflag.nav_graph.ASTAR_MAX_EXPANSIONS", 50000)
        partial = ng._astar(start, goal_set, gx, gy, max_expansions=5)
        assert partial and partial[0] == start and partial[-1] != goal

    def test_per_call_max_ms_overrides_module_constant(self, monkeypatch):
        """`max_ms=0` erzwingt sofortiges Zeitbudget-Aus → Best-Effort-Teilpfad, auch wenn die
        Modulkonstante riesig ist."""
        ng, start, _gs, gx, gy, _goal = self._far_world()
        monkeypatch.setattr("bzflag.nav_graph.ASTAR_MAX_MS", 1e9)
        path = ng._astar(start, {(0, 999, 999)}, gx, gy, max_ms=0.0)
        assert path and path[0] == start

    def test_cancel_event_triggers_best_effort(self):
        """Gesetztes Cancel-Event → Best-Effort-Teilpfad statt voller Suche. Gegenprobe ohne
        Cancel: dasselbe (außerhalb des Grids liegende) Ziel erschöpft das Open-Set → []."""
        ng, start, _gs, gx, gy, _goal = self._far_world()
        ev = threading.Event(); ev.set()
        path = ng._astar(start, {(0, 999, 999)}, gx, gy, cancel=ev)
        assert path and path[0] == start
        assert ng._astar(start, {(0, 999, 999)}, gx, gy) == []

    def test_partial_level_and_label(self, caplog):
        """`label`/`partial_level` steuern Text und Stufe des Best-Effort-Logs; Cancel bleibt DEBUG."""
        ng, start, goal_set, gx, gy, _goal = self._far_world()
        # Schnellplan-Tag: DEBUG statt WARNING, Label statt generischem „A*" im Text.
        with caplog.at_level(logging.DEBUG, logger="bzbot"):
            ng._astar(start, goal_set, gx, gy, max_expansions=5,
                      label="Schnellplan", partial_level=logging.DEBUG)
        recs = [r for r in caplog.records if "Schnellplan" in r.getMessage()]
        assert recs and all(r.levelno == logging.DEBUG for r in recs)
        # Default = WARNING / „A*" (rückwärtskompatibel für ungetaggte Caller).
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="bzbot"):
            ng._astar(start, goal_set, gx, gy, max_expansions=5)
        assert any(r.levelno == logging.WARNING and "[NAV] A*:" in r.getMessage()
                   for r in caplog.records)
        # Cancel („Abgebrochen") ist immer normal → DEBUG, auch bei partial_level=INFO.
        caplog.clear()
        ev = threading.Event(); ev.set()
        with caplog.at_level(logging.DEBUG, logger="bzbot"):
            ng._astar(start, {(0, 999, 999)}, gx, gy, cancel=ev,
                      label="Vollsuche", partial_level=logging.INFO)
        recs = [r for r in caplog.records if "Abgebrochen" in r.getMessage()]
        assert recs and all(r.levelno == logging.DEBUG for r in recs)

    def _jump_world(self):
        box = _make_box(0.0, 0.0, 0.0, 30.0, 15.0, 10.0)
        ng = NavGraph(_make_world(boxes=[box], world_half=97.5), max_jump_h=18.4)
        g = ng.layers[0]
        ix, iy = g.clamp_cell(*g.world_to_cell(-95.0, 0.0))
        wx, wy = g.cell_to_world(ix, iy)
        return ng, g, ix, iy, wx, wy

    def test_vertical_neighbors_honours_blocked_param(self):
        """`blocked_jump_wps` wird als Parameter (nicht von self) ausgewertet: das geblockte
        Sprung-Ziel verschwindet aus den Kanten."""
        ng, g, ix, iy, wx, wy = self._jump_world()
        ups = [(n, c) for n, c in ng._vertical_neighbors(0, ix, iy, wx, wy, g)
               if ng.layers[n[0]].z > g.z]
        assert ups, "Setup: Sprungkante erwartet"
        (dl, dx, dy), _ = ups[0]
        dst = ng.layers[dl]; dwx, dwy = dst.cell_to_world(dx, dy)
        blocked = frozenset({(round(dwx), round(dwy), dst.z)})
        ups_blk = [n for n, _ in ng._vertical_neighbors(0, ix, iy, wx, wy, g, blocked)
                   if ng.layers[n[0]].z > g.z]
        assert (dl, dx, dy) not in ups_blk

    def test_plan_path_reentrant_concurrent_different_blocked(self):
        """Zwei Threads beplanen denselben gecachten Graph gleichzeitig mit VERSCHIEDENEN
        blocked_jump_wps → jeder reproduziert immer sein Einzel-Baseline-Ergebnis (kein Clobber
        des früher auf self gehaltenen _blocked_jump_wps)."""
        ng, g, ix, iy, wx, wy = self._jump_world()
        ups = ng._vertical_neighbors(0, ix, iy, wx, wy, g)
        (dl, dx, dy), _ = [(n, c) for n, c in ups if ng.layers[n[0]].z > g.z][0]
        dst = ng.layers[dl]; dwx, dwy = dst.cell_to_world(dx, dy)
        blocked = frozenset({(round(dwx), round(dwy), dst.z)})

        def _plan(bjw):
            return ng.plan_path(wx, wy, 0.0, 0.0, 0.0, goal_z=10.0, blocked_jump_wps=bjw)

        base_empty = _plan(frozenset())
        base_blk = _plan(blocked)
        assert base_empty and base_blk and base_empty != base_blk  # Block ändert die Route

        results = {"empty": [], "blk": []}
        barrier = threading.Barrier(2)

        def _runner(key, bjw):
            barrier.wait()
            for _ in range(40):
                results[key].append(_plan(bjw))

        t1 = threading.Thread(target=_runner, args=("empty", frozenset()))
        t2 = threading.Thread(target=_runner, args=("blk", blocked))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert all(r == base_empty for r in results["empty"])
        assert all(r == base_blk for r in results["blk"])


# ---------------------------------------------------------------------------
# Teil B: vorberechnete vertikale Kandidaten-Layer (_build_vertical_adjacency)
# ---------------------------------------------------------------------------

class TestVerticalAdjacencyPrecompute:
    """_build_vertical_adjacency cullt die vertikalen Kandidaten-Layer NUR konservativ:
    _vertical_neighbors liefert mit Kandidatenliste byte-IDENTISCHE Kanten wie ein voller
    Layer-Scan (das Optimierungs-Sicherheitsnetz von Teil B)."""

    @staticmethod
    def _real_vs_full(ng, nodes):
        """(real, full) Kanten-Maps: real = Kandidatenliste, full = alle Layer als Kandidaten.
        Mutiert ng nur temporär und stellt die Original-Kandidatenlisten wieder her."""
        real = {(l, i, j): ng._vertical_neighbors(l, i, j, wx, wy, L)
                for (l, i, j, wx, wy, L) in nodes}
        n = len(ng.layers)
        saved_up, saved_down = ng._jump_up_cands, ng._fall_cands
        try:
            ng._jump_up_cands = {lid: list(range(n)) for lid in range(n)}
            ng._fall_cands = {lid: list(range(n)) for lid in range(n)}
            # Kandidatenlisten stecken NICHT im Cache-Key → Cache leeren, sonst liefert der
            # Full-Scan die gecachten real-Kanten und das Sicherheitsnetz wäre trivial-grün.
            ng._reset_vertical_cache()
            full = {(l, i, j): ng._vertical_neighbors(l, i, j, wx, wy, L)
                    for (l, i, j, wx, wy, L) in nodes}
        finally:
            ng._jump_up_cands, ng._fall_cands = saved_up, saved_down
            ng._reset_vertical_cache()   # Full-Scan-Einträge nicht zurücklassen
        return real, full

    @staticmethod
    def _all_nodes(ng, per_layer_cap=None):
        nodes = []
        for lid, L in enumerate(ng.layers):
            cnt = 0
            for iy in range(L.n_y):
                for ix in range(L.n_x):
                    if L.walkable[iy][ix]:
                        wx, wy = L.cell_to_world(ix, iy)
                        nodes.append((lid, ix, iy, wx, wy, L))
                        cnt += 1
                if per_layer_cap and cnt >= per_layer_cap:
                    break
        return nodes

    def test_candidate_lists_match_full_scan_multilayer(self):
        """Synthetische Mehrlagen-Welt (inkl. rotiertem Obstacle + Fall-Zielen): identische Kanten."""
        boxes = [
            _make_box(0, 0, 0, 10, 10, 15.0),                          # z15-Dach
            _make_box(26, 0, 0, 10, 10, 6.0),                          # z6-Dach (tieferes Fall-Ziel)
            _make_box(-26, 0, 0, 8, 8, 30.0),                          # z30-Dach
            _make_box(0, 30, 0, 6, 18, 15.0, angle=math.radians(45)),  # rotiert (lokaler Frame)
            _make_box(0, -30, 0, 14, 4, 14.0),                         # dünner Querbalken
        ]
        ng = NavGraph(_make_world(boxes=boxes, world_half=80.0), max_jump_h=18.4)
        nodes = self._all_nodes(ng)
        real, full = self._real_vs_full(ng, nodes)
        assert sum(len(v) for v in full.values()) > 0, "Test-Welt erzeugt keine vertikalen Kanten"
        mism = [k for k in real if real[k] != full[k]]
        assert not mism, (f"{len(mism)} Knoten mit abweichenden Kanten — Kandidaten-Cull verliert/"
                          f"erfindet Kanten, z.B. {mism[0]}: {real[mism[0]]} != {full[mism[0]]}")


class TestVerticalNeighborCache:
    """Der Sprungkanten-Ergebnis-Cache (_vn_cache) verändert das Resultat nicht: Cache-Hit ==
    Cache-Miss == frischer NavGraph; der laufzeit-dynamische NAV-14-Filter (blocked_jump_wps) wird
    korrekt beim Lesen angewandt (auch auf Cache-Hits)."""

    @staticmethod
    def _mk():
        boxes = [
            _make_box(0, 0, 0, 10, 10, 15.0),
            _make_box(26, 0, 0, 10, 10, 6.0),
            _make_box(-26, 0, 0, 8, 8, 30.0),
            _make_box(0, 30, 0, 6, 18, 15.0, angle=math.radians(45)),
            _make_box(0, -30, 0, 14, 4, 14.0),
        ]
        return NavGraph(_make_world(boxes=boxes, world_half=80.0), max_jump_h=18.4)

    def test_hit_equals_miss_equals_fresh(self):
        """Zweiter Aufruf (Hit) == erster Aufruf (Miss) == frischer, nie gecachter Graph."""
        ng = self._mk()
        fresh = self._mk()
        nodes = TestVerticalAdjacencyPrecompute._all_nodes(ng)
        assert nodes, "Test-Setup: Knoten erwartet"
        total = 0
        for (l, i, j, wx, wy, L) in nodes:
            miss = ng._vertical_neighbors(l, i, j, wx, wy, L)          # populiert Cache
            hit  = ng._vertical_neighbors(l, i, j, wx, wy, L)          # Cache-Hit
            ref  = fresh._vertical_neighbors(l, i, j, wx, wy, L)       # anderer Graph, frisch
            assert miss == hit == ref, f"Cache weicht ab bei Knoten ({l},{i},{j})"
            total += len(miss)
        assert total > 0, "Test-Welt erzeugt keine vertikalen Kanten"

    def test_blocked_filter_applies_on_cache_hit(self):
        """blocked_jump_wps entfernt genau die Zielkante — auch wenn der Cache schon warm ist."""
        ng = self._mk()
        # Einen Knoten mit Sprung-hoch-Kante finden.
        node = target = None
        for (l, i, j, wx, wy, L) in TestVerticalAdjacencyPrecompute._all_nodes(ng):
            ups = [n for n, _ in ng._vertical_neighbors(l, i, j, wx, wy, L)
                   if ng.layers[n[0]].z > L.z]
            if ups:
                node = (l, i, j, wx, wy, L); target = ups[0]; break
        assert node is not None, "Test-Setup: Knoten mit Sprung-hoch-Kante erwartet"

        l, i, j, wx, wy, L = node
        base = ng._vertical_neighbors(l, i, j, wx, wy, L)              # Cache jetzt warm
        dst = ng.layers[target[0]]
        dwx, dwy = dst.cell_to_world(target[1], target[2])
        blk = frozenset({(round(dwx), round(dwy), dst.z)})

        filtered = ng._vertical_neighbors(l, i, j, wx, wy, L, blocked_jump_wps=blk)  # Hit + Filter
        assert target not in [n for n, _ in filtered], "geblocktes Sprungziel muss fehlen"
        removed = [e for e in base if e[0] == target]
        assert set(filtered) == set(base) - set(removed), "nur die geblockte Kante darf entfallen"
        # Ohne Filter erneut (Hit) → wieder vollständig (Filter mutiert den Cache nicht).
        assert ng._vertical_neighbors(l, i, j, wx, wy, L) == base


# ---------------------------------------------------------------------------
# HIX-Querbalken: Sprung von z=15 auf rotierten z=30-Querbalken
# ---------------------------------------------------------------------------

class TestHixCrossbarJump:
    """NavGraph: Sprung von z=15-Dach auf rotierten z=30-Querbalken (HIX-Szenario).

    Kernproblem: der 45°-rotierte Querbalken hat eine sehr große AABB (überdeckt fast
    die ganze Karte), sodass aabb_dist=0 für alle Positionen war und alle Sprungkanten
    blockiert wurden. Fix: Footprint im lokalen Koordinatensystem + Höhencheck.
    """

    @pytest.fixture(scope="class")
    def hix_nav(self):
        """z=15-Plattform außerhalb des Querbalken-Footprints, aber innerhalb der AABB."""
        z15 = _make_box(cx=-50.0, cy=0.0, bz=0.0, hw=10.0, hd=10.0, height=15.0)
        # Querbalken wie in HIX: half_w=4 (von size=4 in .bzw), half_d=100 → AABB überdeckt (-50,0).
        # half_w muss >= TANK_MARGIN=3.5 sein, damit _clip_to_footprint walkable Zellen übrig lässt.
        crossbar = _make_box(cx=0.0, cy=0.0, bz=29.0, hw=4.0, hd=100.0, height=1.0,
                             angle=math.pi / 4)
        wm = _make_world(boxes=[z15, crossbar], world_half=100.0)
        return NavGraph(wm, max_jump_h=18.4)

    def test_crossbar_layer_exists(self, hix_nav):
        """NavGraph erstellt einen FloorLayer bei z=30 für den Querbalken."""
        assert any(abs(l.z - 30.0) < 1.0 for l in hix_nav.layers[1:]), \
            "Querbalken (bottom_z=29, height=1) muss einen Layer bei z=30 erzeugen"

    def test_aabb_covers_platform(self, hix_nav):
        """Voraussetzung: AABB des Querbalken-Layers überdeckt die z=15-Plattform.

        Ohne den Fix wäre aabb_dist=0 → Sprungkante blockiert (der eigentliche Bug).
        """
        crossbar_layer = next(l for l in hix_nav.layers if abs(l.z - 30.0) < 1.0)
        platform_x, platform_y = -50.0, 0.0
        aabb_dist = max(0.0,
                        abs(platform_x - crossbar_layer.cx) - crossbar_layer.half_w,
                        abs(platform_y - crossbar_layer.cy) - crossbar_layer.half_d)
        assert aabb_dist < 0.1, \
            (f"Plattform (-50,0) muss im AABB des Querbalken-Layers liegen "
             f"(aabb_dist={aabb_dist:.1f}) — Testvoraussetzung verletzt")

    def test_jump_from_z15_to_crossbar_z30_allowed(self, hix_nav):
        """Kernfix: Von z=15-Dach muss ein Sprung auf den z=30-Querbalken möglich sein.

        Querbalken-Boden liegt bei z=29; Bot bei z=15 ist deutlich unterhalb →
        freie Luft → Sprung erlaubt (layer.z=15 < obs.bottom_z-TANK_HEIGHT=26.95).
        """
        path = hix_nav.plan_path(-50.0, 0.0, 15.0, 0.0, 0.0, goal_z=30.0)
        assert path, \
            "Kein Pfad z=15→z=30 gefunden — _vertical_neighbors blockiert den Sprung noch?"
        assert path[-1][2] == pytest.approx(30.0, abs=1.0), \
            f"Letzter Wegpunkt nicht auf z=30: z={path[-1][2]}"

    def test_no_jump_from_within_footprint(self):
        """Sprungkante wird blockiert wenn Bot direkt unter dem Ziel-Obstacle (aabb_dist<0.1).

        Früher: nur bei layer.z >= dst_bottom - TANK_HEIGHT blockiert.
        Jetzt: immer blockiert — Sprungbogen muss durch Obstacle-Körper, physikalisch unmöglich.
        Der HIX-Querbalken-Fall (aabb_dist=31 im rotierten Frame) ist nicht betroffen.
        """
        # Breite z=15-Plattform, darüber ein enges z=30-Kästchen (bz=18, kleines gap > TANK_HEIGHT)
        z15 = _make_box(cx=0.0, cy=0.0, bz=0.0, hw=20.0, hd=20.0, height=15.0)
        z30 = _make_box(cx=0.0, cy=0.0, bz=18.0, hw=10.0, hd=10.0, height=12.0)
        wm = _make_world(boxes=[z15, z30], world_half=100.0)
        nav = NavGraph(wm, max_jump_h=18.4)

        # Zelle direkt unter dem z=30-Kästchen auf der z=15-Ebene
        layer15 = next(l for l in nav.layers if abs(l.z - 15.0) < 0.5)
        lid15 = nav.layers.index(layer15)
        ix, iy = layer15.world_to_cell(0.0, 0.0)
        ix, iy = layer15.clamp_cell(ix, iy)
        assert layer15.walkable[iy][ix], "Zelle direkt unter z=30-Kästchen muss begehbar sein"

        wx, wy = layer15.cell_to_world(ix, iy)
        neighbors = nav._vertical_neighbors(lid15, ix, iy, wx, wy, layer15)
        z30_jumps = [n for n, _ in neighbors if nav.layers[n[0]].z > 20.0]
        assert len(z30_jumps) == 0, \
            "Keine Sprungkante auf z=30 wenn Bot direkt unter dem Obstacle-Footprint steht"


# ---------------------------------------------------------------------------
# _margin_for: Ebenen-Split (voller Margin am Boden, reduziert nur auf Dächern)
# ---------------------------------------------------------------------------

class TestMarginForLayer:
    """Request 1: dünne Wände nutzen reduzierten THIN_WALL_MARGIN NUR auf Dach-/Plattform-
    Layern (z>0.5). Auf dem Boden (z=0) bekommen auch dünne Obstacles den vollen TANK_MARGIN,
    damit der Bot nicht an den kleinen Kreuz-Obstacles hängen bleibt."""

    def test_thin_obstacle_full_margin_on_ground(self):
        thin = _make_box(cx=0.0, cy=0.0, bz=0.0, hw=0.5, hd=10.0, height=5.0)
        assert _margin_for(thin, 0.0) == TANK_MARGIN

    def test_thin_obstacle_reduced_margin_on_roof(self):
        thin = _make_box(cx=0.0, cy=0.0, bz=14.0, hw=0.5, hd=10.0, height=16.0)
        assert _margin_for(thin, 15.0) == THIN_WALL_MARGIN

    def test_thick_obstacle_full_margin_everywhere(self):
        thick = _make_box(cx=0.0, cy=0.0, bz=0.0, hw=10.0, hd=10.0, height=5.0)
        assert _margin_for(thick, 0.0) == TANK_MARGIN
        assert _margin_for(thick, 15.0) == TANK_MARGIN

    def test_ground_thin_wall_blocks_full_margin_ring(self):
        """Dünne Wand am Boden blockiert Zellen bis ~TANK_MARGIN (nicht nur ~THIN_WALL_MARGIN).
        Eine Zelle ~2.5u neben der Wand (zwischen THIN_WALL_MARGIN=1.4 und TANK_MARGIN=3.5)
        muss am Boden blockiert sein."""
        wall = _make_box(cx=0.0, cy=0.0, bz=0.0, hw=0.5, hd=40.0, height=5.0)
        wm = _make_world(boxes=[wall], world_half=60.0)
        nav = NavGraph(wm, max_jump_h=18.4)
        ground = nav.layers[0]
        # Zelle ~2.5u seitlich der Wandmitte (x≈3.0): innerhalb TANK_MARGIN, außerhalb THIN_WALL_MARGIN
        ix, iy = ground.world_to_cell(3.0, 0.0)
        ix, iy = ground.clamp_cell(ix, iy)
        assert not ground.walkable[iy][ix], \
            "Boden-Zelle im TANK_MARGIN-Ring der dünnen Wand muss blockiert sein"


# ---------------------------------------------------------------------------
# Pixel-on-Sprungkanten: Absprung vom Rand, Spitze rotierter Plattformen, Front-Catch
# ---------------------------------------------------------------------------

class TestPixelOnJump:
    """Request 2: Sprungkanten modellieren die Pixel-on-Physik (Absprung-Überhang +
    Front-Catch) und die echte Geometrie rotierter Zielplattformen (Diamant-Spitze)."""

    def test_jump_onto_rotated_diamond_tip(self):
        """Sprung von z=15-Plattform auf die SPITZE einer 45°-gedrehten z=30-Plattform.
        Früher verhinderte die Landeklammer auf den achsenparallelen Inschriften-Kasten
        die Spitze; jetzt rotationskorrekt + Pixel-on-Front-Catch."""
        z15 = _make_box(cx=-60.0, cy=0.0, bz=0.0, hw=10.0, hd=10.0, height=15.0)
        diamond = _make_box(cx=0.0, cy=0.0, bz=29.0, hw=12.0, hd=12.0, height=1.0,
                            angle=math.pi / 4)
        wm = _make_world(boxes=[z15, diamond], world_half=100.0)
        nav = NavGraph(wm, max_jump_h=18.4)
        path = nav.plan_path(-60.0, 0.0, 15.0, 0.0, 0.0, goal_z=30.0)
        assert path, "Kein Pfad z=15 → Spitze der 45°-z=30-Plattform"
        assert abs(path[-1][2] - 30.0) < 1.0

    def test_no_jump_when_dz_exceeds_max(self):
        """dz >= max_jump_h bleibt abgelehnt — Boden (z=0) auf z=30 (dz=30 > 18.4)."""
        diamond = _make_box(cx=0.0, cy=0.0, bz=29.0, hw=12.0, hd=12.0, height=1.0,
                            angle=math.pi / 4)
        wm = _make_world(boxes=[diamond], world_half=100.0)
        nav = NavGraph(wm, max_jump_h=18.4)
        assert nav.plan_path(-60.0, 0.0, 0.0, 0.0, 0.0, goal_z=30.0) == []

    def test_clearance_blocks_too_close_high_jump(self):
        """Eine zu nah an einer hohen Plattform stehende z=15-Fläche bekommt KEINE Sprungkante:
        der Front-Catch deckelt den Überhang, sodass der Clearance-Mindestabstand erhalten
        bleibt — der Bot kann nicht in die Flanke der z=30-Plattform springen."""
        # z=30-Plattform und z=15-Plattform mit nur kleinem Spalt (deutlich < Clearance-Min ~26u)
        z15 = _make_box(cx=-25.0, cy=0.0, bz=0.0, hw=10.0, hd=10.0, height=15.0)
        z30 = _make_box(cx=0.0, cy=0.0, bz=29.0, hw=10.0, hd=10.0, height=1.0)
        wm = _make_world(boxes=[z15, z30], world_half=80.0)
        nav = NavGraph(wm, max_jump_h=18.4)
        layer15 = next(l for l in nav.layers if abs(l.z - 15.0) < 0.5)
        lid15 = nav.layers.index(layer15)
        found = False
        for iy in range(layer15.n_y):
            for ix in range(layer15.n_x):
                if not layer15.walkable[iy][ix]:
                    continue
                wx, wy = layer15.cell_to_world(ix, iy)
                for nb, _ in nav._vertical_neighbors(lid15, ix, iy, wx, wy, layer15):
                    if nav.layers[nb[0]].z > 20.0:
                        found = True
        assert not found, "Zu nah an hoher Wand: Clearance muss den Sprung verhindern"


# ---------------------------------------------------------------------------
# Sprungkanten-Bewertung: euklidischer Gap + Überhang-Deckelung (nur Randzellen)
# ---------------------------------------------------------------------------

class TestJumpEdgeOriginatesAtPlatformEdge:
    """Regression: Sprungkanten dürfen nur von Zellen NAHE der Plattformkante ausgehen.
    Früher deckelte `overhang = min(t_exit, aabb_dist)` den Überhang auf das Rollen quer über
    die ganze Plattform → A* erzeugte unmögliche Weitsprünge ab Innenzellen (eff_gap zu klein,
    weil aabb_dist = Chebyshev statt euklidisch). Jetzt: euklidischer Gap + Deckelung auf
    `_margin_for + JUMP_EDGE_TOL`."""

    def _setup(self):
        # Breite z=15-Plattform; entfernte 45°-z=30-Diamant-Spitze gerade so erreichbar von der
        # Kante, aber NICHT von der Plattform-Mitte (~40u weiter weg).
        z15 = _make_box(cx=0.0, cy=0.0, bz=0.0, hw=40.0, hd=40.0, height=15.0)
        diamond = _make_box(cx=118.0, cy=0.0, bz=29.0, hw=20.0, hd=20.0, height=1.0,
                            angle=math.pi / 4)
        wm = _make_world(boxes=[z15, diamond], world_half=160.0)
        nav = NavGraph(wm, max_jump_h=18.4)
        layer15 = next(l for l in nav.layers if abs(l.z - 15.0) < 0.5)
        return nav, layer15, nav.layers.index(layer15)

    def test_interior_cell_has_no_jump_edge(self):
        nav, layer15, lid15 = self._setup()
        ix, iy = layer15.clamp_cell(*layer15.world_to_cell(0.0, 0.0))  # Plattform-Mitte
        wx, wy = layer15.cell_to_world(ix, iy)
        z30 = [n for n, _ in nav._vertical_neighbors(lid15, ix, iy, wx, wy, layer15)
               if nav.layers[n[0]].z > 20.0]
        assert not z30, "Innenzelle darf keine z=30-Sprungkante bekommen (Weitsprung quer über Plattform)"

    def test_edge_cells_keep_jump_edge_near_edge_only(self):
        nav, layer15, lid15 = self._setup()
        launch_cells = []
        for iy in range(layer15.n_y):
            for ix in range(layer15.n_x):
                if not layer15.walkable[iy][ix]:
                    continue
                wx, wy = layer15.cell_to_world(ix, iy)
                if any(nav.layers[n[0]].z > 20.0
                       for n, _ in nav._vertical_neighbors(lid15, ix, iy, wx, wy, layer15)):
                    launch_cells.append((wx, wy))
        assert launch_cells, "Mind. eine Randzelle muss die z=30-Spitze erreichen können"
        # Alle Absprungzellen liegen in der äußeren Plattformhälfte (max|Koord| > 20, hw=40) —
        # nie in der tiefen Mitte. Damit ist der Weitsprung-quer-über-Plattform-Bug ausgeschlossen.
        for wx, wy in launch_cells:
            assert max(abs(wx), abs(wy)) > 20.0, \
                f"Absprungzelle ({wx:.1f},{wy:.1f}) liegt in der tiefen Plattform-Mitte"


# ---------------------------------------------------------------------------
# Run-up-Einfügung: VOR der Absprungzelle + Thin-Wall-Guard
# ---------------------------------------------------------------------------

class TestRunupInsertion:
    """Der Run-up-WP muss VOR der Absprungzelle liegen (Bot erreicht sie ausgerichtet und
    springt von dort ab), und darf keine dünne Wand kreuzen."""

    class _FakeNav:
        """Minimaler NavGraph-Stub für _insert_jump_runups (flacher Boden, keine Obstacles)."""
        def __init__(self, obs=None):
            self._obs = obs or []
            self._tele_exit_wps = set()   # keine Teleporter im Stub
        def get_floor_z(self, x, y, z, overhang=0.0):
            return 0.0

    def test_runup_inserted_before_launch_cell(self):
        # Pfad auf z=0 bis zur Absprungzelle (20,0), dann Sprung-rauf auf z=15 bei (40,0).
        path = [(0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (40.0, 0.0, 15.0)]
        out = _insert_jump_runups(path, self._FakeNav())
        # Erwartung: Run-up bei x≈16 (20 - CELL_SIZE) VOR der Absprungzelle (20,0)
        zs = [p for p in out]
        jump_idx = next(i for i in range(1, len(out)) if out[i][2] - out[i-1][2] > 1.5)
        launch = out[jump_idx - 1]
        runup = out[jump_idx - 2]
        assert launch[:2] == (20.0, 0.0), "Absprungzelle bleibt der A*-Sprungursprung (20,0)"
        assert runup[0] < launch[0], "Run-up liegt VOR der Absprungzelle (näher am Pfad-Anfang)"
        assert abs(runup[0] - (20.0 - CELL_SIZE)) < 0.01

    def test_no_runup_when_first_wp_is_launch(self):
        # Absprungzelle ist gleich der Start-WP → kein Run-up (Bot richtet sich vor Ort aus).
        path = [(40.0, 0.0, 0.0), (60.0, 0.0, 15.0)]
        out = _insert_jump_runups(path, self._FakeNav())
        assert out == path

    def test_runup_not_inserted_across_thin_wall(self):
        # Dünne Wand zwischen Run-up-Stelle und Absprungzelle → Run-up wird nicht eingefügt.
        wall = _make_box(cx=16.0, cy=0.0, bz=0.0, hw=0.5, hd=40.0, height=5.0)  # dünn, blockt z=0
        nav = self._FakeNav(obs=[wall])
        # Anfahrt (0,0)→Absprung (20,0), Sprung auf (40,0,z15). Run-up bei x≈16 läge AUF der Wand;
        # Segmente pred→runup / runup→launch kreuzen die Wand bei x=16.
        path = [(0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (40.0, 0.0, 15.0)]
        out = _insert_jump_runups(path, nav)
        assert out == path, "Run-up darf nicht quer zur dünnen Wand eingefügt werden"

    def test_runup_crosses_thin_wall_helper(self):
        wall = _make_box(cx=10.0, cy=0.0, bz=0.0, hw=0.5, hd=40.0, height=5.0)
        nav = self._FakeNav(obs=[wall])
        # Segment quert x=10 → True
        assert _runup_crosses_thin_wall(nav, 0.0, 0.0, 0.0, 6.0, 0.0, 20.0, 0.0) is True
        # Segmente vollständig rechts der Wand → False
        assert _runup_crosses_thin_wall(nav, 0.0, 12.0, 0.0, 16.0, 0.0, 20.0, 0.0) is False


# ---------------------------------------------------------------------------
# Pixel-on-Bodenauflage: get_floor_z mit overhang
# ---------------------------------------------------------------------------

class TestPixelOnFloor:
    """get_floor_z(overhang>0): der Tank bleibt getragen, bis seine Mitte ~overhang über die
    Plattformkante hinaus ist (Pixel-on-Regel)."""

    def test_overhang_extends_support_past_edge(self):
        plat = _make_box(cx=0.0, cy=0.0, bz=0.0, hw=20.0, hd=20.0, height=15.0)  # Kante bei x=20
        nav = NavGraph(_make_world(boxes=[plat], world_half=80.0))
        # Mittelpunkt-Test (overhang=0): jenseits x≈20.5 keine Auflage mehr
        assert nav.get_floor_z(20.4, 0.0, 15.0) == pytest.approx(15.0)
        assert nav.get_floor_z(21.5, 0.0, 15.0) == pytest.approx(0.0)
        # Pixel-on (overhang=1.4): noch getragen bei x=21.5 (Mitte 1.5u über Kante, < 0.5+1.4)
        assert nav.get_floor_z(21.5, 0.0, 15.0, overhang=1.4) == pytest.approx(15.0)
        # deutlich weiter draußen fällt auch Pixel-on weg
        assert nav.get_floor_z(23.0, 0.0, 15.0, overhang=1.4) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# HIX-Karte: Realistische Integrationstests (fixture-basiert)
# ---------------------------------------------------------------------------

def _executable_jump_margins(path, v0=19.0, g=9.8, tank_speed=25.0, edge_tol=JUMP_EDGE_TOL):
    """Laufzeit-Sprungmargen je Sprung-rauf-Übergang im Pfad — spiegelt das Feasibility-Gate des
    Bots (bzbot_ai._nav_jump_geometry_ok) exakt nach:

        disc   = v0² - 2·g·dz                 (disc < 0 → Dach zu hoch → -inf)
        t_desc = (v0 + √disc) / g
        margin = tank_speed·t_desc·1.1 - max(0, hdist - edge_tol)

    margin <= 0 → der Bot würde den Sprung zur Laufzeit als nicht ausführbar VERWERFEN (auch wenn
    der Pfad existiert). Konstanten = bzbot_ai-Defaults (JUMP_VELOCITY=19, GRAVITY=-9.8,
    _tank_speed=25). Gibt eine Liste der Margen (eine je Sprung-rauf) zurück."""
    margins = []
    for i in range(1, len(path)):
        dz = path[i][2] - path[i - 1][2]
        if dz <= 1.5:
            continue
        disc = v0 * v0 - 2.0 * g * dz
        if disc < 0:
            margins.append(float("-inf"))
            continue
        t_desc = (v0 + math.sqrt(disc)) / g
        hdist = math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1])
        eff = max(0.0, hdist - edge_tol)
        margins.append(tank_speed * t_desc * 1.1 - eff)
    return margins


class TestNavHix:
    """Realistische Navigationstests auf Basis eines HIX-Karten-Dumps.

    Fixtures erzeugen:
        python bzbot.py --host <server> --dump-raw tests/fixtures/hix

    Tests werden automatisch übersprungen wenn das Fixture fehlt.
    """

    @pytest.fixture(autouse=True)
    def _full_search_budget(self, monkeypatch):
        """Routing-QUALITÄTS-Tests prüfen VOLLSTÄNDIGE Pfade — unabhängig vom Produktions-Cap
        (ASTAR_MAX_EXPANSIONS=5000 / ASTAR_MAX_MS=60ms, der bewusst Best-Effort-Teilpfade liefert).
        Hier Budget hochsetzen, damit lange/hohe Routen in EINEM Plan komplett sind. Da die Kanten
        byte-identisch zum vollen Scan sind (s. test_vertical_candidate_lists_match_full_scan),
        verhält sich A* hier exakt wie vor der Limit-Senkung. Den Cap selbst testen die dedizierten
        Limit-/Zeitbudget-Tests in TestAstarHeuristic3D."""
        monkeypatch.setattr("bzflag.nav_graph.ASTAR_MAX_EXPANSIONS", 50000)
        monkeypatch.setattr("bzflag.nav_graph.ASTAR_MAX_MS", 1e9)

    @pytest.fixture(scope="class")
    def hix_nav(self):
        from tests.conftest import load_map_fixture
        from bzflag.nav_graph import NavGraph
        wm = load_map_fixture("hix")
        if wm is None:
            pytest.skip(
                "HIX-Fixture fehlt: "
                "'python bzbot.py --host <server> --dump-raw tests/fixtures/hix' ausführen"
            )
        return NavGraph(wm, max_jump_h=18.4)

    def test_hix_ground_to_z15_path_exists(self, hix_nav):
        """Plan findet Weg vom Boden (0,0,0) auf ein z=15-Dach.

        Ziel (90,70): begehbare Zelle der z=15-Plattform L17 abseits der Diagonal-
        Wandlinie (das frühere Ziel (80,80) liegt nach dem Pierce-Through-Fix exakt
        auf einer Wand und ist damit non-walkable)."""
        path = hix_nav.plan_path(0.0, 0.0, 0.0, 90.0, 70.0, goal_z=15.0)
        assert path, "Kein Pfad von Boden auf z=15-Plattform"
        assert any(abs(wp[2] - 15.0) < 1.0 for wp in path)

    def test_hix_z15_to_z30_path_exists(self, hix_nav):
        """Plan findet Weg von z=15-Dach auf z=30-Querbalken."""
        path = hix_nav.plan_path(90.0, 70.0, 15.0, 0.0, 0.0, goal_z=30.0)
        assert path, "Kein Pfad von z=15 auf z=30-Querbalken"
        assert any(abs(wp[2] - 30.0) < 1.0 for wp in path)

    def test_hix_z15_to_z30_edge_jump_executable(self, hix_nav):
        """Regression NAV-17: Der z=15→z=30-Sprung auf die 45°-Westrand-Plattform muss nicht nur
        EXISTIEREN, sondern für den Bot LAUFZEIT-AUSFÜHRBAR sein.

        Bot steht am Westrand der z=15-Plattform (L50), Gegner campt auf dem z=30-Diamanten (L53,
        Spitze ≈ −290.5,0). Vorher wurde als Sprung-Landewegpunkt die weit innen geklemmte Zellmitte
        (−300,0) ausgegeben → Feasibility-Marge nur +1.7u → Sprung am Rand verworfen → 30-s-Cooldown
        → A*-Expansion-Limit → Direktmodus → Sturz über die Kante. Fix: Wegpunkt = Footprint-
        Eintrittspunkt (Plattform-Spitze) → robuste Marge (~+11u)."""
        path = hix_nav.plan_path(-224.0, -8.0, 15.0, -302.0, 0.0, goal_z=30.0)
        assert path, "Kein Pfad von z=15-Westrand auf z=30-Randplattform"
        assert any(abs(wp[2] - 30.0) < 1.0 for wp in path), "Pfad endet nicht auf z=30"
        margins = _executable_jump_margins(path)
        assert margins, "Pfad enthält keinen Sprung-rauf-Übergang"
        worst = min(margins)
        assert worst >= 5.0, (
            f"z15→z30-Sprung nicht robust ausführbar: kleinste Marge {worst:.1f}u < 5.0u "
            f"(Margen {[round(m, 1) for m in margins]})")

    def test_hix_ground_to_z30_path_exists(self, hix_nav):
        """Plan findet Weg direkt vom Boden auf z=30-Querbalken (zwei Sprünge)."""
        path = hix_nav.plan_path(0.0, 0.0, 0.0, 0.0, 0.0, goal_z=30.0)
        assert path, "Kein Pfad von Boden auf z=30-Querbalken"
        assert any(abs(wp[2] - 30.0) < 1.0 for wp in path)

    def test_hix_ground_to_z30_edge_platforms(self, hix_nav):
        """Request 2 / NAV-17: Die vier 45°-gedrehten z=30-Randplattformen (±340,0 / 0,±340) müssen
        vom Boden erreichbar sein (Pixel-on-Sprung von der z=15-Plattform auf die Spitze) UND jeder
        Sprung im Pfad muss laufzeit-ausführbar sein (Marge ≥ 5u), nicht nur „Pfad existiert".
        Früher: 'A* Expansion-Limit' bzw. Sprung-Landewegpunkt zu weit innen (Marge ~+2u → am
        Feasibility-Rand verworfen)."""
        for gx, gy in [(340.0, 0.0), (-340.0, 0.0), (0.0, 340.0), (0.0, -340.0)]:
            path = hix_nav.plan_path(0.0, 0.0, 0.0, gx, gy, goal_z=30.0)
            assert path, f"Kein Pfad auf z=30-Randplattform ({gx},{gy})"
            assert abs(path[-1][2] - 30.0) < 1.0
            margins = _executable_jump_margins(path)
            assert margins, f"Pfad auf ({gx},{gy}) enthält keinen Sprung"
            worst = min(margins)
            assert worst >= 5.0, (
                f"Sprung auf z=30-Randplattform ({gx},{gy}) nicht robust ausführbar: "
                f"kleinste Marge {worst:.1f}u < 5.0u (Margen {[round(m, 1) for m in margins]})")

    # ── Fix 1: gleich-hohe Dach-Flächen verbinden (Tor statt Sprung) ──────────

    @staticmethod
    def _ascents(path):
        return [(path[i], path[i + 1]) for i in range(len(path) - 1)
                if path[i + 1][2] - path[i][2] > 1.5]

    def test_plan_path_prefers_teleport_to_perimeter_near_gate(self, hix_nav):
        """Fix 1: Bot am Boden dicht an einem Eck-Tor, Gegner auf dem z=30-Perimeter-Ring nahe dem
        Tor. Der Aufstieg läuft übers Tor (Landung auf _tele_exit_wps), NICHT per Sprung. Vorher:
        Perimeter-Ring war eine vom Tor-Ausgang getrennte Insel → A* sprang."""
        nav = hix_nav
        for gx, gy in [(390.0, 390.0), (392.5, 360.0), (392.5, 330.0)]:
            path = nav.plan_path(379.0, 379.0, 0.0, gx, gy, goal_z=30.0)
            assert path and path[-1][2] == pytest.approx(30.0, abs=1.0), \
                f"kein Pfad auf z=30 zum Perimeter-Ziel ({gx},{gy})"
            ascents = self._ascents(path)
            assert ascents, f"kein Aufstieg z=0→z=30 im Pfad zu ({gx},{gy})"
            for _a, b in ascents:
                assert (round(b[0], 1), round(b[1], 1)) in nav._tele_exit_wps, \
                    f"Aufstieg zu ({gx},{gy}) ist ein Sprung statt ein Tor-Durchgang"

    def test_same_z_neighbors_connects_adjacent_layers(self, hix_nav):
        """Fix 1 / Konnektivität: _same_z_neighbors liefert für eine z≈30-Dach-Layer mindestens eine
        Kante auf eine ANGRENZENDE Layer GLEICHER Höhe (physisch durchgehende Fläche). Vorher: keine
        Kante zwischen gleich hohen Layern → getrennte Inseln."""
        nav = hix_nav
        z30 = [lid for lid, l in enumerate(nav.layers) if abs(l.z - 30.0) < 0.5]
        assert len(z30) >= 2, "HIX-Fixture sollte mehrere z=30-Layer haben"
        found_cross = False
        for lid in z30:
            layer = nav.layers[lid]
            for iy in range(layer.n_y):
                for ix in range(layer.n_x):
                    if not layer.walkable[iy][ix]:
                        continue
                    wx, wy = layer.cell_to_world(ix, iy)
                    nbrs = nav._same_z_neighbors(lid, ix, iy, wx, wy, layer)
                    if any(n[0] != lid and abs(nav.layers[n[0]].z - layer.z) < 0.1
                           for (n, _c) in nbrs):
                        found_cross = True
                        break
                if found_cross:
                    break
            if found_cross:
                break
        assert found_cross, "_same_z_neighbors verbindet keine angrenzenden gleich hohen Layer"

    def test_plan_path_along_perimeter_stays_on_z30(self, hix_nav):
        """Fix 1: Weg auf dem z=30-Perimeter über eine Ecke (zwei sich berührende Mauer-Layer) bleibt
        OBEN. Vorher fiel der Pfad auf z=0 und stieg wieder auf (Inseln im Graph)."""
        nav = hix_nav
        path = nav.plan_path(392.5, 360.0, 30.0, 360.0, 392.5, goal_z=30.0)
        assert path, "kein z=30→z=30-Pfad über die Perimeter-Ecke"
        assert min(wp[2] for wp in path) >= 28.0, \
            f"Pfad fällt auf z=0 statt oben zu bleiben (min z={min(wp[2] for wp in path):.1f})"

    def test_plan_path_isolated_platform_still_jumps(self, hix_nav):
        """Gegenprobe: die ISOLIERTE z=30-Eck-Plattform (340,0) hat keine berührende Nachbar-Layer
        → _same_z_neighbors verbindet sie NICHT; der finale Aufstieg dorthin bleibt ein Sprung."""
        nav = hix_nav
        path = nav.plan_path(0.0, 0.0, 0.0, 340.0, 0.0, goal_z=30.0)
        assert path and path[-1][2] == pytest.approx(30.0, abs=1.0), \
            "kein Pfad auf die isolierte z=30-Eck-Plattform"
        ascents = self._ascents(path)
        assert ascents, "kein Aufstieg im Pfad zur Eck-Plattform"
        # Der LETZTE Aufstieg (auf die isolierte Plattform) ist ein Sprung, kein Tor-Durchgang.
        _a, b = ascents[-1]
        assert (round(b[0], 1), round(b[1], 1)) not in nav._tele_exit_wps, \
            "Aufstieg auf die isolierte Plattform sollte ein Sprung sein"

    def test_vertical_candidate_lists_match_full_scan(self, hix_nav):
        """Teil B Regression auf der ECHTEN HIX-Karte (57 Layer, große rotierte Diagonalwände):
        _vertical_neighbors mit vorberechneten Kandidaten liefert dieselben Kanten wie der volle
        Layer-Scan — Stichprobe begehbarer Knoten pro Layer. (Mutiert die Fixture nur temporär.)"""
        nav = hix_nav
        nodes = TestVerticalAdjacencyPrecompute._all_nodes(nav, per_layer_cap=40)
        real, full = TestVerticalAdjacencyPrecompute._real_vs_full(nav, nodes)
        mism = [k for k in real if real[k] != full[k]]
        assert not mism, f"{len(mism)} HIX-Knoten mit abweichenden vertikalen Kanten (Cull unsicher)"


# ---------------------------------------------------------------------------
# _segment_crosses_thin_obs: Slab-Test gegen einzelne dünne Obstacle
# ---------------------------------------------------------------------------

class TestSegmentCrossesThinObs:
    """Tests für den 2D-Slab-Test gegen dünne Obstacles."""

    def _thin_wall(self, angle=0.0):
        """Achsenparallele dünne Wand: Breite 1u (half_w=0.5), Länge 40u (half_d=20)."""
        return _make_box(0.0, 0.0, 0.0, 0.5, 20.0, 5.0, angle=angle)

    def test_perpendicular_crossing_detected(self):
        # Segment von x=-10 bis x=+10 bei y=0 kreuzt die Wand bei x=0
        assert _segment_crosses_thin_obs(-10.0, 0.0, 10.0, 0.0, self._thin_wall())

    def test_same_side_no_crossing(self):
        # Segment bleibt komplett links der Wand (x ≤ -1.0, Wand endet bei x=-0.5)
        assert not _segment_crosses_thin_obs(-10.0, 0.0, -1.0, 0.0, self._thin_wall())

    def test_endpoint_at_wall_edge_is_crossing(self):
        # Segment endet exakt an der Wand-Außenkante — Berührung gilt als Crossing
        assert _segment_crosses_thin_obs(-10.0, 0.0, -0.5, 0.0, self._thin_wall())

    def test_rotated_wall_crossing(self):
        # 45°-rotierte Wand — Segment von SW nach NO kreuzt sie
        assert _segment_crosses_thin_obs(-10.0, -10.0, 10.0, 10.0, self._thin_wall(math.pi / 4))

    def test_rotated_wall_miss(self):
        # 45°-rotierte Wand — Segment weit außerhalb
        assert not _segment_crosses_thin_obs(20.0, 0.0, 30.0, 10.0, self._thin_wall(math.pi / 4))


# ---------------------------------------------------------------------------
# _thin_blocked: Vorberechnung verbotener Wegpunkt-Paare
# ---------------------------------------------------------------------------

class TestThinBlocked:
    """Tests für NavGraph._precompute_thin_wall_blocked und self._thin_blocked."""

    def test_thin_obstacle_populates_set(self):
        # half_w=0.5 < CELL_SIZE/2=2 → dünnes Obstacle → muss _thin_blocked befüllen
        thin = _make_box(0.0, 0.0, 0.0, 0.5, 20.0, 5.0)
        wm = _make_world(boxes=[thin], world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        assert len(ng._thin_blocked) > 0

    def test_thick_obstacle_does_not_populate_set(self):
        # half_w=10 >> CELL_SIZE/2=2 → kein dünnes Obstacle
        thick = _make_box(0.0, 0.0, 0.0, 10.0, 10.0, 5.0)
        wm = _make_world(boxes=[thick], world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        assert len(ng._thin_blocked) == 0

    def test_empty_world_no_thin_blocked(self):
        wm = _make_world(boxes=[], world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        assert len(ng._thin_blocked) == 0

    def test_set_contains_both_directions(self):
        # Für jedes Paar (A→B) muss auch (B→A) im Set sein
        thin = _make_box(0.0, 0.0, 0.0, 0.5, 20.0, 5.0)
        wm = _make_world(boxes=[thin], world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        for wx1, wy1, wx2, wy2, z in list(ng._thin_blocked)[:10]:
            assert (wx2, wy2, wx1, wy1, z) in ng._thin_blocked

    def test_astar_does_not_route_through_thin_wall(self):
        """A* darf kein konsekutives Paar im Pfad haben, das eine dünne Wand kreuzt."""
        thin = _make_box(0.0, 0.0, 0.0, 0.5, 20.0, 5.0)  # Wand entlang Y bei x=0
        wm = _make_world(boxes=[thin], world_half=50.0)
        ng = NavGraph(wm, max_jump_h=18.4)
        assert len(ng._thin_blocked) > 0, "Voraussetzung: _thin_blocked muss befüllt sein"

        path = ng.plan_path(-20.0, 0.0, 0.0, 20.0, 0.0, 0.0)
        assert len(path) >= 2, "Pfad von links nach rechts muss existieren"

        for i in range(len(path) - 1):
            ax, ay, az = path[i]
            bx, by, _ = path[i + 1]
            assert (ax, ay, bx, by, az) not in ng._thin_blocked, (
                f"Pfadsegment {i}→{i+1} kreuzt dünne Wand: "
                f"({ax:.1f},{ay:.1f})→({bx:.1f},{by:.1f})"
            )


# ---------------------------------------------------------------------------
# _obstacle_blocks_layer + Durchstoß-Wände (Kernfix)
# ---------------------------------------------------------------------------

class TestObstacleBlocksLayer:
    """Vertikal-Überlappungs-Filter und Blockierung durchstoßender Wände."""

    def test_blocks_layer_at_own_base(self):
        # Aufbau auf Dachhöhe (bottom_z == roof_z) sperrt diese Etage
        obs = _make_box(0.0, 0.0, 15.0, 1.0, 5.0, 5.0)  # z=15..20
        assert _obstacle_blocks_layer(obs, 15.0)

    def test_pierce_through_blocks_lower_layer_not_top(self):
        # Wand z=14..30: sperrt z=15 (Tank steckt drin), aber nicht z=30 (fährt drüber)
        wall = _make_box(0.0, 0.0, 14.0, 0.5, 20.0, 16.0)
        assert _obstacle_blocks_layer(wall, 15.0), "z=15 muss gesperrt sein"
        assert not _obstacle_blocks_layer(wall, 30.0), "z=30 (== Oberkante) nicht sperren"

    def test_low_obstacle_does_not_block_high_layer(self):
        # Niedriges Obstacle z=0..5 sperrt z=15 nicht (genug Kopffreiheit)
        low = _make_box(0.0, 0.0, 0.0, 5.0, 5.0, 5.0)
        assert _obstacle_blocks_layer(low, 0.0)
        assert not _obstacle_blocks_layer(low, 15.0)

    def test_pierce_through_wall_blocks_roof_cells(self):
        """Eine Wand, die unter dem Dach beginnt und durchstößt, macht die
        Wandzellen auf dem Dach non-walkable — seitlich bleibt aber Platz."""
        platform = _make_box(0.0, 0.0, 0.0, 30.0, 30.0, 15.0)  # Dach bei z=15
        wall = _make_box(0.0, 0.0, 14.0, 0.5, 20.0, 16.0)      # dünn, z=14..30, entlang Y
        wm = _make_world(boxes=[platform, wall], world_half=80.0)
        ng = NavGraph(wm, max_jump_h=18.4)

        roof = next((l for l in ng.layers if abs(l.z - 15.0) < 0.5), None)
        assert roof is not None, "z=15-Dach-Layer muss existieren"

        # Zelle direkt auf der Wandlinie (x≈0) ist blockiert
        cix, ciy = roof.world_to_cell(0.0, 0.0)
        assert not roof.walkable[ciy][cix], "Wandzelle auf dem Dach muss blockiert sein"

        # Seitlich (x=±8, > 0.5+THIN_WALL_MARGIN) bleibt begehbar (seitlich befahrbar)
        for sx in (8.0, -8.0):
            six, siy = roof.world_to_cell(sx, 0.0)
            assert roof.walkable[siy][six], f"Zelle bei x={sx} sollte begehbar sein"


# ---------------------------------------------------------------------------
# HIX-Karte: thin_blocked Timing + Korrektheit (fixture-basiert)
# ---------------------------------------------------------------------------

class TestNavHixThinWall:
    """HIX-spezifische Tests für thin_blocked: Timing-Messung und Pfad-Korrektheit.

    Wird automatisch übersprungen wenn tests/fixtures/hix.bin fehlt.
    Timing-Ausgaben erscheinen mit: pytest -s tests/test_nav_graph.py::TestNavHixThinWall
    """

    @pytest.fixture(scope="class")
    def hix_nav(self):
        from tests.conftest import load_map_fixture
        wm = load_map_fixture("hix")
        if wm is None:
            pytest.skip(
                "HIX-Fixture fehlt: "
                "'python bzbot.py --host <server> --dump-raw tests/fixtures/hix' ausführen"
            )
        return NavGraph(wm, max_jump_h=18.4)

    @pytest.mark.perf
    def test_hix_thin_blocked_precomputation_timing(self):
        """Misst die NavGraph-Build-Zeit inklusive _precompute_thin_wall_blocked.

        Ausgabe via pytest -s: [TIMING] HIX NavGraph build: Xms
        Kein assert auf maximale Zeit — nur Dokumentation für Performance-Baseline.
        """
        import time
        from tests.conftest import load_map_fixture
        from bzflag.nav_graph import NavGraph, invalidate_nav_cache

        wm = load_map_fixture("hix")
        if wm is None:
            pytest.skip("HIX-Fixture fehlt")

        times = []
        ng = None
        for _ in range(3):
            invalidate_nav_cache(wm.world_hash or "")
            t0 = time.perf_counter()
            ng = NavGraph(wm, max_jump_h=18.4)
            times.append(time.perf_counter() - t0)

        avg_ms = sum(times) / len(times) * 1000
        n_pairs = len(ng._thin_blocked) // 2
        print(f"\n[TIMING] HIX NavGraph build (3 Runs avg): {avg_ms:.1f}ms")
        print(f"[TIMING] thin_blocked Paare: {n_pairs}")

    @pytest.mark.perf
    def test_hix_plan_path_timing(self, hix_nav):
        """Misst wie lange plan_path für typische HIX-Pfade braucht.

        Ausgabe via pytest -s: [TIMING] HIX plan_path: Xms pro Aufruf
        Kein assert auf maximale Zeit — nur Dokumentation für Performance-Baseline.
        """
        import time

        routes = [
            (0.0, 0.0, 0.0, 300.0, 300.0, 0.0),
            (-300.0, 0.0, 0.0, 300.0, 0.0, 0.0),
            (200.0, -200.0, 0.0, -200.0, 200.0, 0.0),
        ]
        n_runs = 20
        total = 0.0
        for route in routes:
            for _ in range(n_runs):
                t0 = time.perf_counter()
                hix_nav.plan_path(*route)
                total += time.perf_counter() - t0

        avg_ms = total / (len(routes) * n_runs) * 1000
        print(
            f"\n[TIMING] HIX plan_path "
            f"({len(routes)} Routen × {n_runs} Runs avg): {avg_ms:.2f}ms pro Aufruf"
        )

    def test_hix_no_path_segment_crosses_thin_wall(self, hix_nav):
        """Kein konsekutives Wegpunkt-Paar im Pfad darf in thin_blocked sein.

        Enthält neben Bodenrouten auch z=15-Routen auf/zwischen den Plattformen mit
        den diagonalen Durchstoß-Wänden — das ist das eigentliche Bug-Szenario."""
        if not hix_nav._thin_blocked:
            pytest.skip("HIX hat keine dünnen Wände — thin_blocked leer")

        # (sx, sy, sz, gx, gy, [goal_z]) — z=15-Routen exerzieren die Diagonalwände
        routes = [
            (0.0, 0.0, 0.0, 300.0, 300.0, None),
            (-300.0, 0.0, 0.0, 300.0, 0.0, None),
            (200.0, -200.0, 0.0, -200.0, 200.0, None),
            (0.0, 0.0, 0.0, -300.0, -300.0, None),
            (0.0, 0.0, 0.0, 90.0, 70.0, 15.0),       # Boden → z=15-Plattform
            (90.0, 70.0, 15.0, -90.0, -70.0, 15.0),  # z=15 → z=15 quer über Wände
            (90.0, 70.0, 15.0, 0.0, 0.0, 30.0),      # z=15 → z=30 (über Wand drüber)
        ]
        violations = []
        for route in routes:
            sx, sy, sz, gx, gy, gz = route
            path = hix_nav.plan_path(sx, sy, sz, gx, gy, goal_z=gz)
            for i in range(len(path) - 1):
                ax, ay, az = path[i]
                bx, by, _ = path[i + 1]
                if (ax, ay, bx, by, az) in hix_nav._thin_blocked:
                    violations.append(
                        f"Route ({sx:.0f},{sy:.0f})→({gx:.0f},{gy:.0f}) "
                        f"Segment {i}: ({ax:.1f},{ay:.1f})→({bx:.1f},{by:.1f})"
                    )
        assert not violations, "Pfade kreuzen dünne Wände:\n" + "\n".join(violations)

    def test_hix_z15_beams_seitlich_befahrbar(self, hix_nav):
        """Die diagonalen z=15-Laufstege (Source hw=8, hd=107) behalten begehbare Zellen.

        Beleg für die bewusste Entscheidung 'seitlich befahrbar' (THIN_WALL_MARGIN):
        mit vollem TANK_MARGIN verschwänden diese Laufstege komplett, weil die mittige
        Wand keinen Platz ließe. Sie müssen also >0 Zellen behalten — aber wegen der
        mittigen Wand auch deutlich weniger als ein ungestörtes Dach."""
        beams = [
            l for l in hix_nav.layers
            if abs(l.z - 15.0) < 0.5
            and l.source_obstacle is not None
            and abs(l.source_obstacle.half_w - 8.0) < 0.5
            and abs(l.source_obstacle.half_d - 107.0) < 0.5
        ]
        assert len(beams) == 4, f"Erwartet 4 Diagonal-Laufstege, gefunden {len(beams)}"
        for l in beams:
            walkable = sum(1 for row in l.walkable for c in row if c)
            assert walkable > 0, (
                f"Laufsteg ({l.cx:.0f},{l.cy:.0f}) komplett blockiert — "
                f"THIN_WALL_MARGIN zu groß?"
            )


# ---------------------------------------------------------------------------
# Server-Variablen-Physik (_jumpVelocity/_gravity → _v0/_g/_max_jump_h)
# ---------------------------------------------------------------------------

class TestServerVarPhysics:
    """v0/g fließen aus den globalen Server-Variablen in Höhen-Gate UND Bogen-Timing; set_physics()
    reicht spät eintreffende MsgSetVar nach. Kein per-Flag-WG/LG-Verhalten (bewusst out of scope)."""

    def test_max_jump_h_derived_from_v0_g(self):
        wm = _make_world()
        ng = NavGraph(wm, v0=25.0, g=9.8)
        assert ng._v0 == pytest.approx(25.0)
        assert ng._g == pytest.approx(9.8)
        assert ng._max_jump_h == pytest.approx(25.0 ** 2 / (2.0 * 9.8))

    def test_explicit_max_jump_h_backward_compatible(self):
        # Bestehende Tests pinnen max_jump_h=18.4 mit Default-v0/g → bleibt erhalten.
        wm = _make_world()
        ng = NavGraph(wm, max_jump_h=18.4)
        assert ng._max_jump_h == pytest.approx(18.4)
        assert ng._v0 == pytest.approx(19.0)

    def test_v0_extends_jump_up_candidates(self):
        # Dach bei z=25 liegt zwischen max_jump_h(v0=19)≈18.4 und max_jump_h(v0=25)≈31.9.
        box = _make_box(0.0, 0.0, 0.0, 15.0, 15.0, 25.0)
        wm = _make_world(boxes=[box])
        ng_low = NavGraph(wm)                     # v0=19 → Dach unerreichbar
        ng_high = NavGraph(wm, v0=25.0, g=9.8)    # v0=25 → Dach erreichbar
        roof_lid = next(i for i, l in enumerate(ng_low.layers) if abs(l.z - 25.0) < 0.5)
        assert roof_lid not in ng_low._jump_up_cands.get(0, [])
        assert roof_lid in ng_high._jump_up_cands.get(0, [])

    def test_set_physics_updates_candidates_and_clears_cache(self):
        box = _make_box(0.0, 0.0, 0.0, 15.0, 15.0, 25.0)
        wm = _make_world(boxes=[box])
        ng = NavGraph(wm)                          # v0=19
        roof_lid = next(i for i, l in enumerate(ng.layers) if abs(l.z - 25.0) < 0.5)
        assert roof_lid not in ng._jump_up_cands.get(0, [])
        ng._vn_cache[(0, 0, 0, 25.0)] = ["dummy"]  # Cache befüllen
        ng.set_physics(25.0, 9.8)
        assert ng._v0 == pytest.approx(25.0)
        assert ng._max_jump_h == pytest.approx(25.0 ** 2 / (2.0 * 9.8))
        assert roof_lid in ng._jump_up_cands.get(0, [])   # Kandidaten neu gebaut
        assert ng._vn_cache == {}                          # Sprungkanten-Cache geleert

    def test_set_physics_noop_when_unchanged(self):
        wm = _make_world()
        ng = NavGraph(wm)                          # v0=19, g=9.8
        ng._vn_cache[(0, 0, 0, 25.0)] = ["dummy"]
        ng.set_physics(19.0, 9.8)                  # unverändert → No-Op
        assert ng._vn_cache == {(0, 0, 0, 25.0): ["dummy"]}
