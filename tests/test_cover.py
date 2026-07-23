"""Tests für P4-TAC-02 (Deckung): Silhouetten-basierte Deckungs-/Kanten-Erkennung
(_covered_from / _cover_edge_ahead) und der defensive COVER_HOLD-State
(_should_hold_in_cover / _tick_cover_hold inkl. Peek-Zyklus).
"""
import math
import time

import pytest

from bzflag.world_map import BoxObstacle, WorldMap
from bzflag.nav_graph import NavGraph
from bot.models import AIState
from bot.constants import (
    TANK_HALF_DIAG, COVER_HOLD_MAX_S, COVER_HOLD_COOLDOWN_S,
    COVER_PEEK_OUT_S, COVER_PEEK_BACK_S, COVER_CLOSE_EXIT_FRAC, RICO_AIM_MAX_COVER,
)
from conftest import make_player


# ---------------------------------------------------------------------------
# Hilfsfunktionen: Mini-Welt mit einer Box + NavGraph an den Bot hängen
# ---------------------------------------------------------------------------

def _attach_box(bot, cx=30.0, cy=0.0, half_w=10.0, half_d=10.0, height=10.0, angle=0.0):
    """Baut eine WorldMap mit genau einer Box und hängt einen NavGraph an den Bot.
    Ohne NavGraph liefert _segment_clear immer True (nirgends Deckung), daher Pflicht."""
    box = BoxObstacle(cx=cx, cy=cy, bottom_z=0.0, angle=angle,
                      half_w=half_w, half_d=half_d, height=height)
    wm = WorldMap(boxes=[box], teleporters=[], links=[],
                  world_half=2000.0, world_hash="cover-test")
    bot._nav_graph = NavGraph(wm)
    bot._world_map = wm
    return box


# ---------------------------------------------------------------------------
# _covered_from — Silhouetten-Test (Rand statt Zentrum)
# ---------------------------------------------------------------------------

class TestCoveredFrom:
    def test_behind_box_is_covered(self, bot):
        """Bot direkt hinter der Box (Gegner weit in +x) → beide Ränder verdeckt → True."""
        _attach_box(bot)
        bot.pos_x, bot.pos_y, bot.pos_z = 10.0, 0.0, 0.0
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0))
        assert bot._covered_from(2, time.monotonic()) is True

    def test_nose_pokes_out_is_not_covered(self, bot):
        """Zentrum verdeckt, aber ein Silhouetten-Rand (±TANK_HALF_DIAG) schaut über die
        Box-Kante hinaus → NICHT gedeckt (genau der Fall, den der Rand-Test fangen soll)."""
        _attach_box(bot)
        bot.pos_x, bot.pos_y, bot.pos_z = 10.0, 8.0, 0.0
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0))
        # Zentrum wäre gedeckt (Ray y≈8 < Box-Kante 10) …
        assert bot._segment_clear(1000.0, 0.0, 1.57, 10.0, 8.0, 1.57) is False
        # … aber der obere Rand (~y 11.3) schaut heraus → gesamt nicht gedeckt.
        assert bot._covered_from(2, time.monotonic()) is False

    def test_box_too_low_is_not_covered(self, bot):
        """Box niedriger als Mündungshöhe → Schuss-Rays gehen darüber → keine Deckung."""
        _attach_box(bot, height=1.0)
        bot.pos_x, bot.pos_y, bot.pos_z = 10.0, 0.0, 0.0
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0))
        assert bot._covered_from(2, time.monotonic()) is False

    def test_airborne_enemy_shoots_over_cover(self, bot):
        """Gegner am Boden → gedeckt; springt der Gegner hoch genug, schießt er über die
        Box (Ursprung = Gegner-Mündungshöhe, nutzt info.pos[2]) → nicht mehr gedeckt."""
        _attach_box(bot, height=5.0)
        bot.pos_x, bot.pos_y, bot.pos_z = 10.0, 0.0, 0.0
        p = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
        assert bot._covered_from(2, time.monotonic()) is True
        p.pos[2] = 40.0   # Gegner in der Luft
        bot._tick_memo.clear()   # Memo je Tick — hier manuell leeren
        assert bot._covered_from(2, time.monotonic()) is False

    def test_dead_enemy_is_not_covered(self, bot):
        _attach_box(bot)
        bot.pos_x, bot.pos_y, bot.pos_z = 10.0, 0.0, 0.0
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0), alive=False)
        assert bot._covered_from(2, time.monotonic()) is False


# ---------------------------------------------------------------------------
# _cover_edge_ahead — an der Kante (jetzt gedeckt, 1 Tanklänge voraus exponiert)
# ---------------------------------------------------------------------------

