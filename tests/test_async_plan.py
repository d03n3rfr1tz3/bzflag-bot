"""Tests für die asynchrone Pfadplanung (P4-INF-01).

Der Haupt-Thread plant schnell (Defaults); dauerte das länger als NAV_ASYNC_TRIGGER_MS, läuft
parallel eine Vollsuche im Zweit-Thread, deren bessere Route beim nächsten KI-Tick übernommen wird
— sofern sie noch relevant ist (Plan-Generation, Ziel, Drift, Lebend).
"""

import threading

import pytest

from bot.constants import NAV_ASYNC_MAX_EXPANSIONS, NAV_ASYNC_MAX_MS


class _FakeNav:
    """Minimaler NavGraph-Ersatz: plan_path protokolliert die Limits und gibt einen festen Pfad."""

    def __init__(self, path):
        self._path = path
        self.calls = []

    def plan_path(self, sx, sy, sz, gx, gy, blocked_jump_wps=None, goal_z=None,
                  max_expansions=None, max_ms=None, cancel=None, **_kw):
        self.calls.append({"max_expansions": max_expansions, "max_ms": max_ms})
        return list(self._path)


def _prep(bot, nav):
    bot._nav_graph = nav
    bot._nav_jump_cooldowns = {}
    bot.pos = [0.0, 0.0, 0.0]
    bot.alive = True
    bot.own_flag = ""


# ── Trigger ────────────────────────────────────────────────────────────────

def test_async_spawns_when_sync_exceeds_trigger(bot, monkeypatch):
    nav = _FakeNav([(5.0, 0.0, 0.0)])
    _prep(bot, nav)
    monkeypatch.setattr("bot.ai.navigation.NAV_ASYNC_TRIGGER_MS", -1.0)   # elapsed_ms ≥ 0 → immer triggern
    bot._plan_path(5.0, 0.0)
    th = bot._async_plan_thread
    assert th is not None
    th.join(timeout=2.0)
    assert not th.is_alive()
    # Worker rief plan_path mit den großen Hintergrund-Limits auf (letzter Call).
    assert nav.calls[-1]["max_expansions"] == NAV_ASYNC_MAX_EXPANSIONS
    assert nav.calls[-1]["max_ms"] == NAV_ASYNC_MAX_MS
    assert bot._async_plan_result is not None
    assert bot._async_plan_result[0] == bot._plan_gen          # gen passend getaggt


def test_async_not_spawned_when_sync_fast(bot, monkeypatch):
    nav = _FakeNav([(5.0, 0.0, 0.0)])
    _prep(bot, nav)
    monkeypatch.setattr("bot.ai.navigation.NAV_ASYNC_TRIGGER_MS", 1e9)    # nie triggern
    bot._plan_path(5.0, 0.0)
    assert bot._async_plan_thread is None


# ── Einer zur Zeit + Cancel bei Ziel-Wechsel ────────────────────────────────

def test_one_worker_at_a_time_and_cancel_on_goal_change(bot):
    release = threading.Event()

    class _BlockingNav:
        def plan_path(self, *a, cancel=None, **k):
            release.wait(2.0)
            return []

    bot._nav_graph = _BlockingNav()
    bot._submit_async_plan(0.0, 0.0, 0.0, 10.0, 0.0, None, set(), None, gen=5)
    th1 = bot._async_plan_thread
    assert th1 is not None and th1.is_alive()

    # Gleiches Ziel, Worker läuft noch → kein zweiter Thread, kein Cancel.
    bot._submit_async_plan(0.0, 0.0, 0.0, 10.0, 0.0, None, set(), None, gen=6)
    assert bot._async_plan_thread is th1
    assert not bot._async_cancel.is_set()

    # Anderes Ziel → Cancel der laufenden (stale) Suche, weiterhin nur ein Thread.
    bot._submit_async_plan(0.0, 0.0, 0.0, 99.0, 0.0, None, set(), None, gen=7)
    assert bot._async_plan_thread is th1
    assert bot._async_cancel.is_set()

    release.set()
    th1.join(timeout=2.0)


# ── Poll: Übernahme & Verwerfen ─────────────────────────────────────────────

def _seed(bot, *, gen, gx, gy, path, cap=None, sx=0.0, sy=0.0, gz=None):
    bot._plan_gen = gen
    bot._nav_goal = (gx, gy)
    bot._async_plan_result = (gen, gx, gy, gz, cap, sx, sy, path)


