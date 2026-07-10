"""
Regression: dünne, gedrehte (135°) Wand — OBB-Kollision hält den Tank draußen,
kein Wand-Durchschuss (normal/GM), Navigation auf der schmalen Plattform intakt.

Reale HIX-Werte (ohne Fixture-Abhängigkeit synthetisch nachgebaut):
  Wand : cx,cy=180, bottom_z=14, height=16, half_w=0.5, half_d=150, angle=135°
  Deck : cx,cy=180, bottom_z=14, height=1,  half_w=8,   half_d=107, angle=135°
Die Wand verläuft entlang y=x und teilt das 16u-Deck mittig (je Hälfte ~7,5u).
"""

import math
import time
import pytest

from bzflag.world_map import BoxObstacle, WorldMap
from bzflag.nav_graph import NavGraph
from bzflag.obstacle_grid import ObstacleGrid, LOS_GRID_PAD
from bzflag.shot_physics import simulate_shot_path
from conftest import make_player, make_shot

ANG = math.radians(135)
CA, SA = math.cos(ANG), math.sin(ANG)          # lokale x-Achse (Wand-Normale) in Welt
WX, WY = 180.0, 180.0


def _wall():
    return BoxObstacle(cx=WX, cy=WY, bottom_z=14.0, angle=ANG,
                       half_w=0.5, half_d=150.0, height=16.0)


def _deck():
    return BoxObstacle(cx=WX, cy=WY, bottom_z=14.0, angle=ANG,
                       half_w=8.0, half_d=107.0, height=1.0)


def _setup(bot, boxes):
    wm = WorldMap(boxes=list(boxes), world_half=400.0, world_hash="obbwall")
    bot._nav_graph = NavGraph(wm)
    bot._world_map = wm
    bot._shot_grid = ObstacleGrid(wm.solid_obstacles(), pad=LOS_GRID_PAD)
    bot.own_flag = ""
    return wm


def _local_x(px, py):
    """Vorzeichenbehafteter Abstand zur Wandmittellinie entlang der Wand-Normale."""
    return (px - WX) * CA + (py - WY) * SA


def _drive_into_wall(bot, z=15.0, ticks=400):
    """Fährt den Bot von der +local_x-Seite senkrecht in die Wand."""
    bot.pos_x = WX + CA * 12.0; bot.pos_y = WY + SA * 12.0; bot.pos_z = z
    bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    bot.azimuth = math.atan2(-SA, -CA)             # Heading in -local_x (in die Wand)
    for _ in range(ticks):
        bot.vel_x = math.cos(bot.azimuth) * bot._tank_speed
        bot.vel_y = math.sin(bot.azimuth) * bot._tank_speed
        bot._apply_bounds(1.0 / 60.0, 400.0)


# ── Kollision: OBB bleibt draußen ────────────────────────────────────────────

def test_drive_into_135_wall_keeps_obb_out(bot):
    _setup(bot, [_wall()])
    _drive_into_wall(bot)
    lx = _local_x(bot.pos_x, bot.pos_y)
    # Zentrum bleibt ~Halb-Länge (3,0) + Wand-Halb-Breite (0,5) draußen (war ohne Fix ~2,0).
    assert lx >= 3.3, f"Zentrum zu nah an der Wand: local_x={lx:.2f}"
    # Reale Tank-Nase (Zentrum − Halb-Länge Richtung Wand) bleibt auf der Bot-Seite (≥ -0,5).
    nose = lx - bot._tank_length / 2.0
    assert nose >= -0.5, f"Nase ragt durch die Wand: nose_local_x={nose:.2f}"


# ── Kein Wand-Durchschuss (normaler Schuss) ──────────────────────────────────

def test_pressed_bot_not_shot_through_wall(bot):
    _setup(bot, [_wall()])
    _drive_into_wall(bot)
    bot.player_id = 1
    bot.alive = True
    z = bot.pos_z + bot._tank_height * 0.5
    # Gegner 60u auf der Gegenseite; Schuss zielt auf die Bot-Position.
    ex, ey = WX - CA * 60.0, WY - SA * 60.0
    make_player(bot, 2, pos=(ex, ey, bot.pos_z))
    dx, dy = bot.pos_x - ex, bot.pos_y - ey
    d = math.hypot(dx, dy)
    vx, vy = dx / d * bot._shot_speed, dy / d * bot._shot_speed
    now = time.monotonic()
    fire = now - d / bot._shot_speed              # Schuss erreicht die Wand ~jetzt
    # server_ricochet=True erzwingt die Obstacle-Segmentierung (ohne Teleporter/Ricochet nähme
    # simulate_shot_path den Geraden-Early-Out); das erste Segment endet an der Wand.
    segs = simulate_shot_path((ex, ey, z), (vx, vy, 0.0), fire, 3.0, b"\x00\x00",
                              bot._world_map.boxes, 400.0, True,
                              teleporters=bot._world_map.teleporters, link_map=None,
                              solid_obs=bot._world_map.solid_obstacles(),
                              obs_grid=bot._shot_grid)
    make_shot(bot, shooter_id=2, shot_id=1, pos=(ex, ey, z), vel=(vx, vy, 0.0),
              fire_time=fire, lifetime=3.0)
    bot._ricochet_paths[(2, 1)] = segs
    bot._resolve_incoming_shots(now, 0.02)
    assert bot.alive, "Bot wurde durch die dünne Wand abgeschossen"


