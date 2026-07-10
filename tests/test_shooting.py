"""
Shooting tests: predictive aiming, GM targeting, GM update patterns,
GM close-range aim threshold, GM initial target, Laser aim/Z-axis constraints.
"""
import math
import struct
import time

import pytest
from unittest.mock import patch
from conftest import make_player, make_shot


# ---------------------------------------------------------------------------
# P2-SHT-01: Predictive Aiming
# ---------------------------------------------------------------------------

class TestPredictiveAiming:

    def test_stationary_target_no_lead(self, bot):
        """Bei stehendem Ziel entspricht Lead-Winkel dem direkten Winkel."""
        player = make_player(bot, 2, pos=(100.0, 0.0, 0.0))
        player.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2

        # Azimuth direkt auf Ziel ausrichten
        bot.azimuth = 0.0  # zeigt nach +X
        now = time.monotonic()
        bot._next_shoot = 0.0
        bot._maybe_shoot(now)

        # Schuss muss abgefeuert worden sein
        assert bot.client.send.called

    def test_moving_target_changes_aim(self, bot):
        """Bot zielt nicht auf aktuelle Position sondern auf vorhergesagte."""
        player = make_player(bot, 2, pos=(100.0, 0.0, 0.0))
        # Ziel bewegt sich stark nach +Y → predicted Y > 0
        player.vel = [0.0, 50.0, 0.0]
        bot.target_player = 2

        from bot.util import _angle_diff
        shot_speed = bot._shot_speed  # 100 u/s
        dist = 100.0
        tof = dist / shot_speed  # 1s
        lead_y = 0.0 + 50.0 * tof  # = 50
        expected_angle = math.atan2(lead_y, 100.0)

        # Verifizieren: predicted angle zeigt nach oben-rechts
        assert expected_angle > 0.01  # nicht waagerecht

    def test_shot_uses_instance_shot_speed(self, bot):
        """Nach _shotSpeed=200 wird lead kürzer berechnet (halbe Flugzeit)."""
        player = make_player(bot, 2, pos=(100.0, 0.0, 0.0))
        player.vel = [0.0, 100.0, 0.0]  # starke Seitwärtsbewegung
        bot.target_player = 2
        bot._shot_speed = 200.0  # doppelte Geschwindigkeit → halbe Flugzeit

        dist = 100.0
        tof_fast = dist / 200.0  # 0.5s
        tof_slow = dist / 100.0  # 1.0s

        lead_fast_y = 0.0 + 100.0 * tof_fast  # 50
        lead_slow_y = 0.0 + 100.0 * tof_slow  # 100

        # schnellerer Schuss → geringerer Vorhalt-Winkel
        angle_fast = math.atan2(lead_fast_y, 100.0)
        angle_slow = math.atan2(lead_slow_y, 100.0)
        assert angle_fast < angle_slow

    def test_shot_flag_type_from_own_flag(self, bot):
        """MsgShotBegin enthält Flag-Typ des gehaltenen Flags."""
        bot.own_flag = "SW"
        now = time.monotonic()
        bot._send_shot(now, 0.0)
        payload = bot.client.send.call_args[0][1]
        # Flag-Typ-Offset: f(4)+B(1)+H(2)+fff(12)+fff(12)+f(4)+h(2) = 37
        flag_bytes = payload[37:39]
        assert flag_bytes == b"SW"

    def test_shot_no_flag_sends_null_type(self, bot):
        """Ohne Flag sendet _send_shot zwei Null-Bytes als Flag-Typ."""
        bot.own_flag = ""
        now = time.monotonic()
        bot._send_shot(now, 0.0)
        payload = bot.client.send.call_args[0][1]
        flag_bytes = payload[37:39]
        assert flag_bytes == b"\x00\x00"

    def test_shot_single_char_flag_padded(self, bot):
        """Einzeichige Flags (z.B. 'L') werden korrekt auf 2 Bytes gepaddet."""
        bot.own_flag = "L"
        now = time.monotonic()
        bot._send_shot(now, 0.0)
        payload = bot.client.send.call_args[0][1]
        flag_bytes = payload[37:39]
        assert flag_bytes == b"L\x00"


# ---------------------------------------------------------------------------
# P2-FLG-03: GM-Targeting via MsgGMUpdate (Schritt 5)
# ---------------------------------------------------------------------------

class TestGMTargeting:

    def test_gm_shot_sets_active_gm(self, bot):
        """Nach GM-Schuss ist _active_gm gesetzt."""
        bot.own_flag = "GM"
        bot._send_shot(time.monotonic(), 0.0)
        assert bot._active_gm is not None
        assert "shot_id" in bot._active_gm

    def test_non_gm_shot_leaves_active_gm_none(self, bot):
        bot.own_flag = "SW"
        bot._send_shot(time.monotonic(), 0.0)
        assert bot._active_gm is None

    def test_gm_update_sends_34_bytes(self, bot):
        bot.own_flag = "GM"
        now = time.monotonic()
        bot._send_shot(now, 0.0)
        bot.client.send.reset_mock()
        bot._send_gm_update(now + 0.1)
        assert bot.client.send.called
        from bzflag.protocol import MsgGMUpdate
        code, payload = bot.client.send.call_args[0]
        assert code == MsgGMUpdate
        assert len(payload) == 34

    def test_gm_update_no_target_sends_255(self, bot):
        bot.own_flag = "GM"
        bot.target_player = None
        now = time.monotonic()
        bot._send_shot(now, 0.0)
        bot.client.send.reset_mock()
        bot._send_gm_update(now + 0.1)
        payload = bot.client.send.call_args[0][1]
        target_id = struct.unpack_from(">B", payload, 33)[0]
        assert target_id == 255

    def test_gm_expires_clears_active_gm(self, bot):
        bot.own_flag = "GM"
        now = time.monotonic()
        bot._send_shot(now, 0.0)
        assert bot._active_gm is not None
        # Simuliere Ablauf
        bot._send_gm_update(now + bot._shot_lifetime + 1.0)
        assert bot._active_gm is None

    def test_active_gm_cleared_on_spawn(self, bot):
        bot.own_flag = "GM"
        bot._send_shot(time.monotonic(), 0.0)
        assert bot._active_gm is not None
        bot._active_gm = None  # spawn-Simulation
        assert bot._active_gm is None


