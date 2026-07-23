"""
P4-FLG-03: PhantomZone-Manöver — Escape-Zustände, Torwahl (Phase 1/2), Async-
Erreichbarkeits-Validierung, Wire-Flag-Nulling, Schuss-Gating und Zielwahl.

Keep-Gate-Tests (_pz_worth_keeping) liegen in test_flags.py, Querungs-Toggles in
test_teleporter.py, Verwundbarkeits-Regeln in test_hit_detection.py.
"""
import math
import time

import pytest
from unittest.mock import MagicMock, patch
from conftest import make_player, make_shot

from bot.models import AIState
from bzflag.world_map import TeleporterObstacle


def _tele(name, cx, cy):
    return TeleporterObstacle(name=name, cx=cx, cy=cy, bottom_z=0.0, angle=0.0,
                              half_w=0.5, half_d=5.0, height=10.0, border=1.0)


def _wire_two_gates(bot, x0=-100.0, x1=100.0):
    wm = MagicMock()
    wm.teleporters = [_tele("t0", x0, 0.0), _tele("t1", x1, 0.0)]
    wm.boxes = []
    bot._world_map = wm
    bot._link_map = {0: 3, 3: 0, 1: 2, 2: 1}
    bot.world_half = 400.0
    return wm


def _arm_escape(bot):
    """PZ + reicherer Gegner in Radar-Reichweite → _pz_escape_active() wahr (Phase 1)."""
    bot.team = 1
    bot.own_flag = "PZ"
    info = make_player(bot, pid=5, pos=(50.0, 0.0, 0.0))
    info.wins = 10
    return info


# ── Wire-Flag (MsgShotBegin) ─────────────────────────────────────────────────

def test_own_flag_bytes_pz_nulled_when_unzoned(bot):
    """ShotPath.cxx:46: ungezoned nullt der Client das PZ-Flag im Schuss."""
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = False
    assert bot._own_flag_bytes() == b"\x00\x00"


def test_own_flag_bytes_pz_sent_when_zoned(bot):
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    assert bot._own_flag_bytes() == b"PZ"


def test_own_flag_bytes_other_flags_unchanged(bot):
    bot.own_flag = "GM"
    bot.is_phantom_zoned = False
    assert bot._own_flag_bytes() == b"GM"


# ── Escape-Aktivierung ───────────────────────────────────────────────────────

def test_escape_active_when_zoned(bot):
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    assert bot._pz_escape_active() is True


def test_escape_active_with_gate_true(bot):
    _wire_two_gates(bot)
    _arm_escape(bot)
    assert bot._pz_escape_active() is True


def test_escape_inactive_during_rezone_cooldown(bot):
    _wire_two_gates(bot)
    _arm_escape(bot)
    bot._pz_rezone_block_until = time.monotonic() + 30.0
    assert bot._pz_escape_active() is False


def test_escape_inactive_without_pz(bot):
    bot.own_flag = "GM"
    assert bot._pz_escape_active() is False


def test_ground_state_prefers_seeking_during_escape(bot):
    """Flucht schlägt Kampf: trotz target_player kein COMBAT als Return-State."""
    _wire_two_gates(bot)
    _arm_escape(bot)
    bot.target_player = 5
    assert bot._ground_state() == AIState.SEEKING


def test_tick_combat_redirects_to_seeking_during_escape(bot):
    _wire_two_gates(bot)
    _arm_escape(bot)
    bot._ai_state = AIState.COMBAT
    bot._tick_combat(time.monotonic())
    assert bot._ai_state == AIState.SEEKING


# ── Manöver: Torwahl + Validierung ───────────────────────────────────────────

def test_maneuver_phase1_targets_nearest_gate(bot):
    """Unzoned: nächstgelegenes Tor wird Ziel; ohne Nav-Graph gilt es sofort als validiert
    (Direktfahrt ist die einzige Wahrheit) und das Direktziel zeigt auf die Tor-Mitte."""
    _wire_two_gates(bot, x0=-100.0, x1=100.0)
    _arm_escape(bot)
    bot.pos_x = 60.0                      # näher an t1 (x=100)
    bot._nav_graph = None
    bot._pz_maneuver_tick(time.monotonic())
    assert bot._pz_target_gate == 1
    assert bot._pz_validate_result is True
    assert bot.target_pos == pytest.approx((100.0, 0.0))


def test_maneuver_phase2_prefers_other_gate(bot):
    """Zoned über Tor 1 → Phase 2 steuert Tor 0 an (anderes Tor, User-Regel)."""
    _wire_two_gates(bot)
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    bot._pz_zoned_gate = 1
    bot.pos_x = 90.0                      # eigentlich näher an Tor 1 — trotzdem Tor 0
    bot._nav_graph = None
    bot._pz_maneuver_tick(time.monotonic())
    assert bot._pz_target_gate == 0
    assert bot.target_pos == pytest.approx((-100.0, 0.0))


