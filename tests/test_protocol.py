"""
Protocol-level tests: MsgSetVar physics variables, PS_FLAG_ACTIVE status,
shakeTimeout from MsgGameSettings, Burrow flag, and protocol verification fixes.
"""
import math
import struct
import time

import pytest
from conftest import make_player, make_shot, make_th_shot_payload


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _build_setvar(vars_: dict) -> bytes:
    """Baut einen MsgSetVar-Payload aus einem dict {name: value}."""
    items = b""
    for name, val in vars_.items():
        nb = name.encode("utf-8")
        vb = str(val).encode("utf-8")
        items += struct.pack(">B", len(nb)) + nb + struct.pack(">B", len(vb)) + vb
    return struct.pack(">H", len(vars_)) + items


def _fire_setvar(bot, vars_: dict):
    """Sendet einen MsgSetVar-Payload direkt an _on_set_var."""
    from bzflag.protocol import MsgSetVar
    bot._on_set_var(MsgSetVar, _build_setvar(vars_))


# ---------------------------------------------------------------------------
# P2-PRO-01: MsgSetVar – physikalische Variablen
# ---------------------------------------------------------------------------

class TestSetVarPhysics:

    def test_shot_speed_updated(self, bot):
        _fire_setvar(bot, {"_shotSpeed": "200.0"})
        assert bot._shot_speed == pytest.approx(200.0)

    def test_shot_range_updates_lifetime(self, bot):
        _fire_setvar(bot, {"_shotSpeed": "100.0", "_shotRange": "500.0"})
        assert bot._shot_range == pytest.approx(500.0)
        assert bot._shot_lifetime == pytest.approx(5.0)

    def test_shot_speed_recalculates_lifetime(self, bot):
        _fire_setvar(bot, {"_shotRange": "400.0"})
        _fire_setvar(bot, {"_shotSpeed": "200.0"})
        assert bot._shot_lifetime == pytest.approx(2.0)

    def test_tank_speed_updated(self, bot):
        _fire_setvar(bot, {"_tankSpeed": "50.0"})
        assert bot._tank_speed == pytest.approx(50.0)

    def test_tank_ang_vel_updated(self, bot):
        _fire_setvar(bot, {"_tankAngVel": "3.0"})
        assert bot._tank_turn_rate == pytest.approx(3.0)

    def test_wide_angle_ang_updated(self, bot):
        _fire_setvar(bot, {"_wideAngleAng": "2.5"})
        assert bot._wide_angle_ang == pytest.approx(2.5)

    def test_jump_velocity_updated(self, bot):
        _fire_setvar(bot, {"_jumpVelocity": "25.0"})
        assert bot._jump_velocity == pytest.approx(25.0)

    def test_gravity_updated(self, bot):
        _fire_setvar(bot, {"_gravity": "-20.0"})
        assert bot._gravity == pytest.approx(-20.0)

    def test_defaults_unchanged_without_setvar(self, bot):
        from bzbot import SHOT_SPEED_DEFAULT, TANK_SPEED, TANK_TURN_RATE, JUMP_VELOCITY, GRAVITY
        assert bot._shot_speed    == pytest.approx(SHOT_SPEED_DEFAULT)
        assert bot._tank_speed    == pytest.approx(TANK_SPEED)
        assert bot._tank_turn_rate == pytest.approx(TANK_TURN_RATE)
        assert bot._jump_velocity  == pytest.approx(JUMP_VELOCITY)
        assert bot._gravity        == pytest.approx(GRAVITY)

    def test_invalid_value_ignored(self, bot):
        old = bot._shot_speed
        _fire_setvar(bot, {"_shotSpeed": "not_a_number"})
        assert bot._shot_speed == pytest.approx(old)

    def test_zero_shot_speed_ignored(self, bot):
        old = bot._shot_speed
        _fire_setvar(bot, {"_shotSpeed": "0"})
        assert bot._shot_speed == pytest.approx(old)

    def test_negative_tank_speed_ignored(self, bot):
        old = bot._tank_speed
        _fire_setvar(bot, {"_tankSpeed": "-5"})
        assert bot._tank_speed == pytest.approx(old)

    def test_reload_time_still_works(self, bot):
        _fire_setvar(bot, {"_reloadTime": "2.5"})
        assert bot._reload_time == pytest.approx(2.5)

    def test_world_size_still_works(self, bot):
        _fire_setvar(bot, {"_worldSize": "800"})
        assert bot.world_half == pytest.approx(400.0)

    def test_multiple_vars_in_one_packet(self, bot):
        _fire_setvar(bot, {
            "_shotSpeed": "150.0",
            "_tankSpeed": "30.0",
            "_gravity": "-15.0",
        })
        assert bot._shot_speed   == pytest.approx(150.0)
        assert bot._tank_speed   == pytest.approx(30.0)
        assert bot._gravity      == pytest.approx(-15.0)


# ---------------------------------------------------------------------------
# P2-PRO-03: PS_FLAG_ACTIVE in MsgPlayerUpdate
# ---------------------------------------------------------------------------

