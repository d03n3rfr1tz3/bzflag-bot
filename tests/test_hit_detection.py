"""
Hit-Detection-Tests für _resolve_incoming_shots und _check_steamroller.

Konstanten (aus BZFlag global.cxx + SegmentedShotStrategy.cxx):
  TANK_RADIUS      = 0.72 * 6.0 = 4.32
  eff_r default    = TANK_RADIUS * 0.99 ≈ 4.28u  (BZFlag-konforme Kugel)
  OBESITY_FACTOR   = 2.5  → eff_r = 4.32*2.5*0.99 ≈ 10.69u
  TINY_FACTOR      = 0.4  → eff_r = 4.32*0.4*0.99 ≈ 1.71u
  N-Flag           → OBB: half_len=3.5 (Längsachse), half_w=1.0 (Querachse), half_h=1.5
  SHOCK_IN_RADIUS  = 6.0
  SHOCK_OUT_RADIUS = 60.0
  SW_EXPAND_SPEED  = 60.0  → SW-Front kommt nach (d-6)/60 Sekunden
  SR_RADIUS_MULT   = 2.0  → kill if dist < TANK_RADIUS * 3 ≈ 12.96
"""
import math
import time
import pytest
from conftest import make_shot, make_player, make_th_shot_payload


TANK_HEIGHT = 2.05
TANK_CZ     = TANK_HEIGHT / 2   # tank center z when pos[2]=0


def _resolve_incoming_shots(bot):
    """Hilfsfunktion: einen einzelnen _resolve_incoming_shots-Tick ausführen."""
    bot._resolve_incoming_shots(time.monotonic(), 0.02)


def _was_killed(bot):
    return bot.client.send.called and not bot.alive


# ── Shockwave ─────────────────────────────────────────────────────────────────

