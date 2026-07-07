"""
BZFlag Schuss-Physik: Ricochet-Pfad-Simulation.

Port der Algorithmen aus:
  src/bzflag/SegmentedShotStrategy.cxx  — makeSegments() Hauptschleife
  src/game/Intersect.cxx               — timeRayHitsOrigBox, timeRayHitsPyramids,
                                          timeRayHitsPlane, getNormalRect
  src/obstacle/BoxBuilding.cxx         — get3DNormal
  src/obstacle/PyramidBuilding.cxx     — get3DNormal, shrinkFactor

Koordinaten-Konvention: identisch zu world_map.py (BZFlag big-endian, Z nach oben).
"""

import math
from typing import Optional, List, Tuple, TYPE_CHECKING
from collections import namedtuple

from .world_map import BoxObstacle, TeleporterObstacle
# Generische Geometrie-Primitive liegen jetzt in intersect.py (Port bzfs Intersect.cxx).
# Re-Export-Shim: bestehende `from bzflag.shot_physics import …`-Pfade bleiben gültig.
from .intersect import (  # noqa: F401
    _normal_orig_rect, _normal_rect_2d,
    ray_box_hit, _ray_orig_box_hit,
    _segment_hits_obb_3d, _extend_segment,
    rect_rect_overlap,
)

if TYPE_CHECKING:   # nur Typ-Annotation (obs_grid) — kein Laufzeit-Import nötig
    from .obstacle_grid import ObstacleGrid

# Segment eines Schuss-Pfades (alle absoluten Zeitstempel in Sekunden)
Segment = namedtuple('Segment', ['px', 'py', 'pz',   # Startpunkt
                                  'ex', 'ey', 'ez',   # Endpunkt
                                  't_start', 't_end'])

# Physikalischer Mindestabstand (Einheiten) um Selbst-Treffer nach Bounce zu vermeiden.
# Wird in simulate_shot_path() per eps = _EPSILON_DIST / speed in Sekunden umgerechnet,
# damit Laser (100.000 u/s) keinen 100-Einheiten-Blindspot erhält.
_EPSILON_DIST = 0.1


# ---------------------------------------------------------------------------
# Öffentliche Hilfsfunktionen
# ---------------------------------------------------------------------------

def reflect(vx: float, vy: float, vz: float,
            nx: float, ny: float, nz: float) -> Tuple[float, float, float]:
    """
    Reflexion von Vektor v an Fläche mit Normalem n (n muss normiert sein).
    v' = v - 2*(v·n)*n
    """
    dot = vx * nx + vy * ny + vz * nz
    return (vx - 2.0 * dot * nx,
            vy - 2.0 * dot * ny,
            vz - 2.0 * dot * nz)


def can_ricochet(flag_abbr: bytes, is_gm: bool, is_sw: bool,
                 server_ricochet: bool,
                 is_phantom_zoned: bool = False) -> bool:
    """Kann dieser Schusstyp abprallen?"""
    if is_gm or is_sw or flag_abbr == b"SB" or is_phantom_zoned:
        return False
    return server_ricochet or (flag_abbr == b"R\x00")


# ---------------------------------------------------------------------------
# Teleporter (Port von Teleporter::hasCrossed + getPointWRT)
# ---------------------------------------------------------------------------

def build_link_map(links: List[Tuple[int, int]]) -> dict:
    """
    face-Index → Ziel-face-Index. face-Index = teleporter_index*2 + (0=front, 1=back).
    Bei mehreren Zielen pro Quelle gewinnt das erste (deterministisch).
    Mehrfach-Links wählt der Server zufällig — auf HIX hat jedes Face genau eins.
    """
    m: dict = {}
    for src, dst in links:
        if src not in m:
            m[src] = dst
    return m


def ray_teleporter_crossing(ox: float, oy: float, oz: float,
                            dx: float, dy: float, dz: float,
                            tele: TeleporterObstacle) -> Optional[Tuple[float, int]]:
    """
    Port von Teleporter::hasCrossed als Ray-Test: wann (t, Sekunden) quert der Strahl
    das passierbare Feld (Ebene x_local=0)? Liefert (t, face) oder None.
    face = 0 wenn der Strahl von der Seite x_local>0 kommt, sonst 1.
    """
    ca = math.cos(-tele.angle)
    sa = math.sin(-tele.angle)
    rx = ox - tele.cx
    ry = oy - tele.cy
    x0 = ca * rx - sa * ry          # lokales X bei t=0
    xd = ca * dx - sa * dy          # d(lokales X)/dt
    if abs(xd) < 1.0e-9:
        return None                 # parallel zur Feld-Ebene
    t = -x0 / xd
    z_at = oz + dz * t
    if z_at < tele.bottom_z or z_at > tele.bottom_z + tele.height - tele.border:
        return None
    y0 = ca * ry + sa * rx          # lokales Y bei t=0
    yd = ca * dy + sa * dx
    y_at = y0 + yd * t
    if abs(y_at) > tele.half_d - tele.border:
        return None
    face = 0 if x0 > 0.0 else 1
    return (t, face)