# ── Kein Wand-Durchschuss (GM-Rakete) ────────────────────────────────────────

def test_gm_does_not_hit_through_wall(bot):
    _setup(bot, [_wall()])
    bot.player_id = 1
    bot.alive = True
    # Bot 3u auf +Seite, GM-Rakete 2u auf -Seite (Wand dazwischen), Distanz < eff_r.
    bot.pos_x = WX + CA * 2.5; bot.pos_y = WY + SA * 2.5; bot.pos_z = 15.0   # dist zur GM = 4.0u < eff_r (~4.28)
    bot.azimuth = 0.0
    z = bot.pos_z + bot._tank_height * 0.5
    gx, gy = WX - CA * 1.5, WY - SA * 1.5
    make_shot(bot, shooter_id=2, shot_id=7, pos=(gx, gy, z), vel=(CA * 5.0, SA * 5.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=1, fire_time=time.monotonic())
    bot._resolve_incoming_shots(time.monotonic(), 0.02)
    assert bot.alive, "GM-Rakete traf durch die dünne Wand"


def test_gm_hits_without_wall_between(bot):
    """Gegenprobe: OHNE Wand dazwischen trifft dieselbe GM-Konstellation (Occlusion-Check
    verwirft nicht pauschal)."""
    _setup(bot, [])                                # keine Wand
    bot.player_id = 1
    bot.alive = True
    bot.pos_x = WX + CA * 2.5; bot.pos_y = WY + SA * 2.5; bot.pos_z = 15.0   # dist zur GM = 4.0u < eff_r (~4.28)
    bot.azimuth = 0.0
    z = bot.pos_z + bot._tank_height * 0.5
    gx, gy = WX - CA * 1.5, WY - SA * 1.5
    make_shot(bot, shooter_id=2, shot_id=7, pos=(gx, gy, z), vel=(CA * 5.0, SA * 5.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=1, fire_time=time.monotonic())
    bot._resolve_incoming_shots(time.monotonic(), 0.02)
    assert not bot.alive, "GM hätte ohne Wand treffen müssen"


# ── Navigation auf der schmalen Plattform ────────────────────────────────────

def test_floor_support_unchanged_on_narrow_platform(bot):
    """Floor-Support (get_floor_z) ist von der OBB-Wandkollision unberührt: der Tank steht auf
    dem Deck bis nahe an die Kante (überhängend), auch dicht an der Mittelwand."""
    _setup(bot, [_wall(), _deck()])
    # 4u von der Wandmitte (klar auf einer Deck-Hälfte, Deck-Halbbreite 8)
    bot.pos_x = WX + CA * 4.0; bot.pos_y = WY + SA * 4.0; bot.pos_z = 15.0
    assert bot._get_floor_z() == pytest.approx(15.0, abs=0.01)
    # nahe der Deck-Kante (7,5u) noch getragen
    bot.pos_x = WX + CA * 7.5; bot.pos_y = WY + SA * 7.5; bot.pos_z = 15.0
    assert bot._get_floor_z() == pytest.approx(15.0, abs=0.01)


def test_parallel_drive_along_wall_not_stuck(bot):
    """Parallelfahrt entlang einer Deck-Hälfte (Heading = Wandrichtung): der Tank kommt voran
    und bleibt auf seiner Seite (Zentrum ~1,9u von der Wandmitte, nicht durch die Wand)."""
    _setup(bot, [_wall(), _deck()])
    # Startpunkt 3u von der Wandmitte, Heading entlang der Wand-Längsachse (ang + 90°)
    start = [WX + CA * 3.0, WY + SA * 3.0, 15.0]
    bot.pos_x, bot.pos_y, bot.pos_z = start
    bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    bot.azimuth = ANG + math.pi / 2.0
    for _ in range(120):
        bot.vel_x = math.cos(bot.azimuth) * bot._tank_speed
        bot.vel_y = math.sin(bot.azimuth) * bot._tank_speed
        bot._apply_bounds(1.0 / 60.0, 400.0)
    advanced = math.hypot(bot.pos_x - start[0], bot.pos_y - start[1])
    assert advanced >= 10.0, f"Parallelfahrt blockiert: nur {advanced:.1f}u"
    lx = _local_x(bot.pos_x, bot.pos_y)
    assert lx >= 1.5, f"Tank in/durch die Wand gerutscht: local_x={lx:.2f}"