class TestFlagActiveStatus:

    def test_no_flag_no_flag_active_bit(self, bot):
        from bzflag.protocol import PS_FLAG_ACTIVE
        bot.own_flag = ""
        bot._send_update()
        args = bot.client.send.call_args
        payload = args[0][1]
        # status ist bei Offset 5 (float ts=4 + uint8 id=1 = 5) → +4 = offset 9
        status = struct.unpack_from(">h", payload, 9)[0]
        assert not (status & PS_FLAG_ACTIVE)

    def test_with_flag_sets_flag_active_bit(self, bot):
        from bzflag.protocol import PS_FLAG_ACTIVE
        bot.own_flag = "GM"
        bot._send_update()
        args = bot.client.send.call_args
        payload = args[0][1]
        status = struct.unpack_from(">h", payload, 9)[0]
        assert status & PS_FLAG_ACTIVE

    def test_ps_alive_still_set(self, bot):
        from bzflag.protocol import PS_ALIVE
        bot.own_flag = "SW"
        bot._send_update()
        payload = bot.client.send.call_args[0][1]
        status = struct.unpack_from(">h", payload, 9)[0]
        assert status & PS_ALIVE

    def test_explosion_update_sets_exploding_clears_alive(self, bot):
        """Explodierender Bot sendet PS_EXPLODING mit gelöschtem Alive-Bit (kein [nr] beim Observer)."""
        import time as _t
        from bzflag.protocol import PS_ALIVE, PS_EXPLODING
        bot.alive = False
        bot._exploding_until = _t.monotonic() + 1.0
        bot.client.send.reset_mock()
        bot._send_update()
        payload = bot.client.send.call_args[0][1]
        status = struct.unpack_from(">h", payload, 9)[0]
        assert status & PS_EXPLODING
        assert not (status & PS_ALIVE)

    def test_dead_not_exploding_sends_nothing(self, bot):
        """Toter Bot ohne laufende Explosion schweigt (wie der echte Client) — kein Update."""
        bot.alive = False
        bot._exploding_until = 0.0
        bot.client.send.reset_mock()
        bot._send_update()
        assert bot.client.send.call_count == 0


# ---------------------------------------------------------------------------
# Schritte 1–5 (zweite Verifikationsrunde): ShakeTimeout
# ---------------------------------------------------------------------------

class TestShakeTimeout:
    """Schritt 1: shakeTimeout aus MsgGameSettings."""

    def _build_settings(self, shake_timeout_tenths: int) -> bytes:
        """30-Byte MsgGameSettings-Payload mit shakeTimeout an Offset 22."""
        return (
            struct.pack(">f", 400.0)         # worldSize
            + struct.pack(">H", 0)           # gameType
            + struct.pack(">H", 0)           # gameOptions
            + struct.pack(">H", 200)         # maxPlayerSlots
            + struct.pack(">H", 1)           # maxShots
            + struct.pack(">H", 0)           # numFlags
            + struct.pack(">f", 1.0)         # linearAcceleration
            + struct.pack(">f", 0.5)         # angularAcceleration
            + struct.pack(">H", shake_timeout_tenths)  # shakeTimeout
            + struct.pack(">H", 0)           # shakeWins
            + struct.pack(">I", 0)           # syncTime
        )

    def test_shake_timeout_15s(self, bot):
        bot._on_game_settings(self._build_settings(150))  # 15.0s
        assert bot._drop_bad_flag_delay == pytest.approx(15.0)

    def test_shake_timeout_zero_keeps_default(self, bot):
        old = bot._drop_bad_flag_delay
        bot._on_game_settings(self._build_settings(0))
        assert bot._drop_bad_flag_delay == pytest.approx(old)

    def test_shake_timeout_short_payload_ignored(self, bot):
        old = bot._drop_bad_flag_delay
        bot._on_game_settings(b"\x00" * 20)  # zu kurz
        assert bot._drop_bad_flag_delay == pytest.approx(old)


class TestJumpingGameOption:
    """MsgGameSettings gameOptions Bit 0x0008 (JumpingGameStyle) → _server_jumping."""

    def _build_settings(self, game_options: int) -> bytes:
        """8-Byte MsgGameSettings-Payload mit gameOptions an Offset 6."""
        return (
            struct.pack(">f", 400.0)             # worldSize
            + struct.pack(">H", 0)               # gameType
            + struct.pack(">H", game_options)    # gameOptions
        )

    def test_jumping_enabled_sets_flag(self, bot):
        bot._server_jumping = False
        bot._on_game_settings(self._build_settings(0x0008))
        assert bot._server_jumping is True

    def test_jumping_disabled_clears_flag(self, bot):
        bot._server_jumping = True
        bot._on_game_settings(self._build_settings(0x0000))
        assert bot._server_jumping is False

    def test_jumping_independent_of_ricochet(self, bot):
        bot._on_game_settings(self._build_settings(0x0020))  # nur Ricochet
        assert bot._server_ricochet is True
        assert bot._server_jumping is False


