import struct
import math
import time
import pytest


def _build_shot_packet(shooter_id=2, shot_id=1,
                       pos=(100.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0),
                       team=2, flag_type=b"\x00\x00", lifetime=3.5):
    """Baut ein 43-Byte-MsgShotBegin-Paket."""
    data  = struct.pack(">f",   0.0)                        # [0:4]  timestamp
    data += struct.pack(">B",   shooter_id)                 # [4]    shooter_id
    data += struct.pack(">H",   shot_id)                    # [5:7]  shot_id
    data += struct.pack(">fff", *pos)                       # [7:19] pos xyz
    data += struct.pack(">fff", *vel)                       # [19:31] vel xyz
    data += struct.pack(">f",   0.0)                        # [31:35] dt
    data += struct.pack(">h",   team)                       # [35:37] team
    data += flag_type                                       # [37:39] flag_type (2 bytes)
    data += struct.pack(">f",   lifetime)                   # [39:43] lifetime
    assert len(data) == 43
    return data


# ── Normal shot parsing ───────────────────────────────────────────────────────

def test_parse_normal_shot(bot):
    from bzflag.protocol import MsgShotBegin
    payload = _build_shot_packet(shooter_id=2, shot_id=5, flag_type=b"\x00\x00")
    bot._on_shot_begin(MsgShotBegin, payload)
    with bot._shots_lock:
        assert (2, 5) in bot._shots
        s = bot._shots[(2, 5)]
    assert s.is_sw    is False
    assert s.is_gm    is False
    assert s.is_laser is False
    assert s.flag_abbr == b"\x00\x00"


def test_parse_shockwave(bot):
    from bzflag.protocol import MsgShotBegin
    payload = _build_shot_packet(shooter_id=2, shot_id=1, flag_type=b"SW")
    bot._on_shot_begin(MsgShotBegin, payload)
    with bot._shots_lock:
        s = bot._shots[(2, 1)]
    assert s.is_sw    is True
    assert s.flag_abbr == b"SW"


def test_parse_guided_missile(bot):
    from bzflag.protocol import MsgShotBegin
    payload = _build_shot_packet(shooter_id=2, shot_id=1, flag_type=b"GM")
    bot._on_shot_begin(MsgShotBegin, payload)
    with bot._shots_lock:
        s = bot._shots[(2, 1)]
    assert s.is_gm    is True
    assert s.flag_abbr == b"GM"


def test_parse_laser(bot):
    from bzflag.protocol import MsgShotBegin
    payload = _build_shot_packet(shooter_id=2, shot_id=1, flag_type=b"L\x00")
    bot._on_shot_begin(MsgShotBegin, payload)
    with bot._shots_lock:
        s = bot._shots[(2, 1)]
    assert s.is_laser is True
    assert s.flag_abbr == b"L\x00"


def test_parse_shot_pos_vel(bot):
    from bzflag.protocol import MsgShotBegin
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(50.0, 30.0, 1.5), vel=(-80.0, 20.0, 0.0),
        flag_type=b"\x00\x00"
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    with bot._shots_lock:
        s = bot._shots[(2, 1)]
    assert s.pos[0] == pytest.approx(50.0, abs=0.01)
    assert s.pos[1] == pytest.approx(30.0, abs=0.01)
    assert s.vel[0] == pytest.approx(-80.0, abs=0.01)


def test_parse_own_shot_tracked(bot):
    """Eigene Schüsse werden getrackt — damit Rückpraller als Selbsttreff erkannt werden."""
    from bzflag.protocol import MsgShotBegin
    bot.player_id = 2  # shooter == self
    payload = _build_shot_packet(shooter_id=2, shot_id=1, flag_type=b"\x00\x00")
    bot._on_shot_begin(MsgShotBegin, payload)
    with bot._shots_lock:
        assert (2, 1) in bot._shots


# ── Immediate-hit checks on parse ─────────────────────────────────────────────

def test_sw_no_instant_kill_on_parse(bot):
    """SW innerhalb Killzone: kein Instant-Kill beim Empfang — SW-Front kommt zeitbasiert."""
    from bzflag.protocol import MsgShotBegin
    bot.pos   = [0.0, 0.0, 0.0]
    bot.alive = True
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(30.0, 0.0, 1.025),   # 30u horizontal, innerhalb Killzone
        vel=(0.0, 0.0, 0.0),
        flag_type=b"SW",
        lifetime=3.5,
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    # Kein sofortiger Kill — Welle braucht (30-6)/60 = 0.4s zum Eintreffen
    bot.client.send.assert_not_called()
    assert bot.alive is True


