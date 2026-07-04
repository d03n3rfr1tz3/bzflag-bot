"""
Tests für die neuen Capability-Check-Hilfsfunktionen in bot/ai/capabilities.py:
_has_presence, _can_drive_through_obstacles, _is_inside_obstacle,
_apply_movement_caps, _can_shoot, _can_jump, _is_landed (NAV-04).
"""
import time
import pytest
from bzflag.world_map import BoxObstacle, WorldMap
from bzflag.nav_graph import NavGraph


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _make_nav(bot, boxes, world_hash="cap_test"):
    wm = WorldMap(boxes=boxes, teleporters=[], links=[],
                  world_half=100.0, world_hash=world_hash)
    bot._nav_graph = NavGraph(wm)
    bot._world_map = wm


def _make_box(cx, cy, hw, hd, height):
    return BoxObstacle(cx=cx, cy=cy, bottom_z=0.0,
                       angle=0.0, half_w=hw, half_d=hd, height=height)


# ── _has_presence ─────────────────────────────────────────────────────────────
# _has_presence() leitet die Anwesenheit aus der Spielerliste ab: jeder Eintrag mit
# Nicht-Bot-Callsign (Mitspieler ODER Zuschauer) zählt; eigene Bots (Peers, Manager-
# Fallback-Observer) zählen nicht.

def _add_observer(bot, pid, callsign):
    from bot.models import PlayerInfo
    from bzflag.protocol import TEAM_OBSERVER
    bot.players[pid] = PlayerInfo(callsign=callsign, team=TEAM_OBSERVER, is_human=False)


def test_has_presence_with_human_player(bot):
    from conftest import make_player
    make_player(bot, 2, is_human=True)          # Callsign "Player2" → Mensch
    assert bot._has_presence() is True


def test_has_presence_with_human_observer(bot):
    _add_observer(bot, 5, "Zuschauer")          # menschlicher Zuschauer (kein Bot-Callsign)
    assert bot._has_presence() is True


def test_has_presence_bot_observer_does_not_count(bot):
    _add_observer(bot, 6, "Bot_99")             # Manager-/Peer-Bot als Observer → zählt NICHT
    assert bot._has_presence() is False


def test_has_presence_only_peer_bots(bot):
    from bot.models import PlayerInfo
    bot.players[7] = PlayerInfo(callsign="Bot_02", team=2, is_human=False)   # Peer-Bot
    bot.players[1] = PlayerInfo(callsign="TestBot", team=2, is_human=False)  # eigener Bot
    assert bot._has_presence() is False


def test_has_presence_empty(bot):
    assert bot._has_presence() is False


# ── Spawn-Entscheidung (IDLE vs. SEEKING) berücksichtigt Zuschauer ──────────────

def _spawn_payload(pid, x=0.0, y=0.0, z=0.0, az=0.0):
    import struct
    return struct.pack(">B", pid) + struct.pack(">fff", x, y, z) + struct.pack(">f", az)


def test_spawn_seeking_with_human_observer(bot, monkeypatch):
    """Spawnt der Bot, während nur ein menschlicher Zuschauer da ist, → SEEKING (nicht IDLE)."""
    from bot.models import AIState
    monkeypatch.setattr(bot, "_new_target", lambda: None)
    _add_observer(bot, 5, "Zuschauer")          # Mensch im Beobachter-Modus
    bot.human_count = 0                          # kein aktiver Mitspieler
    bot._on_alive(0, _spawn_payload(bot.player_id))
    assert bot._ai_state == AIState.SEEKING


def test_spawn_idle_with_only_bot_observer(bot, monkeypatch):
    """Nur ein Bot-Observer (Manager-Fallback) → keine menschliche Anwesenheit → IDLE."""
    from bot.models import AIState
    monkeypatch.setattr(bot, "_new_target", lambda: None)
    _add_observer(bot, 6, "Bot_99")             # eigener Bot als Observer
    bot.human_count = 0
    bot._on_alive(0, _spawn_payload(bot.player_id))
    assert bot._ai_state == AIState.IDLE


# ── Kampf-Gate hängt an Anwesenheit, nicht an zielbaren Menschen ───────────────
# _find_target_player/_maybe_shoot/_tick_combat aktivieren den Kampf, sobald irgendein
# Mensch (Mitspieler ODER Zuschauer) da ist — Peer-Bots auf Gegner-Teams sind gültige Ziele,
# damit die Bots für einen Zuschauer lebendig wirken. Kein Mensch → passiv.