class TestBurrowFlag:
    """Schritt 2: Burrow-Flag reduziert Radar; verbietet Sprung."""

    def test_radar_full_when_no_burrow(self, bot):
        bot.own_flag = ""
        bot.pos = [0.0, 0.0, 0.0]
        from bzbot import RADAR_RANGE
        assert bot._effective_radar_range() == pytest.approx(RADAR_RANGE)

    def test_radar_full_when_burrow_above_ground(self, bot):
        bot.own_flag = "BU"
        bot.pos = [0.0, 0.0, 0.0]  # nicht unterirdisch
        from bzbot import RADAR_RANGE
        assert bot._effective_radar_range() == pytest.approx(RADAR_RANGE)

    def test_radar_reduced_when_burrowed(self, bot):
        bot.own_flag = "BU"
        bot.pos = [0.0, 0.0, -1.0]  # unterirdisch
        from bzbot import RADAR_RANGE
        assert bot._effective_radar_range() == pytest.approx(RADAR_RANGE * 0.25)

    def test_bu_sinks_to_burrow_depth(self, bot):
        """BU-Flagge: Tank sinkt durch Schwerkraft auf BURROW_DEPTH."""
        from bzbot_ai import BURROW_DEPTH
        bot.own_flag = "BU"
        bot.pos = [0.0, 0.0, 0.0]
        bot.vel = [0.0, 0.0, 0.0]
        now = time.monotonic()
        for _ in range(20):
            bot._run_physics(dt=0.1, now=now)
            now += 0.1
        assert bot.pos[2] < 0.0
        assert bot.pos[2] >= BURROW_DEPTH - 0.01

    def test_bu_drop_resets_z(self, bot):
        """_on_drop_flag bei BU: pos[2] wird auf 0 zurückgesetzt."""
        from bzflag.protocol import MsgDropFlag
        from bzbot_ai import BURROW_DEPTH
        bot.own_flag = "BU"
        bot.pos = [0.0, 0.0, BURROW_DEPTH]
        bot.vel = [0.0, 0.0, 0.0]
        bot.player_id = 1
        payload = struct.pack(">B", 1)  # pid = bot.player_id
        bot._on_drop_flag(MsgDropFlag, payload)
        assert bot.own_flag == ""
        assert bot.pos[2] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Verification fixes (protocol-related free functions)
# ---------------------------------------------------------------------------

def test_tank_turn_rate_default_is_pi_over_4(bot):
    """TANK_TURN_RATE Default ist π/4 ≈ 0.785 (aus BZFlag global.cxx)."""
    from bzbot import TANK_TURN_RATE
    assert TANK_TURN_RATE == pytest.approx(0.785398, rel=1e-4)


def test_max_shots_default_is_1(bot):
    """MAX_SHOTS_DEFAULT ist 1 (aus BZFlag CmdLineOptions.h)."""
    from bzbot import MAX_SHOTS_DEFAULT
    assert MAX_SHOTS_DEFAULT == 1


def test_flag_update_uses_57_byte_entries(bot):
    """_on_flag_update verarbeitet 57-Byte-Einträge korrekt."""
    from bzflag.protocol import MsgFlagUpdate
    # Manuell 57-Byte-Entry mit bekannter Position bauen
    flag_id = 3
    x, y, z = 42.0, -17.5, 0.0
    abbr_b = b"SW"
    status = 1  # onGround
    entry = (
        struct.pack(">H", flag_id)
        + abbr_b
        + struct.pack(">H", status)
        + struct.pack(">H", 0)
        + struct.pack(">B", 255)
        + struct.pack(">fff", x, y, z)
        + struct.pack(">fff", 0, 0, 0)  # launchPos
        + struct.pack(">fff", 0, 0, 0)  # landingPos
        + struct.pack(">f", 0.0) * 3    # flightTime + flightEnd + initVel
    )
    assert len(entry) == 57
    payload = struct.pack(">H", 1) + entry
    bot._on_flag_update(MsgFlagUpdate, payload)
    assert flag_id in bot.flags
    assert bot.flags[flag_id].pos[0] == pytest.approx(x)
    assert bot.flags[flag_id].pos[1] == pytest.approx(y)


def test_game_time_parses_uint64(bot):
    """_on_game_time parst uint64 Mikrosekunden korrekt (nicht als double)."""
    from bzflag.protocol import MsgGameTime
    # 1_000_000 µs = 1.0 Sekunden
    server_us = 1_000_000
    msb = (server_us >> 32) & 0xFFFFFFFF
    lsb = server_us & 0xFFFFFFFF
    payload = struct.pack(">II", msb, lsb)
    bot._on_game_time(MsgGameTime, payload)
    expected_offset = 1.0 - time.monotonic()
    assert bot._server_time_offset == pytest.approx(expected_offset, abs=0.5)


def test_grab_flag_sends_2_bytes(bot):
    """_try_grab_flag sendet MsgGrabFlag mit 2-Byte uint16 flag_id."""
    from bzflag.protocol import MsgGrabFlag
    bot._try_grab_flag(42)
    code, payload = bot.client.send.call_args[0]
    assert code == MsgGrabFlag
    assert len(payload) == 2
    assert struct.unpack(">H", payload)[0] == 42


# ---------------------------------------------------------------------------
# UDP-Handshake-Retry: MsgUDPLinkRequest wird wiederholt, bis udp_active.
# Hintergrund: der Stock-Client sendet den Request nur einmal; geht das eine UDP-Paket
# verloren, bleibt udp_active=False die ganze Session → der Bot schießt nie (Gate in
# _can_shoot, weil TCP-Shots gekickt werden). retry_udp_link() macht den Handshake robust.
# ---------------------------------------------------------------------------

