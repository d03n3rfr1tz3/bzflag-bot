"""
Tests für bzflag/shot_physics.py.

Verifiziert die portierten BZFlag-Algorithmen:
  reflect(), get_box_normal(), get_pyramid_normal(),
  ray_box_hit(), ray_pyramid_hit(), simulate_shot_path().
"""
import math
import pytest
from bzflag.shot_physics import (
    reflect, can_ricochet,
    get_box_normal, get_pyramid_normal,
    ray_box_hit, ray_pyramid_hit,
    simulate_shot_path, Segment,
)
from bzflag.world_map import BoxObstacle


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _box(cx=0.0, cy=0.0, bz=0.0, angle=0.0, hw=5.0, hd=5.0, height=5.0,
         is_pyr=False, z_flip=False, ricochet=False, shoot_through=False):
    return BoxObstacle(cx=cx, cy=cy, bottom_z=bz, angle=angle,
                       half_w=hw, half_d=hd, height=height,
                       is_pyramid=is_pyr, z_flip=z_flip,
                       ricochet=ricochet, shoot_through=shoot_through)


def _approx(a, b, tol=1e-4):
    return abs(a - b) < tol


def _vec_approx(va, vb, tol=1e-4):
    return all(_approx(a, b, tol) for a, b in zip(va, vb))


def _unit(vx, vy, vz):
    n = math.sqrt(vx**2 + vy**2 + vz**2)
    return vx/n, vy/n, vz/n


# ---------------------------------------------------------------------------
# reflect()
# ---------------------------------------------------------------------------

def test_reflect_horizontal_floor():
    """Schuss von oben auf Boden → Z-Komponente kehrt sich um."""
    vx, vy, vz = reflect(0.0, 0.0, -1.0,  0.0, 0.0, 1.0)
    assert _approx(vx, 0.0)
    assert _approx(vy, 0.0)
    assert _approx(vz, 1.0)


def test_reflect_east_wall():
    """Schuss nach Osten an Ostwand (normal = -1,0,0) → X umkehren."""
    vx, vy, vz = reflect(1.0, 0.0, 0.0,  -1.0, 0.0, 0.0)
    assert _approx(vx, -1.0)
    assert _approx(vy,  0.0)
    assert _approx(vz,  0.0)


def test_reflect_45_degrees():
    """45°-Schuss auf vertikale Wand → Y bleibt, X negiert."""
    spd = 1.0 / math.sqrt(2)
    vx, vy, vz = reflect(spd, spd, 0.0,  -1.0, 0.0, 0.0)
    assert _approx(vx, -spd)
    assert _approx(vy,  spd)
    assert _approx(vz,  0.0)


def test_reflect_perpendicular():
    """Senkrechter Einfall → Schuss kehrt genau zurück."""
    vx, vy, vz = reflect(-3.0, 0.0, 0.0,  1.0, 0.0, 0.0)
    assert _approx(vx, 3.0)
    assert _approx(vy, 0.0)
    assert _approx(vz, 0.0)


# ---------------------------------------------------------------------------
# can_ricochet()
# ---------------------------------------------------------------------------

def test_can_ricochet_normal_no_server():
    assert not can_ricochet(b"\x00\x00", is_gm=False, is_sw=False, server_ricochet=False)


def test_can_ricochet_r_flag():
    assert can_ricochet(b"R\x00", is_gm=False, is_sw=False, server_ricochet=False)


def test_can_ricochet_server_ricochet():
    assert can_ricochet(b"\x00\x00", is_gm=False, is_sw=False, server_ricochet=True)


def test_can_ricochet_gm_never():
    assert not can_ricochet(b"GM", is_gm=True, is_sw=False, server_ricochet=True)


def test_can_ricochet_sw_never():
    assert not can_ricochet(b"SW", is_gm=False, is_sw=True, server_ricochet=True)


def test_can_ricochet_sb_never():
    """SB (Super Bullet) benutzt Through-Physik — kein Bounce auch bei server_ricochet."""
    assert not can_ricochet(b"SB", is_gm=False, is_sw=False, server_ricochet=True)
    assert not can_ricochet(b"SB", is_gm=False, is_sw=False, server_ricochet=False)


