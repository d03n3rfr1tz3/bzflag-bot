"""
Generische 2D/3D-Geometrie-Schnitttests (Broad-/Narrow-Phase-Primitive).

Port der Algorithmen aus src/game/Intersect.cxx (BZFlag):
  timeRayHitsOrigBox, getNormalOrigRect, getNormalRect,
  testRectRect / testOrigRectRect (OBB-OBB-Overlap).

Aus shot_physics.py herausgelöst: diese Primitive sind reine Geometrie (kein
Schuss-/Teleporter-Kontext) und werden sowohl von der Schuss-Physik als auch
von der reaktiven Tank-Kollision (bot/ai/physics.py) genutzt — EINE Quelle der
Wahrheit für das Form-Modell. shot_physics.py re-exportiert die verschobenen
Namen per Shim, damit bestehende Importpfade unverändert gültig bleiben.

Koordinaten-Konvention: identisch zu world_map.py (BZFlag big-endian, Z nach oben).
"""

import math
from typing import Optional, Tuple

from .world_map import BoxObstacle


# ---------------------------------------------------------------------------
# Rechteck-Normalen (Port von getNormalOrigRect / getNormalRect)
# ---------------------------------------------------------------------------

def _normal_orig_rect(px: float, py: float, dx: float, dy: float) -> float:
    """
    Port von getNormalOrigRect(). Winkel (Radiant) der nächsten Wand eines
    achsenparallelen Rechtecks [-dx,dx]×[-dy,dy] relativ zum Punkt (px,py).
    """
    if px > dx:           # east of box
        if py > dy:
            return math.atan2(py - dy, px - dx)   # ne corner
        elif py < -dy:
            return math.atan2(py + dy, px - dx)   # se corner
        else:
            return 0.0                              # east side
    if px < -dx:          # west of box
        if py > dy:
            return math.atan2(py - dy, px + dx)   # nw corner
        elif py < -dy:
            return math.atan2(py + dy, px + dx)   # sw corner
        else:
            return math.pi                          # west side
    if py > dy:
        return 0.5 * math.pi                        # north of box
    if py < -dy:
        return 1.5 * math.pi                        # south of box

    # inside box — find closest wall
    if px > 0.0:
        if py > 0.0:
            return 0.0 if dy * px > dx * py else 0.5 * math.pi
        else:
            return 0.0 if dy * px > -dx * py else 1.5 * math.pi
    else:
        if py > 0.0:
            return math.pi if dy * px < -dx * py else 0.5 * math.pi
        else:
            return math.pi if dy * px < dx * py else 1.5 * math.pi


def _normal_rect_2d(px: float, py: float,
                    cx: float, cy: float, angle: float,
                    dx: float, dy: float) -> Tuple[float, float]:
    """
    Port von getNormalRect(). Gibt (nx, ny) der nächsten Obstacle-Wand
    in Weltkoordinaten zurück (Z-Komponente = 0).
    """
    c = math.cos(-angle)
    s = math.sin(-angle)
    lx = c * (px - cx) - s * (py - cy)
    ly = c * (py - cy) + s * (px - cx)
    norm_angle = _normal_orig_rect(lx, ly, dx, dy) + angle
    return math.cos(norm_angle), math.sin(norm_angle)


# ---------------------------------------------------------------------------
# Ray-Box-Intersection (Port von timeRayHitsBlock / timeRayHitsOrigBox)
# ---------------------------------------------------------------------------

def ray_box_hit(ox: float, oy: float, oz: float,
                dx: float, dy: float, dz: float,
                box: BoxObstacle) -> Optional[Tuple[float, float, float, float]]:
    """
    Port von timeRayHitsBlock() inkl. Flächennormale.
    Transformiert Ray in Box-Lokalkoordinaten, ruft AABB-Slab-Test auf.

    Gibt (t, nx, ny, nz) zurück (Normale in Weltkoordinaten), oder None.
    """
    angle = box.angle
    c = math.cos(-angle)
    s = math.sin(-angle)

    # Translate + rotate to local
    tx = ox - box.cx
    ty = oy - box.cy
    lox = c * tx - s * ty
    loy = c * ty + s * tx
    loz = oz - box.bottom_z
    ldx = c * dx - s * dy
    ldy = c * dy + s * dx
    ldz = dz

    result = _ray_orig_box_hit(lox, loy, loz, ldx, ldy, ldz,
                               box.half_w, box.half_d, box.height)
    if result is None:
        return None

    t, nlx, nly, nlz = result

    # Rotate normal back to world (Z-Komponente bleibt unverändert)
    cf = math.cos(angle)
    sf = math.sin(angle)
    nx_w = cf * nlx - sf * nly
    ny_w = cf * nly + sf * nlx
    return t, nx_w, ny_w, nlz


