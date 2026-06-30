"""
Performance-/Timing-Checks (KEIN Assert) für die rechenintensiven Funktionen.

Lauf:
    pytest -m perf -s -v

Jede Funktion gibt eine `[PERF] <name>: …` Zeile aus. Im Normallauf (`pytest tests/`)
werden diese Tests übersprungen (siehe conftest.pytest_collection_modifyitems) — sie sollen
nur auf Anforderung laufen, nicht die schnelle Unit-Suite ausbremsen.

Abgedeckt: nav_graph (Aufbau, A*, NAV-19-Bogencheck), Schuss-Simulation (Ricochet+Teleporter),
Schuss-Ray-Kernels, Hitbox-Detection (Kernel + end-to-end) und die Indirekt-Ziel-Suche.
"""
import math
import time

import pytest

from bzflag.world_map import BoxObstacle, TeleporterObstacle, WorldMap
from bzflag.nav_graph import NavGraph, invalidate_nav_cache
from bzflag.shot_physics import (
    simulate_shot_path, ray_box_hit, ray_pyramid_hit, ray_teleporter_crossing,
    build_link_map, _segment_hits_obb_3d,
)
from conftest import make_player, make_shot

pytestmark = pytest.mark.perf   # alle Tests dieses Moduls sind perf-Tests


def _ms_per_call(fn, iters):
    """Mittlere Laufzeit von fn() in Millisekunden über iters Wiederholungen."""
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    return (time.perf_counter() - t0) / iters * 1000.0


def _perf_world(world_half=200.0):
    """Repräsentatives Hindernisfeld: Boxen-Raster in mehreren Höhen (→ mehrere Layer +
    dünne Wände) plus zwei verlinkte Teleporter. Eine Quelle für alle Welt-basierten Benches."""
    boxes = []
    step = 50.0
    n = int(world_half / step)
    h_cycle = (6.15, 14.0, 0.0, 29.0)   # distinkte Unterkanten/Dachhöhen → mehrere Floor-Layer
    k = 0
    for ix in range(-n, n + 1):
        for iy in range(-n, n + 1):
            if ix == 0 and iy == 0:
                continue                 # Startfeld frei lassen
            height = h_cycle[k % len(h_cycle)]
            k += 1
            if height > 0.0:
                boxes.append(BoxObstacle(cx=ix * step, cy=iy * step, bottom_z=0.0, angle=0.0,
                                         half_w=10.0, half_d=10.0, height=height))
            # zusätzlich Überhänge mit erhöhter Unterkante → mehrere _undersides-Gruppen
            # (realistisch für den NAV-19-Bogencheck: Querbalken auf z=14, z=29).
            if k % 3 == 0:
                boxes.append(BoxObstacle(cx=ix * step, cy=iy * step, bottom_z=14.0, angle=0.0,
                                         half_w=12.0, half_d=4.0, height=2.0))
            elif k % 5 == 0:
                boxes.append(BoxObstacle(cx=ix * step, cy=iy * step, bottom_z=29.0, angle=0.0,
                                         half_w=4.0, half_d=12.0, height=2.0))
    teles = [
        TeleporterObstacle(name="t0", cx=-80.0, cy=0.0, bottom_z=0.0, angle=0.0,
                           half_w=0.5, half_d=4.0, height=10.0, border=0.5),
        TeleporterObstacle(name="t1", cx=80.0, cy=0.0, bottom_z=0.0, angle=0.0,
                           half_w=0.5, half_d=4.0, height=10.0, border=0.5),
    ]
    links = [(0, 3), (2, 1)]            # tele0↔tele1 über je eine Seite
    return WorldMap(boxes=boxes, teleporters=teles, links=links,
                    world_half=world_half, world_hash="perf")


# ── nav_graph (Primärfokus) ───────────────────────────────────────────────────

def test_perf_navgraph_build():
    wm = _perf_world()

    def build():
        invalidate_nav_cache(wm.world_hash)
        NavGraph(wm, max_jump_h=18.4)

    ms = _ms_per_call(build, iters=5)
    print(f"\n[PERF] NavGraph build (inkl. thin-wall precompute): {ms:.2f}ms  ({len(wm.boxes)} Boxen)")