class TestCoverEdgeAhead:
    def test_at_edge_is_true(self, bot):
        """Bot gedeckt, aber eine Tanklänge in Fahrtrichtung (+y) exponiert → Kante."""
        _attach_box(bot)
        bot.pos_x, bot.pos_y, bot.pos_z = 10.0, 2.0, 0.0
        bot.azimuth = math.pi / 2   # nach +y (aus der Deckung heraus)
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0))
        assert bot._covered_from(2, time.monotonic()) is True
        assert bot._cover_edge_ahead(2, time.monotonic()) is True

    def test_deep_in_cover_is_false(self, bot):
        """Bot tief in Deckung: auch der Probepunkt (1 Tanklänge voraus) ist gedeckt → keine Kante."""
        _attach_box(bot)
        bot.pos_x, bot.pos_y, bot.pos_z = 10.0, 0.0, 0.0
        bot.azimuth = math.pi / 2
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0))
        assert bot._covered_from(2, time.monotonic()) is True
        assert bot._cover_edge_ahead(2, time.monotonic()) is False

    def test_exposed_is_false(self, bot):
        """Bot gar nicht in Deckung → keine Kante (Vorbedingung _covered_from schlägt fehl)."""
        _attach_box(bot)
        bot.pos_x, bot.pos_y, bot.pos_z = 10.0, 40.0, 0.0
        bot.azimuth = math.pi / 2
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0))
        assert bot._covered_from(2, time.monotonic()) is False
        assert bot._cover_edge_ahead(2, time.monotonic()) is False

    def test_wall_slide_uses_tangent(self, bot):
        """Wall-Slide: Azimut zeigt leicht in die Box-Wand (Probe stur entlang Azimut läge IM
        Gebäude). Die Wand-Tangente lenkt die Probe entlang der Wand → Kante wird erkannt."""
        _attach_box(bot)
        bot.pos_x, bot.pos_y, bot.pos_z = 18.0, 2.0, 0.0   # dicht an der Box-Rückwand (x=20)
        bot.azimuth = math.radians(45.0)   # nach +x/+y, also in die Wand hinein
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0))
        assert bot._covered_from(2, time.monotonic()) is True
        assert bot._cover_edge_ahead(2, time.monotonic()) is True

    def test_wall_slide_far_from_edge_is_false(self, bot):
        """Gleiche Schleif-Situation, aber die Kante ist entlang der Tangente > 1 Tanklänge
        entfernt (Probepunkt bleibt gedeckt) → keine Kante, kein Fehlalarm."""
        _attach_box(bot)
        bot.pos_x, bot.pos_y, bot.pos_z = 18.0, -2.0, 0.0
        bot.azimuth = math.radians(45.0)
        make_player(bot, 2, pos=(1000.0, 0.0, 0.0))
        assert bot._covered_from(2, time.monotonic()) is True
        assert bot._cover_edge_ahead(2, time.monotonic()) is False


# ---------------------------------------------------------------------------
# Verhalten: _should_hold_in_cover / _tick_combat-Eingang / _tick_cover_hold
# (Geometrie hier gemockt, um die Zustands-Logik isoliert zu testen)
# ---------------------------------------------------------------------------