def teleport_through(px: float, py: float, pz: float,
                     dx: float, dy: float, dz: float,
                     t1: TeleporterObstacle, face1: int,
                     t2: TeleporterObstacle, face2: int
                     ) -> Tuple[float, float, float, float, float, float]:
    """
    Port von Teleporter::getPointWRT: transformiert Position + Richtung vom Eintritts-Face
    face1 auf t1 zum Austritts-Face face2 auf t2. Liefert (px',py',pz', dx',dy',dz').
    Skaliert Y/Z mit den relativen aktiven Flächen, dreht um (radians2 - radians1).
    """
    pi = math.pi
    radians1 = t1.angle + (0.0 if face1 == 0 else pi)
    radians2 = t2.angle + (0.0 if face2 == 1 else pi)

    # An t1-Ursprung verschieben, Rotation zurücknehmen (rotateZ(-radians1))
    x = px - t1.cx
    y = py - t1.cy
    z = pz - t1.bottom_z
    ca, sa = math.cos(-radians1), math.sin(-radians1)
    lx = x * ca - y * sa
    ly = x * sa + y * ca

    # Fixes X-Offset auf der Austrittsseite, Y/Z auf relative aktive Flächen skalieren
    dims1y = (t1.half_d - t1.border) or 1.0e-6
    dims1z = (t1.height - t1.border) or 1.0e-6
    sx = -t2.half_w
    sy = ly * ((t2.half_d - t2.border) / dims1y)
    sz = z * ((t2.height - t2.border) / dims1z)

    # Rotation von t2 anwenden (rotateZ(+radians2)), an t2-Ursprung verschieben
    cb, sb = math.cos(radians2), math.sin(radians2)
    npx = sx * cb - sy * sb + t2.cx
    npy = sx * sb + sy * cb + t2.cy
    npz = sz + t2.bottom_z

    # Richtung um (radians2 - radians1) drehen (Z unverändert)
    a = radians2 - radians1
    c, s = math.cos(a), math.sin(a)
    ndx = c * dx - s * dy
    ndy = c * dy + s * dx
    return (npx, npy, npz, ndx, ndy, dz)


# ---------------------------------------------------------------------------
# Interne Normalenberechnung (Port von getNormalOrigRect + getNormalRect)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Normalenberechnung für Box und Pyramide
# (_normal_orig_rect / _normal_rect_2d → bzflag/intersect.py, oben re-exportiert)
# ---------------------------------------------------------------------------

def get_box_normal(px: float, py: float, pz: float,
                   box: BoxObstacle) -> Tuple[float, float, float]:
    """
    Port von BoxBuilding::get3DNormal(). Gibt (nx, ny, nz) normiert.
    Boden → (0,0,-1), Decke → (0,0,+1), Wand → 2D-Normal mit nz=0.
    """
    eps = 1.0e-3
    if abs(pz - box.bottom_z) < eps:
        return 0.0, 0.0, -1.0
    if abs(pz - (box.bottom_z + box.height)) < eps:
        return 0.0, 0.0, 1.0
    nx, ny = _normal_rect_2d(px, py, box.cx, box.cy, box.angle,
                              box.half_w, box.half_d)
    return nx, ny, 0.0


def _shrink_factor(z: float, bottom_z: float, height: float,
                   z_flip: bool) -> float:
    """
    Port von PyramidBuilding::shrinkFactor(z, height=0).
    Querschnitts-Skalierungsfaktor der Pyramide auf Höhe z:
      1.0 = volle Basis, 0.0 = Spitze.
    """
    if height <= 1.0e-6:
        return 1.0
    t = (z - bottom_z) / height
    s = t if z_flip else (1.0 - t)
    return max(0.0, min(1.0, s))


