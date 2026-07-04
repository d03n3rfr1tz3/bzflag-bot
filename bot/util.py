"""Geometrie-Hilfsfunktionen ohne Bot-Zustand (W2, FABLE-PLAN Teil 3)."""

import math


def _angle_diff(target: float, current: float) -> float:
    """Kürzeste Winkeldifferenz in (-π, π]: Richtung von current nach target.
    Genau 180° gibt -π zurück (CW bevorzugt bei Halbkreis-Grenzfall)."""
    d = (target - current) % (2 * math.pi)
    return d - 2 * math.pi if d >= math.pi else d


def _wrap(a: float) -> float:
    """Normalisiert Winkel auf [-π, π]."""
    while a >  math.pi: a -= 2 * math.pi
    while a < -math.pi: a += 2 * math.pi
    return a


def _segment_point_dist3d(ax: float, ay: float, az: float,
                           bx: float, by: float, bz: float,
                           cx: float, cy: float, cz: float) -> float:
    """Minimaler Abstand von Punkt C zum 3D-Liniensegment A→B."""
    abx, aby, abz = bx-ax, by-ay, bz-az
    acx, acy, acz = cx-ax, cy-ay, cz-az
    ab2 = abx**2 + aby**2 + abz**2
    # Segment hat Länge 0 → Abstand ist direkte Distanz A→C
    if ab2 < 1e-10:
        return math.sqrt(acx**2 + acy**2 + acz**2)
    # t=0 → Punkt liegt am Anfang A, t=1 → am Ende B; clampen hält t im Segment
    t = max(0.0, min(1.0, (acx*abx + acy*aby + acz*abz) / ab2))
    dx, dy, dz = acx - t*abx, acy - t*aby, acz - t*abz
    return math.sqrt(dx**2 + dy**2 + dz**2)
