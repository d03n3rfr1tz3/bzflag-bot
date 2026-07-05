"""
Tests für das IDLE-Parken: Ohne echte Präsenz (Spieler/Zuschauer) wandert der Bot
nicht mehr endlos, sondern fährt einen evtl. Rest-Pfad zu Ende und bleibt dann stehen.
"""
import time
from unittest.mock import MagicMock

from bot.models import AIState


def _idle(bot):
    bot._ai_state = AIState.IDLE
    bot._new_target = MagicMock()
    bot._handle_threat = lambda now: False
    bot._has_presence = lambda: False


def test_advance_path_end_parks_in_idle(bot):
    """Pfadende im IDLE → parken statt _new_target: target_pos None, vel/ang_vel 0."""
    _idle(bot)
    bot._nav_path = []          # Pfad bereits leer → Pfadende-Zweig
    bot.vel = [5.0, 5.0, 0.0]
    bot.ang_vel = 1.0
    bot.target_pos = (10.0, 0.0)

    bot._advance_path()

    assert bot.target_pos is None
    assert bot._nav_path == []
    assert bot._nav_goal is None
    assert bot.vel[0] == 0.0 and bot.vel[1] == 0.0
    assert bot.ang_vel == 0.0
    bot._new_target.assert_not_called()


def test_tick_idle_does_not_wander(bot):
    """IDLE ohne Ziel: kein neues Wander-Ziel (Bot bleibt stehen)."""
    _idle(bot)
    bot.target_pos = None

    bot._tick_idle(time.monotonic())

    bot._new_target.assert_not_called()
    assert bot.target_pos is None
    assert bot._move_reverse is False


def test_tick_idle_transitions_to_seeking_on_presence(bot):
    """Präsenz (Spieler ODER Zuschauer) → Übergang IDLE→SEEKING."""
    _idle(bot)
    bot._has_presence = lambda: True
    bot._transition_to = MagicMock()

    bot._tick_idle(time.monotonic())

    bot._transition_to.assert_called_once_with(AIState.SEEKING)
    bot._new_target.assert_not_called()


def test_tick_idle_still_dodges_threats(bot):
    """Bedrohung wird auch im Passiv-Modus behandelt (kein Wandern, aber Ausweichen)."""
    _idle(bot)
    bot._handle_threat = MagicMock(return_value=True)
    bot._transition_to = MagicMock()
    bot.target_pos = None

    bot._tick_idle(time.monotonic())

    bot._handle_threat.assert_called_once()
    bot._transition_to.assert_not_called()   # Threat-Zweig kehrt vorher zurück
    bot._new_target.assert_not_called()


def test_parked_idle_stays_stopped_in_movement(bot):
    """60-Hz-Bewegung: geparktes IDLE (target_pos None) nullt Rest-Geschwindigkeit."""
    bot._ai_state = AIState.IDLE
    bot.target_pos = None
    bot._jumping = False
    bot.vel = [7.0, -3.0, 0.0]
    bot.ang_vel = 0.8

    # ai_tick=False: nur den 60-Hz-Bewegungszweig ausführen, kein KI-Tick.
    bot._dispatch_movement(1.0 / 60.0, time.monotonic(), ai_tick=False)

    assert bot.vel[0] == 0.0 and bot.vel[1] == 0.0
    assert bot.ang_vel == 0.0
