"""
N2 (Leerlauf-Early-Outs): _resolve_incoming_shots, _cleanup_shots und
_find_incoming_shot laufen 60 Hz auch ohne aktive Schüsse — die Early-Outs
sparen Lock/Iterations-Overhead, dürfen aber das Verhalten nicht ändern:
insbesondere muss die Hit-Check-Referenz weiterlaufen, damit Prüf-Fenster
und Relativ-Sweep beim ersten Schuss identisch aufsetzen (FABLE-PLAN.md N2).
"""
import math
import time
from conftest import make_shot


TANK_HEIGHT = 2.05
TANK_CZ     = TANK_HEIGHT / 2


# ── _resolve_incoming_shots ───────────────────────────────────────────────────

def test_resolve_empty_updates_check_refs(bot):
    """Early-Out muss _last_hit_check_t/_pos genauso nachführen wie der
    Vollpfad — sonst wüchse das Prüf-Fenster im Leerlauf unbegrenzt."""
    bot.pos = [10.0, -5.0, 0.0]
    now = time.monotonic()
    bot._last_hit_check_t   = now - 30.0
    bot._last_hit_check_pos = None
    bot._resolve_incoming_shots(now, 0.02)
    assert bot._last_hit_check_t == now
    assert bot._last_hit_check_pos == (10.0, -5.0, 0.0)
    assert bot.alive


def test_resolve_first_shot_after_idle_still_hits(bot):
    """Nach Leerlauf-Ticks (Early-Out) muss der erste Schuss im normalen
    Prüf-Fenster [letzter Check, now] erkannt werden — hier ein Schuss, der
    den Tank zwischen zwei Ticks durchquert."""
    bot.pos = [0.0, 0.0, 0.0]
    now = time.monotonic()
    # Leerlauf-Tick (leere Dicts → Early-Out setzt die Referenz)
    bot._resolve_incoming_shots(now - 0.02, 0.02)
    # Schuss, der zum Zeitpunkt `now` genau im Tank steht
    make_shot(bot, pos=(2.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
              fire_time=now - 0.02)
    bot._resolve_incoming_shots(now, 0.02)
    assert not bot.alive


# ── _cleanup_shots ────────────────────────────────────────────────────────────

def test_cleanup_empty_is_noop(bot):
    bot._cleanup_shots(time.monotonic())
    assert bot._shots == {} and bot._ricochet_paths == {}


def test_cleanup_still_removes_expired(bot):
    """Early-Out darf den Aufräum-Pfad nicht abschneiden, sobald Schüsse da sind."""
    now = time.monotonic()
    make_shot(bot, shooter_id=2, shot_id=1, fire_time=now - 10.0, lifetime=3.5)
    bot._ricochet_paths[(2, 1)] = []
    bot._cleanup_shots(now)
    assert (2, 1) not in bot._shots
    assert (2, 1) not in bot._ricochet_paths


# ── _find_incoming_shot ───────────────────────────────────────────────────────

def test_find_incoming_empty_returns_none(bot):
    shot, t = bot._find_incoming_shot(time.monotonic())
    assert shot is None and math.isinf(t)


def test_find_incoming_still_detects_threat(bot):
    """Mit aktivem Schuss muss der normale Bedrohungs-Pfad weiter greifen."""
    bot.pos = [0.0, 0.0, 0.0]
    make_shot(bot, pos=(50.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0))
    shot, t = bot._find_incoming_shot(time.monotonic())
    assert shot is not None
    assert t < 1.0
