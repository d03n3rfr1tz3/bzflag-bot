"""
ObstacleGrid: Uniformes Spatial-Hash-Grid (Broad-Phase) über eine statische
Hindernisliste.

Aus nav_graph.py herausgelöst (W6), damit auch shot_physics.simulate_shot_path
das Grid nutzen kann (P1): nav_graph importiert bereits shot_physics — die
Gegenrichtung wäre ein Importzyklus. nav_graph re-exportiert ObstacleGrid und
die Grid-Konstanten, bestehende Importe bleiben gültig.
"""

import math
from typing import Dict, List, Tuple

from .world_map import BoxObstacle

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
TANK_HALF_WIDTH = 1.4        # = TANK_WIDTH/2 (Normal-Tank); der Planer ist flaggen-agnostisch und
                             # deckungsgleich zur reaktiven Decken-Kollision (_effective_half_width)
GRID_CELL = 16.0             # Zellgröße (u) des ObstacleGrid-Broad-Phase für die 60-Hz-Punkt-Queries
                             # (get_floor_z, Wall-Slide/Decken-Kollision, Box-Innen-Check).
GRID_PAD  = 0.5 + TANK_HALF_WIDTH + 0.01  # Polsterung der Box-AABB bei der Grid-Registrierung. Muss ≥ dem
                             # maximalen Prüf-Margin sein: get_floor_z testet auf half+0.5+overhang
                             # (overhang ≤ TANK_HALF_WIDTH), der Physik-Wall-Slide auf half+eff_half_width
                             # (≤ TANK_HALF_WIDTH). +0.01 Float-Sicherheit. Größer = mehr Kandidaten, aber nie
                             # False Negatives (die exakte Narrow-Phase entscheidet unverändert).
LOS_GRID_PAD = 0.5           # Pad des LoS-Ray-Grids (_los_grid). Die LoS-Narrow-Phase (Slab-Test) hat
                             # Margin 0 → Pad nur zur Float-Robustheit an Zellgrenzen; kleiner Wert hält die
                             # Kandidatenzahl je durchquerter Zelle niedrig.