class TestUdpLinkRetry:
    from unittest.mock import MagicMock as _MagicMock

    @pytest.fixture
    def client(self):
        from bzflag.client import BZFlagClient
        c = BZFlagClient(host="localhost", port=5154)
        c.player_id = 7
        c._udp_sock = self._MagicMock()
        return c

    def test_noop_when_udp_active(self, client):
        client.udp_active = True
        client._udp_link_sent_at = 0.0   # Throttle wäre abgelaufen → nur udp_active gatet
        client.retry_udp_link()
        client._udp_sock.sendto.assert_not_called()

    def test_noop_within_interval(self, client):
        client.udp_active = False
        client._udp_link_sent_at = time.monotonic()   # gerade erst gesendet → gedrosselt
        client.retry_udp_link()
        client._udp_sock.sendto.assert_not_called()

    def test_resends_after_interval(self, client):
        from bzflag.client import UDP_LINK_RETRY_INTERVAL
        client.udp_active = False
        client._udp_link_sent_at = time.monotonic() - UDP_LINK_RETRY_INTERVAL - 0.1
        before = client._udp_link_attempts
        client.retry_udp_link()
        client._udp_sock.sendto.assert_called_once()
        assert client._udp_link_attempts == before + 1
        assert client._udp_link_sent_at == pytest.approx(time.monotonic(), abs=0.5)

    def test_noop_without_socket(self, client):
        client.udp_active = False
        client._udp_sock = None
        client._udp_link_sent_at = 0.0
        client.retry_udp_link()   # darf nicht crashen (Früh-Return)

    def test_noop_without_player_id(self, client):
        client.udp_active = False
        client.player_id = None
        client._udp_link_sent_at = 0.0
        client.retry_udp_link()
        client._udp_sock.sendto.assert_not_called()

    def test_established_stops_retry(self, client):
        from bzflag.protocol import MsgUDPLinkEstablished
        client.udp_active = False
        client._h_udp_link_established(MsgUDPLinkEstablished, b"")
        assert client.udp_active is True
        client._udp_link_sent_at = 0.0   # Throttle abgelaufen → nur udp_active gatet
        client.retry_udp_link()
        client._udp_sock.sendto.assert_not_called()

    def test_send_payload_is_player_id(self, client):
        from bzflag.protocol import MsgUDPLinkRequest
        client._send_udp_link_request()
        client._udp_sock.sendto.assert_called_once()
        sent_data, addr = client._udp_sock.sendto.call_args[0]
        assert addr == ("localhost", 5154)
        # Paket: [uint16 len][uint16 code][payload]; payload = 1 Byte player_id
        length, code = struct.unpack_from(">HH", sent_data)
        assert code == MsgUDPLinkRequest
        assert sent_data[4:] == struct.pack(">B", 7)

    def test_warns_once_after_threshold(self, client, caplog):
        from bzflag.client import UDP_LINK_RETRY_INTERVAL, _UDP_LINK_WARN_AFTER
        import logging
        client.udp_active = False
        client._udp_link_attempts = _UDP_LINK_WARN_AFTER - 1   # nächster Versuch erreicht die Schwelle
        client._udp_link_sent_at = time.monotonic() - UDP_LINK_RETRY_INTERVAL - 0.1
        with caplog.at_level(logging.WARNING, logger="bzflag.client"):
            client.retry_udp_link()
        assert any("UDP-Handshake" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Order number tests (from TestNewFeatures)
# ---------------------------------------------------------------------------

def test_order_number_stale_packet_rejected(bot):
    """Veraltetes Positions-Paket (ältere Order-Nummer) wird ignoriert."""
    from conftest import make_player
    from bzflag.protocol import MsgPlayerUpdateSmall, PS_ALIVE
    p = make_player(bot, 2, pos=(10.0, 0.0, 0.0))
    p.last_order = 100
    payload = (
        struct.pack(">f", 0.0)
        + struct.pack(">B", 2)
        + struct.pack(">i", 50)       # ältere Order (50 < 100)
        + struct.pack(">h", PS_ALIVE)
        + struct.pack(">hhh", 500, 0, 0)  # neue "Position" → sollte ignoriert werden
    )
    bot._on_player_update_small(MsgPlayerUpdateSmall, payload)
    assert bot.players[2].pos[0] == pytest.approx(10.0)  # unverändert


def test_order_number_newer_packet_accepted(bot):
    """Neueres Paket (höhere Order-Nummer) wird akzeptiert."""
    from conftest import make_player
    from bzflag.protocol import MsgPlayerUpdateSmall, PS_ALIVE
    p = make_player(bot, 2, pos=(10.0, 0.0, 0.0))
    p.last_order = 50
    _SMALL_SCALE = 32766.0
    _SMALL_MAX_DIST = 0.02 * _SMALL_SCALE
    new_x_raw = int(100.0 * _SMALL_SCALE / _SMALL_MAX_DIST)
    payload = (
        struct.pack(">f", 0.0)
        + struct.pack(">B", 2)
        + struct.pack(">i", 100)      # neuere Order
        + struct.pack(">h", PS_ALIVE)
        + struct.pack(">hhh", new_x_raw, 0, 0)
    )
    bot._on_player_update_small(MsgPlayerUpdateSmall, payload)
    assert bot.players[2].pos[0] == pytest.approx(100.0, abs=1.0)


# ── Fix 13: MsgScoreOver (Rundenende) ────────────────────────────────────────

class TestScoreOver:
    """_on_score_over: Rundenende via Score-Limit — Bot explodiert (wie der echte Client via
    explodeTank, KEINE MsgKilled), bleibt sichtbar (Linger) und reconnectet. Beim Beitritt
    zwischen Runden (nie gespielt) wird NICHT reconnectet."""

    def test_score_over_alive_bot_explodes_and_reconnects(self, bot):
        """MsgScoreOver, Bot hat gespielt → alive=False, Explosion, reconnect+Linger, KEINE MsgKilled."""
        from bzflag.protocol import MsgScoreOver, MsgKilled
        bot.alive = True
        bot._has_spawned = True
        bot.client.send.reset_mock()
        bot._on_score_over(MsgScoreOver, bytes([2]))
        assert bot.alive is False
        assert bot._exploding_until > 0.0
        assert bot._reconnect_needed is True
        assert bot._round_over_until is not None
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgKilled not in codes

    def test_score_over_empty_payload_no_crash(self, bot):
        """Leeres Payload: kein Absturz, reconnect + Linger trotzdem gesetzt."""
        from bzflag.protocol import MsgScoreOver
        bot.alive = True
        bot._has_spawned = True
        bot._on_score_over(MsgScoreOver, b"")
        assert bot._reconnect_needed is True
        assert bot._round_over_until is not None

    def test_score_over_already_dead_no_killed(self, bot):
        """MsgScoreOver wenn Bot bereits tot (hat gespielt) → kein MsgKilled, aber reconnect."""
        from bzflag.protocol import MsgScoreOver, MsgKilled
        bot.alive = False
        bot._has_spawned = True
        bot.client.send.reset_mock()
        bot._on_score_over(MsgScoreOver, bytes([1]))
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgKilled not in codes
        assert bot._reconnect_needed is True

    def test_score_over_fresh_join_waits_no_reconnect(self, bot):
        """MsgScoreOver beim Beitritt zwischen Runden (nie gespielt) → kein Reconnect, nur _game_over."""
        from bzflag.protocol import MsgScoreOver
        bot.alive = False
        bot._has_spawned = False
        bot._on_score_over(MsgScoreOver, bytes([1]))
        assert bot._game_over is True
        assert bot._reconnect_needed is False
        assert bot._round_over_until is None


# ── Fix 18: MsgTimeUpdate Reconnect ──────────────────────────────────────────

class TestTimeUpdate:
    """_on_time_update: timeLeft>0 Runde aktiv (ggf. spawnen), =0 Rundenende (Explosion+Reconnect,
    keine MsgKilled), <0 Countdown-Pause (kein Rundenende)."""

    def test_time_up_alive_explodes_and_reconnects(self, bot):
        """timeLeft=0, Bot hat gespielt → alive=False, Explosion, reconnect+Linger, KEINE MsgKilled."""
        from bzflag.protocol import MsgTimeUpdate, MsgKilled
        bot.alive = True
        bot._has_spawned = True
        bot.client.send.reset_mock()
        payload = struct.pack("!i", 0)
        bot._on_time_update(MsgTimeUpdate, payload)
        assert bot.alive is False
        assert bot._exploding_until > 0.0
        assert bot._reconnect_needed is True
        assert bot._round_over_until is not None
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgKilled not in codes

    def test_time_up_already_dead_no_killed(self, bot):
        """timeLeft=0, Bot bereits tot (hat gespielt) → kein MsgKilled, aber reconnect."""
        from bzflag.protocol import MsgTimeUpdate, MsgKilled
        bot.alive = False
        bot._has_spawned = True
        bot.client.send.reset_mock()
        payload = struct.pack("!i", 0)
        bot._on_time_update(MsgTimeUpdate, payload)
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgKilled not in codes
        assert bot._reconnect_needed is True

    def test_time_up_fresh_join_waits_no_reconnect(self, bot):
        """timeLeft=0 beim Beitritt zwischen Runden (nie gespielt) → kein Reconnect, nur _game_over."""
        from bzflag.protocol import MsgTimeUpdate
        bot.alive = False
        bot._has_spawned = False
        bot._on_time_update(MsgTimeUpdate, struct.pack("!i", 0))
        assert bot._game_over is True
        assert bot._reconnect_needed is False
        assert bot._round_over_until is None

    def test_time_start_after_game_over_spawns(self, bot):
        """timeLeft>0 nach _game_over (Rundenstart) → MsgAlive (Spawn) gesendet, _game_over=False."""
        from bzflag.protocol import MsgTimeUpdate, MsgAlive
        bot.alive = False
        bot._game_over = True
        bot.death_time = None
        bot._spawn_sent_at = None
        bot.client.send.reset_mock()
        bot._on_time_update(MsgTimeUpdate, struct.pack("!i", 120))
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgAlive in codes
        assert bot._game_over is False

    def test_time_paused_no_round_over(self, bot):
        """timeLeft<0 (Countdown-Pause) → kein Rundenende/Reconnect."""
        from bzflag.protocol import MsgTimeUpdate
        bot.alive = True
        bot._has_spawned = True
        bot._on_time_update(MsgTimeUpdate, struct.pack("!i", -1))
        assert bot.alive is True
        assert not bot._reconnect_needed
        assert bot._round_over_until is None

    def test_time_remaining_no_action(self, bot):
        """timeLeft=30 (Runde läuft, kein vorheriges Game-Over) → keine Aktion."""
        from bzflag.protocol import MsgTimeUpdate
        bot.alive = True
        bot.client.send.reset_mock()
        payload = struct.pack("!i", 30)
        bot._on_time_update(MsgTimeUpdate, payload)
        assert bot.alive is True
        assert not bot._reconnect_needed

    def test_time_update_short_payload_ignored(self, bot):
        """Zu kurzes Payload → kein Absturz."""
        from bzflag.protocol import MsgTimeUpdate
        bot._on_time_update(MsgTimeUpdate, b"\x00\x00")
        assert bot.alive is True


# ── Fix 18: TH-Opfer MsgDropFlag ─────────────────────────────────────────────

def test_th_victim_sends_transfer_flag(bot):
    """TH-Treffer mit eigener Flagge → MsgTransferFlag gesendet, Bot lebt, own_flag bleibt."""
    from bzflag.protocol import MsgTransferFlag, MsgKilled
    bot.pos = [0.0, 0.0, 0.0]
    bot.own_flag = "GM"
    bot.alive = True
    bot.client.send.reset_mock()
    bot._on_shot_begin(0, make_th_shot_payload(shooter_id=3, shot_id=1))
    assert bot.alive is True
    assert bot.own_flag == "GM"  # bleibt bis Server-Bestätigung (MsgTransferFlag-Echo)
    codes = [call[0][0] for call in bot.client.send.call_args_list]
    assert MsgTransferFlag in codes
    assert MsgKilled not in codes


# ── Fix 18: Genocide-Opfer ────────────────────────────────────────────────────

class TestGenocideVictim:
    """_on_killed: Bot stirbt mit wenn Teamkamerad durch G getötet wird."""

    def _build_killed_broadcast(self, victim_id, killer_id, reason, shot_id, flag_abbr: bytes):
        """Baut MsgKilled-Broadcast-Payload (Server→Client-Format)."""
        return (
            struct.pack(">B", victim_id)
            + struct.pack(">B", killer_id)
            + struct.pack(">h", reason)
            + struct.pack(">h", shot_id)
            + flag_abbr
        )

    def test_genocide_kills_bot_when_teammate_dies(self, bot):
        """Teamkamerad durch G-Flagge getötet → Bot stirbt mit, sendet MsgKilled(GenocideEffect)."""
        from conftest import make_player
        from bzflag.protocol import MsgKilled as MsgKilledCode
        from bzbot_ai import AIState
        # Bot in Team 2
        bot.team = 2
        bot.alive = True
        bot.client.send.reset_mock()
        # Teamkamerad registrieren
        victim = make_player(bot, pid=5)  # team=2 per conftest
        assert victim.team == 2
        # Broadcast: Spieler 3 (killer) tötet Spieler 5 (victim) mit G-Flagge
        payload = self._build_killed_broadcast(
            victim_id=5, killer_id=3, reason=1, shot_id=7, flag_abbr=b"G\x00"
        )
        bot._on_killed(MsgKilledCode, payload)
        assert bot.alive is False
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgKilledCode in codes
        # Payload prüfen: reason=KILL_REASON_GENOCIDED=3
        killed_data = [c[0][1] for c in bot.client.send.call_args_list if c[0][0] == MsgKilledCode][0]
        reason = struct.unpack_from(">H", killed_data, 1)[0]
        assert reason == 3  # KILL_REASON_GENOCIDED

    def test_genocide_no_kill_when_rogue(self, bot):
        """Bot ist Rogue (team=0) → kein GenocideEffect."""
        from conftest import make_player
        from bzflag.protocol import MsgKilled as MsgKilledCode
        bot.team = 0  # Rogue
        bot.alive = True
        bot.client.send.reset_mock()
        victim = make_player(bot, pid=5)
        victim.team = 0  # auch Rogue
        payload = self._build_killed_broadcast(
            victim_id=5, killer_id=3, reason=1, shot_id=7, flag_abbr=b"G\x00"
        )
        bot._on_killed(MsgKilledCode, payload)
        assert bot.alive is True  # Rogue stirbt nicht durch Genocide

    def test_genocide_no_kill_different_team(self, bot):
        """Opfer in anderem Team → kein GenocideEffect für den Bot."""
        from conftest import make_player
        from bzflag.protocol import MsgKilled as MsgKilledCode
        bot.team = 1  # Red
        bot.alive = True
        bot.client.send.reset_mock()
        victim = make_player(bot, pid=5)
        victim.team = 2  # Green — anderes Team als Bot
        payload = self._build_killed_broadcast(
            victim_id=5, killer_id=3, reason=1, shot_id=7, flag_abbr=b"G\x00"
        )
        bot._on_killed(MsgKilledCode, payload)
        assert bot.alive is True  # Bot in anderem Team, kein Genocide

    def test_genocide_no_kill_when_already_dead(self, bot):
        """Bot bereits tot → kein doppelter MsgKilled."""
        from conftest import make_player
        from bzflag.protocol import MsgKilled as MsgKilledCode
        bot.team = 2
        bot.alive = False
        bot.client.send.reset_mock()
        victim = make_player(bot, pid=5)
        payload = self._build_killed_broadcast(
            victim_id=5, killer_id=3, reason=1, shot_id=7, flag_abbr=b"G\x00"
        )
        bot._on_killed(MsgKilledCode, payload)
        bot.client.send.assert_not_called()


class TestExplosionAndFalling:
    """Explosions-Zustand (PS_EXPLODING) bei jedem Tod + PS_FALLING aus echtem Luftzustand —
    client-treu (Player.cxx setExplode / LocalPlayer.cxx explodeTank)."""

    def test_start_explosion_launches_up_capped(self, bot):
        """_start_explosion: Aufwärts-Velocity > 0, ≤ zMax-Cap, Horizontal-Momentum bleibt erhalten."""
        import math, time as _t
        bot.vel = [3.0, -4.0, 0.0]
        bot._start_explosion(_t.monotonic())
        assert bot.vel[0] == 3.0 and bot.vel[1] == -4.0   # Horizontal erhalten
        assert bot.vel[2] > 0.0
        assert bot.vel[2] <= math.sqrt(-2.0 * 49.0 * bot._gravity) + 1e-6
        assert bot._exploding_until > _t.monotonic()

    def test_normal_kill_enters_explosion_state(self, bot):
        """_report_killed: sendet weiterhin MsgKilled UND startet den Explosions-Zustand."""
        import time as _t
        from types import SimpleNamespace
        from bzflag.protocol import MsgKilled
        bot.alive = True
        bot.vel = [5.0, 0.0, 0.0]
        bot.client.send.reset_mock()
        shot = SimpleNamespace(shooter_id=3, shot_id=7, flag_abbr=b"\x00\x00")
        bot._report_killed(shot)
        codes = [call[0][0] for call in bot.client.send.call_args_list]
        assert MsgKilled in codes
        assert bot._exploding_until > _t.monotonic()
        assert bot.vel[2] > 0.0

    def test_falling_bit_set_when_off_ground(self, bot):
        """_send_update lebend über Boden → PS_FALLING gesetzt (freier Fall, nicht nur Sprung)."""
        from bzflag.protocol import PS_FALLING
        bot.alive = True
        bot._jumping = False
        bot.pos = [0.0, 0.0, 12.0]   # über dem Boden (kein NavGraph → floor 0)
        bot.client.send.reset_mock()
        bot._send_update()
        status = struct.unpack_from(">h", bot.client.send.call_args[0][1], 9)[0]
        assert status & PS_FALLING

    def test_no_falling_bit_on_ground(self, bot):
        """_send_update lebend am Boden → kein PS_FALLING."""
        from bzflag.protocol import PS_FALLING
        bot.alive = True
        bot._jumping = False
        bot.pos = [0.0, 0.0, 0.0]
        bot.client.send.reset_mock()
        bot._send_update()
        status = struct.unpack_from(">h", bot.client.send.call_args[0][1], 9)[0]
        assert not (status & PS_FALLING)

    def test_report_killed_high_shot_id_no_overflow(self, bot):
        """_report_killed mit High-Bit-/Sentinel-Shot-ID (>32767) crasht nicht und packt
        die unteren 16 Bit (byte-treu zu int16_t(shotId) im echten Client)."""
        from types import SimpleNamespace
        from bzflag.protocol import MsgKilled
        for sid, expected in ((0xFFFF, b"\xff\xff"), (40000, b"\x9c\x40")):
            bot.alive = True
            bot.client.send.reset_mock()
            shot = SimpleNamespace(shooter_id=3, shot_id=sid, flag_abbr=b"\x00\x00")
            bot._report_killed(shot)   # darf keine struct.error werfen
            payload = [c[0][1] for c in bot.client.send.call_args_list
                       if c[0][0] == MsgKilled][0]
            # Layout: >B killer (1) + >H reason (2) + shotId (2) → Offset 3
            assert payload[3:5] == expected


# ── Fix 19: FakePack-PZ + MsgNearFlag + MsgTransferFlag ──────────────────────

def _build_flag_update_entry(flag_id, abbr_b, status, x=0.0, y=0.0, z=0.0):
    """Baut einen 57-Byte MsgFlagUpdate-Eintrag (flag_id + 55 Bytes Flag-Daten)."""
    entry = (
        struct.pack(">H", flag_id)
        + abbr_b
        + struct.pack(">H", status)
        + struct.pack(">H", 0)          # flagTeam
        + struct.pack(">B", 255)        # owner (kein Spieler)
        + struct.pack(">fff", x, y, z)  # pos
        + struct.pack(">fff", 0, 0, 0)  # launchPos
        + struct.pack(">fff", 0, 0, 0)  # landingPos
        + struct.pack(">f", 0.0) * 3   # flightTime + flightEnd + initVel
    )
    assert len(entry) == 57
    return struct.pack(">H", 1) + entry  # count=1


def test_flag_update_fake_pz_stored_as_empty(bot):
    """_on_flag_update: abbr_b=b'PZ' (Server-Fake) → flags[id].abbr == ''."""
    from bzflag.protocol import MsgFlagUpdate
    payload = _build_flag_update_entry(flag_id=5, abbr_b=b"PZ", status=1)
    bot._on_flag_update(MsgFlagUpdate, payload)
    assert 5 in bot.flags
    assert bot.flags[5].abbr == ""


def test_flag_update_real_abbr_stored(bot):
    """_on_flag_update: abbr_b=b'GM' (echter Typ) → flags[id].abbr == 'GM'."""
    from bzflag.protocol import MsgFlagUpdate
    payload = _build_flag_update_entry(flag_id=7, abbr_b=b"GM", status=1)
    bot._on_flag_update(MsgFlagUpdate, payload)
    assert 7 in bot.flags
    assert bot.flags[7].abbr == "GM"


def _build_near_flag_payload(x, y, z, flag_name):
    """Baut MsgNearFlag-Payload: 3×float + uint32 name_len + name."""
    name_b = flag_name.encode("ascii")
    return struct.pack(">fff", x, y, z) + struct.pack(">I", len(name_b)) + name_b


def test_on_near_flag_updates_abbr(bot):
    """_on_near_flag: Flagge bei (10,20) mit abbr='' → abbr wird auf 'GM' gesetzt."""
    from bzbot import FlagInfo
    from bzflag.protocol import MsgNearFlag
    bot.own_flag = "ID"
    bot.flags[3] = FlagInfo(flag_id=3, abbr="", status=1, pos=[10.0, 20.0, 0.0])
    payload = _build_near_flag_payload(10.0, 20.0, 0.0, "Guided Missile")
    bot._on_near_flag(MsgNearFlag, payload)
    assert bot.flags[3].abbr == "GM"


def test_on_near_flag_good_flag_drops_id(bot):
    """_on_near_flag: gute Flagge identifiziert → _try_drop_flag aufgerufen (MsgDropFlag)."""
    from bzbot import FlagInfo
    from bzflag.protocol import MsgNearFlag, MsgDropFlag
    bot.own_flag = "ID"
    bot.good_flags = {"GM"}
    bot.flags[3] = FlagInfo(flag_id=3, abbr="", status=1, pos=[10.0, 20.0, 0.0])
    bot.client.send.reset_mock()
    payload = _build_near_flag_payload(10.0, 20.0, 0.0, "Guided Missile")
    bot._on_near_flag(MsgNearFlag, payload)
    codes = [call[0][0] for call in bot.client.send.call_args_list]
    assert MsgDropFlag in codes


def test_on_near_flag_ignored_without_id(bot):
    """_on_near_flag: Bot hält kein ID → kein abbr-Update, kein Send."""
    from bzbot import FlagInfo
    from bzflag.protocol import MsgNearFlag
    bot.own_flag = "GM"
    bot.flags[3] = FlagInfo(flag_id=3, abbr="", status=1, pos=[10.0, 20.0, 0.0])
    bot.client.send.reset_mock()
    payload = _build_near_flag_payload(10.0, 20.0, 0.0, "Guided Missile")
    bot._on_near_flag(MsgNearFlag, payload)
    assert bot.flags[3].abbr == ""
    bot.client.send.assert_not_called()


def test_on_transfer_flag_clears_own_flag(bot):
    """_on_transfer_flag: from_id == bot.player_id → own_flag wird geleert."""
    from bzflag.protocol import MsgTransferFlag
    bot.own_flag = "GM"
    bot.player_id = 1
    # Payload: from=bot(1), to=5, flag_index=0, abbr="GM"
    payload = struct.pack(">BBH", 1, 5, 0) + b"GM"
    bot._on_transfer_flag(MsgTransferFlag, payload)
    assert bot.own_flag == ""


def test_on_transfer_flag_other_player_no_change(bot):
    """_on_transfer_flag: weder from noch to ist der Bot → own_flag unverändert."""
    from bzflag.protocol import MsgTransferFlag
    bot.own_flag = "GM"
    bot.player_id = 1
    payload = struct.pack(">BBH", 5, 6, 0) + b"SB"  # anderer Spieler
    bot._on_transfer_flag(MsgTransferFlag, payload)
    assert bot.own_flag == "GM"


def test_on_transfer_flag_thief_sets_own_flag(bot):
    """_on_transfer_flag: to_id == bot.player_id → own_flag gesetzt (TH-Diebstahl)."""
    from bzflag.protocol import MsgTransferFlag
    bot.own_flag = "TH"
    bot.player_id = 1
    # Payload: from=5, to=bot(1), flag_index=3, abbr="SB"
    payload = struct.pack(">BBH", 5, 1, 3) + b"SB"
    bot._on_transfer_flag(MsgTransferFlag, payload)
    assert bot.own_flag == "SB"


# ---------------------------------------------------------------------------
# Player-Typ-/Team-Konstanten (müssen dem Server-Enum entsprechen)
# ---------------------------------------------------------------------------

class TestPlayerTypeTeamConstants:
    """Schützt die korrigierten Konstanten gegen Regressionen.

    Server: enum PlayerType { TankPlayer=0, ComputerPlayer=1 } (global.h),
    enum TeamColor { … PurpleTeam=4, ObserverTeam=5, RabbitTeam=6, HunterTeam=7 }.
    Ein Observer ist KEIN Typ, sondern ein Team → es darf kein PLAYER_TYPE_OBSERVER
    geben. Der Server liest das Team als int16, daher ist 0xFFFF = NoTeam (nicht Observer).
    """

    def test_player_types(self):
        import bzflag.protocol as p
        assert p.PLAYER_TYPE_TANK == 0
        assert p.PLAYER_TYPE_COMPUTER == 1
        assert not hasattr(p, "PLAYER_TYPE_OBSERVER")

    def test_team_values(self):
        import bzflag.protocol as p
        assert p.TEAM_ROGUE == 0
        assert p.TEAM_RED == 1
        assert p.TEAM_GREEN == 2
        assert p.TEAM_BLUE == 3
        assert p.TEAM_PURPLE == 4
        assert p.TEAM_OBSERVER == 5
        assert p.TEAM_RABBIT == 6
        assert p.TEAM_HUNTER == 7
        assert p.TEAM_AUTOMATIC == 0xFFFE

    def test_enter_payload_observer_is_tank_plus_observerteam(self):
        """build_enter_payload für den Observer: type=TankPlayer(0), team=ObserverTeam(5)."""
        from bzflag.protocol import (
            build_enter_payload, PLAYER_TYPE_TANK, TEAM_OBSERVER,
        )
        payload = build_enter_payload(
            callsign="Obs", player_type=PLAYER_TYPE_TANK, team=TEAM_OBSERVER,
        )
        # Erste 4 Bytes: uint16 type, uint16 team (big-endian)
        assert payload[:4] == b"\x00\x00\x00\x05"