def test_can_ricochet_pz_only_when_phantomized():
    """PZ-Schüsse prallen nur ab wenn der Schütze NICHT aktiv phantomzoned ist."""
    assert not can_ricochet(b"PZ", False, False, True, is_phantom_zoned=True)
    assert     can_ricochet(b"PZ", False, False, True, is_phantom_zoned=False)
    assert not can_ricochet(b"PZ", False, False, False, is_phantom_zoned=False)


def test_laser_bounce_near_origin():
    """Laser-Abpraller an Wand 10u vom Abschusspunkt — war vorher durch alten _EPSILON=1e-3 unsichtbar."""
    # Laser bei speed=100_000 u/s: alter eps=1e-3 s = 100 Einheiten Blindspot
    # Neuer eps=0.1/100_000=1e-6 s → Wand bei x=10 muss erkannt werden
    laser_speed = 100_000.0  # u/s
    wall = _box(cx=15.0, cy=0.0, bz=0.0, hw=5.0, hd=50.0, height=20.0, ricochet=True)
    segs = simulate_shot_path(
        pos=(0.0, 0.0, 1.0),
        vel=(laser_speed, 0.0, 0.0),
        fire_time=0.0,
        lifetime=0.1,     # LASER_AD_LIFE
        flag_abbr=b"\x00\x00",
        obstacles=[wall],
        world_half=400.0,
        server_ricochet=True,
    )
    # Erster Bounce muss erkannt worden sein (≥2 Segmente)
    assert len(segs) >= 2, f"Erwartet ≥2 Segmente (Bounce erkannt), erhalten: {len(segs)}"
    # Erstes Segment endet an der Westwand der Box (x≈10)
    assert abs(segs[0].ex - 10.0) < 1.0, f"Bounce-Punkt ex={segs[0].ex:.2f} ≠ ~10"


# ---------------------------------------------------------------------------
# get_box_normal()
# ---------------------------------------------------------------------------

def test_box_normal_floor():
    box = _box(bz=0.0, height=5.0)
    n = get_box_normal(0.0, 0.0, 0.0, box)
    assert _vec_approx(n, (0.0, 0.0, -1.0))


def test_box_normal_ceiling():
    box = _box(bz=0.0, height=5.0)
    n = get_box_normal(0.0, 0.0, 5.0, box)
    assert _vec_approx(n, (0.0, 0.0, 1.0))


def test_box_normal_east_wall():
    """Auftreffpunkt östlich der Box-Mitte → Normale zeigt nach Osten."""
    box = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0)
    n = get_box_normal(5.0, 0.0, 2.5, box)   # auf Ostwand
    assert _approx(n[0], 1.0, tol=0.01)
    assert _approx(n[1], 0.0, tol=0.01)
    assert _approx(n[2], 0.0, tol=0.01)


def test_box_normal_north_wall():
    box = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0)
    n = get_box_normal(0.0, 5.0, 2.5, box)   # auf Nordwand
    assert _approx(n[0], 0.0, tol=0.01)
    assert _approx(n[1], 1.0, tol=0.01)
    assert _approx(n[2], 0.0, tol=0.01)


def test_box_normal_rotated_45():
    """Box um 45° gedreht — Ostwand liegt nun in NE-Richtung."""
    box = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0, angle=math.pi/4)
    # Auftreffpunkt auf der NE-Wand: (5*cos45, 5*sin45)
    hit_x = 5.0 * math.cos(math.pi/4)
    hit_y = 5.0 * math.sin(math.pi/4)
    n = get_box_normal(hit_x, hit_y, 2.5, box)
    # Normale soll in NE-Richtung zeigen
    assert n[0] > 0.0
    assert n[1] > 0.0
    assert _approx(n[2], 0.0, tol=0.01)


# ---------------------------------------------------------------------------
# get_pyramid_normal()
# ---------------------------------------------------------------------------

def test_pyramid_normal_base_below():
    """Auftreffpunkt direkt an der Basis (pz=bottom_z) → Boden-Normale."""
    pyr = _box(bz=0.0, height=5.0, is_pyr=True)
    n = get_pyramid_normal(0.0, 0.0, 0.0, pyr)
    assert _vec_approx(n, (0.0, 0.0, -1.0))