def _add_peer_bot_foe(bot, pid=2, pos=(50.0, 0.0, 0.0)):
    """Lebender, kürzlich gesehener Peer-Bot auf Gegner-Team (Bot-Callsign → keine Anwesenheit)."""
    from conftest import make_player
    foe = make_player(bot, pid=pid, pos=pos, is_human=False, flag="")
    foe.callsign = "Bot_02"
    return foe


def test_find_target_with_human_observer(bot):
    """Nur Peer-Bots + ein menschlicher Zuschauer (human_count==0) → der Peer-Bot wird Ziel."""
    _add_peer_bot_foe(bot, pid=2)
    _add_observer(bot, 5, "Zuschauer")           # menschlicher Zuschauer → _has_presence True
    bot.human_count = 0
    assert bot._has_presence() is True
    assert bot._find_target_player() == 2


def test_find_target_none_without_human(bot):
    """Nur Peer-Bots, kein Mensch → kein Ziel (Passivmodus)."""
    _add_peer_bot_foe(bot, pid=2)
    bot.human_count = 0
    assert bot._has_presence() is False
    assert bot._find_target_player() is None


def test_random_shot_fires_for_observer(bot):
    """Look-alive-Random-Schuss feuert, sobald ein Mensch zusieht (auch reiner Zuschauer)."""
    bot.own_flag = ""
    bot.target_player = None
    _add_observer(bot, 5, "Zuschauer")
    bot.human_count = 0
    bot._next_shoot = 0.0
    bot._maybe_shoot(time.monotonic())
    bot.client.send.assert_called()


def test_no_random_shot_with_only_peer_bots(bot):
    """Nur Peer-Bots, kein Mensch → kein Look-alive-Schuss."""
    bot.own_flag = ""
    bot.target_player = None
    _add_peer_bot_foe(bot, pid=2)
    bot.human_count = 0
    bot._next_shoot = 0.0
    bot._maybe_shoot(time.monotonic())
    bot.client.send.assert_not_called()


def test_tick_combat_stays_with_observer(bot, monkeypatch):
    """COMBAT bleibt bei Zuschauer-Anwesenheit + gültigem Ziel erhalten (kein Austritt bei human_count==0)."""
    from bot.models import AIState
    _add_peer_bot_foe(bot, pid=2)
    _add_observer(bot, 5, "Zuschauer")
    bot.human_count = 0
    bot.target_player = 2
    bot._ai_state = AIState.COMBAT
    monkeypatch.setattr(bot, "_handle_threat", lambda now: False)
    monkeypatch.setattr(bot, "_check_opportunistic_grab", lambda now: None)
    monkeypatch.setattr(bot, "_check_tactical_jump", lambda now: False)
    monkeypatch.setattr(bot, "_check_z_attack_jump", lambda now: None)
    bot._tick_combat(time.monotonic())
    assert bot._ai_state == AIState.COMBAT
    assert bot.target_player == 2


def test_tick_combat_exits_to_idle_without_human(bot):
    """Verlässt der letzte Mensch die Szene → COMBAT fällt auf IDLE (kein Ziel mehr nötig)."""
    from bot.models import AIState
    _add_peer_bot_foe(bot, pid=2)
    bot.human_count = 0
    bot.target_player = 2
    bot._ai_state = AIState.COMBAT
    bot._tick_combat(time.monotonic())
    assert bot._ai_state == AIState.IDLE


# ── _can_drive_through_obstacles ──────────────────────────────────────────────

def test_can_drive_through_obstacles_oo_flag(bot):
    bot.own_flag = "OO"
    assert bot._can_drive_through_obstacles() is True


def test_can_drive_through_obstacles_no_flag(bot):
    bot.own_flag = ""
    assert bot._can_drive_through_obstacles() is False


# ── _is_inside_obstacle ───────────────────────────────────────────────────────

def test_is_inside_obstacle_outside_building(bot):
    """Bot weit vom Gebäude → nicht in blockierter Zelle."""
    box = _make_box(50.0, 0.0, 8.0, 8.0, 10.0)
    _make_nav(bot, [box])
    bot.pos      = [0.0, 0.0, 0.0]
    bot.own_flag = ""
    assert bot._is_inside_obstacle() is False


def test_is_inside_obstacle_inside_building(bot):
    """Bot im Gebäude (blockierte Zelle) → True."""
    box = _make_box(0.0, 0.0, 10.0, 10.0, 10.0)
    _make_nav(bot, [box], world_hash="cap_inside")
    bot.pos      = [0.0, 0.0, 0.0]
    bot.own_flag = ""
    assert bot._is_inside_obstacle() is True


