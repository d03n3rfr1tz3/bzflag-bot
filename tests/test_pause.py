"""
Tests für Pause-Behandlung: MsgPause-Parsing + KI-Verhalten gegenüber pausierten
(unverwundbaren) Spielern — keine Schüsse verschwenden, kurz auf Rückkehr warten,
dann SEEKING. Pausierte werden nicht neu anvisiert und nicht durch Staleness als
Geist verworfen.
"""
import struct
import time
import pytest
from conftest import make_player


def test_on_pause_sets_and_clears(bot):
    """MsgPause [pid][paused] setzt/löscht das paused-Flag des Spielers."""
    from bzflag.protocol import MsgPause
    make_player(bot, pid=2, pos=(50.0, 0.0, 0.0))
    bot._on_pause(MsgPause, struct.pack(">BB", 2, 1))
    assert bot.players[2].paused is True
    bot._on_pause(MsgPause, struct.pack(">BB", 2, 0))
    assert bot.players[2].paused is False


def test_on_pause_unknown_player_noop(bot):
    """Pause für unbekannten Spieler crasht nicht."""
    from bzflag.protocol import MsgPause
    bot._on_pause(MsgPause, struct.pack(">BB", 99, 1))  # kein KeyError


def test_paused_enemy_not_acquired(bot):
    """Pausierter Gegner (in FoV + Radar) wird NICHT als Ziel gewählt."""
    bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = 0.0
    bot.human_count = 1
    info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0))
    assert bot._find_target_player() == 2      # Kontrolle: ohne Pause anvisierbar
    info.paused = True
    assert bot._find_target_player() is None


def test_get_enemy_pos_paused_ignores_staleness(bot):
    """Pausierte sind kein Geist: _get_enemy_pos liefert die bekannte Position trotz Staleness."""
    info = make_player(bot, pid=2, pos=(50.0, 10.0, 0.0))
    info.last_seen = time.monotonic() - 999.0   # längst veraltet
    info.paused = True
    assert bot._get_enemy_pos(2) == (50.0, 10.0)
    info.paused = False
    assert bot._get_enemy_pos(2) is None         # ohne Pause greift die Staleness


def test_paused_target_not_shot(bot, monkeypatch):
    """Pausiertes Ziel → kein gezielter Schuss (Slots/limitierte Schüsse bleiben erhalten)."""
    bot.own_flag = ""
    bot.human_count = 1
    bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = 0.0
    bot._next_shoot = 0.0
    monkeypatch.setattr(bot, "_can_shoot", lambda: True)
    monkeypatch.setattr(bot, "_next_slot_ready", lambda now: True)
    info = make_player(bot, pid=2, pos=(20.0, 0.0, 0.0))
    bot.target_player = 2
    fired = []
    monkeypatch.setattr(bot, "_maybe_shoot_standard", lambda *a, **k: fired.append(1))
    # Kontrolle: ohne Pause wird der Schuss-Pfad erreicht
    bot._maybe_shoot(time.monotonic())
    assert fired
    # Pausiert → Gate greift vor dem Schuss
    fired.clear()
    info.paused = True
    bot._maybe_shoot(time.monotonic())
    assert not fired


def test_paused_target_dropped_after_wait(bot):
    """Pausiertes Ziel: kurz halten, nach PAUSE_WAIT_S aufgeben → SEEKING."""
    from bot.constants import PAUSE_WAIT_S
    from bot.models import AIState
    bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = 0.0
    bot.human_count = 1
    bot._ai_state = AIState.COMBAT
    info = make_player(bot, pid=2, pos=(30.0, 0.0, 0.0))
    info.paused = True
    bot.target_player = 2
    now = time.monotonic()
    # Erster Tick: Warte-Timer startet, Ziel bleibt gehalten
    bot._tick_combat(now)
    assert bot.target_player == 2
    assert bot._target_paused_since is not None
    # Warte-Fenster überschritten → Ziel aufgeben → SEEKING
    bot._target_paused_since = now - PAUSE_WAIT_S - 1.0
    bot._tick_combat(now)
    assert bot.target_player is None
    assert bot._ai_state == AIState.SEEKING


def test_unpause_allows_reacquire(bot):
    """Nach Un-Pause ist der Gegner wieder anvisierbar."""
    bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = 0.0
    bot.human_count = 1
    info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0))
    info.paused = True
    assert bot._find_target_player() is None
    info.paused = False
    assert bot._find_target_player() == 2