def test_pyramid_normal_east_face_has_z_component():
    """Schrägfläche der Pyramide: Normale hat positive Z-Komponente."""
    pyr = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0, is_pyr=True)
    # Treffer auf halber Höhe, östliche Fläche
    n = get_pyramid_normal(2.5, 0.0, 2.5, pyr)
    assert n[0] > 0.0     # zeigt nach Osten
    assert n[2] > 0.0     # zeigt nach oben (Schrägfläche)
    length = math.sqrt(n[0]**2 + n[1]**2 + n[2]**2)
    assert _approx(length, 1.0, tol=0.01)


def test_pyramid_zflip_base_above():
    """Invertierte Pyramide (Spitze unten): Basis-Normale zeigt nach oben."""
    pyr = _box(bz=0.0, height=5.0, is_pyr=True, z_flip=True)
    # Bei z_flip ist die Basis oben (z=height)
    n = get_pyramid_normal(0.0, 0.0, 5.0, pyr)
    assert _vec_approx(n, (0.0, 0.0, 1.0))


def test_pyramid_zflip_face_nz_negative():
    """Invertierte Pyramide: Schrägfläche hat negative Z-Komponente."""
    pyr = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0,
               is_pyr=True, z_flip=True)
    n = get_pyramid_normal(2.5, 0.0, 2.5, pyr)
    assert n[0] > 0.0
    assert n[2] < 0.0    # z_flip negiert nz


# ---------------------------------------------------------------------------
# ray_box_hit()
# ---------------------------------------------------------------------------

def test_ray_box_hit_east_face():
    """Ray von West nach Ost trifft Ostwand."""
    box = _box(cx=10.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0)
    # Ray startet bei x=0, fährt in +X Richtung
    result = ray_box_hit(0.0, 0.0, 2.5,  1.0, 0.0, 0.0,  box)
    assert result is not None
    t, nx, ny, nz = result
    assert _approx(t, 5.0)          # trifft Westwand der Box bei x=5 → t=5
    assert _approx(nx, -1.0, 0.01)  # Westwand → Normale zeigt nach Westen
    assert _approx(ny,  0.0, 0.01)
    assert _approx(nz,  0.0, 0.01)


def test_ray_box_hit_miss():
    """Ray fährt an Box vorbei."""
    box = _box(cx=10.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0)
    result = ray_box_hit(0.0, 20.0, 2.5,  1.0, 0.0, 0.0,  box)
    assert result is None


def test_ray_box_hit_ceiling():
    """Ray von unten trifft Boxdecke."""
    box = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=2.0, height=3.0)
    result = ray_box_hit(0.0, 0.0, 0.0,  0.0, 0.0, 1.0,  box)
    assert result is not None
    t, nx, ny, nz = result
    assert _approx(t, 2.0)          # Boden der Box bei z=2
    assert _approx(nz, -1.0, 0.01)  # Boden-Normale zeigt nach unten


def test_ray_box_hit_rotated():
    """Ray trifft rotierte Box — Normale ist in Weltkoordinaten."""
    angle = math.pi / 4
    box = _box(cx=20.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0, angle=angle)
    # Ray horizontal von links
    result = ray_box_hit(0.0, 0.0, 2.5,  1.0, 0.0, 0.0,  box)
    assert result is not None
    _, nx, ny, nz = result
    # Normale liegt in XY-Ebene
    assert _approx(nz, 0.0, 0.01)
    # Normale zeigt nach Westen (in Richtung Schussherkunft)
    assert nx < 0.0


def test_ray_box_no_hit_from_inside():
    """Ray startet innerhalb der Box → None (kein Bounce von innen)."""
    box = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0)
    result = ray_box_hit(0.0, 0.0, 2.5,  1.0, 0.0, 0.0,  box)
    assert result is None


# ---------------------------------------------------------------------------
# ray_pyramid_hit()
# ---------------------------------------------------------------------------

