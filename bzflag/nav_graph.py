"""
NavGraph: 3D-Navigationsgraph für BZFlag.

Aufbau (einmalig nach Weltdownload):
  1. Weltboden-Layer (z=0): ±world_half, Zellen WALKABLE/BLOCKED
  2. Dach-Layer pro Gebäude (z = bottom_z + height): WALKABLE im begehbaren Bereich
  3. Sprung-/Fall-Kanten werden während A* on-the-fly berechnet

A*-Knoten: (layer_id, ix, iy)

Teilt NavGraph-Instanzen zwischen Bot-Instanzen desselben Servers (shared Cache
nach world_hash, sodass mehrere Bots nicht doppelt berechnen).
"""

import heapq
import logging
import math
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from .world_map import (BoxObstacle, WorldMap,
                        teleporter_solid_boxes, teleporter_field_box)
from .shot_physics import build_link_map, teleport_through
# ObstacleGrid + Grid-Konstanten leben seit W6 in obstacle_grid.py (P1 nutzt sie
# in shot_physics; ein Import in Gegenrichtung wäre ein Zyklus). Der Re-Export
# hier hält bestehende Importe (Tests, Aufrufer) unverändert gültig.
from .obstacle_grid import (ObstacleGrid, GRID_CELL, GRID_PAD,   # noqa: F401
                            LOS_GRID_PAD, TANK_HALF_WIDTH)

logger = logging.getLogger("bzbot")

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
CELL_SIZE    = 4             # Rastergröße in BZFlag-Einheiten
TANK_MARGIN  = 3.5           # Rand-Puffer = ceil(TANK_HALF_DIAG≈3.31); physikalisch korrekte Mindestdistanz
THIN_WALL_MARGIN = 1.4       # = TANK_WIDTH/2; reduzierter Rand-Puffer für dünne Wände, damit schmale
                             # Laufstege seitlich an der Wand befahrbar bleiben (bewusst enger als TANK_MARGIN)
TANK_HEIGHT  = 2.05          # BZFlag-Standard-Tankhöhe (half_height=1.0 + Spielraum)
# TANK_HALF_WIDTH: kanonische Definition seit W6 in obstacle_grid.py (re-importiert oben)
JUMP_RANGE   = 95.0          # Max. aabb_dist für Sprung-Kanten (Abstiegsformel, dz≥5: max ~90u)
JUMP_EDGE_TOL = 1.4          # = TANK_WIDTH/2: zulässiger Überhang des Tank-Mittelpunkts über
                             # Plattformkanten. BZFlag-Physik: der Tank trägt/landet, solange auch
                             # nur ein Pixel seiner Hitbox aufliegt — der Mittelpunkt darf also um
                             # bis zu eine halbe Tankbreite über die Kante hinausragen. Gilt für den
                             # Absprung (Überhang am Quellrand) wie für die Landung (Front-Catch).
MAX_ROOF_H   = 55.0          # Maximale Dach-Höhe für Roof-Layer (≈ 3 × max_jump_h)
_ASTAR_WEIGHT = 1.5          # Weighted/Epsilon-optimal A*: 2–4× schneller, max. 50% suboptimal
NAV_JUMP_UP_PENALTY = 150.0  # Sicherheits-Aufschlag auf Sprung-hoch-Kanten NUR auf Teleporter-Karten:
                             # ein verwundbarer Sprung-Arc ist riskanter als die Tor-Fahrt → der Bot nimmt
                             # bis ~150u Umweg zu einem Tor in Kauf. Verbietet Sprünge nicht (nur verteuert),
                             # nur-per-Sprung erreichbare Orte bleiben erreichbar.
ASTAR_MAX_EXPANSIONS = 5000  # Knoten-Expansionslimit pro A*-Suche. Die hohe Sprung-Penalty bläht die
                             # Suche auf (Heuristik kennt die Penalty nicht); bei Limit-Treffer liefert
                             # _astar den Best-Effort-Teilpfad zum zielnächsten Knoten statt []. Per-Knoten-
                             # Kosten gemessen ~13-34µs auf HIX (nach _build_vertical_adjacency; davor ~25-45µs
                             # bei 57-Layer-Scan/Knoten) → Worst-Case bei 5000 ≈ ~140ms (vorher ~480-645ms bei
                             # 15000), nur bei Replan. Zusätzlich durch ASTAR_MAX_MS gedeckelt. Modulkonstante →
                             # Tests patchbar. (P4: 50k-Zweitthread parallel.)
ASTAR_MAX_MS = 125.0         # Wall-Clock-Budget pro A*-Suche (ms). Alle 1024 Expansionen geprüft; bei
                             # Überschreitung → gleicher Best-Effort-Teilpfad wie beim Knotenlimit. Robuster
                             # als reine Knotenzahl, da µs/Knoten mit der Layer-Dichte schwankt. Tests patchbar.
# GRID_CELL/GRID_PAD/LOS_GRID_PAD: seit W6 in obstacle_grid.py (re-importiert oben)

DIRS_8 = [(-1, -1), (-1, 0), (-1, 1),
          (0,  -1),           (0,  1),
          (1,  -1), (1,  0), (1,  1)]
_INF = float("inf")

# Modul-Level Cache: world_hash → NavGraph
_nav_cache: Dict[str, "NavGraph"] = {}


def get_nav_graph(world_map: WorldMap, max_jump_h: Optional[float] = None,
                  v0: float = 19.0, g: float = 9.8) -> "NavGraph":
    """Gibt einen gecachten NavGraph für diese WorldMap zurück. ``v0``/``g`` sind die (globalen)
    Server-Variablen _jumpVelocity/_gravity; ``max_jump_h=None`` → aus v0/g abgeleitet."""
    key = world_map.world_hash or str(id(world_map))
    if key not in _nav_cache:
        _nav_cache[key] = NavGraph(world_map, max_jump_h, v0, g)
    return _nav_cache[key]


def invalidate_nav_cache(world_hash: str) -> None:
    """Entfernt einen Cache-Eintrag, damit der NavGraph beim nächsten Aufruf neu gebaut wird.
    Nötig wenn world_half sich nach dem ersten _deliver_world() ändert (MsgSetVar _worldSize
    kommt in BZFlag 2.4 nach dem Welt-Download)."""
    _nav_cache.pop(world_hash, None)


# ---------------------------------------------------------------------------
# FloorLayer
# ---------------------------------------------------------------------------

class FloorLayer:
    """Eine navigierbare Bodenfläche (Weltboden oder Gebäudedach)."""

    __slots__ = ("z", "cx", "cy", "half_w", "half_d",
                 "n_x", "n_y", "walkable", "source_obstacle")

    def __init__(self, z: float, cx: float, cy: float,
                 half_w: float, half_d: float,
                 n_x: int, n_y: int,
                 walkable: List[bytearray],
                 source_obstacle: Optional[BoxObstacle]) -> None:
        self.z             = z
        self.cx            = cx
        self.cy            = cy
        self.half_w        = half_w
        self.half_d        = half_d
        self.n_x           = n_x
        self.n_y           = n_y
        self.walkable      = walkable
        self.source_obstacle = source_obstacle

    def world_to_cell(self, wx: float, wy: float) -> Tuple[int, int]:
        ix = int((wx - (self.cx - self.half_w)) / CELL_SIZE)
        iy = int((wy - (self.cy - self.half_d)) / CELL_SIZE)
        return ix, iy

    def cell_to_world(self, ix: int, iy: int) -> Tuple[float, float]:
        wx = (self.cx - self.half_w) + (ix + 0.5) * CELL_SIZE
        wy = (self.cy - self.half_d) + (iy + 0.5) * CELL_SIZE
        return wx, wy

    def contains_xy(self, x: float, y: float) -> bool:
        return abs(x - self.cx) <= self.half_w and abs(y - self.cy) <= self.half_d

    def clamp_cell(self, ix: int, iy: int) -> Tuple[int, int]:
        return max(0, min(self.n_x - 1, ix)), max(0, min(self.n_y - 1, iy))

    def is_walkable_xy(self, x: float, y: float) -> bool:
        ix, iy = self.world_to_cell(x, y)
        if not (0 <= ix < self.n_x and 0 <= iy < self.n_y):
            return False
        return bool(self.walkable[iy][ix])

    def has_any_walkable(self) -> bool:
        for row in self.walkable:
            if any(row):
                return True
        return False


# ---------------------------------------------------------------------------
# NavGraph
# ---------------------------------------------------------------------------

