"""
Tests für bzflag/intersect.py — insbesondere den OBB-OBB-Overlap rect_rect_overlap
(Port bzfs testRectRect) und den Backward-Compat-Shim aus bzflag.shot_physics.
"""

import math
import pytest

from bzflag.intersect import rect_rect_overlap


# ── rect_rect_overlap: Box1 (Obstacle) = (cx,cy,angle,hw,hd); Box2 (Tank) = (px,py,pangle,phl,phw)

def test_axis_aligned_overlap():
    # Wand x[-5,5], Tank (lang) bei x=8 → Tank x[3,13] überlappt bei x[3,5]
    assert rect_rect_overlap(0, 0, 0.0, 5, 5,  8, 0, 0.0, 5, 2) is True


def test_axis_aligned_separated():
    # Tank bei x=12 → x[7,17], keine Überlappung mit x[-5,5]
    assert rect_rect_overlap(0, 0, 0.0, 5, 5,  12, 0, 0.0, 5, 2) is False


def test_fully_inside():
    assert rect_rect_overlap(0, 0, 0.0, 5, 5,  0, 0, 0.0, 0.2, 0.2) is True


def test_corner_gap_strict():
    # Tank x[1.01,3], y[1.01,3] gegen Box x[-1,1],y[-1,1] → klarer Spalt → False
    assert rect_rect_overlap(0, 0, 0.0, 1, 1,  2.01, 2.01, 0.0, 1, 1) is False


def test_long_tank_pierces_thin_wall_where_circle_fails():
    """Kernfall: dünne Wand (hw=0.5) + langer Tank (phl=3) senkrecht davor, Zentrum 2.0u
    entfernt. Der alte Kreis-Test (Radius = Halb-Breite 1.4) sähe KEINE Überlappung
    (2.0 > 0.5+1.4=1.9), die Tank-OBB ragt mit der Nase (3.0) aber durch → True."""
    # Kreis-Näherung (zur Kontrast-Doku):
    assert 2.0 > 0.5 + 1.4          # Kreis-Test würde "kein Overlap" sagen
    # OBB: Tank zeigt in +x (phl entlang x), Zentrum 2.0u vor der Wand → Nase erreicht x=-1
    assert rect_rect_overlap(0, 0, 0.0, 0.5, 150.0,  2.0, 0.0, 0.0, 3.0, 1.4) is True


def test_short_tank_no_pierce_thin_wall():
    # Gleiche Wand, aber kurzer Tank (phl=1.0) bei x=2.0 → Nase erreicht nur x=1 → kein Overlap
    assert rect_rect_overlap(0, 0, 0.0, 0.5, 150.0,  2.0, 0.0, 0.0, 1.0, 1.4) is False


def test_rotated_wall_perpendicular_pierce():
    """Reale HIX-Geometrie: 135°-Wand bei (180,180), hw=0.5. Tank 2.0u auf der
    Senkrechten (lokale x-Achse der Wand) und in die Wand zeigend → Overlap."""
    ang = math.radians(135)
    perp = (math.cos(ang), math.sin(ang))     # lokale x-Richtung der Wand
    px, py = 180 + perp[0] * 2.0, 180 + perp[1] * 2.0
    # Tank zeigt in die Wand (Heading entlang -perp = pangle = ang + pi), phl=3
    assert rect_rect_overlap(180, 180, ang, 0.5, 150.0,
                             px, py, ang + math.pi, 3.0, 1.4) is True


def test_rotated_wall_parallel_far_no_overlap():
    """Tank fährt PARALLEL zur 135°-Wand, 5u seitlich versetzt → nur Halb-Breite (1.4)
    zeigt zur Wand, 5 > 0.5+1.4 → kein Overlap (Parallelfahrt bleibt frei)."""
    ang = math.radians(135)
    perp = (math.cos(ang), math.sin(ang))
    px, py = 180 + perp[0] * 5.0, 180 + perp[1] * 5.0
    # Heading entlang der Wand (ang + 90°), phl=3 entlang der Wandrichtung
    assert rect_rect_overlap(180, 180, ang, 0.5, 150.0,
                             px, py, ang + math.pi / 2, 3.0, 1.4) is False


def test_shim_reexport_identity():
    """Alte Importpfade aus bzflag.shot_physics liefern dieselben Funktionsobjekte."""
    import bzflag.intersect as I
    import bzflag.shot_physics as S
    for name in ("rect_rect_overlap", "_segment_hits_obb_3d", "ray_box_hit",
                 "_ray_orig_box_hit", "_normal_rect_2d", "_extend_segment"):
        assert getattr(S, name) is getattr(I, name), name
