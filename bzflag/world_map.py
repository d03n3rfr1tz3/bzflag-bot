"""
Datenklassen für geparste BZFlag-Weltdaten (WorldMap).

Koordinaten-Konvention (aus BZFlag-Quellcode BoxBuilding::setExtents):
  pos.x, pos.y = Mittelpunkt X/Y
  pos.z        = Unterkante Z (pos.z + height = Oberkante)
  size[0/1]    = Halb-Ausdehnungen (half_w/half_d) in X/Y bei angle=0
  size[2]      = volle Höhe
  angle        = Rotation um Z-Achse in Radiant (Yaw)
"""

import math
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class BoxObstacle:
    """Box- oder Pyramiden-Obstacle."""
    cx: float        # Mittelpunkt X
    cy: float        # Mittelpunkt Y
    bottom_z: float  # Unterkante Z
    angle: float     # Yaw-Rotation in Radiant
    half_w: float    # Halb-Breite (X bei angle=0)
    half_d: float    # Halb-Tiefe  (Y bei angle=0)
    height: float    # volle Höhe
    drive_through: bool = False
    shoot_through: bool = False
    is_pyramid: bool = False   # True = PyramidBuilding (schräge Flächen)
    z_flip: bool    = False    # True = invertierte Pyramide (_FLIP_Z 0x04)
    ricochet: bool  = False    # True = Schüsse prallen ab (_RICOCHET 0x08)

    # Vorberechnete, abgeleitete Werte (Hindernisse sind nach dem Welt-Laden STATISCH — angle,
    # bottom_z und height werden nie mutiert). Sparen die per-Aufruf-Trigonometrie in den heißen
    # Kollisions-/Boden-/LoS-Schleifen (get_floor_z, _apply_obstacle_bounds, _segment_clear, …).
    # World→Local-Konvention überall: lx = dx*cos_a + dy*sin_a; ly = -dx*sin_a + dy*cos_a.
    cos_a: float  = field(init=False, repr=False)  # cos(angle)
    sin_a: float  = field(init=False, repr=False)  # sin(angle)
    roof_z: float = field(init=False, repr=False)  # bottom_z + height (Oberkante)

    def __post_init__(self) -> None:
        self.cos_a  = math.cos(self.angle)
        self.sin_a  = math.sin(self.angle)
        self.roof_z = self.bottom_z + self.height


@dataclass
class TeleporterObstacle:
    """Teleporter-Obstacle (für Phase 3 aktiv nutzbar).

    half_d/height sind die EFFEKTIVEN (finalisierten) Maße — der Parser repliziert
    BZFlag Teleporter::finalize() (vertikal: half_d=origSize+2*border, height=origSize+border).
    Damit gilt für das Querungsfeld direkt getBreadth()-border = half_d-border,
    getHeight()-border = height-border."""
    name: str
    cx: float
    cy: float
    bottom_z: float
    angle: float
    half_w: float
    half_d: float
    height: float
    border: float
    horizontal: bool = False


@dataclass
class WorldMap:
    """Geparste Karten-Geometrie eines BZFlag-Servers."""
    boxes: List[BoxObstacle] = field(default_factory=list)
    teleporters: List[TeleporterObstacle] = field(default_factory=list)
    links: List[Tuple[int, int]] = field(default_factory=list)  # (from_face, to_face)
    world_half: float = 200.0
    world_hash: str = ""


# ---------------------------------------------------------------------------
# Teleporter-Kollisionsgeometrie (Port von Teleporter::inBox, P3-NAV-02)
# ---------------------------------------------------------------------------

def teleporter_solid_boxes(tele: TeleporterObstacle) -> List[BoxObstacle]:
    """Die soliden Teile eines Teleporters als BoxObstacle: zwei Seiten-Posts + Crossbar.

    Port von Teleporter::inBox (src/obstacle/Teleporter.cxx): die zwei „border columns" sind
    Quadrate (Halbgröße r = border/2) im Abstand d = breadth - border/2 seitlich der Mitte und
    reichen vom Boden bis zur Crossbar-Unterkante; der Crossbar (oberer Querbalken) spannt die
    volle Breite/Tiefe von der Crossbar-Unterkante bis zur Teleporter-Oberkante.

    Diese Boxen sind Fahr-/Sprung-Kollision (Planer + reaktiv) — eine Quelle der Wahrheit. Das
    Querungsfeld dazwischen bleibt frei (siehe teleporter_field_box)."""
    border = tele.border
    tele_top = tele.bottom_z + tele.height
    crossbar_bottom = tele_top - border
    d = tele.half_d - 0.5 * border
    r = 0.5 * border
    c = math.cos(tele.angle)
    s = math.sin(tele.angle)
    post_h = max(0.0, crossbar_bottom - tele.bottom_z)
    boxes: List[BoxObstacle] = []
    for sign in (1.0, -1.0):
        boxes.append(BoxObstacle(
            cx=tele.cx - sign * s * d, cy=tele.cy + sign * c * d,
            bottom_z=tele.bottom_z, angle=tele.angle,
            half_w=r, half_d=r, height=post_h,
            drive_through=False, shoot_through=False,
        ))
    boxes.append(BoxObstacle(
        cx=tele.cx, cy=tele.cy,
        bottom_z=crossbar_bottom, angle=tele.angle,
        half_w=tele.half_w, half_d=tele.half_d, height=border,
        drive_through=False, shoot_through=False,
    ))
    return boxes


def teleporter_field_box(tele: TeleporterObstacle) -> BoxObstacle:
    """Das Querungsfeld (lokales x≈0) als reines Mark-Hilfsobjekt.

    NICHT solide — wird nur genutzt, um die NavGraph-Layer-Zellen im Spalt als non-walkable zu
    sperren, damit A* ausschließlich über die vorberechnete Portal-Kante quert. Darf NIE in
    NavGraph._obs oder die reaktive Kollision wandern (das Feld ist begeh-/befahrbar, um den
    Teleport auszulösen, und ist keine Steh-/Lande-Fläche)."""
    border = tele.border
    return BoxObstacle(
        cx=tele.cx, cy=tele.cy, bottom_z=tele.bottom_z, angle=tele.angle,
        half_w=0.5 * border, half_d=max(0.0, tele.half_d - border),
        height=max(0.0, tele.height - border),
        drive_through=False, shoot_through=False,
    )