def test_maneuver_phase2_single_gate_falls_back_to_same(bot):
    """Nur ein Tor auf der Karte: Phase 2 nutzt dasselbe Tor (die Selbes-Tor-Drop-Regel
    greift dann bei der Querung)."""
    wm = MagicMock()
    wm.teleporters = [_tele("t0", -100.0, 0.0)]
    wm.boxes = []
    bot._world_map = wm
    bot._link_map = {0: 1, 1: 0}
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    bot._pz_zoned_gate = 0
    bot._nav_graph = None
    bot._pz_maneuver_tick(time.monotonic())
    assert bot._pz_target_gate == 0


def test_maneuver_zoned_drives_through_buildings(bot):
    """Zoned aktiviert den Direktziel-Modus (_can_drive_through_obstacles → _plan_path
    setzt die Tor-Mitte als Direktziel, kein A*)."""
    _wire_two_gates(bot)
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    assert bot._can_drive_through_obstacles() is True
    bot._nav_graph = MagicMock()          # Graph vorhanden — darf zoned NICHT befragt werden
    bot._plan_path(-100.0, 0.0)
    bot._nav_graph.plan_path.assert_not_called()
    assert bot.target_pos == pytest.approx((-100.0, 0.0))


def test_maneuver_validation_timeout_locks_gate_and_advances(bot):
    """Async-Validierung ohne Ergebnis nach PZ_PLAN_TIMEOUT_S → Tor gesperrt
    (PZ_GATE_RETRY_S), Ziel geleert — nächster Tick wählt den nächsten Kandidaten."""
    _wire_two_gates(bot)
    _arm_escape(bot)
    now = time.monotonic()
    bot._pz_target_gate = 1
    bot._pz_validate_goal = (100.0, 0.0)
    bot._pz_validate_result = None
    bot._pz_validate_deadline = now - 0.1          # abgelaufen
    bot._pz_maneuver_tick(now)
    assert bot._pz_target_gate is None
    assert bot._pz_failed_gates.get(1, 0.0) > now
    # Nächster Tick: Ausweich-Kandidat Tor 0
    bot._nav_graph = None
    bot._tick_memo.clear()
    bot._pz_maneuver_tick(time.monotonic())
    assert bot._pz_target_gate == 0


def test_maneuver_all_gates_failed_sets_unreachable(bot):
    """Alle Tore in der Fail-Sperre → _pz_unreachable_until gesetzt → Keep-Gate fällt
    (_pz_worth_keeping False) → der Core-Loop droppt die Flagge."""
    _wire_two_gates(bot)
    _arm_escape(bot)
    now = time.monotonic()
    bot._pz_failed_gates = {0: now + 30.0, 1: now + 30.0}
    bot._pz_maneuver_tick(now)
    assert bot._pz_unreachable_until > now
    bot._tick_memo.clear()
    assert bot._pz_worth_keeping() is False


def test_maneuver_engages_nav_tele_when_close(bot):
    """Endanflug: nah an der Tor-Mitte → NAV_TELE (Overshoot-Fahrt durch die Ebene)."""
    _wire_two_gates(bot)
    _arm_escape(bot)
    bot._ai_state = AIState.SEEKING
    bot.pos_x = 90.0; bot.pos_y = 0.0     # 10u vor Tor 1 (< NAV_TELE_ENGAGE_DIST)
    bot._nav_graph = None
    bot._pz_maneuver_tick(time.monotonic())
    assert bot._ai_state == AIState.NAV_TELE
    assert bot._nav_tele_center == pytest.approx((100.0, 0.0))


def test_tick_seeking_drives_maneuver_and_skips_targeting(bot):
    """SEEKING während Escape: kein Ziel-Erwerb (kein COMBAT), Manöver läuft."""
    _wire_two_gates(bot)
    _arm_escape(bot)
    bot._ai_state = AIState.SEEKING
    bot._nav_graph = None
    with patch.object(bot, "_handle_threat", return_value=False):
        bot._tick_seeking(time.monotonic())
    assert bot._ai_state == AIState.SEEKING
    assert bot._pz_target_gate is not None


# ── Schuss-Gating (gezoned trifft nur Gezonte) ───────────────────────────────

