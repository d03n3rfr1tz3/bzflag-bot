"""
Tests für Rabbit-Chase-Team-Umbelegung via MsgNewRabbit:
Neuer Rabbit → TEAM_RABBIT, alle anderen Nicht-Beobachter → TEAM_HUNTER.
Prüft außerdem, dass die Freund/Feind-Logik (_is_foe) nach der Umbelegung
korrekt greift (Rabbit vs. Hunter = Feind, Hunter vs. Hunter = kein Feind).
"""
import struct
import pytest
from conftest import make_player
from bzflag.protocol import (
    MsgNewRabbit, TEAM_RABBIT, TEAM_HUNTER, TEAM_OBSERVER,
)


def test_new_rabbit_assigns_rabbit_and_hunters(bot):
    """Der genannte Spieler wird Rabbit, andere Nicht-Beobachter werden Hunter."""
    make_player(bot, pid=2)
    make_player(bot, pid=3)
    bot._on_new_rabbit(MsgNewRabbit, struct.pack(">B", 2))
    assert bot.players[2].team == TEAM_RABBIT
    assert bot.players[3].team == TEAM_HUNTER


def test_new_rabbit_leaves_observer_untouched(bot):
    """Beobachter bleiben Beobachter, auch wenn sie nicht der Rabbit sind."""
    make_player(bot, pid=2)
    obs = make_player(bot, pid=4)
    obs.team = TEAM_OBSERVER
    bot._on_new_rabbit(MsgNewRabbit, struct.pack(">B", 2))
    assert bot.players[2].team == TEAM_RABBIT
    assert bot.players[4].team == TEAM_OBSERVER


def test_bot_becomes_rabbit(bot):
    """Wird der Bot selbst als Rabbit ernannt, folgt self.team; ein Hunter ist Feind."""
    make_player(bot, pid=1)      # der Bot selbst (player_id=1)
    hunter = make_player(bot, pid=2)
    bot._on_new_rabbit(MsgNewRabbit, struct.pack(">B", 1))
    assert bot.team == TEAM_RABBIT
    assert bot.players[1].team == TEAM_RABBIT
    assert hunter.team == TEAM_HUNTER
    assert bot._is_foe(hunter, True) is True


def test_bot_becomes_hunter_and_targets_rabbit(bot):
    """Bot als Hunter: Rabbit ist Feind, ein anderer Hunter ist kein Feind."""
    make_player(bot, pid=1)      # der Bot selbst
    rabbit = make_player(bot, pid=2)
    other_hunter = make_player(bot, pid=3)
    bot._on_new_rabbit(MsgNewRabbit, struct.pack(">B", 2))
    assert bot.team == TEAM_HUNTER
    assert rabbit.team == TEAM_RABBIT
    assert other_hunter.team == TEAM_HUNTER
    assert bot._is_foe(rabbit, True) is True
    assert bot._is_foe(other_hunter, True) is False


def test_bot_not_in_players_defensive_sync(bot):
    """Steht der Bot nicht im players-Dict, wird self.team dennoch korrekt gesetzt."""
    bot.team = 2
    make_player(bot, pid=2)      # nur ein anderer Spieler, Bot (pid=1) fehlt
    bot._on_new_rabbit(MsgNewRabbit, struct.pack(">B", 2))
    assert bot.players[2].team == TEAM_RABBIT
    assert bot.team == TEAM_HUNTER


def test_new_rabbit_no_rabbit_all_hunters(bot):
    """pid==0xFF (NoPlayer): niemand wird Rabbit, alle Nicht-Beobachter werden Hunter."""
    make_player(bot, pid=2)
    make_player(bot, pid=3)
    bot._on_new_rabbit(MsgNewRabbit, struct.pack(">B", 0xFF))
    assert bot.players[2].team == TEAM_HUNTER
    assert bot.players[3].team == TEAM_HUNTER
    assert TEAM_RABBIT not in (bot.players[2].team, bot.players[3].team)


def test_new_rabbit_empty_payload_noop(bot):
    """Leeres Payload → No-Op ohne Crash und ohne Team-Änderung."""
    p = make_player(bot, pid=2)
    p.team = 2
    bot._on_new_rabbit(MsgNewRabbit, b"")
    assert bot.players[2].team == 2