def test_perf_navgraph_plan_path():
    wm = _perf_world()
    invalidate_nav_cache(wm.world_hash)
    ng = NavGraph(wm, max_jump_h=18.4)
    routes = [
        (-180.0, -180.0, 0.0,  180.0,  180.0, 0.0),
        (-180.0,  180.0, 0.0,  180.0, -180.0, 0.0),
        (   0.0, -180.0, 0.0,    0.0,  180.0, 0.0),
    ]

    def run():
        for r in routes:
            ng.plan_path(*r)

    ms = _ms_per_call(run, iters=20) / len(routes)
    print(f"\n[PERF] plan_path (A*, inkl. _vertical_neighbors/_arc_clears_overhangs): {ms:.3f}ms/Aufruf")


def test_perf_navgraph_plan_path_hix():
    """Worst-Case auf der ECHTEN HIX-Karte (57 Layer): teure vertikale/hohe Routen, die das
    Expansionslimit erreichen — der Fall, der live den 1-2s-Freeze auslöste. Misst den Produktions-
    pfad (Limit 5000 + ASTAR_MAX_MS-Budget + vorberechnete vertikale Kandidaten); der 5000-Knoten-Cap
    bindet hier bei ~140ms. Vor der Optimierung lag der Worst-Case bei ~480-645ms (Limit 15000,
    57-Layer-Scan/Knoten). Skippt ohne HIX-Fixture."""
    from conftest import load_map_fixture
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("HIX-Fixture fehlt: 'python bzbot.py --host <server> --dump-raw tests/fixtures/hix'")
    invalidate_nav_cache(wm.world_hash)
    ng = NavGraph(wm, max_jump_h=18.4)
    routes = [
        (0.0, 0.0, 0.0, 0.0, 0.0, 30.0),            # Boden→zentraler z30-Turm (Limit-Treffer)
        (-340.0, -45.0, 15.0, 340.0, 45.0, 30.0),   # z15→z30 quer (dichte Vertikal-Region)
        (0.0, 0.0, 0.0, 340.0, 0.0, 30.0),          # Boden→isolierte z30-Eck-Plattform (340u)
    ]

    def run():
        for r in routes:
            ng.plan_path(r[0], r[1], r[2], r[3], r[4], goal_z=r[5])

    ms = _ms_per_call(run, iters=10) / len(routes)
    print(f"\n[PERF] plan_path HIX worst-case (57 Layer, Limit 5000 + Budget): {ms:.2f}ms/Aufruf")


def test_perf_navgraph_arc_overhangs():
    wm = _perf_world()
    invalidate_nav_cache(wm.world_hash)
    ng = NavGraph(wm, max_jump_h=18.4)
    src = ng.layers[0]
    dst = ng.layers[-1]
    N = 2000

    def run():
        for _ in range(N):
            ng._arc_clears_overhangs(-150.0, -150.0, src, dst, 1.0, 0.0, 25.0)

    us = _ms_per_call(run, iters=5) / N * 1000.0
    print(f"\n[PERF] _arc_clears_overhangs (NAV-19): {us:.3f}us/Aufruf  "
          f"({len(ng._undersides)} Unterkanten-Gruppen)")


# ── Schuss: Ricochet + Teleporter ─────────────────────────────────────────────

def test_perf_simulate_shot_path():
    wm = _perf_world()
    lmap = build_link_map(wm.links)
    angles = [math.radians(a) for a in range(0, 360, 5)]

    def run():
        for az in angles:
            simulate_shot_path(
                pos=(-150.0, -150.0, 1.0),
                vel=(math.cos(az) * 100.0, math.sin(az) * 100.0, 0.0),
                fire_time=0.0, lifetime=3.5, flag_abbr=b"R\x00",
                obstacles=wm.boxes, world_half=wm.world_half,
                server_ricochet=True, max_bounces=4, wall_height=6.15,
                teleporters=wm.teleporters, link_map=lmap, tele_log=[])

    ms = _ms_per_call(run, iters=10) / len(angles)
    print(f"\n[PERF] simulate_shot_path (Ricochet+Teleporter): {ms:.4f}ms/Schuss  "
          f"({len(wm.boxes)} Boxen, {len(angles)} Winkel)")