def test_is_inside_obstacle_oo_bypasses(bot):
    """OO-Flagge → _is_inside_obstacle gibt False zurück, auch im Gebäude."""
    box = _make_box(0.0, 0.0, 10.0, 10.0, 10.0)
    _make_nav(bot, [box], world_hash="cap_oo")
    bot.pos      = [0.0, 0.0, 0.0]
    bot.own_flag = "OO"
    assert bot._is_inside_obstacle() is False


def test_is_inside_obstacle_on_top_is_outside(bot):
    """Fix 2: Bot-Basis bündig AUF der Box-Oberkante (pz == bottom_z+height) gilt als 'nicht innen'
    (er steht darauf), knapp darunter weiterhin als 'innen'. Sichert die Fahr-Teleporter-Durchfahrt:
    der Austritt landet exakt auf z=Box-Top und darf nicht revertiert werden."""
    box = _make_box(0.0, 0.0, 10.0, 10.0, 10.0)   # bottom_z=0, height=10 → Oberkante z=10
    _make_nav(bot, [box], world_hash="cap_ontop")
    bot.own_flag = ""
    bot.pos = [0.0, 0.0, 10.0]    # Basis exakt auf der Oberkante → steht darauf
    assert bot._is_inside_obstacle() is False
    bot.pos = [0.0, 0.0, 9.7]     # knapp darin → innen
    assert bot._is_inside_obstacle() is True


# ── _apply_movement_caps ──────────────────────────────────────────────────────

def test_caps_fo_prevents_reverse(bot):
    """FO-Flagge (Forward Only): Rückwärts-Speed auf 0 begrenzt."""
    bot.own_flag = "FO"
    speed, _ = bot._apply_movement_caps(-1.0, 0.0)
    assert speed == pytest.approx(0.0)


def test_caps_ro_prevents_forward(bot):
    """RO-Flagge (Reverse Only): Vorwärts-Speed auf 0 begrenzt."""
    bot.own_flag = "RO"
    speed, _ = bot._apply_movement_caps(1.0, 0.0)
    assert speed == pytest.approx(0.0)


def test_caps_lt_prevents_right_turn(bot):
    """LT-Flagge (Left Turn Only): Rechtsdrehung (negative ang_vel) auf 0 begrenzt."""
    bot.own_flag = "LT"
    _, ang_vel = bot._apply_movement_caps(0.0, -1.0)   # negativ = rechts/clockwise
    assert ang_vel == pytest.approx(0.0)


def test_caps_rt_prevents_left_turn(bot):
    """RT-Flagge (Right Turn Only): Linksdrehung (positive ang_vel) auf 0 begrenzt."""
    bot.own_flag = "RT"
    _, ang_vel = bot._apply_movement_caps(0.0, 1.0)    # positiv = links/counterclockwise
    assert ang_vel == pytest.approx(0.0)


# ── _can_shoot ────────────────────────────────────────────────────────────────

def test_can_shoot_debug_flag_disables(bot):
    bot._debug_no_shoot = True
    assert bot._can_shoot() is False


def test_can_shoot_no_udp(bot):
    bot.client.udp_active = False
    assert bot._can_shoot() is False


def test_can_shoot_no_player_id(bot):
    bot.player_id = None
    assert bot._can_shoot() is False


def test_can_shoot_all_ok(bot):
    """Alle Voraussetzungen erfüllt (fixture-Standard) → True."""
    assert bot._can_shoot() is True


# ── _can_jump ─────────────────────────────────────────────────────────────────

def test_can_jump_debug_flag_disables(bot):
    bot._debug_no_jump = True
    assert bot._can_jump(time.monotonic()) is False


def test_can_jump_while_jumping(bot):
    bot._jumping = True
    assert bot._can_jump(time.monotonic()) is False


def test_can_jump_nj_flag(bot):
    bot.own_flag = "NJ"
    assert bot._can_jump(time.monotonic()) is False


def test_can_jump_cooldown_active(bot):
    now = time.monotonic()
    bot._last_jump_at = now
    assert bot._can_jump(now) is False


def test_can_jump_all_conditions_met(bot):
    now = time.monotonic()
    bot._last_jump_at = now - 100.0
    assert bot._can_jump(now) is True


def test_can_jump_blocked_when_server_no_jumping(bot):
    """Server ohne -j (JumpingGameStyle aus) → kein Sprung ohne Sprung-Flagge."""
    now = time.monotonic()
    bot._last_jump_at = now - 100.0
    bot._server_jumping = False
    bot.own_flag = ""
    assert bot._can_jump(now) is False