def get_pyramid_normal(px: float, py: float, pz: float,
                       pyr: BoxObstacle) -> Tuple[float, float, float]:
    """
    Port von PyramidBuilding::get3DNormal(). Gibt exakten (nx, ny, nz)
    inkl. Z-Neigungskomponente der Pyramidenfläche.
    """
    eps = 1.0e-3
    top_z = pyr.bottom_z + pyr.height
    s = _shrink_factor(pz, pyr.bottom_z, pyr.height, pyr.z_flip)

    # Sonderfälle: Spitze oder Basis
    if s == 0.0:
        if pyr.z_flip:
            if pz >= top_z:
                return 0.0, 0.0, 1.0
        else:
            if pz <= pyr.bottom_z:
                return 0.0, 0.0, -1.0
    if s >= 1.0 - eps:
        return (0.0, 0.0, 1.0) if pyr.z_flip else (0.0, 0.0, -1.0)

    # 2D-Normal auf der aktuellen Querschnittsebene
    nx, ny = _normal_rect_2d(px, py, pyr.cx, pyr.cy, pyr.angle,
                              s * pyr.half_w, s * pyr.half_d)

    # X-Wand oder Y-Wand getroffen? (Port der FIXME-Logik aus PyramidBuilding.cxx)
    norm_angle = math.atan2(ny, nx)
    right_angle = abs((norm_angle - pyr.angle + 0.5 * math.pi) % math.pi)
    base_len = pyr.half_d if (right_angle < 0.1 or right_angle > math.pi - 0.1) \
               else pyr.half_w

    hyp = math.hypot(pyr.height, base_len)
    if hyp < 1.0e-6:
        return (0.0, 0.0, 1.0) if pyr.z_flip else (0.0, 0.0, -1.0)

    h = 1.0 / hyp
    nx3 = nx * h * pyr.height
    ny3 = ny * h * pyr.height
    nz3 = h * base_len * (-1.0 if pyr.z_flip else 1.0)
    return nx3, ny3, nz3


# ---------------------------------------------------------------------------
# Ray-Box-Intersection: ray_box_hit / _ray_orig_box_hit → bzflag/intersect.py
# (oben re-exportiert; simulate_shot_path nutzt sie über den Shim-Import)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Ray-Pyramid-Intersection (Port von timeRayHitsPlane + timeRayHitsPyramids)
# ---------------------------------------------------------------------------

def _time_ray_hits_plane(pb: list, db: list,
                          x1: tuple, x2: tuple, x3: tuple) -> float:
    """
    Port von timeRayHitsPlane(). Halbraum-Eintrittszeit.
    pb = aktuelle Ray-Position (mutable 3-Liste), db = Richtung.
    x1,x2,x3 definieren die Ebene (3-Tupel).

    Gibt 0.0 zurück wenn bereits auf der richtigen Seite,
    -1.0 wenn Ebene nie erreicht wird.
    """
    ux = x2[0] - x1[0]; uy = x2[1] - x1[1]; uz = x2[2] - x1[2]
    vx = x3[0] - x1[0]; vy = x3[1] - x1[1]; vz = x3[2] - x1[2]
    ddx = pb[0] - x1[0]; ddy = pb[1] - x1[1]; ddz = pb[2] - x1[2]

    # Ebenennormale (Kreuzprodukt, unnormiert)
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx

    distance = nx * ddx + ny * ddy + nz * ddz
    if distance <= 0.0:
        return 0.0   # bereits auf der richtigen Seite

    velocity = nx * db[0] + ny * db[1] + nz * db[2]
    if velocity >= 0.0:
        return -1.0  # parallel oder von Ebene weg

    return -distance / velocity