class TestCoverHoldBehaviour:
    def _setup(self, bot, monkeypatch, enemy_flag="", edge=True, covered=True,
               dist=100.0):
        bot.pos_x, bot.pos_y, bot.pos_z = 0.0, 0.0, 0.0
        bot.azimuth = 0.0
        bot.target_player = 2
        bot._nav_path = []          # Direktmodus
        bot._cover_cooldown_until = 0.0
        p = make_player(bot, 2, pos=(dist, 0.0, 0.0), is_human=True, flag=enemy_flag)
        p.alive = True
        monkeypatch.setattr(bot, "_cover_edge_ahead", lambda pid, now=0.0: edge)
        monkeypatch.setattr(bot, "_covered_from", lambda pid, now=0.0: covered)
        return p

    def test_enters_cover_hold(self, bot, monkeypatch):
        self._setup(bot, monkeypatch, edge=True)
        now = time.monotonic()
        assert bot._should_hold_in_cover(now) is True
        bot._ai_state = AIState.COMBAT
        bot._tick_combat(now)
        assert bot._ai_state == AIState.COVER_HOLD
        assert bot._cover_hold_until > now

    def test_no_enter_when_nav_path_active(self, bot, monkeypatch):
        """A*-Modus (nav_path gesetzt) → kein Eintritt, Navigation nicht unterbrechen."""
        self._setup(bot, monkeypatch, edge=True)
        bot._nav_path = [(50.0, 0.0, 0.0)]
        assert bot._should_hold_in_cover(time.monotonic()) is False

    def test_no_enter_out_of_range(self, bot, monkeypatch):
        self._setup(bot, monkeypatch, edge=True, dist=2000.0)
        assert bot._should_hold_in_cover(time.monotonic()) is False

    def test_no_enter_sb_enemy(self, bot, monkeypatch):
        """Super-Bullet durchschlägt Wände → Deckung wertlos → kein Eintritt."""
        self._setup(bot, monkeypatch, enemy_flag="SB", edge=True)
        assert bot._should_hold_in_cover(time.monotonic()) is False

    def test_no_enter_sw_enemy(self, bot, monkeypatch):
        self._setup(bot, monkeypatch, enemy_flag="SW", edge=True)
        assert bot._should_hold_in_cover(time.monotonic()) is False

    def test_enter_gm_enemy(self, bot, monkeypatch):
        """GM: Deckung bricht den Lock → ausdrücklich erwünscht (keine Ausnahme)."""
        self._setup(bot, monkeypatch, enemy_flag="GM", edge=True)
        assert bot._should_hold_in_cover(time.monotonic()) is True

    def test_cooldown_blocks_reentry(self, bot, monkeypatch):
        self._setup(bot, monkeypatch, edge=True)
        now = time.monotonic()
        bot._cover_cooldown_until = now + 5.0
        assert bot._should_hold_in_cover(now) is False

    def test_exit_on_timeout(self, bot, monkeypatch):
        """Nach Ablauf von _cover_hold_until → zurück in den Boden-State + Cooldown gesetzt."""
        self._setup(bot, monkeypatch, edge=True, covered=True)
        now = time.monotonic()
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now - 1.0   # bereits abgelaufen
        bot._tick_cover_hold(now)
        assert bot._ai_state != AIState.COVER_HOLD
        assert bot._cover_cooldown_until >= now + COVER_HOLD_COOLDOWN_S - 0.01

    def test_exit_when_cover_lost(self, bot, monkeypatch):
        """Gegner umrundet die Box (_covered_from False) → vorzeitiger Ausgang."""
        self._setup(bot, monkeypatch, edge=True, covered=False)
        now = time.monotonic()
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now + COVER_HOLD_MAX_S
        bot._tick_cover_hold(now)
        assert bot._ai_state != AIState.COVER_HOLD

    def test_exit_when_enemy_takes_sb(self, bot, monkeypatch):
        """Gegner nimmt SB auf → Deckung wertlos → Ausgang."""
        p = self._setup(bot, monkeypatch, edge=True, covered=True)
        now = time.monotonic()
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now + COVER_HOLD_MAX_S
        p.flag = "SB"
        bot._tick_cover_hold(now)
        assert bot._ai_state != AIState.COVER_HOLD

    def test_peek_starts_and_cycles(self, bot, monkeypatch):
        """Peek: Phase 0 → 1 (vorfahren) bei bestandenem Zufalls-Gate; die 60-Hz-Ausführung
        schaltet 1 → 2 (rückwärts) → 0 nach den Phasen-Deadlines."""
        self._setup(bot, monkeypatch, edge=True, covered=True)
        now = time.monotonic()
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now + COVER_HOLD_MAX_S
        bot._cover_peek_phase = 0
        monkeypatch.setattr("random.random", lambda: 0.0)   # Gate bestehen
        bot._tick_cover_hold(now)
        assert bot._cover_peek_phase == 1
        assert bot._cover_peek_until == pytest.approx(now + COVER_PEEK_OUT_S)

    def test_peek_execution_moves_and_returns(self, bot, monkeypatch):
        """60-Hz-Dispatch: Phase 1 fährt vorwärts, nach Deadline → Phase 2 rückwärts, dann → 0."""
        self._setup(bot, monkeypatch, edge=True, covered=True)
        now = time.monotonic()
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now + COVER_HOLD_MAX_S
        bot.azimuth = 0.0
        # Phase 1: vorfahren (vel in +x, da azimuth 0)
        bot._cover_peek_phase = 1
        bot._cover_peek_until = now + COVER_PEEK_OUT_S
        bot._dispatch_movement(1.0 / 60.0, now, ai_tick=False)
        assert bot.vel_x > 0.0
        # Deadline überschritten → Phase 2 (rückwärts)
        bot._cover_peek_until = now - 0.01
        bot._cover_peek_phase = 1
        bot._dispatch_movement(1.0 / 60.0, now, ai_tick=False)
        assert bot._cover_peek_phase == 2
        # Phase 2 abgelaufen → zurück auf Halten (Phase 0)
        bot._cover_peek_until = now - 0.01
        bot._dispatch_movement(1.0 / 60.0, now, ai_tick=False)
        assert bot._cover_peek_phase == 0


