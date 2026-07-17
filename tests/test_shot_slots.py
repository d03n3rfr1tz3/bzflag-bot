"""Tests für P4-TAC-05: per-Gegner-Schuss-Slot-Tracking (PlayerInfo.slot_reload_at),
Befüllung in _on_shot_begin, Reset bei Tod/Respawn und die Abfrage-Helper
(_enemy_slots_empty / _enemy_next_slot_ready_in / _reload_time_for_flag)."""
import struct
import time

import pytest

from conftest import make_player
from test_shot_parsing import _build_shot_packet


def _shot(bot, **kw):
    from bzflag.protocol import MsgShotBegin
    bot._on_shot_begin(MsgShotBegin, _build_shot_packet(**kw))


# ── Befüllung in _on_shot_begin ───────────────────────────────────────────

def test_shot_sets_slot_reload(bot):
    make_player(bot, 2)
    t0 = time.monotonic()
    _shot(bot, shooter_id=2, shot_id=0)          # Slot 0
    slots = bot.players[2].slot_reload_at
    assert len(slots) == 1
    assert slots[0] == pytest.approx(t0 + bot._reload_time, abs=0.2)


def test_generation_bits_select_slot(bot):
    """shot_id = (generation << 8) | slot → nur der untere Byte-Index zählt."""
    make_player(bot, 2)
    _shot(bot, shooter_id=2, shot_id=(3 << 8) | 1)   # Generation 3, Slot 1
    slots = bot.players[2].slot_reload_at
    assert len(slots) == 2          # bis Index 1 aufgefüllt
    assert slots[0] == 0.0          # ungesehener Slot 0 gilt als geladen
    assert slots[1] > 0.0


def test_mg_shooter_faster_reload(bot):
    make_player(bot, 2)
    t0 = time.monotonic()
    _shot(bot, shooter_id=2, shot_id=0, flag_type=b"MG")
    ready = bot.players[2].slot_reload_at[0]
    assert ready == pytest.approx(t0 + bot._reload_time / bot._mgun_ad_rate, abs=0.2)


def test_rapidfire_shooter_faster_reload(bot):
    make_player(bot, 2)
    t0 = time.monotonic()
    _shot(bot, shooter_id=2, shot_id=0, flag_type=b"F\x00")
    ready = bot.players[2].slot_reload_at[0]
    assert ready == pytest.approx(t0 + bot._reload_time / bot._rfire_ad_rate, abs=0.2)


def test_unknown_shooter_no_crash(bot):
    """Schütze nicht in players → kein Eintrag, kein Fehler."""
    _shot(bot, shooter_id=99, shot_id=0)
    assert 99 not in bot.players


def test_own_shot_not_tracked_as_enemy(bot):
    """Eigener Schuss (shooter == player_id) landet nicht in einem PlayerInfo."""
    bot.player_id = 1
    make_player(bot, 1)          # unrealistisch, aber prüft den shooter==player_id-Guard
    _shot(bot, shooter_id=1, shot_id=0)
    assert bot.players[1].slot_reload_at == []


# ── Reset bei Tod / Respawn ───────────────────────────────────────────────

def test_death_resets_slots(bot):
    from bzflag.protocol import MsgKilled
    p = make_player(bot, 2)
    p.slot_reload_at = [time.monotonic() + 3.5]
    bot._on_killed(MsgKilled, struct.pack(">B", 2))
    assert bot.players[2].slot_reload_at == []


def test_respawn_resets_slots(bot):
    from bzflag.protocol import MsgAlive
    p = make_player(bot, 2, alive=False)
    p.slot_reload_at = [time.monotonic() + 3.5]
    payload = struct.pack(">B", 2) + struct.pack(">fff", 0.0, 0.0, 0.0) + struct.pack(">f", 0.0)
    bot._on_alive(MsgAlive, payload)
    assert bot.players[2].slot_reload_at == []


# ── Abfrage-Helper ────────────────────────────────────────────────────────

def test_slots_empty_and_window(bot):
    bot._max_shots = 2
    p = make_player(bot, 2)
    now = time.monotonic()
    # Beide Slots in der Zukunft → leergeschossen; Fenster = früherer Slot.
    p.slot_reload_at = [now + 3.0, now + 1.0]
    assert bot._enemy_slots_empty(p, now) is True
    assert bot._enemy_next_slot_ready_in(p, now) == pytest.approx(1.0, abs=0.01)


def test_one_slot_free_not_empty(bot):
    bot._max_shots = 2
    p = make_player(bot, 2)
    now = time.monotonic()
    p.slot_reload_at = [now + 3.0, now - 0.5]   # Slot 1 bereits frei
    assert bot._enemy_slots_empty(p, now) is False
    assert bot._enemy_next_slot_ready_in(p, now) == 0.0


def test_unknown_slots_treated_as_loaded(bot):
    """Weniger bekannte Slots als _max_shots → konservativ 'geladen' (nicht leer)."""
    bot._max_shots = 2
    p = make_player(bot, 2)
    now = time.monotonic()
    p.slot_reload_at = [now + 3.0]      # nur 1 von 2 Slots bekannt
    assert bot._enemy_slots_empty(p, now) is False
    assert bot._enemy_next_slot_ready_in(p, now) == 0.0


# ── Refactor-Absicherung: _effective_reload_time delegiert korrekt ─────────

@pytest.mark.parametrize("flag,expected", [
    ("", 3.5),
    ("MG", 3.5 / 10.0),
    ("F", 3.5 / 2.0),
])
def test_effective_reload_time_unchanged(bot, flag, expected):
    bot.own_flag = flag
    assert bot._effective_reload_time() == pytest.approx(expected)
    assert bot._reload_time_for_flag(flag) == pytest.approx(expected)