def test_ray_pyramid_hit_slant_face():
    """Ray von Seite trifft Schrägfläche der Pyramide."""
    pyr = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0, is_pyr=True)
    # Ray kommt von Osten (x=20) in -X Richtung auf halber Höhe
    result = ray_pyramid_hit(20.0, 0.0, 2.5,  -1.0, 0.0, 0.0,  pyr)
    assert result is not None
    t, nx, ny, nz = result
    assert t > 0.0
    assert nx > 0.0      # Normale zeigt nach Osten
    assert nz > 0.0      # Z-Komponente vorhanden (Schrägfläche)


def test_ray_pyramid_hit_miss_above():
    """Ray fliegt über die Pyramidenspitze hinweg."""
    pyr = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0, is_pyr=True)
    result = ray_pyramid_hit(20.0, 0.0, 10.0,  -1.0, 0.0, 0.0,  pyr)
    assert result is None


def test_ray_pyramid_zflip():
    """Invertierte Pyramide: Ray trifft Schrägfläche von unten."""
    pyr = _box(cx=0.0, cy=0.0, hw=5.0, hd=5.0, bz=0.0, height=5.0,
               is_pyr=True, z_flip=True)
    # Spitze unten: von Osten kommend auf halber Höhe
    result = ray_pyramid_hit(20.0, 0.0, 2.5,  -1.0, 0.0, 0.0,  pyr)
    assert result is not None
    t, nx, ny, nz = result
    assert nx > 0.0
    assert nz < 0.0     # z_flip → nz negiert


# ---------------------------------------------------------------------------
# simulate_shot_path()
# ---------------------------------------------------------------------------

def _straight_shot(pos=(0.0, 0.0, 1.0), vel=(100.0, 0.0, 0.0),
                   lifetime=2.0, flag=b"\x00\x00",
                   obstacles=None, world_half=200.0, server_rico=False,
                   fire_time=0.0):
    return simulate_shot_path(pos, vel, fire_time, lifetime, flag,
                               obstacles or [], world_half, server_rico)


def test_straight_path_no_ricochet():
    """Nicht-Ricochet-Schuss → ein gerades Segment."""
    segs = _straight_shot(vel=(100.0, 0.0, 0.0), lifetime=1.0, server_rico=False)
    assert len(segs) == 1
    s = segs[0]
    assert _approx(s.t_start, 0.0)
    assert _approx(s.t_end, 1.0)
    assert _approx(s.ex, 100.0)   # x = 100*1
    assert _approx(s.ey, 0.0)
    assert _approx(s.ez, 1.0)


def test_r_flag_no_obstacles_straight():
    """R-Flag-Schuss ohne Obstacles → ein gerades Segment bis Lifetime."""
    segs = _straight_shot(flag=b"R\x00", lifetime=1.0, server_rico=False)
    assert len(segs) == 1
    assert _approx(segs[0].ex, 100.0)