class TestCoverHoldTac05:
    """P4-TAC-05: Gegner-Schuss-Slots steuern Eingang/Ausgang/Peek von COVER_HOLD."""

    def _setup(self, bot, monkeypatch, dist=100.0):
        bot.pos_x, bot.pos_y, bot.pos_z = 0.0, 0.0, 0.0
        bot.azimuth = 0.0
        bot.target_player = 2
        bot._nav_path = []
        bot._cover_cooldown_until = 0.0
        bot._max_shots = 1
        bot._slot_reload_at = [0.0]      # eigener Slot bereit
        bot._shot_slot = 0
        p = make_player(bot, 2, pos=(dist, 0.0, 0.0), is_human=True)
        p.alive = True
        monkeypatch.setattr(bot, "_cover_edge_ahead", lambda pid, now=0.0: True)
        monkeypatch.setattr(bot, "_covered_from", lambda pid, now=0.0: True)
        return p

    def test_no_enter_when_enemy_empty_wide_window(self, bot, monkeypatch):
        """Gegner leergeschossen + großes Fenster → nicht verstecken, angreifen."""
        p = self._setup(bot, monkeypatch)
        now = time.monotonic()
        p.slot_reload_at = [now + 3.0]
        assert bot._should_hold_in_cover(now) is False

    def test_enter_when_window_too_small(self, bot, monkeypatch):
        """Gegner zwar leer, aber gleich wieder geladen → Fenster zu klein → weiter halten."""
        p = self._setup(bot, monkeypatch)
        now = time.monotonic()
        p.slot_reload_at = [now + 0.5]
        assert bot._should_hold_in_cover(now) is True

    def test_exit_when_enemy_empty_and_own_ready(self, bot, monkeypatch):
        """In COVER_HOLD: Gegner leer + Fenster groß + eigener Slot bereit → sofort raus."""
        p = self._setup(bot, monkeypatch)
        now = time.monotonic()
        p.slot_reload_at = [now + 3.0]
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now + COVER_HOLD_MAX_S   # Timeout NICHT erreicht
        bot._tick_cover_hold(now)
        assert bot._ai_state != AIState.COVER_HOLD

    def test_no_exit_when_own_slot_not_ready(self, bot, monkeypatch):
        """Gegner leer, aber eigener Schuss noch nicht bereit → nicht ausbrechen (nichts zu tun)."""
        p = self._setup(bot, monkeypatch)
        now = time.monotonic()
        p.slot_reload_at = [now + 3.0]
        bot._slot_reload_at = [now + 5.0]      # eigener Slot im Cooldown
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now + COVER_HOLD_MAX_S
        bot._tick_cover_hold(now)
        assert bot._ai_state == AIState.COVER_HOLD

    def test_no_peek_when_enemy_empty(self, bot, monkeypatch):
        """Gegner leer → Peek sinnlos (provoziert nichts) → kein Peek-Start trotz Zufalls-Gate."""
        p = self._setup(bot, monkeypatch)
        now = time.monotonic()
        p.slot_reload_at = [now + 0.5]         # leer, aber Fenster klein → kein Ausbruch, aber auch kein Peek
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now + COVER_HOLD_MAX_S
        bot._cover_peek_phase = 0
        monkeypatch.setattr("random.random", lambda: 0.0)
        bot._tick_cover_hold(now)
        assert bot._cover_peek_phase == 0

    def test_close_exit(self, bot, monkeypatch):
        """Gegner sehr nah (< optimale Distanz × Faktor) → COMBAT übernimmt wieder das Abstandhalten."""
        p = self._setup(bot, monkeypatch, dist=bot._effective_optimal_range() * COVER_CLOSE_EXIT_FRAC - 5.0)
        now = time.monotonic()
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_hold_until = now + COVER_HOLD_MAX_S
        bot._tick_cover_hold(now)
        assert bot._ai_state != AIState.COVER_HOLD

    def test_rico_drive_aim_uses_cache_without_los(self, bot, monkeypatch):
        """Ohne LoS dreht COVER_HOLD auf den gecachten Abprall-Azimut statt auf den Gegner."""
        self._setup(bot, monkeypatch)
        now = time.monotonic()
        bot._rico_aim_cache = (now, 2, (1.234, False))
        monkeypatch.setattr(bot, "_has_los_to_enemy", lambda pid: False)
        assert bot._cover_hold_aim_az(2, (100.0, 0.0)) == pytest.approx(1.234)
        # Mit LoS: direkt aufs Ziel (az = 0 bei Gegner in +x).
        monkeypatch.setattr(bot, "_has_los_to_enemy", lambda pid: True)
        assert bot._cover_hold_aim_az(2, (100.0, 0.0)) == pytest.approx(0.0)

    def test_wide_sweep_param_passed_through(self, bot, monkeypatch):
        """_find_ricochet_aim_angle reicht den breiteren Deckungs-Sweep an _compute_ricochet_aim durch."""
        self._setup(bot, monkeypatch)
        bot._rico_aim_cache = None
        captured = {}
        def _fake(pid, pp, amax=45):
            captured["amax"] = amax
            return None
        monkeypatch.setattr(bot, "_compute_ricochet_aim", _fake)
        bot._find_ricochet_aim_angle(2, None, RICO_AIM_MAX_COVER)
        assert captured["amax"] == RICO_AIM_MAX_COVER