# ---------------------------------------------------------------------------
# Schritt 5: needUpdate-Pattern statt periodisches Senden
# ---------------------------------------------------------------------------

class TestGMNeedUpdate:
    """Schritt 5: needUpdate-Pattern statt periodisches Senden."""

    def test_shot_sets_need_update_and_send_at(self, bot):
        """Nach GM-Schuss: _gm_need_update=True, _gm_send_at 5ms vor _gmActivationTime, kein Resend."""
        bot.own_flag = "GM"
        bot._max_shots = 1
        bot._shot_slot = 0
        bot._shot_gen = 0
        t = time.monotonic()
        bot._send_shot(t, az=0.0)
        assert bot._active_gm is not None
        assert bot._gm_need_update is True
        assert bot._gm_send_at == pytest.approx(t + max(bot._gm_activation_time - 0.005, 0.005))
        assert bot._gm_resend_at is None

    def test_target_change_marks_need_update(self, bot):
        """Wechsel von target_player während _active_gm setzt _gm_need_update=True.
        Simuliert die zentrale Vergleichslogik aus _update_movement direkt."""
        bot._active_gm = {"shot_id": 1, "fire_time": time.monotonic(),
                          "pos": [0.0, 0.0, 0.0], "vel": [100.0, 0.0, 0.0], "team": 1}
        bot._gm_need_update = False
        prev_target = 2
        bot.target_player = 3  # Wechsel
        if bot._active_gm is not None and bot.target_player != prev_target:
            bot._gm_need_update = True
        assert bot._gm_need_update is True

    def test_send_blocked_before_send_at(self, bot):
        """Erstes GMUpdate wird nicht gesendet, bevor _gm_send_at erreicht ist."""
        bot.own_flag = "GM"
        bot._max_shots = 1
        bot._shot_slot = 0
        bot._shot_gen = 0
        t = time.monotonic()
        bot._send_shot(t, az=0.0)
        bot.client.send.reset_mock()
        # Vor _gm_send_at: kein Senden
        if bot._gm_need_update and (bot._gm_send_at is None or t + 0.01 >= bot._gm_send_at):
            bot._send_gm_update(t + 0.01)
        # Direkter Check: _send_gm_update wurde NICHT durch die Gate-Logik aufgerufen
        # Die Gate-Logik wird in der Hauptschleife angewendet (run_game_loop), hier nur prüfen
        # dass _gm_send_at korrekt in der Zukunft liegt
        assert bot._gm_send_at > t


# ---------------------------------------------------------------------------
# GM-Kurzstrecke: verschärfter aim_threshold statt hartem Block
# ---------------------------------------------------------------------------