def test_perf_shot_ray_kernels():
    box = BoxObstacle(cx=50.0, cy=0.0, bottom_z=0.0, angle=0.3,
                      half_w=10.0, half_d=10.0, height=14.0)
    pyr = BoxObstacle(cx=50.0, cy=0.0, bottom_z=0.0, angle=0.3,
                      half_w=10.0, half_d=10.0, height=14.0, is_pyramid=True)
    tele = TeleporterObstacle(name="t", cx=50.0, cy=0.0, bottom_z=0.0, angle=0.2,
                              half_w=0.5, half_d=4.0, height=10.0, border=0.5)
    ox, oy, oz = 0.0, 0.0, 1.0
    dx, dy, dz = 100.0, 5.0, 0.0
    N = 200_000
    mb = _ms_per_call(lambda: ray_box_hit(ox, oy, oz, dx, dy, dz, box), N)
    mp = _ms_per_call(lambda: ray_pyramid_hit(ox, oy, oz, dx, dy, dz, pyr), N)
    mt = _ms_per_call(lambda: ray_teleporter_crossing(ox, oy, oz, dx, dy, dz, tele), N)
    print(f"\n[PERF] ray_box_hit:             {mb * 1000:.3f}us/Aufruf")
    print(f"[PERF] ray_pyramid_hit:         {mp * 1000:.3f}us/Aufruf")
    print(f"[PERF] ray_teleporter_crossing: {mt * 1000:.3f}us/Aufruf")


# ── Hitbox-Detection ──────────────────────────────────────────────────────────

def test_perf_segment_obb_hitbox():
    args = (0.0, 0.0, 1.0, 100.0, 2.0, 1.0, 50.0, 0.0, 1.0, 0.4, 6.0, 1.9, 1.5)
    N = 200_000
    m = _ms_per_call(lambda: _segment_hits_obb_3d(*args), N)
    print(f"\n[PERF] _segment_hits_obb_3d (Hitbox-Kernel): {m * 1000:.3f}us/Aufruf")


def test_perf_resolve_incoming_shots(bot):
    bot.pos = [0.0, 0.0, 0.0]
    bot.azimuth = 0.0
    now = time.monotonic()
    # 60 anfliegende, aber verfehlende Schüsse → voller OBB-Test je Schuss, keine Treffer/Removes.
    for i in range(60):
        make_shot(bot, shooter_id=2, shot_id=i,
                  pos=(200.0, 20.0 + i * 0.5, 1.0), vel=(-100.0, 0.0, 0.0))
    ms = _ms_per_call(lambda: bot._resolve_incoming_shots(now, 0.016), iters=2000)
    print(f"\n[PERF] _resolve_incoming_shots (60 Schüsse): {ms:.4f}ms/Tick")


def test_perf_check_teleport_crossing(bot):
    """P3-NAV-02: zentraler 60-Hz-Hook — typischer Tick OHNE Querung (Loop über alle Teleporter,
    kein Treffer). Misst den Dauer-Overhead, den jeder Bewegungstick jetzt trägt."""
    wm = _perf_world()
    bot.own_flag = ""
    bot.world_half = wm.world_half
    bot._world_map = wm
    bot._link_map = build_link_map(wm.links)
    bot.pos = [0.0, 100.0, 3.0]          # weit weg von beiden Toren → kein Crossing
    bot.vel = [25.0, 0.0, 0.0]
    old = (-1.0, 100.0, 3.0)
    ms = _ms_per_call(lambda: bot._check_teleport_crossing(old, 0.0), iters=20000)
    print(f"\n[PERF] _check_teleport_crossing ({len(wm.teleporters)} Tele, kein Crossing): "
          f"{ms * 1000:.3f}us/Tick")


# ── Ricochet/Teleporter end-to-end (Winkel-Sweep) ─────────────────────────────

def test_perf_compute_ricochet_aim(bot):
    wm = _perf_world()
    bot.pos = [-150.0, -150.0, 0.0]
    bot.azimuth = 0.0
    bot.own_flag = ""
    bot.world_half = wm.world_half
    bot._world_map = wm
    bot._link_map = build_link_map(wm.links)
    bot._server_ricochet = True
    make_player(bot, pid=2, pos=(150.0, 150.0, 0.0), flag="")
    ms = _ms_per_call(lambda: bot._compute_ricochet_aim(2, None), iters=5)
    print(f"\n[PERF] _compute_ricochet_aim (Winkel-Sweep x simulate_shot_path): {ms:.3f}ms/Aufruf")
