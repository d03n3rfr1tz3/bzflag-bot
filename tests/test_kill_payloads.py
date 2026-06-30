import struct
import time
import pytest
from conftest import make_shot


def _decode_payload(call_args):
    """Extrahiert den Payload-Bytes aus dem client.send()-Aufruf."""
    from bzflag.protocol import MsgKilled
    args, kwargs = call_args
    assert args[0] == MsgKilled
    return args[1]


def _parse_payload(data: bytes):
    killer_id = struct.unpack_from(">B", data, 0)[0]
    reason    = struct.unpack_from(">H", data, 1)[0]
    shot_id   = struct.unpack_from(">h", data, 3)[0]
    flag_abbr = data[5:7]
    return killer_id, reason, shot_id, flag_abbr


# ── _report_killed ────────────────────────────────────────────────────────────

def test_kill_normalshot(bot):
    s = make_shot(bot, shooter_id=5, shot_id=3, flag_abbr=b"\x00\x00")
    bot._report_killed(s)
    payload = _decode_payload(bot.client.send.call_args)
    killer_id, reason, shot_id, flag_abbr = _parse_payload(payload)
    assert killer_id   == 5
    assert reason      == 1   # KILL_REASON_SHOT
    assert shot_id     == 3
    assert flag_abbr   == b"\x00\x00"


def test_kill_shockwave(bot):
    s = make_shot(bot, shooter_id=7, shot_id=10, flag_abbr=b"SW", is_sw=True)
    bot._report_killed(s)
    payload = _decode_payload(bot.client.send.call_args)
    _, reason, _, flag_abbr = _parse_payload(payload)
    assert reason    == 1
    assert flag_abbr == b"SW"


def test_kill_guided_missile(bot):
    s = make_shot(bot, shooter_id=3, shot_id=2, flag_abbr=b"GM", is_gm=True)
    bot._report_killed(s)
    payload = _decode_payload(bot.client.send.call_args)
    _, reason, _, flag_abbr = _parse_payload(payload)
    assert reason    == 1
    assert flag_abbr == b"GM"


def test_kill_laser(bot):
    s = make_shot(bot, shooter_id=4, shot_id=7, flag_abbr=b"L\x00", is_laser=True)
    bot._report_killed(s)
    payload = _decode_payload(bot.client.send.call_args)
    _, reason, _, flag_abbr = _parse_payload(payload)
    assert reason    == 1
    assert flag_abbr == b"L\x00"


def test_kill_genocide(bot):
    s = make_shot(bot, shooter_id=6, shot_id=1, flag_abbr=b"G\x00")
    bot._report_killed(s)
    payload = _decode_payload(bot.client.send.call_args)
    _, reason, _, flag_abbr = _parse_payload(payload)
    assert reason    == 3   # KILL_REASON_GENOCIDED
    assert flag_abbr == b"G\x00"


def test_kill_sets_dead(bot):
    s = make_shot(bot, flag_abbr=b"\x00\x00")
    bot._report_killed(s)
    assert bot.alive is False
    assert bot.death_time is not None


def test_kill_idempotent_when_already_dead(bot):
    bot.alive = False
    s = make_shot(bot, flag_abbr=b"\x00\x00")
    bot._report_killed(s)
    bot.client.send.assert_not_called()


# ── _report_steamrolled ───────────────────────────────────────────────────────

def test_steamrolled_payload(bot):
    bot._report_steamrolled(killer_id=9)
    payload = _decode_payload(bot.client.send.call_args)
    killer_id, reason, shot_id, flag_abbr = _parse_payload(payload)
    assert killer_id   == 9
    assert reason      == 2   # KILL_REASON_RUNOVER
    assert shot_id     == 0
    assert flag_abbr   == b"SR"


def test_steamrolled_sets_dead(bot):
    bot._report_steamrolled(killer_id=3)
    assert bot.alive is False
    assert bot.death_time is not None


def test_steamrolled_idempotent_when_already_dead(bot):
    bot.alive = False
    bot._report_steamrolled(killer_id=3)
    bot.client.send.assert_not_called()


# ── Payload length ────────────────────────────────────────────────────────────

def test_kill_payload_length(bot):
    s = make_shot(bot, flag_abbr=b"SW", is_sw=True)
    bot._report_killed(s)
    payload = _decode_payload(bot.client.send.call_args)
    assert len(payload) == 7


def test_steamrolled_payload_length(bot):
    bot._report_steamrolled(killer_id=1)
    payload = _decode_payload(bot.client.send.call_args)
    assert len(payload) == 7