@pytest.mark.parametrize("flag", ["WG", "BY", "JP"])
def test_can_jump_flag_overrides_server_no_jumping(bot, flag):
    """Sprung-gewährende Flaggen (Wings/Bouncy/Jumping) springen auch ohne -j."""
    now = time.monotonic()
    bot._last_jump_at = now - 100.0
    bot._server_jumping = False
    bot.own_flag = flag
    assert bot._can_jump(now) is True


# ── _is_landed (NAV-04: Aufstiegs-Check) ─────────────────────────────────────

def test_is_landed_ascending_returns_false(bot):
    """Bot steigt auf (vel[2] > 0.1) → _is_landed False, auch nahe am Boden (NAV-04)."""
    bot.vel[2] = 5.0
    bot.pos[2] = 0.1
    assert bot._is_landed() is False


def test_is_landed_at_ground(bot):
    """Bot am Boden, keine Aufwärtsbewegung → _is_landed True."""
    bot.vel[2] = 0.0
    bot.pos[2] = 0.0
    assert bot._is_landed() is True


# ── _apply_obstacle_bounds (COL-01: Wall-Sliding) ────────────────────────────

def test_obstacle_bounds_blocks_perpendicular(bot):
    """Bot fährt gerade auf Wand zu → vx wird auf 0 gesetzt."""
    box = _make_box(5.0, 0.0, 5.0, 5.0, 10.0)
    _make_nav(bot, [box])
    bot.pos      = [0.0, 0.0, 0.0]
    bot.own_flag = ""
    bot.vel      = [25.0, 0.0, 0.0]
    bot._apply_obstacle_bounds(0.1)
    assert bot.vel[0] == pytest.approx(0.0, abs=0.1)
    assert bot.vel[1] == pytest.approx(0.0, abs=0.1)


def test_obstacle_bounds_slides_parallel(bot):
    """Bot fährt parallel zur Wand → Geschwindigkeit bleibt unverändert."""
    box = _make_box(0.0, 15.0, 5.0, 5.0, 10.0)
    _make_nav(bot, [box])
    bot.pos      = [0.0, 0.0, 0.0]
    bot.own_flag = ""
    bot.vel      = [25.0, 0.0, 0.0]
    bot._apply_obstacle_bounds(0.1)
    assert bot.vel[0] == pytest.approx(25.0, abs=0.1)
    assert bot.vel[1] == pytest.approx(0.0, abs=0.1)


def test_obstacle_bounds_oo_bypasses(bot):
    """OO-Flagge: Kollisionskorrektur wird übersprungen."""
    box = _make_box(5.0, 0.0, 5.0, 5.0, 10.0)
    _make_nav(bot, [box])
    bot.pos      = [0.0, 0.0, 0.0]
    bot.own_flag = "OO"
    bot.vel      = [25.0, 0.0, 0.0]
    bot._apply_obstacle_bounds(0.1)
    assert bot.vel[0] == pytest.approx(25.0, abs=0.1)


def test_obstacle_bounds_roof_not_blocked(bot):
    """Bot steht auf dem Dach (pz = Gebäudehöhe) → wird nicht blockiert."""
    box = _make_box(0.0, 0.0, 5.0, 5.0, 10.0)
    _make_nav(bot, [box])
    bot.pos      = [0.0, 0.0, 10.0]
    bot.own_flag = ""
    bot.vel      = [25.0, 0.0, 0.0]
    bot._apply_obstacle_bounds(0.1)
    assert bot.vel[0] == pytest.approx(25.0, abs=0.1)


# ── Burrow-Boden-Gate & OO-Landung (z-abhängig) ───────────────────────────────

def test_burrow_floor_on_building(bot):
    """BU: auf einem Dach trägt das Dach (kein Durchsacken); nur am Boden sinkt der Bot auf
    BURROW_DEPTH."""
    box = _make_box(0.0, 0.0, 5.0, 5.0, 10.0)
    _make_nav(bot, [box])
    bot.own_flag = "BU"
    bot.pos = [0.0, 0.0, 10.0]               # auf dem Dach
    assert bot._get_floor_z() == pytest.approx(10.0)
    bot.pos = [50.0, 50.0, 0.0]              # am Boden, abseits des Gebäudes
    assert bot._get_floor_z() == pytest.approx(bot._burrow_depth)