def test_sw_immediate_miss_too_far(bot):
    """SW fired 90u away → outside SHOCK_OUT_RADIUS (60u) → no kill."""
    from bzflag.protocol import MsgShotBegin
    bot.pos   = [0.0, 0.0, 0.0]
    bot.alive = True
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(90.0, 0.0, 1.025),
        flag_type=b"SW",
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    bot.client.send.assert_not_called()


def test_laser_immediate_hit(bot):
    """Laser fired along x-axis directly through tank center → kill."""
    from bzflag.protocol import MsgShotBegin, MsgKilled
    bot.pos   = [0.0, 0.0, 0.0]
    bot.alive = True
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(-200.0, 0.0, 1.025),       # laser starts 200u left of tank
        vel=(100.0, 0.0, 0.0),           # flies rightward through tank center
        flag_type=b"L\x00",
        lifetime=3.5,
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    # _report_killed sendet jetzt Stopp-Update (MsgPlayerUpdate) + MsgKilled
    codes = [c[0][0] for c in bot.client.send.call_args_list]
    assert MsgKilled in codes


def test_laser_immediate_miss(bot):
    """Laser fired 20u to the side → perpendicular distance > HIT_RADIUS → no kill."""
    from bzflag.protocol import MsgShotBegin
    bot.pos   = [0.0, 0.0, 0.0]
    bot.alive = True
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(-200.0, 20.0, 1.025),       # laser passes 20u to the side
        vel=(100.0, 0.0, 0.0),
        flag_type=b"L\x00",
        lifetime=3.5,
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    bot.client.send.assert_not_called()


def test_sw_own_shot_no_kill(bot):
    """Eigener SW-Schuss tötet den Bot nicht."""
    from bzflag.protocol import MsgShotBegin
    bot.player_id = 2
    bot.pos   = [0.0, 0.0, 0.0]
    bot.alive = True
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(0.0, 0.0, 1.025),
        vel=(0.0, 0.0, 0.0),
        flag_type=b"SW", lifetime=3.5,
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    bot.client.send.assert_not_called()
    assert bot.alive is True


def test_sw_point_blank_kill(bot):
    """SW-Schuss startet bereits INNERHALB _shockInRadius (6u) → Sofort-Treffer,
    weil die Wellenfront in _resolve_incoming_shots nur nach außen wandert und
    diese Position sonst nie erfassen würde."""
    from bzflag.protocol import MsgShotBegin, MsgKilled
    bot.pos   = [0.0, 0.0, 0.0]
    bot.alive = True
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(3.0, 0.0, 1.025),   # 3u horizontal < shock_in_radius (6u)
        vel=(0.0, 0.0, 0.0),
        flag_type=b"SW", lifetime=3.5,
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    codes = [c[0][0] for c in bot.client.send.call_args_list]
    assert MsgKilled in codes
    assert bot.alive is False


def test_sw_point_blank_own_shot_no_kill(bot):
    """Eigener SW-Schuss punktblank tötet den Bot nicht (siehe test_sw_own_shot_no_kill,
    hier explizit innerhalb _shockInRadius)."""
    from bzflag.protocol import MsgShotBegin
    bot.player_id = 2
    bot.pos   = [0.0, 0.0, 0.0]
    bot.alive = True
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(3.0, 0.0, 1.025),
        vel=(0.0, 0.0, 0.0),
        flag_type=b"SW", lifetime=3.5,
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    bot.client.send.assert_not_called()
    assert bot.alive is True


def test_sw_point_blank_sh_survives_and_drops_flag(bot):
    """Bot hält SH und steht punktblank in der SW-Killzone → überlebt, droppt Flag."""
    from bzflag.protocol import MsgShotBegin, MsgDropFlag, MsgKilled
    bot.pos      = [0.0, 0.0, 0.0]
    bot.alive    = True
    bot.own_flag = "SH"
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(3.0, 0.0, 1.025),
        vel=(0.0, 0.0, 0.0),
        flag_type=b"SW", lifetime=3.5,
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    assert bot.alive is True
    codes = [c[0][0] for c in bot.client.send.call_args_list]
    assert MsgDropFlag in codes
    assert MsgKilled not in codes


def test_gm_own_shot_no_kill(bot):
    """Eigener GM-Schuss tötet den Bot nicht (kein Sofort-Kill beim Start)."""
    from bzflag.protocol import MsgShotBegin
    bot.player_id = 2
    bot.pos   = [0.0, 0.0, 0.0]
    bot.alive = True
    payload = _build_shot_packet(
        shooter_id=2, shot_id=1,
        pos=(0.0, 0.0, 1.025),
        vel=(100.0, 0.0, 0.0),
        flag_type=b"GM", lifetime=5.0,
    )
    bot._on_shot_begin(MsgShotBegin, payload)
    bot.client.send.assert_not_called()
    assert bot.alive is True
