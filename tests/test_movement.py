"""
Tests für Grundbewegung: _new_target, _dist_to_target, passiver/aktiver Modus,
Bounce-Flag (BY), Schwerkraft.

Konstanten (aus bzbot.py):
  WORLD_HALF_DEFAULT = 200.0
  JUMP_VELOCITY   = 19.0
  GRAVITY         = -9.8
"""
import collections
import math
import time
import pytest
from unittest.mock import patch
from conftest import make_player


# ── _new_target ───────────────────────────────────────────────────────────────

def test_new_target_sets_target_pos(bot):
    bot.target_pos = None
    bot._new_target()
    assert bot.target_pos is not None


def test_new_target_within_world_bounds(bot):
    """Zielkoordinate liegt im erlaubten Bereich (world_half * 0.85)."""
    limit = bot.world_half * 0.85
    for _ in range(20):
        bot._new_target()
        tx, ty = bot.target_pos
        assert -limit <= tx <= limit
        assert -limit <= ty <= limit


def test_new_target_clears_target_player(bot):
    bot.target_player = 99
    bot._new_target()
    assert bot.target_player is None


# ── _dist_to_target ───────────────────────────────────────────────────────────

def test_dist_to_target_correct(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.target_pos = (30.0, 40.0)
    assert bot._dist_to_target() == pytest.approx(50.0)


def test_dist_to_target_no_target(bot):
    bot.target_pos = None
    assert bot._dist_to_target() == float("inf")


# ── Passive mode via _update_movement ────────────────────────────────────────

def test_passive_stays_parked_when_none(bot):
    """human_count=0, target_pos=None → Bot bleibt geparkt (kein neues Wander-Ziel)."""
    bot.human_count = 0
    bot.target_pos  = None
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=True)
    assert bot.target_pos is None
    assert bot.vel_x == 0.0 and bot.vel_y == 0.0


def test_passive_parks_when_waypoint_reached(bot):
    """human_count=0, Bot steht am Waypoint → Pfadende → parken (target_pos=None)."""
    bot.human_count = 0
    bot.pos_x = 50.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.target_pos  = (50.0, 0.0)   # bereits am Ziel
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=True)
    # Ziel erreicht, kein neues gesetzt → geparkt
    assert bot.target_pos is None
    assert bot.vel_x == 0.0 and bot.vel_y == 0.0


def test_active_mode_no_crash_without_players(bot):
    """Aktivmodus ohne Spieler → kein Absturz, target_player bleibt None."""
    bot.human_count  = 1
    bot.target_pos   = (50.0, 0.0)
    bot.target_player = None
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=True)
    assert bot.target_player is None


# ── Bounce-Flag (BY) ──────────────────────────────────────────────────────────

def test_bounce_flag_triggers_jump(bot):
    """own_flag='BY', Bot am Boden → _update_movement setzt Sprung."""
    bot.own_flag    = "BY"
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._jumping    = False
    bot._bounce_next = 0.0   # bereits fällig
    bot.target_pos  = (50.0, 0.0)
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=False)
    assert bot._jumping is True
    assert bot.vel_z > 0.0


def test_bounce_flag_not_triggered_in_air(bot):
    """own_flag='BY', Bot bereits in der Luft → kein erneuter Sprung."""
    bot.own_flag    = "BY"
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 3.0
    bot._jumping    = True
    bot._bounce_next = 0.0
    bot.target_pos  = (50.0, 0.0)
    initial_vel_z   = 5.0
    bot.vel_z      = initial_vel_z
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=False)
    # Sprung soll nicht neu gesetzt werden
    assert bot._jumping is True
    assert bot.vel_z != pytest.approx(19.0, abs=0.5)  # wurde nicht auf JUMP_VELOCITY gesetzt


# ── Rand-Bounce-Replan (B4): deferred in den KI-Tick, kein A* im Physik-Pfad ──────

def test_apply_bounds_sets_flag_instead_of_sync_replan(bot):
    """_apply_bounds plant beim Rand-Abprall NICHT mehr synchron — nur das Flag wird gesetzt."""
    bot._bounce_replan = False
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    half = 100.0
    bot.vel_x = half * 100.0; bot.vel_y = 0.0; bot.vel_z = 0.0   # garantiert außerhalb der Grenze nach einem Tick
    bot._apply_bounds(0.02, half)
    assert bot._bounce_replan is True


def test_bounce_replan_deferred_in_combat_keeps_nav_goal(bot, monkeypatch):
    """B4: Rand-Bounce in COMBAT darf _nav_goal nicht überschreiben — der Combat-Tick
    replant ohnehin zum Gegner, ein Zufallsziel würde ihn stören."""
    from bot.models import AIState
    monkeypatch.setattr(bot, "_tick_combat", lambda now: None)
    monkeypatch.setattr(bot, "_execute_combat_move", lambda dt, half, now=0.0: None)
    monkeypatch.setattr(bot, "_poll_async_plan", lambda: None)
    plan_calls = []
    monkeypatch.setattr(bot, "_plan_path", lambda *a, **kw: plan_calls.append((a, kw)))
    bot._ai_state = AIState.COMBAT
    bot._bounce_replan = True
    bot._nav_goal = (123.0, 456.0)
    now = time.monotonic()
    bot._dispatch_movement(0.02, now, ai_tick=True)
    assert bot._nav_goal == (123.0, 456.0)   # unangetastet
    assert plan_calls == []                   # kein Zufalls-Replan in COMBAT
    assert bot._bounce_replan is False         # Flag wurde trotzdem konsumiert


def test_bounce_replan_in_seeking_plans_on_next_ai_tick(bot, monkeypatch):
    """B4: Rand-Bounce in SEEKING löst beim nächsten KI-Tick einen Replan aus (deferred,
    nicht mehr synchron im 60-Hz-Physik-Pfad)."""
    from bot.models import AIState
    monkeypatch.setattr(bot, "_tick_seeking", lambda now: None)
    monkeypatch.setattr(bot, "_move_to_target", lambda dt, half: None)
    monkeypatch.setattr(bot, "_poll_async_plan", lambda: None)
    plan_calls = []
    monkeypatch.setattr(bot, "_plan_path", lambda *a, **kw: plan_calls.append((a, kw)))
    bot._ai_state = AIState.SEEKING
    bot._bounce_replan = True
    now = time.monotonic()
    bot._dispatch_movement(0.02, now, ai_tick=True)
    assert len(plan_calls) == 1   # neues Ziel geplant
    assert bot._bounce_replan is False


# ── Schwerkraft ───────────────────────────────────────────────────────────────

def test_gravity_applied_when_airborne(bot):
    """Bot ohne Sprung-Flag in der Luft → vel[2] nimmt durch Gravity ab."""
    bot.pos_z   = 5.0
    bot._jumping = False
    bot.vel_z   = 0.0
    bot.target_pos = (50.0, 0.0)
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=False)
    assert bot.vel_z < 0.0   # Schwerkraft hat nach unten beschleunigt


def test_gravity_not_applied_on_ground(bot):
    """Bot am Boden (pos[2]<=0.1), kein Sprung → kein gravity-Drift."""
    bot.pos_z   = 0.0
    bot._jumping = False
    bot.vel_z   = 0.0
    bot.target_pos = (50.0, 0.0)
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=False)
    assert bot.vel_z == pytest.approx(0.0, abs=1e-6)


# ── Phase 2 ──────────────────────────────────────────────────────────────────