class ObstacleGrid:
    """Uniformes Spatial-Hash-Grid über eine STATISCHE Hindernisliste (Broad-Phase).

    Ersetzt den linearen Scan über alle Hindernisse in den 60-Hz-Punkt-Abfragen (get_floor_z,
    Wall-Slide/Decken-Kollision, Box-Innen-Check). Jede Box wird — um ihre rotierte AABB plus
    ``pad`` geweitet — in alle überlappten Grid-Zellen eingetragen. ``query_point`` liefert dann nur
    die Kandidaten der Punkt-Zelle; die exakte Narrow-Phase (rotierter Box-Test) bleibt beim Aufrufer.

    Korrektheit (keine False Negatives): liegt ein Punkt innerhalb ``Box + margin`` mit
    ``margin ≤ pad``, dann überlappt die gepolsterte AABB die Punkt-Zelle → die Box ist dort
    registriert. Ein zu großes ``pad`` liefert nur mehr Kandidaten, nie zu wenige.
    """

    __slots__ = ("cell", "_grid", "_order")

    def __init__(self, boxes: List[BoxObstacle],
                 cell: float = GRID_CELL, pad: float = GRID_PAD) -> None:
        self.cell = cell
        self._grid: Dict[Tuple[int, int], List[BoxObstacle]] = {}
        # Eingabe-Reihenfolge (id→Index): stellt in query_segment die ursprüngliche Iterations-
        # Reihenfolge wieder her (die reaktive Kollision ist bei Mehrfach-Treffern reihenfolge-
        # abhängig). Bucket-Listen sind bereits in Eingabe-Reihenfolge (query_point braucht kein Sort).
        self._order: Dict[int, int] = {id(b): i for i, b in enumerate(boxes)}
        for box in boxes:
            ext_x = box.half_w * abs(box.cos_a) + box.half_d * abs(box.sin_a) + pad
            ext_y = box.half_w * abs(box.sin_a) + box.half_d * abs(box.cos_a) + pad
            gx0 = int(math.floor((box.cx - ext_x) / cell))
            gx1 = int(math.floor((box.cx + ext_x) / cell))
            gy0 = int(math.floor((box.cy - ext_y) / cell))
            gy1 = int(math.floor((box.cy + ext_y) / cell))
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    self._grid.setdefault((gx, gy), []).append(box)

    def query_point(self, x: float, y: float) -> List[BoxObstacle]:
        """Kandidaten-Boxen der Zelle, die (x, y) enthält (leer wenn keine)."""
        return self._grid.get((int(math.floor(x / self.cell)),
                               int(math.floor(y / self.cell))), _EMPTY_BOXES)

    def query_segment(self, x1: float, y1: float,
                      x2: float, y2: float) -> List[BoxObstacle]:
        """Kandidaten-Boxen über alle Zellen im Bounding-Rechteck der Strecke (x1,y1)-(x2,y2),
        jede Box höchstens einmal. Für die reaktive Wall-Slide-Kollision: der prädizierte Punkt
        (px+vx·dt, py+vy·dt) wandert innerhalb der Schleife (Geschwindigkeit wird geklemmt) — die
        Strecke ist sub-zellig, daher genügt das Zell-AABB (kein DDA), und die Dedup bewahrt die
        Ein-Box-pro-Iteration-Semantik der Originalschleife."""
        c = self.cell
        gx0 = int(math.floor(min(x1, x2) / c)); gx1 = int(math.floor(max(x1, x2) / c))
        gy0 = int(math.floor(min(y1, y2) / c)); gy1 = int(math.floor(max(y1, y2) / c))
        if gx0 == gx1 and gy0 == gy1:
            return self._grid.get((gx0, gy0), _EMPTY_BOXES)
        seen: set = set()
        out: List[BoxObstacle] = []
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                for box in self._grid.get((gx, gy), ()):
                    if id(box) not in seen:
                        seen.add(id(box))
                        out.append(box)
        out.sort(key=lambda b: self._order[id(b)])  # Eingabe-Reihenfolge wiederherstellen
        return out

    def query_ray(self, x1: float, y1: float,
                  x2: float, y2: float) -> List[BoxObstacle]:
        """DDA-Strahllauf (Amanatides-Woo): Kandidaten-Boxen aller Zellen, die die Strecke
        (x1,y1)-(x2,y2) kreuzt, jede Box höchstens einmal. Für LoS-/Wand-Strahlen, die quer über die
        Karte reichen können — besucht nur die tatsächlich durchquerten Zellen (nicht das ganze
        Bounding-Rechteck wie query_segment). Reihenfolge irrelevant (Aufrufer: any-hit bzw. min-t;
        die exakte 3D-Narrow-Phase entscheidet)."""
        c = self.cell
        gx = int(math.floor(x1 / c)); gy = int(math.floor(y1 / c))
        gx_end = int(math.floor(x2 / c)); gy_end = int(math.floor(y2 / c))
        if gx == gx_end and gy == gy_end:
            return self._grid.get((gx, gy), _EMPTY_BOXES)
        dx = x2 - x1; dy = y2 - y1
        # t ∈ [0,1] entlang der Strecke; t_max_* = t bis zur nächsten Zellgrenze, t_delta_* = t pro Zelle.
        if dx > 0:
            step_x = 1;  t_max_x = ((gx + 1) * c - x1) / dx; t_delta_x = c / dx
        elif dx < 0:
            step_x = -1; t_max_x = (gx * c - x1) / dx;       t_delta_x = -c / dx
        else:
            step_x = 0;  t_max_x = math.inf;                 t_delta_x = math.inf
        if dy > 0:
            step_y = 1;  t_max_y = ((gy + 1) * c - y1) / dy; t_delta_y = c / dy
        elif dy < 0:
            step_y = -1; t_max_y = (gy * c - y1) / dy;       t_delta_y = -c / dy
        else:
            step_y = 0;  t_max_y = math.inf;                 t_delta_y = math.inf
        seen: set = set()
        out: List[BoxObstacle] = []
        # Schrittobergrenze (Manhattan-Distanz der Zellen) sichert gegen Float-bedingte Endlos-Läufe.
        max_steps = abs(gx_end - gx) + abs(gy_end - gy) + 2
        for _ in range(max_steps + 1):
            for box in self._grid.get((gx, gy), ()):
                bid = id(box)
                if bid not in seen:
                    seen.add(bid)
                    out.append(box)
            if gx == gx_end and gy == gy_end:
                break
            if t_max_x < t_max_y:
                gx += step_x; t_max_x += t_delta_x
            else:
                gy += step_y; t_max_y += t_delta_y
        return out


_EMPTY_BOXES: List[BoxObstacle] = []