def ray_pyramid_hit(ox: float, oy: float, oz: float,
                    dx: float, dy: float, dz: float,
                    pyr: BoxObstacle) -> Optional[Tuple[float, float, float, float]]:
    """
    Port von timeRayHitsPyramids(). Half-Space-Erosion durch alle 5 Flächen.
    Gibt (t, nx, ny, nz) mit weltkoordinierter Normale zurück, oder None.
    """
    angle = pyr.angle
    hw = abs(pyr.half_w)
    hd = abs(pyr.half_d)
    hh = abs(pyr.height)

    if hw < 1.0e-6 or hd < 1.0e-6 or hh < 1.0e-6:
        return None

    c = math.cos(-angle)
    s = math.sin(-angle)

    # Translate + rotate to local
    tx = ox - pyr.cx
    ty = oy - pyr.cy
    pb = [c * tx - s * ty,
          c * ty + s * tx,
          oz - pyr.bottom_z]
    db = [c * dx - s * dy,
          c * dy + s * dx,
          dz]

    # ZFlip: Pyramide auf den Kopf stellen (Spitze oben → Spitze unten)
    if pyr.z_flip:
        pb[2] = hh - pb[2]
        db[2] = -db[2]

    residual_time = 0.0
    apex = (0.0, 0.0, hh)

    # Face 1: (-hw,-hd,0), (+hw,-hd,0), Apex
    t = _time_ray_hits_plane(pb, db, (-hw, -hd, 0.0), (hw, -hd, 0.0), apex)
    if t < -0.5:
        return None
    for i in range(3):
        pb[i] += t * db[i]
    residual_time += t

    # Face 2: (+hw,-hd,0), (+hw,+hd,0), Apex
    t = _time_ray_hits_plane(pb, db, (hw, -hd, 0.0), (hw, hd, 0.0), apex)
    if t < -0.5:
        return None
    for i in range(3):
        pb[i] += t * db[i]
    residual_time += t

    # Face 3: (+hw,+hd,0), (-hw,+hd,0), Apex
    t = _time_ray_hits_plane(pb, db, (hw, hd, 0.0), (-hw, hd, 0.0), apex)
    if t < -0.5:
        return None
    for i in range(3):
        pb[i] += t * db[i]
    residual_time += t

    # Face 4: (-hw,+hd,0), (-hw,-hd,0), Apex
    t = _time_ray_hits_plane(pb, db, (-hw, hd, 0.0), (-hw, -hd, 0.0), apex)
    if t < -0.5:
        return None
    for i in range(3):
        pb[i] += t * db[i]
    residual_time += t

    # Base: (-hw,-hd,0), (-hw,+hd,0), (+hw,+hd,0)
    t = _time_ray_hits_plane(pb, db, (-hw, -hd, 0.0), (-hw, hd, 0.0), (hw, hd, 0.0))
    if t < -0.5:
        return None
    for i in range(3):
        pb[i] += t * db[i]
    residual_time += t

    # Liegt der Endpunkt tatsächlich innerhalb der Pyramide? (Bounding + Shrink-Check)
    eps = 1.0e-3
    absx, absy = abs(pb[0]), abs(pb[1])
    if absx > hw + eps * hw:
        return None
    if absy > hd + eps * hd:
        return None
    if pb[2] < -eps * hh:
        return None
    if absx * hd > absy * hw:
        if hh * (hw - absx) - pb[2] * hw < -eps * hh:
            return None
    else:
        if hh * (hd - absy) - pb[2] * hd < -eps * hh:
            return None

    if residual_time < 0.0:
        return None

    # Auftreffpunkt in Weltkoordinaten → exakte 3D-Normale berechnen
    hit_wx = ox + residual_time * dx
    hit_wy = oy + residual_time * dy
    hit_wz = oz + residual_time * dz
    nx_w, ny_w, nz_w = get_pyramid_normal(hit_wx, hit_wy, hit_wz, pyr)
    return residual_time, nx_w, ny_w, nz_w


# ---------------------------------------------------------------------------
# Weltgrenzen-Treffer
# ---------------------------------------------------------------------------

def _world_boundary_hits(ox: float, oy: float, oz: float,
                          dx: float, dy: float, dz: float,
                          world_half: float,
                          wall_height: float = 6.15) -> List[Tuple[float, float, float, float]]:
    """
    Gibt alle Treffer-Zeiten für die vier Außenwände (bei ±world_half in X und Y)
    als Liste von (t, nx, ny, nz) zurück.
    Treffer werden ignoriert, wenn die Z-Koordinate am Aufprallpunkt über wall_height liegt
    (Port von SegmentedShotStrategy.cxx:457–462).
    """
    hits = []
    if dx > 1.0e-9:
        t = (world_half - ox) / dx
        if t > 0.0 and oz + t * dz <= wall_height:
            hits.append((t, -1.0, 0.0, 0.0))
    elif dx < -1.0e-9:
        t = (-world_half - ox) / dx
        if t > 0.0 and oz + t * dz <= wall_height:
            hits.append((t, 1.0, 0.0, 0.0))
    if dy > 1.0e-9:
        t = (world_half - oy) / dy
        if t > 0.0 and oz + t * dz <= wall_height:
            hits.append((t, 0.0, -1.0, 0.0))
    elif dy < -1.0e-9:
        t = (-world_half - oy) / dy
        if t > 0.0 and oz + t * dz <= wall_height:
            hits.append((t, 0.0, 1.0, 0.0))
    return hits