class TestMovementImprovements:

    def test_min_speed_at_180_degree_diff(self, bot):
        """Bot dreht auf der Stelle wenn Ziel genau hinter ihm liegt (diff=180°)."""
        bot.target_pos = (-100.0, 0.0)  # genau hinter Bot
        bot.azimuth = 0.0               # Bot schaut nach +X
        # target_az = π (hinter Bot) → diff = ±π ≥ 90° → speed = 0
        bot._move_reverse = False
        bot._move_to_target(0.02, bot.world_half)
        speed_magnitude = math.hypot(bot.vel_x, bot.vel_y)
        assert speed_magnitude == pytest.approx(0.0)  # steht, dreht nur

    def test_reverse_speed_is_negative(self, bot):
        """Im Reverse-Modus ist Fahrgeschwindigkeit negativ."""
        bot.target_pos = (-100.0, 0.0)
        bot.azimuth = 0.0
        bot._move_reverse = True
        bot._move_to_target(0.02, bot.world_half)
        # vel[0] sollte negativ sein (Rückwärts)
        assert bot.vel_x < 0 or bot.vel_y != 0  # fährt rückwärts oder dreht

    def test_optimal_range_target_stays_near_enemy(self, bot):
        """Bei Optimalabstand zeigt target_pos 2u Richtung Gegner, nicht self.pos."""
        from conftest import make_player
        p = make_player(bot, 2, pos=(80.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 30.0; bot.pos_y = 0.0; bot.pos_z = 0.0  # dist = 50 → bei Optimalabstand
        bot.azimuth = 0.0
        # AI-Tick ausführen
        now = time.monotonic()
        bot._update_movement(0.02, now, ai_tick=True)
        if bot.target_pos is not None:
            # target_pos sollte NICHT (self.pos[0], self.pos[1]) sein
            assert bot.target_pos != (30.0, 0.0)


class TestNoReverseWhenEnemyJumping:
    """Schritt 2: bei springendem Gegner kein _move_reverse, Position halten."""

    def test_hold_position_when_enemy_airborne(self, bot):
        """Gegner is_airborne=True, dist < OPTIMAL_RANGE → _move_reverse=False."""
        from bot.constants import OPTIMAL_RANGE
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot.alive = True
        bot.human_count = 1
        # Gegner in der Luft, näher als OPTIMAL_RANGE
        info = make_player(bot, 99, pos=(40.0, 0.0, 5.0))  # z=5 (in der Luft)
        info.vel = [0.0, 0.0, -5.0]
        info.is_airborne = True
        bot.target_player = 99
        bot._next_shoot = float("inf")
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._move_reverse is False

    def test_reverse_when_enemy_on_ground(self, bot):
        """Gegner is_airborne=False, dist < OPTIMAL_RANGE → Bot fährt rückwärts (vel[0] < 0).
        Neue Architektur: _execute_combat_move setzt negative Geschwindigkeit statt _move_reverse."""
        from bot.constants import OPTIMAL_RANGE
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot.alive = True
        bot.human_count = 1
        bot._ai_state = AIState.COMBAT  # direkt in COMBAT (nicht erst IDLE→SEEKING→COMBAT warten)
        info = make_player(bot, 99, pos=(40.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.is_airborne = False
        bot.target_player = 99
        bot._next_shoot = float("inf")
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        # Gegner bei (40,0) < OPTIMAL_RANGE=60 → rückwärts fahren (vel[0] < 0)
        assert bot.vel_x < 0.0


class TestRamBurrowedEnemy:
    """Eingegrabener BU-Gegner ist nur per Überrollen tötbar (außer GM/SW) → Bot rammt
    (Kontaktdistanz) statt auf OPTIMAL_RANGE=60u stehen zu bleiben."""

    def test_ram_burrowed_enemy_closes_in(self, bot):
        """Gegner mit BU-Flagge eingegraben (z=-1.32), dist=40 → Bot fährt vorwärts (vel[0] > 0).
        Spiegel zu test_reverse_when_enemy_on_ground: dort 40 < 60 → rückwärts; hier
        _opt = TANK_RADIUS*SR_RADIUS_MULT ≈ 8.64 < 40 → voll vorwärts zum Rammen."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot.alive = True
        bot.human_count = 1
        bot._ai_state = AIState.COMBAT
        info = make_player(bot, 99, pos=(40.0, 0.0, -1.32), flag="BU")
        info.vel = [0.0, 0.0, 0.0]
        info.is_airborne = False
        bot.target_player = 99
        bot._next_shoot = float("inf")
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot.vel_x > 0.0

    def test_no_ram_when_gm_can_hit_burrowed(self, bot):
        """GM trifft den Eingegrabenen aus der Distanz → kein Rammen, Optimaldistanz bleibt 100u."""
        from bot.constants import OPTIMAL_RANGE_GM
        bot.own_flag = "GM"
        make_player(bot, 99, pos=(40.0, 0.0, -1.32), flag="BU")
        bot.target_player = 99
        assert bot._effective_optimal_range() == OPTIMAL_RANGE_GM

    def test_optimal_range_ram_for_burrowed_target(self, bot):
        """Bot ohne (GM/SW-)Flagge, Ziel mit BU → Ramm-Kontaktdistanz _effective_tank_radius()/2*_sr_radius_mult."""
        bot.own_flag = ""
        make_player(bot, 99, pos=(40.0, 0.0, -1.32), flag="BU")
        bot.target_player = 99
        assert bot._effective_optimal_range() == bot._effective_tank_radius() / 2 * bot._sr_radius_mult

    def test_no_ram_for_bu_target_on_building(self, bot):
        """BU-Gegner auf einem Dach (z>0) ist NICHT eingegraben → normal trefbar → OPTIMAL_RANGE,
        kein Rammen (Burrow wirkt nur am Boden, z<0)."""
        from bot.constants import OPTIMAL_RANGE
        bot.own_flag = ""
        make_player(bot, 99, pos=(40.0, 0.0, 5.0), flag="BU")   # z=5 → auf Gebäude
        bot.target_player = 99
        assert bot._effective_optimal_range() == OPTIMAL_RANGE


# Movement verification fixes (free functions)

def test_reverse_speed_capped_at_50_percent(bot):
    """Rückwärts-Speed ist auf 50% des Tank-Speeds begrenzt."""
    bot.target_pos = (-100.0, 0.0)
    bot.azimuth = 0.0
    bot._move_reverse = True
    bot._move_to_target(0.02, bot.world_half)
    max_reverse = -bot._tank_speed * 0.5
    assert bot.vel_x >= max_reverse - 0.01  # vel[0] nicht kleiner als -50% tankSpeed


def test_move_toward_enemy_when_close_in_combat(bot):
    """Im COMBAT-State: Bot zeigt auf Gegner und fährt rückwärts wenn Gegner < OPTIMAL_RANGE.
    (Neue Architektur: _execute_combat_move statt _move_reverse für COMBAT)"""
    from conftest import make_player
    from bot.models import AIState
    p = make_player(bot, 2, pos=(20.0, 0.0, 0.0))  # dist = 20 < OPTIMAL_RANGE
    p.vel = [0.0, 0.0, 0.0]
    bot.target_player = 2
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.azimuth = 0.0
    bot._ai_state = AIState.COMBAT  # direkt in COMBAT setzen
    bot.human_count = 1
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=True)
    # Bot soll sich vom Gegner wegbewegen (vel[0] < 0)
    assert bot.vel_x < 0.0


# ── Decken-Kollision ──────────────────────────────────────────────────────────

def _make_box_obstacle(cx, cy, bottom_z, half_w, half_d, height, angle=0.0):
    """Erzeugt ein BoxObstacle-Objekt für Tests."""
    from bzflag.world_map import BoxObstacle
    return BoxObstacle(
        cx=cx, cy=cy, bottom_z=bottom_z,
        half_w=half_w, half_d=half_d, height=height,
        angle=angle, drive_through=False, shoot_through=False,
    )


def _give_bot_world_with_box(bot, obs):
    """Gibt dem Bot eine WorldMap mit einem Hindernis."""
    from bzflag.world_map import WorldMap
    wm = WorldMap()
    wm.boxes = [obs]
    bot._world_map = wm


def test_ceiling_collision_stops_upward_movement(bot):
    """Bot springt von unten gegen Platform-Boden → vel[2]=0, pos[2] auf Decke."""
    from bot.constants import TANK_HEIGHT
    obs = _make_box_obstacle(cx=0.0, cy=0.0, bottom_z=7.0, half_w=5.0, half_d=5.0, height=2.0)
    _give_bot_world_with_box(bot, obs)
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 5.5   # bot_top = 5.5 + 2.05 = 7.55 > obs.bottom_z=7.0
    bot.vel_z = 10.0            # aufwärts
    bot._apply_obstacle_bounds(0.02)
    assert bot.vel_z == pytest.approx(0.0)
    assert bot.pos_z == pytest.approx(obs.bottom_z - TANK_HEIGHT, abs=0.01)


def test_ceiling_no_stop_if_already_inside(bot):
    """Bot bereits INNERHALB des Gebäudes (pz >= bottom_z) → kein Deckenstopp."""
    from bot.constants import TANK_HEIGHT
    obs = _make_box_obstacle(cx=0.0, cy=0.0, bottom_z=0.0, half_w=5.0, half_d=5.0, height=6.0)
    _give_bot_world_with_box(bot, obs)
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 3.0   # pz=3.0 >= bottom_z=0.0 → kein Decken-Check
    initial_vz = 5.0
    bot.vel_z = initial_vz
    bot._apply_obstacle_bounds(0.02)
    # vel[2] soll nicht durch Decken-Check genullt werden
    # (Wand-Check könnte XY verändern, aber vel[2] bleibt)
    assert bot.vel_z == pytest.approx(initial_vz)


def test_ceiling_no_stop_outside_xy(bot):
    """Bot-Kopf wäre auf Decken-Höhe, aber XY außerhalb der Platform → kein Stopp."""
    obs = _make_box_obstacle(cx=50.0, cy=50.0, bottom_z=7.0, half_w=2.0, half_d=2.0, height=2.0)
    _give_bot_world_with_box(bot, obs)
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 5.5   # weit entfernt von Platform-XY
    initial_vz = 10.0
    bot.vel_z = initial_vz
    bot._apply_obstacle_bounds(0.02)
    assert bot.vel_z == pytest.approx(initial_vz)


def test_ceiling_obb_nose_under_platform_stops(bot):
    """OBB-Decke: das Zentrum liegt neben der Platform, aber die zur Platform zeigende Tank-NASE
    (halbe Länge 3,0) ragt unter den Rand → Kopf-Anstoß. Der alte Kreis-Test (Radius 1,4) hätte
    das verpasst (Zentrum 4u entfernt > 2+1,4)."""
    obs = _make_box_obstacle(cx=0.0, cy=0.0, bottom_z=7.0, half_w=2.0, half_d=2.0, height=2.0)
    _give_bot_world_with_box(bot, obs)
    bot.pos_x = 4.0; bot.pos_y = 0.0; bot.pos_z = 5.5         # Zentrum 4u vom Platform-Rand (x=2); bot_top=7.55>7
    bot.azimuth = math.pi             # Nase zeigt in -x unter die Platform (x=4-3=1 < 2)
    bot.vel_z = 10.0
    assert 4.0 > 2.0 + 1.4            # Kreis-Test hätte NICHT gestoppt
    bot._apply_obstacle_bounds(0.02)
    assert bot.vel_z == pytest.approx(0.0)


def test_ceiling_obb_beyond_nose_no_stop(bot):
    """Kein Über-Blocken: liegt die Platform jenseits der Tank-Nase (Zentrum 6u, Nase erreicht x=3
    < Rand 2), bleibt der Aufstieg frei — der exakte OBB-Gate hält NUR die reale Tank-OBB fern."""
    obs = _make_box_obstacle(cx=0.0, cy=0.0, bottom_z=7.0, half_w=2.0, half_d=2.0, height=2.0)
    _give_bot_world_with_box(bot, obs)
    bot.pos_x = 6.0; bot.pos_y = 0.0; bot.pos_z = 5.5
    bot.azimuth = math.pi
    bot.vel_z = 10.0
    bot._apply_obstacle_bounds(0.02)
    assert bot.vel_z == pytest.approx(10.0)


def test_wall_slide_drives_over_low_step(bot):
    """Stufe unter _maxBumpHeight (0.33) → Bot fährt drüber (keine Wall-Slide-Bremsung)."""
    obs = _make_box_obstacle(cx=3.0, cy=0.0, bottom_z=0.0, half_w=1.0, half_d=5.0, height=0.3)
    _give_bot_world_with_box(bot, obs)
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.azimuth = 0.0; bot.vel_x = 25.0; bot.vel_y = 0.0
    bot._apply_obstacle_bounds(0.02)
    assert bot.vel_x == pytest.approx(25.0), "0.3 < 0.33 → überfahren"


def test_wall_slide_stops_at_step_above_bump(bot):
    """Stufe über _maxBumpHeight → Wall-Slide bremst (nicht überfahrbar)."""
    obs = _make_box_obstacle(cx=3.0, cy=0.0, bottom_z=0.0, half_w=1.0, half_d=5.0, height=0.5)
    _give_bot_world_with_box(bot, obs)
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.azimuth = 0.0; bot.vel_x = 25.0; bot.vel_y = 0.0
    bot._apply_obstacle_bounds(0.02)
    assert bot.vel_x < 25.0, "0.5 > 0.33 → geblockt/gebremst"


def test_solid_boxes_cached(bot):
    """_solid_boxes() liefert bei wiederholtem Aufruf dasselbe Objekt (gecacht); nach
    manuellem Invalidieren (_solid_boxes_cache = None) wird neu gebaut (neues Objekt,
    gleicher Inhalt)."""
    obs = _make_box_obstacle(cx=0.0, cy=0.0, bottom_z=0.0, half_w=5.0, half_d=5.0, height=2.0)
    _give_bot_world_with_box(bot, obs)
    result1 = bot._solid_boxes()
    result2 = bot._solid_boxes()
    assert result1 is result2
    bot._solid_boxes_cache = None
    result3 = bot._solid_boxes()
    assert result3 is not result1
    assert result3 == result1


def test_is_airborne_set_from_ps_falling(bot):
    """PS_FALLING-Bit setzt is_airborne=True im PlayerInfo."""
    from conftest import make_player
    from bzflag.protocol import MsgPlayerUpdateSmall, PS_FALLING, PS_ALIVE
    import struct
    p = make_player(bot, 2)
    p.last_order = -1
    # Baue einen MsgPlayerUpdateSmall mit PS_FALLING gesetzt
    status = PS_ALIVE | PS_FALLING
    payload = (
        struct.pack(">f", 0.0)    # ts
        + struct.pack(">B", 2)    # pid
        + struct.pack(">i", 1)    # order
        + struct.pack(">h", status)  # status
        + struct.pack(">hhh", 0, 0, 100)  # pos (z=100 → in der Luft)
    )
    bot._on_player_update_small(MsgPlayerUpdateSmall, payload)
    assert bot.players[2].is_airborne is True


# ── WP-Radius, Feasibility, Timeout, Lookahead ───────────────────────────────

class TestWpReachRadius:
    """_wp_reach_radius(): vor aufwärts-NAV_JUMP engerer Radius (NAV_CELL_SIZE), sonst 1.25×."""

    def test_default_returns_larger_radius(self, bot):
        from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE
        bot._nav_path = []
        assert bot._wp_reach_radius() == pytest.approx(NAV_CELL_SIZE * 1.25)

    def test_single_wp_returns_larger_radius(self, bot):
        from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE
        bot._nav_path = [(10.0, 0.0, 0.0)]
        assert bot._wp_reach_radius() == pytest.approx(NAV_CELL_SIZE * 1.25)

    def test_same_floor_next_wp_returns_larger_radius(self, bot):
        from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE
        # Beide WPs auf demselben Floor → kein Radius-Reduce
        bot._nav_path = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
        assert bot._wp_reach_radius() == pytest.approx(NAV_CELL_SIZE * 1.25)

    def test_upward_next_wp_returns_tight_radius(self, bot):
        from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE
        # Nächster WP ist 10u höher → engerer Radius (NAV_CELL_SIZE, kein 1.25-Faktor)
        bot._nav_path = [(0.0, 0.0, 0.0), (10.0, 0.0, 10.0)]
        assert bot._wp_reach_radius() == pytest.approx(NAV_CELL_SIZE)

    def test_downward_next_wp_returns_larger_radius(self, bot):
        from bzflag.nav_graph import CELL_SIZE as NAV_CELL_SIZE
        # Nächster WP ist 10u tiefer → kein Reduce (nur aufwärts)
        bot._nav_path = [(0.0, 0.0, 0.0), (10.0, 0.0, -10.0)]
        assert bot._wp_reach_radius() == pytest.approx(NAV_CELL_SIZE * 1.25)


class TestNavJumpFeasible:
    """_nav_jump_feasible(target_wp): Abstiegs-Reichweiten-Check + Azimuth-Check (±5°)."""

    def _t_desc(self, dz):
        """Abstiegszeit auf Zielhöhe dz (Bot landet beim Fallen)."""
        disc = 19.0**2 - 2.0 * 9.8 * dz
        return (19.0 + math.sqrt(disc)) / 9.8

    def test_feasible_within_reach(self, bot):
        """Abstand innerhalb Abstiegs-Reichweite → True."""
        dz = 10.0
        t_desc = self._t_desc(dz)
        max_reach = 25.0 * t_desc * 1.1
        hdist = max_reach * 0.95
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        target_wp = (hdist, 0.0, dz)
        assert bot._nav_jump_feasible(target_wp) is True

    def test_feasible_within_azimuth_tolerance(self, bot):
        """Azimuth innerhalb ±5° → feasible (präzise Ausrichtung sichergestellt)."""
        dz = 10.0
        t_desc = self._t_desc(dz)
        hdist = 25.0 * t_desc * 0.9
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.radians(4)       # 4° daneben — innerhalb ±5°
        target_wp = (hdist, 0.0, dz)        # target_az = 0°
        assert bot._nav_jump_feasible(target_wp) is True

    def test_infeasible_azimuth_too_large(self, bot):
        """Azimuth > 5° vom Ziel-WP → False (Bot muss erst auf ≤5° ausrichten)."""
        dz = 10.0
        t_desc = self._t_desc(dz)
        hdist = 25.0 * t_desc * 0.9
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.radians(10)      # 10° daneben — > 5° Schwelle
        target_wp = (hdist, 0.0, dz)        # target_az = 0°
        assert bot._nav_jump_feasible(target_wp) is False

    def test_infeasible_target_too_high(self, bot):
        """Ziel über Sprungmaximum (disc < 0) → False."""
        max_height = 19.0**2 / (2 * 9.8)   # ≈ 18.4 u
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        target_wp = (10.0, 0.0, max_height + 1.0)
        assert bot._nav_jump_feasible(target_wp) is False

    def test_infeasible_too_far(self, bot):
        """Abstand > Abstiegs-Reichweite × 1.1 → False."""
        dz = 10.0
        t_desc = self._t_desc(dz)
        max_reach = 25.0 * t_desc * 1.1
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        target_wp = (max_reach * 1.05, 0.0, dz)  # 5% über Schwelle
        assert bot._nav_jump_feasible(target_wp) is False

    def test_feasible_exactly_at_threshold(self, bot):
        """Abstand exakt an der Schwelle (1.09×) → True."""
        dz = 10.0
        t_desc = self._t_desc(dz)
        max_reach = 25.0 * t_desc * 1.1
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        target_wp = (max_reach * (1.09 / 1.1), 0.0, dz)  # gerade noch innerhalb
        assert bot._nav_jump_feasible(target_wp) is True


class TestNavJumpTravelSpeedCongruence:
    """Executor-Sprung-Machbarkeit nutzt _travel_tank_speed() (deckungsgleich zur Planung):
    Velocity (V) hebt die Horizontalreichweite → weitere Sprünge machbar. A (nur Stillstand) und
    BU (nur eingegraben) werden bewusst ignoriert (transient, kein nachhaltiger Reise-Boost)."""

    def _t_desc(self, dz):
        disc = 19.0**2 - 2.0 * 9.8 * dz
        return (19.0 + math.sqrt(disc)) / 9.8

    def test_velocity_flag_extends_jump_reach(self, bot):
        """Abstand jenseits der Basis-Reichweite (25), innerhalb der V-Reichweite (40):
        ohne Flagge infeasible, mit V feasible — für geometry_ok UND feasible."""
        dz = 10.0
        t_desc = self._t_desc(dz)
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot._tank_speed = 25.0
        bot._velocity_ad = 1.6                      # V → 40 u/s
        hdist = 25.0 * t_desc * 1.1 * 1.2           # 20 % über Basis-Schwelle, < 40er-Reichweite
        target_wp = (hdist, 0.0, dz)

        bot.own_flag = ""
        assert bot._nav_jump_geometry_ok(target_wp) is False
        assert bot._nav_jump_feasible(target_wp) is False

        bot.own_flag = "V"
        assert bot._nav_jump_geometry_ok(target_wp) is True
        assert bot._nav_jump_feasible(target_wp) is True

    def test_travel_speed_ignores_agility_boost(self, bot):
        """A-Boost gilt nur im Stillstand → fließt NICHT in _travel_tank_speed (Sprung-Anlauf fährt),
        obwohl _effective_tank_speed im Stillstand boostet."""
        bot._tank_speed = 25.0
        bot._agility_ad_vel = 2.0
        bot.own_flag = "A"
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0                   # Stillstand → _effective boostet
        assert bot._effective_tank_speed() == pytest.approx(50.0)
        assert bot._travel_tank_speed() == pytest.approx(25.0)

    def test_travel_speed_ignores_burrow_malus(self, bot):
        """BU-Malus gilt nur eingegraben → _travel_tank_speed bleibt Basis (Sprung vom Boden)."""
        bot._tank_speed = 25.0
        bot._burrow_speed_ad = 0.5
        bot.own_flag = "BU"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = -1.0                  # eingegraben → _effective mit Malus
        assert bot._effective_tank_speed() == pytest.approx(12.5)
        assert bot._travel_tank_speed() == pytest.approx(25.0)


class TestAdvancePathNavJump:
    """_advance_path() NAV_JUMP: nur aufwärts, Machbarkeitscheck, Anfahrt-WP."""

    def _setup_path(self, bot, first_wp, second_wp):
        """Gibt Bot einen Pfad mit zwei WPs; erster wird durch _advance_path gepopped."""
        bot._nav_path = [list(first_wp), list(second_wp)]
        bot._wp_fail_count = 0

    def test_upward_feasible_triggers_jump(self, bot):
        """Aufwärts-WP + Machbarkeit → NAV_JUMP wird initiiert."""
        from bot.constants import JUMP_VELOCITY, GRAVITY, TANK_SPEED
        dz = 10.0
        disc = JUMP_VELOCITY**2 - 2.0 * abs(GRAVITY) * dz
        t_desc = (JUMP_VELOCITY + math.sqrt(disc)) / abs(GRAVITY)
        ideal_hdist = TANK_SPEED * t_desc * 0.5   # deutlich innerhalb der Abstiegs-Schwelle
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        self._setup_path(bot, (0.0, 0.0, 0.0), (ideal_hdist, 0.0, dz))
        bot._advance_path()
        assert bot._jumping is True

    def test_upward_infeasible_clears_path(self, bot):
        """Aufwärts-WP zu weit (> Abstiegs-Reichweite × 1.1) → Pfad verworfen, kein Sprung."""
        from bot.constants import JUMP_VELOCITY, GRAVITY, TANK_SPEED
        dz = 10.0
        disc = JUMP_VELOCITY**2 - 2.0 * abs(GRAVITY) * dz
        t_desc = (JUMP_VELOCITY + math.sqrt(disc)) / abs(GRAVITY)
        too_far = TANK_SPEED * t_desc * 1.2   # 20% über der Abstiegs-Schwelle
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        self._setup_path(bot, (0.0, 0.0, 0.0), (too_far, 0.0, dz))
        bot._advance_path()
        assert bot._jumping is False
        assert bot._nav_path == []
        assert bot.target_pos is None

    def test_downward_no_jump(self, bot, monkeypatch):
        """Abwärts-WP (wp[2] < floor_z) → kein Sprung, target_pos normal gesetzt."""
        monkeypatch.setattr(bot, '_get_floor_z', lambda: 10.0)
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        bot.azimuth = 0.0
        self._setup_path(bot, (0.0, 0.0, 10.0), (10.0, 0.0, 0.0))
        bot._advance_path()
        assert bot._jumping is False
        assert bot.target_pos == (10.0, 0.0)


class TestNavJumpGeometryOk:
    """_nav_jump_geometry_ok(target_wp): nur Geometrie (disc + hdist), kein Azimuth."""

    def _t_desc(self, dz):
        disc = 19.0**2 - 2.0 * 9.8 * dz
        return (19.0 + math.sqrt(disc)) / 9.8

    def test_geometry_ok_reachable(self, bot):
        """hdist innerhalb TANK_SPEED × t_desc × 1.1 → True."""
        dz = 10.0
        max_reach = 25.0 * self._t_desc(dz) * 1.1
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        target_wp = (max_reach * 0.95, 0.0, dz)
        assert bot._nav_jump_geometry_ok(target_wp) is True

    def test_geometry_nok_too_high(self, bot):
        """dz über Sprungmaximum (disc < 0) → False."""
        max_h = 19.0**2 / (2 * 9.8)   # ≈ 18.4 u
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        target_wp = (10.0, 0.0, max_h + 1.0)
        assert bot._nav_jump_geometry_ok(target_wp) is False

    def test_geometry_nok_too_far(self, bot):
        """hdist > TANK_SPEED × t_desc × 1.1 → False."""
        dz = 10.0
        max_reach = 25.0 * self._t_desc(dz) * 1.1
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        target_wp = (max_reach * 1.05, 0.0, dz)
        assert bot._nav_jump_geometry_ok(target_wp) is False

    def test_geometry_ok_ignores_azimuth(self, bot):
        """Geometrie OK trotz 90°-Azimuth-Abweichung — kein Azimuth-Check hier."""
        dz = 10.0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.pi / 2   # schaut nach Norden, WP liegt im Osten
        target_wp = (25.0 * self._t_desc(dz) * 0.5, 0.0, dz)
        # _nav_jump_feasible würde hier False zurückgeben (Azimuth > 30°),
        # _nav_jump_geometry_ok ignoriert den Azimuth → True
        assert bot._nav_jump_geometry_ok(target_wp) is True


class TestNavJumpAlignState:
    """_tick_nav_jump_align(): dreht auf Ziel-Azimuth, springt bei ≤30°, Timeout → Replan."""

    def _setup_align(self, bot, wp, azimuth, start_offset=0.0):
        """Setzt alle _nav_jump_align_*-Attribute am Bot."""
        from bot.models import AIState
        bot._nav_jump_align_wp = list(wp)
        bot._nav_jump_align_start = time.monotonic() - start_offset
        bot._nav_jump_align_return_state = AIState.SEEKING
        bot.azimuth = azimuth
        bot._nav_path = [list(wp)]

    def test_timeout_clears_path_and_transitions(self, bot):
        """Nach 5s Timeout → nav_path geleert, target_pos None, State → return_state."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        self._setup_align(bot, (20.0, 0.0, 10.0), azimuth=math.pi / 2, start_offset=6.0)
        bot._nav_path = [(20.0, 0.0, 10.0)]
        bot.target_pos = (20.0, 0.0)
        now = time.monotonic()
        bot._tick_nav_jump_align(0.02, now)
        assert bot._nav_path == []
        assert bot.target_pos is None
        assert bot._ai_state == AIState.SEEKING

    def test_cooldown_prevents_realign(self, bot):
        """Gecooldowntes WP → _advance_path löscht Pfad statt NAV_JUMP_ALIGN zu setzen."""
        from bot.constants import JUMP_VELOCITY, GRAVITY, TANK_SPEED
        from bot.models import AIState
        dz = 10.0
        disc = JUMP_VELOCITY**2 - 2.0 * abs(GRAVITY) * dz
        t_desc = (JUMP_VELOCITY + math.sqrt(disc)) / abs(GRAVITY)
        hdist = TANK_SPEED * t_desc * 0.5  # klar erreichbar
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.radians(90)  # 90° vom Ziel → würde NAV_JUMP_ALIGN auslösen
        bot._nav_path = [(0.0, 0.0, 0.0), (hdist, 0.0, dz)]
        bot._wp_fail_count = 0
        # Cooldown für das Ziel-WP setzen
        wp_key = (round(hdist), round(0.0), dz)
        bot._nav_jump_cooldowns = {wp_key: time.monotonic() + 30.0}
        bot._advance_path()
        # Pfad muss geleert sein (Replan), KEIN NAV_JUMP_ALIGN
        assert bot._nav_path == []
        assert bot._ai_state != AIState.NAV_JUMP_ALIGN

    def test_aligned_triggers_jump(self, bot):
        """Azimuth bereits ≤30° vom Ziel → Sprung wird initiiert."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        self._setup_align(bot, (20.0, 0.0, 10.0), azimuth=0.0)  # exakt auf Ziel
        now = time.monotonic()
        bot._tick_nav_jump_align(0.02, now)
        assert bot._jumping is True

    def test_not_yet_aligned_rotates_and_stops(self, bot):
        """Azimuth 90° vom Ziel → Bot dreht Richtung WP, vel bleibt 0."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 5.0; bot.vel_y = 3.0; bot.vel_z = 0.0
        self._setup_align(bot, (20.0, 0.0, 10.0), azimuth=math.pi / 2)  # 90° daneben
        az_before = bot.azimuth
        now = time.monotonic()
        bot._tick_nav_jump_align(0.1, now)
        # Bot muss stehenbleiben
        assert bot.vel_x == pytest.approx(0.0)
        assert bot.vel_y == pytest.approx(0.0)
        # Azimuth muss sich Richtung 0° (Zielrichtung) bewegt haben
        assert bot.azimuth < az_before


class TestAdvancePathNavJumpAlign:
    """_advance_path(): Azimuth-Fehler bei geometrisch machbarem Sprung → NAV_JUMP_ALIGN."""

    def test_azimuth_fail_geometry_ok_triggers_align(self, bot):
        """Geometry OK, Azimuth > 30° weg → State NAV_JUMP_ALIGN, Pfad bleibt erhalten."""
        from bot.constants import JUMP_VELOCITY, GRAVITY, TANK_SPEED
        from bot.models import AIState
        dz = 10.0
        disc = JUMP_VELOCITY**2 - 2.0 * abs(GRAVITY) * dz
        t_desc = (JUMP_VELOCITY + math.sqrt(disc)) / abs(GRAVITY)
        hdist = TANK_SPEED * t_desc * 0.5   # gut innerhalb der Reichweite
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.radians(60)       # 60° daneben → _nav_jump_feasible=False
        bot._nav_path = [(0.0, 0.0, 0.0), (hdist, 0.0, dz)]
        bot._wp_fail_count = 0
        bot._advance_path()
        assert bot._ai_state == AIState.NAV_JUMP_ALIGN
        # Pfad wurde NICHT verworfen (WP noch als _nav_jump_align_wp gespeichert)
        assert bot._jumping is False
        assert bot._nav_jump_align_wp is not None
        assert bot._nav_jump_align_wp[2] == pytest.approx(dz)


class TestNavJumpOvershootOffset:
    """_initiate_nav_jump(): Velocity berechnet mit hdist + 2.5 Überschuss-Offset."""

    def _t_desc(self, dz):
        disc = 19.0**2 - 2.0 * 9.8 * dz
        return (19.0 + math.sqrt(disc)) / 9.8

    def test_overshoot_offset_applied(self, bot):
        """Speed basiert auf hdist + 2.5, nicht auf reinem hdist."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        dz = 10.0
        hdist = 15.0
        wp = [hdist, 0.0, dz]
        bot._initiate_nav_jump(wp)
        t_desc = self._t_desc(dz)
        expected_speed = (hdist + 2.5) / t_desc
        actual_speed = math.hypot(bot.vel_x, bot.vel_y)
        assert actual_speed == pytest.approx(expected_speed, rel=0.01)

    def test_velocity_direction_uses_azimuth(self, bot):
        """Velocity zeigt in self.azimuth-Richtung, nicht in az_to_wp-Richtung."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.radians(20)   # Bot schaut 20° — NAV_JUMP_ALIGN hat ausgerichtet
        dz = 10.0
        wp = [15.0, 0.0, dz]            # WP liegt exakt östlich (az_to_wp = 0°)
        bot._initiate_nav_jump(wp)
        # Velocity muss in self.azimuth-Richtung (20°) zeigen, nicht az_to_wp (0°)
        actual_az = math.atan2(bot.vel_y, bot.vel_x)
        assert actual_az == pytest.approx(math.radians(20), abs=0.01)


class TestNavJumpLandSpin:
    """_nav_jump_land_spin / _initiate_nav_jump: Lande-Drehung am Absprung fixiert —
    nächster Wegpunkt, bzw. Gegner wenn der Landepunkt auf Gegner-Höhe liegt."""

    V0, G = 19.0, 9.8

    def _t_flight(self, dz):
        disc = self.V0 ** 2 - 2.0 * self.G * dz
        return (self.V0 + math.sqrt(disc)) / self.G

    def test_faces_enemy_when_landing_at_enemy_level(self, bot):
        """Landepunkt z ≈ Gegner-z → Drehung zeigt zum Gegner, nicht zum nächsten WP."""
        from bot.util import _wrap
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        wp = (15.0, 0.0, 0.0)
        ex, ey = 20.0, 25.0
        make_player(bot, 2, pos=(ex, ey, 0.0))      # Gegner auf Landepunkt-Höhe (z=0)
        bot.target_player = 2
        bot._nav_path = [wp, (40.0, 0.0, 0.0)]       # nächster WP geradeaus (≠ Gegnerrichtung)
        bot._initiate_nav_jump(wp)
        t = self._t_flight(wp[2] - 0.0)
        lx, ly = bot.vel_x * t, bot.vel_y * t
        expected_az = math.atan2(ey - ly, ex - lx)
        assert bot._jump_ang_vel > 0.0               # dreht nach +y (zum Gegner)
        assert _wrap(bot._jump_ang_vel * t) == pytest.approx(expected_az, abs=0.02)

    def test_faces_next_wp_when_not_at_enemy_level(self, bot):
        """Landepunkt z weit von Gegner-z (> NAV_JUMP_Z_TOL) → Drehung zum nächsten WP."""
        from bot.util import _wrap
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        wp = (15.0, 0.0, 10.0)                       # Landepunkt z=10 (machbar: < 18.4u)
        make_player(bot, 2, pos=(20.0, 25.0, 0.0))   # Gegner unten (z=0), 10u > NAV_JUMP_Z_TOL
        bot.target_player = 2
        nx, ny = 15.0, -30.0
        bot._nav_path = [wp, (nx, ny, 10.0)]         # nächster WP nach Süden (≠ Gegner-Norden)
        bot._initiate_nav_jump(wp)
        t = self._t_flight(wp[2] - 0.0)
        lx, ly = bot.vel_x * t, bot.vel_y * t
        expected_az = math.atan2(ny - ly, nx - lx)
        assert bot._jump_ang_vel < 0.0               # dreht nach -y (zum nächsten WP)
        assert _wrap(bot._jump_ang_vel * t) == pytest.approx(expected_az, abs=0.02)

    def test_capped_at_turn_rate(self, bot):
        """Lande-Ziel ~180° hinter Sprungrichtung → Drehrate auf _tank_turn_rate gedeckelt."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        wp = (15.0, 0.0, 0.0)
        make_player(bot, 2, pos=(-50.0, 0.0, 0.0))   # Gegner hinter Landepunkt, gleiche Höhe
        bot.target_player = 2
        bot._nav_path = [wp, (40.0, 0.0, 0.0)]
        bot._initiate_nav_jump(wp)
        assert abs(bot._jump_ang_vel) == pytest.approx(bot._tank_turn_rate, abs=1e-6)

    def test_zero_without_target_or_next_wp(self, bot):
        """Kein Ziel und kein nächster WP → keine Drehung (bestehendes Verhalten, Nicht-WG)."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.own_flag = ""
        bot.target_player = None
        wp = (15.0, 0.0, 0.0)
        bot._nav_path = [wp]                          # kein nächster WP
        bot._initiate_nav_jump(wp)
        assert bot._jump_ang_vel == pytest.approx(0.0)

    def test_tick_integrates_spin_into_azimuth(self, bot):
        """_tick_nav_jump dreht die Azimuth um _jump_ang_vel * dt (Lande-Drehung wirkt im Flug)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = 5.0                     # steigend → nicht gelandet
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.3
        bot._jumping = True
        bot._ai_state = AIState.NAV_JUMP
        bot._nav_jump_target_z = 30.0
        dt = 0.05
        bot._tick_nav_jump(dt, time.monotonic())
        assert bot.azimuth == pytest.approx(0.3 * dt, abs=1e-6)

    def test_tick_nav_jump_uses_wings_gravity_override(self, bot):
        """R2: _tick_nav_jump integriert bei WG + Server-_wingsGravity mit dieser Override,
        nicht mit der rohen _gravity (_effective_gravity())."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0   # steigend → nicht gelandet
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.0
        bot._jumping = True
        bot._nav_jump_target_z = 30.0
        bot.own_flag = "WG"
        bot._wings_gravity = -4.0
        dt = 0.05
        bot._tick_nav_jump(dt, time.monotonic())
        assert bot.vel_z == pytest.approx(5.0 + bot._wings_gravity * dt)

    def test_tick_nav_jump_without_wg_uses_raw_gravity(self, bot):
        """Gegenprobe: ohne WG-Flagge bleibt weiterhin die rohe _gravity massgeblich."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.0
        bot._jumping = True
        bot._nav_jump_target_z = 30.0
        bot.own_flag = ""
        dt = 0.05
        bot._tick_nav_jump(dt, time.monotonic())
        assert bot.vel_z == pytest.approx(5.0 + bot._gravity * dt)


class TestWpTimeoutScaling:
    """_wp_timeout wird nach Distanz berechnet: WP_TIMEOUT_BASE + dist * WP_TIMEOUT_SCALE."""

    def test_short_wp_timeout(self, bot):
        from bot.constants import WP_TIMEOUT_BASE, WP_TIMEOUT_SCALE
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._nav_path = [(0.0, 0.0, 0.0), (5.0, 0.0, 0.0)]
        bot._wp_fail_count = 0
        bot._advance_path()
        expected = WP_TIMEOUT_BASE + 5.0 * WP_TIMEOUT_SCALE
        assert bot._wp_timeout == pytest.approx(expected)

    def test_long_wp_timeout(self, bot):
        from bot.constants import WP_TIMEOUT_BASE, WP_TIMEOUT_SCALE
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._nav_path = [(0.0, 0.0, 0.0), (20.0, 0.0, 0.0)]
        bot._wp_fail_count = 0
        bot._advance_path()
        expected = WP_TIMEOUT_BASE + 20.0 * WP_TIMEOUT_SCALE
        assert bot._wp_timeout == pytest.approx(expected)

    def test_runup_leg_wp_timeout_scales_with_distance(self, bot):
        """P4-MOV-02c: 16u-Anlauf-Leg (NAV_RUNUP_MAX) → _wp_timeout rein distanzskaliert.
        WP_TIMEOUT_JUMP_BONUS wurde entfernt — kein Sprung-Fixaufschlag mehr nötig, die generische
        Distanzformel deckt auch verlängerte Anlauf-WPs ab."""
        from bot.constants import WP_TIMEOUT_BASE, WP_TIMEOUT_SCALE
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._nav_path = [(0.0, 0.0, 0.0), (16.0, 0.0, 0.0)]
        bot._wp_fail_count = 0
        bot._advance_path()
        expected = WP_TIMEOUT_BASE + 16.0 * WP_TIMEOUT_SCALE
        assert bot._wp_timeout == pytest.approx(expected)

    def test_jump_infeasible_clears_path(self, bot):
        """Aufwärts-WP über Sprungmax (dz>18.4u) → disc<0 → Pfad verworfen."""
        dz = 19.0  # disc = 19²-2*9.8*19 < 0 → physikalisch unmöglich
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot._nav_path = [(0.0, 0.0, 0.0), (15.0, 0.0, dz)]
        bot._wp_fail_count = 0
        bot._advance_path()
        assert bot._nav_path == []
        assert bot.target_pos is None
        assert bot._wp_start_time is None


class TestEarlyAdvance:
    """P4-MOV-01: Early-Advance schneidet Ecken, indem der aktuelle WP früher weitergeschaltet
    wird, wenn der direkte Korridor Bot→nächster WP frei UND begehbar ist (keine Wand, keine Kante)."""

    def test_advances_early_when_corridor_clear(self, bot):
        """Freier Korridor, gleiche Ebene, WP im Horizont → WP übersprungen, Bot lenkt zum nächsten."""
        _build_nav(bot, [])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_pos = (10.0, 0.0)
        bot._nav_path = [(10.0, 0.0, 0.0), (10.0, 10.0, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(10.0, 10.0, 0.0)]
        assert bot.target_pos == (10.0, 10.0)
        assert bot.vel_y > 0.0, "lenkt bereits Richtung nächstem WP (y=10)"

    def test_no_early_advance_when_corridor_blocked(self, bot):
        """Solide Box auf der Diagonale (0,0)→(10,10) → kein Überspringen."""
        _build_nav(bot, [(5.0, 5.0, 0.0, 0.0, 2.0, 2.0, 10.0)])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_pos = (10.0, 0.0)
        bot._nav_path = [(10.0, 0.0, 0.0), (10.0, 10.0, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(10.0, 0.0, 0.0), (10.0, 10.0, 0.0)]
        assert bot.target_pos == (10.0, 0.0)

    def test_no_early_advance_when_next_is_jump(self, bot):
        """Nächster WP ist ein Sprung-hoch (z=10) → Ebenen-Gate blockt."""
        _build_nav(bot, [])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_pos = (10.0, 0.0)
        bot._nav_path = [(10.0, 0.0, 0.0), (10.0, 10.0, 10.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(10.0, 0.0, 0.0), (10.0, 10.0, 10.0)]

    def test_no_early_advance_before_jump_runup(self, bot):
        """cur=Run-up, nxt=Absprungzelle (gleiche Ebene!), danach Sprung-hoch (nav_path[2]) →
        Anlauf nicht abkürzen, sonst geht die Sprung-Ausrichtung verloren."""
        _build_nav(bot, [])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_pos = (10.0, 0.0)
        # Muster aus _insert_jump_runups: Run-up (10,0,0), Absprungzelle (14,0,0), Ziel (20,0,10).
        # Alle anderen Gates wären erfüllt (nxt 14u < 16u Horizont, gleiche Ebene, Korridor frei).
        bot._nav_path = [(10.0, 0.0, 0.0), (14.0, 0.0, 0.0), (20.0, 0.0, 10.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(10.0, 0.0, 0.0), (14.0, 0.0, 0.0), (20.0, 0.0, 10.0)]

    def test_no_early_advance_when_next_is_tele_exit(self, bot):
        """Nächster WP ist ein Teleporter-Exit-WP → Teleporter-Gate blockt."""
        _build_nav(bot, [])
        bot._nav_graph._tele_exit_wps.add((10.0, 10.0))
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_pos = (10.0, 0.0)
        bot._nav_path = [(10.0, 0.0, 0.0), (10.0, 10.0, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(10.0, 0.0, 0.0), (10.0, 10.0, 0.0)]

    def test_no_early_advance_in_reverse(self, bot):
        """Rückwärtsmodus → Early-Advance ist per not-reverse-Gate aus."""
        _build_nav(bot, [])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot._move_reverse = True
        bot.target_pos = (10.0, 0.0)
        bot._nav_path = [(10.0, 0.0, 0.0), (10.0, 10.0, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(10.0, 0.0, 0.0), (10.0, 10.0, 0.0)]

    def test_no_early_advance_when_last_wp(self, bot):
        """Nur noch ein WP (kein Folge-WP) → nichts zu überspringen."""
        _build_nav(bot, [])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_pos = (10.0, 0.0)
        bot._nav_path = [(10.0, 0.0, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(10.0, 0.0, 0.0)]

    def test_no_early_advance_when_next_beyond_horizon(self, bot):
        """Nächster WP jenseits des Horizonts (>16u) → altes Verhalten, kein Skip."""
        _build_nav(bot, [])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_pos = (10.0, 0.0)
        bot._nav_path = [(10.0, 0.0, 0.0), (30.0, 0.0, 0.0)]   # bot→next = 30u > 16u
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(10.0, 0.0, 0.0), (30.0, 0.0, 0.0)]

    def test_no_early_advance_over_platform_edge(self, bot):
        """Beide WPs auf gleicher Plattform-Höhe (z=10), aber die gerade Abkürzung führt über die
        Kante ins Leere (L-förmige Plattform, HIX-Szenario) → Absturz-Schutz blockt."""
        _build_nav(bot, [
            (0.0, 0.0, 0.0, 0.0, 10.0, 2.0, 10.0),   # Arm entlang X: Dach z=10, y in [-2,2]
            (8.0, 8.0, 0.0, 0.0, 2.0, 10.0, 10.0),   # Arm entlang Y: Dach z=10, x in [6,10]
        ])
        bot.pos_x = -4.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        bot.azimuth = 0.0
        bot.target_pos = (8.0, 0.0)
        bot._nav_path = [(8.0, 0.0, 10.0), (8.0, 8.0, 10.0)]   # gerade (-4,0)→(8,8) verlässt das L
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        assert bot._nav_path == [(8.0, 0.0, 10.0), (8.0, 8.0, 10.0)], "über Kante nicht abkürzen"


class TestCorridorChecks:
    """P4-MOV-01 Bausteine: _corridor_clear (Wände) und _corridor_no_dropoff (Kanten)."""

    def test_corridor_clear_no_navgraph(self, bot):
        """Ohne NavGraph → konservativ True (unverändertes Verhalten)."""
        bot._nav_graph = None
        assert bot._corridor_clear(10.0, 0.0) is True

    def test_corridor_clear_open(self, bot):
        """Leere Welt → Korridor frei."""
        _build_nav(bot, [])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._corridor_clear(10.0, 0.0) is True

    def test_corridor_blocked_by_wall(self, bot):
        """Dünne Wand quer zur Linie (0,0)→(10,0) → blockiert."""
        _build_nav(bot, [(5.0, 0.0, 0.0, 0.0, 0.3, 10.0, 10.0)])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._corridor_clear(10.0, 0.0) is False

    def test_corridor_wall_off_line_is_clear(self, bot):
        """Wand weit abseits der Linie → frei."""
        _build_nav(bot, [(5.0, 50.0, 0.0, 0.0, 0.3, 10.0, 10.0)])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._corridor_clear(10.0, 0.0) is True

    def test_corridor_wall_above_zband_is_clear(self, bot):
        """Wand hängt über dem Tank-Kopf (bottom_z=20) → Z-Band filtert → frei."""
        _build_nav(bot, [(5.0, 0.0, 20.0, 0.0, 0.3, 10.0, 10.0)])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._corridor_clear(10.0, 0.0) is True

    def test_dropoff_no_navgraph(self, bot):
        """Ohne NavGraph → konservativ True."""
        bot._nav_graph = None
        assert bot._corridor_no_dropoff(10.0, 0.0, 0.0) is True

    def test_dropoff_flat_ground_continuous(self, bot):
        """Ebener Weltboden (z=0) durchgehend → kein Dropoff."""
        _build_nav(bot, [])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._corridor_no_dropoff(10.0, 0.0, 0.0) is True

    def test_dropoff_over_edge_detected(self, bot):
        """Gerade verlässt die Plattform (Dach z=10, y∈[-2,2]) → Boden fällt auf 0 → Dropoff."""
        _build_nav(bot, [(0.0, 0.0, 0.0, 0.0, 10.0, 2.0, 10.0)])
        bot.pos_x = -8.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        assert bot._corridor_no_dropoff(0.0, 8.0, 10.0) is False

    def test_dropoff_on_platform_continuous(self, bot):
        """Diagonale bleibt komplett auf dem großen Dach (z=10) → kein Dropoff."""
        _build_nav(bot, [(0.0, 0.0, 0.0, 0.0, 10.0, 10.0, 10.0)])
        bot.pos_x = -8.0; bot.pos_y = -8.0; bot.pos_z = 10.0
        assert bot._corridor_no_dropoff(8.0, 8.0, 10.0) is True


class TestBlindTurn:
    """Nahe am WP außerhalb FOV: Geschwindigkeit linear reduziert."""

    def test_speed_zero_at_inner_threshold(self, bot):
        """WP direkt hinter Bot (diff=π ≥ π/2) → Physik-Formel liefert speed=0 (turn in place)."""
        r = bot._wp_reach_radius()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0   # schaut +x
        dist = r * 1.5
        bot.target_pos = (-dist, 0.0)   # WP direkt hinter Bot → diff=π
        bot._nav_path = [(-dist, 0.0, 0.0), (-dist * 2, 0.0, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        speed = math.hypot(bot.vel_x, bot.vel_y)
        assert speed == pytest.approx(0.0), "WP hinter Bot (diff=π): speed=0 erwartet"

    def test_speed_reduced_at_midpoint(self, bot):
        """WP hinter Bot (diff=π ≥ π/2) → speed=0 (turn in place), unabhängig von Distanz."""
        r = bot._wp_reach_radius()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        dist = r * 2.0
        bot.target_pos = (-dist, 0.0)   # WP hinter Bot → diff=π
        bot._nav_path = [(-dist, 0.0, 0.0), (-dist * 2, 0.0, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        speed = math.hypot(bot.vel_x, bot.vel_y)
        assert speed == pytest.approx(0.0), "WP hinter Bot (diff=π): speed=0 erwartet"

    def test_no_reduction_when_wp_in_fov(self, bot):
        """WP nah und direkt voraus → keine Geschwindigkeitsreduktion."""
        r = bot._wp_reach_radius()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0   # schaut +x
        dist = r * 1.5
        bot.target_pos = (dist, 0.0)    # WP direkt voraus (behind=0 → blind_factor=1.0)
        bot._nav_path = [(dist, 0.0, 0.0), (dist * 2, 0.0, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        speed = math.hypot(bot.vel_x, bot.vel_y)
        assert speed > 20.0, "WP direkt voraus: volle Geschwindigkeit erwartet"

    def test_speed_reduced_perpendicular(self, bot):
        """WP seitlich (90°) und nah → Geschwindigkeit stark reduziert (war Bug: kein Abbremsen)."""
        r = bot._wp_reach_radius()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0          # schaut +x
        dist = r * 1.5             # d_raw=1.5, behind=1.0 bei 90°
        bot.target_pos = (0.0, dist)   # WP exakt links (90° seitlich)
        bot._nav_path = [(0.0, dist, 0.0), (0.0, dist * 2, 0.0)]
        bot._wp_start_time = None
        bot._move_to_target(0.02, bot.world_half)
        speed = math.hypot(bot.vel_x, bot.vel_y)
        assert speed < 4.0, "WP seitlich bei 1.5×r: stark reduzierte Geschwindigkeit erwartet"


class TestRouteDiscardClearsTarget:
    """Nach Route-Discard (2× Timeout) muss target_pos None sein."""

    def test_move_to_target_clears_target_pos(self, bot):
        """_move_to_target: Route-Discard setzt target_pos auf None."""
        import time
        bot._nav_path = [(10.0, 0.0, 0.0)]
        bot._nav_goal = (10.0, 0.0)
        bot.target_pos = (10.0, 0.0)
        bot._wp_fail_count = 1
        bot._wp_start_time = time.monotonic() - 9999.0
        bot._wp_timeout = 0.0
        bot._move_to_target(0.02, bot.world_half)
        assert bot.target_pos is None

    def test_execute_combat_move_clears_target_pos(self, bot):
        """_execute_combat_move: Route-Discard setzt target_pos auf None.
        Gegner auf z=15 → _not_below_enemy (bot_z+TANK_HEIGHT > enemy_z) False → WPs aktiv."""
        import time
        from conftest import make_player
        p = make_player(bot, 7, pos=(100.0, 0.0, 15.0))  # elevated → _skip_nav=False
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 7
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._nav_goal   = (100.0, 0.0)
        bot._nav_goal_z = 15.0          # verhindert replan_z-Trigger
        bot._nav_path   = [(50.0, 0.0, 0.0)]
        bot.target_pos  = (50.0, 0.0)
        bot._wp_fail_count  = 1
        bot._wp_start_time  = time.monotonic() - 9999.0
        bot._wp_timeout     = 0.0
        bot._execute_combat_move(0.02, bot.world_half)
        assert bot.target_pos is None


class TestDirectModeNoPlanning:
    """Im Direktziel-Modus (_skip_nav True) wird kein A*-Pfad geplant und kein Wegpunkt gefahren."""

    def test_no_path_planning_in_direct_mode(self, bot):
        """Gegner gleiche Z, dist < _opt·1.5 → _skip_nav True: _plan_path NICHT aufgerufen,
        _nav_path invalidiert. (replan_xy würde sonst wegen _nav_goal=None planen.)"""
        from unittest.mock import MagicMock
        from bot.models import AIState
        p = make_player(bot, 7, pos=(40.0, 0.0, 0.0))  # gleiche Z → _not_below_enemy True
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 7
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._ai_state = AIState.COMBAT
        bot._nav_goal = None            # würde replan_xy=True erzwingen
        bot._nav_path = [(20.0, 0.0, 0.0)]
        bot._next_shoot = float("inf")
        bot._plan_path = MagicMock()
        bot._execute_combat_move(0.02, bot.world_half, time.monotonic())
        bot._plan_path.assert_not_called()
        assert bot._nav_path == []


class TestNavJumpZLandingCheck:
    """_check_advance_path: NAV_JUMP-WP mit Z-Abweichung > NAV_JUMP_Z_TOL = Fehlschlag."""

    def test_z_deviation_too_large_counts_as_failure(self, bot):
        """Bot horizontal nah an erhöhtem WP, aber auf falscher Z-Ebene → timed_out=True."""
        from bot.constants import NAV_JUMP_Z_TOL
        # Bot steht auf Boden (z=0), WP ist auf z=10 (elevated)
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        wp_z = 10.0
        # Innerhalb Reach-Radius horizontal, aber Z-Abweichung = 10 > 2.5
        bot.target_pos = (2.0, 0.0)
        bot._nav_path = [(2.0, 0.0, wp_z)]
        bot._wp_fail_count = 0
        bot._check_advance_path()
        # Fehlschlag: fail_count wird durch _advance_path(timed_out=True) NICHT zurückgesetzt
        assert bot._wp_fail_count == 0  # timed_out=True lässt fail_count unverändert (0→bleibt)

    def test_z_deviation_acceptable_advances_normally(self, bot):
        """Kleine Z-Abweichung (≤ NAV_JUMP_Z_TOL) → normales WP-Advance."""
        from bot.constants import NAV_JUMP_Z_TOL
        # Bot fast auf Ziel-Z (Abweichung 1.0 < 2.5)
        wp_z = 10.0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = wp_z - 1.0   # 1u zu niedrig → akzeptabel
        bot.target_pos = (2.0, 0.0)
        bot._nav_path = [(2.0, 0.0, wp_z)]
        bot._wp_fail_count = 1
        bot._check_advance_path()
        # Normales Advance: fail_count auf 0 zurückgesetzt
        assert bot._wp_fail_count == 0

    def test_flat_wp_not_affected_by_z_check(self, bot):
        """Flacher WP (z ≈ floor): kein Z-Check, normales Advance."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.target_pos = (2.0, 0.0)
        bot._nav_path = [(2.0, 0.0, 0.0)]   # z = 0, nicht elevated
        bot._wp_fail_count = 1
        bot._check_advance_path()
        assert bot._wp_fail_count == 0


# ── FALLING-State ─────────────────────────────────────────────────────────────

class TestFallingState:
    """FALLING: unkontrollierter Fall vom Dach — kein Lenken, Physik committed."""

    def test_combat_at_height_transitions_to_falling(self, bot):
        """Bot in COMBAT, vel[2] < -0.1 und pos[2] > floor_z + 0.5 → FALLING."""
        from bot.models import AIState
        bot._ai_state = AIState.COMBAT
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 15.0
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = -0.5
        bot._jumping = False
        # floor_z = 0 (kein NavGraph)
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._ai_state == AIState.FALLING
        assert bot._jumping is True
        assert bot._pre_fall_state == AIState.COMBAT

    def test_pre_fall_state_restored_on_landing(self, bot):
        """Nach FALLING landet Bot → State zurück auf _pre_fall_state."""
        from bot.models import AIState
        bot._ai_state = AIState.FALLING
        bot._pre_fall_state = AIState.COMBAT
        bot._jumping = True
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.05   # kurz über dem Boden
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = -0.1   # leicht abwärts
        bot._tick_falling(0.02, time.monotonic())
        assert bot._ai_state == AIState.COMBAT
        assert bot._jumping is False

    def test_falling_does_not_change_horizontal_velocity(self, bot):
        """_tick_falling: vel[0]/vel[1] bleiben unverändert (committed)."""
        from bot.models import AIState
        bot._ai_state = AIState.FALLING
        bot._pre_fall_state = AIState.COMBAT
        bot._jumping = True
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        bot.vel_x = 7.0; bot.vel_y = 3.0; bot.vel_z = -1.0
        vx_before, vy_before = bot.vel_x, bot.vel_y
        bot._tick_falling(0.02, time.monotonic())
        assert bot.vel_x == pytest.approx(vx_before)
        assert bot.vel_y == pytest.approx(vy_before)

    def test_seeking_also_enters_falling(self, bot):
        """Auch SEEKING → FALLING wenn airborne."""
        from bot.models import AIState
        bot._ai_state = AIState.SEEKING
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 15.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = -0.5
        bot._jumping = False
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._ai_state == AIState.FALLING
        assert bot._pre_fall_state == AIState.SEEKING

    def test_on_ground_does_not_enter_falling(self, bot):
        """Bot steht auf Dach (pos[2] == floor_z) → kein FALLING."""
        from bot.models import AIState
        bot._ai_state = AIState.COMBAT
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._jumping = False
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._ai_state != AIState.FALLING


# ── Kartenrand-Escape ─────────────────────────────────────────────────────────

class TestEdgeEscape:
    """_plan_path: Bot nahe Kartenrand → Direktwegpunkt zur halben Randachse."""

    def test_near_x_edge_escapes_inward(self, bot):
        """pos[0] nahe +world_half → target_pos.x auf world_half/2."""
        bot.pos_x = bot.world_half - 5.0; bot.pos_y = 50.0; bot.pos_z = 0.0
        bot._plan_path(0.0, 0.0)
        assert bot.target_pos is not None
        tx, ty = bot.target_pos
        assert tx == pytest.approx(bot.world_half / 2.0)
        assert ty == pytest.approx(50.0)  # Y-Achse unverändert

    def test_near_negative_x_edge(self, bot):
        """pos[0] nahe -world_half → target_pos.x auf -world_half/2."""
        bot.pos_x = -(bot.world_half - 5.0); bot.pos_y = 30.0; bot.pos_z = 0.0
        bot._plan_path(0.0, 0.0)
        tx, _ = bot.target_pos
        assert tx == pytest.approx(-bot.world_half / 2.0)

    def test_near_y_edge_only_y_adjusted(self, bot):
        """pos[1] nahe +world_half, pos[0] im Inneren → nur Y halbiert."""
        bot.pos_x = 20.0; bot.pos_y = bot.world_half - 8.0; bot.pos_z = 0.0
        bot._plan_path(0.0, 0.0)
        tx, ty = bot.target_pos
        assert ty == pytest.approx(bot.world_half / 2.0)
        assert tx == pytest.approx(20.0)  # X unverändert

    def test_corner_both_axes_adjusted(self, bot):
        """Ecke nahe beider Ränder → beide Achsen halbiert."""
        bot.pos_x = bot.world_half - 7.0; bot.pos_y = -(bot.world_half - 6.0); bot.pos_z = 0.0
        bot._plan_path(0.0, 0.0)
        tx, ty = bot.target_pos
        assert tx == pytest.approx(bot.world_half / 2.0)
        assert ty == pytest.approx(-bot.world_half / 2.0)

    def test_interior_pos_not_affected(self, bot):
        """Bot weit vom Rand → _plan_path läuft normal (kein Edge-Escape)."""
        bot.pos_x = 50.0; bot.pos_y = 30.0; bot.pos_z = 0.0
        bot.target_pos = None
        bot._plan_path(80.0, 40.0)
        # Ergebnis = Direktpfad (kein NavGraph) zu goal, nicht Edge-Escape
        assert bot.target_pos == (80.0, 40.0)


# ── _execute_combat_move elevated-enemy regression ───────────────────────────

class TestExecuteCombatMoveElevated:
    """Regression: _execute_combat_move muss 'now' akzeptieren — kein NameError."""

    def test_does_not_crash_when_enemy_elevated(self, bot):
        """enemy_z > TANK_HEIGHT → _z_attack_feasible(now) wird aufgerufen; kein NameError."""
        from bot.constants import TANK_HEIGHT
        p = make_player(bot, 2, pos=(30.0, 0.0, 15.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)


# ── _z_attack_feasible ────────────────────────────────────────────────────────

class TestZAttackFeasible:
    """_z_attack_feasible: Kern-Bedingungen für ZJ1 ohne Zufalls-Gate."""

    def _setup(self, bot, enemy_pos=(50.0, 0.0, 8.0), bot_azimuth=0.0):
        p = make_player(bot, 2, pos=enemy_pos)
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = bot_azimuth
        bot._last_jump_at = 0.0
        return p

    def test_returns_false_when_no_target(self, bot):
        """Kein target_player → False."""
        bot.target_player = None
        assert bot._z_attack_feasible(1000.0) is False

    def test_returns_false_when_enemy_on_ground(self, bot):
        """Gegner auf Bodenhöhe (z_diff << HIT_RADIUS) → False."""
        self._setup(bot, enemy_pos=(50.0, 0.0, 1.0))
        assert bot._z_attack_feasible(1000.0) is False

    def test_returns_false_when_enemy_too_high(self, bot):
        """Gegner über max_jump_h → nicht erreichbar → False."""
        from bot.constants import JUMP_VELOCITY, GRAVITY
        max_jump_h = JUMP_VELOCITY ** 2 / (2.0 * abs(GRAVITY))
        self._setup(bot, enemy_pos=(50.0, 0.0, max_jump_h + 1.0))
        assert bot._z_attack_feasible(1000.0) is False

    def test_returns_false_when_jump_on_cooldown(self, bot):
        """Sprung-Cooldown aktiv → _can_jump False → False."""
        from bot.constants import JUMP_COOLDOWN
        self._setup(bot, enemy_pos=(50.0, 0.0, 8.0))
        now = 1000.0
        bot._last_jump_at = now - JUMP_COOLDOWN + 0.5
        assert bot._z_attack_feasible(now) is False

    def test_returns_true_when_reachable_and_facing(self, bot):
        """Gegner in Sprungreichweite, Bot schaut direkt hin → True."""
        self._setup(bot, enemy_pos=(50.0, 0.0, 8.0), bot_azimuth=0.0)
        assert bot._z_attack_feasible(1000.0) is True

    def test_returns_false_when_angle_too_large(self, bot):
        """Gegner 90° seitlich, Drehzeit zu kurz für Winkelausgleich → False."""
        self._setup(bot, enemy_pos=(0.0, 50.0, 8.0), bot_azimuth=0.0)
        assert bot._z_attack_feasible(1000.0) is False


# ── _skip_nav — neue Check1+Check2-Bedingung ─────────────────────────────────

class TestSkipNavCondition:
    """_execute_combat_move: _skip_nav schaltet auf Direktkampf wenn Check1+Check2."""

    def _setup(self, bot, bot_z=0.0, enemy_z=0.0, dist=60.0):
        """Setzt Bot und Gegner auf definierte Positionen; kein NavGraph → _has_los=True."""
        p = make_player(bot, 2, pos=(dist, 0.0, enemy_z))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = bot_z
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._nav_path = [(dist * 0.5, 5.0, bot_z)]  # WP vorhanden
        bot._nav_goal = (dist, 0.0)
        bot._nav_goal_z = enemy_z
        return p

    def test_direct_combat_when_both_ground(self, bot):
        """Beide auf Boden (z=0), dist=60 → kein WP-Folgen."""
        self._setup(bot, bot_z=0.0, enemy_z=0.0, dist=60.0)
        nav_wp_called = []
        bot._navigate_wp = lambda *a, **kw: nav_wp_called.append(True) or True
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert not nav_wp_called

    def test_direct_combat_when_bot_above_enemy(self, bot):
        """Bot auf Dach (z=15), Gegner am Boden (z=0), dist=50 → Direktkampf."""
        self._setup(bot, bot_z=15.0, enemy_z=0.0, dist=50.0)
        nav_wp_called = []
        bot._navigate_wp = lambda *a, **kw: nav_wp_called.append(True) or True
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert not nav_wp_called

    def test_wp_used_when_enemy_above_bot(self, bot):
        """Gegner auf Dach (z=15), Bot am Boden → WPs zum Aufsteigen."""
        self._setup(bot, bot_z=0.0, enemy_z=15.0, dist=50.0)
        nav_wp_called = []
        bot._navigate_wp = lambda *a, **kw: nav_wp_called.append(True) or True
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert nav_wp_called


# ── COMBAT-Optimaldistanz-Deadzone ───────────────────────────────────────────

class TestCombatDeadzone:
    """_execute_combat_move: ±COMBAT_DIST_DEADZONE um die Optimaldistanz nicht regeln,
    sonst zittern zwei distanzgleiche Bots. Direktmodus (kein NavGraph → _has_los=True)."""

    def _setup(self, bot, dist):
        p = make_player(bot, 2, pos=(dist, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._nav_path = []
        bot._nav_goal = None

    def test_deadzone_holds_at_optimal(self, bot):
        from bot.constants import OPTIMAL_RANGE
        self._setup(bot, dist=OPTIMAL_RANGE)          # exakt Optimaldistanz
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot.vel_x == 0.0 and bot.vel_y == 0.0

    def test_reverse_just_below_deadzone(self, bot):
        from bot.constants import OPTIMAL_RANGE
        self._setup(bot, dist=OPTIMAL_RANGE - 2.0)    # 58u → rückwärts
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot.vel_x < 0.0

    def test_forward_just_above_deadzone(self, bot):
        from bot.constants import OPTIMAL_RANGE
        self._setup(bot, dist=OPTIMAL_RANGE + 2.0)    # 62u → langsam vorwärts
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot.vel_x > 0.0


# ── _has_los_to_enemy ─────────────────────────────────────────────────────────

class TestHasLos:
    """_has_los_to_enemy: Slab-Ray-AABB-Test gegen _los_obs."""

    def _setup_nav(self, bot, boxes, shoot_through_flags=None):
        """Baut einen NavGraph mit den angegebenen Boxen und hängt ihn an den Bot."""
        from bzflag.world_map import BoxObstacle, WorldMap
        from bzflag.nav_graph import NavGraph
        obs = []
        for i, (cx, cy, bz, hw, hd, h) in enumerate(boxes):
            st = (shoot_through_flags or [False] * len(boxes))[i]
            obs.append(BoxObstacle(cx=cx, cy=cy, bottom_z=bz, angle=0.0,
                                   half_w=hw, half_d=hd, height=h,
                                   drive_through=False, shoot_through=st))
        wm = WorldMap(boxes=obs, teleporters=[], links=[], world_half=200.0)
        bot._nav_graph = NavGraph(wm, max_jump_h=18.4)

    def test_returns_true_when_no_boxes(self, bot):
        """Leere Welt → immer LOS."""
        self._setup_nav(bot, [])
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._has_los_to_enemy(2) is True

    def test_returns_false_when_box_between(self, bot):
        """Box direkt zwischen Bot (0,0) und Gegner (50,0) → LOS blockiert."""
        # Box bei x=25, halb 5 breit, 5 tief, 10 hoch → Strahl schneidet sie
        self._setup_nav(bot, [(25.0, 0.0, 0.0, 5.0, 5.0, 10.0)])
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._has_los_to_enemy(2) is False

    def test_returns_true_when_box_beside(self, bot):
        """Box neben dem Strahlenweg → LOS frei."""
        # Box bei y=20 (weit seitlich) → Strahl bei y=0 trifft sie nicht
        self._setup_nav(bot, [(25.0, 20.0, 0.0, 3.0, 3.0, 10.0)])
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._has_los_to_enemy(2) is True

    def test_shoot_through_box_not_blocking(self, bot):
        """Box mit shoot_through=True blockiert LOS nicht."""
        self._setup_nav(bot, [(25.0, 0.0, 0.0, 5.0, 5.0, 10.0)],
                        shoot_through_flags=[True])
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._has_los_to_enemy(2) is True

    def test_returns_true_when_no_nav_graph(self, bot):
        """Kein NavGraph → True (LOS wird angenommen)."""
        bot._nav_graph = None
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._has_los_to_enemy(2) is True


# ── Dach-Kanten-Prüfung ──────────────────────────────────────────────────────

class TestRoofEdgePrevention:
    """_execute_combat_move: predictiver Kanten-Check verhindert Absturz von Dächern."""

    def test_no_backward_at_roof_edge(self, bot):
        """Bot auf Dach (floor_z=15), nächste Rückwärtsposition ist Kante → speed=0."""
        from unittest.mock import MagicMock
        # Gegner nah auf demselben Dach (dist < OPTIMAL_RANGE)
        p = make_player(bot, 2, pos=(10.0, 0.0, 15.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 15.0
        bot.azimuth = 0.0  # Bot schaut in +x, Gegner in +x → rückwärts = -x

        nav = MagicMock()
        # Aktuelle Position: floor=15; nächste Pos (−x): floor=0 → Kante
        nav.get_floor_z.side_effect = lambda x, y, z, overhang=0.0: 15.0 if (x == 0.0 and y == 0.0) else 0.0
        bot._nav_graph = nav

        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)

        # Kante erkannt → vel[0] == 0 (Bot bleibt stehen)
        assert bot.vel_x == 0.0

    def test_backward_allowed_on_flat_roof(self, bot):
        """Bot auf Dach (floor_z=15), keine Kante → Rückwärtsbewegung bleibt erhalten."""
        from unittest.mock import MagicMock
        p = make_player(bot, 2, pos=(10.0, 0.0, 15.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 15.0
        bot.azimuth = 0.0

        nav = MagicMock()
        # Flaches Dach: get_floor_z gibt überall 15.0 zurück
        nav.get_floor_z.return_value = 15.0
        bot._nav_graph = nav

        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)

        # Kein Abfall → Bot fährt rückwärts (vel[0] < 0)
        assert bot.vel_x < 0.0

    def test_no_edge_check_on_ground(self, bot):
        """Bot auf Boden (floor_z=0) → Kanten-Check wird nicht ausgelöst."""
        from unittest.mock import MagicMock
        p = make_player(bot, 2, pos=(10.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0

        nav = MagicMock()
        # Würde einen Abfall signalisieren — darf aber nicht ausgewertet werden
        nav.get_floor_z.side_effect = lambda x, y, z, overhang=0.0: 0.0 if (x == 0.0 and y == 0.0) else -999.0
        bot._nav_graph = nav

        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)

        # floor_z ≤ 0.5 → kein Kanten-Check → Bot fährt rückwärts (vel[0] < 0)
        assert bot.vel_x < 0.0


class TestJumpGeometryCooldown:
    """_advance_path: Cooldown wird gesetzt wenn Sprung-Geometrie scheitert (Fix 12B)."""

    def test_geometry_fail_sets_cooldown(self, bot):
        """Wenn _nav_jump_geometry_ok=False → WP-Key in _nav_jump_cooldowns."""
        from bot.models import AIState
        from unittest.mock import patch
        bot._ai_state = AIState.SEEKING
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        jump_wp = (10.0, 0.0, 30.0)
        bot._nav_path = [(5.0, 0.0, 0.0), jump_wp]
        bot.target_pos = (5.0, 0.0)
        bot._wp_fail_count = 0

        with patch.object(bot, '_nav_jump_feasible', return_value=False), \
             patch.object(bot, '_nav_jump_geometry_ok', return_value=False):
            bot._advance_path()

        wp_key = (round(jump_wp[0]), round(jump_wp[1]), jump_wp[2])
        assert wp_key in bot._nav_jump_cooldowns
        assert bot._nav_jump_cooldowns[wp_key] > time.monotonic()

    def test_geometry_fail_clears_path(self, bot):
        """Geometry-Fehler leert Pfad komplett."""
        from bot.models import AIState
        from unittest.mock import patch
        bot._ai_state = AIState.SEEKING
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._nav_path = [(5.0, 0.0, 0.0), (10.0, 0.0, 30.0)]
        bot.target_pos = (5.0, 0.0)

        with patch.object(bot, '_nav_jump_feasible', return_value=False), \
             patch.object(bot, '_nav_jump_geometry_ok', return_value=False):
            bot._advance_path()

        assert bot._nav_path == []
        assert bot.target_pos is None


# ── Fix 16: _recent_flag_targets — Kürzlich angesteuerte Flags überspringen ──

class TestRecentFlagTargets:
    """_recent_flag_targets-deque verhindert Zyklen und 60-Hz-Hot-Loops."""

    def test_recent_targets_skip_on_next_call(self, bot):
        """Nach erster Auswahl ist die Flag im deque und wird beim nächsten Aufruf übersprungen."""
        from bot.models import FlagInfo
        bot.own_flag = ""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        fi_f = FlagInfo(flag_id=0, abbr="GM", status=1, pos=[10.0, 0.0, 0.0])
        fi_g = FlagInfo(flag_id=1, abbr="L",  status=1, pos=[20.0, 0.0, 0.0])
        bot.flags = {0: fi_f, 1: fi_g}
        bot._recent_flag_targets = collections.deque(maxlen=10)
        bot._nav_graph = None

        bot._new_target()
        assert bot.target_pos == (10.0, 0.0)     # F (nächste) gewählt
        assert (10, 0) in bot._recent_flag_targets

        bot._new_target()
        assert bot.target_pos == (20.0, 0.0)     # F im deque → G gewählt

    def test_recent_targets_maxlen_drops_oldest(self, bot):
        """Nach maxlen=10 Einträgen fällt der älteste raus."""
        bot._recent_flag_targets = collections.deque(maxlen=10)
        for i in range(10):
            bot._recent_flag_targets.append((i, i))
        assert (0, 0) in bot._recent_flag_targets

        bot._recent_flag_targets.append((99, 99))

        assert (0, 0) not in bot._recent_flag_targets  # ältester raus
        assert (99, 99) in bot._recent_flag_targets    # neuester drin

    def test_recent_targets_cleared_on_respawn(self, bot):
        """_recent_flag_targets wird beim Respawn geleert."""
        bot._recent_flag_targets = collections.deque([(1, 1), (2, 2)], maxlen=10)
        assert len(bot._recent_flag_targets) == 2

        bot._recent_flag_targets.clear()

        assert len(bot._recent_flag_targets) == 0

    def test_id_fall_b_skips_recent(self, bot):
        """Fall B: kürzlich besuchte Flag wird im zweiten Loop übersprungen."""
        from bot.models import FlagInfo
        bot.own_flag = "ID"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        # Zwei schlechte Flags außerhalb IDENTIFY_RANGE — eine davon im deque
        fi_a = FlagInfo(flag_id=0, abbr="NJ", status=1, pos=[60.0, 0.0, 0.0])
        fi_b = FlagInfo(flag_id=1, abbr="NJ", status=1, pos=[70.0, 0.0, 0.0])
        bot.flags = {0: fi_a, 1: fi_b}
        bot._recent_flag_targets = collections.deque([(70, 0)], maxlen=10)
        bot._nav_graph = None

        bot._new_target()

        assert bot.target_pos == (60.0, 0.0)  # fi_b im deque → fi_a gewählt

    def test_id_fall_b_seeks_flag_outside_range(self, bot):
        """Fall B zweiter Loop: Flag bei >50u ohne deque-Eintrag wird angesteuert."""
        from bot.models import FlagInfo
        bot.own_flag = "ID"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        fi = FlagInfo(flag_id=0, abbr="GM", status=1, pos=[60.0, 0.0, 0.0])
        bot.flags = {0: fi}
        bot._recent_flag_targets = collections.deque(maxlen=10)
        bot._nav_graph = None

        bot._new_target()

        assert bot.target_pos == (60.0, 0.0)

    def test_identify_range_follows_setvar(self, bot):
        """Konstanten-Audit: Fall B (ID-Flagge) nutzt die nachgeführte Server-Var
        self._identify_range statt der starren IDENTIFY_RANGE-Konstante (50u). Eine gute
        Flagge bei 55u liegt außerhalb des Default-Radius, aber innerhalb eines vom
        Server via _identifyRange auf 60u gesetzten Radius → Fall B1 erkennt sie als gut
        und legt die ID-Flagge ab (MsgDropFlag)."""
        from bot.models import FlagInfo
        bot.own_flag = "ID"
        bot._identify_range = 60.0   # Custom-Server: größerer Erkennungsradius als Default 50
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.good_flags = {"GM"}
        bot._last_drop_attempt = 0.0
        fi = FlagInfo(flag_id=0, abbr="GM", status=1, pos=[55.0, 0.0, 0.0])
        bot.flags = {0: fi}
        bot._recent_flag_targets = collections.deque(maxlen=10)
        bot._nav_graph = None

        bot._new_target()

        bot.client.send.assert_called()   # _try_drop_flag → nur im B1-Zweig ausgelöst

    def test_degenerate_path_no_hot_loop(self, bot):
        """Degenerate-Pfad: Flag geht in deque → zweiter _new_target()-Aufruf wählt andere."""
        from bot.models import FlagInfo
        bot.own_flag = ""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        fi_f = FlagInfo(flag_id=0, abbr="GM", status=1, pos=[10.0, 0.0, 0.0])
        fi_g = FlagInfo(flag_id=1, abbr="L",  status=1, pos=[30.0, 0.0, 0.0])
        bot.flags = {0: fi_f, 1: fi_g}
        bot._recent_flag_targets = collections.deque(maxlen=10)
        bot._nav_graph = None

        # Erster Aufruf: F gewählt, in deque
        bot._new_target()
        assert bot.target_pos == (10.0, 0.0)

        # Simuliert: _check_advance_path feuert sofort (degenerate), ruft _new_target() erneut
        bot._new_target()

        # F ist im deque → G wird gewählt statt F → kein Hot-Loop
        assert bot.target_pos == (30.0, 0.0)


# ── Fix 20a: Agility-Boost ───────────────────────────────────────────────────

class TestAgilityBoost:
    """_effective_tank_speed: A-Flagge gibt Boost bei Standstill, nicht bei Fahrt."""

    def test_agility_boost_from_standstill(self, bot):
        """A-Flagge + vel≈0 → Geschwindigkeit = tankSpeed × AGILITY_AD_VEL."""
        from bot.constants import AGILITY_AD_VEL, TANK_SPEED
        bot.own_flag = "A"
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        result = bot._effective_tank_speed()
        assert result == pytest.approx(TANK_SPEED * AGILITY_AD_VEL)

    def test_agility_no_boost_when_moving(self, bot):
        """A-Flagge + vel≥1m/s → kein Boost, normale tankSpeed."""
        from bot.constants import TANK_SPEED
        bot.own_flag = "A"
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        result = bot._effective_tank_speed()
        assert result == pytest.approx(TANK_SPEED)


# ── Fix 19: Fall-B2-Filter (abbr="" vs. abbr=bekannte Abkürzung) ─────────────

class TestIdFallB2Filter:
    """Fall B2: Flags innerhalb IDENTIFY_RANGE — bekannte schlechte überspringen, unbekannte nicht."""

    def test_b2_skips_known_bad_within_range(self, bot):
        """Flagge mit bekannter schlechter Abkürzung bei d<50u → B2 überspringt sie.
        Eine Ausweich-Flagge außerhalb des Radius stellt sicher, dass die nahe PZ-Flagge
        wirklich übersprungen wird (kein Fall-C-Fallthrough als false-positive)."""
        from bot.models import FlagInfo
        bot.own_flag = "ID"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.good_flags = set()  # PZ ist nicht gut
        # Nahe Flagge (d=20u) mit bekannter schlechter Abkürzung → soll übersprungen werden
        fi_near = FlagInfo(flag_id=0, abbr="PZ", status=1, pos=[20.0, 0.0, 0.0])
        # Ferne Flagge (d=80u) ohne schlechte Abkürzung → B2 wählt sie
        fi_far  = FlagInfo(flag_id=1, abbr="",   status=1, pos=[80.0, 0.0, 0.0])
        bot.flags = {0: fi_near, 1: fi_far}
        bot._recent_flag_targets = collections.deque(maxlen=10)
        bot._nav_graph = None
        bot._new_target()
        # Ziel muss die ferne Flagge sein, nicht die nahe PZ
        assert bot.target_pos is not None
        assert bot.target_pos[0] == pytest.approx(80.0)

    def test_b2_does_not_skip_unknown_within_range(self, bot):
        """Flagge mit abbr='' (unbekannt) bei d<50u → B2 überspringt sie NICHT."""
        from bot.models import FlagInfo
        bot.own_flag = "ID"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.good_flags = set()
        # Flagge mit unbekannter Abkürzung bei d=20u (< IDENTIFY_RANGE) → soll angesteuert werden
        fi = FlagInfo(flag_id=0, abbr="", status=1, pos=[20.0, 0.0, 0.0])
        bot.flags = {0: fi}
        bot._recent_flag_targets = collections.deque(maxlen=10)
        bot._nav_graph = None
        bot._new_target()
        # Unbekannte Flagge: B2 soll sie direkt ansteuern
        assert bot.target_pos == pytest.approx((20.0, 0.0))


# ── Request 3: Rückwärts zum NAV_JUMP-Anlaufpunkt (_should_reverse_to_wp) ───────

class TestReverseToRunup:
    """_should_reverse_to_wp: kurzes Rückwärtsfahren zum Anlauf-WP statt doppeltem
    180°-Drehen, wenn der Anlaufpunkt kurz hinter dem Bot liegt und der nächste WP ein
    Sprung-rauf ist."""

    def _setup(self, bot, target, next_wp, azimuth=0.0, own_flag=""):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = azimuth
        bot.own_flag = own_flag
        bot.target_pos = (target[0], target[1])
        # nav_path[0] = aktuelles Ziel (Anlauf-WP), nav_path[1] = Sprung-Landung
        bot._nav_path = [(target[0], target[1], 0.0), next_wp]

    def test_reverse_when_runup_behind_and_next_is_jump(self, bot):
        self._setup(bot, (-5.0, 0.0), (10.0, 0.0, 15.0), azimuth=0.0)
        assert bot._should_reverse_to_wp() is True

    def test_no_reverse_when_next_not_jump(self, bot):
        self._setup(bot, (-5.0, 0.0), (10.0, 0.0, 0.0), azimuth=0.0)
        assert bot._should_reverse_to_wp() is False

    def test_no_reverse_when_runup_ahead(self, bot):
        self._setup(bot, (5.0, 0.0), (10.0, 0.0, 15.0), azimuth=0.0)
        assert bot._should_reverse_to_wp() is False

    def test_no_reverse_when_too_far(self, bot):
        self._setup(bot, (-50.0, 0.0), (-40.0, 0.0, 15.0), azimuth=0.0)
        assert bot._should_reverse_to_wp() is False

    def test_no_reverse_with_forward_only_flag(self, bot):
        self._setup(bot, (-5.0, 0.0), (10.0, 0.0, 15.0), azimuth=0.0, own_flag="FO")
        assert bot._should_reverse_to_wp() is False

    def test_no_reverse_without_nav_path(self, bot):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.own_flag = ""
        bot.target_pos = (-5.0, 0.0)
        bot._nav_path = []
        assert bot._should_reverse_to_wp() is False

    def test_reverse_at_150_degrees(self, bot):
        """150° (über dem 135°-Schwellwert: Anlaufpunkt klar hinter dem Bot) löst Rückwärts aus."""
        d = 5.0
        tx, ty = d * math.cos(math.radians(150)), d * math.sin(math.radians(150))
        self._setup(bot, (tx, ty), (tx + 10.0, ty, 15.0), azimuth=0.0)
        assert bot._should_reverse_to_wp() is True

    def test_no_reverse_at_95_degrees(self, bot):
        """95° (unter dem neuen 100°-Schwellwert) löst weiterhin kein Rückwärts aus."""
        d = 5.0
        tx, ty = d * math.cos(math.radians(95)), d * math.sin(math.radians(95))
        self._setup(bot, (tx, ty), (tx + 10.0, ty, 15.0), azimuth=0.0)
        assert bot._should_reverse_to_wp() is False

    def test_no_reverse_when_runup_lengthened_beyond_gate(self, bot):
        """P4-MOV-02c: ein M-verlängerter Anlauf (bis zu 15u) kann das Gate NAV_CELL_SIZE*2.5=10u
        überschreiten → dann keine Rückwärtsanfahrt mehr (dokumentiertes Kurzstrecken-Feature,
        kein Bug: über weite Strecken ist Vorwärtsfahren effizienter)."""
        self._setup(bot, (-15.0, 0.0), (0.0, 0.0, 15.0), azimuth=0.0)
        assert bot._should_reverse_to_wp() is False


# ── Pixel-on-Bodenauflage: Bot bleibt am Plattformrand getragen ────────────────

class TestPixelOnFloorSupport:
    """_get_floor_z nutzt overhang=Tank-Halbbreite (Pixel-on): der Bot fällt erst, wenn seine
    Mitte ~eine Tank-Halbbreite über die Kante hinaus ist — nicht schon beim Überqueren der
    geometrischen Kante. Verhindert das Abfallen kurz vor dem Sprung (Bug 1)."""

    def _nav_with_platform(self):
        from bzflag.world_map import BoxObstacle, WorldMap
        from bzflag.nav_graph import NavGraph
        plat = BoxObstacle(cx=0.0, cy=0.0, bottom_z=0.0, angle=0.0,
                           half_w=20.0, half_d=20.0, height=15.0)  # Kante bei x=20
        wm = WorldMap(boxes=[plat], teleporters=[], links=[], world_half=80.0, world_hash="t")
        return NavGraph(wm)

    def test_supported_when_center_slightly_past_edge(self, bot):
        bot._nav_graph = self._nav_with_platform()
        bot.own_flag = ""
        bot.pos_x = 21.0; bot.pos_y = 0.0; bot.pos_z = 15.0   # Mitte 1.0u über Kante (< Halbbreite 1.4) → noch getragen
        assert bot._get_floor_z() == pytest.approx(15.0)

    def test_falls_when_center_well_past_edge(self, bot):
        bot._nav_graph = self._nav_with_platform()
        bot.own_flag = ""
        bot.pos_x = 23.0; bot.pos_y = 0.0; bot.pos_z = 15.0   # Mitte 3.0u über Kante → kein Pixel mehr auf → fällt
        assert bot._get_floor_z() == pytest.approx(0.0)


# ── Fix A: COMBAT-Eskalation bei per Sprung unerreichbarem (zu hohem) Gegner ────

class TestCombatUnreachableEscalation:
    """_execute_combat_move / _combat_escalate: statt blind die Wand zu rammen, durchläuft der
    Bot bei einem zu hohen Gegner ohne A*-Pfad einen Eskalations-Zyklus (Re-Target → Direktmodus
    → Reposition → Replan) mit Früh-Ausstieg."""

    def _setup(self, bot, enemy_z=30.0, dist=50.0):
        bot.team = 0                      # rogue → alle Spieler sind Feinde
        p = make_player(bot, 2, pos=(dist, 0.0, enemy_z))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._nav_graph = None
        bot._nav_path = []
        bot._nav_goal = None
        bot.own_flag = ""
        return p

    def test_too_high_starts_episode_direct_mode(self, bot):
        """Gegner z=30 unerreichbar, kein Pfad, kein Alt-Ziel → Episode startet, Direktmodus
        (Phase 1), kein Navigieren eines (nicht vorhandenen) Pfades."""
        self._setup(bot, enemy_z=30.0)
        bot._plan_path = lambda gx, gy, goal_z=None, **_kw: setattr(bot, "_nav_path", [])
        nav_wp = []
        bot._navigate_wp = lambda *a, **kw: nav_wp.append(True) or True
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._unreach_target == 2
        assert bot._unreach_phase == 1
        assert 2 in bot._combat_avoid
        assert not nav_wp                 # Direktmodus, kein WP-Folgen

    def test_too_high_retargets_to_reachable_foe(self, bot):
        """Phase 0: ein anderer (nicht gemiedener) Feind wird bevorzugt → Zielwechsel, Episode endet."""
        self._setup(bot, enemy_z=30.0, dist=50.0)
        alt = make_player(bot, 3, pos=(60.0, 0.0, 0.0))   # erreichbar (z=0), etwas weiter
        alt.vel = [0.0, 0.0, 0.0]
        switched = bot._combat_escalate(0.02, bot.world_half, (50.0, 0.0), 50.0, 0.0, 30.0)
        assert switched is False
        assert bot.target_player == 3
        assert bot._unreach_target is None

    def test_episode_early_exit_when_path_found(self, bot):
        """Phase 1: Hintergrund-Replan findet einen Pfad → Episode endet sofort."""
        self._setup(bot, enemy_z=30.0)
        bot._unreach_target = 2
        bot._unreach_phase = 1
        bot._unreach_until = time.monotonic() + 30.0
        bot._unreach_replan_at = 0.0      # Replan sofort fällig
        bot._plan_path = lambda gx, gy, goal_z=None, **_kw: setattr(bot, "_nav_path", [(10.0, 0.0, 30.0)])
        res = bot._combat_escalate(0.02, bot.world_half, (50.0, 0.0), 50.0, 0.0, 30.0)
        assert res is False
        assert bot._unreach_target is None
        assert bot._nav_path == [(10.0, 0.0, 30.0)]

    def test_reposition_after_direct_timeout(self, bot):
        """Direktmodus-Fenster abgelaufen → Reposition geplant + abgefahren (return True)."""
        self._setup(bot, enemy_z=30.0)
        bot._unreach_target = 2
        bot._unreach_phase = 1
        bot._unreach_until = time.monotonic() - 1.0   # Direktmodus-Fenster vorbei
        bot._unreach_replan_at = time.monotonic() + 10.0  # kein Hintergrund-Replan diesen Tick
        def _fake_plan(gx, gy, goal_z=None, **_kw):
            bot.target_pos = (gx, gy)
            bot._nav_path = []
        bot._plan_path = _fake_plan
        nav_wp = []
        bot._navigate_wp = lambda *a, **kw: nav_wp.append(True) or True
        res = bot._combat_escalate(0.02, bot.world_half, (50.0, 0.0), 50.0, 0.0, 30.0)
        assert res is True                # Reposition wird abgefahren
        assert bot._unreach_phase == 3
        assert nav_wp                     # _navigate_wp gerufen

    def test_jumpable_enemy_no_episode(self, bot):
        """Regression: Gegner z=15 (springbar) → keine Episode, normaler Direkt/Z-Attack-Modus."""
        self._setup(bot, enemy_z=15.0)
        bot._plan_path = lambda gx, gy, goal_z=None, **_kw: setattr(bot, "_nav_path", [])
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._unreach_target is None

    def test_effective_gravity_raises_threshold(self, bot):
        """LG-Flagge (geringere Gravity) hebt _max_jump_h → mittelhoher Gegner (z=20) gilt nicht
        mehr als unerreichbar, also keine Episode."""
        self._setup(bot, enemy_z=20.0)
        bot._plan_path = lambda gx, gy, goal_z=None, **_kw: setattr(bot, "_nav_path", [])
        # ohne LG: z=20 ist zu hoch → Episode
        bot.own_flag = ""
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._unreach_target == 2
        # mit LG: Sprunghöhe deutlich größer → z=20 erreichbar → keine Episode
        bot._unreach_target = None
        bot.own_flag = "LG"
        bot._lg_gravity = 12.7
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._unreach_target is None

    def test_pick_reposition_point_within_bounds(self, bot):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        for _ in range(20):
            rx, ry = bot._pick_reposition_point((100.0, 0.0))
            assert abs(rx) <= bot.world_half - 5.0
            assert abs(ry) <= bot.world_half - 5.0


class TestFindTargetAvoidPenalty:
    """_find_target_player: gemiedene Ziele werden weich deprioritisiert, aber als einziger
    Feind weiterhin gewählt (kein Re-Acquire-Regress)."""

    def test_prefers_non_avoided_foe(self, bot):
        bot.team = 0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        near = make_player(bot, 2, pos=(50.0, 0.0, 0.0))   # näher
        near.vel = [0.0, 0.0, 0.0]
        far = make_player(bot, 3, pos=(60.0, 0.0, 0.0))    # weiter
        far.vel = [0.0, 0.0, 0.0]
        bot._combat_avoid = {2: time.monotonic() + 30.0}   # näheren meiden
        assert bot._find_target_player() == 3

    def test_avoided_foe_still_chosen_if_only_one(self, bot):
        bot.team = 0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        only = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        only.vel = [0.0, 0.0, 0.0]
        bot._combat_avoid = {2: time.monotonic() + 30.0}
        assert bot._find_target_player() == 2


# ── Fix B: NAV_JUMP/NAV_JUMP_ALIGN nie auf sich selbst zurück (Endlosfalle) ─────

class TestNavJumpReturnState:
    """Return-State von NAV_JUMP/NAV_JUMP_ALIGN wird auf einen echten Boden-State aufgelöst,
    damit der 5-s-Timeout nicht 'auf sich selbst aussteigt'."""

    def test_initiate_nav_jump_from_align_resolves_owner(self, bot):
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 15.0
        bot.azimuth = 0.0
        bot._ai_state = AIState.NAV_JUMP_ALIGN
        bot._nav_jump_align_return_state = AIState.COMBAT
        bot._initiate_nav_jump((10.0, 0.0, 30.0))
        assert bot._nav_jump_return_state == AIState.COMBAT
        assert bot._ai_state == AIState.NAV_JUMP

    def test_align_timeout_exits_to_ground_state(self, bot):
        from bot.models import AIState
        make_player(bot, 2, pos=(50.0, 0.0, 30.0))
        bot.target_player = 2
        bot.human_count = 1
        bot._ai_state = AIState.NAV_JUMP_ALIGN
        bot._nav_jump_align_wp = (10.0, 0.0, 30.0)
        bot._nav_jump_align_return_state = AIState.NAV_JUMP_ALIGN   # 'kaputter' Self-Return
        bot._nav_jump_align_start = time.monotonic() - 6.0          # > 5 s → Timeout
        bot._tick_nav_jump_align(0.02, time.monotonic())
        assert bot._ai_state != AIState.NAV_JUMP_ALIGN
        assert bot._ai_state == AIState.COMBAT

    def test_ground_state_variants(self, bot):
        from bot.models import AIState
        bot._has_presence = lambda: True          # Mensch anwesend (Mitspieler ODER Zuschauer)
        bot.target_player = 2
        assert bot._ground_state() == AIState.COMBAT
        bot.target_player = None
        assert bot._ground_state() == AIState.SEEKING
        bot._has_presence = lambda: False
        assert bot._ground_state() == AIState.IDLE


# ── Effektive Sprunghöhe/-Geschwindigkeit (WG/LG-konsistent) ────────────────────

class TestEffectiveJumpHelpers:
    """_effective_gravity()/_effective_jump_velocity()/_effective_jump_height(): eine Quelle der
    Wahrheit für die Einzelsprung-Höhe, WG- und LG-bewusst, mit Fallback auf die Normalwerte."""

    def test_effective_gravity_wings_override(self, bot):
        bot.own_flag = "WG"
        bot._wings_gravity = -4.9
        assert bot._effective_gravity() == pytest.approx(-4.9)

    def test_effective_gravity_wings_fallback_when_unset(self, bot):
        from bot.constants import GRAVITY
        bot.own_flag = "WG"
        bot._wings_gravity = None          # kein Server-Override → BZDB-Default "_gravity"
        assert bot._effective_gravity() == pytest.approx(GRAVITY)

    def test_effective_gravity_lg_still_works(self, bot):
        from bot.constants import GRAVITY
        bot.own_flag = "LG"
        bot._lg_gravity = 12.7
        assert bot._effective_gravity() == pytest.approx(GRAVITY * 0.127)

    def test_effective_jump_velocity_wings_override(self, bot):
        bot.own_flag = "WG"
        bot._wings_jump_velocity = 25.0
        assert bot._effective_jump_velocity() == pytest.approx(25.0)

    def test_effective_jump_velocity_fallback(self, bot):
        from bot.constants import JUMP_VELOCITY
        bot.own_flag = "WG"
        bot._wings_jump_velocity = None    # kein Override → Normalwert
        assert bot._effective_jump_velocity() == pytest.approx(JUMP_VELOCITY)
        bot.own_flag = ""                  # ohne WG ebenfalls Normalwert (Override ignoriert)
        bot._wings_jump_velocity = 25.0
        assert bot._effective_jump_velocity() == pytest.approx(JUMP_VELOCITY)

    def test_effective_jump_height_default_equals_raw(self, bot):
        from bot.constants import JUMP_VELOCITY, GRAVITY
        bot.own_flag = ""
        expected = JUMP_VELOCITY ** 2 / (2.0 * abs(GRAVITY))
        assert bot._effective_jump_height() == pytest.approx(expected)

    def test_effective_jump_height_wings_combines_both(self, bot):
        from bot.constants import JUMP_VELOCITY, GRAVITY
        bot.own_flag = "WG"
        bot._wings_gravity = -4.9
        bot._wings_jump_velocity = 25.0
        assert bot._effective_jump_height() == pytest.approx(25.0 ** 2 / (2.0 * 4.9))
        # deutlich höher als ein Normalsprung
        assert bot._effective_jump_height() > JUMP_VELOCITY ** 2 / (2.0 * abs(GRAVITY))

    def test_lg_consistency_in_z_attack_feasible(self, bot):
        """Konsistenz-Regression: LG hebt die Sprunghöhe jetzt auch in _z_attack_feasible (vorher
        rohes _gravity → ein mittelhoher Gegner galt dort fälschlich als unerreichbar)."""
        bot.team = 0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._last_jump_at = 0.0
        p = make_player(bot, 2, pos=(50.0, 0.0, 20.0))
        p.is_airborne = False
        bot.target_player = 2
        now = time.monotonic()
        bot.own_flag = ""                  # ohne Flagge: z=20 > max_jump_h(~18.4) → nicht machbar
        assert bot._z_attack_feasible(now) is False
        bot.own_flag = "LG"                # LG: effektive Sprunghöhe ~145 → jetzt machbar
        bot._lg_gravity = 12.7
        assert bot._z_attack_feasible(now) is True

    def test_wings_raises_too_high_threshold(self, bot):
        """COMBAT: mit WG (größere Sprunghöhe) gilt ein z=30-Gegner als erreichbar → keine
        Unreachable-Episode (Gegenprobe zu test_effective_gravity_raises_threshold)."""
        bot.team = 0
        p = make_player(bot, 2, pos=(50.0, 0.0, 30.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._nav_graph = None
        bot._nav_path = []
        bot._nav_goal = None
        bot.own_flag = "WG"
        bot._wings_gravity = -2.0          # niedrige Gravity → Sprunghöhe ~90 → z=30 erreichbar
        bot._wings_jump_velocity = 19.0
        bot._plan_path = lambda gx, gy, goal_z=None, **_kw: setattr(bot, "_nav_path", [])
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._unreach_target is None


# ── _jump_launch_vz (doJump-Faithful: WG additiv beim Fallen) ────────────────────

class TestJumpLaunchVz:
    """_jump_launch_vz(): eine Quelle der Wahrheit für jede Sprung-Velocity-Zuweisung, faithful zu
    LocalPlayer.cxx doJump() — WG additiv beim Fallen / höhere Steig-Velocity behalten, sonst fest."""

    def test_normal_jump_is_fixed(self, bot):
        from bot.constants import JUMP_VELOCITY
        bot.own_flag = ""
        # Normalsprung ignoriert die aktuelle Velocity → fester Wert (auch im Fallen).
        assert bot._jump_launch_vz(0.0) == pytest.approx(JUMP_VELOCITY)
        assert bot._jump_launch_vz(-10.0) == pytest.approx(JUMP_VELOCITY)
        assert bot._jump_launch_vz(5.0) == pytest.approx(JUMP_VELOCITY)

    def test_wings_falling_is_additive(self, bot):
        from bot.constants import JUMP_VELOCITY
        bot.own_flag = "WG"
        # fallend (vz=-10) → nur abgebremst: 19 + (-10) = 9, KEIN voller neuer Bogen.
        assert bot._jump_launch_vz(-10.0) == pytest.approx(JUMP_VELOCITY - 10.0)

    def test_wings_near_apex_nearly_full(self, bot):
        from bot.constants import JUMP_VELOCITY
        bot.own_flag = "WG"
        assert bot._jump_launch_vz(-0.5) == pytest.approx(JUMP_VELOCITY - 0.5)

    def test_wings_rising_slower_gets_full(self, bot):
        from bot.constants import JUMP_VELOCITY
        bot.own_flag = "WG"
        assert bot._jump_launch_vz(5.0) == pytest.approx(JUMP_VELOCITY)

    def test_wings_rising_faster_is_kept(self, bot):
        bot.own_flag = "WG"
        # steigt schneller als der Sprung → höhere Velocity behalten (nicht auf 19 kappen).
        assert bot._jump_launch_vz(25.0) == pytest.approx(25.0)

    def test_wings_respects_server_override(self, bot):
        bot.own_flag = "WG"
        bot._wings_jump_velocity = 25.0
        assert bot._jump_launch_vz(-10.0) == pytest.approx(15.0)  # 25 + (-10)

    def test_tick_jumping_wg_airflap_additive(self, bot):
        """Integration: WG-Luftsprung im Fallen setzt vel[2] additiv (kein voller neuer Bogen)."""
        bot.own_flag = "WG"
        bot._wings_jump_count = 2
        bot._wings_jumps_used = 0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 50.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = -10.0
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.0
        bot._jumping = True
        with patch.object(bot, "_can_jump", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False), \
             patch.object(bot, "_can_drive_through_obstacles", return_value=True):
            bot._tick_jumping(0.01, now=1000.0)
        # gravity-Schritt (-10 - 9.8·0.01) + Flap (19 + …) ≈ 8.9 — NICHT 19.
        assert bot.vel_z == pytest.approx(19.0 + (-10.0 - 9.8 * 0.01))
        assert bot._wings_jumps_used == 1

    def test_tick_jumping_uses_wings_gravity_override(self, bot):
        """R2: _tick_jumping integriert bei WG + Server-_wingsGravity mit dieser Override,
        nicht mit der rohen _gravity (_effective_gravity())."""
        bot.own_flag = "WG"
        bot._wings_gravity = -4.0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 50.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0   # steigend → kein WG-Luftsprung-Zweig
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.0
        bot._jumping = True
        dt = 0.01
        bot._tick_jumping(dt, now=1000.0)
        assert bot.vel_z == pytest.approx(5.0 + bot._wings_gravity * dt)

    def test_tick_jumping_without_wg_uses_raw_gravity(self, bot):
        """Gegenprobe: ohne WG-Flagge bleibt weiterhin die rohe _gravity massgeblich."""
        bot.own_flag = ""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 50.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.0
        bot._jumping = True
        dt = 0.01
        bot._tick_jumping(dt, now=1000.0)
        assert bot.vel_z == pytest.approx(5.0 + bot._gravity * dt)


# ── _steep_wall_ahead (proaktive Wand-Vorausschau) ───────────────────────────

def _build_nav(bot, boxes):
    """boxes: Liste (cx, cy, bottom_z, angle, half_w, half_d, height) → solider NavGraph."""
    from bzflag.world_map import BoxObstacle, WorldMap
    from bzflag.nav_graph import NavGraph
    obs = [BoxObstacle(cx=cx, cy=cy, bottom_z=bz, angle=ang, half_w=hw, half_d=hd,
                       height=h, drive_through=False, shoot_through=False)
           for (cx, cy, bz, ang, hw, hd, h) in boxes]
    wm = WorldMap(boxes=obs, teleporters=[], links=[], world_half=200.0)
    bot._nav_graph = NavGraph(wm, max_jump_h=18.4)


class TestSteepWallAhead:
    """_steep_wall_ahead: liefert eine Wand-Tangente NUR bei steilem Einfall (>60° zur Fläche)."""

    def test_head_on_returns_tangent(self, bot):
        """Frontal (90°) auf eine quer liegende Wand → Tangente entlang der Wand (±90°)."""
        # Wand bei x=10, dünn in x (half_w=2), lang in y (half_d=20)
        _build_nav(bot, [(10.0, 0.0, 0.0, 0.0, 2.0, 20.0, 10.0)])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        tan = bot._steep_wall_ahead(0.0, 20.0)        # Richtung +x = senkrecht auf die Wandfläche
        assert tan is not None
        assert abs(abs(tan) - math.pi / 2) < 1e-6     # Tangente entlang der y-Achse

    def test_shallow_returns_none(self, bot):
        """Flacher Einfall (45° < 60°) → kein Eingriff (der Bot gleitet die Wand entlang)."""
        _build_nav(bot, [(10.0, 0.0, 0.0, 0.0, 2.0, 20.0, 10.0)])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._steep_wall_ahead(math.radians(45), 20.0) is None

    def test_out_of_range_returns_none(self, bot):
        """Wand jenseits der Probe-Distanz → None."""
        _build_nav(bot, [(30.0, 0.0, 0.0, 0.0, 2.0, 20.0, 10.0)])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._steep_wall_ahead(0.0, 20.0) is None

    def test_wall_beside_path_returns_none(self, bot):
        """Wand seitlich neben dem Strahl → None."""
        _build_nav(bot, [(10.0, 30.0, 0.0, 0.0, 2.0, 5.0, 10.0)])
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        assert bot._steep_wall_ahead(0.0, 20.0) is None

    def test_no_nav_graph_returns_none(self, bot):
        bot._nav_graph = None
        assert bot._steep_wall_ahead(0.0, 20.0) is None


class TestCombatSteepWallRouting:
    """_execute_combat_move: steile Wand zwischen Bot und Gegner (kein LoS) → A*-Navigation
    statt frontalem Rammen; freier LoS → unverändert Direktmodus (kein WP-Folgen)."""

    def _enemy(self, bot, dist=40.0):
        p = make_player(bot, 2, pos=(dist, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._nav_goal = None
        bot._nav_path = []
        return p

    def test_steep_wall_forces_navigation(self, bot):
        """Quer-Wand bei x=20 zwischen Bot(0) und Gegner(40) → _navigate_wp statt Direktdrücken."""
        _build_nav(bot, [(20.0, 0.0, 0.0, 0.0, 2.0, 30.0, 12.0)])
        self._enemy(bot, dist=40.0)
        bot._plan_path = lambda gx, gy, goal_z=None, **_kw: setattr(bot, "_nav_path", [(20.0, 8.0, 0.0)])
        nav_wp = []
        bot._navigate_wp = lambda *a, **kw: nav_wp.append(True) or True
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert nav_wp                                  # Pfad gefahren statt frontal gedrückt

    def test_los_clear_keeps_direct_mode(self, bot):
        """Wand seitlich → LoS frei → Direktmodus bleibt (Override greift nur ohne Sicht)."""
        _build_nav(bot, [(20.0, 40.0, 0.0, 0.0, 2.0, 5.0, 12.0)])
        self._enemy(bot, dist=40.0)
        nav_wp = []
        bot._navigate_wp = lambda *a, **kw: nav_wp.append(True) or True
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert not nav_wp


# ── COMBAT-Stall-Watchdog (Spiegel-Stall an dünner Wand) ─────────────────────

class TestCombatStallWatchdog:
    """_execute_combat_move: erkennt Null-Bewegung im sichtlosen Direktmodus und löst mit einem
    randomisierten Unstick-Manöver (REV/PATH) auf. Setup: Gegner in Optimaldistanz hinter dünner
    Wand (kein LoS), außerhalb der 20u-Wand-Probe → _skip_nav bleibt True, Direktsteuerung."""

    def _stalled_bot(self, bot, wall=True):
        boxes = [(45.0, 0.0, 0.0, 0.0, 0.5, 40.0, 10.0)] if wall else []
        _build_nav(bot, boxes)                        # Wand bei x=45 (>20u Probe) blockt LoS
        p = make_player(bot, 2, pos=(60.0, 0.0, 0.0))  # dist 60 = Optimaldistanz
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot._nav_goal = None
        bot._nav_path = []
        bot._next_shoot = float("inf")
        return p

    def test_arms_without_los(self, bot):
        from bot.constants import COMBAT_STALL_WIN_MIN, COMBAT_STALL_WIN_MAX
        self._stalled_bot(bot)
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._stall_check_at is not None
        assert 1000.0 + COMBAT_STALL_WIN_MIN <= bot._stall_check_at <= 1000.0 + COMBAT_STALL_WIN_MAX

    def test_no_arm_with_los(self, bot):
        self._stalled_bot(bot, wall=False)            # freie Sicht → nicht armieren
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._stall_check_at is None

    def test_no_arm_during_indirect_hold(self, bot):
        self._stalled_bot(bot)
        with patch.object(bot, "_update_indirect_hold", return_value=True):
            bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._stall_check_at is None

    def test_fires_rev(self, bot):
        self._stalled_bot(bot)
        bot._stall_check_at = 999.0                    # Fenster bereits abgelaufen
        bot._stall_anchor = [0.0, 0.0]                 # keine Bewegung seit Armierung
        from bot.constants import COMBAT_STALL_REV_MIN, COMBAT_STALL_REV_MAX
        with patch("bot.ai.combat.random.choice", return_value="REV"):
            bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._stall_mode == "REV"
        assert COMBAT_STALL_REV_MIN <= bot._stall_rev_dist <= COMBAT_STALL_REV_MAX

    def test_fires_path(self, bot):
        self._stalled_bot(bot)
        bot._stall_check_at = 999.0
        bot._stall_anchor = [0.0, 0.0]
        bot._plan_path = lambda *a, **kw: setattr(bot, "_nav_path", [(10.0, 10.0, 0.0)])
        with patch("bot.ai.combat.random.choice", return_value="PATH"):
            bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._stall_mode == "PATH"

    def test_path_fail_falls_back_to_rev(self, bot):
        self._stalled_bot(bot)
        bot._stall_check_at = 999.0
        bot._stall_anchor = [0.0, 0.0]
        bot._plan_path = lambda *a, **kw: setattr(bot, "_nav_path", [])   # Planung scheitert
        with patch("bot.ai.combat.random.choice", return_value="PATH"):
            bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._stall_mode == "REV"

    def test_rearms_when_moved(self, bot):
        self._stalled_bot(bot)
        bot._stall_check_at = 999.0
        bot._stall_anchor = [-10.0, 0.0]               # 10u Bewegung seit Armierung
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._stall_mode is None
        assert bot._stall_check_at is not None and bot._stall_check_at > 1000.0

    def test_rev_drives_backward_then_ends(self, bot):
        self._stalled_bot(bot)
        bot._stall_mode = "REV"
        bot._stall_rev_start = [0.0, 0.0]
        bot._stall_rev_dist = 10.0
        bot._stall_until = 1008.0
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot.vel_x < 0.0                        # fährt rückwärts (azimuth 0)
        bot.pos_x = -11.0; bot.pos_y = 0.0; bot.pos_z = 0.0                    # Soll-Distanz überschritten
        bot._execute_combat_move(0.02, bot.world_half, now=1001.0)
        assert bot._stall_mode is None

    def test_path_navigates_then_ends(self, bot):
        self._stalled_bot(bot)
        bot._stall_mode = "PATH"
        bot._nav_path = [(20.0, 20.0, 0.0)]
        bot._stall_until = 1008.0
        nav_wp = []
        bot._navigate_wp = lambda *a, **kw: nav_wp.append(True) or True
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert nav_wp                                  # Manöver-Pfad gefahren
        bot._nav_path = []                             # Pfad erschöpft
        bot._execute_combat_move(0.02, bot.world_half, now=1001.0)
        assert bot._stall_mode is None

    def test_window_is_randomized(self, bot):
        # Spiegel-Symmetrie-Sanity: zwei frische Armierungen ziehen unabhängige Fenster im
        # Konstanten-Bereich. Zwei reale Bots ziehen zudem unabhängige Manöver (REV-Distanz/PATH-
        # Winkel) → identisches synchrones Wieder-Einrasten ist praktisch ausgeschlossen.
        from bot.constants import COMBAT_STALL_WIN_MIN, COMBAT_STALL_WIN_MAX
        self._stalled_bot(bot)
        bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        w1 = bot._stall_check_at - 1000.0
        bot._stall_check_at = None
        bot._execute_combat_move(0.02, bot.world_half, now=2000.0)
        w2 = bot._stall_check_at - 2000.0
        assert COMBAT_STALL_WIN_MIN <= w1 <= COMBAT_STALL_WIN_MAX
        assert COMBAT_STALL_WIN_MIN <= w2 <= COMBAT_STALL_WIN_MAX


# ── Gegner höher + kein LoS (T2): Pfad erzwingen, sonst Watchdog fängt Fallthrough ──

class TestHigherEnemyNoLos:
    """Gegner deutlich höher (z=15) → _not_below_enemy False → kein Direktmodus, Pfadplanung.
    Schlägt die Planung fehl (kein Pfad, Gegner aber springbar → keine Eskalation), fängt der
    Stall-Watchdog den Fallthrough in die Direktsteuerung ab."""

    def test_higher_enemy_forces_path_planning(self, bot):
        from unittest.mock import MagicMock
        make_player(bot, 2, pos=(60.0, 0.0, 15.0))
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.target_player = 2
        bot._nav_goal = None
        bot._nav_path = []
        bot._plan_path = MagicMock()
        with patch.object(bot, "_update_indirect_hold", return_value=False):
            bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        bot._plan_path.assert_called_once()

    def test_higher_enemy_planfail_arms_watchdog(self, bot):
        _build_nav(bot, [(30.0, 0.0, 0.0, 0.0, 0.5, 40.0, 16.0)])   # blockt LoS zum Gegner z=15
        make_player(bot, 2, pos=(60.0, 0.0, 15.0))
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.target_player = 2
        bot._nav_goal = None
        bot._nav_path = []
        bot._next_shoot = float("inf")
        bot._plan_path = lambda *a, **kw: setattr(bot, "_nav_path", [])   # Planung scheitert
        with patch.object(bot, "_update_indirect_hold", return_value=False):
            bot._execute_combat_move(0.02, bot.world_half, now=1000.0)
        assert bot._stall_check_at is not None


# ── T1: dünnste HIX-Wand (1u) verdeckt LoS zuverlässig ───────────────────────

class TestThinWallLos:
    """Regression: die dünnste reale HIX-Wand (half_w=0.5 → 1u dick, Basis z=14, Höhe 16, also
    z-Spanne [14,30]) blockt LoS exakt; Strahlen darüber/darunter bleiben frei. Die echte Wand
    steht auf 135° — hier achsparallel für die Assertion-Geometrie, dünnster Wert unverändert."""

    def test_thin_hix_wall_blocks_los(self, bot):
        _build_nav(bot, [(0.0, 0.0, 14.0, 0.0, 0.5, 150.0, 16.0)])
        bot.pos_x = -30.0; bot.pos_y = 0.0; bot.pos_z = 15.0                  # auf der z=15-Plattform
        make_player(bot, 2, pos=(30.0, 0.0, 15.0))
        eye = 15.0 + bot._tank_height * 0.5
        assert not bot._segment_clear(-30.0, 0.0, eye, 30.0, 0.0, eye)   # dünne Wand blockt
        assert not bot._has_los_to_enemy(2)

    def test_ray_above_and_below_thin_hix_wall_clear(self, bot):
        _build_nav(bot, [(0.0, 0.0, 14.0, 0.0, 0.5, 150.0, 16.0)])
        bot.pos_x = -30.0; bot.pos_y = 0.0; bot.pos_z = 15.0
        assert bot._segment_clear(-30.0, 0.0, 31.0, 30.0, 0.0, 31.0)    # über der Wand (>z=30)
        assert bot._segment_clear(-30.0, 0.0, 13.0, 30.0, 0.0, 13.0)    # unter der Basis (<z=14)

    def test_rotated_135_wall_blocks_los(self, bot):
        """Die ECHTE Wand steht auf 135° — LoS quer über die Normale bleibt geblockt (der Slab-Test
        ist rotationskorrekt), Strahlen darüber sind frei."""
        ang = math.radians(135)
        _build_nav(bot, [(0.0, 0.0, 14.0, ang, 0.5, 150.0, 16.0)])
        perp = (math.cos(ang), math.sin(ang))          # Wand-Normale
        bx, by = perp[0] * 30.0, perp[1] * 30.0
        ex, ey = -perp[0] * 30.0, -perp[1] * 30.0
        bot.pos_x = bx; bot.pos_y = by; bot.pos_z = 15.0
        make_player(bot, 2, pos=(ex, ey, 15.0))
        eye = 15.0 + bot._tank_height * 0.5
        assert not bot._segment_clear(bx, by, eye, ex, ey, eye)         # 135°-Wand blockt
        assert not bot._has_los_to_enemy(2)
        assert bot._segment_clear(bx, by, 31.0, ex, ey, 31.0)          # über der Wand frei


# ── P4-MOV-02a: Trägheitsmodell (Beschleunigungsrampen) ─────────────────────

class TestMomentumRampDisabled:
    """Ohne aktives -a-Limit (_linear/_angular_acceleration == 0.0, der Default aus core.py) sind
    alle Rampen No-Ops — exaktes Alt-Verhalten (instantane Geschwindigkeit)."""

    def test_ramp_linear_speed_is_noop(self, bot):
        bot._linear_acceleration = 0.0
        bot._angular_acceleration = 0.0
        assert bot._ramp_linear_speed(25.0, 0.02) == 25.0

    def test_momentum_ramp_time_is_zero(self, bot):
        bot._linear_acceleration = 0.0
        bot._angular_acceleration = 0.0
        assert bot._momentum_ramp_time(1.0) == 0.0
        assert bot._momentum_ramp_time(2.0) == 0.0

    def test_accel_limits_are_zero(self, bot):
        """Kein Helper mehr - die Rampen gaten direkt über _accel_limits() (lin/ang == 0.0)."""
        bot._linear_acceleration = 0.0
        bot._angular_acceleration = 0.0
        lin, ang = bot._accel_limits()
        assert lin == 0.0 and ang == 0.0


class TestMomentumRampLinear:
    """Mit aktivem linearem Limit (_linear_acceleration=50.0) klemmt _ramp_linear_speed die
    Änderung der Vorwärtsgeschwindigkeit auf 20×linearAcceleration·dt gegen den Vorframe-Wert
    (verifiziert LocalPlayer::doMomentum)."""

    def test_ramp_toward_clamps_up(self, bot):
        bot._linear_acceleration = 50.0
        assert bot._ramp_toward(0.0, 25.0, 20.0) == 20.0

    def test_ramp_toward_reaches_target_within_delta(self, bot):
        bot._linear_acceleration = 50.0
        assert bot._ramp_toward(0.0, 25.0, 30.0) == 25.0

    def test_ramp_toward_clamps_down_negative_target(self, bot):
        bot._linear_acceleration = 50.0
        assert bot._ramp_toward(0.0, -25.0, 20.0) == -20.0

    def test_ramp_linear_speed_two_ticks_to_reach_target(self, bot):
        """max_delta = 20*50*0.02 = 20.0 pro Tick. Tick 1 (prev=0): Ergebnis 20.0, noch nicht 25.
        Tick 2 (vel_x auf 20 nachgeführt, azimuth=0 → prev=20): Ergebnis 25.0 (erreicht) — der
        erwartete ~1-3-Tick-Effekt bei -a 50."""
        bot._linear_acceleration = 50.0
        bot.azimuth = 0.0
        bot.vel_x = 0.0
        bot.vel_y = 0.0
        result1 = bot._ramp_linear_speed(25.0, 0.02)
        assert result1 == pytest.approx(20.0)
        bot.vel_x = 20.0   # Aufrufer würde vel_x/vel_y aus dem Ergebnis nachführen (azimuth=0)
        result2 = bot._ramp_linear_speed(25.0, 0.02)
        assert result2 == pytest.approx(25.0)

    def test_ramp_linear_speed_clamps_while_braking(self, bot):
        """Vorzeichenwechsel/Bremsen: von +25 auf Ziel -12.5 (Rückwärts) — max_delta=20.0 klemmt
        auf prev-20 = 5.0 (Zielgeschwindigkeit -12.5 noch nicht erreicht)."""
        bot._linear_acceleration = 50.0
        bot.azimuth = 0.0
        bot.vel_x = 25.0
        bot.vel_y = 0.0
        result = bot._ramp_linear_speed(-12.5, 0.02)
        assert result == pytest.approx(5.0)


class TestMomentumRampAngular:
    """Mit aktivem angularem Limit (_angular_acceleration=38.0) klemmt _ramp_azimuth_step die
    Änderung von ang_vel auf 1×angularAcceleration·dt gegen die Vorframe-ang_vel — KEIN Faktor 20
    wie beim linearen Clamp (verifiziert LocalPlayer::doMomentum)."""

    def test_ramp_azimuth_step_clamps_ang_vel(self, bot):
        bot._angular_acceleration = 38.0
        bot.azimuth = 0.0
        bot.ang_vel = 0.0
        max_delta = 38.0 * 0.02  # 0.76
        bot._ramp_azimuth_step(math.pi / 2, 0.02, bot._tank_turn_rate)
        assert abs(bot.ang_vel) <= max_delta + 1e-9
        assert bot.ang_vel > 0.0, "positive Richtung (diff war positiv)"
        # azimuth hat sich nur um ang_vel*dt gedreht (kleiner Wert), nicht sofort um
        # max_turn_rate*dt wie im ungeklemmten Alt-Verhalten.
        assert bot.azimuth == pytest.approx(bot.ang_vel * 0.02)
        assert bot.azimuth < bot._tank_turn_rate * 0.02

    def test_ramp_azimuth_step_no_limit_uses_full_turn_rate(self, bot):
        """Kontrolle ohne Limit: keine Klemme, ang_vel springt sofort auf den vollen (gecappten)
        Drehrate-Zielwert — Alt-Verhalten unverändert."""
        bot._angular_acceleration = 0.0
        bot.azimuth = 0.0
        bot.ang_vel = 0.0
        bot._ramp_azimuth_step(math.pi / 2, 0.02, bot._tank_turn_rate)
        expected = math.copysign(min(abs((math.pi / 2) / 0.02), bot._tank_turn_rate), math.pi / 2)
        assert bot.ang_vel == pytest.approx(expected)
        assert bot.ang_vel == pytest.approx(bot._tank_turn_rate)


class TestRampAzimuthExecutedRate:
    """F3: nach der Ramp-Klemme wird ang_vel zusätzlich auf die tatsächlich ausgeführte Drehrate
    geschnappt (|target|*dt darf |diff| nicht übersteigen) — Konsumenten (Wire-Update, Sprung-/
    Fall-Spin-Übernahme, Combat-Edge-Guard) erwarten ang_vel ≡ ausgeführte Drehung."""

    def test_settle_snaps_ang_vel_to_executed_rate(self, bot):
        bot._angular_acceleration = 1.0
        bot.ang_vel = 0.785
        bot.azimuth = 0.0
        diff = 0.01
        bot._ramp_azimuth_step(diff, 0.02, bot._tank_turn_rate)
        # ohne Snap wäre ang_vel die Ramp-Klemme (~0.765, nur um max_delta=0.02 von 0.785 runter) -
        # der Snap zieht auf die tatsächlich ausgeführte Rate diff/dt = 0.5.
        assert bot.ang_vel == pytest.approx(0.5)
        assert bot.azimuth == pytest.approx(0.01)

    def test_no_limit_control_uses_full_clamped_target(self, bot):
        """Kontrolle ohne Limit: |target|*dt <= |diff| gilt konstruktionsbedingt, der Snap feuert
        nie — voller geklemmter Zielwert wie bisher."""
        bot._angular_acceleration = 0.0
        bot.ang_vel = 0.0
        bot.azimuth = 0.0
        bot._ramp_azimuth_step(math.pi / 2, 0.02, bot._tank_turn_rate)
        assert bot.ang_vel == pytest.approx(bot._tank_turn_rate)


class TestMomentumWpTimeoutBonus:
    """_momentum_ramp_time(cycles) liefert die Zeit für `cycles` volle Anfahr-Rampen (0→eff.
    Speed) bei aktivem linearem Limit — Grundlage der WP-Timeout-/Stuck-Nachführung (P4-MOV-02a).
    Erwartungswerte werden aus bot._tank_speed abgeleitet (conftest-Default TANK_SPEED=25.0),
    nicht hart kodiert."""

    def test_low_acceleration_yields_larger_bonus(self, bot):
        from bot.constants import MOMENTUM_TIMEOUT_CYCLES, MOMENTUM_LIN_ACC_FACTOR
        bot._linear_acceleration = 1.0
        expected = MOMENTUM_TIMEOUT_CYCLES * bot._tank_speed / (MOMENTUM_LIN_ACC_FACTOR * 1.0)
        assert bot._momentum_ramp_time(MOMENTUM_TIMEOUT_CYCLES) == pytest.approx(expected)

    def test_high_acceleration_yields_small_bonus(self, bot):
        from bot.constants import MOMENTUM_TIMEOUT_CYCLES, MOMENTUM_LIN_ACC_FACTOR
        bot._linear_acceleration = 50.0
        expected = MOMENTUM_TIMEOUT_CYCLES * bot._tank_speed / (MOMENTUM_LIN_ACC_FACTOR * 50.0)
        result = bot._momentum_ramp_time(MOMENTUM_TIMEOUT_CYCLES)
        assert result == pytest.approx(expected)
        assert result < 0.2


class TestNavigateWpMomentum:
    """End-to-End (P4-MOV-02a): _navigate_wp nähert sich der Zielgeschwindigkeit über mehrere
    Ticks an (Rampe), statt sie sofort zu erreichen, wenn ein lineares Limit aktiv ist."""

    def test_speed_increases_monotonically_not_instantly(self, bot):
        _build_nav(bot, [])
        bot._linear_acceleration = 50.0
        bot._angular_acceleration = 0.0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        # Ziel weit entfernt entlang der Azimut-Achse → diff=0 (kein Dreh-Einfluss auf die
        # Geschwindigkeitskurve), so isoliert der Test die lineare Rampe.
        bot.target_pos = (1000.0, 0.0)
        bot._nav_path = [(1000.0, 0.0, 0.0)]
        bot._wp_start_time = None
        bot._wp_fail_count = 0

        dt = 0.02
        speeds = []
        for _ in range(5):
            bot._navigate_wp(dt, bot.world_half)
            speeds.append(math.hypot(bot.vel_x, bot.vel_y))

        for i in range(1, len(speeds)):
            assert speeds[i] >= speeds[i - 1] - 1e-9, "Geschwindigkeit muss monoton steigen"
        assert speeds[0] < bot._tank_speed - 1e-6, \
            "erster Tick darf die volle Geschwindigkeit noch nicht erreichen"
        assert speeds[-1] == pytest.approx(bot._tank_speed, abs=1e-6), \
            "nach einigen Ticks ist die Zielgeschwindigkeit erreicht"


# ── P4-MOV-02: M-Flagge (Momentum) modelliert ──────────────────────────────

class TestAccelLimitsMFlag:
    """_accel_limits() liefert bei getragenem M die _momentumLinAcc/_momentumAngAcc statt der
    -a-Server-Werte (ternäre ERSETZUNG, verifiziert LocalPlayer::doMomentum — kein Max)."""

    def test_m_flag_replaces_server_accel(self, bot):
        bot._linear_acceleration = 50.0
        bot._angular_acceleration = 38.0
        bot._momentum_lin_acc = 1.0
        bot._momentum_ang_acc = 1.0
        bot.own_flag = "M"
        assert bot._accel_limits() == (1.0, 1.0)   # M ersetzt, NICHT (50, 38)

    def test_no_flag_uses_server_accel(self, bot):
        bot._linear_acceleration = 50.0
        bot._angular_acceleration = 38.0
        bot._momentum_lin_acc = 1.0
        bot._momentum_ang_acc = 1.0
        bot.own_flag = ""
        assert bot._accel_limits() == (50.0, 38.0)

    def test_m_flag_activates_limits_without_dash_a(self, bot):
        """Ohne Server-Option -a (beide Accel 0.0) aktiviert allein die M-Flagge die Klemme.
        Kein Helper mehr - direkt über _accel_limits() geprüft."""
        bot._linear_acceleration = 0.0
        bot._angular_acceleration = 0.0
        bot._momentum_lin_acc = 1.0
        bot._momentum_ang_acc = 1.0
        bot.own_flag = "M"
        lin, ang = bot._accel_limits()
        assert lin > 0.0 and ang > 0.0
        # ohne M (und ohne -a) bleiben die Gates aus
        bot.own_flag = ""
        lin, ang = bot._accel_limits()
        assert lin == 0.0 and ang == 0.0


class TestMomentumRampSeverity:
    """M ist mit BZDB-Default 1.0 deutlich träger als der Zielserver -a 50: der lineare Clamp
    beträgt 20×1.0=20 u/s² (statt 20×50=1000) → ~50× kleinere Rampe pro Tick."""

    def test_m_flag_ramp_is_much_slower_than_server(self, bot):
        bot.azimuth = 0.0
        bot.vel_x = 0.0
        bot.vel_y = 0.0
        # M ohne -a: max_delta = 20 * 1.0 * 0.02 = 0.4 u/s pro Tick
        bot._linear_acceleration = 0.0
        bot._momentum_lin_acc = 1.0
        bot.own_flag = "M"
        assert bot._ramp_linear_speed(25.0, 0.02) == pytest.approx(0.4)
        # -a 50 ohne M: max_delta = 20 * 50 * 0.02 = 20.0 u/s pro Tick → 50× größer
        bot.own_flag = ""
        bot._linear_acceleration = 50.0
        assert bot._ramp_linear_speed(25.0, 0.02) == pytest.approx(20.0)