def test_poll_adopts_relevant_result(bot):
    _prep(bot, _FakeNav([]))
    bot.azimuth = 0.0
    path = [(2.0, 0.0, 0.0), (4.0, 0.0, 0.0)]
    _seed(bot, gen=5, gx=10.0, gy=0.0, path=path)
    bot._poll_async_plan()
    assert bot._nav_path == path
    assert bot.target_pos == (2.0, 0.0)
    assert bot._async_plan_result is None      # konsumiert


def test_poll_applies_cap_wps(bot):
    _prep(bot, _FakeNav([]))
    bot.azimuth = 0.0
    path = [(2.0, 0.0, 0.0), (4.0, 0.0, 0.0), (6.0, 0.0, 0.0)]
    _seed(bot, gen=5, gx=10.0, gy=0.0, path=path, cap=2)
    bot._poll_async_plan()
    assert len(bot._nav_path) == 2


def test_poll_discards_on_stale_generation(bot):
    _prep(bot, _FakeNav([]))
    bot._nav_path = ["SENTINEL"]
    _seed(bot, gen=5, gx=10.0, gy=0.0, path=[(2.0, 0.0, 0.0)])
    bot._plan_gen = 6                          # neuerer Plan-Request seither
    bot._poll_async_plan()
    assert bot._nav_path == ["SENTINEL"]


def test_poll_discards_on_goal_change(bot):
    _prep(bot, _FakeNav([]))
    bot._nav_path = ["SENTINEL"]
    _seed(bot, gen=5, gx=10.0, gy=0.0, path=[(2.0, 0.0, 0.0)])
    bot._nav_goal = (99.0, 0.0)                # Ziel hat gewechselt
    bot._poll_async_plan()
    assert bot._nav_path == ["SENTINEL"]


def test_poll_discards_when_off_route(bot):
    """Bot weit von ALLEN Pfad-Segmenten (weggeschossen/teleportiert) → verwerfen (kein Resync)."""
    _prep(bot, _FakeNav([]))
    bot.azimuth = 0.0
    bot._nav_path = ["SENTINEL"]
    bot.pos = [100.0, 100.0, 0.0]                  # weit weg von der Route nahe Ursprung
    _seed(bot, gen=5, gx=10.0, gy=0.0,
          path=[(0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (8.0, 0.0, 0.0)])
    bot._poll_async_plan()
    assert bot._nav_path == ["SENTINEL"]


def test_poll_resyncs_skips_traversed_prefix(bot):
    """Bot ist auf der Route vorgefahren → abgefahrener Prefix wird übersprungen, kein Zurückdrehen."""
    _prep(bot, _FakeNav([]))
    bot.azimuth = 0.0
    bot.pos = [7.0, 0.0, 0.0]                       # mitten auf Segment (4,0)→(8,0)
    path = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (8.0, 0.0, 0.0), (12.0, 0.0, 0.0)]
    _seed(bot, gen=5, gx=12.0, gy=0.0, path=path)
    bot._poll_async_plan()
    # Prefix (0,0)/(4,0) gedroppt; nächstes Ziel ist der vordere WP (8,0), nicht WP0.
    assert bot.target_pos == (8.0, 0.0)
    assert bot._nav_path == [(8.0, 0.0, 0.0), (12.0, 0.0, 0.0)]
    assert (0.0, 0.0) not in [(w[0], w[1]) for w in bot._nav_path]


def test_poll_discards_when_dead(bot):
    _prep(bot, _FakeNav([]))
    bot._nav_path = ["SENTINEL"]
    bot.alive = False
    _seed(bot, gen=5, gx=10.0, gy=0.0, path=[(2.0, 0.0, 0.0)])
    bot._poll_async_plan()
    assert bot._nav_path == ["SENTINEL"]


def test_poll_discards_empty_path(bot):
    _prep(bot, _FakeNav([]))
    bot._nav_path = ["SENTINEL"]
    _seed(bot, gen=5, gx=10.0, gy=0.0, path=[])
    bot._poll_async_plan()
    assert bot._nav_path == ["SENTINEL"]


def test_poll_noop_without_result(bot):
    _prep(bot, _FakeNav([]))
    bot._nav_path = ["SENTINEL"]
    bot._async_plan_result = None
    bot._poll_async_plan()
    assert bot._nav_path == ["SENTINEL"]