def test_sw_hit_middle_distance(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # SW fired 0.5s ago: sw_front = 6 + 0.5*60 = 36u > dist=30u → Treffer
    make_shot(bot, pos=(30.0, 0.0, TANK_CZ), is_sw=True, flag_abbr=b"SW",
              fire_time=time.monotonic() - 0.5)
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_sw_hit_3d_height(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # SW center at z=30, tank center at z≈1.025 → dist≈28.97 inside donut
    # SW fired 0.5s ago: sw_front = 36u > 28.97u → Treffer
    make_shot(bot, pos=(0.0, 0.0, 30.0), is_sw=True, flag_abbr=b"SW",
              fire_time=time.monotonic() - 0.5)
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_sw_no_hit_before_wave_arrives(bot):
    """SW gerade abgefeuert (elapsed≈0): sw_front=6u, Bot bei d=30u → noch kein Treffer."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(30.0, 0.0, TANK_CZ), is_sw=True, flag_abbr=b"SW",
              fire_time=time.monotonic())
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


def test_sw_miss_too_far(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(80.0, 0.0, TANK_CZ), is_sw=True, flag_abbr=b"SW")
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


def test_sw_miss_inside_inner_radius(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(3.0, 0.0, TANK_CZ), is_sw=True, flag_abbr=b"SW")
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


def test_sw_miss_exactly_on_outer_radius(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # dist == SHOCK_OUT_RADIUS → condition is strictly < 60, so miss
    make_shot(bot, pos=(60.0, 0.0, TANK_CZ), is_sw=True, flag_abbr=b"SW")
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


def test_sw_no_hit_when_dead(bot):
    bot.alive = False
    make_shot(bot, pos=(30.0, 0.0, TANK_CZ), is_sw=True, flag_abbr=b"SW")
    _resolve_incoming_shots(bot)
    bot.client.send.assert_not_called()


# ── Normal shot ───────────────────────────────────────────────────────────────

def test_normalshot_direct_hit(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # Shot starts 2u in front, flying directly toward bot → segment passes through
    make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0))
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_normalshot_miss(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # Shot passes 20u to the side
    make_shot(bot, pos=(200.0, 20.0, TANK_CZ), vel=(-100.0, 0.0, 0.0))
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


# ── PhantomZone: Phantom-Schüsse gezoneder Gegner ────────────────────────────

class TestPhantomShotHarmless:
    """Wire-Flag „PZ" = Schütze war beim Feuern gezoned (sein Client nullt das
    Flag sonst). Phantom-Schüsse treffen nur ebenfalls gezonede Ziele
    (LocalPlayer::checkHit: „zoned shots only kill zoned tanks") — der Bot ist
    nie gezoned, darf also weder daran sterben noch ausweichen. SB bleibt als
    wanddurchdringender Schuss unverändert eine echte Bedrohung."""

    def test_pz_shot_does_not_kill(self, bot):
        """Geometrie wie test_normalshot_direct_hit, aber Wire-Flag PZ → kein Tod."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                  flag_abbr=b"PZ")
        _resolve_incoming_shots(bot)
        assert not _was_killed(bot)

    def test_sb_shot_still_kills(self, bot):
        """Kontrolltest: identische Geometrie mit SB → Treffer wie bisher."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                  flag_abbr=b"SB")
        _resolve_incoming_shots(bot)
        assert _was_killed(bot)

    def test_pz_shot_not_a_threat(self, bot):
        """Kein Dodge-Auslöser: _find_incoming_shot ignoriert Phantom-Schüsse."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        make_shot(bot, pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                  flag_abbr=b"PZ")
        shot, t = bot._find_incoming_shot(time.monotonic())
        assert shot is None and t == float("inf")

    def test_pz_shot_phase_path_not_a_threat(self, bot):
        """Auch der gecachte Phase-Pfad (Teleporter-Karten) ist keine Bedrohung."""
        from bzflag.shot_physics import Segment
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        now = time.monotonic()
        s = make_shot(bot, pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                      flag_abbr=b"PZ", fire_time=now)
        bot._ricochet_paths[(s.shooter_id, s.shot_id)] = [Segment(
            50.0, 0.0, 1.025, -300.0, 0.0, 1.025, now, now + 3.5)]
        shot, _ = bot._find_incoming_shot(now)
        assert shot is None

    def test_pz_shot_stays_tracked_until_expiry(self, bot):
        """Der Schuss bleibt in _shots (MsgShotEnd-Buchhaltung), wird nur ignoriert."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        s = make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                      flag_abbr=b"PZ")
        _resolve_incoming_shots(bot)
        assert (s.shooter_id, s.shot_id) in bot._shots

    def test_rico_threat_survives_trailing_pz_shot_in_first_loop(self, bot):
        """Regressionstest (R1): _find_incoming_shot muss im Rico-Zweig den zugehörigen
        `rico_shot` auf PZ prüfen, nicht die stehengebliebene `shot`-Variable der ersten
        Schleife. Ist der ZULETZT in _shots eingefügte Eintrag ein PZ-Direktschuss, darf
        das einen echten (Nicht-PZ) Rico-Treffer nicht mehr als harmlos maskieren."""
        from bzflag.shot_physics import Segment
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        now = time.monotonic()
        # 1) echter Rico-Schuss mit Segment-Cache: direkte Bedrohung
        rico = make_shot(bot, shooter_id=3, shot_id=1, pos=(50.0, 0.0, 1.025),
                          vel=(-100.0, 0.0, 0.0), fire_time=now)
        bot._ricochet_paths[(rico.shooter_id, rico.shot_id)] = [Segment(
            50.0, 0.0, 1.025, -300.0, 0.0, 1.025, now, now + 3.5)]
        # 2) danach ein PZ-Direktschuss (kein Rico-Pfad) -> letzter Eintrag der ersten
        # Schleife, also der Wert, den `shot` beim Verlassen der Schleife noch trägt
        make_shot(bot, shooter_id=2, shot_id=1, pos=(2.0, 0.0, 1.025),
                  vel=(-100.0, 0.0, 0.0), flag_abbr=b"PZ", fire_time=now)
        shot, t = bot._find_incoming_shot(now)
        assert shot is rico
        assert t < float("inf")

    def test_any_incoming_threat_ignores_pz_shots(self, bot):
        """_any_incoming_threat muss PZ-Schüsse ebenso ignorieren wie _find_incoming_shot
        — sowohl im Direkt- als auch im Rico-Pfad-Zweig (Gegenprobe: ohne PZ-Flag ist
        dieselbe Konstellation eine Bedrohung)."""
        from bzflag.shot_physics import Segment
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        now = time.monotonic()
        vels = ((0.0, 0.0),)
        direct = make_shot(bot, shooter_id=2, shot_id=1, pos=(2.0, 0.0, 1.025),
                            vel=(-100.0, 0.0, 0.0), flag_abbr=b"PZ", fire_time=now)
        rico = make_shot(bot, shooter_id=3, shot_id=1, pos=(50.0, 0.0, 1.025),
                          vel=(-100.0, 0.0, 0.0), flag_abbr=b"PZ", fire_time=now)
        bot._ricochet_paths[(rico.shooter_id, rico.shot_id)] = [Segment(
            50.0, 0.0, 1.025, -300.0, 0.0, 1.025, now, now + 3.5)]
        assert bot._any_incoming_threat(now, vels) is False
        # Gegenprobe: ohne PZ-Flag ist dieselbe Konstellation eine Bedrohung
        direct.flag_abbr = b"\x00\x00"
        rico.flag_abbr = b"\x00\x00"
        assert bot._any_incoming_threat(now, vels) is True


def test_normalshot_expired_no_hit(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # Shot lifetime already expired at fire_time - 1
    ft = time.monotonic() - 10.0
    make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              lifetime=3.5, fire_time=ft)
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


# ── Variable shot speeds (simulating MG/RF/SB-like parameters) ────────────────

def test_fast_shot_hits(bot):
    """Fast shot (200 u/s) from close range still hits."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-200.0, 0.0, 0.0), lifetime=1.5)
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_slow_shot_hits_if_on_course(bot):
    """Slow shot (50 u/s) still hits if trajectory passes through tank."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-50.0, 0.0, 0.0), lifetime=5.0)
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_short_lifetime_shot_expires_before_hit(bot):
    """Shot with 0.05s lifetime expires before reaching target at 200u."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # lifetime = 0.05s, shot is 200u away → can only travel 100*0.05 = 5u → expires
    ft = time.monotonic() - 0.1   # already 0.1s old, lifetime was only 0.05s
    make_shot(bot, pos=(200.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              lifetime=0.05, fire_time=ft)
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


# ── Hit radius (BZFlag-konform) ───────────────────────────────────────────────

def test_normal_hit_radius_matches_bzflag(bot):
    """Normaler Tank: eff_r = TANK_RADIUS * 0.99 ≈ 4.28u (BZFlag-konform)."""
    from bot.constants import TANK_RADIUS
    bot.own_flag = ""
    eff_r = bot._effective_hit_radius()
    assert eff_r == pytest.approx(TANK_RADIUS * 0.99, abs=0.01)


def test_obesity_flag_increases_hit_radius(bot):
    """Obesity-OBB ist breiter als normal: Schuss 3.5u seitlich trifft O-Tank, nicht Normaltank."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.own_flag = "O"
    # O-OBB-Halbbreite = TANK_WIDTH/2*2.5+SHOT_RADIUS = 1.4*2.5+0.5 = 4.0u → 3.5u trifft
    # Normal-OBB-Halbbreite = TANK_WIDTH/2+SHOT_RADIUS = 1.4+0.5 = 1.9u → 3.5u verfehlt
    make_shot(bot, pos=(2.0, 3.5, TANK_CZ), vel=(-100.0, 0.0, 0.0))
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_obesity_flag_miss_if_still_too_far(bot):
    """With Obesity, shot 20u away still misses (20 > TANK_RADIUS*OBESITY_FACTOR*0.99≈10.69u)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.own_flag = "O"
    make_shot(bot, pos=(2.0, 20.0, TANK_CZ), vel=(-100.0, 0.0, 0.0))
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


# ── Narrow flag (OBB) ─────────────────────────────────────────────────────────

def test_narrow_flag_still_hittable(bot):
    """Bot mit N-Flagge, Schuss direkt auf Mittellinie → Treffer (OBB-Treffer)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.own_flag = "N"
    bot.azimuth = 0.0
    make_shot(bot, pos=(1.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0))
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_narrow_flag_miss_outside_obb(bot):
    """Bot mit N-Flagge: Schuss 2u seitlich verfehlt (Y=2.0 > OBB half_w=1.0)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.own_flag = "N"
    bot.azimuth = 0.0
    make_shot(bot, pos=(2.0, 2.0, TANK_CZ), vel=(-100.0, 0.0, 0.0))
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


def test_narrow_side_hit_at_2u(bot):
    """N-Flag: Seitangriff bei x=2u entlang Längsachse → Treffer (OBB half_len=3.5u).
    Schuss bei (2.0, 0.5) fliegt in -Y: y=0.5 inside OBB half_w=1.0, x=2.0 inside half_len=3.5.
    dist zum Tank-Zentrum ≈ 2.06u > alter _MIN_HIT_RADIUS=1.5u → mit Kugel wäre das miss gewesen."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.own_flag = "N"
    bot.azimuth = 0.0
    make_shot(bot, pos=(2.0, 0.5, TANK_CZ), vel=(0.0, -100.0, 0.0))
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_narrow_front_miss_via_obb(bot):
    """N-Flag: Frontalschuss 1.2u seitlich → MISS via OBB (half_w=1.0u).
    Schuss bei (0.5, 1.2) von vorne: dist≈1.3u < 1.5u (alte Kugel wäre Treffer!), OBB korrekt MISS."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.own_flag = "N"
    bot.azimuth = 0.0
    # Schuss von vorne (vel in -X), 1.2u in Y-Richtung versetzt (= Querachse)
    # y=1.2 > OBB half_w=1.0 → MISS (auch wenn dist≈1.3u < alter Mindest-Kugel 1.5u)
    make_shot(bot, pos=(0.5, 1.2, TANK_CZ), vel=(-100.0, 0.0, 0.0))
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


# ── GM (Guided Missile) ───────────────────────────────────────────────────────

def test_gm_targeting_self_hits(bot):
    """GM targeting our bot, already on course → hit."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(1.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=1)
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


def test_gm_no_target_misses(bot):
    """GM with gm_target_pid=255 (no target) flies straight → misses if off-axis."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(200.0, 20.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=255)
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


def test_gm_targeting_other_player_no_hit(bot):
    """GM targeting another player (pid=3) steers toward them, not at us."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_player(bot, pid=3, pos=(0.0, 200.0, 0.0))
    make_shot(bot, pos=(200.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=3)
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


def test_gm_rate_limited_turn_no_false_hit(bot):
    """GM zeigt anfangs weg vom Bot (+X Richtung); Ziel wird gesetzt.
    Mit Rate-Limiting (0.628 rad/s) kann die Rakete sich in einem Tick (dt=0.02s)
    nur um 0.0126 rad drehen → bleibt weit vom Bot → kein Treffer."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    # GM 50u hinter dem Bot (bei +X), fliegt in +X weg vom Bot
    make_shot(bot, pos=(50.0, 0.0, TANK_CZ), vel=(100.0, 0.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=1)
    bot._resolve_incoming_shots(time.monotonic(), 0.02)
    assert not _was_killed(bot)


def _make_nav(bot, boxes, world_hash="gm_wall_test"):
    """Baut ein WorldMap+NavGraph mit den gegebenen soliden Boxen und hängt es an
    den Bot — Basis für _segment_clear (nav._los_grid/_los_obs)."""
    from bzflag.world_map import WorldMap
    from bzflag.nav_graph import NavGraph
    wm = WorldMap(boxes=boxes, teleporters=[], links=[],
                  world_half=200.0, world_hash=world_hash)
    bot._nav_graph = NavGraph(wm)
    bot._world_map = wm


def _wall_box(cx, cy=0.0, half_w=0.3, half_d=10.0, height=10.0):
    from bzflag.world_map import BoxObstacle
    return BoxObstacle(cx=cx, cy=cy, bottom_z=0.0, angle=0.0,
                       half_w=half_w, half_d=half_d, height=height)


def test_gm_wall_hit_terminates_missile(bot):
    """GM fliegt gegen eine solide Wand, die zwischen ihrem Ausgangs- und Zielpunkt
    liegt — sie muss dort (wie im echten Client: checkBuildings/setExpiring)
    sterben statt den Tank direkt dahinter zu treffen."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    _make_nav(bot, [_wall_box(cx=1.0)])
    # Ohne Wand würde diese Rakete den Tank in diesem Tick treffen (2.0 -2.0 = 0.0).
    make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=1)
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)
    assert (2, 1) not in bot._shots


def test_gm_wall_hit_stays_dead_even_with_later_los(bot):
    """Regressionstest fürs eigentliche Bug-Symptom: die Rakete bleibt tot, auch
    wenn ein späterer Tick (Bot bewegt sich) wieder freie Sicht hätte."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    _make_nav(bot, [_wall_box(cx=1.0)])
    make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=1)
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)
    # Bot "bewegt" sich seitlich weg von der Wand — vorher hätte die (weiterhin
    # simulierte) Rakete hier wieder freie Sicht/Homing-Kurs bekommen können.
    bot.pos_x = 10.0; bot.pos_y = 10.0
    _resolve_incoming_shots(bot)
    assert not _was_killed(bot)


def test_gm_thin_wall_not_skipped_in_one_tick(bot):
    """Ein dünnes Hindernis, das komplett zwischen Start- und Endpunkt eines
    einzelnen Ticks liegt (keiner der beiden Endpunkte steckt in der Box), darf
    nicht übersprungen werden — der Segment-Test muss das ganze Tick-Segment
    prüfen, nicht nur die neue Position."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    _make_nav(bot, [_wall_box(cx=25.0, half_w=0.1)])
    # Großer dt-Sprung: Rakete fliegt von x=50 nach x=0 (durch die dünne Wand bei
    # x=25) in einem einzigen Tick.
    shot = make_shot(bot, pos=(50.0, 0.0, TANK_CZ), vel=(-2500.0, 0.0, 0.0),
                     is_gm=True, flag_abbr=b"GM", gm_target_pid=1)
    bot._resolve_incoming_shots(time.monotonic(), 0.02)
    assert not _was_killed(bot)
    assert (shot.shooter_id, shot.shot_id) not in bot._shots


def test_gm_no_wall_between_still_hits(bot):
    """Bestandsverhalten: ohne Hindernis auf der Flugbahn trifft die Rakete wie
    zuvor (Regressionsschutz gegen zu aggressive Wand-Terminierung)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    _make_nav(bot, [_wall_box(cx=100.0)])  # Wand weit abseits der Flugbahn
    make_shot(bot, pos=(1.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              is_gm=True, flag_abbr=b"GM", gm_target_pid=1)
    _resolve_incoming_shots(bot)
    assert _was_killed(bot)


# ── Steamroller ───────────────────────────────────────────────────────────────

def test_sr_kills_when_close(bot):
    """SR player within kill radius → bot reports steamrolled."""
    from bzflag.protocol import MsgKilled
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_player(bot, pid=3, pos=(5.0, 0.0, 0.0), flag="SR")
    bot._check_steamroller(time.monotonic())
    assert bot.client.send.called
    assert bot.client.send.call_args[0][0] == MsgKilled
    assert not bot.alive


def test_sr_no_kill_without_sr_flag(bot):
    """Player without SR flag touching bot → no kill."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_player(bot, pid=3, pos=(5.0, 0.0, 0.0), flag="")
    bot._check_steamroller(time.monotonic())
    bot.client.send.assert_not_called()


def test_sr_no_kill_when_too_far(bot):
    """SR player 50u away → outside kill radius → no kill."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_player(bot, pid=3, pos=(50.0, 0.0, 0.0), flag="SR")
    bot._check_steamroller(time.monotonic())
    bot.client.send.assert_not_called()


def test_sr_no_kill_when_stale(bot):
    """SR player nearby but last_seen > 1s ago → ignored."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    p = make_player(bot, pid=3, pos=(5.0, 0.0, 0.0), flag="SR")
    p.last_seen = time.monotonic() - 2.0   # 2s old
    bot._check_steamroller(time.monotonic())
    bot.client.send.assert_not_called()


def test_sr_no_kill_when_dead_player(bot):
    """SR player nearby but not alive → no kill."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_player(bot, pid=3, pos=(5.0, 0.0, 0.0), flag="SR", alive=False)
    bot._check_steamroller(time.monotonic())
    bot.client.send.assert_not_called()


def test_sr_no_kill_when_bot_already_dead(bot):
    """Bot already dead → SR check shouldn't produce a second kill."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.alive = False
    make_player(bot, pid=3, pos=(5.0, 0.0, 0.0), flag="SR")
    bot._check_steamroller(time.monotonic())
    bot.client.send.assert_not_called()


def test_bu_on_roof_not_overrun(bot):
    """Bot trägt BU, steht aber auf einem Dach (pos[2] >= 0) → nicht überrollbar
    durch normale (Nicht-SR-)Gegner — BU wirkt nur eingegraben."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 10.0
    bot.own_flag = "BU"
    make_player(bot, pid=3, pos=(5.0, 0.0, 10.0), flag="")
    bot._check_steamroller(time.monotonic())
    bot.client.send.assert_not_called()
    assert bot.alive


def test_bu_burrowed_overrun(bot):
    """Bot trägt BU und ist eingegraben (pos[2] < 0) → normaler naher Gegner
    überrollt ihn (BU-Sonderfall: Steamroller-Check gilt dann auch ohne SR)."""
    from bzflag.protocol import MsgKilled
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = -1.32
    bot.own_flag = "BU"
    make_player(bot, pid=3, pos=(5.0, 0.0, -1.32), flag="")
    bot._check_steamroller(time.monotonic())
    assert bot.client.send.called
    assert bot.client.send.call_args[0][0] == MsgKilled
    assert not bot.alive


def test_sr_kills_burrowed_bu_bot_too(bot):
    """SR-Verhalten unverändert: SR-Gegner überrollt auch einen eingegrabenen
    BU-Bot (Regression-Guard nach der pos[2]-Bedingung)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = -1.32
    bot.own_flag = "BU"
    make_player(bot, pid=3, pos=(5.0, 0.0, -1.32), flag="SR")
    bot._check_steamroller(time.monotonic())
    assert not bot.alive


# ── Phase 2 ──────────────────────────────────────────────────────────────────

class TestThiefFlagHit:
    """Fix J2: TH-Schuss (Thief) tötet Bot nicht — stiehlt nur Flagge (Sofort-Check in _on_shot_begin)."""

    def test_thief_shot_steals_flag_not_kills(self, bot):
        """Bot hält Flagge; TH-Schuss trifft → MsgTransferFlag gesendet, Bot lebt."""
        from bzflag.protocol import MsgTransferFlag, MsgKilled
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.own_flag = "GM"
        bot.alive = True
        bot.client.send.reset_mock()
        bot._on_shot_begin(0, make_th_shot_payload(shooter_id=2, shot_id=1))
        assert bot.alive is True
        assert bot.own_flag == "GM"  # own_flag bleibt bis Server-Bestätigung
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgTransferFlag in codes
        assert MsgKilled not in codes

    def test_thief_shot_no_flag_no_kill(self, bot):
        """Bot hat keine Flagge; TH-Schuss trifft → Bot lebt, keine Aktion."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.own_flag = ""
        bot.alive = True
        bot.client.send.reset_mock()
        bot._on_shot_begin(0, make_th_shot_payload(shooter_id=2, shot_id=1))
        assert bot.alive is True
        assert not bot.client.send.called

    def test_normal_shot_still_kills(self, bot):
        """Normaler Schuss trifft → Bot stirbt (Regression-Guard)."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.own_flag = ""
        bot.alive = True
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(1.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                  flag_abbr=b"\x00\x00")
        bot._resolve_incoming_shots(now, 0.1)
        assert bot.alive is False


# ── Phase 2 ──────────────────────────────────────────────────────────────

class TestPastClosestApproachSkip:
    """Fix RF1: Schuss der sich vom Bot wegbewegt triggert keinen Treffer."""

    def test_shot_moving_away_not_hit(self, bot):
        """Schuss bei (+50,0) bewegt sich in +X vom Bot (0,0) weg → kein Treffer."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.alive = True
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(100.0, 0.0, 0.0))
        _resolve_incoming_shots(bot)
        assert bot.alive is True

    def test_shot_approaching_still_hits(self, bot):
        """Schuss bei (-1,0) fliegt auf Bot (0,0) zu → Treffer korrekt erkannt."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.alive = True
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(-1.0, 0.0, 1.025), vel=(100.0, 0.0, 0.0))
        _resolve_incoming_shots(bot)
        assert bot.alive is False

    def test_bot_in_shot_wake_no_hit(self, bot):
        """RF-Szenario: Schuss bei (+3,0) schon vorbei, Bot liegt im Nachlauf der Trajektorie.
        Ohne Fix: _segment_point_dist3d=1.0 < HIT_RADIUS=5.616 → falsch positiv.
        Mit Fix: vel·rel = 100*3 > 0 → skip → kein Treffer."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.alive = True
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(3.0, 0.0, 1.025), vel=(100.0, 0.0, 0.0))
        _resolve_incoming_shots(bot)
        assert bot.alive is True


def test_report_killed_resets_dodging(bot):
    """_report_killed setzt _dodging=False (Fix S2: Konsistenz mit _on_killed/_report_steamrolled)."""
    make_shot(bot, shooter_id=2, shot_id=1,
              pos=(1.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
    bot._dodging = True
    bot.alive = True
    shot_obj = list(bot._shots.values())[0]
    bot._report_killed(shot_obj)
    assert bot._dodging is False


# ── Fix 11: SH (Shield) absorbiert Treffer ────────────────────────────────────

class TestShieldFlagHit:
    """SH-Flag: Bot überlebt ersten Treffer, droppt Flag statt zu sterben."""

    def test_sh_hit_survives_and_drops_flag(self, bot):
        """Bot hält SH; normaler Schuss trifft → alive=True, MsgDropFlag gesendet."""
        from bzflag.protocol import MsgDropFlag, MsgKilled
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.own_flag = "SH"
        bot.alive = True
        bot.good_flags.add("SH")
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(1.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                  flag_abbr=b"\x00\x00")
        bot._resolve_incoming_shots(now, 0.1)
        assert bot.alive is True
        assert bot.client.send.called
        code = bot.client.send.call_args[0][0]
        assert code == MsgDropFlag
        assert code != MsgKilled

    def test_sh_hit_no_report_killed(self, bot):
        """SH-Treffer sendet kein MsgKilled."""
        from bzflag.protocol import MsgKilled
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.own_flag = "SH"
        bot.alive = True
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(1.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._resolve_incoming_shots(now, 0.1)
        for call in bot.client.send.call_args_list:
            assert call[0][0] != MsgKilled

    def test_th_shot_takes_priority_over_sh(self, bot):
        """TH-Schuss hat Vorrang vor SH: MsgTransferFlag gesendet, Bot lebt."""
        from bzflag.protocol import MsgTransferFlag, MsgKilled
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.own_flag = "SH"
        bot.alive = True
        bot.client.send.reset_mock()
        bot._on_shot_begin(0, make_th_shot_payload(shooter_id=2, shot_id=1))
        assert bot.alive is True
        assert bot.own_flag == "SH"  # own_flag bleibt bis Server-Bestätigung
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgTransferFlag in codes
        assert MsgKilled not in codes

    def test_normal_hit_without_sh_still_kills(self, bot):
        """Ohne SH: normaler Treffer tötet Bot (Regression-Guard)."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.own_flag = ""
        bot.alive = True
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(1.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._resolve_incoming_shots(now, 0.1)
        assert bot.alive is False


# ── Stall-Segmente (F4: kein Geister-Durchflug) ──────────────────────────

class TestStallSegmentCrossing:
    """F4: Ein Segment, das den Tank innerhalb eines (langen) Ticks DURCHquert,
    muss treffen — der Wegbewegen-Skip darf nur greifen, wenn sich schon der
    Segment-ANFANG entfernt (sonst Geister-Durchflug bei Stalls)."""

    def test_segment_crossing_tank_hits(self, bot):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.own_flag = ""
        now = time.monotonic()
        # Vor 0,15s bei x=10 abgefeuert, jetzt bei x=-5: hat den Tank durchquert,
        # der Endpunkt entfernt sich bereits → der alte Endpunkt-Skip verwarf
        # das Segment und der Schuss traf nie.
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(10.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                  fire_time=now - 0.15)
        bot._resolve_incoming_shots(now, 0.5)
        assert bot.alive is False


# ── Cleanup abgelaufener Schüsse (F3: _ricochet_paths-Leak) ──────────────

class TestCleanupShots:
    """_cleanup_shots muss abgelaufene Schüsse aus BEIDEN Dicts entfernen.

    _resolve_incoming_shots räumt nur solange self.alive — läuft der Schuss
    während Tod/Respawn ab, ist _cleanup_shots die einzige Aufräumstelle.
    Ohne den _ricochet_paths-Pop wachsen Speicher und
    _find_incoming_shot-Scan mit der Uptime (Server-CPU-Degradation).
    """

    def test_expired_shot_removed_from_both_dicts(self, bot):
        now = time.monotonic()
        s = make_shot(bot, shooter_id=2, shot_id=7,
                      pos=(200.0, 0.0, 1.0), vel=(-100.0, 0.0, 0.0),
                      lifetime=3.5, fire_time=now - 10.0)
        key = (2, 7)
        bot._ricochet_paths[key] = [((200.0, 0.0, 1.0), (0.0, 0.0, 1.0),
                                     s.fire_time, s.fire_time + 2.0)]
        assert s.is_expired(now)
        bot._cleanup_shots(now)
        assert key not in bot._shots
        assert key not in bot._ricochet_paths

    def test_active_shot_untouched(self, bot):
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=8,
                  pos=(200.0, 0.0, 1.0), vel=(-100.0, 0.0, 0.0),
                  lifetime=3.5, fire_time=now)
        key = (2, 8)
        bot._ricochet_paths[key] = [((200.0, 0.0, 1.0), (0.0, 0.0, 1.0),
                                     now, now + 2.0)]
        bot._cleanup_shots(now)
        assert key in bot._shots
        assert key in bot._ricochet_paths

    def test_cleanup_while_dead(self, bot):
        """Kernszenario des Leaks: Bot ist tot, Schuss läuft ab."""
        bot.alive = False
        now = time.monotonic()
        make_shot(bot, shooter_id=3, shot_id=9,
                  pos=(200.0, 0.0, 1.0), vel=(-100.0, 0.0, 0.0),
                  lifetime=3.5, fire_time=now - 10.0)
        key = (3, 9)
        bot._ricochet_paths[key] = [((200.0, 0.0, 1.0), (0.0, 0.0, 1.0),
                                     now - 10.0, now - 8.0)]
        bot._cleanup_shots(now)
        assert key not in bot._shots
        assert key not in bot._ricochet_paths