def test_zoned_bot_aims_only_at_zoned_target(bot):
    """Bot gezoned + Ziel ungezoned → kein gezielter Schuss (Phantom-Schüsse können
    Ungezonte nicht treffen); Ziel gezoned → gezielter Schuss läuft."""
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    bot._next_shoot = 0.0
    info = make_player(bot, pid=7, pos=(30.0, 0.0, 0.0))
    bot.target_player = 7
    with patch.object(bot, "_can_shoot", return_value=True), \
         patch.object(bot, "_next_slot_ready", return_value=True), \
         patch.object(bot, "_maybe_shoot_standard") as aimed:
        info.is_phantom_zoned = False
        bot._maybe_shoot(time.monotonic())
        aimed.assert_not_called()
        info.is_phantom_zoned = True
        bot._maybe_shoot(time.monotonic())
        aimed.assert_called_once()


def test_unzoned_pz_carrier_shoots_normally(bot):
    """PZ getragen, aber ungezoned: Schüsse sind normale Schüsse → gezielter Schuss
    auf ungezonte Ziele bleibt erlaubt."""
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = False
    bot._next_shoot = 0.0
    make_player(bot, pid=7, pos=(30.0, 0.0, 0.0))
    bot.target_player = 7
    with patch.object(bot, "_can_shoot", return_value=True), \
         patch.object(bot, "_next_slot_ready", return_value=True), \
         patch.object(bot, "_muzzle_clear", return_value=True), \
         patch.object(bot, "_maybe_shoot_standard") as aimed:
        bot._maybe_shoot(time.monotonic())
        aimed.assert_called_once()


def test_zoned_bot_prefers_zoned_targets(bot):
    """Ziel-Scoring: gezoned wird der (einzig treffbare) gezonte Gegner bevorzugt,
    auch wenn ein ungezonter näher steht."""
    bot.team = 1
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    near = make_player(bot, pid=7, pos=(30.0, 0.0, 0.0))
    far = make_player(bot, pid=8, pos=(90.0, 0.0, 0.0))
    far.is_phantom_zoned = True
    assert bot._find_target_player() == 8


# ── Status-Bits / Resets ─────────────────────────────────────────────────────

def test_drop_flag_clears_zoned(bot):
    """MsgDropFlag (eigener Drop): zoned endet mit dem Flaggenverlust."""
    import struct
    from bzflag.protocol import MsgDropFlag
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    bot._pz_zoned_gate = 1
    bot._on_drop_flag(MsgDropFlag, struct.pack(">B", bot.player_id))
    assert bot.own_flag == ""
    assert bot.is_phantom_zoned is False
    assert bot._pz_zoned_gate is None


def test_add_player_parses_initial_scores(bot):
    """MsgAddPlayer liefert wins/losses/tks — spät joinende Bots kennen die Stände
    damit schon vor dem ersten MsgScore-Broadcast."""
    import struct
    payload = (struct.pack(">BHHHHH", 9, 0, 2, 7, 3, 0)
               + b"Scorer".ljust(32, b"\x00") + b"\x00" * 128)
    bot._on_add_player(0, payload)
    assert bot.players[9].wins == 7
    assert bot.players[9].losses == 3


def test_maneuver_phase2_detours_around_zone_gate(bot):
    """Direkt nach dem Zonen steht der Bot hinter Tor A — die Gerade zu Tor B führt zurück
    durch As Feld (sofortiges Selbes-Tor-Unzonen + ungewollter Drop). Das Manöver setzt
    stattdessen einen Ausweichpunkt seitlich am Torende, auf der Seite des Bots."""
    _wire_two_gates(bot, x0=-80.0, x1=80.0)
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    bot._pz_zoned_gate = 0
    bot.pos_x = -82.0; bot.pos_y = 0.5    # knapp HINTER Tor 0 (Zoning-Überfahrt)
    bot._nav_graph = None
    bot._pz_maneuver_tick(time.monotonic())
    assert bot._pz_target_gate == 1
    tx, ty = bot.target_pos
    assert tx < -70.0, "Ausweichpunkt muss noch bei Tor 0 liegen, nicht direkt (+80, 0)"
    assert abs(ty) >= 8.0, "Ausweichpunkt muss seitlich am Torende vorbeiführen"


def test_maneuver_phase2_free_line_targets_gate_directly(bot):
    """Kreuzt die Gerade zum Ziel-Tor das Zone-Tor-Feld nicht (Bot schon seitlich versetzt),
    fährt das Manöver das Ziel-Tor direkt an."""
    _wire_two_gates(bot, x0=-80.0, x1=80.0)
    bot.own_flag = "PZ"
    bot.is_phantom_zoned = True
    bot._pz_zoned_gate = 0
    bot.pos_x = -84.0; bot.pos_y = 20.0   # seitlich hinter Tor 0: Gerade verfehlt das Feld
    bot._nav_graph = None
    bot._pz_maneuver_tick(time.monotonic())
    assert bot._pz_target_gate == 1
    assert bot.target_pos == pytest.approx((80.0, 0.0))
