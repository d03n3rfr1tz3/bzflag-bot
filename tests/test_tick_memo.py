"""
P4a: Per-Tick-Memo für _get_floor_z / _muzzle_clear / _has_los_to_enemy.

Der Memo ist ein reiner Funktions-Memo: alle Eingaben (Position, Flagge,
Azimut, Gegnerposition) stecken im Key. Geprüft wird, dass
(a) der Memo tatsächlich benutzt wird (präparierter Cache-Eintrag gewinnt),
(b) geänderte Eingaben einen neuen Key treffen (kein staler Treffer),
(c) die Methoden ohne _tick_memo-Attribut funktionieren (Mixin-Robustheit).
"""
from conftest import make_player

from bot.constants import TANK_HEIGHT


def test_floor_z_memo_hit_wins(bot):
    bot.pos = [10.0, 20.0, 0.0]
    bot._tick_memo[("floor", 10.0, 20.0, 0.0, bot.own_flag)] = 123.5
    assert bot._get_floor_z() == 123.5


def test_floor_z_position_change_misses(bot):
    bot.pos = [10.0, 20.0, 0.0]
    bot._tick_memo[("floor", 10.0, 20.0, 0.0, bot.own_flag)] = 123.5
    bot.pos[0] = 11.0
    assert bot._get_floor_z() == 0.0   # kein NavGraph im Fixture → Weltboden


def test_floor_z_stores_result(bot):
    bot.pos = [1.0, 2.0, 0.0]
    z = bot._get_floor_z()
    assert bot._tick_memo[("floor", 1.0, 2.0, 0.0, bot.own_flag)] == z


def test_muzzle_clear_memo_hit_wins(bot):
    bot.pos = [0.0, 0.0, 0.0]
    bot._tick_memo[("muzzle", 0.5, 0.0, 0.0, 0.0)] = False
    assert bot._muzzle_clear(0.5) is False


def test_los_memo_key_includes_enemy_pos(bot):
    info = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
    assert bot._has_los_to_enemy(2) is True     # freie Bahn (keine Karte)
    # Präparierter Eintrag für die AKTUELLE Konstellation gewinnt …
    key = ("los", 2, 0.0, 0.0, 0.0, 50.0, 0.0, TANK_HEIGHT * 0.5)
    bot._tick_memo[key] = False
    assert bot._has_los_to_enemy(2) is False
    # … bewegt sich der Gegner, greift der Eintrag nicht mehr (neuer Key)
    info.pos[0] = 60.0
    assert bot._has_los_to_enemy(2) is True


def test_methods_work_without_memo_attr(bot):
    """BZBotAI ist ein Mixin — die Methoden dürfen nicht am fehlenden
    Attribut sterben (getattr-Fallback, Memo dann einfach aus)."""
    del bot._tick_memo
    make_player(bot, 2, pos=(50.0, 0.0, 0.0))
    assert bot._get_floor_z() == 0.0
    assert bot._has_los_to_enemy(2) is True
    assert bot._muzzle_clear(0.0) in (True, False)