def test_solid_obs_prefiltered_equivalent():
    """P2: vorgefilterte solid_obs-Liste liefert exakt dieselben Segmente wie
    der interne shoot_through-Filter (inkl. ignorierter Glas-Box)."""
    glass = _box(cx=20.0, cy=0.0, hw=5.0, hd=100.0, height=10.0, shoot_through=True)
    wall  = _box(cx=50.0, cy=0.0, hw=5.0, hd=100.0, height=10.0)
    obs = [glass, wall]
    a = simulate_shot_path((0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                           b"R\x00", obs, 200.0, False)
    b = simulate_shot_path((0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                           b"R\x00", obs, 200.0, False,
                           solid_obs=[o for o in obs if not o.shoot_through])
    assert a == b
    assert len(a) >= 2                     # Wand reflektiert …
    assert _approx(a[0].ex, 45.0, tol=0.1)  # … Glas-Box nicht


def test_bounce_off_axis_aligned_box():
    """R-Flag-Schuss prallt an einer Wand ab → mindestens 2 Segmente."""
    # Box-Wand bei x=50 (von 45 bis 55)
    wall = _box(cx=50.0, cy=0.0, hw=5.0, hd=100.0, bz=0.0, height=10.0)
    segs = simulate_shot_path(
        (0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 0.0, 2.0,
        b"R\x00", [wall], 200.0, False,
    )
    assert len(segs) >= 2
    # Erste Segment endet an der Wand (x≈45)
    s0 = segs[0]
    assert _approx(s0.ex, 45.0, tol=0.1)
    # Zweites Segment startet dort und fährt in -X Richtung
    s1 = segs[1]
    assert s1.ex < s0.ex   # Abpraller geht nach Westen zurück


def test_world_boundary_bounce():
    """Schuss trifft Weltgrenze und prallt ab."""
    segs = simulate_shot_path(
        (0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 0.0, 5.0,
        b"R\x00", [], 200.0, False,
    )
    # Trifft bei t=2s die x=+200 Wand
    assert len(segs) >= 2
    s0 = segs[0]
    assert _approx(s0.ex, 200.0, tol=0.1)
    s1 = segs[1]
    assert s1.ex < 200.0   # Schuss kehrt um


def test_server_ricochet_normal_shot():
    """Normaler Schuss mit server_ricochet=True → prallt auch ab."""
    wall = _box(cx=50.0, cy=0.0, hw=5.0, hd=100.0, bz=0.0, height=10.0)
    segs = simulate_shot_path(
        (0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 0.0, 2.0,
        b"\x00\x00", [wall], 200.0, True,   # server_ricochet=True
    )
    assert len(segs) >= 2


def test_shoot_through_ignored():
    """shoot_through-Obstacle wird nicht als Hindernis gewertet."""
    wall = _box(cx=50.0, cy=0.0, hw=5.0, hd=100.0, bz=0.0, height=10.0,
                shoot_through=True)
    segs = simulate_shot_path(
        (0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 0.0, 1.0,
        b"R\x00", [wall], 200.0, False,
    )
    # Schuss fliegt durch → ein gerades Segment
    assert len(segs) == 1
    assert _approx(segs[0].ex, 100.0, tol=0.1)


def test_laser_many_bounces():
    """Laser: sehr schnell + kurze Lifetime → viele Bounces möglich."""
    laser_speed = 100_000.0
    laser_life  = 0.1
    segs = simulate_shot_path(
        (0.0, 0.0, 1.0), (laser_speed, 0.0, 0.0), 0.0, laser_life,
        b"L\x00", [], 200.0, True,   # server_ricochet für Laser
    )
    # Bei Weltgröße 200u und 100000u/s Speed: viele Bounces innerhalb 0.1s
    assert len(segs) > 5


def test_segment_time_continuity():
    """Aufeinanderfolgende Segmente sind zeitlich lückenlos."""
    wall = _box(cx=50.0, cy=0.0, hw=5.0, hd=100.0, bz=0.0, height=10.0)
    segs = simulate_shot_path(
        (0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 10.0, 3.0,
        b"R\x00", [wall], 200.0, False,
    )
    for i in range(len(segs) - 1):
        assert _approx(segs[i].t_end, segs[i+1].t_start, tol=1e-4)


def test_segment_spatial_continuity():
    """Endpunkt eines Segments = Startpunkt des nächsten."""
    wall = _box(cx=50.0, cy=0.0, hw=5.0, hd=100.0, bz=0.0, height=10.0)
    segs = simulate_shot_path(
        (0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 0.0, 3.0,
        b"R\x00", [wall], 200.0, False,
    )
    for i in range(len(segs) - 1):
        assert _approx(segs[i].ex, segs[i+1].px, tol=0.01)
        assert _approx(segs[i].ey, segs[i+1].py, tol=0.01)
        assert _approx(segs[i].ez, segs[i+1].pz, tol=0.01)


def test_total_lifetime_respected():
    """Summe aller Segment-Dauer = shot.lifetime."""
    wall = _box(cx=50.0, cy=0.0, hw=5.0, hd=100.0, bz=0.0, height=10.0)
    lifetime = 2.5
    segs = simulate_shot_path(
        (0.0, 0.0, 1.0), (100.0, 0.0, 0.0), 0.0, lifetime,
        b"R\x00", [wall], 200.0, False,
    )
    total = sum(s.t_end - s.t_start for s in segs)
    assert _approx(total, lifetime, tol=1e-3)


def test_empty_for_zero_speed():
    """Schuss mit Geschwindigkeit 0 → leere Segmentliste."""
    segs = simulate_shot_path(
        (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, 1.0,
        b"R\x00", [], 200.0, True,
    )
    assert segs == []


# ---------------------------------------------------------------------------
# P1: Broad-Phase-Grid-Äquivalenz (obs_grid == linearer Scan, exakt)
# ---------------------------------------------------------------------------

import random

from conftest import load_map_fixture
from bzflag.obstacle_grid import ObstacleGrid, LOS_GRID_PAD
from bzflag.shot_physics import build_link_map


class TestShotPathGridEquivalence:
    """Das Grid ist nur eine Broad-Phase-Übermenge entlang des Strahls — die
    Segmentlisten müssen EXAKT identisch sein (0 Mismatch), auch bei rotierten
    Boxen, Pyramiden und Schüssen mit Z-Komponente."""

    def _obs(self):
        return [
            _box(cx=30.0, cy=0.0, hw=5.0, hd=20.0, height=10.0),
            _box(cx=-40.0, cy=25.0, hw=8.0, hd=3.0, height=12.0,
                 angle=math.radians(30.0)),
            _box(cx=10.0, cy=-45.0, hw=6.0, hd=6.0, height=9.0, is_pyr=True),
            _box(cx=-20.0, cy=-20.0, hw=4.0, hd=4.0, height=14.0, is_pyr=True,
                 angle=math.radians(45.0)),
            _box(cx=60.0, cy=60.0, hw=5.0, hd=5.0, height=8.0),
            _box(cx=0.0, cy=40.0, hw=12.0, hd=2.0, height=6.0, shoot_through=True),
        ]

    def test_random_shots_identical(self):
        rng = random.Random(42)
        obs = self._obs()
        solid = [o for o in obs if not o.shoot_through]
        grid = ObstacleGrid(solid, pad=LOS_GRID_PAD)
        for i in range(400):
            pos = (rng.uniform(-90.0, 90.0), rng.uniform(-90.0, 90.0),
                   rng.uniform(0.5, 8.0))
            az = rng.uniform(-math.pi, math.pi)
            speed = rng.choice([50.0, 100.0, 200.0])
            vel = (math.cos(az) * speed, math.sin(az) * speed,
                   rng.choice([0.0, 0.0, 10.0, -5.0]))
            life = rng.uniform(0.5, 3.5)
            a = simulate_shot_path(pos, vel, 0.0, life, b"R\x00",
                                   obs, 100.0, True)
            b = simulate_shot_path(pos, vel, 0.0, life, b"R\x00",
                                   obs, 100.0, True,
                                   solid_obs=solid, obs_grid=grid)
            assert a == b, (i, pos, vel, life)

    def test_hix_real_map_identical(self):
        """Echte Karte (HIX: hunderte Obstacles, Pyramiden, Teleporter):
        randomisierte Rico-Schüsse, Segmentlisten exakt gleich."""
        wm = load_map_fixture("hix")
        if wm is None:
            pytest.skip("hix-Fixture fehlt (tests/fixtures/hix.bin)")
        lmap = build_link_map(wm.links)
        solid = wm.solid_obstacles()
        grid = ObstacleGrid(solid, pad=LOS_GRID_PAD)
        rng = random.Random(1337)
        half = wm.world_half
        for i in range(200):
            pos = (rng.uniform(-half * 0.95, half * 0.95),
                   rng.uniform(-half * 0.95, half * 0.95),
                   rng.uniform(0.5, 25.0))
            az = rng.uniform(-math.pi, math.pi)
            vel = (math.cos(az) * 100.0, math.sin(az) * 100.0, 0.0)
            a = simulate_shot_path(pos, vel, 0.0, 3.5, b"\x00\x00",
                                   wm.boxes, half, True,
                                   teleporters=wm.teleporters, link_map=lmap)
            b = simulate_shot_path(pos, vel, 0.0, 3.5, b"\x00\x00",
                                   wm.boxes, half, True,
                                   teleporters=wm.teleporters, link_map=lmap,
                                   solid_obs=solid, obs_grid=grid)
            assert a == b, (i, pos, vel)