class NavGraph:
    """3D-Navigationsgraph (Boden + Dächer + Sprung-/Fall-Kanten)."""

    def __init__(self, world_map: WorldMap, max_jump_h: Optional[float] = None,
                 v0: float = 19.0, g: float = 9.8) -> None:
        # Nur Hindernisse die tatsächlich blockieren (nicht "drive_through" wie Teleporter-Felder).
        # P3-NAV-02: Teleporter-Posts + Crossbar sind solide Fahr-/Sprung-Kollision → in _obs
        # (Layer-Block, get_floor_z, _undersides), aber NICHT in _los_obs (Schuss-/LoS-Logik
        # P3-NAV-01 bleibt unberührt). Das Querungsfeld bleibt frei (separater Layer-Block unten).
        self._teleporters = world_map.teleporters
        self._obs        = [b for b in world_map.boxes if not b.drive_through]
        for _t in self._teleporters:
            self._obs.extend(teleporter_solid_boxes(_t))
        self._los_obs    = [b for b in world_map.boxes if not b.shoot_through]
        # Broad-Phase-Grid über die soliden Boxen (statisch) für die 60-Hz-Punkt-Queries:
        # get_floor_z sowie die reaktive Physik (Wall-Slide/Decken-Kollision, Box-Innen-Check in
        # bot/ai, die denselben Kandidatensatz world_map.boxes+tele_solid − drive_through nutzen).
        self._solid_grid = ObstacleGrid(self._obs)
        # Eigenes Ray-Grid über die LoS-Boxen (not shoot_through) für _segment_clear/_steep_wall_ahead
        # (query_ray). Anderer Kandidatensatz als _obs → separates Grid.
        self._los_grid = ObstacleGrid(self._los_obs, pad=LOS_GRID_PAD)
        # NAV-19: Hindernisse nach Unterkante (bottom_z) gruppieren — meist nur wenige distinkte
        # Höhen. Erlaubt den Sprungbogen-Überhang-Check via geschlossener Kopf-Kreuzung pro Höhe
        # (kein Sampling) statt eines Scans über alle Boxen. Siehe _arc_clears_overhangs.
        self._undersides: Dict[float, List[BoxObstacle]] = {}
        for _o in self._obs:
            self._undersides.setdefault(round(_o.bottom_z, 1), []).append(_o)
        self._world_half = world_map.world_half
        # Sprungphysik aus den (globalen) Server-Variablen _jumpVelocity/_gravity. Das Höhen-Gate
        # _max_jump_h und das Bogen-Timing (_v0/_g in _compute_vertical_edges) stammen aus DERSELBEN
        # Quelle → konsistent. max_jump_h=None → aus v0/g ableiten. Späte MsgSetVar: set_physics().
        self._v0         = v0     # Sprung-Anfangsgeschwindigkeit in u/s
        self._g          = g      # Schwerkraft in u/s² (positiver Betrag)
        self._max_jump_h = max_jump_h if max_jump_h is not None else v0 * v0 / (2.0 * g)
        self._tank_speed = 25.0   # horizontale Fahrgeschwindigkeit für Sprung-Kanten (Basis; per-Flag-Boost via plan_path-Param)
        # gecooldownte Sprung-Ziele (NAV-14) werden NICHT mehr auf self gehalten, sondern pro
        # plan_path() als Parameter durch _astar/_vertical_neighbors gereicht → reentrant (P4-INF-01).
        self._debug_path: bool = False
        self.layers: List[FloorLayer] = []

        self._thin_blocked: set = set()
        # P3-NAV-02: vorberechnete Teleporter-Portal-Kanten (entry_node → (exit_node, cost)).
        self._teleport_edges: Dict[Tuple[int, int, int], Tuple[Tuple[int, int, int], float]] = {}
        self._tele_exit_wps: set = set()  # Welt-(x,y) der Exit-Knoten (Cross-Floor-Sprung-Guards)
        # P3-NAV-02 (NAV_TELE): Austritts-WP-(x,y) → (cx,cy) der Quell-Tele, in deren Mitte der Bot
        # das letzte Stück direkt hineinfährt (statt am mittenseitigen Exit-WP davor zu stoppen).
        self._tele_cross_centers: Dict[Tuple[float, float], Tuple[float, float]] = {}
        # Vorberechnete same-z-Nachbarschaft der Dach-Layer (berührende Footprint-AABBs gleicher Höhe).
        self._same_z_touch: Dict[int, List[int]] = {}
        # Vorberechnete vertikale Kandidaten-Layer je Quell-Layer (konservative Footprint-AABB-Filter):
        # Sprung-rauf (innerhalb JUMP_RANGE) bzw. Fall-runter (Spalten-Überlappung). _vertical_neighbors
        # prüft dann nur diese wenigen statt aller Layer — exakte Geometrie/Feasibility bleibt pro-Knoten.
        self._jump_up_cands: Dict[int, List[int]] = {}
        self._fall_cands: Dict[int, List[int]] = {}
        # Ergebnis-Cache der Sprungkanten-Planung: (lid, ix, iy, tank_speed) → Liste
        # (neighbor, cost, block_key). Die Topologie ist nach dem Welt-Laden statisch (walkable wird
        # nur in __init__ mutiert), tank_speed steckt im Key (Flaggen-Boost selbst-invalidierend). Der
        # laufzeit-dynamische NAV-14-Filter (blocked_jump_wps) wird über block_key erst beim Lesen
        # angewandt. Von beiden Threads geteilt — Writes idempotent (deterministisch), kein Lock nötig.
        self._vn_cache: Dict[Tuple[int, int, int, float], List] = {}
        self._build_ground_layer()
        self._build_roof_layers()
        self._build_same_z_adjacency()
        self._build_vertical_adjacency()
        self._precompute_thin_wall_blocked()
        self._build_teleport_edges(world_map)

        # Zähle begehbare Zellen über alle Etagen — erscheint im Log zur Diagnose
        walkable_count = sum(
            sum(sum(1 for w in row if w) for row in lyr.walkable)
            for lyr in self.layers
        )
        logger.info(
            "[PTH] NavGraph: %d Etagen, %d begehbare Zellen gesamt",
            len(self.layers), walkable_count,
        )

    # ── Layer-Aufbau ──────────────────────────────────────────────────────

    def _build_ground_layer(self) -> None:
        half = self._world_half
        # Rasteranzahl: gesamte Kartenbreite durch Zellgröße
        n = max(1, int(2.0 * half / CELL_SIZE))
        # P9: bytearray (0/1) statt List[List[bool]] — bessere Cache-Lokalität (ein zusammen-
        # hängender Byte-Block pro Zeile statt N einzelner PyObject-bool-Referenzen).
        walkable = [bytearray(b"\x01" * n) for _ in range(n)]  # alle Zellen zunächst frei
        ground = FloorLayer(z=0.0, cx=0.0, cy=0.0, half_w=half, half_d=half,
                            n_x=n, n_y=n, walkable=walkable,
                            source_obstacle=None)
        # Nur Obstacles einzeichnen, die den Tank am Boden sperren (vertikale Überlappung
        # mit der Tank-Box z=0..TANK_HEIGHT). Obstacles hoch über dem Boden (z.B. Wände ab
        # z=15) werden nicht geblockt, weil der Bot darunter frei fahren kann.
        for obs in self._obs:
            if _obstacle_blocks_layer(obs, 0.0):
                _mark_blocked(ground, obs, _margin_for(obs, 0.0))
        # P3-NAV-02: Teleporter-Querungsfeld als Layer-Wand sperren (NICHT in _obs, sonst würde
        # get_floor_z das offene Feld als Steh-/Lande-Fläche sehen). A* quert nur per Portal-Kante.
        for tele in self._teleporters:
            fb = teleporter_field_box(tele)
            if _obstacle_blocks_layer(fb, 0.0):
                _mark_blocked(ground, fb, _margin_for(fb, 0.0))
        self.layers.append(ground)

    def _build_roof_layers(self) -> None:
        for obs in self._obs:
            roof_z = obs.bottom_z + obs.height
            # Dächer über MAX_ROOF_H überspringen — zu hoch zum Draufspringen
            if roof_z > MAX_ROOF_H:
                continue
            # Begehbare Fläche auf dem Dach = AABB des Gebäudes − TANK_MARGIN auf jeder Seite
            # (AABB weil Dach-Layer achsenparallel ausgerichtet ist)
            cos_a = abs(obs.cos_a)
            sin_a = abs(obs.sin_a)
            ext_x = obs.half_w * cos_a + obs.half_d * sin_a
            ext_y = obs.half_w * sin_a + obs.half_d * cos_a
            w = ext_x - TANK_MARGIN
            d = ext_y - TANK_MARGIN
            if w < CELL_SIZE or d < CELL_SIZE:
                continue  # zu schmal für eine begehbare Dachfläche
            n_x = max(1, int(2.0 * w / CELL_SIZE))
            n_y = max(1, int(2.0 * d / CELL_SIZE))
            walkable = [bytearray(b"\x01" * n_x) for _ in range(n_y)]  # alle Dachzellen zunächst frei
            roof = FloorLayer(z=roof_z, cx=obs.cx, cy=obs.cy,
                              half_w=w, half_d=d,
                              n_x=n_x, n_y=n_y, walkable=walkable,
                              source_obstacle=obs)
            # Zellen außerhalb des tatsächlichen rotierten Obstacle-Footprints blockieren.
            # Verhindert, dass AABB-überdimensionierte Layer bei diagonalen Obstacles
            # fälschlicherweise riesige walkable-Flächen erzeugen.
            _clip_to_footprint(roof, obs, TANK_MARGIN)
            # Blockiere Zellen durch Obstacles, die den Tank auf diesem Dach sperren —
            # sowohl Aufbauten (beginnen auf Dachhöhe) als auch Wände, die UNTER dem Dach
            # beginnen und durch die Tank-Höhe nach oben stoßen (vertikale Überlappung,
            # siehe _obstacle_blocks_layer). Dünne Wände nutzen reduzierten Margin, damit
            # schmale Laufstege seitlich an der Wand befahrbar bleiben.
            for obs2 in self._obs:
                if obs2 is obs:
                    continue
                if _obstacle_blocks_layer(obs2, roof_z):
                    _mark_blocked(roof, obs2, _margin_for(obs2, roof_z))
            # Dach-Layer nur hinzufügen wenn mindestens eine Zelle tatsächlich begehbar ist
            if not roof.has_any_walkable():
                continue
            self.layers.append(roof)

    def _precompute_thin_wall_blocked(self) -> None:
        """Berechnet verbotene Wegpunkt-Paare für dünne Obstacles einmalig beim Build.

        Dünne Obstacles (min(half_w, half_d)*2 < CELL_SIZE) können zwischen zwei
        Rasterzellen durchfallen: A* umkurvt sie korrekt, aber _smooth_path kann
        dabei entstehende Direktlinien erzeugen die die Wand schneiden. Die hier
        berechneten Paare werden in _smooth_path per O(1)-Lookup geprüft.
        """
        thin_obs = [o for o in self._obs if min(o.half_w, o.half_d) * 2 < CELL_SIZE]
        if not thin_obs:
            return

        for obs in thin_obs:
            cos_a = abs(obs.cos_a)
            sin_a = abs(obs.sin_a)
            ext_x = obs.half_w * cos_a + obs.half_d * sin_a + CELL_SIZE * 3
            ext_y = obs.half_w * sin_a + obs.half_d * cos_a + CELL_SIZE * 3

            for layer in self.layers:
                # Identischer z-Filter wie beim Layer-Build (_obstacle_blocks_layer):
                # nur Etagen, auf denen die Wand den Tank physisch sperrt. Muss exakt
                # mit dem Build übereinstimmen, sonst werden Paare für Etagen erzeugt,
                # auf denen die Wand gar keine Zellen blockiert → Edge-Aushungerung.
                if not _obstacle_blocks_layer(obs, layer.z):
                    continue

                ix0, iy0 = layer.world_to_cell(obs.cx - ext_x, obs.cy - ext_y)
                ix1, iy1 = layer.world_to_cell(obs.cx + ext_x, obs.cy + ext_y)
                ix0, iy0 = max(0, ix0), max(0, iy0)
                ix1, iy1 = min(layer.n_x - 1, ix1), min(layer.n_y - 1, iy1)

                cells: List[Tuple[float, float]] = []
                for iy in range(iy0, iy1 + 1):
                    for ix in range(ix0, ix1 + 1):
                        if layer.walkable[iy][ix]:
                            cells.append(layer.cell_to_world(ix, iy))

                z = layer.z
                for i in range(len(cells)):
                    for j in range(i + 1, len(cells)):
                        wx1, wy1 = cells[i]
                        wx2, wy2 = cells[j]
                        if _segment_crosses_thin_obs(wx1, wy1, wx2, wy2, obs):
                            self._thin_blocked.add((wx1, wy1, wx2, wy2, z))
                            self._thin_blocked.add((wx2, wy2, wx1, wy1, z))

        if self._thin_blocked:
            logger.debug("[PTH] thin_blocked: %d verbotene Wegpunkt-Paare vorberechnet",
                         len(self._thin_blocked) // 2)

    # ── Bodenhöhe (60 Hz) ─────────────────────────────────────────────────

    def get_floor_z(self, x: float, y: float, z: float, overhang: float = 0.0) -> float:
        """Höchste Bodenfläche unterhalb von (x, y) bei Höhe z.

        overhang > 0 weitet den Auflage-Test um diese Strecke (Pixel-on-Regel: der Tank wird
        getragen, solange auch nur ein Pixel seiner Hitbox auf der Fläche liegt — die Mitte
        darf bis ~Tank-Halbbreite über die Kante hinaus). Default 0.0 = exakter Mittelpunkt-Test.
        """
        floor_z = 0.0
        # Broad-Phase: nur Boxen der Punkt-Zelle statt linear über alle _obs (Ergebnis identisch,
        # das Grid ist eine korrekt gepolsterte Übermenge — s. ObstacleGrid).
        for obs in self._solid_grid.query_point(x, y):
            roof_z = obs.roof_z
            # Dächer die weit über dem Bot hängen überspringen (2u Puffer wegen Landung); tiefere
            # Dächer als das bisherige Maximum können es ohnehin nicht heben → früher Cut.
            if roof_z > z + 2.0 or roof_z <= floor_z:
                continue
            # Nur wenn der Bot wirklich über diesem Gebäude steht (Box-Test; Pixel-on via overhang)
            if _point_in_rotated_box(obs, x, y, margin=overhang):
                floor_z = roof_z
        return floor_z

    def find_layer_at(self, x: float, y: float, z: float) -> int:
        """Gibt den layer_id der Etage zurück, auf der der Bot steht."""
        best_lid = 0
        best_z   = -1.0
        for lid, layer in enumerate(self.layers):
            # Layer überspringen die höher sind als der Bot (0.5u Toleranz)
            if layer.z > z + 0.5:
                continue
            # Dach-Layer: nur wenn Bot tatsächlich über diesem Gebäude steht
            if layer.source_obstacle is not None and not _point_in_rotated_box(layer.source_obstacle, x, y):
                continue
            # Höchste passende Etage gewinnt (Bot steht auf dem höchsten Boden unter ihm)
            if layer.z > best_z:
                best_z   = layer.z
                best_lid = lid
        return best_lid

    # ── Pfadplanung ───────────────────────────────────────────────────────

    def plan_path(
        self, sx: float, sy: float, sz: float, gx: float, gy: float,
        blocked_jump_wps=None, goal_z: float | None = None,
        max_expansions: int | None = None, max_ms: float | None = None,
        cancel=None, label: str = "A*", partial_level: int = logging.WARNING,
        tank_speed: float | None = None,
    ) -> List[Tuple[float, float, float]]:
        """
        A*-Pfadsuche von (sx,sy,sz) nach (gx,gy).
        Rückgabe: Liste von (wx, wy, layer_z)-Wegpunkten oder [].

        Reentrant: ``blocked_jump_wps``, ``max_expansions``, ``max_ms``, ``cancel`` und
        ``tank_speed`` werden als lokale Parameter durch ``_astar`` gereicht — kein geteilter
        Mutable-Zustand auf ``self``. So können Haupt- und Hintergrund-Thread denselben gecachten
        Graph parallel beplanen (P4-INF-01). ``cancel`` ist ein Objekt mit ``.is_set()`` (z.B.
        threading.Event). ``tank_speed`` (None → Basis ``self._tank_speed`` 25.0) ist die
        horizontale Sprung-Reisegeschwindigkeit; höher (Velocity/Thief) ⇒ weitere Sprünge machbar,
        deckungsgleich zum reaktiven Executor (bot/ai/capabilities._travel_tank_speed).
        """
        bjw = blocked_jump_wps or frozenset()
        # Start-Knoten
        start_lid = self.find_layer_at(sx, sy, sz)
        start_layer = self.layers[start_lid]
        six, siy = start_layer.world_to_cell(sx, sy)
        six, siy = start_layer.clamp_cell(six, siy)
        start_was_nonwalkable = not start_layer.walkable[siy][six]
        if start_was_nonwalkable:
            six, siy = _nearest_walkable(start_layer, six, siy, max_r=20)
            if six < 0:
                if self._debug_path:
                    logger.debug("Pfad: Kein Pfad – Start non-walkable+isolated (%.0f,%.0f)", sx, sy)
                return []
        start = (start_lid, six, siy)

        # Ziel-Knoten-Set (alle Layer, die (gx,gy) enthalten)
        goal_set: set = set()
        for lid, layer in enumerate(self.layers):
            if not layer.contains_xy(gx, gy):
                continue
            gix, giy = layer.world_to_cell(gx, gy)
            gix, giy = layer.clamp_cell(gix, giy)
            if not layer.walkable[giy][gix]:
                gix, giy = _nearest_walkable(layer, gix, giy, max_r=20)
                if gix < 0:
                    continue
            goal_set.add((lid, gix, giy))

        # goal_z: nur Zellen auf Zielhöhe — kein Fallback auf andere Ebenen
        if goal_z is not None and goal_set:
            filtered = {node for node in goal_set
                        if abs(self.layers[node[0]].z - goal_z) <= TANK_HEIGHT}
            if filtered:
                goal_set = filtered
            else:
                return []  # kein Layer auf Zielhöhe → Route nicht möglich

        if not goal_set:
            # Kein Ziel in bekannter Layer → Boden-Fallback
            g_layer = self.layers[0]
            gix, giy = g_layer.world_to_cell(gx, gy)
            gix, giy = g_layer.clamp_cell(gix, giy)
            if not g_layer.walkable[giy][gix]:
                gix, giy = _nearest_walkable(g_layer, gix, giy, max_r=20)
                if gix < 0:
                    if self._debug_path:
                        logger.debug("Pfad: Kein Pfad – Ziel-Fallback non-walkable (%.0f,%.0f)→(%.0f,%.0f)",
                                     sx, sy, gx, gy)
                    return []
            goal_set.add((0, gix, giy))

        # A*
        path_nodes = self._astar(start, goal_set, gx, gy, goal_z=goal_z,
                                 blocked_jump_wps=bjw, max_expansions=max_expansions,
                                 max_ms=max_ms, cancel=cancel,
                                 label=label, partial_level=partial_level,
                                 tank_speed=tank_speed)
        if not path_nodes:
            if self._debug_path:
                logger.debug(
                    "Pfad: Kein Pfad – start=%s nonwalk=%s goal_set=%d (%.0f,%.0f)→(%.0f,%.0f)",
                    start, start_was_nonwalkable, len(goal_set), sx, sy, gx, gy,
                )
            if start_was_nonwalkable:
                wx, wy = start_layer.cell_to_world(six, siy)
                return [(wx, wy, start_layer.z)]
            return []

        # In Weltkoordinaten umwandeln
        waypoints = []
        keep_idx: set = set()      # P3-NAV-02: Portal-Wegpunkte vor dem Smoothing schützen
        prev_z:  Optional[float] = None
        prev_wx: Optional[float] = None
        prev_wy: Optional[float] = None
        prev_node: Optional[Tuple] = None
        for idx, (lid, ix, iy) in enumerate(path_nodes):
            layer = self.layers[lid]
            wx, wy = layer.cell_to_world(ix, iy)
            # Teleport-Hop (prev_node → current via Portal-Kante)? Entry+Exit vor dem Smoothing fix
            # halten (sonst als kollinear weggekürzt). Bei Cross-Floor-Toren (z=30-Austritt) ist der
            # Hop NICHT z-konstant.
            is_tele_exit = False
            if prev_node is not None:
                e = self._teleport_edges.get(prev_node)
                if e is not None and e[0] == (lid, ix, iy):
                    is_tele_exit = True
                    keep_idx.add(idx - 1)
                    keep_idx.add(idx)
            # Sprung-Landung (Etage steigt): Wegpunkt auf den realen Footprint-Eintrittspunkt
            # (Plattformkante / Diamant-Spitze) statt der weit innen geklemmten Zellmitte legen —
            # deckungsgleich mit der Erreichbarkeitsprüfung in _vertical_neighbors. Sonst bekäme der
            # Bot ein zu weit innen liegendes Sprungziel und verwirft den Sprung am Feasibility-Rand
            # (rotierte z=30-Randplattformen). Der A*-Knoten (Zelle) bleibt unverändert.
            # NICHT für Teleport-Exits: die werden per Tor durchquert (reaktiv), nicht angesprungen —
            # ihre reale Austrittsposition (z.B. z=30 am Ziel-Tor) muss erhalten bleiben.
            if prev_z is not None and layer.z - prev_z > 1.5 and not is_tele_exit:
                # prev_wx/prev_wy werden stets zusammen mit prev_z gesetzt → hier nicht None
                assert prev_wx is not None and prev_wy is not None
                wx, wy, _ = _entry_point(layer, prev_wx, prev_wy)
            waypoints.append((wx, wy, layer.z))
            prev_z, prev_wx, prev_wy = layer.z, wx, wy
            prev_node = (lid, ix, iy)

        return _insert_jump_runups(
            _smooth_path(waypoints, self._thin_blocked, keep_idx), self)

    # ── A* ────────────────────────────────────────────────────────────────

    def _astar(
        self, start: Tuple, goal_set: set, gx: float, gy: float,
        goal_z: float | None = None, blocked_jump_wps=frozenset(),
        max_expansions: int | None = None, max_ms: float | None = None,
        cancel=None, label: str = "A*", partial_level: int = logging.WARNING,
        tank_speed: float | None = None,
    ) -> List[Tuple]:
        if start in goal_set:
            return [start]
        me = ASTAR_MAX_EXPANSIONS if max_expansions is None else max_expansions
        mm = ASTAR_MAX_MS if max_ms is None else max_ms

        g_score   = {start: 0.0}
        came_from: Dict[Tuple, Optional[Tuple]] = {start: None}
        closed:   set = set()
        counter   = 1
        # Best-Effort-Fallback: dem Ziel nächster bisher expandierter Knoten. Bei Limit-Treffer
        # liefern wir den Teilpfad dorthin statt [] — sonst fällt der Bot auf den Blind-Direktweg
        # zurück (rammt das Gebäude statt das Tor zu nehmen).
        best_node = start
        best_h    = _h(self.layers[start[0]], start[1], start[2], gx, gy, goal_z)
        # P7: h wird zusammen mit dem Knoten im Heap-Tupel mitgeführt (f, counter, h, node) —
        # spart die h-Neuberechnung (cell_to_world + hypot) bei jedem Pop; der Start-Push nutzt
        # das bereits berechnete best_h. Die Vergleichssemantik bleibt korrekt: counter ist pro
        # Push eindeutig, der Heap-Vergleich erreicht h/node also nie.
        open_heap = [(0.0, 0, best_h, start)]
        t_deadline = time.perf_counter() + mm / 1000.0

        def _best_effort(grund: str) -> List[Tuple]:
            """Teilpfad zum zielnächsten expandierten Knoten (oder [] ohne Fortschritt)."""
            sx0, sy0 = self.layers[start[0]].cell_to_world(start[1], start[2])
            # Cancel (Ziel-Wechsel) ist immer normal → DEBUG; sonst die vom Caller gewählte Stufe
            # (Schnellplan = DEBUG: Teilpfad ist hier der erwartete Normalfall, Vollsuche holt nach).
            lvl = logging.DEBUG if grund == "Abgebrochen" else partial_level
            if best_node != start:
                path = []
                node: Optional[Tuple] = best_node
                while node is not None:
                    path.append(node)
                    node = came_from[node]
                logger.log(
                    lvl, "[NAV] %s: %s (%.0f,%.0f)→(%.0f,%.0f) → Teilpfad (%d Knoten, Rest-h=%.0f)",
                    label, grund, sx0, sy0, gx, gy, len(path), best_h)
                return list(reversed(path))
            logger.log(lvl, "[NAV] %s: %s (%.0f,%.0f)→(%.0f,%.0f) → kein Fortschritt",
                       label, grund, sx0, sy0, gx, gy)
            return []

        while open_heap:
            f, _, h_cur, current = heapq.heappop(open_heap)

            if current in closed:
                continue
            closed.add(current)

            if h_cur < best_h:
                best_h, best_node = h_cur, current

            if len(closed) >= me:
                return _best_effort("Limit erreicht")
            # Wall-Clock-Budget + kooperatives Cancel nur alle 1024 Expansionen prüfen
            # (perf_counter/is_set sind nicht gratis):
            if len(closed) % 1024 == 0:
                # GIL explizit freigeben, damit der 60-Hz-Sendeloop pünktlich bleibt, während
                # dieser A*-Lauf im Hintergrund-Thread (_submit_async_plan) läuft. Interpretiert
                # yielded der Bytecode-Loop von selbst an solchen Grenzen — als mypyc-Native-Code
                # (Track 5) gibt es diese automatischen Yield-Punkte nicht mehr, sonst könnte eine
                # lange Vollsuche _send_update blockieren ([nr]-Stalls). time.sleep(0) wirkt
                # interpretiert wie kompiliert identisch und ist im Schnellplan (selten >1024
                # Expansionen) vernachlässigbar.
                time.sleep(0)
                if time.perf_counter() > t_deadline:
                    return _best_effort("Zeitbudget erreicht")
                if cancel is not None and cancel.is_set():
                    return _best_effort("Abgebrochen")

            if current in goal_set:
                path = []
                node2: Optional[Tuple] = current
                while node2 is not None:
                    path.append(node2)
                    node2 = came_from[node2]
                return list(reversed(path))

            lid, ix, iy = current
            layer = self.layers[lid]
            cur_g = g_score[current]
            wx, wy = layer.cell_to_world(ix, iy)

            # ── Horizontale Nachbarn (8-direktional, gleiche Etage) ────────
            for dix, diy in DIRS_8:
                nix, niy = ix + dix, iy + diy
                # Zelle außerhalb der Layer-Grenzen → überspringen
                if not (0 <= nix < layer.n_x and 0 <= niy < layer.n_y):
                    continue
                # Zelle ist blockiert (Gebäude oder Sicherheitsabstand) → überspringen
                if not layer.walkable[niy][nix]:
                    continue
                # Thin-Wall-Guard: Kante verwerfen, wenn das Paar eine dünne Wand kreuzt
                # (vorberechnet, O(1)-Set-Lookup). Verhindert Diagonalen, die zwischen den
                # seitlichen Streifen einer dünnen Wand hindurchschneiden.
                if self._thin_blocked:
                    nwx, nwy = layer.cell_to_world(nix, niy)
                    if (wx, wy, nwx, nwy, layer.z) in self._thin_blocked:
                        continue
                # Diagonale Bewegung kostet √2 × so viel wie geradeaus (~1.414)
                cost = CELL_SIZE * (1.414 if (dix and diy) else 1.0)
                neighbor = (lid, nix, niy)
                new_g = cur_g + cost
                if new_g < g_score.get(neighbor, _INF):
                    g_score[neighbor]   = new_g
                    came_from[neighbor] = current
                    h = _h(self.layers[lid], nix, niy, gx, gy, goal_z)
                    counter += 1
                    heapq.heappush(open_heap, (new_g + _ASTAR_WEIGHT * h, counter, h, neighbor))

            # ── Vertikale Nachbarn (on-the-fly) ───────────────────────────
            for neighbor, cost in self._vertical_neighbors(lid, ix, iy, wx, wy, layer,
                                                           blocked_jump_wps, tank_speed):
                new_g = cur_g + cost
                if new_g < g_score.get(neighbor, _INF):
                    g_score[neighbor]   = new_g
                    came_from[neighbor] = current
                    n_lid, n_ix, n_iy = neighbor
                    h = _h(self.layers[n_lid], n_ix, n_iy, gx, gy, goal_z)
                    counter += 1
                    heapq.heappush(open_heap, (new_g + _ASTAR_WEIGHT * h, counter, h, neighbor))

            # ── Gleich-hohe Nachbar-Layer (berührende Dach-Flächen, on-the-fly) ──
            for neighbor, cost in self._same_z_neighbors(lid, ix, iy, wx, wy, layer):
                new_g = cur_g + cost
                if new_g < g_score.get(neighbor, _INF):
                    g_score[neighbor]   = new_g
                    came_from[neighbor] = current
                    n_lid, n_ix, n_iy = neighbor
                    h = _h(self.layers[n_lid], n_ix, n_iy, gx, gy, goal_z)
                    counter += 1
                    heapq.heappush(open_heap, (new_g + _ASTAR_WEIGHT * h, counter, h, neighbor))

            # ── Teleporter-Portal-Kante (P3-NAV-02, vorberechnet, O(1)-Lookup) ──
            edge = self._teleport_edges.get(current)
            if edge is not None:
                neighbor, cost = edge
                new_g = cur_g + cost
                if new_g < g_score.get(neighbor, _INF):
                    g_score[neighbor]   = new_g
                    came_from[neighbor] = current
                    n_lid, n_ix, n_iy = neighbor
                    h = _h(self.layers[n_lid], n_ix, n_iy, gx, gy, goal_z)
                    counter += 1
                    heapq.heappush(open_heap, (new_g + _ASTAR_WEIGHT * h, counter, h, neighbor))

        return []

    def _vertical_neighbors(
        self, lid: int, ix: int, iy: int, wx: float, wy: float, layer: FloorLayer,
        blocked_jump_wps=frozenset(), tank_speed: float | None = None,
    ) -> List[Tuple[Tuple, float]]:
        """Sprung-rauf/Fall-runter Kanten eines Knotens (gecacht).

        Ergebnis-Cache keyed auf ``(lid, ix, iy, ts)`` — die Kanten-Topologie ist nach dem Welt-Laden
        statisch, ``ts`` (horizontale Sprung-Reisegeschwindigkeit; None → Basis ``self._tank_speed``)
        steckt im Key und ist damit selbst-invalidierend (Flaggen-Boost). ``blocked_jump_wps``
        (gecooldownte Sprung-Ziele, NAV-14) wird als Parameter gereicht statt von ``self`` gelesen →
        reentrant (P4-INF-01) und erst beim Lesen über den gespeicherten ``block_key`` als Filter
        angewandt, damit der Cache laufzeit-stabil bleibt."""
        ts = self._tank_speed if tank_speed is None else tank_speed
        key = (lid, ix, iy, ts)
        edges = self._vn_cache.get(key)
        if edges is None:
            edges = self._compute_vertical_edges(lid, ix, iy, wx, wy, layer, ts)
            self._vn_cache[key] = edges
        if not blocked_jump_wps:
            return [(nb, c) for nb, c, _bk in edges]
        return [(nb, c) for nb, c, bk in edges
                if bk is None or bk not in blocked_jump_wps]

    def _reset_vertical_cache(self) -> None:
        """Leert den Sprungkanten-Cache. In Produktion nie nötig (Geometrie/Physik nach ``__init__``
        konstant); nur für Tests, die nachträglich ``_jump_up_cands``/``_fall_cands`` mutieren (diese
        stecken nicht im Cache-Key)."""
        self._vn_cache.clear()

    def set_physics(self, v0: float, g: float) -> None:
        """Aktualisiert die Sprungphysik (globale Server-Variablen _jumpVelocity/_gravity), falls ein
        ``MsgSetVar`` erst NACH dem Graph-Bau eintrifft (Reihenfolge in BZFlag 2.4 nicht garantiert,
        vgl. ``invalidate_nav_cache``). No-Op bei unveränderten Werten → über den geteilten Cache
        idempotent. Baut Kandidaten (``_max_jump_h``-abhängig) neu und leert den Sprungkanten-Cache.
        Nur zur Join-Zeit gedacht (kein paralleles Async-Planning aktiv)."""
        if v0 == self._v0 and g == self._g:
            return
        self._v0 = v0
        self._g = g
        self._max_jump_h = v0 * v0 / (2.0 * g)
        self._build_vertical_adjacency()
        self._reset_vertical_cache()

    def _compute_vertical_edges(
        self, lid: int, ix: int, iy: int, wx: float, wy: float, layer: FloorLayer,
        ts: float,
    ) -> List[Tuple[Tuple, float, object]]:
        """Berechnet Sprung-rauf und Fall-runter Kanten für einen Knoten (ungefiltert, gecacht).

        Rückgabe je Kante ``(neighbor, cost, block_key)``: ``block_key = (round(dst_wx),
        round(dst_wy), dst.z)`` für Sprung-hoch-Kanten (NAV-14-Filter wird erst beim Lesen in
        ``_vertical_neighbors`` angewandt), ``None`` für Fall-Kanten. ``ts`` ist die horizontale
        Sprung-Reisegeschwindigkeit (Basis oder Flaggen-erhöht)."""
        result: List[Tuple[Tuple, float, object]] = []

        # ── Sprung-rauf ────────────────────────────────────────────────────
        # Nur die vorberechneten Kandidaten-Layer (konservativer JUMP_RANGE-AABB-Filter) statt aller
        # Layer; die inneren Prüfungen bleiben als exaktes pro-Knoten-Netz erhalten.
        for dst_lid in self._jump_up_cands.get(lid, ()):
            dst = self.layers[dst_lid]
            # Nur höher gelegene Ebenen sind Sprungziele
            if dst.z <= layer.z + 0.1:
                continue
            dz = dst.z - layer.z
            # Höhenunterschied übersteigt die maximale Sprunghöhe → nicht erreichbar
            if dz >= self._max_jump_h:
                continue

            # Ziel-Footprint: Abstand (aabb_dist) und nächstgelegener Punkt (Eintrittspunkt np)
            # zur Startzelle — exakte (ggf. rotierte) Footprint-Geometrie via _entry_point, damit
            # auch Diamant-Spitzen als Landefläche zugelassen werden. Der Eintrittspunkt (np_x,
            # np_y) ist sowohl Sprungrichtung als auch reale Landefläche (→ plan_path nutzt ihn
            # ebenfalls als Sprung-Landewegpunkt, deckungsgleich mit dieser Erreichbarkeitsprüfung).
            np_x, np_y, aabb_dist = _entry_point(dst, wx, wy)
            # Gebäude liegt komplett außerhalb der Sprungreichweite → überspringen
            if aabb_dist > JUMP_RANGE:
                continue
            # Startzelle direkt im horizontalen Footprint des Zielgebäudes.
            # Sprung von direkt unterhalb ist immer blockiert — der Obstacle-Körper
            # (dst_bottom..dst.z) liegt im Weg des Sprungbogens.
            if aabb_dist < 0.1:
                continue

            # Sprungrichtung = zum Eintrittspunkt des Ziel-Footprints (nicht zum Mittelpunkt:
            # bei langen, schräg über die Startfläche laufenden Balken liegt der Mittelpunkt
            # weit weg entlang des Balkens — der Eintrittspunkt ist der korrekte, kürzeste Weg).
            dir_x = np_x - wx
            dir_y = np_y - wy
            dir_len = math.hypot(dir_x, dir_y)
            if dir_len < 0.1:
                dir_x, dir_y = 1.0, 0.0
            else:
                dir_x /= dir_len; dir_y /= dir_len

            # Echte zu überbrückende Horizontalstrecke = EUKLIDISCHER Abstand der Zellmitte zum
            # Eintrittspunkt np (dir_len). aabb_dist ist nur der Chebyshev-Abstand im gedrehten
            # Frame und unterschätzt bei Diamant-Spitzen die reale Sprungweite stark — daher hier
            # dir_len verwenden (aabb_dist bleibt nur für JUMP_RANGE-Cull und den <0.1-Schutz).
            gap = dir_len

            # Absprung-Überhang: der Tank rollt von der Margin-eingerückten Startzelle bis zum
            # Quell-Footprint-Rand (+JUMP_EDGE_TOL, Pixel-on) vor, bevor er abspringt → die zu
            # überbrückende Strecke verkürzt sich. Analytischer Ray-Box-Exit (rotationskorrekt).
            # WICHTIG: gedeckelt auf den Margin-Inset der Startzelle + JUMP_EDGE_TOL, NICHT auf
            # t_exit/aabb_dist — sonst entspräche der Überhang dem Rollen quer über die ganze
            # Plattform bis zum Rand und Innenzellen bekämen unmöglich weite Sprungkanten.
            # Bodenstart (source_obstacle == None) hat keinen Überhang.
            overhang = 0.0
            src = layer.source_obstacle
            if src is not None:
                cos_r = math.cos(-src.angle); sin_r = math.sin(-src.angle)
                px = (wx - src.cx) * cos_r - (wy - src.cy) * sin_r
                py = (wx - src.cx) * sin_r + (wy - src.cy) * cos_r
                ldx = dir_x * cos_r - dir_y * sin_r
                ldy = dir_x * sin_r + dir_y * cos_r
                hw_t = src.half_w + JUMP_EDGE_TOL
                hd_t = src.half_d + JUMP_EDGE_TOL
                t_exit = _INF
                if ldx > 1e-9:
                    t_exit = min(t_exit, (hw_t - px) / ldx)
                elif ldx < -1e-9:
                    t_exit = min(t_exit, (-hw_t - px) / ldx)
                if ldy > 1e-9:
                    t_exit = min(t_exit, (hd_t - py) / ldy)
                elif ldy < -1e-9:
                    t_exit = min(t_exit, (-hd_t - py) / ldy)
                if 0.0 < t_exit < _INF:
                    src_margin = _margin_for(src, layer.z)
                    overhang = min(t_exit, src_margin + JUMP_EDGE_TOL)

            # Landepunkt = Eintrittspunkt → nächste begehbare Zelle.
            d_ix, d_iy = dst.world_to_cell(np_x, np_y)
            d_ix, d_iy = dst.clamp_cell(d_ix, d_iy)
            if not dst.walkable[d_iy][d_ix]:
                # Exakter Landepunkt blockiert → nächste freie Zelle suchen
                d_ix, d_iy = _nearest_walkable(dst, d_ix, d_iy)
                if d_ix < 0:
                    continue

            dst_wx, dst_wy = dst.cell_to_world(d_ix, d_iy)
            hdist = math.hypot(dst_wx - wx, dst_wy - wy)

            # Prüfen ob der Sprung physikalisch möglich ist (Details → DEVELOPER.md §5 "Sprung-Kanten"):
            # disc wird negativ wenn das Dach zu hoch ist → Bot kann nicht hoch genug springen
            disc = self._v0 ** 2 - 2.0 * self._g * dz
            if disc < 0:
                continue
            # Abstiegszeit: Zeit bis der Bot beim Fallen die Dachhöhe erreicht
            t_desc = (self._v0 + math.sqrt(disc)) / self._g

            # Clearance-Mindestabstand: der Bot muss beim Erreichen der Zielkante schon hoch
            # genug sein (z >= dz-0.5), sonst stößt er seitlich gegen die Wand. Aus der
            # Steigphase z(t)=v0*t-0.5*g*t² folgt ein minimaler horizontaler Absprungabstand.
            disc_cl = self._v0 ** 2 - 2.0 * self._g * (dz - 0.5)
            if disc_cl <= 0:
                wall_dist_min = 0.0
            else:
                t_min = (self._v0 - math.sqrt(disc_cl)) / self._g
                wall_dist_min = max(0.0, ts * t_min)
            # Absprung-Überhang nutzen, aber so deckeln, dass der Clearance-Mindestabstand
            # erhalten bleibt: der Bot rollt nur so weit zur Kante vor, wie er noch über den
            # Zielrand kommt (Trade-off gap-Verkürzung ↔ Steig-Runway).
            overhang = min(overhang, max(0.0, gap - wall_dist_min))

            # Effektiv zu überbrückende Strecke = Abstand zur Zielkante minus Absprung-Überhang
            # minus Front-Catch (der Tank landet, sobald seine Front die Kante erreicht —
            # Pixel-on). 10% Sicherheitspuffer — theoretisches Maximum selten erreichbar.
            eff_gap = max(0.0, gap - overhang - JUMP_EDGE_TOL)
            if eff_gap > ts * t_desc * 0.9:
                continue
            # Clearance-Prüfung (durch die Überhang-Deckelung i.d.R. erfüllt; zur Sicherheit):
            wall_dist = max(0.0, gap - overhang)
            if wall_dist > 0.01:
                t_wall = wall_dist / ts
                z_at_wall = self._v0 * t_wall - 0.5 * self._g * t_wall ** 2
                if z_at_wall < dz - 0.5:
                    continue
            # (Der frühere ±30°-Korridor-Check (NAV-15) entfällt: Erreichbarkeit ist bereits
            # durch eff_gap abgedeckt, und seine „Überschuss bei Maximalreichweite"-Semantik
            # war für Fast-Senkrecht-Sprünge auf schmale Diagonalbalken falsch — der Bot wählt
            # seine Absprunggeschwindigkeit passend zum Landepunkt und schießt nicht hinaus.)

            # NAV-19: Sprungbogen gegen dazwischenliegende Überhänge prüfen (Kopfstoß vermeiden).
            # Richtung + hspeed deckungsgleich zu _initiate_nav_jump (zum Landepunkt, needed_hspeed).
            if hdist > 0.1:
                hspeed = ts
                calc = (hdist + 2.5) / max(t_desc, 0.01)
                if 1.0 < calc <= ts:
                    hspeed = calc
                adir_x = (dst_wx - wx) / hdist
                adir_y = (dst_wy - wy) / hdist
                if not self._arc_clears_overhangs(wx, wy, layer, dst, adir_x, adir_y, hspeed):
                    continue

            # Sprung-Kosten: gleichwertig zu Fall-Kosten (hdist*1.5), damit A* Sprünge
            # bei kurzem Weg nach oben gegenüber langen Bodenumwegen bevorzugt.
            cost = hdist * 1.5 + dz * 1.0
            # Auf Teleporter-Karten Sprung-hoch verteuern: der Bot soll lieber das (sichere) Tor
            # nehmen als sich im verwundbaren Sprung-Arc abschießen zu lassen (nur Aufschlag, kein
            # Verbot — nur-per-Sprung erreichbare Orte bleiben erreichbar).
            if self._teleporters:
                cost += NAV_JUMP_UP_PENALTY
            # NAV-14-Block wird NICHT hier gefiltert (Cache-Stabilität) — der block_key wird
            # gespeichert und erst in _vertical_neighbors gegen blocked_jump_wps geprüft.
            block_key = (round(dst_wx), round(dst_wy), dst.z)
            result.append(((dst_lid, d_ix, d_iy), cost, block_key))

        # ── Fall-runter ────────────────────────────────────────────────────
        # Fallen ist nur von Dächern möglich (layer.source_obstacle != None = Dach-Ebene)
        if layer.source_obstacle is not None:
            # Nur von Rand-Zellen aus fallen: ein Tank in der Dach-Mitte kann nicht einfach
            # herunterfallen, weil kein Weg zum Rand führt ohne ihn zu passieren
            is_boundary = (ix == 0 or ix == layer.n_x - 1 or
                           iy == 0 or iy == layer.n_y - 1)
            if is_boundary:
                for dst_lid in self._fall_cands.get(lid, ()):
                    dst = self.layers[dst_lid]
                    # Nur tiefere Ebenen sind Fallziele
                    if dst.z >= layer.z:
                        continue
                    # Ziel: Zelle direkt unter uns (senkrechter Fall)
                    d_ix, d_iy = dst.world_to_cell(wx, wy)
                    d_ix, d_iy = dst.clamp_cell(d_ix, d_iy)
                    if not dst.walkable[d_iy][d_ix]:
                        d_ix, d_iy = _nearest_walkable(dst, d_ix, d_iy)
                        if d_ix < 0:
                            continue
                    dst_wx, dst_wy = dst.cell_to_world(d_ix, d_iy)
                    hdist = math.hypot(dst_wx - wx, dst_wy - wy)
                    # Fall-Kosten: Höhe und Drift summieren; +5.0 Grundstrafe damit Springen
                    # bevorzugt wird wenn beide Optionen möglich sind
                    cost  = (layer.z - dst.z) * 0.5 + hdist * 1.5 + 5.0
                    result.append(((dst_lid, d_ix, d_iy), cost, None))

        return result

    def _build_same_z_adjacency(self) -> None:
        """Vorberechnung: pro Dach-Layer die Liste gleich hoher Layer, deren (achsenparallele)
        Footprint-AABB ihn berührt/überlappt (innerhalb CELL_SIZE*1.5). _same_z_neighbors prüft dann
        nur diese wenigen Kandidaten statt aller Layer (z=15 hat z.B. 40 gleich hohe Layer). Der
        AABB-Test ist konservativ (umschließt jedes real berührende Paar) — die exakte Zell-/
        Distanzprüfung erfolgt weiterhin pro Knoten in _same_z_neighbors. Boden-Layer (z≈0) haben
        keine gleich hohen Partner und bleiben außen vor."""
        tol = CELL_SIZE * 1.5
        n = len(self.layers)
        for a in range(n):
            la = self.layers[a]
            if la.z <= 0.1:
                continue
            cands: List[int] = []
            for b in range(n):
                if b == a:
                    continue
                lb = self.layers[b]
                if abs(lb.z - la.z) > 0.1:
                    continue
                gap_x = abs(la.cx - lb.cx) - (la.half_w + lb.half_w)
                gap_y = abs(la.cy - lb.cy) - (la.half_d + lb.half_d)
                if gap_x <= tol and gap_y <= tol:
                    cands.append(b)
            if cands:
                self._same_z_touch[a] = cands

    def _build_vertical_adjacency(self) -> None:
        """Vorberechnung der vertikalen Kandidaten-Layer je Quell-Layer, damit _vertical_neighbors
        nur diese statt aller Layer (HIX: 57) prüft. Die teure exakte Prüfung (Eintrittspunkt,
        Sprung-Feasibility, NAV-19, Kosten, LIVE blocked_jump_wps) bleibt unverändert pro Knoten —
        das Ergebnis ist byte-identisch zum vollen Scan (Regressionstest in test_nav_graph).

        - **Sprung-rauf**: höhere Layer (dz < max_jump_h), deren Footprint-AABB ≤ JUMP_RANGE entfernt
          ist. Konservatives Superset: `_entry_point.aabb_dist` misst den Abstand zum (in der Grid-
          AABB enthaltenen) Footprint, ist also ≥ dem Layer-AABB-Gap → akzeptiert _vertical_neighbors
          einen Knoten (aabb_dist ≤ JUMP_RANGE), ist gap ≤ JUMP_RANGE und der Layer hier enthalten.
        - **Fall-runter**: ALLE tieferen Layer (nur von Dach-Layern). Der Fall-Zweig hat KEINEN
          Horizontal-Cull — `_nearest_walkable` (ohne max_r) liefert für jeden tieferen Layer eine
          Zelle. Eine AABB-Einschränkung würde Kanten verlieren → bewusst kein Gap-Cull. Trifft nur
          Rand-Zellen von Dach-Layern, daher unkritisch für die Performance (Engpass ist Sprung-rauf)."""
        n = len(self.layers)
        for a in range(n):
            la = self.layers[a]
            is_roof = la.source_obstacle is not None
            ups: List[int] = []
            downs: List[int] = []
            for b in range(n):
                if b == a:
                    continue
                lb = self.layers[b]
                # Sprung-rauf: höhere Layer innerhalb max_jump_h, Footprint-AABB ≤ JUMP_RANGE.
                if lb.z > la.z + 0.1:
                    if (lb.z - la.z) < self._max_jump_h:
                        gap_x = abs(la.cx - lb.cx) - (la.half_w + lb.half_w)
                        gap_y = abs(la.cy - lb.cy) - (la.half_d + lb.half_d)
                        if gap_x <= JUMP_RANGE and gap_y <= JUMP_RANGE:
                            ups.append(b)
                # Fall-runter (nur von Dach-Layern): JEDER tiefere Layer (kein Horizontal-Cull).
                elif lb.z < la.z - 0.1 and is_roof:
                    downs.append(b)
            if ups:
                self._jump_up_cands[a] = ups
            if downs:
                self._fall_cands[a] = downs

    def _same_z_neighbors(
        self, lid: int, ix: int, iy: int, wx: float, wy: float, layer: FloorLayer
    ) -> List[Tuple[Tuple, float]]:
        """Kanten zu BERÜHRENDEN Dach-Layern GLEICHER Höhe. _build_roof_layers legt pro Obstacle
        eine eigene Layer an; eine physisch durchgehende Dach-Fläche (z.B. der HIX-Perimeter-Ring +
        die Diagonal-Mauern, auf denen die Tor-Ausgänge sitzen) zerfällt so in getrennte Inseln,
        zwischen denen weder horizontale (gleiche Layer) noch _vertical_neighbors (nur dz≠0) Kanten
        bestehen. Verbindet nur ANGRENZENDE/überlappende Layer (≤ ~1.5 Zellen) → echte Lücken (z.B.
        isolierte Eck-Plattformen) bleiben getrennt und werden dort weiterhin per Sprung überbrückt.
        Kandidaten kommen aus dem vorberechneten _same_z_touch → auf dem Boden sofort [] (kostenlos)."""
        out: List[Tuple[Tuple, float]] = []
        for dst_lid in self._same_z_touch.get(lid, ()):
            dst = self.layers[dst_lid]
            d_ix, d_iy = dst.clamp_cell(*dst.world_to_cell(wx, wy))
            if not dst.walkable[d_iy][d_ix]:
                d_ix, d_iy = _nearest_walkable(dst, d_ix, d_iy, max_r=1)
                if d_ix < 0:
                    continue
            dst_wx, dst_wy = dst.cell_to_world(d_ix, d_iy)
            dist = math.hypot(dst_wx - wx, dst_wy - wy)
            if dist <= CELL_SIZE * 1.5:
                out.append(((dst_lid, d_ix, d_iy), max(CELL_SIZE, dist)))
        return out

    def _arc_clears_overhangs(self, wx, wy, src_layer, dst, dir_x, dir_y, hspeed) -> bool:
        """NAV-19: False, wenn der Sprungbogen-Kopf in der Steigphase die Unterkante eines
        DAZWISCHEN liegenden Hindernisses trifft (sichtbares Kopfstoßen → Sprung misslingt).

        Quell- und Ziel-Obstacle sind ausgenommen — deren Körper bilden Absprung bzw. Landung
        (sonst würde die eigene Unterseite des Ziel-Aufbaus den Sprung fälschlich verwerfen).
        Pro distinkter Unterkante (self._undersides) wird die Steig-Zeit, in der der Kopf
        (z+TANK_HEIGHT) sie kreuzt, GESCHLOSSEN gelöst — kein Sampling, kein durchrutschender
        dünner Überhang. Horizontal-Prädikat deckungsgleich zur reaktiven Decken-Kollision in
        bot/ai/physics._apply_obstacle_bounds (rotierte Box + halbe Tankbreite)."""
        v0, g, z0 = self._v0, self._g, src_layer.z
        apex = v0 * v0 / (2.0 * g)                                   # max. Steighöhe ≈ 18.4u
        skip = {id(getattr(src_layer, "source_obstacle", None)),
                id(getattr(dst, "source_obstacle", None))}
        for bz, group in self._undersides.items():
            # Nur Unterkanten, die der Kopf in der Steigphase überhaupt erreicht.
            if bz <= z0 + TANK_HEIGHT + 0.1 or bz > z0 + apex + TANK_HEIGHT:
                continue
            disc = v0 * v0 - 2.0 * g * (bz - TANK_HEIGHT - z0)
            if disc < 0:
                continue
            t = (v0 - math.sqrt(disc)) / g                          # aufsteigender Ast
            x = wx + dir_x * hspeed * t
            y = wy + dir_y * hspeed * t
            for obs in group:
                if id(obs) in skip:
                    continue
                cos_a = obs.cos_a; sin_a = obs.sin_a
                lx = (x - obs.cx) * cos_a + (y - obs.cy) * sin_a
                ly = -(x - obs.cx) * sin_a + (y - obs.cy) * cos_a
                if abs(lx) < obs.half_w + TANK_HALF_WIDTH and abs(ly) < obs.half_d + TANK_HALF_WIDTH:
                    return False
        return True

    # ── Teleporter-Portal-Kanten (P3-NAV-02) ──────────────────────────────

    def _world_to_node_at_z(self, wx: float, wy: float,
                            z: float) -> Optional[Tuple[int, int, int]]:
        """Snappt einen Weltpunkt auf den nächsten begehbaren Knoten der Etage auf Höhe z.
        Wählt die Etage via find_layer_at (statt fix Boden) — so landet ein Teleporter-Austritt auf
        einer z=30-Plattform auch wirklich auf der Roof-Layer (P3-NAV-02-Folgefix)."""
        lid = self.find_layer_at(wx, wy, z)
        layer = self.layers[lid]
        if not layer.contains_xy(wx, wy):
            return None
        ix, iy = layer.world_to_cell(wx, wy)
        ix, iy = layer.clamp_cell(ix, iy)
        if not layer.walkable[iy][ix]:
            ix, iy = _nearest_walkable(layer, ix, iy, max_r=8)
            if ix < 0:
                return None
        return (lid, ix, iy)

    def _build_teleport_edges(self, world_map: WorldMap) -> None:
        """Vorberechnung der A*-Portal-Kanten: pro verlinktem Teleporter-Face ein
        entry_node → (exit_node, cost). Entry liegt knapp außerhalb des Feld-Strips auf der
        Eintrittsseite, der Exit-Knoten via teleport_through (= reale Austrittsposition) etwas
        in Austrittsrichtung herausgeschoben. Deckungsgleich zur reaktiven Querung in
        bot/ai/navigation._check_teleport_crossing."""
        self._tele_cross_centers = {}
        teles = world_map.teleporters
        if not teles:
            return
        link_map = build_link_map(world_map.links)
        # Anfahrt-/Austritts-Offset: hinter den (mit TANK_MARGIN gesperrten) Feld-Strip,
        # damit der Knoten garantiert auf einer begehbaren Zelle liegt.
        for ti, tele in enumerate(teles):
            r = 0.5 * tele.border
            off = r + TANK_MARGIN + CELL_SIZE
            c = math.cos(tele.angle); s = math.sin(tele.angle)
            for face in (0, 1):
                target = link_map.get(2 * ti + face)
                if target is None:
                    continue
                exit_ti, exit_face = target // 2, target & 1
                if exit_ti >= len(teles):
                    continue
                exit_tele = teles[exit_ti]
                # Entry-Anfahrpunkt: face 0 ⇒ Eintritt von der lokalen +x-Seite (vgl.
                # ray_teleporter_crossing: face=0 wenn x_local>0), face 1 ⇒ -x-Seite.
                sign = 1.0 if face == 0 else -1.0
                ex = tele.cx + sign * off * c
                ey = tele.cy + sign * off * s
                # Richtung ins Feld (lokal -sign in x), für teleport_through.
                dir_x, dir_y = -sign * c, -sign * s
                npx, npy, npz, ndx, ndy, _ndz = teleport_through(
                    ex, ey, tele.bottom_z, dir_x, dir_y, 0.0,
                    tele, face, exit_tele, exit_face)
                dlen = math.hypot(ndx, ndy) or 1.0
                qx = npx + (ndx / dlen) * off
                qy = npy + (ndy / dlen) * off
                # Entry/Exit auf die jeweils korrekte Etage snappen: Eintritt auf Tor-Boden-Höhe,
                # Austritt auf das von teleport_through gelieferte Exit-z (z.B. z=30-Plattform) —
                # sonst modellierte A* den Cross-Floor-Teleport als Boden-Landung und sprang lieber.
                entry_node = self._world_to_node_at_z(ex, ey, tele.bottom_z)
                exit_node  = self._world_to_node_at_z(qx, qy, npz)
                if entry_node is None or exit_node is None or entry_node == exit_node:
                    continue
                self._teleport_edges[entry_node] = (exit_node, float(CELL_SIZE))
                # Quell-Tor-Mitte je Austritts-WP cachen (NAV_TELE fährt dorthin durch die Tor-Ebene).
                ewx, ewy = self.layers[exit_node[0]].cell_to_world(exit_node[1], exit_node[2])
                self._tele_cross_centers[(round(ewx, 1), round(ewy, 1))] = (tele.cx, tele.cy)
        # Exit-Knoten-Welt-Positionen cachen (Pipeline-Guards: kein Sprung-/Run-up-Misdeut der
        # cross-floor Exit-Wegpunkte, s. plan_path/_insert_jump_runups/_advance_path).
        self._tele_exit_wps = set()
        for exit_node, _cost in self._teleport_edges.values():
            ewx, ewy = self.layers[exit_node[0]].cell_to_world(exit_node[1], exit_node[2])
            self._tele_exit_wps.add((round(ewx, 1), round(ewy, 1)))


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _obstacle_blocks_layer(obs: BoxObstacle, layer_z: float) -> bool:
    """True, wenn obs den Tank-Körper sperrt, der auf Höhe layer_z steht.

    Geprüft wird die vertikale Überlappung von [obs.bottom_z, obs.bottom_z+height]
    mit der Tank-Box [layer_z, layer_z+TANK_HEIGHT]. Das +0.1 schließt das Dach-
    erzeugende Obstacle aus (dessen Oberkante == layer_z) und Obstacles, deren
    Oberkante exakt auf Etagenhöhe liegt (Tank fährt drüber).

    Entscheidend für Wände, die UNTER einer Dachfläche beginnen und durch die
    Tank-Höhe nach oben stoßen (z.B. HIX-Diagonalwände bottom_z=14, height=16):
    diese sperren z=15, aber nicht z=30 (dort == Oberkante → drüberfahren)."""
    return obs.bottom_z < layer_z + TANK_HEIGHT and obs.bottom_z + obs.height > layer_z + 0.1


def _margin_for(obs: BoxObstacle, layer_z: float) -> float:
    """Margin für obs auf der Etage layer_z.

    Reduzierter THIN_WALL_MARGIN nur für dünne Wände auf Dach-/Plattform-Layern
    (layer_z > 0.5), damit schmale Laufstege seitlich an der Wand befahrbar bleiben.
    Auf dem Boden-Layer (z=0) bekommen auch dünne Obstacles den vollen TANK_MARGIN:
    dort ist meist Platz, und der reduzierte Rand ließ den Bot an kleinen Kreuz-
    Obstacles hängen bleiben. Dicke Obstacles behalten überall den vollen TANK_MARGIN."""
    thin = min(obs.half_w, obs.half_d) * 2 < CELL_SIZE
    return THIN_WALL_MARGIN if (thin and layer_z > 0.5) else TANK_MARGIN


def _entry_point(dst: FloorLayer, wx: float, wy: float) -> Tuple[float, float, float]:
    """Nächstgelegener Punkt auf dem (ggf. rotierten) Ziel-Footprint zur Quelle (wx,wy).

    Rückgabe (np_x, np_y, aabb_dist): Der Eintrittspunkt np ist sowohl Sprungrichtung als auch
    die reale Landefläche (Plattformkante bzw. Diamant-Spitze einer 45°-Plattform). Rotierte
    Obstacles werden im lokalen Frame exakt geklemmt (auf obs.half_w/half_d), achsenparallele per
    AABB (auf dst.half_w/half_d). aabb_dist ist der Chebyshev-Abstand (nur für JUMP_RANGE-Cull +
    <0.1-Schutz). Wird in _vertical_neighbors (Sprungkanten-Planung) UND in plan_path (genauer
    Sprung-Landewegpunkt) genutzt — eine Quelle der Wahrheit."""
    obs = dst.source_obstacle
    if obs is not None and abs(obs.angle) > 0.01:
        ca = obs.cos_a; sa = obs.sin_a
        lx = (wx - dst.cx) * ca + (wy - dst.cy) * sa
        ly = -(wx - dst.cx) * sa + (wy - dst.cy) * ca
        aabb_dist = max(0.0, abs(lx) - obs.half_w, abs(ly) - obs.half_d)
        clx = max(-obs.half_w, min(obs.half_w, lx))
        cly = max(-obs.half_d, min(obs.half_d, ly))
        return dst.cx + clx * ca - cly * sa, dst.cy + clx * sa + cly * ca, aabb_dist
    aabb_dist = max(0.0, abs(wx - dst.cx) - dst.half_w, abs(wy - dst.cy) - dst.half_d)
    np_x = max(dst.cx - dst.half_w, min(dst.cx + dst.half_w, wx))
    np_y = max(dst.cy - dst.half_d, min(dst.cy + dst.half_d, wy))
    return np_x, np_y, aabb_dist


def _clip_to_footprint(layer: FloorLayer, obs: BoxObstacle,
                       margin: float) -> None:
    """Markiert Zellen außerhalb des tatsächlichen rotierten Obstacle-Footprints als
    non-walkable. Notwendig für diagonale Obstacles, deren AABB deutlich größer als der
    physische Footprint ist (z.B. hw=4, hd=550, angle=45° → AABB ≈ 391×391u)."""
    cos_a = obs.cos_a
    sin_a = obs.sin_a
    hw_inner = obs.half_w - margin
    hd_inner = obs.half_d - margin
    for iy in range(layer.n_y):
        for ix in range(layer.n_x):
            if not layer.walkable[iy][ix]:
                continue
            wx, wy = layer.cell_to_world(ix, iy)
            dx = wx - obs.cx
            dy = wy - obs.cy
            lx = dx * cos_a + dy * sin_a
            ly = -dx * sin_a + dy * cos_a
            if abs(lx) > hw_inner or abs(ly) > hd_inner:
                layer.walkable[iy][ix] = 0


def _mark_blocked(layer: FloorLayer, obs: BoxObstacle,
                  margin: float) -> None:
    """Markiert Zellen in layer als blockiert durch obs (exakter Rotated-Box-Test + margin)."""
    # AABB des rotierten Obstacles: äußere Hülle für die Schleifengrenzen
    cos_a = obs.cos_a
    sin_a = obs.sin_a
    ext_x = obs.half_w * abs(cos_a) + obs.half_d * abs(sin_a) + margin
    ext_y = obs.half_w * abs(sin_a) + obs.half_d * abs(cos_a) + margin
    ix0, iy0 = layer.world_to_cell(obs.cx - ext_x, obs.cy - ext_y)
    ix1, iy1 = layer.world_to_cell(obs.cx + ext_x, obs.cy + ext_y)
    ix0, iy0 = max(0, ix0), max(0, iy0)
    ix1, iy1 = min(layer.n_x - 1, ix1), min(layer.n_y - 1, iy1)
    for row in range(iy0, iy1 + 1):
        for col in range(ix0, ix1 + 1):
            # Zell-Mitte in lokale Obstacle-Koordinaten drehen und prüfen
            wx, wy = layer.cell_to_world(col, row)
            dx = wx - obs.cx
            dy = wy - obs.cy
            lx = dx * cos_a + dy * sin_a
            ly = -dx * sin_a + dy * cos_a
            # Nur blockieren wenn die Zell-Mitte wirklich im Obstacle + margin liegt
            if abs(lx) <= obs.half_w + margin and abs(ly) <= obs.half_d + margin:
                layer.walkable[row][col] = 0


def _point_in_rotated_box(obs: BoxObstacle, x: float, y: float, margin: float = 0.0) -> bool:
    """Punkt-in-rotierter-Box-Test (für get_floor_z). margin weitet die Box (Pixel-on-Auflage)."""
    dx = x - obs.cx
    dy = y - obs.cy
    cos_a = obs.cos_a
    sin_a = obs.sin_a
    lx =  dx * cos_a + dy * sin_a
    ly = -dx * sin_a + dy * cos_a
    return (abs(lx) <= obs.half_w + 0.5 + margin
            and abs(ly) <= obs.half_d + 0.5 + margin)


def _nearest_walkable(layer: FloorLayer, ix: int, iy: int,
                       max_r: int = 6) -> Tuple[int, int]:
    """BFS: nächste begehbare Zelle in layer ausgehend von (ix, iy)."""
    if (0 <= ix < layer.n_x and 0 <= iy < layer.n_y and
            layer.walkable[iy][ix]):
        return ix, iy
    q = deque([(ix, iy, 0)])
    seen: set = set()
    while q:
        cx, cy, r = q.popleft()
        if (cx, cy) in seen or r > max_r:
            continue
        seen.add((cx, cy))
        if (0 <= cx < layer.n_x and 0 <= cy < layer.n_y and
                layer.walkable[cy][cx]):
            return cx, cy
        for dcx, dcy in DIRS_8:
            nc, nd = cx + dcx, cy + dcy
            if (nc, nd) not in seen:
                q.append((nc, nd, r + 1))
    return -1, -1


def _h(layer: FloorLayer, ix: int, iy: int, gx: float, gy: float,
       goal_z: float | None = None) -> float:
    """A*-Heuristik: Euklidische Distanz + Z-Aufstieg zum Ziel (admissibel)."""
    wx, wy = layer.cell_to_world(ix, iy)
    h = math.hypot(gx - wx, gy - wy)
    if goal_z is not None:
        h += max(0.0, goal_z - layer.z)
    return h


def _segment_crosses_thin_obs(
    x1: float, y1: float, x2: float, y2: float, obs: BoxObstacle,
) -> bool:
    """Slab-Test: kreuzt Segment (x1,y1)-(x2,y2) den 2D-Footprint von obs?"""
    ddx, ddy = x2 - x1, y2 - y1
    cos_a = obs.cos_a
    sin_a = obs.sin_a
    ox = (x1 - obs.cx) * cos_a + (y1 - obs.cy) * sin_a
    oy = -(x1 - obs.cx) * sin_a + (y1 - obs.cy) * cos_a
    dx = ddx * cos_a + ddy * sin_a
    dy = -ddx * sin_a + ddy * cos_a
    t_min, t_max = 0.0, 1.0
    for o, d, h in ((ox, dx, obs.half_w), (oy, dy, obs.half_d)):
        if abs(d) < 1e-9:
            if abs(o) > h:
                return False
        else:
            ta, tb = (-h - o) / d, (h - o) / d
            if ta > tb:
                ta, tb = tb, ta
            t_min = max(t_min, ta)
            t_max = min(t_max, tb)
            if t_min > t_max:
                return False
    return t_min <= t_max


def _smooth_path(
    waypoints: List[Tuple[float, float, float]],
    thin_blocked: Optional[set] = None,
    keep: Optional[set] = None,
) -> List[Tuple[float, float, float]]:
    """Entfernt redundante Zwischenpunkte auf gleicher Etage.

    thin_blocked: vorberechnete verbotene Wegpunkt-Paare (aus _precompute_thin_wall_blocked).
    Wird ein Wegpunkt B entfernt und die entstehende Direktlinie result[-1]→nxt kreuzt eine
    dünne Wand, bleibt B erhalten.
    keep: Wegpunkt-Indizes, die nie entfernt werden dürfen (P3-NAV-02: Teleporter-Portal-Hop —
    Entry/Exit sind z-konstant und würden sonst als kollinear weggekürzt).
    """
    if len(waypoints) <= 2:
        return waypoints
    result = [waypoints[0]]
    for i in range(1, len(waypoints) - 1):
        prev, curr, nxt = waypoints[i - 1], waypoints[i], waypoints[i + 1]
        # Portal-Hop-Endpunkt (Entry/Exit): immer behalten
        if keep and i in keep:
            result.append(curr)
            continue
        # Etagen-Wechsel: Sprung- oder Fall-Punkt muss immer erhalten bleiben
        if curr[2] != prev[2] or curr[2] != nxt[2]:
            result.append(curr)
            continue
        # Richtungsänderung zwischen den drei Punkten berechnen (Winkelunterschied in Rad)
        d01 = math.atan2(curr[1] - prev[1], curr[0] - prev[0])
        d12 = math.atan2(nxt[1]  - curr[1], nxt[0]  - curr[0])
        diff = abs(d12 - d01)
        # Winkel-Überlauf korrigieren (z.B. 359° → 1° sollte nur 2° Unterschied sein)
        if diff > math.pi:
            diff = 2 * math.pi - diff
        # Kurve spitzer als 15° → Knickpunkt behalten
        if diff > math.radians(15):
            result.append(curr)
            continue
        # Thin-Wall-Guard: würde die Abkürzung result[-1] → nxt eine dünne Wand schneiden?
        if thin_blocked:
            px, py, pz = result[-1]
            nx, ny, _ = nxt
            if (px, py, nx, ny, pz) in thin_blocked:
                result.append(curr)
                continue
        # Sicher entfernbar
    result.append(waypoints[-1])
    return result


def _runup_crosses_thin_wall(
    nav: "NavGraph", z: float,
    px: float, py: float, rx: float, ry: float, jx: float, jy: float,
) -> bool:
    """True wenn die Anfahrt (prev→runup) oder der Absprung (runup→Sprungpunkt) eine dünne Wand
    der Sprungebene z kreuzt. Verhindert, dass ein Run-up quer zu einer dünnen Trennwand (z.B.
    der diagonalen z=15-Wand) platziert wird, an der der Bot sonst hängen bliebe."""
    for obs in nav._obs:
        if min(obs.half_w, obs.half_d) * 2 >= CELL_SIZE:
            continue
        if not _obstacle_blocks_layer(obs, z):
            continue
        if (_segment_crosses_thin_obs(px, py, rx, ry, obs)
                or _segment_crosses_thin_obs(rx, ry, jx, jy, obs)):
            return True
    return False


def _insert_jump_runups(
    path: List[Tuple[float, float, float]], nav: "NavGraph"
) -> List[Tuple[float, float, float]]:
    """Fügt vor jeder Absprungzelle (Höhenwechsel) einen Run-up-WP ein.

    Der Run-up-WP liegt CELL_SIZE hinter der Absprungzelle in Sprungrichtung und wird VOR ihr in
    den Pfad eingefügt. Dadurch erreicht der Bot die Absprungzelle bereits in Sprungrichtung
    ausgerichtet und springt von dort ab (nicht vom Run-up dahinter) — er muss am Absprung weder
    drehen noch zurücksetzen.
    """
    if len(path) < 2:
        return path
    result = [path[0]]
    for i in range(1, len(path)):
        wp   = path[i]
        prev = result[-1]                       # Absprungzelle (A*-Sprungkanten-Ursprung)
        # Teleport-Exit (z.B. z=30 am Ziel-Tor) wird durchquert, nicht angesprungen → kein Run-up.
        is_tele_exit = (round(wp[0], 1), round(wp[1], 1)) in nav._tele_exit_wps
        if abs(wp[2] - prev[2]) > 1.5 and len(result) >= 2 and not is_tele_exit:
            jdx = wp[0] - prev[0]
            jdy = wp[1] - prev[1]
            jlen = math.hypot(jdx, jdy)
            if jlen > 0.1:
                jdx /= jlen
                jdy /= jlen
                rx = prev[0] - jdx * CELL_SIZE
                ry = prev[1] - jdy * CELL_SIZE
                pred = result[-2]               # WP vor der Absprungzelle (Anfahrt)
                fz = nav.get_floor_z(rx, ry, prev[2] + 1.0)
                if abs(fz - prev[2]) < 1.0 and not _runup_crosses_thin_wall(
                        nav, prev[2], pred[0], pred[1], rx, ry, prev[0], prev[1]):
                    # VOR der Absprungzelle einfügen (Bot fährt pred → runup → Absprung → Sprung)
                    result.insert(len(result) - 1, (rx, ry, prev[2]))
        result.append(wp)
    return result