# ---------------------------------------------------------------------------
# Geometrie-Hilfsfunktionen
# ---------------------------------------------------------------------------

# _segment_hits_obb_3d / _extend_segment → bzflag/intersect.py (oben re-exportiert).


# ---------------------------------------------------------------------------
# Hauptfunktion: Schuss-Pfad-Simulation
# ---------------------------------------------------------------------------

_MAX_BOUNCES_DEFAULT = 100  # BZFlag-Standard (SegmentedShotStrategy::makeSegments)


def simulate_shot_path(pos: Tuple[float, float, float],
                        vel: Tuple[float, float, float],
                        fire_time: float,
                        lifetime: float,
                        flag_abbr: bytes,
                        obstacles: List[BoxObstacle],
                        world_half: float,
                        server_ricochet: bool,
                        max_bounces: int = _MAX_BOUNCES_DEFAULT,
                        wall_height: float = 6.15,
                        teleporters: Optional[List[TeleporterObstacle]] = None,
                        link_map: Optional[dict] = None,
                        tele_log: Optional[list] = None,
                        solid_obs: Optional[List[BoxObstacle]] = None,
                        obs_grid: Optional["ObstacleGrid"] = None,
                        phase_walls: bool = False) -> List[Segment]:
    """
    Port von SegmentedShotStrategy::makeSegments() + Teleporter-Querung.
    Simuliert den vollständigen Schuss-Pfad inkl. Abpraller und Teleporter.

    Gibt eine Liste von Segment-namedtuples zurück. Bei einer Teleporter-Querung
    ist der Pfad bewusst diskontinuierlich (Segment-Ende am Eintritt, nächstes
    Segment beginnt am Austritts-Teleporter).

    reflect_all = server_ricochet OR flag==R. Teleporter transportieren JEDEN Schuss,
    auch ohne Ricochet — daher läuft die Schleife sobald reflect_all ODER teleporters.
    Max. `max_bounces` Iterationen (deckelt auch Teleport-Ping-Pong).

    phase_walls=True (SB/PZ, ObstacleEffect::Through): Obstacles und Weltgrenzen
    stoppen den Schuss NICHT (makeSegments überspringt für Through nur die
    Gebäude-Suche) — Teleporter-Querungen greifen aber weiterhin, denn der
    Teleporter-Lookup in makeSegments läuft unabhängig vom ObstacleEffect.
    """
    speed = math.sqrt(vel[0]**2 + vel[1]**2 + vel[2]**2)
    if speed < 1.0e-6 or lifetime <= 0.0:
        return []

    # Zeitäquivalent von _EPSILON_DIST: verhindert Selbst-Treffer nach Bounce.
    # Laser (100.000 u/s) → eps ≈ 1e-6 s (0.1 Einheiten); Normal (100 u/s) → 0.001 s.
    eps = _EPSILON_DIST / max(speed, 1.0)

    reflect_all = server_ricochet or (flag_abbr == b"R\x00")
    teles = teleporters or []
    lmap = link_map or {}

    if not teles and (phase_walls or not reflect_all):
        # Keine Teleporter und nichts, was den Schuss ablenken kann
        # (phasend oder ohne Ricochet): gerader Pfad bis Lifetime-Ende
        return [Segment(pos[0], pos[1], pos[2],
                        pos[0] + vel[0] * lifetime,
                        pos[1] + vel[1] * lifetime,
                        pos[2] + vel[2] * lifetime,
                        fire_time, fire_time + lifetime)]

    # Alle Obstacles die Schüsse ablenken können (shoot_through → transparent).
    # solid_obs = vorgefilterte Liste (WorldMap.solid_obstacles()) — erspart den
    # Filter über alle Obstacles pro Aufruf; ohne solid_obs unverändertes Verhalten.
    test_obs = solid_obs if solid_obs is not None else \
        [o for o in obstacles if not o.shoot_through]

    ox, oy, oz = pos[0], pos[1], pos[2]
    # Geschwindigkeitsvektor als Richtung — t aus Intersection-Tests ist in Sekunden
    ddx, ddy, ddz = vel[0], vel[1], vel[2]
    time_left = lifetime
    abs_time = fire_time

    segments: List[Segment] = []

    for _ in range(max_bounces):
        if time_left <= eps:
            break

        # Finde nächstes Ereignis: Obstacles + Weltgrenzen + Teleporter-Querungen
        best_t = time_left + 1.0   # sentinel > time_left
        best_n = (0.0, 0.0, 0.0)
        best_tele: Optional[Tuple[int, int, int, int]] = None  # (eintritt_ti, eintritt_face, ziel_ti, ziel_face) oder None

        if not phase_walls:
            # Weltgrenzen
            for wt, wnx, wny, wnz in _world_boundary_hits(ox, oy, oz, ddx, ddy, ddz, world_half, wall_height):
                if eps < wt < best_t:
                    best_t = wt
                    best_n = (wnx, wny, wnz)
                    best_tele = None

            # Obstacles — Broad-Phase (P1): nur Kandidaten der in XY überflogenen
            # Zellen statt aller Obstacles. Exakt äquivalent: die DDA liefert jede
            # Box, deren gepolsterte AABB eine durchquerte Zelle berührt (keine
            # False Negatives, s. ObstacleGrid-Docstring); Hits jenseits time_left,
            # die der Query-Strecke fehlen könnten, enden in beiden Pfaden im
            # identischen „Segment bis Lifetime-Ende"-Zweig (best_t > time_left).
            if obs_grid is not None:
                cand_obs = obs_grid.query_ray(ox, oy,
                                              ox + ddx * time_left,
                                              oy + ddy * time_left)
            else:
                cand_obs = test_obs
            for obs in cand_obs:
                if obs.is_pyramid:
                    result = ray_pyramid_hit(ox, oy, oz, ddx, ddy, ddz, obs)
                else:
                    result = ray_box_hit(ox, oy, oz, ddx, ddy, ddz, obs)
                if result is None:
                    continue
                t_hit, hnx, hny, hnz = result
                if eps < t_hit < best_t:
                    best_t = t_hit
                    best_n = (hnx, hny, hnz)
                    best_tele = None

        # Teleporter-Querungen (transportieren jeden Schuss)
        for ti, tele in enumerate(teles):
            res = ray_teleporter_crossing(ox, oy, oz, ddx, ddy, ddz, tele)
            if res is None:
                continue
            t_cross, face = res
            if not (eps < t_cross < best_t):
                continue
            dst_face = lmap.get(ti * 2 + face)
            if dst_face is None:
                continue   # unverlinktes Face → ignorieren
            best_t = t_cross
            best_n = (0.0, 0.0, 0.0)
            best_tele = (ti, face, dst_face // 2, dst_face % 2)  # (eintritt_ti, eintritt_face, ziel_ti, ziel_face)

        # Kein Ereignis innerhalb Restzeit → Segment bis Lifetime-Ende
        if best_t > time_left:
            segments.append(Segment(
                ox, oy, oz,
                ox + ddx * time_left,
                oy + ddy * time_left,
                oz + ddz * time_left,
                abs_time, abs_time + time_left,
            ))
            break

        # Segment bis Auftreff-/Querungspunkt
        hit_x = ox + ddx * best_t
        hit_y = oy + ddy * best_t
        hit_z = oz + ddz * best_t
        segments.append(Segment(
            ox, oy, oz,
            hit_x, hit_y, hit_z,
            abs_time, abs_time + best_t,
        ))
        abs_time += best_t
        time_left -= best_t

        if best_tele is not None:
            # Teleporter-Querung: Position+Richtung am Austritts-Face transformieren,
            # weiterlaufen (kein Reflektieren). eps + Austritts-Offset verhindern Re-Querung.
            e_ti, e_face, d_ti, d_face = best_tele
            _ang_in = math.atan2(ddy, ddx)
            ox, oy, oz, ddx, ddy, ddz = teleport_through(
                hit_x, hit_y, hit_z, ddx, ddy, ddz,
                teles[e_ti], e_face, teles[d_ti], d_face)
            if tele_log is not None:
                tele_log.append((e_ti, e_face, d_ti, d_face,
                                 (hit_x, hit_y, hit_z), (ox, oy, oz),
                                 _ang_in, math.atan2(ddy, ddx)))
            continue

        # Wand/Box getroffen
        if not reflect_all:
            break  # normaler Schuss stirbt am Hindernis

        # Reflexion (reflect_all)
        ddx, ddy, ddz = reflect(ddx, ddy, ddz, *best_n)
        ox, oy, oz = hit_x, hit_y, hit_z

    return segments