def _ray_orig_box_hit(px: float, py: float, pz: float,
                      vx: float, vy: float, vz: float,
                      dx: float, dy: float,
                      dz: float) -> Optional[Tuple[float, float, float, float]]:
    """
    Port von timeRayHitsOrigBox(). Box: x=[-dx,dx], y=[-dy,dy], z=[0,dz].
    Gibt (t, face_nx, face_ny, face_nz) in Lokalkoordinaten zurück, oder None.
    """
    if abs(px) <= dx and abs(py) <= dy and 0.0 <= pz <= dz:
        return None  # innerhalb der Box — kein gültiger Treffer

    tx = ty = tz = -1.0

    if px > dx:           # east
        if vx >= 0.0: return None
        tx = (dx - px) / vx
    elif px < -dx:        # west
        if vx <= 0.0: return None
        tx = -(dx + px) / vx

    if py > dy:           # north
        if vy >= 0.0: return None
        ty = (dy - py) / vy
    elif py < -dy:        # south
        if vy <= 0.0: return None
        ty = -(dy + py) / vy

    if pz > dz:           # above
        if vz >= 0.0: return None
        tz = (dz - pz) / vz
    elif pz < 0.0:        # below
        if vz <= 0.0: return None
        tz = -pz / vz

    # Kandidaten validieren: liegt Auftreffpunkt innerhalb der Box-Grenzen?
    if tx >= 0.0:
        hy = py + tx * vy
        hz = pz + tx * vz
        if abs(hy) > dy or hz < 0.0 or hz > dz:
            tx = -1.0
    if ty >= 0.0:
        hx = px + ty * vx
        hz = pz + ty * vz
        if abs(hx) > dx or hz < 0.0 or hz > dz:
            ty = -1.0
    if tz >= 0.0:
        hx = px + tz * vx
        hy = py + tz * vy
        if abs(hx) > dx or abs(hy) > dy:
            tz = -1.0

    if tx < 0.0 and ty < 0.0 and tz < 0.0:
        return None

    # Minimales t auswählen (Port der if-Kaskade aus timeRayHitsOrigBox)
    if tx < 0.0:
        if ty < 0.0:
            t, face = tz, 2
        elif tz < 0.0 or ty < tz:
            t, face = ty, 1
        else:
            t, face = tz, 2
    elif ty < 0.0:
        if tz < 0.0 or tx < tz:
            t, face = tx, 0
        else:
            t, face = tz, 2
    elif tz < 0.0:
        t, face = (tx, 0) if tx < ty else (ty, 1)
    else:
        if tx < ty and tx < tz:
            t, face = tx, 0
        elif ty < tz:
            t, face = ty, 1
        else:
            t, face = tz, 2

    if t < 0.0:
        return None

    # Normale aus getroffener Fläche + Angriffsseite
    if face == 0:
        return t, (1.0 if px > 0.0 else -1.0), 0.0, 0.0
    elif face == 1:
        return t, 0.0, (1.0 if py > 0.0 else -1.0), 0.0
    else:
        return t, 0.0, 0.0, (1.0 if pz > dz else -1.0)


# ---------------------------------------------------------------------------
# Segment-OBB-Test (parametrische Slab-Methode)
# ---------------------------------------------------------------------------

def _segment_hits_obb_3d(ax: float, ay: float, az: float,
                         bx: float, by: float, bz: float,
                         cx: float, cy: float, cz: float, angle: float,
                         half_len: float, half_w: float, half_h: float) -> bool:
    """Parametrische Slab-Methode: Segment [A,B] gegen OBB (Mittelpunkt cx,cy,cz, Winkel angle)."""
    cos_a = math.cos(-angle); sin_a = math.sin(-angle)
    def to_local(x: float, y: float, z: float):
        dx, dy = x - cx, y - cy
        return (dx*cos_a - dy*sin_a, dx*sin_a + dy*cos_a, z - cz)
    alx, aly, alz = to_local(ax, ay, az)
    blx, bly, blz = to_local(bx, by, bz)
    t_min, t_max = 0.0, 1.0
    for a_c, b_c, half in ((alx, blx, half_len),
                           (aly, bly, half_w),
                           (alz, blz, half_h)):
        d = b_c - a_c
        if abs(d) < 1e-9:
            if abs(a_c) > half:
                return False
        else:
            t1 = (-half - a_c) / d;  t2 = (half - a_c) / d
            if t1 > t2: t1, t2 = t2, t1
            t_min = max(t_min, t1);  t_max = min(t_max, t2)
            if t_min > t_max:
                return False
    return True