def test_burrow_speed_turn_only_below_ground(bot):
    """BU-Malus auf Speed/Turn nur eingegraben (z<0), nicht auf einem Dach (z>0)."""
    bot.own_flag = "BU"
    bot.pos = [0.0, 0.0, -1.32]
    assert bot._effective_tank_speed() == pytest.approx(bot._tank_speed * bot._burrow_speed_ad)
    assert bot._effective_turn_rate() == pytest.approx(bot._tank_turn_rate * bot._burrow_ang_ad)
    bot.pos = [0.0, 0.0, 5.0]
    assert bot._effective_tank_speed() == pytest.approx(bot._tank_speed)
    assert bot._effective_turn_rate() == pytest.approx(bot._tank_turn_rate)


def test_oo_floor_is_ground(bot):
    """OO phast durch Gebäude → Boden ist immer z=0, auch auf einem Dach."""
    box = _make_box(0.0, 0.0, 5.0, 5.0, 10.0)
    _make_nav(bot, [box])
    bot.own_flag = "OO"
    bot.pos = [0.0, 0.0, 10.0]
    assert bot._get_floor_z() == pytest.approx(0.0)


def test_oo_no_rooftop_navjump(bot):
    """OO: Dach-Wegpunkt löst KEINEN NAV_JUMP/NAV_JUMP_ALIGN aus (kein Landen auf Dächern) —
    der Bot fährt den WP am Boden phasend an."""
    from bot.models import AIState
    bot.own_flag = "OO"
    bot.pos = [0.0, 0.0, 0.0]
    bot._ai_state = AIState.SEEKING
    bot._nav_path = [(0.0, 0.0, 0.0), (10.0, 0.0, 10.0)]   # 2. WP = Dach (z=10)
    bot._advance_path()
    assert bot._ai_state not in (AIState.NAV_JUMP, AIState.NAV_JUMP_ALIGN)
    assert bot.target_pos == (10.0, 0.0)


# ── Sicht-FoV (vereinheitlicht) + WA + _is_ahead-Geometrie ────────────────────

def test_effective_fov_default(bot):
    """Ohne WA = halber Target-FoV (±37.5°), EINZIGER Sicht-Kegel."""
    from bot.constants import TARGET_FOV
    bot.own_flag = ""
    assert bot._effective_fov() == pytest.approx(TARGET_FOV / 2.0)

def test_effective_fov_wide_angle(bot):
    """WA verbreitert den Sicht-FoV auf halben _wideAngleAng (~±50°)."""
    from bot.constants import WIDE_ANGLE_ANG
    bot.own_flag = "WA"
    assert bot._effective_fov() == pytest.approx(WIDE_ANGLE_ANG / 2.0)
    assert bot._effective_fov() > 0.6545  # > 37.5° → tatsächlich breiter

def test_effective_fov_wide_angle_server_var(bot):
    """_wideAngleAng vom Server treibt den WA-FoV."""
    import math
    bot.own_flag = "WA"
    bot._wide_angle_ang = math.radians(140)
    assert bot._effective_fov() == pytest.approx(math.radians(70))

def test_is_ahead_geometry(bot):
    """_is_ahead = ±90°-„liegt vor mir" (kein Sicht-FoV): 60° seitlich vor, 180° hinter."""
    import math
    bot.pos = [0.0, 0.0, 0.0]
    bot.azimuth = 0.0
    assert bot._is_ahead(math.cos(math.radians(60)) * 50, math.sin(math.radians(60)) * 50) is True
    assert bot._is_ahead(-50.0, 0.0) is False   # direkt hinter dem Bot


# ── Flag-Klassifizierung (FLG-03: Vollständigkeit) ────────────────────────────

def test_bad_flags_contains_all_official():
    """Alle 14 offiziellen bösen BZFlag-Flags sind in BAD_FLAGS_DEFAULT."""
    from bot.constants import BAD_FLAGS_DEFAULT
    official_bad = {"B", "BY", "CB", "FO", "JM", "LT", "M",
                    "NJ", "O", "RC", "RO", "RT", "TR", "WA"}
    missing = official_bad - BAD_FLAGS_DEFAULT
    assert not missing, f"Fehlende bad_flags: {missing}"


def test_good_and_bad_flags_disjoint():
    """Kein Flag ist gleichzeitig in GOOD_FLAGS_DEFAULT und BAD_FLAGS_DEFAULT."""
    from bot.constants import GOOD_FLAGS_DEFAULT, BAD_FLAGS_DEFAULT
    overlap = GOOD_FLAGS_DEFAULT & BAD_FLAGS_DEFAULT
    assert not overlap, f"Flags in beiden Listen: {overlap}"
