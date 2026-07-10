"""
Tests für B3: on_world_ready_extra-Callback (mypyc-kompatible Alternative zum
Methoden-Monkeypatch von _on_world_ready, siehe bzbot.py --dump-map/--dump-raw).
"""
import pytest


def test_on_world_ready_extra_called_with_none(bot):
    """world_map=None (Parse-Fehler) → _on_world_ready kehrt früh zurück, der
    Extra-Callback muss trotzdem aufgerufen werden (try/finally)."""
    calls = []
    bot.on_world_ready_extra = lambda wm: calls.append(wm)
    bot._on_world_ready(None)
    assert calls == [None]


def test_on_world_ready_extra_called_with_world_map(bot):
    """Regulärer Weltload (Stub-WorldMap) → Extra-Callback erhält dieselbe WorldMap."""
    from bzflag.world_map import WorldMap
    wm = WorldMap()
    calls = []
    bot.on_world_ready_extra = lambda w: calls.append(w)
    bot._on_world_ready(wm)
    assert calls == [wm]
    assert bot._world_map is wm


def test_on_world_ready_without_extra_callback_still_works(bot):
    """Kein Extra-Callback gesetzt (Default None) → _on_world_ready läuft ohne Fehler durch."""
    from bzflag.world_map import WorldMap
    assert bot.on_world_ready_extra is None
    bot._on_world_ready(WorldMap())
    bot._on_world_ready(None)


def test_on_world_ready_extra_exception_is_swallowed(bot, caplog):
    """Ein fehlerhafter Extra-Callback darf _on_world_ready nicht crashen lassen
    (analog zu anderen defensiven Callback-Aufrufen im Bot)."""
    import logging

    def _boom(wm):
        raise RuntimeError("kaputt")

    bot.on_world_ready_extra = _boom
    with caplog.at_level(logging.ERROR, logger="bzbot"):
        bot._on_world_ready(None)   # darf nicht raisen
    assert any("on_world_ready_extra" in r.getMessage() for r in caplog.records)