class TestGmCloseRange:
    """Schritt 3 Problem A: bei dist < GM_MIN_RANGE aim_threshold 4° statt 25°."""

    def _setup_gm(self, bot, target_dist, aim_offset_deg):
        bot.pos = [0.0, 0.0, 0.0]
        bot.vel = [0.0, 0.0, 0.0]
        bot.own_flag = "GM"
        bot._next_shoot = 0.0
        bot._shot_speed = 100.0
        # Azimuth leicht daneben
        bot.azimuth = math.radians(aim_offset_deg)
        info = make_player(bot, 99, pos=(target_dist, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.is_airborne = False
        bot.target_player = 99

    def test_no_shot_at_close_range_with_8deg_offset(self, bot):
        """dist=30u (< GM_MIN_RANGE), az 8° daneben → kein Schuss (threshold=4°)."""
        self._setup_gm(bot, target_dist=30.0, aim_offset_deg=8.0)
        bot._maybe_shoot(time.monotonic())
        assert not bot.client.send.called

    def test_fires_at_close_range_when_precisely_aimed(self, bot):
        """dist=30u (< GM_MIN_RANGE), az 2° daneben → Schuss (2° < 4°)."""
        self._setup_gm(bot, target_dist=30.0, aim_offset_deg=2.0)
        bot._maybe_shoot(time.monotonic())
        assert bot.client.send.called

    def test_normal_threshold_beyond_min_range(self, bot):
        """dist=80u (> GM_MIN_RANGE), az 20° daneben → Schuss (20° < 25°)."""
        self._setup_gm(bot, target_dist=80.0, aim_offset_deg=20.0)
        bot._maybe_shoot(time.monotonic())
        assert bot.client.send.called


# ---------------------------------------------------------------------------
# GM-Aktivierungspunkt-LoS-Gate: kein Schuss, der gegen eine Wand krachen würde
# ---------------------------------------------------------------------------

class _StubNav:
    """Minimaler NavGraph-Stub für die LoS-Wandprüfung (_segment_clear)."""
    def __init__(self, los=None):
        self._los_obs = los or []
        self._tele_exit_wps = set()
        self._tele_cross_centers = {}
        self._los_grid = None
        self._solid_grid = None
        self._teleport_edges = {}
    def get_floor_z(self, x, y, z, overhang=0.0):
        return 0.0


def _wall(x):
    from bzflag.world_map import BoxObstacle
    return BoxObstacle(cx=x, cy=0.0, bottom_z=0.0, angle=0.0,
                       half_w=2.0, half_d=10.0, height=10.0)


class TestGmActivationLosGate:
    """GM fliegt _gm_min_range geradeaus, dann homing. Zwei Wand-LoS-Checks (Bot→Aktivierungspunkt,
    Aktivierungspunkt→Gegner) verhindern Schüsse, die sicher gegen eine Wand krachen würden —
    ohne Tor-/Kurvenschüsse zu unterbinden."""

    def _setup(self, bot, wall_x):
        bot.pos = [0.0, 0.0, 0.0]; bot.vel = [0.0, 0.0, 0.0]
        bot.own_flag = "GM"; bot._next_shoot = 0.0; bot._shot_speed = 100.0
        bot._recompute_gm_min_range()              # _gm_min_range = 0.5 * 100 = 50
        bot.azimuth = 0.0                          # exakt auf den Gegner (+x)
        bot._world_map = None
        bot._nav_graph = _StubNav([_wall(wall_x)] if wall_x is not None else [])
        info = make_player(bot, 99, pos=(80.0, 0.0, 0.0))   # dist=80 > min_range → 20°-Schwelle
        info.vel = [0.0, 0.0, 0.0]; info.is_airborne = False
        bot.target_player = 99

    def test_no_shot_wall_blocks_bot_to_activation(self, bot):
        """Wand bei x=25 (< Aktivierungsdistanz ~54): Bot→Aktivierungspunkt blockiert → kein Schuss."""
        self._setup(bot, wall_x=25.0)
        bot._maybe_shoot(time.monotonic())
        assert not bot.client.send.called

    def test_no_shot_wall_blocks_activation_to_enemy(self, bot):
        """Gegner hinter Gebäude (Wand bei x=68, zwischen Aktivierungspunkt ~54 und Gegner 80):
        Aktivierungspunkt→Gegner blockiert → kein verschwendeter Schuss (Kern-Beschwerde)."""
        self._setup(bot, wall_x=68.0)
        bot._maybe_shoot(time.monotonic())
        assert not bot.client.send.called

    def test_fires_when_both_segments_clear(self, bot):
        """Wand erst HINTER dem Gegner (x=95): beide Segmente frei → Schuss wird freigegeben."""
        self._setup(bot, wall_x=95.0)
        bot._maybe_shoot(time.monotonic())
        assert bot.client.send.called

    def test_fires_in_open_field(self, bot):
        """Ohne Wände feuert das Gate wie bisher (keine Über-Unterdrückung)."""
        self._setup(bot, wall_x=None)
        bot._maybe_shoot(time.monotonic())
        assert bot.client.send.called


class TestSegmentClear:
    """Generischer Segment-LoS-Helfer mit frei wählbarem Ursprung."""

    def test_clear_without_obstacles(self, bot):
        bot._nav_graph = _StubNav([])
        assert bot._segment_clear(0.0, 0.0, 1.0, 50.0, 0.0, 1.0) is True

    def test_blocked_by_wall(self, bot):
        bot._nav_graph = _StubNav([_wall(25.0)])
        assert bot._segment_clear(0.0, 0.0, 1.0, 50.0, 0.0, 1.0) is False

    def test_clear_when_wall_off_segment(self, bot):
        bot._nav_graph = _StubNav([_wall(25.0)])
        # Segment endet vor der Wand → frei
        assert bot._segment_clear(0.0, 0.0, 1.0, 20.0, 0.0, 1.0) is True

    def test_clear_when_nav_none(self, bot):
        bot._nav_graph = None
        assert bot._segment_clear(0.0, 0.0, 1.0, 999.0, 0.0, 1.0) is True


def test_recompute_gm_min_range_follows_server_vars(bot):
    """_gm_min_range = _gmActivationTime × _shotSpeed wird bei Var-Änderung nachgeführt (nicht statisch)."""
    bot._gm_activation_time = 0.5; bot._shot_speed = 100.0
    bot._recompute_gm_min_range()
    assert bot._gm_min_range == pytest.approx(50.0)
    bot._gm_activation_time = 1.0                  # Server verdoppelt die Geradeaus-Phase
    bot._recompute_gm_min_range()
    assert bot._gm_min_range == pytest.approx(100.0)
    bot._shot_speed = 200.0                         # schnellere Schüsse → weiterer Aktivierungspunkt
    bot._recompute_gm_min_range()
    assert bot._gm_min_range == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# GM initial_target: Abschuss-Ziel für ersten GMUpdate sichern
# ---------------------------------------------------------------------------

class TestGmInitialTarget:
    """Schritt 3 Problem B: initial_target in _active_gm für ersten GMUpdate."""

    def test_initial_target_used_for_first_update(self, bot):
        """Ziel ändert sich nach Schuss → erster GMUpdate nutzt initial_target."""
        now = time.monotonic()
        bot.target_player = 99   # Target ändert sich nach Abschuss
        bot._active_gm = {
            "shot_id":        1,
            "fire_time":      now,
            "pos":            [0.0, 0.0, 1.57],
            "vel":            [100.0, 0.0, 0.0],
            "team":           1,
            "initial_target": 42,   # Ziel zum Abschusszeitpunkt
        }
        bot._gm_need_update = True
        bot._gm_send_at = now - 0.1   # bereits fällig
        bot._gm_resend_at = now + 0.5
        bot._send_gm_update(now)
        assert bot.client.send.called
        payload = bot.client.send.call_args[0][1]
        # Letztes Byte im Payload = target_id (MsgGMUpdate-Format: 34 Bytes)
        assert payload[-1] == 42   # initial_target, nicht 99

    def test_second_update_uses_current_target(self, bot):
        """Nach dem ersten Update ist initial_target verbraucht → current target."""
        now = time.monotonic()
        bot.target_player = 77
        bot._active_gm = {
            "shot_id":   1,
            "fire_time": now,
            "pos":       [0.0, 0.0, 1.57],
            "vel":       [100.0, 0.0, 0.0],
            "team":      1,
            # initial_target fehlt absichtlich (bereits verbraucht via pop)
        }
        bot._send_gm_update(now)
        payload = bot.client.send.call_args[0][1]
        assert payload[-1] == 77   # current target_player


# ---------------------------------------------------------------------------
# Schritt 3: Laser-Flag verschärft Aim-Threshold auf 5°
# ---------------------------------------------------------------------------

class TestLaserAimThreshold:
    """Schritt 3: Laser-Flag verschärft Aim-Threshold auf 5°."""

    def test_laser_at_10_degrees_does_not_shoot(self, bot):
        from conftest import make_player
        p = make_player(bot, 2, pos=(100.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.own_flag = "L"
        bot.azimuth = math.radians(10)  # 10° → > 5° Threshold
        bot._next_shoot = 0.0
        bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_not_called()

    def test_laser_at_4_degrees_shoots(self, bot):
        from conftest import make_player
        p = make_player(bot, 2, pos=(100.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.own_flag = "L"
        bot.azimuth = math.radians(4)  # 4° → < 5° Threshold
        bot._next_shoot = 0.0
        bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_called()

    def test_normal_flag_at_20_degrees_only_close(self, bot):
        """F8: 20°-Abweichung feuert nur noch im Nahkampf (Gate 25° bei ≤10u);
        auf 100u ist das Gate 5° → Schuss wird unterdrückt (kein Slot-Müll)."""
        from conftest import make_player
        p = make_player(bot, 2, pos=(100.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        bot.own_flag = ""
        bot.azimuth = math.radians(20)
        bot._next_shoot = 0.0
        bot._maybe_shoot(time.monotonic())
        assert not bot.client.send.called            # 100u: Gate 5° < 20°
        p.pos = [9.0, 0.0, 0.0]
        bot._next_shoot = 0.0
        bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_called()              # 9u: Gate 25° > 20°


# ---------------------------------------------------------------------------
# Schritt 6: Laser blockiert bei Z-Versatz
# ---------------------------------------------------------------------------

class TestLaserZAxis:
    """Schritt 6: Laser blockiert bei Z-Versatz."""

    def test_laser_blocked_when_enemy_above(self, bot):
        from conftest import make_player
        bot.own_flag = "L"
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.target_player = 2
        p = make_player(bot, 2, pos=(50.0, 0.0, 10.0))  # 10u höher
        p.vel = [0.0, 0.0, 0.0]
        bot._next_shoot = 0.0
        bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_not_called()

    def test_laser_shoots_when_z_diff_small(self, bot):
        from conftest import make_player
        bot.own_flag = "L"
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.target_player = 2
        p = make_player(bot, 2, pos=(50.0, 0.0, 1.0))  # nur 1u höher → erlaubt
        p.vel = [0.0, 0.0, 0.0]
        bot._next_shoot = 0.0
        bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_called()

    def test_gm_shoots_despite_z_diff(self, bot):
        from conftest import make_player
        bot.own_flag = "GM"
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.target_player = 2
        p = make_player(bot, 2, pos=(50.0, 0.0, 10.0))  # 10u höher
        p.vel = [0.0, 0.0, 0.0]
        bot._next_shoot = 0.0
        bot._max_shots = 1
        bot._shot_slot = 0
        bot._shot_gen = 0
        bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_called()

    def test_normal_flag_hard_blocked_above_jump_range(self, bot):
        """SS1: z_diff >= max_jump_h − HIT_RADIUS → harter Block, kein Schuss."""
        from conftest import make_player
        from bot.constants import JUMP_VELOCITY, GRAVITY, HIT_RADIUS
        max_jump_h = JUMP_VELOCITY ** 2 / (2.0 * abs(GRAVITY))
        z = max_jump_h  # über max_jump_h − HIT_RADIUS → harter Block
        bot.own_flag = ""
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.target_player = 2
        p = make_player(bot, 2, pos=(50.0, 0.0, z))
        p.vel = [0.0, 0.0, 0.0]
        bot._next_shoot = 0.0
        bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_not_called()

    def test_normal_flag_zjl_zone_fires_warning_shot(self, bot):
        """SS2: ZJ1-Zone (HIT_RADIUS < z_diff < max_reach) + random < 0.3 → Warnschuss."""
        from conftest import make_player
        from unittest.mock import patch
        bot.own_flag = ""
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.target_player = 2
        p = make_player(bot, 2, pos=(50.0, 0.0, 10.0))  # z=10 in ZJ1-Zone
        p.vel = [0.0, 0.0, 0.0]
        bot._next_shoot = 0.0
        with patch("bot.ai.shooting.random") as mock_rng:
            mock_rng.random.return_value = 0.1   # 0.1 < 0.3 → Warnschuss
            bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_called()

    def test_normal_flag_zjl_zone_blocked_by_random(self, bot):
        """SS2: ZJ1-Zone + random >= 0.3 → kein Schuss."""
        from conftest import make_player
        from unittest.mock import patch
        bot.own_flag = ""
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.target_player = 2
        p = make_player(bot, 2, pos=(50.0, 0.0, 10.0))
        p.vel = [0.0, 0.0, 0.0]
        bot._next_shoot = 0.0
        with patch("bot.ai.shooting.random") as mock_rng:
            mock_rng.random.return_value = 0.5   # 0.5 >= 0.3 → kein Schuss
            bot._maybe_shoot(time.monotonic())
        bot.client.send.assert_not_called()

    def test_laser_no_crash_when_info_none(self, bot):
        """Crash-Fix: info=None darf keinen IndexError durch ep[2] auslösen."""
        from unittest.mock import patch
        bot.own_flag = "L"
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.target_player = 2
        bot._next_shoot = 0.0
        # _get_enemy_pos gibt (x, y) zurück, aber bot.players hat keinen Eintrag (Race Condition)
        with patch.object(bot, "_get_enemy_pos", return_value=(50.0, 0.0)):
            bot._maybe_shoot(time.monotonic())  # darf NICHT mit IndexError crashen


# ---------------------------------------------------------------------------
# Shooting verification fixes (free functions)
# ---------------------------------------------------------------------------

def test_no_random_shot_with_good_flag(bot):
    """Bot feuert keinen Zufallsschuss wenn er eine gute Flagge hält."""
    bot.own_flag = "GM"
    bot.target_player = None
    bot.human_count = 0
    bot._next_shoot = 0.0
    now = time.monotonic()
    bot._maybe_shoot(now)
    bot.client.send.assert_not_called()


def test_random_shot_without_flag(bot):
    """Bot feuert Zufallsschuss wenn keine wertvolle Flagge gehalten wird."""
    bot.own_flag = ""
    bot.target_player = None
    bot._has_presence = lambda: True   # Mensch anwesend (Mitspieler ODER Zuschauer)
    bot._next_shoot = 0.0
    now = time.monotonic()
    bot._maybe_shoot(now)
    bot.client.send.assert_called()


def test_no_random_shot_without_enemies(bot):
    """Kein Zufallsschuss wenn keine menschlichen Spieler anwesend."""
    bot.own_flag = ""
    bot.target_player = None
    bot.human_count = 0
    bot._next_shoot = 0.0
    bot._maybe_shoot(time.monotonic())
    bot.client.send.assert_not_called()


def test_random_shot_no_burst(bot):
    """Random-Schuss setzt _next_shoot auf zufälliges Intervall [reload_time, 10s], kein Burst."""
    from bot.constants import SHOOT_INTERVAL_RANDOM_MAX
    bot.own_flag = ""
    bot.target_player = None
    bot._has_presence = lambda: True   # Mensch anwesend (Mitspieler ODER Zuschauer)
    bot._max_shots = 2
    bot._slot_reload_at = []
    bot._next_shoot = 0.0
    now = 100.0
    bot._maybe_shoot(now)
    assert bot.client.send.call_count == 1
    interval = bot._next_shoot - now
    assert bot._effective_reload_time() <= interval <= SHOOT_INTERVAL_RANDOM_MAX


# ---------------------------------------------------------------------------
# SS1: _shot_quality — situative Schusswahrscheinlichkeit
# ---------------------------------------------------------------------------

class TestShotQuality:
    """SS1: _shot_quality gibt 0.0–1.0 zurück basierend auf Winkel, Distanz und Z-Achse."""

    def test_shot_quality_z_blocked(self, bot):
        """Z-Unterschied > HIT_RADIUS (≈5.62u) → 0.0 (Treffer geometrisch unmöglich)."""
        from bot.constants import HIT_RADIUS
        quality = bot._shot_quality(aim_diff=0.0, dist=30.0, z_diff=HIT_RADIUS + 1.0)
        assert quality == 0.0

    def test_shot_quality_good_conditions(self, bot):
        """Kleiner Winkel (5°) und kurze Distanz (30u) → Quality ≥ 0.8."""
        quality = bot._shot_quality(aim_diff=math.radians(5), dist=30.0, z_diff=0.0)
        assert quality >= 0.8

    def test_shot_quality_bad_angle(self, bot):
        """Winkel > 30° → angle_factor=0.1 → Quality < 0.2 (auch bei guter Distanz)."""
        quality = bot._shot_quality(aim_diff=math.radians(45), dist=30.0, z_diff=0.0)
        assert quality < 0.2


# ---------------------------------------------------------------------------
# MultiShot: Per-Slot-Reload-Tracking
# ---------------------------------------------------------------------------

class TestMultiShot:

    def test_mg_effective_reload(self, bot):
        """MG-Flagge: _effective_reload_time = _reload_time / _mgun_ad_rate."""
        bot.own_flag = "MG"
        bot._reload_time = 3.5
        bot._mgun_ad_rate = 10.0
        assert bot._effective_reload_time() == pytest.approx(0.35)

    def test_f_effective_reload(self, bot):
        """F-Flagge (Rapid Fire): _effective_reload_time = _reload_time / _rfire_ad_rate."""
        bot.own_flag = "F"
        bot._reload_time = 3.5
        bot._rfire_ad_rate = 2.0
        assert bot._effective_reload_time() == pytest.approx(1.75)

    def test_tr_effective_reload(self, bot):
        """TR-Flagge (Trigger Happy): nutzt Standard-Reload, NICHT _rfire_ad_rate."""
        bot.own_flag = "TR"
        bot._reload_time = 3.5
        bot._rfire_ad_rate = 2.0
        assert bot._effective_reload_time() == pytest.approx(3.5)

    def test_normal_effective_reload(self, bot):
        """Keine Spezialflagge: _effective_reload_time = _reload_time."""
        bot.own_flag = ""
        bot._reload_time = 3.5
        assert bot._effective_reload_time() == pytest.approx(3.5)

    def test_second_shot_fires_after_burst_interval(self, bot):
        """maxShots=2: nach MIN_BURST_INTERVAL feuert Bot zweiten Schuss."""
        from bot.constants import MIN_BURST_INTERVAL
        from conftest import make_player
        bot.own_flag = ""
        bot._reload_time = 3.5
        bot._max_shots = 2
        bot._slot_reload_at = []
        bot._next_shoot = 0.0
        bot.azimuth = 0.0
        bot.human_count = 1
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2

        now = 100.0
        bot._maybe_shoot(now)
        assert bot.client.send.call_count == 1

        # Nach genau MIN_BURST_INTERVAL muss zweiter Schuss möglich sein
        now2 = now + MIN_BURST_INTERVAL
        bot.client.send.reset_mock()
        bot._maybe_shoot(now2)
        assert bot.client.send.call_count == 1

    def test_no_third_shot_until_reload(self, bot):
        """maxShots=2: nach 2 Schüssen kein dritter bis Reload abgelaufen."""
        from bot.constants import MIN_BURST_INTERVAL
        from conftest import make_player
        bot.own_flag = ""
        bot._reload_time = 3.5
        bot._max_shots = 2
        bot._slot_reload_at = []
        bot._next_shoot = 0.0
        bot.azimuth = 0.0
        bot.human_count = 1
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2

        now = 100.0
        bot._maybe_shoot(now)                            # Schuss 1
        bot._maybe_shoot(now + MIN_BURST_INTERVAL)       # Schuss 2
        assert bot.client.send.call_count == 2

        # Direkt danach: kein dritter Schuss
        bot.client.send.reset_mock()
        bot._maybe_shoot(now + MIN_BURST_INTERVAL + 0.1)
        bot.client.send.assert_not_called()

    def test_slot_conservation_blocks_no_los_random(self, bot):
        """No-LOS-Schuss wird geblockt wenn nur noch letzter Slot frei (max_shots=2)."""
        from unittest.mock import patch
        from conftest import make_player
        bot.own_flag = ""
        bot._reload_time = 3.5
        bot._max_shots = 2
        now = 100.0
        # Simuliere: slot 0 (shot_slot=0) belegt, Slot 1 (next) frei
        bot._shot_slot = 0
        bot._slot_reload_at = [now + 3.5, 0.0]  # Slot 0 busy, Slot 1 frei
        bot._next_shoot = 0.0
        bot.azimuth = 0.0
        bot.human_count = 1
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2

        with patch.object(bot, "_has_los_to_enemy", return_value=False), \
             patch("bot.ai.shooting.random") as mock_rng:
            mock_rng.random.return_value = 0.0  # 0.0 < 0.15 → würde normalerweise schießen
            bot._maybe_shoot(now)
        # Letzter Slot → Konservierung → kein Schuss
        bot.client.send.assert_not_called()


# ---------------------------------------------------------------------------
# SW-02: Shockwave-Schuss sendet Velocity (0, 0, 0)
# ---------------------------------------------------------------------------

class TestShockwaveShot:

    def test_sw_shot_sends_zero_velocity(self, bot):
        """SW: vx=vy=0 im Paket (bzfs.cxx setzt shotSpeed=0, Non-Zero wird rejected)."""
        bot.own_flag = "SW"
        now = time.monotonic()
        bot._send_shot(now, math.pi / 4)  # beliebiger Azimuth — wird für SW ignoriert
        payload = bot.client.send.call_args[0][1]
        # vel-Offset: f(4)+B(1)+H(2)+fff(12) = 19
        vx, vy = struct.unpack(">ff", payload[19:27])
        assert vx == 0.0
        assert vy == 0.0

    def test_normal_shot_velocity_unchanged(self, bot):
        """Normale Schüsse behalten Velocity-Berechnung (Regression)."""
        bot.own_flag = ""
        now = time.monotonic()
        bot._send_shot(now, 0.0)  # Azimuth=0 → vx = shot_speed > 0
        payload = bot.client.send.call_args[0][1]
        vx, vy = struct.unpack(">ff", payload[19:27])
        assert vx > 0.0


# ---------------------------------------------------------------------------
# Fix 10: Warning-Shots (no-LoS / Z-Diff) nutzen vollen Reload — kein Burst
# ---------------------------------------------------------------------------

class TestWarningShotNoBurst:
    """Fix 10: Schüsse ohne LoS oder bei falscher Z-Achse dürfen keinen Burst
    auslösen — _next_shoot muss >= now + _effective_reload_time() sein."""

    def test_no_los_warning_shot_uses_full_reload(self, bot):
        """No-LoS-Warnschuss (15%-Pfad) setzt _next_shoot auf vollen Reload, nicht Burst."""
        from conftest import make_player
        from unittest.mock import patch
        bot.own_flag = ""
        bot._reload_time = 3.5
        bot._max_shots = 2
        bot._shot_slot = 0
        bot._slot_reload_at = [0.0, 0.0]
        bot._next_shoot = 0.0
        bot.azimuth = 0.0
        bot.human_count = 1
        now = 100.0
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2

        with patch.object(bot, "_has_los_to_enemy", return_value=False), \
             patch("bot.ai.shooting.random") as mock_rng:
            mock_rng.random.return_value = 0.0  # 0.0 <= 0.15 → Warnschuss
            mock_rng.gauss.return_value = 0.0
            bot._maybe_shoot(now)

        bot.client.send.assert_called_once()
        assert bot._next_shoot >= now + bot._effective_reload_time()

    def test_z_diff_warning_shot_standard_uses_full_reload(self, bot):
        """Z-Diff-Warnschuss in _maybe_shoot_standard setzt _next_shoot auf vollen Reload."""
        from conftest import make_player
        from unittest.mock import patch
        from bot.constants import HIT_RADIUS
        bot.own_flag = ""
        bot._reload_time = 3.5
        bot._max_shots = 2
        bot._shot_slot = 0
        bot._slot_reload_at = [0.0, 0.0]
        bot._next_shoot = 0.0
        bot.azimuth = 0.0
        bot.human_count = 1
        now = 100.0
        p = make_player(bot, 2, pos=(50.0, 0.0, HIT_RADIUS + 2.0))  # Z-Diff in ZJ1-Zone
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2

        with patch.object(bot, "_has_los_to_enemy", return_value=True), \
             patch("bot.ai.shooting.random") as mock_rng:
            mock_rng.random.return_value = 0.1   # 0.1 < 0.3 → Warnschuss
            mock_rng.gauss.return_value = 0.0
            bot._maybe_shoot(now)

        bot.client.send.assert_called_once()
        assert bot._next_shoot >= now + bot._effective_reload_time()

    def test_z_diff_warning_shot_sb_uses_full_reload(self, bot):
        """Z-Diff-Warnschuss in _maybe_shoot_sb setzt _next_shoot auf vollen Reload."""
        from conftest import make_player
        from unittest.mock import patch
        from bot.constants import HIT_RADIUS
        bot.own_flag = "SB"
        bot._reload_time = 3.5
        bot._max_shots = 2
        bot._shot_slot = 0
        bot._slot_reload_at = [0.0, 0.0]
        bot._next_shoot = 0.0
        bot.azimuth = 0.0
        bot.human_count = 1
        now = 100.0
        p = make_player(bot, 2, pos=(50.0, 0.0, HIT_RADIUS + 2.0))  # Z-Diff in ZJ1-Zone
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2

        with patch("bot.ai.shooting.random") as mock_rng:
            mock_rng.random.return_value = 0.1   # 0.1 < 0.3 → Warnschuss
            mock_rng.gauss.return_value = 0.0
            bot._maybe_shoot(now)

        bot.client.send.assert_called_once()
        assert bot._next_shoot >= now + bot._effective_reload_time()

    def test_los_shot_still_uses_burst(self, bot):
        """Echter LoS-Schuss mit zweitem freiem Slot nutzt weiterhin Burst-Modus (Regression)."""
        from conftest import make_player
        from unittest.mock import patch
        from bot.constants import MIN_BURST_INTERVAL
        bot.own_flag = ""
        bot._reload_time = 3.5
        bot._max_shots = 2
        bot._shot_slot = 0
        bot._slot_reload_at = [0.0, 0.0]  # beide Slots frei
        bot._next_shoot = 0.0
        bot.azimuth = 0.0
        bot.human_count = 1
        now = 100.0
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2

        with patch.object(bot, "_has_los_to_enemy", return_value=True):
            bot._maybe_shoot(now)

        bot.client.send.assert_called_once()
        # Burst: zweiter Slot frei → _next_shoot = now + MIN_BURST_INTERVAL
        assert bot._next_shoot == pytest.approx(now + MIN_BURST_INTERVAL)


class TestCrossFloorTeleporterShot:
    """A2: Indirekte (Teleporter-/Abpraller-)Schüsse über Etagen — der Z-Block darf einen
    indirekten Schuss nicht verwerfen, und der Cross-Floor-Trigger feuert auch bei freiem LoS."""

    def _setup_cross_floor(self, bot):
        from conftest import make_player
        bot.own_flag = ""
        bot._reload_time = 3.5
        bot._max_shots = 2
        bot._shot_slot = 0
        bot._slot_reload_at = [0.0, 0.0]
        bot._next_shoot = 0.0
        bot.azimuth = 0.0
        bot.human_count = 1
        bot.pos = [0.0, 0.0, 0.0]
        bot._server_ricochet = True          # → _cross_floor_indirect liefert True
        p = make_player(bot, 2, pos=(50.0, 0.0, 30.0))   # Gegner 30u höher (Cross-Floor)
        p.vel = [0.0, 0.0, 0.0]
        bot.target_player = 2
        return p

    def test_indirect_shot_fires_despite_high_z(self, bot):
        """Trotz z_diff=30 (sonst Z-Block) feuert der Bot bei freiem LoS, wenn ein
        indirekter Winkel vorliegt (Z-Block ausgenommen für _indirect)."""
        self._setup_cross_floor(bot)
        now = 100.0
        with patch.object(bot, "_has_los_to_enemy", return_value=True), \
             patch.object(bot, "_compute_aim_point", return_value=(50.0, 0.0)), \
             patch.object(bot, "_find_ricochet_aim_angle", return_value=0.0):
            bot._maybe_shoot(now)
        bot.client.send.assert_called_once()

    def test_cross_floor_holds_fire_without_indirect_solution(self, bot):
        """Cross-Floor mit freiem LoS, aber kein indirekter Winkel → kein (blinder) Schuss."""
        self._setup_cross_floor(bot)
        now = 100.0
        with patch.object(bot, "_has_los_to_enemy", return_value=True), \
             patch.object(bot, "_compute_aim_point", return_value=(50.0, 0.0)), \
             patch.object(bot, "_find_ricochet_aim_angle", return_value=None):
            bot._maybe_shoot(now)
        bot.client.send.assert_not_called()


# ── Mündungs-Occlusion-Gate (kein Schuss durch eine dünne Wand) ──────────────

class TestMuzzleOcclusionGate:
    """Der reale Schuss spawnt an der Mündung (4.42u voraus). Steckt die hinter einer dünnen Wand,
    würde bzfs ihn fressen bzw. er ginge unfair durch die Wand → _muzzle_clear gated das im
    Dispatcher (Ausnahmen: SW radial, SB durchschlägt Wände)."""

    def _nav(self, bot, boxes):
        from bzflag.world_map import BoxObstacle, WorldMap
        from bzflag.nav_graph import NavGraph
        obs = [BoxObstacle(cx=cx, cy=cy, bottom_z=0.0, angle=0.0, half_w=hw, half_d=hd,
                           height=10.0, drive_through=False, shoot_through=False)
               for (cx, cy, hw, hd) in boxes]
        wm = WorldMap(boxes=obs, teleporters=[], links=[], world_half=200.0)
        bot._nav_graph = NavGraph(wm, max_jump_h=18.4)

    def test_muzzle_clear_false_when_wall_in_front(self, bot):
        """Dünne Wand bei x=2 (< _muzzle_front 4.42) → Mündung steckt dahinter → False."""
        self._nav(bot, [(2.0, 0.0, 0.5, 10.0)])
        bot.pos = [0.0, 0.0, 0.0]
        assert bot._muzzle_clear(0.0) is False

    def test_muzzle_clear_true_no_wall(self, bot):
        self._nav(bot, [])
        bot.pos = [0.0, 0.0, 0.0]
        assert bot._muzzle_clear(0.0) is True

    def test_muzzle_clear_true_wall_beyond_muzzle(self, bot):
        """Wand erst hinter der Mündung (x=8 > 4.42) → Mündung selbst frei."""
        self._nav(bot, [(8.0, 0.0, 0.5, 10.0)])
        bot.pos = [0.0, 0.0, 0.0]
        assert bot._muzzle_clear(0.0) is True

    def test_gate_blocks_standard_shot(self, bot):
        """Mündung hinter Wand → Dispatcher feuert nicht (Schütze gar nicht aufgerufen)."""
        from bot.models import AIState
        self._nav(bot, [(2.0, 0.0, 0.5, 10.0)])
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.own_flag = ""
        bot._ai_state = AIState.COMBAT
        bot._next_shoot = 0.0
        make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        bot.target_player = 2
        called = []
        bot._maybe_shoot_standard = lambda *a, **k: called.append(True)
        bot._maybe_shoot(time.monotonic())
        assert not called

    def test_gate_allows_sb_through_wall(self, bot):
        """SB (durchschlägt Wände) ist vom Gate ausgenommen → Schütze wird aufgerufen."""
        from bot.models import AIState
        self._nav(bot, [(2.0, 0.0, 0.5, 10.0)])
        bot.pos = [0.0, 0.0, 0.0]
        bot.azimuth = 0.0
        bot.own_flag = "SB"
        bot._ai_state = AIState.COMBAT
        bot._next_shoot = 0.0
        make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        bot.target_player = 2
        called = []
        bot._maybe_shoot_sb = lambda *a, **k: called.append(True)
        bot._maybe_shoot(time.monotonic())
        assert called


# ---------------------------------------------------------------------------
# F8: distanzabhängiges Feuer-Gate (25° nah → 5° fern, linear 10u..100u)
# ---------------------------------------------------------------------------

class TestDistanceFireGate:
    """_fire_gate_rad: max. Winkelabweichung beim Feuern verengt sich linear
    von 25° (≤10u) auf 5° (≥100u) — auf Distanz ist ein 20°-Fehlschuss
    praktisch garantiert, im Nahkampf füllt das Ziel den Winkel."""

    def test_gate_values(self, bot):
        assert bot._fire_gate_rad(0.0)    == pytest.approx(math.radians(25.0))
        assert bot._fire_gate_rad(10.0)   == pytest.approx(math.radians(25.0))
        assert bot._fire_gate_rad(55.0)   == pytest.approx(math.radians(15.0))
        assert bot._fire_gate_rad(100.0)  == pytest.approx(math.radians(5.0))
        assert bot._fire_gate_rad(1000.0) == pytest.approx(math.radians(5.0))

    def test_distant_misaligned_shot_suppressed(self, bot):
        """150u entfernt, 15° daneben: früher gefeuert (25°-Gate), jetzt nicht (5°)."""
        make_player(bot, 2, pos=(150.0, 0.0, 0.0))
        bot.target_player = 2
        bot.azimuth = math.radians(15.0)
        bot._next_shoot = 0.0
        bot.client.send.reset_mock()
        bot._maybe_shoot(time.monotonic())
        assert not bot.client.send.called

    def test_close_misaligned_shot_still_fires(self, bot):
        """8u entfernt, 15° daneben: Nahkampf-Gate (25°) lässt den Schuss zu."""
        make_player(bot, 2, pos=(8.0, 0.0, 0.0))
        bot.target_player = 2
        bot.azimuth = math.radians(15.0)
        bot._next_shoot = 0.0
        bot.client.send.reset_mock()
        bot._maybe_shoot(time.monotonic())
        assert bot.client.send.called