def _extend_segment(ax: float, ay: float, az: float,
                    bx: float, by: float, bz: float,
                    extra: float) -> Tuple[float, float, float,
                                           float, float, float]:
    """Verlängert das Segment [A,B] an beiden Enden um `extra` entlang seiner
    eigenen Richtung (SB-Längskapsel: der Bolt ragt vor und hinter das
    Schusszentrum, seitlich bleibt die Reichweite unverändert)."""
    dx, dy, dz = bx - ax, by - ay, bz - az
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length < 1.0e-9:
        return ax, ay, az, bx, by, bz
    f = extra / length
    ex, ey, ez = dx * f, dy * f, dz * f
    return ax - ex, ay - ey, az - ez, bx + ex, by + ey, bz + ez


# ---------------------------------------------------------------------------
# OBB-OBB-Overlap (Port von testRectRect / testOrigRectRect)
# ---------------------------------------------------------------------------

_RRO_BOX = ((1.0, 1.0), (1.0, -1.0), (-1.0, -1.0), (-1.0, 1.0))


def _test_orig_rect_rect(px: float, py: float, angle: float,
                         dx1: float, dy1: float,
                         dx2: float, dy2: float) -> bool:
    """Port von testOrigRectRect(). Rechteck 1: Zentrum (px,py), um `angle`
    gedreht, Halbmaße dx1×dy1. Rechteck 2: achsparallel im Ursprung, dx2×dy2.
    True = Überlappung (Berühren zählt strikt NICHT)."""
    c = math.cos(angle); s = math.sin(angle)
    # Ursprung (Zentrum von Rechteck 2) in Rechteck-1-Frame → innen?
    sx = c * px + s * py
    sy = c * py - s * px
    if abs(sx) < dx1 and abs(sy) < dy1:
        return True

    # Ecken von Rechteck 1 gegen Rechteck 2 klassifizieren; Ecke innen → True
    corner1 = []
    region = []
    for bx, by in _RRO_BOX:
        c1x = px + c * dx1 * bx - s * dy1 * by
        c1y = py + s * dx1 * bx + c * dy1 * by
        rx = -1 if c1x < -dx2 else (1 if c1x > dx2 else 0)
        ry = -1 if c1y < -dy2 else (1 if c1y > dy2 else 0)
        if rx == 0 and ry == 0:
            return True
        corner1.append((c1x, c1y))
        region.append((rx, ry))

    # jede Kante von Rechteck 1 prüfen
    for i in range(4):
        j = (i + 1) % 4
        ri = region[i]; rj = region[j]
        if ri[0] == rj[0]:
            if ri[0] == 0 and ri[1] != rj[1]:
                return True
            continue
        elif ri[1] == rj[1]:
            if ri[1] == 0:
                return True
            continue

        # Ecke von Rechteck 2 bestimmen, zwischen der die Kante durchlaufen könnte
        if ri[0] == 0:
            c2x = rj[0] * dx2; c2y = ri[1] * dy2
        elif rj[0] == 0:
            c2x = ri[0] * dx2; c2y = rj[1] * dy2
        elif ri[1] == 0:
            c2x = ri[0] * dx2; c2y = rj[1] * dy2
        else:
            c2x = rj[0] * dx2; c2y = ri[1] * dy2

        # kreuzt die Kante das Rechteck?
        e0 = corner1[j][0] - corner1[i][0]
        e1 = corner1[j][1] - corner1[i][1]
        cix, ciy = corner1[i]
        if ((e1 * (c2x - cix) - e0 * (c2y - ciy)) *
                (e1 * (c2x + cix) - e0 * (c2y + ciy))) > 0.0:
            return True
    return False


def rect_rect_overlap(cx: float, cy: float, angle: float, hw: float, hd: float,
                      px: float, py: float, pangle: float,
                      phl: float, phw: float) -> bool:
    """OBB-OBB-Overlap (2D). Box1 (Obstacle): Zentrum (cx,cy), Winkel angle,
    Halbmaße hw×hd. Box2 (Tank): Zentrum (px,py), Winkel pangle, Halbmaße
    phl×phw. Port von BZFlags testRectRect (src/game/Intersect.cxx) — volle SAT
    über beide Rechtecke. Berühren zählt strikt NICHT als Overlap."""
    pax = px - cx
    pay = py - cy
    c = math.cos(-angle); s = math.sin(-angle)
    pbx = c * pax - s * pay
    pby = c * pay + s * pax
    return _test_orig_rect_rect(pbx, pby, pangle - angle, phl, phw, hw, hd)