class TestCoverHoldPeekMomentum:
    """P4-MOV-02b: Der COVER_HOLD-Peek (Vor-/Zurückpendel) ist Bodenfahrt → die Geschwindigkeit
    rampt bei aktivem -a hoch, statt instant auf 0.6×tank_speed zu springen (ohne -a unverändert)."""

    def _setup_peek(self, bot):
        now = time.monotonic()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.target_player = None          # kein Gegner → reine Peek-Bewegung, keine Aim-Drehung
        bot._jumping = False
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_peek_phase = 1          # Phase 1: vorfahren (0.6×tank_speed)
        bot._cover_peek_until = now + 10.0
        return now

    def test_peek_ramps_with_dash_a(self, bot):
        now = self._setup_peek(bot)
        bot._linear_acceleration = 10.0    # max_delta = 20×10×0.02 = 4.0 u/s pro Tick
        bot._dispatch_movement(0.02, now, ai_tick=False)
        peek_speed = bot._tank_speed * 0.6
        assert bot.vel_x == pytest.approx(4.0, abs=1e-6)   # gerampt, noch nicht bei 0.6×speed
        assert bot.vel_x < peek_speed
        # F2: _apply_bounds integriert mit der NEUEN (gerampten) vel — wie beim LANDING_SHOT-Fix.
        assert bot.pos_x == pytest.approx(4.0 * 0.02, abs=1e-6)

    def test_peek_instant_without_dash_a(self, bot):
        now = self._setup_peek(bot)
        bot._linear_acceleration = 0.0
        bot._dispatch_movement(0.02, now, ai_tick=False)
        assert bot.vel_x == pytest.approx(bot._tank_speed * 0.6, abs=1e-6)
        # F2: auch ohne -a bewegt der Peek jetzt real (vorher Bug: pos blieb 0).
        assert bot.pos_x == pytest.approx(bot._tank_speed * 0.6 * 0.02, abs=1e-6)


class TestCoverHoldPeekIntegration:
    """F2: Der Peek-Zyklus bewegt den Bot jetzt real (nicht nur auf dem Wire) — Phase 1
    (vorfahren) lässt pos_x monoton wachsen, Phase 2 (rückwärts) lässt sie wieder sinken."""

    def _setup_peek(self, bot):
        now = time.monotonic()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.target_player = None          # kein Gegner → reine Peek-Bewegung, keine Aim-Drehung
        bot._jumping = False
        bot._ai_state = AIState.COVER_HOLD
        bot._cover_peek_until = now + 10.0
        return now

    def test_peek_phase1_advances_position(self, bot):
        """Phase 1 (vorfahren) ohne -a: pos_x wächst über mehrere Ticks hinweg monoton."""
        now = self._setup_peek(bot)
        bot._linear_acceleration = 0.0
        bot._cover_peek_phase = 1
        dt = 0.02
        last_x = bot.pos_x
        for _ in range(10):
            bot._dispatch_movement(dt, now, ai_tick=False)
            assert bot.pos_x > last_x   # real vorgefahren, nicht nur vel gesetzt
            last_x = bot.pos_x
            now += dt

    def test_peek_phase2_retreats_position(self, bot):
        """Phase 2 (rückwärts) ohne -a: pos_x sinkt über mehrere Ticks hinweg."""
        now = self._setup_peek(bot)
        bot._linear_acceleration = 0.0
        bot._cover_peek_phase = 2
        dt = 0.02
        start_x = bot.pos_x
        for _ in range(10):
            bot._dispatch_movement(dt, now, ai_tick=False)
            now += dt
        assert bot.pos_x < start_x
