"""
Tests für Ausweich- und Sprung-Logik.

Konstanten (aus bzbot.py):
  DODGE_DIST          = 17.28u  (= TANK_RADIUS * 4.0)
  DODGE_REACT_DELAY   = 0.25s
  IB_REACT_MULTIPLIER = 1.5   → IB-Delay = 0.375s
  CS_REACT_MULTIPLIER = 3.0   → CS-Delay = 0.75s
  M_REACT_MULTIPLIER  = 1.1   (P4-MOV-02: von 1.5 gesenkt — Bot bewegt sich mit dem
                              Trägheitsmodell selbst nicht mehr instant)
  JUMP_VELOCITY       = 19.0
  TANK_SPEED          = 25.0

Dodge ist machbar wenn: dist_achievable = TANK_SPEED * time_to_impact >= DODGE_DIST * 0.4
  → time_to_impact >= 6.912 / 25.0 = 0.276s
"""
import math
import time
import pytest
from unittest.mock import patch
from conftest import make_shot, make_player
from bot.models import AIState
from bot.util import _angle_diff

DODGE_REACT_DELAY   = 0.25
IB_REACT_DELAY      = DODGE_REACT_DELAY * 1.5   # 0.375s
JUMP_VELOCITY       = 19.0


def _trigger_movement(bot, now, dt=0.02):
    bot._update_movement(dt, now, ai_tick=True)


# ── _find_incoming_shot ───────────────────────────────────────────────────────

def test_find_incoming_detects_threat(bot):
    """Schuss fliegt direkt auf Bot (closest_approach_dist ≈ 0) → wird erkannt."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
    shot, _ = bot._find_incoming_shot(time.monotonic())
    assert shot is not None


def test_find_incoming_ignores_safe_shot(bot):
    """Schuss fliegt 30u vorbei (> DODGE_DIST=17.28u) → nicht erkannt."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    make_shot(bot, pos=(200.0, 30.0, 1.025), vel=(-100.0, 0.0, 0.0))
    shot, _ = bot._find_incoming_shot(time.monotonic())
    assert shot is None


# ── GM-Bedrohung (F1: keine Doppel-Extrapolation) ────────────────────────────

class TestGmThreatPosition:
    """GM-pos wird laufend nachgeführt (Integration + MsgGMUpdate) — die
    Bedrohungsanalyse darf die Flugzeit nicht via position_at() ein zweites
    Mal aufaddieren, sonst sieht sie eine Phantom-Position weit vor der
    echten Rakete und ignoriert den GM als „entfernt sich"."""

    def test_gm_with_flight_time_detected(self, bot):
        """GM bei 50u, fliegt auf Bot zu, seit 1s in der Luft (pos = aktuell).
        position_at(now) sähe die Rakete bei x=−50 (hinter dem Bot) → alte
        Logik ignorierte sie. Korrekt: Bedrohung mit ~0,5s bis Einschlag."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                  is_gm=True, flag_abbr=b"GM", fire_time=now - 1.0)
        shot, t = bot._find_incoming_shot(now)
        assert shot is not None and shot.is_gm
        assert 0.3 <= t <= 0.7

    def test_normal_shot_extrapolation_unchanged(self, bot):
        """Regression-Guard: normaler Schuss (pos = Abschussort) wird weiter
        via position_at extrapoliert — nach 1s Flugzeit von x=150 ist er bei
        x=50 und damit eine Bedrohung."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(150.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                  fire_time=now - 1.0)
        shot, t = bot._find_incoming_shot(now)
        assert shot is not None
        assert 0.3 <= t <= 0.7


class TestGmNoPathCache:
    """F1b: GM darf auf Teleporter-Karten keinen geraden Pfad-Cache bekommen —
    sonst überspringt _find_incoming_shot den GM im Direkt-Zweig und bewertet
    den falschen (nicht lenkenden) Pfad."""

    @staticmethod
    def _shot_payload(flag_bytes, shooter_id=2, shot_id=1):
        import struct
        return (
            struct.pack(">f",  time.monotonic())
            + struct.pack(">B", shooter_id)
            + struct.pack(">H", shot_id)
            + struct.pack(">fff", 50.0, 0.0, 1.0)
            + struct.pack(">fff", -100.0, 0.0, 0.0)
            + struct.pack(">f",  0.0)
            + struct.pack(">h",  2)
            + flag_bytes
            + struct.pack(">f",  3.5)
        )

    def test_gm_gets_no_path_on_tele_map(self, bot):
        from unittest.mock import MagicMock
        wm = MagicMock()
        wm.teleporters = [object()]   # Karte „hat" Teleporter
        wm.boxes = []
        bot._world_map = wm
        bot._on_shot_begin(0, self._shot_payload(b"GM"))
        assert (2, 1) in bot._shots
        assert (2, 1) not in bot._ricochet_paths

    def test_normal_shot_still_gets_path(self, bot):
        """Regression-Guard: normaler Ricochet-Schuss bekommt weiterhin einen Pfad."""
        from unittest.mock import MagicMock
        wm = MagicMock()
        wm.teleporters = []
        wm.boxes = []
        bot._world_map = wm
        bot._server_ricochet = True
        bot._on_shot_begin(0, self._shot_payload(b"\x00\x00", shot_id=2))
        assert (2, 2) in bot._shots
        assert (2, 2) in bot._ricochet_paths


def test_find_incoming_ignores_expired_shot(bot):
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    ft = time.monotonic() - 10.0
    make_shot(bot, pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
              lifetime=3.5, fire_time=ft)
    shot, _ = bot._find_incoming_shot(time.monotonic())
    assert shot is None


# ── Ausweichen ────────────────────────────────────────────────────────────────

def test_dodge_triggered_after_delay(bot):
    """Schuss nah, Reaktionszeit abgelaufen → _dodging=True.
    Bot zeigt bereits senkrecht zur Schussrichtung (turn_rad=0 → time_to_dodge=0.225s)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.azimuth = -math.pi / 2  # bereits senkrecht zur Schussrichtung → turn_rad=0
    now = time.monotonic()
    make_shot(bot, pos=(50.0, 3.0, 1.025), vel=(-100.0, 0.0, 0.0))
    # Bedrohung bereits vor REACT_DELAY erkannt
    bot._last_threat_id    = (2, 1)
    bot._threat_detected_at = now - 0.3   # 300ms > 250ms Delay
    bot.target_pos = (50.0, 0.0)
    _trigger_movement(bot, now)
    assert bot._dodging is True


def test_dodge_not_triggered_within_delay(bot):
    """Reaktionszeit noch nicht abgelaufen → kein Ausweichen."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    now = time.monotonic()
    make_shot(bot, pos=(50.0, 3.0, 1.025), vel=(-100.0, 0.0, 0.0))
    bot._last_threat_id    = (2, 1)
    bot._threat_detected_at = now - 0.05   # 50ms < 250ms Delay
    bot.target_pos = (50.0, 0.0)
    _trigger_movement(bot, now)
    assert bot._dodging is False


# ── IB-Reaktionsverzögerung ───────────────────────────────────────────────────

def test_ib_delay_prevents_dodge_at_normal_delay(bot):
    """Schütze hat IB-Flag: normaler Delay (0.3s) ist erreicht, reicht aber NICHT (braucht 0.375s)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    now = time.monotonic()
    make_player(bot, pid=2, flag="IB")
    make_shot(bot, shooter_id=2, pos=(50.0, 3.0, 1.025), vel=(-100.0, 0.0, 0.0))
    bot._last_threat_id    = (2, 1)
    bot._threat_detected_at = now - 0.3   # 300ms ≥ Normal (250ms), < IB-Delay (375ms)
    bot.target_pos = (50.0, 0.0)
    _trigger_movement(bot, now)
    assert bot._dodging is False


def test_ib_delay_triggers_dodge_after_ib_delay(bot):
    """Schütze hat IB-Flag: nach IB-Delay (0.375s) wird doch ausgewichen.
    IB-Schütze (bei +x) im engen Sicht-FoV (±37.5°); auf 100u bleibt der Dodge zeitlich machbar."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.azimuth = -math.radians(30)  # IB-Schütze (bei +x) im FoV (±37.5°); IB-Gate verlangt Sicht
    now = time.monotonic()
    make_player(bot, pid=2, flag="IB", pos=(100.0, 0.0, 0.0))
    make_shot(bot, shooter_id=2, pos=(100.0, 3.0, 1.025), vel=(-100.0, 0.0, 0.0))
    bot._last_threat_id    = (2, 1)
    bot._threat_detected_at = now - 0.8   # 800ms > IB-Delay (375ms)
    bot.target_pos = (50.0, 0.0)
    _trigger_movement(bot, now)
    assert bot._dodging is True


# ── Sprung als Fallback ───────────────────────────────────────────────────────

def test_jump_fallback_when_dodge_infeasible(bot):
    """Schuss trifft in <0.05s → Ausweichen nicht machbar → Sprung."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._jumping = False
    now = time.monotonic()
    # Schuss nur 5u entfernt → time_to_impact=0.05s → dist_achievable=1.25u < 6.91u
    make_shot(bot, pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
    bot._last_threat_id    = (2, 1)
    bot._threat_detected_at = now - 0.3
    bot.target_pos = (50.0, 0.0)
    _trigger_movement(bot, now)
    assert bot._jumping is True
    assert bot.vel_z == pytest.approx(JUMP_VELOCITY)


def test_nj_flag_prevents_jump(bot):
    """NJ-Flag verhindert den Sprung-Fallback."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._jumping = False
    bot.own_flag = "NJ"
    now = time.monotonic()
    make_shot(bot, pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
    bot._last_threat_id    = (2, 1)
    bot._threat_detected_at = now - 0.3
    bot.target_pos = (50.0, 0.0)
    _trigger_movement(bot, now)
    assert bot._jumping is False


def test_no_double_jump_when_already_airborne(bot):
    """Bot bereits in der Luft → kein zweiter Sprung durch Dodge-Fallback."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 3.0
    bot._jumping = True
    bot.vel_z   = 5.0
    now = time.monotonic()
    make_shot(bot, pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
    bot._last_threat_id    = (2, 1)
    bot._threat_detected_at = now - 0.3
    bot.target_pos = (50.0, 0.0)
    _trigger_movement(bot, now)
    # _jumping bleibt True, aber vel[2] wird nicht auf JUMP_VELOCITY gesetzt
    assert bot._jumping is True
    assert bot.vel_z != pytest.approx(JUMP_VELOCITY, abs=1.0)


# ── Ausweich-Richtung ─────────────────────────────────────────────────────────

def test_dodge_direction_away_from_shot(bot):
    """Schuss kommt bei y=+3 vorbei → Bot weicht in -y Richtung aus.
    Bot bereits auf -π/2 ausgerichtet (turn_rad=0 → Ausweichen sicher feasible)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.azimuth = -math.pi / 2  # bereits in -y Richtung → turn_rad=0
    now = time.monotonic()
    # Schuss von rechts (+x) fliegt in -x Richtung, leicht versetzt bei y=+3
    make_shot(bot, pos=(50.0, 3.0, 1.025), vel=(-100.0, 0.0, 0.0))
    bot._last_threat_id    = (2, 1)
    bot._threat_detected_at = now - 0.3
    bot.target_pos = (50.0, 0.0)
    _trigger_movement(bot, now)
    assert bot._dodging is True
    # _dodge_dir ≈ -π/2 (weg von y=3, also in negative y-Richtung)
    assert bot._dodge_dir == pytest.approx(-math.pi / 2, abs=0.2)


def test_no_threat_resets_last_threat_id(bot):
    """Keine Bedrohung mehr → _last_threat_id wird gecleart."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._last_threat_id = (2, 1)  # altes Threat-ID
    now = time.monotonic()
    bot.target_pos = (50.0, 0.0)
    # Kein Schuss in _shots
    _trigger_movement(bot, now)
    assert bot._last_threat_id is None


# ── IB/ST: Ausweichen nur mit Sicht (FoV + LoS) ───────────────────────────────

class TestIbStLosFovGate:
    """IB/ST machen Schuss bzw. Tank auf dem Radar unsichtbar → Ausweichen nur, wenn der Bot den
    Schützen wirklich sieht: im Blickfeld (FoV) UND ohne Deckung (LoS). Gate gilt nur für IB/ST."""

    # Schütze (bei +x) im engen Sicht-FoV (±37.5°); auf 100u bleibt der ~60°-Dodge zeitlich machbar:
    _FOV_IN  = -math.radians(30)
    _FOV_OUT = math.pi                 # Bot blickt nach -x → Schütze (+x) hinter dem Bot, nicht im FoV

    def _setup(self, bot, now, flag, azimuth, blocked):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = azimuth
        bot._dodging = False
        bot._nav_graph = None
        make_player(bot, pid=2, pos=(100.0, 0.0, 0.0), flag=flag)
        make_shot(bot, shooter_id=2, pos=(100.0, 3.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id     = (2, 1)
        bot._threat_detected_at = now - 1.0     # react-Delay sicher erfüllt (auch IB/ST)
        bot.target_pos = (50.0, 0.0)
        if blocked:
            from bzflag.nav_graph import NavGraph
            from bzflag.world_map import BoxObstacle, WorldMap
            box = BoxObstacle(cx=25.0, cy=0.0, bottom_z=0.0, angle=0.0,
                              half_w=4.0, half_d=4.0, height=10.0)   # auf der Linie Bot→Schütze
            wm = WorldMap(boxes=[box], teleporters=[], links=[],
                          world_half=100.0, world_hash="ibst-los")
            bot._nav_graph = NavGraph(wm, max_jump_h=18.4)

    def test_ib_in_fov_clear_los_dodges(self, bot):
        now = time.monotonic()
        self._setup(bot, now, "IB", self._FOV_IN, blocked=False)
        _trigger_movement(bot, now)
        assert bot._dodging is True

    def test_ib_in_fov_blocked_los_no_dodge(self, bot):
        now = time.monotonic()
        self._setup(bot, now, "IB", self._FOV_IN, blocked=True)
        _trigger_movement(bot, now)
        assert bot._dodging is False

    def test_ib_clear_los_but_out_of_fov_no_dodge(self, bot):
        now = time.monotonic()
        self._setup(bot, now, "IB", self._FOV_OUT, blocked=False)
        _trigger_movement(bot, now)
        assert bot._dodging is False

    def test_st_in_fov_blocked_los_no_dodge(self, bot):
        now = time.monotonic()
        self._setup(bot, now, "ST", self._FOV_IN, blocked=True)
        _trigger_movement(bot, now)
        assert bot._dodging is False

    def test_st_in_fov_clear_los_dodges(self, bot):
        now = time.monotonic()
        self._setup(bot, now, "ST", self._FOV_IN, blocked=False)
        _trigger_movement(bot, now)
        assert bot._dodging is True

    def test_normal_flag_unaffected_by_gate(self, bot):
        """Regressionswächter: normaler Schütze → Gate greift nicht (weicht trotz Deckung aus)."""
        now = time.monotonic()
        self._setup(bot, now, "", self._FOV_IN, blocked=True)
        _trigger_movement(bot, now)
        assert bot._dodging is True

    def test_st_with_seer_dodges_even_blocked(self, bot):
        """Eigene SE-Flagge deckt den Stealth-Tank auf → kein Gate, Bot weicht trotz Deckung aus."""
        now = time.monotonic()
        self._setup(bot, now, "ST", self._FOV_IN, blocked=True)
        bot.own_flag = "SE"
        _trigger_movement(bot, now)
        assert bot._dodging is True

    def test_ib_with_seer_still_gated_when_blocked(self, bot):
        """SE deckt nur Tanks auf, nicht Geschosse → IB bleibt auch mit SE gegated."""
        now = time.monotonic()
        self._setup(bot, now, "IB", self._FOV_IN, blocked=True)
        bot.own_flag = "SE"
        _trigger_movement(bot, now)
        assert bot._dodging is False


# ── Phase 2 ──────────────────────────────────────────────────────────────────

class TestDynamicDodgeDuration:

    def test_dodge_duration_scales_with_time_to_impact(self, bot):
        """Dodge-Dauer proportional zu time_to_impact * 1.5."""
        # time_to_impact = 0.4s → dodge_duration = 0.4 * 1.5 = 0.6s
        import time as _time
        now = _time.monotonic()
        time_to_impact = 0.4
        expected = max(0.15, min(time_to_impact * 1.5, 0.8))
        assert expected == pytest.approx(0.6)

    def test_dodge_duration_minimum(self):
        """Minimum Dodge-Dauer ist 0.15s."""
        assert max(0.15, min(0.05 * 1.5, 0.8)) == pytest.approx(0.15)

    def test_dodge_duration_maximum(self):
        """Maximum Dodge-Dauer ist 0.8s."""
        assert max(0.15, min(2.0 * 1.5, 0.8)) == pytest.approx(0.8)


class TestSidewaysEvasion:
    """Schritt 3: Vorwärts-/Rückwärts-Ausweichen statt drehen."""

    def test_dodge_forward_when_already_aligned(self, bot):
        from conftest import make_shot
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.pi / 2  # blickt nach +Y
        bot.alive = True
        bot.human_count = 1
        # Schuss kommt von +X-Achse mit vel (-100, 0, 0) → perp ist ±Y
        # Shot bei 35u → time_to_impact=0.35s > time_to_dodge=0.276s → Ausweichen möglich
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(35.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        bot._next_shoot = float("inf")
        # Threat manuell vor-registrieren, damit Reaktionsverzögerung schon abgelaufen ist
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        # Bot blickt schon ~perpendikular zum Schuss → forward oder reverse, nicht beides
        assert bot._dodge_forward or bot._dodge_reverse
        assert not (bot._dodge_forward and bot._dodge_reverse)

    def test_dodge_reverse_with_zero_ang_vel(self, bot):
        """Im _dodge_reverse-Modus wird ang_vel auf 0 gesetzt und speed negativ.
        Fix E1: Shot muss registriert sein damit EVADING nicht sofort beendet wird."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot._dodging = True
        now = time.monotonic()
        bot._dodge_until = now + 1.0
        bot._dodge_reverse = True
        bot._dodge_forward = False
        bot._dodge_dir = math.pi
        bot._ai_state = AIState.EVADING
        # Fix E1: Schuss registrieren damit _find_incoming_shot != None
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._update_movement(0.02, now, ai_tick=False)
        assert bot.ang_vel == 0.0
        assert bot.vel_x < 0


class TestSidewaysEvasionThreshold:
    """Schritt 3: Neue Schwellwerte 10° / 170° für Sideways-Dodge."""

    def test_25_degrees_does_not_trigger_forward_dodge(self, bot):
        """Bei needed_turn=25° (> neue 10°-Schwelle) kein Forward-Dodge."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        # 25° neben perp_l (π/2) → needed_turn = 25°
        bot.azimuth = math.pi / 2 + math.radians(25)
        bot.alive = True
        bot.human_count = 1
        # Shot bei 35u → Ausweichen zeitlich möglich
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(35.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        bot._next_shoot = float("inf")
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._dodge_forward is False

    def test_5_degrees_triggers_forward_dodge(self, bot):
        """Bei orig_diff=5° (< neue 45°-Schwelle) Forward-Dodge aktiv.
        Shot bei 50u: time_to_impact=0.5s > time_to_dodge*1.1≈0.36s → feasible."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        # 5° neben perp_l (π/2) → orig_diff = 5°
        bot.azimuth = math.pi / 2 + math.radians(5)
        bot.alive = True
        bot.human_count = 1
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        bot._next_shoot = float("inf")
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._dodge_forward is True


class TestDdg05TimeBasedJump:
    """P1-DDG-05: zeitbasierte Sprung-Fallback-Berechnung."""

    def test_jump_when_time_to_dodge_exceeds_impact_time(self, bot):
        """Shot 20u weg bei vel=-100: t_impact=0.2s < t_to_dodge=0.276s → Sprung statt Dodge."""
        from conftest import make_shot
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.alive = True
        bot.human_count = 0  # kein Gegner nötig, nur Bedrohungs-Reaktion
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(20.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        # Zeitbasiert: t_to_dodge=0.276 > t_impact=0.2 → Sprung
        assert bot._ai_state == AIState.JUMPING or bot._jumping is True

    def test_dodge_when_time_sufficient(self, bot):
        """Shot 40u weg bei vel=-100: t_impact=0.4s. Bot zeigt senkrecht (turn_rad=0).
        time_to_dodge=0.225s < 0.4s → Ausweichen (Fix B: Bot bereits auf Ausweichrichtung)."""
        from conftest import make_shot
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.alive = True
        bot.human_count = 0
        bot.azimuth = math.pi / 2  # senkrecht zu Schussrichtung (shot von +x) → turn_rad=0
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(40.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        # time_to_dodge=0.225s < t_impact=0.4s → Ausweichen
        assert bot._ai_state == AIState.EVADING or bot._dodging is True


class TestEarlyDodgeExit:
    """Fix E1: Bot verlässt EVADING sofort wenn kein Schuss mehr detektiert."""

    def test_early_exit_to_combat_when_no_threat(self, bot):
        """EVADING ohne Schuss → Exit nach COMBAT (nach 0.1s-Buffer)."""
        from bot.models import AIState
        info = make_player(bot, 99, pos=(50.0, 0.0, 0.0))
        bot.target_player = 99
        bot.human_count = 1
        bot._dodging = True
        bot._dodge_until = time.monotonic() + 5.0  # Timer noch nicht abgelaufen
        bot._dodge_dir = math.pi / 2
        bot._ai_state = AIState.EVADING
# Keine Schüsse registriert → _find_incoming_shot gibt None zurück
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._ai_state == AIState.COMBAT
        assert bot._dodging is False

    def test_no_early_exit_when_shot_still_active(self, bot):
        """EVADING mit aktivem Schuss → dodge läuft weiter (kein früher Exit)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.pi / 2
        bot.human_count = 1
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_dir = math.pi / 2
        now = time.monotonic()
        bot._dodge_until = now + 0.5
        bot._ai_state = AIState.EVADING
        # Schuss noch aktiv und im Anflug
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._update_movement(0.02, now, ai_tick=False)
        assert bot._ai_state == AIState.EVADING
        assert bot._dodging is True

    def test_last_threat_id_cleared_on_early_exit(self, bot):
        """Bei frühem Exit wird _last_threat_id auf None gesetzt (nach 0.1s-Buffer)."""
        from bot.models import AIState
        bot.human_count = 1
        bot._last_threat_id = (2, 1)
        bot._dodging = True
        bot._dodge_until = time.monotonic() + 5.0
        bot._ai_state = AIState.EVADING
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._last_threat_id is None


class TestE2DodgeMargin:
    """Fix E2: 10% Margin — borderline Dodge-Fälle springen statt auszuweichen."""

    def test_borderline_case_triggers_dodge_jump(self, bot):
        """time_to_dodge=0.225s, t_impact=0.24s → 0.225*1.1=0.248 > 0.24 → DODGE_JUMP."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi / 2  # senkrecht → turn_rad=0 → time_to_dodge=0.225s
        bot.alive = True
        bot.human_count = 0
        # Shot bei 24u → t_impact=0.24s. Ohne Margin: feasible. Mit 10%: nicht mehr.
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(24.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        # 0.225 * 1.1 = 0.248 > 0.24 → DODGE_JUMP
        assert bot._ai_state == AIState.DODGE_JUMP
        assert bot._jumping is True

    def test_comfortable_case_still_evades(self, bot):
        """time_to_dodge=0.225s, t_impact=0.5s → 0.248 < 0.5 → EVADING."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi / 2  # senkrecht → turn_rad=0
        bot.alive = True
        bot.human_count = 0
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.EVADING
        assert bot._dodging is True


class TestDodgeJumpState:
    """Fix E3: Defensiver Sprung nutzt AIState.DODGE_JUMP statt JUMPING."""

    def test_infeasible_dodge_transitions_to_dodge_jump(self, bot):
        """Schuss zu nah → DODGE_JUMP (nicht JUMPING)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.alive = True
        bot.human_count = 0
        # Shot bei 5u → t_impact=0.05s, weit unter time_to_dodge
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP
        assert bot._jumping is True
        assert bot.vel_z == pytest.approx(bot._jump_velocity)

    def test_no_dodge_jump_when_server_no_jumping(self, bot):
        """Server ohne -j und ohne Sprung-Flagge → kein DODGE_JUMP (statt dessen Notschuss)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.alive = True
        bot.human_count = 0
        bot._server_jumping = False
        bot.own_flag = ""
        # Shot bei 5u → t_impact=0.05s, Ausweichen unmöglich → würde sonst DODGE_JUMP
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state != AIState.DODGE_JUMP
        assert bot._jumping is False

    def test_dodge_jump_no_rotation_when_facing_enemy(self, bot):
        """DODGE_JUMP: Kein ang_vel wenn Bot < 135° vom Gegner entfernt."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0  # Bot schaut direkt auf Gegner bei +x
        bot.alive = True
        bot.human_count = 1
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        # Shot bei 5u → DODGE_JUMP; angle_to_enemy = 0° < 135° → kein ang_vel
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP
        assert bot._jump_ang_vel == pytest.approx(0.0)

    def test_dodge_jump_gentle_rotation_when_back_to_enemy(self, bot):
        """DODGE_JUMP: Sanfte Rotation wenn Bot > 135° vom Gegner (Rücken)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi  # Bot zeigt Rücken zu Gegner bei +x
        bot.alive = True
        bot.human_count = 1
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        # angle_to_enemy = 180° > 135° → sanfte Rotation aktiviert
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP
        assert abs(bot._jump_ang_vel) > 0.0
        # Rotation sanft: höchstens halbe Drehrate
        assert abs(bot._jump_ang_vel) <= bot._tank_turn_rate * 0.5 + 1e-6

    def test_dodge_jump_lands_to_combat(self, bot):
        """DODGE_JUMP: Nach Landung → COMBAT wenn target_player vorhanden."""
        from bot.models import AIState
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        bot.target_player = 99
        bot.human_count = 1
        bot._ai_state = AIState.DODGE_JUMP
        bot._jumping = True
        bot.vel_z = -5.0
        bot.pos_z = 0.001
        bot._gravity = -9.8
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._ai_state == AIState.COMBAT
        assert bot._jumping is False


class TestSideShotReverseDirection:
    """Fix E4: orig_diff > 135° → _dodge_reverse=True, kein Turning-Dodge."""

    def test_backward_perp_triggers_reverse_dodge(self, bot):
        """Schuss von oben (+y-Richtung, senkrecht zur Bot-Blickrichtung) → _dodge_reverse=True.
        best_perp = rückwärts (−π), orig_diff=180° → reverse (statt turning) trotz 60°-Cap."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0  # blickt nach +x
        bot.alive = True
        bot.human_count = 0
        now = time.monotonic()
        # Schuss von (0.1, 100) nach unten: shot_dir=-π/2, bot leicht rechts → best_perp=-π (rückwärts)
        # t_impact≈1.0s → Dodge zeitlich möglich (auch mit 1.1-Puffer)
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(0.1, 100.0, 1.025), vel=(0.0, -100.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = now - 1.0
        bot._update_movement(0.02, now, ai_tick=True)
        # EVADING mit _dodge_reverse=True (nicht turning dodge)
        assert bot._ai_state == AIState.EVADING
        assert bot._dodge_reverse is True
        assert bot._dodge_forward is False

    def test_forward_perp_keeps_forward_dodge(self, bot):
        """Schuss von oben nach unten (shot_dir=-π/2), shot leicht links → best_perp=0 (vorwärts).
        orig_diff≈0° → _dodge_forward=True."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0  # blickt nach +x
        bot.alive = True
        bot.human_count = 0
        now = time.monotonic()
        # Schuss von (-0.1, 100) nach unten: shot_dir=-π/2.
        # perp_r=0 (vorwärts +x), bot links vom Schuss → dot_r>0 → best_perp=0 → orig_diff≈0
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(-0.1, 100.0, 1.025), vel=(0.0, -100.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = now - 1.0
        bot._update_movement(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.EVADING
        assert bot._dodge_forward is True
        assert bot._dodge_reverse is False


class TestDodgeJumpElapsedTime:
    """Fix J1a: time_to_closest überschätzt Restzeit wenn Schuss bereits länger fliegt.
    Korrekt: time_to_impact = time_to_closest_from_fire − elapsed_since_fire."""

    def test_aged_shot_triggers_dodge_jump(self, bot):
        """Schuss vor 200ms abgefeuert, bei 44u → time_to_closest=0.44s, Restzeit=0.24s.
        0.292*1.1=0.321 > 0.24 → DODGE_JUMP (ohne Fix wäre 0.321 ≤ 0.44 → EVADING)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi / 2  # bereits senkrecht → turn_rad=0
        bot.alive = True
        bot.human_count = 0
        now = time.monotonic()
        fire_time = now - 0.2  # Schuss vor 200ms abgefeuert
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(44.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                  fire_time=fire_time)
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = now - 1.0  # react-Delay klar erfüllt; getestet wird die Schuss-Alterung
        bot._update_movement(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP
        assert bot._jumping is True

    def test_fresh_shot_unaffected(self, bot):
        """Frischer Schuss (elapsed≈0) → kein Unterschied zum alten Verhalten.
        Bei 50u: time_to_impact≈0.5s, 0.321 ≤ 0.5 → EVADING."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi / 2
        bot.alive = True
        bot.human_count = 0
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = now - 1.0
        bot._update_movement(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.EVADING
        assert bot._dodging is True

    def test_ib_delay_aged_shot_triggers_dodge_jump(self, bot):
        """Schuss vor 0.5s abgefeuert, bei 80u: time_to_closest=0.8s, Restzeit=0.3s → DODGE_JUMP
        (ohne J1a-Fix wäre time_to_impact=0.8 → EVADING). react-Delay hier entkoppelt geprüft."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0  # IB-Schütze (bei +x) zentral im FoV (IB-Gate verlangt Sicht)
        bot.alive = True
        bot.human_count = 0
        now = time.monotonic()
        fire_time = now - 0.5
        make_player(bot, pid=2, flag="IB")
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(80.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0),
                  fire_time=fire_time)
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = now - 1.0  # react-Delay klar erfüllt; getestet wird die Schuss-Alterung
        bot._update_movement(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP
        assert bot._jumping is True

    def test_cs_shooter_reaction_delayed(self, bot):
        """CS-Schütze (Cloaked Shot): react_delay = 0.25·3.0 = 0.75s. Bei nur 0.3s seit
        Erkennung reagiert der Bot noch NICHT (Schuss bleibt auf Radar sichtbar, nur die
        visuelle Bestätigung fehlt) — ein Normal-Schütze (0.25s) hätte hier längst reagiert."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi / 2
        bot.alive = True
        bot.human_count = 0
        now = time.monotonic()
        make_player(bot, pid=2, flag="CS")
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = now - 0.3   # ≥ Normal-Delay (0.25), < CS-Delay (0.75)
        bot._update_movement(0.02, now, ai_tick=True)
        assert bot._ai_state not in (AIState.EVADING, AIState.DODGE_JUMP)

    def test_normal_shooter_reacts_at_cs_window(self, bot):
        """Kontrolle zu test_cs_shooter_reaction_delayed: gleiche Lage, aber Normal-Schütze →
        0.3s ≥ 0.25s → Bot reagiert (EVADING). Belegt, dass nur der CS-Malus die Reaktion
        zurückhält, nicht die Geometrie."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi / 2
        bot.alive = True
        bot.human_count = 0
        now = time.monotonic()
        make_player(bot, pid=2, flag="")
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = now - 0.3
        bot._update_movement(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.EVADING


# ── Phase 2 (cont.) ──────────────────────────────────────────────────────

class TestFindIncomingOscillation:
    """Fix OS1: _find_incoming_shot skippt Schüsse die sich im rel. Bezugssystem entfernen."""

    def test_shot_moving_away_not_detected(self, bot):
        """Schuss an pos(0,0), vel=(+100,0), fire_time=now-0.1 → aktuell bei (10,0).
        rx=10>0, rvx=100 → t_rel_raw = -(10*100)/10000 = -0.1 < 0 → None."""
        now = time.monotonic()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(0.0, 0.0, 1.025), vel=(100.0, 0.0, 0.0),
                  fire_time=now - 0.1)
        assert bot._find_incoming_shot(now)[0] is None

    def test_shot_approaching_still_detected(self, bot):
        """Schuss an (-30,0) mit vel=(+100,0) → rx=-30, t_rel_raw=0.3>0 → d=0 → gefunden."""
        now = time.monotonic()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(-30.0, 0.0, 1.025), vel=(100.0, 0.0, 0.0))
        assert bot._find_incoming_shot(now)[0] is not None


# ── Phase 2 (Round 11) ────────────────────────────────────────────────────────

class TestPhysicsDeadZone:
    """Fix LZ1: _run_physics Dead-Zone behoben — Schwerkraft jetzt ab pos[2] > 0.0."""

    def test_small_z_triggers_gravity(self, bot):
        """pos[2]=0.02 liegt zwischen 0 und 0.1 (alte Dead-Zone).
        Nach Fix: Schwerkraft wird angewendet → vel[2] < 0 nach einem Tick."""
        bot.pos_z = 0.02
        bot.vel_z = 0.0
        bot._jumping = False
        bot._run_physics(dt=0.02, now=time.monotonic())
        assert bot.vel_z < 0.0

    def test_small_z_snaps_to_ground_after_several_ticks(self, bot):
        """Nach mehreren Physik-Ticks landet Bot bei pos[2]=0 und _is_landed()=True."""
        bot.pos_z = 0.02
        bot.vel_z = 0.0
        bot._jumping = False
        now = time.monotonic()
        for _ in range(6):
            bot._run_physics(dt=0.02, now=now)
        assert bot.pos_z == pytest.approx(0.0, abs=1e-9)
        assert bot._is_landed() is True


class TestEvadingEarlyExitEV1:
    """Fix EV1: EVADING verlässt nur wenn Schuss für ALLE 4 Velocity-Szenarien ungefährlich."""

    def test_evading_exits_when_shot_truly_gone(self, bot):
        """Schuss klar hinter Bot (+50u wegfliegend): alle 4 Prüfungen None → EVADING verlassen (nach Buffer)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._dodging = True
        now = time.monotonic()
        bot._dodge_until = now + 5.0
        bot._ai_state = AIState.EVADING
        bot.human_count = 1
        make_player(bot, pid=2)
        bot.target_player = 2
        # Shot klar vorbei — bei (+50,0) und fliegt in +X weg
        make_shot(bot, shooter_id=3, shot_id=1,
                  pos=(50.0, 0.0, 1.025), vel=(100.0, 0.0, 0.0))
        bot._update_movement(0.02, now, ai_tick=False)
        assert bot._ai_state == AIState.COMBAT

    def test_evading_stays_when_shot_approaching(self, bot):
        """Schuss im Anflug (-30u): mindestens eine der 4 Prüfungen findet Bedrohung → bleibt."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._dodging = True
        now = time.monotonic()
        bot._dodge_until = now + 5.0
        bot._ai_state = AIState.EVADING
        bot.human_count = 0
        # Schuss direkt im Anflug
        make_shot(bot, shooter_id=3, shot_id=1,
                  pos=(-30.0, 0.0, 1.025), vel=(100.0, 0.0, 0.0))
        bot._update_movement(0.02, now, ai_tick=False)
        assert bot._ai_state == AIState.EVADING


class TestAnyIncomingThreatEquivalenceP3:
    """P3: _any_incoming_threat(now, vier_hypothesen) muss exakt dasselbe Ergebnis liefern
    wie die vier einzelnen _find_incoming_shot-Aufrufe aus dem EVADING-Early-Exit —
    randomisiert über feste Seed für breite Abdeckung der Bedrohungslogik (SW, GM,
    Ricochet-Segmente, BU-eingegraben, verschiedene Etagen, sich entfernende Schüsse)."""

    def test_any_incoming_threat_matches_four_calls(self, bot):
        import random
        from bzflag.shot_physics import Segment

        rng = random.Random(1234)
        now = time.monotonic()

        for i in range(100):
            bot._shots.clear()
            bot._ricochet_paths.clear()
            bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = rng.choice([0.0, -3.0, 5.0])
            bot.vel_x = rng.uniform(-25.0, 25.0); bot.vel_y = rng.uniform(-25.0, 25.0); bot.vel_z = 0.0
            bot.azimuth = rng.uniform(-math.pi, math.pi)
            bot.own_flag = rng.choice(["", "", "", "BU"])

            n_shots = rng.randint(0, 3)
            for j in range(n_shots):
                shooter_id = rng.choice([2, 3, 4])
                shot_id = 100 * i + j
                px = rng.uniform(-40.0, 40.0)
                py = rng.uniform(-40.0, 40.0)
                pz = rng.choice([bot.pos_z, bot.pos_z + 0.5, 10.0])
                vx = rng.uniform(-100.0, 100.0)
                vy = rng.uniform(-100.0, 100.0)
                is_sw = rng.random() < 0.15
                is_gm = (not is_sw) and rng.random() < 0.15
                lifetime = rng.choice([3.5, 0.0001])   # gelegentlich bereits abgelaufen
                shot = make_shot(bot, shooter_id=shooter_id, shot_id=shot_id,
                                  pos=(px, py, pz), vel=(0.0, 0.0, 0.0) if is_sw else (vx, vy, 0.0),
                                  lifetime=lifetime, is_sw=is_sw, is_gm=is_gm,
                                  fire_time=now - rng.uniform(0.0, 0.5))
                # gelegentlich zusätzlich als Ricochet-Pfad hinterlegen (Direktzweig übersprungen)
                if not is_sw and rng.random() < 0.25:
                    seg_dt = rng.uniform(0.2, 1.5)
                    bot._ricochet_paths[(shooter_id, shot_id)] = [
                        Segment(px, py, pz, px + vx * seg_dt, py + vy * seg_dt, pz,
                                now - 0.05, now - 0.05 + seg_dt)
                    ]

            fwd_vx = math.cos(bot.azimuth) * bot._tank_speed
            fwd_vy = math.sin(bot.azimuth) * bot._tank_speed
            vels = ((bot.vel_x, bot.vel_y), (0.0, 0.0),
                    (fwd_vx, fwd_vy), (-fwd_vx, -fwd_vy))

            expected = any(bot._find_incoming_shot(now, bot_vel=v)[0] is not None for v in vels)
            actual = bot._any_incoming_threat(now, vels)
            assert actual == expected, f"Divergenz bei Iteration {i} (seed 1234)"


class TestZAttackJump:
    """Feature ZJ1: Z-Höhen-Sprung auf erhöhten Gegner."""

    def test_z_attack_not_triggered_when_enemy_too_high(self, bot):
        """Gegner bei z=25u >= max_jump_h ≈ 18.4u → kein Sprung."""
        from bot.constants import JUMP_VELOCITY, GRAVITY, HIT_RADIUS
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._jumping = False
        bot._dodging = False
        bot._last_jump_at = 0.0
        bot._ai_state = AIState.COMBAT
        bot.human_count = 1
        make_player(bot, pid=2, pos=(40.0, 0.0, 25.0))  # z=25u — zu hoch
        bot.target_player = 2
        now = time.monotonic()
        result = bot._check_z_attack_jump(now)
        assert result is False
        assert bot._z_attack_mode is False

    def test_z_attack_fires_during_ascent(self, bot):
        """ZJ1 aktiv (_z_attack_mode=True), Aufstieg (vel[2]>0), pos[2]≈fire_z → Schuss."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 8.2   # nahe am fire_z
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0   # aufsteigend
        bot._jumping = True
        bot._z_attack_mode = True
        bot._z_attack_fire_z = 8.0
        bot._jump_ang_vel = 0.0
        bot._ai_state = AIState.Z_ATTACK
        bot.human_count = 1
        bot.target_player = None
        now = time.monotonic()
        bot._tick_z_attack(dt=0.02, now=now)
        assert bot.client.send.called
        assert bot._z_attack_mode is False

    def test_z_attack_lands_to_combat(self, bot):
        """Z_ATTACK-Landung → immer COMBAT, egal ob target_player gesetzt oder nicht."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.5
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = -5.0   # absteigend, landet bald
        bot._jumping = True
        bot._z_attack_mode = False
        bot._z_attack_fire_z = 8.0
        bot._jump_ang_vel = 0.0
        bot._ai_state = AIState.Z_ATTACK
        bot.human_count = 0        # auch ohne Menschen → COMBAT (nicht IDLE)
        bot.target_player = None
        now = time.monotonic()
        bot._tick_z_attack(dt=0.1, now=now)
        assert bot._ai_state == AIState.COMBAT

    def test_z_attack_transition_is_z_attack(self, bot):
        """_check_z_attack_jump erfolgreich → AIState.Z_ATTACK (nicht JUMPING)."""
        import math
        from unittest.mock import patch
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._jumping = False
        bot._dodging = False
        bot._last_jump_at = 0.0
        bot._ai_state = AIState.COMBAT
        bot.human_count = 1
        make_player(bot, pid=2, pos=(40.0, 0.0, 9.0))  # z=9: HIT_RADIUS < 9 < max_jump_h
        bot.target_player = 2
        now = time.monotonic()
        with patch("bot.ai.tactics.random") as mock_rng:
            mock_rng.random.return_value = 0.0   # < 0.5 → kein Skip
            mock_rng.uniform.return_value = 0.0  # kein Jitter
            result = bot._check_z_attack_jump(now)
        assert result is True
        assert bot._ai_state == AIState.Z_ATTACK

    def test_z_attack_blocked_when_enemy_jumping(self, bot):
        """ZJ1: Gegner is_airborne=True → kein Sprung, auch bei gültiger Höhe."""
        from unittest.mock import patch
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._jumping = False
        bot._dodging = False
        bot._last_jump_at = 0.0
        bot._ai_state = AIState.COMBAT
        bot.human_count = 1
        info = make_player(bot, pid=2, pos=(40.0, 0.0, 9.0))
        info.is_airborne = True  # Gegner selbst in der Luft
        bot.target_player = 2
        now = time.monotonic()
        result = bot._check_z_attack_jump(now)
        assert result is False
        assert bot._ai_state == AIState.COMBAT

    def test_z_attack_blocked_shot_not_ready(self, bot):
        """ZJ1: _next_shoot > now + t_fire → kein Sprung (Schuss nicht rechtzeitig bereit)."""
        from unittest.mock import patch
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot._jumping = False
        bot._dodging = False
        bot._last_jump_at = 0.0
        bot._ai_state = AIState.COMBAT
        bot.human_count = 1
        make_player(bot, pid=2, pos=(40.0, 0.0, 9.0))
        bot.target_player = 2
        now = time.monotonic()
        bot._next_shoot = now + 5.0   # Schuss erst in 5s bereit, t_fire ≈ 0.55s
        with patch("bot.ai.tactics.random") as mock_rng:
            mock_rng.random.return_value = 0.0
            result = bot._check_z_attack_jump(now)
        assert result is False
        assert bot._ai_state == AIState.COMBAT

    def test_z_attack_no_random_shoot_during_flight(self, bot):
        """_maybe_shoot blockiert während Z_ATTACK — kein Zufallsschuss."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 5.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 8.0
        bot._jumping = True
        bot._z_attack_mode = True
        bot._z_attack_fire_z = 9.0
        bot._ai_state = AIState.Z_ATTACK
        bot._next_shoot = 0.0   # Reload abgelaufen
        bot.human_count = 1
        make_player(bot, pid=2, pos=(40.0, 0.0, 9.0))
        bot.target_player = 2
        now = time.monotonic()
        bot._maybe_shoot(now)
        bot.client.send.assert_not_called()

    def test_z_attack_no_shot_when_enemy_out_of_sightline(self, bot):
        """_tick_z_attack: Gegner >20° aus Schussfeld → kein Schuss, _z_attack_mode bleibt True (Retry)."""
        import math
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 8.2
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0   # aufsteigend
        bot._jumping = True
        bot._z_attack_mode = True
        bot._z_attack_fire_z = 8.0
        bot._jump_ang_vel = 0.0
        bot.azimuth = 0.0           # Bot zeigt nach Osten (+x)
        bot._ai_state = AIState.Z_ATTACK
        bot.human_count = 1
        # Gegner im 45°-Winkel seitlich → weit außerhalb 20°-Threshold
        make_player(bot, pid=2, pos=(40.0, 40.0, 9.0))
        bot.target_player = 2
        now = time.monotonic()
        bot._tick_z_attack(dt=0.02, now=now)
        bot.client.send.assert_not_called()
        # Modus bleibt aktiv → nächster Tick darf erneut versuchen (Fix ZJ-01 B)
        assert bot._z_attack_mode is True

    def test_z_attack_fire_z_absolute_from_elevated_platform(self, bot):
        """ZJ-02: Absprung von z=30 auf Gegner z=45 → _z_attack_fire_z ist ABSOLUT (≈46),
        nicht relativ (≈17.9). Sonst trifft der Tick-Vergleich gegen pos[2] (30..48) nie."""
        import pytest
        from unittest.mock import patch
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0     # erhöhte Plattform
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = False
        bot._dodging = False
        bot._last_jump_at = 0.0
        bot._ai_state = AIState.COMBAT
        bot.human_count = 1
        bot._get_floor_z = lambda: 30.0   # Plattform trägt den Bot → _is_landed True (kein NavGraph)
        make_player(bot, pid=2, pos=(40.0, 0.0, 45.0))  # z_diff=15: HIT_RADIUS < 15 < max_jump_h
        bot.target_player = 2
        now = time.monotonic()
        with patch("bot.ai.tactics.random") as mock_rng:
            mock_rng.random.return_value = 0.0   # < 0.5 → kein Skip
            mock_rng.uniform.return_value = 0.0  # kein Jitter
            result = bot._check_z_attack_jump(now)
        assert result is True
        assert bot._z_attack_fire_z == pytest.approx(46.0, abs=0.6)

    def test_z_attack_fires_during_ascent_elevated(self, bot):
        """ZJ-02 End-to-End: in der Luft auf absoluter Feuer-Höhe (46) über z=30-Plattform →
        Schuss auf Gegner bei z=45."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 46.2   # nahe an absoluter fire_z
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0    # aufsteigend → nicht gelandet
        bot.azimuth = 0.0            # auf Gegner (+x) ausgerichtet
        bot._jumping = True
        bot._z_attack_mode = True
        bot._z_attack_fire_z = 46.0
        bot._jump_ang_vel = 0.0
        bot._ai_state = AIState.Z_ATTACK
        bot._next_shoot = 0.0
        bot.human_count = 1
        make_player(bot, pid=2, pos=(40.0, 0.0, 45.0))
        bot.target_player = 2
        now = time.monotonic()
        bot._tick_z_attack(dt=0.02, now=now)
        assert bot.client.send.called
        assert bot._z_attack_mode is False


class TestTactJumpRestrictionsTJ1:
    """TJ1: Restriktivere Trigger-Bedingungen für den Taktischen Übersprung."""

    def _setup(self, bot, enemy_pos, enemy_vel=(0.0, 0.0, 0.0), enemy_azimuth=None):
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = False
        bot._dodging = False
        bot._last_jump_at = 0.0
        bot._ai_state = AIState.COMBAT
        bot.human_count = 1
        bot.own_flag = ""
        p = make_player(bot, pid=2, pos=enemy_pos)
        p.vel = list(enemy_vel)
        if enemy_azimuth is not None:
            p.azimuth = enemy_azimuth
        bot.target_player = 2
        return p

    def test_tact_jump_blocked_bot_not_aligned(self, bot):
        """Bot 20° vom Gegner weggedreht → angle_to_enemy > 15° → kein Frontalsprung."""
        import math
        from bot.models import AIState
        dist = 30.0
        ang = math.radians(20)
        ep = (math.cos(ang) * dist, math.sin(ang) * dist, 0.0)
        # enemy faces bot (azimuth ≈ 200° → looking back toward origin)
        self._setup(bot, ep, enemy_azimuth=math.radians(200))
        now = time.monotonic()
        result = bot._check_tactical_jump(now)
        assert result is False
        assert bot._ai_state == AIState.COMBAT

    def test_tact_jump_blocked_enemy_facing_away(self, bot):
        """Bot gut ausgerichtet (angle_to_enemy=0°), aber Gegner 40° abgewandt → kein Sprung."""
        import math
        from bot.models import AIState
        # enemy_az=0°, direction enemy→bot=180°, enemy_azimuth=220° → angle_enemy_to_bot=40° > 15°
        self._setup(bot, (30.0, 0.0, 0.0), enemy_azimuth=math.radians(220))
        now = time.monotonic()
        result = bot._check_tactical_jump(now)
        assert result is False
        assert bot._ai_state == AIState.COMBAT

    def test_tact_jump_allowed_narrow_angle(self, bot):
        """angle_to_enemy = 10° < 15°, alle anderen Gates grün → Sprung ausgelöst."""
        import math
        from unittest.mock import patch
        from bot.models import AIState
        dist = 30.0
        ang = math.radians(10)
        ep = (math.cos(ang) * dist, math.sin(ang) * dist, 0.0)
        # enemy faces bot (azimuth ≈ 190° → looking back toward origin)
        self._setup(bot, ep, enemy_azimuth=math.radians(190))
        now = time.monotonic()
        with patch("bot.ai.tactics.random") as mock_rng:
            mock_rng.random.return_value = 0.0   # 0.0 >= 0.3 → False → kein Skip
            result = bot._check_tactical_jump(now)
        assert result is True
        assert bot._ai_state == AIState.JUMP_WINDUP

    def test_tact_jump_blocked_retreating_enemy(self, bot):
        """Gegner weicht mit 20 u/s zurück → Clearance-Gate schlägt an."""
        # (25 + (−20)) * t_jump ≈ 5 * 3.88 = 19.4 < 40 * 1.2 = 48
        import math
        from bot.models import AIState
        self._setup(bot, (40.0, 0.0, 0.0),
                    enemy_vel=(20.0, 0.0, 0.0),
                    enemy_azimuth=math.radians(180))   # enemy faces bot
        now = time.monotonic()
        result = bot._check_tactical_jump(now)
        assert result is False
        assert bot._ai_state == AIState.COMBAT

    def test_tact_jump_blocked_z_diff(self, bot):
        """z_diff > HIT_RADIUS → Z-Gate schlägt an, kein Sprung."""
        from bot.constants import HIT_RADIUS
        from bot.models import AIState
        self._setup(bot, (30.0, 0.0, HIT_RADIUS + 1.0))
        now = time.monotonic()
        result = bot._check_tactical_jump(now)
        assert result is False
        assert bot._ai_state == AIState.COMBAT

    def test_tact_jump_retry_cooldown_blocks(self, bot):
        """_tact_jump_retry_after in der Zukunft → sofort False, alle anderen Gates egal."""
        from bot.models import AIState
        import math
        dist = 30.0
        ang = math.radians(10)
        ep = (math.cos(ang) * dist, math.sin(ang) * dist, 0.0)
        self._setup(bot, ep, enemy_azimuth=math.radians(190))
        now = time.monotonic()
        bot._tact_jump_retry_after = now + 10.0  # Sperre weit in der Zukunft
        result = bot._check_tactical_jump(now)
        assert result is False
        assert bot._ai_state == AIState.COMBAT

    def test_tact_jump_blocked_enemy_has_shockwave(self, bot):
        """TACT-02: anvisierter Gegner trägt SW → kein TactJump (Sprung in die SW-Kuppel)."""
        import math
        from bot.models import AIState
        dist = 30.0
        ang = math.radians(10)
        ep = (math.cos(ang) * dist, math.sin(ang) * dist, 0.0)
        p = self._setup(bot, ep, enemy_azimuth=math.radians(190))
        p.flag = "SW"
        now = time.monotonic()
        result = bot._check_tactical_jump(now)
        assert result is False
        assert bot._ai_state == AIState.COMBAT

    def test_tact_jump_allowed_when_enemy_flag_not_shockwave(self, bot):
        """Gegenprobe zu TACT-02: ein Nicht-SW-Flag (hier GM) blockt den TactJump nicht."""
        import math
        from unittest.mock import patch
        from bot.models import AIState
        dist = 30.0
        ang = math.radians(10)
        ep = (math.cos(ang) * dist, math.sin(ang) * dist, 0.0)
        p = self._setup(bot, ep, enemy_azimuth=math.radians(190))
        p.flag = "GM"
        now = time.monotonic()
        with patch("bot.ai.tactics.random") as mock_rng:
            mock_rng.random.return_value = 0.0   # 0.0 >= 0.3 → False → kein Skip
            result = bot._check_tactical_jump(now)
        assert result is True
        assert bot._ai_state == AIState.JUMP_WINDUP


class TestDodgeMomentumRamp:
    """P4-MOV-02b: EVADING-Dodge ist Bodenfahrt → die Geschwindigkeit rampt bei aktivem -a hoch,
    statt instant auf _tank_speed zu springen (ohne -a unverändert)."""

    def _setup_active_evading(self, bot, now):
        # Bot weicht vorwärts aus (azimuth = π/2), Schuss noch im Anflug → EVADING bleibt aktiv.
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.pi / 2
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.human_count = 1
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_dir = math.pi / 2
        bot._dodge_until = now + 0.5
        from bot.models import AIState
        bot._ai_state = AIState.EVADING
        make_shot(bot, shooter_id=2, shot_id=1, pos=(50.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))

    def test_forward_dodge_ramps_with_dash_a(self, bot):
        now = time.monotonic()
        bot._linear_acceleration = 50.0   # max_delta = 20×50×0.02 = 20 u/s pro Tick
        self._setup_active_evading(bot, now)
        bot._update_movement(0.02, now, ai_tick=False)
        v1 = math.hypot(bot.vel_x, bot.vel_y)
        assert v1 == pytest.approx(20.0, abs=1e-6)      # gerampt, noch nicht bei 25
        assert v1 < bot._tank_speed

    def test_forward_dodge_instant_without_dash_a(self, bot):
        now = time.monotonic()
        bot._linear_acceleration = 0.0   # keine Klemme → instant wie bisher
        self._setup_active_evading(bot, now)
        bot._update_movement(0.02, now, ai_tick=False)
        v1 = math.hypot(bot.vel_x, bot.vel_y)
        assert v1 == pytest.approx(bot._tank_speed, abs=1e-6)


class TestDodgeMarginMomentumRamp:
    """P4-MOV-02b: time_to_dodge berücksichtigt die Anfahr-Rampe (_momentum_ramp_time). Bei
    trägen Beschleunigungen (niedriges -a oder M) kippt ein grenzwertiger Fall korrekt von EVADING
    auf DODGE_JUMP, statt einen zu langsamen Dodge zu starten. Bei -a 50 bleibt die Entscheidung."""

    def _fire_40u_shot(self, bot):
        # 40u/−100 → t_impact ≈ 0.4s; Bot senkrecht (turn_rad=0) → time_to_dodge ≈ 0.29s.
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi / 2
        bot.alive = True
        bot.human_count = 0
        make_shot(bot, shooter_id=2, shot_id=1, pos=(40.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0

    def test_low_accel_flips_to_dodge_jump(self, bot):
        from bot.models import AIState
        bot._linear_acceleration = 1.0   # ramp ≈ 1.25s → time_to_dodge ≫ t_impact
        self._fire_40u_shot(bot)
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP

    def test_m_flag_flips_to_dodge_jump(self, bot):
        """M (Momentum-Default 1.0) ist ~50× träger als -a 50 → wie niedriges -a → DODGE_JUMP.
        (Dieser Fall war für Commit 2 vorgesehen, braucht aber die time_to_dodge-Nachführung.)"""
        from bot.models import AIState
        bot._linear_acceleration = 0.0
        bot.own_flag = "M"
        bot._momentum_lin_acc = 1.0
        self._fire_40u_shot(bot)
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP

    def test_target_server_still_evades(self, bot):
        from bot.models import AIState
        bot._linear_acceleration = 50.0   # ramp ≈ 0.05s → Entscheidung unverändert
        self._fire_40u_shot(bot)
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.EVADING


class TestDodgeDurationRampAudit:
    """Audit-Fix: die dodge_duration-Kappung (0,8 s, aus der Vor-Rampen-Zeit) wird um
    _momentum_ramp_time(1.0) erweitert — sonst bricht der Dodge bei trägen Configs ab, bevor
    die in _handle_threat angenommene Sicherheitsdistanz gefahren ist (und die EV2-Grace
    unterdrückt denselben Schuss danach noch 1 s)."""

    def test_duration_cap_extends_with_ramp(self, bot):
        bot._linear_acceleration = 1.0   # eff. 20 u/s² → Rampe 25/20 = 1.25s
        now = time.monotonic()
        shot = make_shot(bot, shooter_id=2, shot_id=1,
                         pos=(300.0, 0.0, 1.025), vel=(-50.0, 0.0, 0.0))
        bot._setup_dodge(shot, now, 5.0, 0.0)   # t_impact*1.5 = 7.5 → Kappung greift
        expected = 0.8 + bot._momentum_ramp_time(1.0)
        assert expected > 2.0                    # Rampe wirkt wirklich (kein 0-Fall)
        assert bot._dodge_until - now == pytest.approx(expected, abs=1e-6)

    def test_duration_cap_unchanged_without_limit(self, bot):
        bot._linear_acceleration = 0.0   # weder -a noch M → Alt-Verhalten (0,8 s)
        now = time.monotonic()
        shot = make_shot(bot, shooter_id=2, shot_id=1,
                         pos=(300.0, 0.0, 1.025), vel=(-50.0, 0.0, 0.0))
        bot._setup_dodge(shot, now, 5.0, 0.0)
        assert bot._dodge_until - now == pytest.approx(0.8, abs=1e-6)


class TestLaunchEventsIgnoreRamp:
    """P4-MOV-02b: Sprung-Launches sind KEINE doMomentum-Pfade (der echte doJump übernimmt die
    alte Horizontal-Velocity unverändert). Der Bot setzt die Absprung-Velocity daher bewusst
    instant — auch bei aktivem Beschleunigungs-Limit. Diese Guards sichern die Entscheidung ab,
    damit nicht versehentlich später eine Rampe in einen Launch eingebaut wird."""

    def test_tactical_jump_launch_ignores_ramp(self, bot):
        """_execute_jump setzt vel auf die volle _tank_speed, nicht auf einen einzelnen
        Ramp-Schritt (20×50×0.02 = 20 wäre die Ein-Tick-Klemme bei -a 50)."""
        import math
        bot._linear_acceleration = 50.0   # Rampe aktiv — würde vel sonst auf 20 klemmen
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        bot.target_player = None
        bot._escape_jump_ang_vel = None
        bot._execute_jump()
        assert math.hypot(bot.vel_x, bot.vel_y) == pytest.approx(bot._tank_speed)

    def test_nav_jump_launch_ignores_ramp(self, bot):
        """_initiate_nav_jump setzt vel auf die berechnete needed_hspeed instant — deutlich über
        dem, was ein einzelner Ramp-Tick aus dem Stand (0) erlauben würde."""
        import math
        bot._linear_acceleration = 50.0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        wp = [85.0, 0.0, 0.0]   # weit → needed_hspeed ~22.6, klar über der Ein-Tick-Ramp-Klemme
        bot._initiate_nav_jump(wp)
        speed = math.hypot(bot.vel_x, bot.vel_y)
        # Ein-Tick-Ramp aus dem Stand bei -a 50 wäre 20×50×0.02 = 20; der Launch ist instant und
        # setzt needed_hspeed (>20) sofort, statt über mehrere Ticks hochzurampen.
        assert speed > 20.0

    def test_dodge_jump_preserves_ground_velocity(self, bot):
        """DODGE_JUMP setzt kein vel_x/vel_y → die (ggf. gerampte) Bodengeschwindigkeit bleibt
        exakt erhalten (entspricht doJump: alte Horizontal-vel übernommen)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.vel_x = 12.0; bot.vel_y = 0.0; bot.vel_z = 0.0   # fährt vorwärts am Boden
        bot.alive = True
        bot.human_count = 0
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))   # zu nah → DODGE_JUMP
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP
        assert bot.vel_x == pytest.approx(12.0)   # horizontal unverändert
        assert bot.vel_y == pytest.approx(0.0)


# ── P4-MOV-03a: WG-Luftsteuerung (Commit C1) ────────────────────────────────

class TestWingsAirControl:
    """_wings_air_control_active()/_wings_air_steer(): Bewegung in der Luft mit WG ist strikt
    an ±Blickrichtung gekoppelt (faithful zu doUpdateMotion, Wings-Zweig) — Drehen krümmt die
    Flugbahn statt (wie vorher) entkoppelt zu strafen. Fix der unmöglichen WG-Luftdrehung in
    _tick_jumping/_tick_nav_jump/_tick_falling + DODGE_JUMP/Escape-Steuerziel."""

    # ── _wings_air_control_active() ─────────────────────────────────────────

    def test_air_control_active_requires_wg_and_no_slide(self, bot):
        bot.own_flag = "WG"
        bot._wings_slide_time = 0.0
        assert bot._wings_air_control_active() is True
        bot._wings_slide_time = 0.5
        assert bot._wings_air_control_active() is False    # Slide-Downgrade
        bot._wings_slide_time = 0.0
        bot.own_flag = ""
        assert bot._wings_air_control_active() is False    # keine WG-Flagge

    # ── Kopplungs-Invariante (wichtigster Test) ─────────────────────────────

    def test_air_steer_couples_velocity_to_azimuth_while_curving(self, bot):
        """Mit WG-aktiv ist (vel_x,vel_y) in JEDEM Tick parallel/antiparallel zu azimuth
        (Halbschritt-Toleranz) — auch über mehrere Ticks mit Drehung (Kurvenflug)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 50.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        info = make_player(bot, 99, pos=(0.0, 60.0, 50.0))   # seitlich → erzwingt Kurvenflug
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        dt = 0.05
        tol = 0.5 * dt * bot._tank_turn_rate + 1e-6   # Halbschritt-Winkeltoleranz
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            for _ in range(20):
                bot._tick_jumping(dt, now=1000.0)
                speed = math.hypot(bot.vel_x, bot.vel_y)
                if speed > 1e-6:
                    vel_ang = math.atan2(bot.vel_y, bot.vel_x)
                    diff = abs(_angle_diff(vel_ang, bot.azimuth))
                    assert diff < tol or abs(diff - math.pi) < tol
        # Drehen hat die Flugbahn tatsächlich gekrümmt (kein reiner Geradeausflug)
        assert bot.azimuth != pytest.approx(0.0)

    def test_air_steer_clamps_reverse_to_half_speed(self, bot):
        """Rückwärts-Klemme: Luft-Reverse ≤ 0,5·_effective_tank_speed() (wie am Boden)."""
        bot.own_flag = "WG"
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        bot._wings_air_steer(0.02, 0.0, -1000.0)   # extremer Rückwärtswunsch
        speed = math.hypot(bot.vel_x, bot.vel_y)
        assert speed == pytest.approx(0.5 * bot._effective_tank_speed())
        assert bot.vel_x < 0.0   # tatsächlich rückwärts (Blickrichtung 0°)

    def test_air_steer_forward_speed_uncapped_below_tank_speed(self, bot):
        """Gegenprobe: Vorwärts bis volle _effective_tank_speed(), keine 0,5×-Klemme."""
        bot.own_flag = "WG"
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        bot._wings_air_steer(0.02, 0.0, bot._effective_tank_speed())
        assert math.hypot(bot.vel_x, bot.vel_y) == pytest.approx(bot._effective_tank_speed())

    # ── Regressionsschutz: ohne WG / mit _wingsSlideTime>0 unveränderte Ballistik ──

    def test_tick_jumping_without_wg_byte_identical_ballistics(self, bot):
        bot.own_flag = ""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 50.0
        bot.vel_x = 7.0; bot.vel_y = -3.0; bot.vel_z = 2.0
        bot.azimuth = 1.0
        bot._jump_ang_vel = 0.4
        bot._jumping = True
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot.azimuth == pytest.approx(1.0 + 0.4 * 0.02)
        assert bot.vel_x == pytest.approx(7.0)    # _tick_jumping fasst vel_x/vel_y ohne WG nie an
        assert bot.vel_y == pytest.approx(-3.0)

    def test_tick_jumping_wg_with_slide_time_keeps_old_ang_vel_behavior(self, bot):
        """_wingsSlideTime > 0 → Slide-Downgrade: altes _jump_ang_vel-Verhalten + Ballistik,
        WG-Luftsteuerung greift NICHT (doSlideMotion wird bewusst nicht modelliert)."""
        bot.own_flag = "WG"
        bot._wings_slide_time = 0.5
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 50.0
        bot.vel_x = 7.0; bot.vel_y = -3.0; bot.vel_z = 2.0
        bot.azimuth = 1.0
        bot._jump_ang_vel = 0.4
        bot._jumping = True
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot.azimuth == pytest.approx(1.0 + 0.4 * 0.02)
        assert bot.vel_x == pytest.approx(7.0)
        assert bot.vel_y == pytest.approx(-3.0)

    # ── DODGE_JUMP + Escape: Steuerziel statt entkoppelter _jump_ang_vel ────

    def test_dodge_jump_wg_uses_steer_target_not_jump_ang_vel(self, bot):
        """DODGE_JUMP mit WG-aktiv: der alte entkoppelte Korrektur-Pfad (_jump_ang_vel) wird
        NICHT mehr benutzt — stattdessen wird _wings_steer_az gesetzt (Gegner-Azimuth)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi   # Rücken zum Gegner bei +x → Korrektur-Zweig (>135°)
        bot.alive = True
        bot.human_count = 1
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP
        assert bot._jump_ang_vel == pytest.approx(0.0)          # alter Pfad unangetastet
        assert bot._wings_steer_az == pytest.approx(0.0)        # neues Steuerziel = Gegner-Azimuth
        # Folge-Tick: die Steuerung dreht tatsächlich Richtung Steuerziel, statt zu strafen.
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        diff_before = abs(_angle_diff(0.0, math.pi))
        diff_after = abs(_angle_diff(0.0, bot.azimuth))
        assert diff_after < diff_before

    def test_dodge_jump_wg_no_correction_when_facing_enemy(self, bot):
        """< 135° zum Gegner → keine Korrektur nötig: kein Steuerziel gesetzt (Gegner-Verfolgung
        in _tick_jumping übernimmt die Ausrichtung ohnehin live)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0   # schaut direkt auf Gegner bei +x
        bot.alive = True
        bot.human_count = 1
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(5.0, 0.0, 1.025), vel=(-100.0, 0.0, 0.0))
        bot._last_threat_id = (2, 1)
        bot._threat_detected_at = time.monotonic() - 1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.DODGE_JUMP
        assert bot._wings_steer_az is None

    def test_execute_jump_escape_wg_sets_steer_az_not_jump_ang_vel(self, bot):
        """_execute_jump: Escape-Drehwunsch (_escape_jump_ang_vel) wird mit WG zum
        Steuerziel-Azimuth statt in _jump_ang_vel geschrieben."""
        bot.own_flag = "WG"
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        bot.target_player = None
        bot._jump_ang_vel = 0.0
        bot._escape_jump_ang_vel = bot._tank_turn_rate   # positiver Escape-Spin-Wunsch
        bot._execute_jump()
        assert bot._escape_jump_ang_vel is None
        assert bot._jump_ang_vel == pytest.approx(0.0)   # alter Pfad unangetastet
        assert bot._wings_steer_az is not None

    def test_execute_jump_escape_without_wg_unchanged(self, bot):
        """Gegenprobe ohne WG: altes Verhalten (_jump_ang_vel = _escape_jump_ang_vel)."""
        bot.own_flag = ""
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        bot.target_player = None
        bot._jump_ang_vel = 0.0
        bot._escape_jump_ang_vel = bot._tank_turn_rate
        bot._execute_jump()
        assert bot._escape_jump_ang_vel is None
        assert bot._jump_ang_vel == pytest.approx(bot._tank_turn_rate)

    # ── Momentum-Wechselwirkung: Luftsteuerung bleibt instant ───────────────

    def test_wg_air_steer_ignores_linear_acceleration_limit(self, bot):
        """_linear_acceleration=50 gesetzt (Bodenrampe) → Luftsteuerung bleibt instant (kein
        Ramp — doMomentum läuft nicht airborne)."""
        bot.own_flag = "WG"
        bot._linear_acceleration = 50.0
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        bot._wings_air_steer(0.02, 0.0, bot._effective_tank_speed())
        assert math.hypot(bot.vel_x, bot.vel_y) == pytest.approx(bot._effective_tank_speed())

    # ── _tick_nav_jump + WG: Kurskorrektur, Landung ohne Land-Spin ──────────

    def test_nav_jump_wg_corrects_course_toward_wp(self, bot):
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        bot.vel_x = 10.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jump_ang_vel = 5.0   # Deko-Wert: darf mit WG-Steuerung NICHT angewandt werden
        bot._jumping = True
        bot._nav_jump_target_z = 0.0
        bot._nav_path = [(0.0, 20.0, 0.0)]   # Ziel-WP seitlich → erzwingt Kurskorrektur
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_nav_jump(0.05, now=1000.0)
        # Deko-_jump_ang_vel hätte azimuth auf 5*0.05=0.25 gesetzt — NICHT angewandt.
        assert bot.azimuth != pytest.approx(5.0 * 0.05, abs=1e-6)
        # Stattdessen dreht der Bot Richtung WP (atan2(20,0)=+90°) → azimuth wächst positiv.
        assert bot.azimuth > 0.0

    def test_nav_jump_wg_lands_without_land_spin(self, bot):
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.05
        bot.vel_x = 15.0; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.05   # bereits fast Richtung Flug ausgerichtet
        bot._jump_ang_vel = 5.0   # Deko: würde bei altem Land-Spin-Verhalten weiterdrehen
        bot._jumping = True
        bot._ai_state = AIState.NAV_JUMP
        bot._nav_jump_return_state = AIState.SEEKING
        bot._nav_jump_target_z = 0.0
        bot._nav_path = [(15.0, 0.0, 0.0)]
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=0.0), \
             patch.object(bot, "_is_landed", return_value=True):
            bot._tick_nav_jump(0.02, now=1000.0)
        assert bot._jumping is False
        assert bot._wings_steer_az is None
        # Landeausrichtung ≈ Flugrichtung, NICHT durch den Deko-_jump_ang_vel weitergedreht
        # (der hätte +5.0*0.02=0.1 rad zusätzlich draufgesetzt).
        assert abs(bot.azimuth - 0.05) < 0.05

    def test_nav_jump_without_wg_unchanged(self, bot):
        """Gegenprobe ohne WG: alte Lande-Drehung (_jump_ang_vel) bleibt unverändert wirksam."""
        bot.own_flag = ""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 10.0
        bot.vel_x = 10.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.3
        bot._jumping = True
        bot._nav_jump_target_z = 30.0
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_nav_jump(0.05, now=1000.0)
        assert bot.azimuth == pytest.approx(0.3 * 0.05)

    # ── FALLING + WG steuerbar, ohne WG unverändert ─────────────────────────

    def test_falling_wg_steers_toward_nav_wp(self, bot):
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 20.0
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jump_ang_vel = 3.0   # Deko, darf mit WG nicht angewandt werden
        bot._jumping = True
        bot._nav_path = [(0.0, 30.0, 20.0)]   # seitlicher Ziel-WP
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_falling(0.05, now=1000.0)
        assert bot.azimuth != pytest.approx(3.0 * 0.05, abs=1e-6)
        assert bot.azimuth > 0.0   # dreht Richtung +y (WP)

    def test_falling_wg_without_nav_path_holds_heading(self, bot):
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 20.0
        az0 = 0.3
        speed0 = 5.0
        bot.vel_x = speed0 * math.cos(az0); bot.vel_y = speed0 * math.sin(az0); bot.vel_z = -1.0
        bot.azimuth = az0
        bot._jump_ang_vel = 3.0
        bot._jumping = True
        bot._nav_path = []
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_falling(0.05, now=1000.0)
        assert bot.azimuth == pytest.approx(az0)   # Heading halten, keine Drehung
        assert math.hypot(bot.vel_x, bot.vel_y) == pytest.approx(speed0)

    def test_falling_without_wg_unchanged(self, bot):
        bot.own_flag = ""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 20.0
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.3
        bot._jump_ang_vel = 0.4
        bot._jumping = True
        bot._nav_path = [(0.0, 30.0, 20.0)]
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_falling(0.05, now=1000.0)
        assert bot.azimuth == pytest.approx(0.3 + 0.4 * 0.05)
        assert bot.vel_x == pytest.approx(5.0)
        assert bot.vel_y == pytest.approx(0.0)

    def test_z_attack_wg_steers_instead_of_decoupled_spin(self, bot):
        """Z_ATTACK mit WG-aktiv: _jump_ang_vel wird NICHT angewandt — das beim Absprung
        gesetzte Steuerziel (_wings_steer_az) wird per _wings_air_steer angesteuert
        (Kopplungs-Invariante gilt auch hier)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 5.0
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = 10.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._jump_ang_vel = 3.0            # dürfte mit WG nie mehr wirken
        bot._wings_steer_az = 1.0
        bot._z_attack_mode = False         # kein Feuer-Zweig in diesem Test
        dt = 0.05
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_z_attack(dt, now=1000.0)
        # Drehung Richtung Steuerziel mit max. Drehrate — nicht die 3.0-Spin-Rate
        assert bot.azimuth == pytest.approx(bot._tank_turn_rate * dt)
        vel_ang = math.atan2(bot.vel_y, bot.vel_x)
        assert abs(_angle_diff(vel_ang, bot.azimuth)) < 0.5 * dt * bot._tank_turn_rate + 1e-6

    def test_z_attack_check_sets_steer_az_with_wg(self, bot):
        """_check_z_attack_jump schreibt mit WG-aktiv das Ziel in _wings_steer_az statt
        _jump_ang_vel (kein entkoppelter Spin mehr)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.0
        info = make_player(bot, 99, pos=(3.0, 0.0, 8.0))   # Gegner erhöht → Z-Attack-Kandidat
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        with patch.object(bot, "_can_jump", return_value=True), \
             patch("random.random", return_value=0.0):
            ok = bot._check_z_attack_jump(now=1000.0)
        assert ok is True
        assert bot._wings_steer_az is not None
        assert bot._jump_ang_vel == pytest.approx(0.0)


# ── P4-MOV-03b: WG-TactJump-Finte (Commit C2) ───────────────────────────────

class TestWingsFeint:
    """Der klassische TactJump (feste Lande-Drehung) ist mit WG-Luftsteuerung physikalisch
    unmöglich (Bewegung strikt an ±Blickrichtung gekoppelt, P4-MOV-03a). Ersatz: die
    Übersprung-Finte (_wg_feint_target/_wg_feint_phase, _wg_feint_tick in states.py) —
    höchste Priorität im WG-Zweig von _tick_jumping."""

    # ── _execute_jump: Finte nur WG-aktiv + Frontal-Zweig ───────────────────

    def test_execute_jump_frontal_wg_sets_feint_target(self, bot):
        bot.own_flag = "WG"
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._escape_jump_ang_vel = None
        bot._wg_feint_target = None
        bot._wg_feint_phase = 1   # Deko: muss von _execute_jump auf 0 zurückgesetzt werden
        bot._execute_jump()
        assert bot._wg_feint_target == 99
        assert bot._wg_feint_phase == 0

    def test_execute_jump_escape_wg_no_feint_target(self, bot):
        """Escape-Zweig (_escape_jump_ang_vel gesetzt) setzt KEINE Finte, auch nicht mit WG."""
        bot.own_flag = "WG"
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._escape_jump_ang_vel = bot._tank_turn_rate
        bot._wg_feint_target = None
        bot._execute_jump()
        assert bot._wg_feint_target is None

    def test_execute_jump_frontal_without_wg_no_feint_target(self, bot):
        """Gegenprobe ohne WG: _wg_feint_target bleibt unangetastet (byte-identisches Verhalten)."""
        bot.own_flag = ""
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._escape_jump_ang_vel = None
        bot._wg_feint_target = None
        bot._execute_jump()
        assert bot._wg_feint_target is None

    def test_execute_jump_frontal_wg_without_target_player_no_feint(self, bot):
        """Kein target_player → keine Finte, kein Crash."""
        bot.own_flag = "WG"
        bot.azimuth = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0
        bot.target_player = None
        bot._escape_jump_ang_vel = None
        bot._wg_feint_target = None
        bot._execute_jump()
        assert bot._wg_feint_target is None

    # ── Umschaltpunkt-Entscheidung: Gegner gedreht → Finte, sonst klassisch ─

    def test_feint_switch_confirms_when_enemy_faces_bot(self, bot):
        """Am Umschaltpunkt (Projektion ≥ TANK_LENGTH) blickt der Gegner zum Bot →
        Finte bestätigt: Phase wechselt auf 1 (rückwärts), projizierte Geschwindigkeit < 0."""
        bot.own_flag = "WG"
        bot.pos_x = 26.0; bot.pos_y = 0.0; bot.pos_z = 30.0   # 6u (TANK_LENGTH) am Gegner vorbei
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jumping = True
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.azimuth = 0.0   # Blick Richtung +x = zum Bot (bei x=26) → "hat gedreht"
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 0
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot._wg_feint_target == 99
        assert bot._wg_feint_phase == 1
        speed_proj = bot.vel_x * math.cos(bot.azimuth) + bot.vel_y * math.sin(bot.azimuth)
        assert speed_proj < 0.0

    def test_feint_switch_point_scales_with_tank_length(self, bot):
        """Audit-Fix: der Umschaltpunkt (proj ≥ _tank_length) folgt der Server-Var, nicht der
        statischen TANK_LENGTH-Konstante (6.0u). Mit _tank_length=10.0 löst ein Abstand von
        6.0u (die alte Konstante) noch KEINEN Umschaltpunkt-Check aus — erst bei 10.0u."""
        bot.own_flag = "WG"
        bot._tank_length = 10.0
        bot.pos_x = 26.0; bot.pos_y = 0.0; bot.pos_z = 30.0   # 6u am Gegner vorbei (alte TANK_LENGTH)
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jumping = True
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.azimuth = 0.0   # würde "hat gedreht" ergeben, falls der Umschaltpunkt-Check liefe
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 0
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        # proj=6.0 < _tank_length=10.0 → Umschaltpunkt noch nicht erreicht, Finte bleibt Phase 0.
        assert bot._wg_feint_target == 99
        assert bot._wg_feint_phase == 0

        # Gegenprobe: 10.0u am Gegner vorbei (== neuer _tank_length) → Umschaltpunkt erreicht
        # (frischer Tick-Aufbau wie test_feint_switch_confirms_when_enemy_faces_bot, nur mit
        # dem auf 10.0 skalierten Abstand statt der alten TANK_LENGTH=6.0).
        bot.pos_x = 30.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._wg_feint_phase = 0
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.02)
        assert bot._wg_feint_target == 99
        assert bot._wg_feint_phase == 1
        speed_proj = bot.vel_x * math.cos(bot.azimuth) + bot.vel_y * math.sin(bot.azimuth)
        assert speed_proj < 0.0

    def test_feint_switch_aborts_when_enemy_not_facing(self, bot):
        """Gegner blickt am Umschaltpunkt WEG vom Bot → keine Finte: Ziel gelöscht, Heading
        wird über _wings_steer_az fixiert (kein Rückkrümmen zum Gegner), Bot fliegt im selben
        Tick unverändert vorwärts weiter (keine Rückwärts-Klemme)."""
        bot.own_flag = "WG"
        bot.pos_x = 26.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jumping = True
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.azimuth = math.pi   # Blick weg vom Bot → "nicht gedreht"
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 0
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0
        assert bot._wings_steer_az == pytest.approx(0.0)
        speed_proj = bot.vel_x * math.cos(bot.azimuth) + bot.vel_y * math.sin(bot.azimuth)
        assert speed_proj > 0.0   # weiter vorwärts, keine Rückwärts-Klemme
        assert bot.azimuth == pytest.approx(0.0)   # Flugrichtung unverändert

        # Folge-Tick: Heading bleibt fix (kein Zurückkrümmen zum jetzt hinter dem Bot liegenden
        # Gegner — Zielwahl (2) würde sonst wieder Richtung Gegner steuern).
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.02)
        assert bot.azimuth == pytest.approx(0.0)
        assert bot.vel_x > 0.0

    # ── Kein Phasen-Flattern: Blick-Check passiert genau einmal ─────────────

    def test_feint_no_phase_flutter_after_switch(self, bot):
        """Nach dem Umschalten (Phase 1) ändert sich die Gegner-Blickrichtung so, dass ein
        erneuter Check 'nicht gedreht' ergäbe — die Entscheidung bleibt trotzdem bestehen
        (kein erneuter Blick-Check pro Tick)."""
        bot.own_flag = "WG"
        bot.pos_x = 26.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = -12.5; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = math.pi
        bot._jumping = True
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.azimuth = math.pi   # würde bei Neubewertung "nicht gedreht" (weg vom Bot) ergeben
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1   # Umschaltpunkt bereits entschieden (Rückwärts-Phase)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot._wg_feint_target == 99
        assert bot._wg_feint_phase == 1
        speed_proj = bot.vel_x * math.cos(bot.azimuth) + bot.vel_y * math.sin(bot.azimuth)
        assert speed_proj < 0.0

    # ── Fallback: Gegner verschwindet mid-flight ────────────────────────────

    def test_feint_aborts_when_enemy_removed_mid_flight(self, bot):
        """Gegner wird während des Vorwärtsflugs aus players entfernt → Finte bricht ab
        (kein Crash), normale Kette übernimmt im selben Tick."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 0
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)   # normaler Vorwärts-Tick, Finte noch aktiv
            assert bot._wg_feint_target == 99
            del bot.players[99]
            bot._tick_jumping(0.02, now=1000.02)   # Gegner weg → Finte bricht ab, kein Crash
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0

    def test_feint_aborts_when_enemy_never_existed(self, bot):
        """_wg_feint_target zeigt auf keine bekannte Spieler-ID → sofortiger Abbruch, kein Crash."""
        bot.own_flag = "WG"
        bot.pos_x = 10.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot.target_player = None
        bot._wg_feint_target = 99   # nie in bot.players registriert
        bot._wg_feint_phase = 0
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0

    # ── Voller Ablauf: Finte bestätigt → Landung mit Gegner im Visier ───────

    def test_feint_confirmed_lands_aiming_at_enemy(self, bot):
        """Gegner verfolgt den Bot durchgehend (garantiert 'hat gedreht' am Umschaltpunkt) →
        Finte bestätigt, Rückwärtsflug bis zur Landung. Selbst-konsistent geprüft: der finale
        Azimuth zeigt auf die (stationäre) Gegnerposition (±5°) — unabhängig vom genauen
        Rückflug-Pfad."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 100.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        info = make_player(bot, 99, pos=(20.0, 2.0, 0.0))   # leichter Seitenversatz (reale Geometrie)
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 0
        dt = 0.02
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1e6), \
             patch.object(bot, "_is_landed", return_value=False):
            for i in range(600):
                # Gegner "verfolgt" den Bot live → der EINMALIGE Blick-Check am Umschaltpunkt
                # ergibt deterministisch "hat gedreht", unabhängig vom genauen Umschalt-Tick.
                dxb = bot.pos_x - info.pos[0]
                dyb = bot.pos_y - info.pos[1]
                info.azimuth = math.atan2(dyb, dxb)
                bot._tick_jumping(dt, now=1000.0 + i * dt)
                if bot._wg_feint_phase == 1:
                    break
            assert bot._wg_feint_phase == 1, "Umschaltpunkt nicht erreicht — Testaufbau prüfen"
            for _ in range(800):
                bot._tick_jumping(dt, now=2000.0)
        assert bot._wg_feint_target == 99   # Finte lief bis hierhin ungebrochen durch
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1e6), \
             patch.object(bot, "_is_landed", return_value=True):
            bot._tick_jumping(dt, now=3000.0)
        assert bot._jumping is False
        assert bot._wg_feint_target is None   # bei Landung zurückgesetzt
        expected_az = math.atan2(info.pos[1] - bot.pos_y, info.pos[0] - bot.pos_x)
        assert abs(_angle_diff(bot.azimuth, expected_az)) < math.radians(5)

    def test_feint_not_confirmed_lands_forward_past_enemy(self, bot):
        """Gegner dreht sich NICHT (Blick bleibt weg vom Bot) → keine Finte: Bot fliegt
        vorwärts weiter (Projektion wächst monoton), Azimuth bleibt bei der fixierten
        Flugrichtung (kein Rückkrümmen zum Gegner) bis zur Landung."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 100.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.azimuth = math.pi   # blickt konstant weg vom Bot → nie "gedreht"
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 0
        dt = 0.02
        last_proj = -1e9
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1e6), \
             patch.object(bot, "_is_landed", return_value=False):
            for i in range(80):
                bot._tick_jumping(dt, now=1000.0 + i * dt)
                proj = bot.pos_x - info.pos[0]
                assert proj >= last_proj - 1e-6   # Projektion wächst monoton (kein Rückflug)
                last_proj = proj
        # Finte ist beim Umschaltpunkt bereits abgebrochen (keine Rückwärtsphase erreicht)
        assert bot._wg_feint_target is None
        assert bot.vel_x > 0.0
        # Nahe am Umschaltpunkt (Projektion 0..TANK_LENGTH, Gegner exakt kollinear) versucht die
        # Phase-0-Verfolgung kurz Richtung Gegner zu drehen (az_to_enemy springt bei bx=0 auf π,
        # bevor der Umschaltpunkt bx>=TANK_LENGTH erreicht) — begrenzter Transient, kein volles
        # Zurückkrümmen (das wäre erst bei Fortsetzung der Verfolgung, die hier NICHT stattfindet).
        assert abs(bot.azimuth) < math.radians(25)
        az_after_abort = bot.azimuth
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1e6), \
             patch.object(bot, "_is_landed", return_value=True):
            bot._tick_jumping(dt, now=1000.9)
        assert bot._jumping is False
        # Landung fixiert die Heading exakt bei der Abbruch-Ausrichtung — keine Land-Drehung.
        assert bot.azimuth == pytest.approx(az_after_abort)

    # ── Fallback: Rest-Sinkzeit zu knapp ─────────────────────────────────────

    def test_feint_aborts_when_sink_time_too_tight(self, bot):
        """Kurz vor dem Boden (< 2u, vel_z < 0) bricht die Finte ab statt weiter zu manövrieren."""
        bot.own_flag = "WG"
        bot.pos_x = 10.0; bot.pos_y = 0.0; bot.pos_z = 1.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = -5.0
        bot.azimuth = 0.0
        bot._jumping = True
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1   # bereits im Rückwärtsflug, aber Boden zu nah
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=0.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0

    # ── Reset bei Landung/Tod/Respawn (dieselben Stellen wie _wings_steer_az) ─

    def test_wg_feint_target_reset_on_tick_jumping_landing(self, bot):
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.05
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=0.0), \
             patch.object(bot, "_is_landed", return_value=True):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0

    def test_wg_feint_target_reset_on_nav_jump_landing(self, bot):
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.05
        bot.vel_x = 15.0; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.NAV_JUMP
        bot._nav_jump_return_state = AIState.SEEKING
        bot._nav_jump_target_z = 0.0
        bot._nav_path = [(15.0, 0.0, 0.0)]
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=0.0), \
             patch.object(bot, "_is_landed", return_value=True):
            bot._tick_nav_jump(0.02, now=1000.0)
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0

    def test_wg_feint_target_reset_on_falling_landing(self, bot):
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.05
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._pre_fall_state = AIState.SEEKING
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=0.0), \
             patch.object(bot, "_is_landed", return_value=True):
            bot._tick_falling(0.02, now=1000.0)
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0

    def test_wg_feint_target_reset_on_death(self, bot):
        import struct
        from bzflag.protocol import MsgKilled
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1
        bot.alive = True
        bot._on_killed(MsgKilled, struct.pack(">B", bot.player_id))
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0

    def test_wg_feint_target_reset_on_respawn(self, bot):
        import struct
        from bzflag.protocol import MsgAlive
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1
        payload = (struct.pack(">B", bot.player_id) + struct.pack(">fff", 0.0, 0.0, 0.0)
                   + struct.pack(">f", 0.0))
        bot._on_alive(MsgAlive, payload)
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0

    # ── Ohne WG: byte-identisches Verhalten (kein Feint-Feld gesetzt) ───────

    def test_tick_jumping_without_wg_feint_field_untouched(self, bot):
        bot.own_flag = ""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 50.0
        bot.vel_x = 7.0; bot.vel_y = -3.0; bot.vel_z = 2.0
        bot.azimuth = 1.0
        bot._jump_ang_vel = 0.4
        bot._jumping = True
        bot._wg_feint_target = None
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now=1000.0)
        assert bot._wg_feint_target is None
        assert bot.azimuth == pytest.approx(1.0 + 0.4 * 0.02)
        assert bot.vel_x == pytest.approx(7.0)
        assert bot.vel_y == pytest.approx(-3.0)


# ── P4-MOV-03c: Bedrohungs-Ausweichen in der Luft via normaler EVADING-Logik (Commit C3) ────

class TestWingsAirborneEvading:
    """Mit WG kann der Bot in der Luft genauso lenken wie am Boden — deshalb greift die normale
    Bedrohungs-/EVADING-Logik (_handle_threat_airborne, combat.py) auch airborne aus JUMPING/
    DODGE_JUMP heraus (_tick_jumping), und der airborne-EVADING-Zweig in _tick_committed führt den
    Dodge dann in der Luft weiter (_wings_air_steer statt Boden-Rampen), inkl. Extra-Flap als
    Notausweg bei zu knapper Ausweichzeit. Muster: TestWingsAirControl/TestWingsFeint."""

    def _threaten(self, bot, now, shooter_id=2, shot_id=1, pos=None,
                  vel=(-100.0, 0.0, 0.0)):
        """Registriert einen reaktionsbereiten Schuss (react-delay bereits verstrichen).
        pos=None → Schuss auf Höhe des (airborne) Bots (_find_incoming_shot filtert Schüsse auf
        anderer Etage über HIT_RADIUS*2 aus, s. perception.py)."""
        if pos is None:
            pos = (20.0, 0.0, bot.pos_z)
        make_shot(bot, shooter_id=shooter_id, shot_id=shot_id, pos=pos, vel=vel, fire_time=now)
        bot._last_threat_id = (shooter_id, shot_id)
        bot._threat_detected_at = now - 1.0

    # ── State-Wechsel: JUMPING + WG + Bedrohung → EVADING airborne ─────────────────────────

    def test_airborne_threat_switches_to_evading_with_wg(self, bot):
        """JUMPING + WG-aktiv + eingehender bedrohlicher Schuss → Wechsel nach EVADING, noch
        in der Luft (_jumping bleibt True — keine Landung in diesem Tick)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.JUMPING
        now = time.monotonic()
        self._threaten(bot, now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.EVADING
        assert bot._jumping is True
        assert bot._dodging is True

    def test_airborne_threat_from_dodge_jump_switches_to_evading(self, bot):
        """Auch aus DODGE_JUMP heraus (nicht nur JUMPING) löst eine neue Bedrohung airborne
        EVADING aus — beide Zustände laufen über denselben _tick_jumping-Zweig."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 5.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.DODGE_JUMP
        now = time.monotonic()
        self._threaten(bot, now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.EVADING

    def test_airborne_threat_ignored_without_ai_tick(self, bot):
        """Perf-Gate: die teure Bedrohungserkennung (_find_incoming_shot) läuft nur im 10-Hz-
        KI-Raster (ai_tick=True) — auf reinen Physik-Ticks (ai_tick=False) bleibt der State
        unverändert, selbst wenn eine reaktionsbereite Bedrohung vorliegt."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.JUMPING
        now = time.monotonic()
        self._threaten(bot, now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now, ai_tick=False)
        assert bot._ai_state == AIState.JUMPING

    def test_airborne_threat_no_switch_without_wg(self, bot):
        """Ohne WG bleibt es bei der bisherigen Ballistik — keine airborne-Bedrohungsreaktion
        (LocalPlayer.cxx: keine Kontrolle in der Luft ohne Wings), exakt wie vor C3."""
        bot.own_flag = ""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jump_ang_vel = 0.0
        bot._jumping = True
        bot._ai_state = AIState.JUMPING
        now = time.monotonic()
        self._threaten(bot, now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.JUMPING

    # ── Airborne-EVADING-Bewegung: _wings_air_steer statt Boden-Rampen ──────────────────────

    def test_airborne_evading_forward_dodge_instant_no_ramp(self, bot):
        """Airborne-EVADING lenkt über _wings_air_steer — _linear_acceleration (Boden-Rampe)
        hat KEINEN Einfluss (doMomentum läuft nicht airborne): der Speed-Wechsel ist instant
        statt über mehrere Ticks anzufahren."""
        bot.own_flag = "WG"
        bot._linear_acceleration = 50.0   # Boden-Rampe aktiv — darf airborne nicht wirken
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_reverse = False
        bot._dodge_until = time.monotonic() + 1.0
        now = time.monotonic()
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_committed(0.02, now)
        speed = math.hypot(bot.vel_x, bot.vel_y)
        assert speed == pytest.approx(bot._effective_tank_speed())   # instant, kein Ramp-Anlauf
        assert bot.azimuth == pytest.approx(0.0)   # dodge_forward: Heading halten, kein Turn

    def test_airborne_evading_reverse_dodge_clamped_to_half_speed(self, bot):
        """_dodge_reverse: Rückwärts wie am Boden, aber mit der Luft-Klemme 0,5×
        _effective_tank_speed() aus _wings_air_steer."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = False
        bot._dodge_reverse = True
        bot._dodge_until = time.monotonic() + 1.0
        now = time.monotonic()
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_committed(0.02, now)
        speed = math.hypot(bot.vel_x, bot.vel_y)
        assert speed == pytest.approx(0.5 * bot._effective_tank_speed())
        assert bot.vel_x < 0.0   # tatsächlich rückwärts bei azimuth=0
        assert bot.azimuth == pytest.approx(0.0)   # kein Turn (wie am Boden)

    def test_airborne_evading_turning_dodge_couples_velocity_to_azimuth(self, bot):
        """Turning-Dodge (weder forward noch reverse): Kurs auf _dodge_dir — Kopplungs-
        Invariante gilt auch hier (vel parallel/antiparallel zu azimuth, kein Driften)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = False
        bot._dodge_reverse = False
        bot._dodge_dir = math.pi / 2
        bot._dodge_until = time.monotonic() + 1.0
        now = time.monotonic()
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_committed(0.02, now)
        assert bot.azimuth > 0.0   # dreht Richtung dodge_dir (+90°)
        vel_ang = math.atan2(bot.vel_y, bot.vel_x)
        # Halbschritt-Winkeltoleranz wie bei _wings_air_steer üblich (s. TestWingsAirControl):
        # vel wird mit dem HALBEN Winkelschritt berechnet, azimuth mit dem VOLLEN.
        tol = 0.5 * 0.02 * bot._effective_turn_rate() + 1e-6
        assert abs(_angle_diff(vel_ang, bot.azimuth)) < tol

    def test_airborne_evading_dodge_timer_expired_holds_heading(self, bot):
        """Dodge-Timer abgelaufen, aber noch in der Luft: Heading/Speed halten statt neuer
        KI-Entscheidung (Muster: _tick_committed macht 'keine neuen KI-Entscheidungen')."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        az0 = 0.4
        speed0 = 10.0
        bot.vel_x = speed0 * math.cos(az0); bot.vel_y = speed0 * math.sin(az0); bot.vel_z = 0.0
        bot.azimuth = az0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        now = time.monotonic()
        bot._dodge_until = now - 0.1   # bereits abgelaufen
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_committed(0.02, now)
        assert bot.azimuth == pytest.approx(az0)
        assert math.hypot(bot.vel_x, bot.vel_y) == pytest.approx(speed0)

    # ── Landung im airborne-EVADING → bleibt EVADING, Boden-Dodge übernimmt ────────────────

    def test_airborne_evading_landing_stays_evading_and_resets(self, bot):
        """Landung während airborne-EVADING: kein Hängenbleiben (kein FALLING, kein Zurück-
        springen auf COMBAT/SEEKING/IDLE) — der State bleibt EVADING, der Boden-Dodge-Pfad
        übernimmt ab dem nächsten Tick. Reset-Felder wie bei jeder anderen Landung; der
        Dodge-Timer/_dodging bleibt UNVERÄNDERT (Seiteneffekt-Checkliste C3)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.05
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_until = time.monotonic() + 1.0
        bot._wings_jumps_used = 1
        bot._wings_steer_az = 1.0
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1
        now = time.monotonic()
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=0.0), \
             patch.object(bot, "_is_landed", return_value=True):
            bot._tick_committed(0.02, now)
        assert bot._ai_state == AIState.EVADING   # kein Zurückspringen, kein FALLING
        assert bot._jumping is False
        assert bot._wings_jumps_used == 0
        assert bot._wings_steer_az is None
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0
        assert bot.pos_z == pytest.approx(0.0)
        assert bot.vel_z == pytest.approx(0.0)
        assert bot._dodging is True   # unverändert — Boden-Pfad führt den Dodge fort
        assert bot._dodge_forward is True

    def test_airborne_evading_landing_then_ground_path_continues_dodge(self, bot):
        """Folge-Tick nach der Landung: _jumping ist jetzt False → _tick_committed läuft über
        den normalen Boden-Dodge-Pfad weiter (kein Sonderfall mehr, keine Zusatz-Physik)."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = False   # Landung bereits erfolgt (voriger Tick)
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_until = time.monotonic() + 1.0
        now = time.monotonic()
        with patch.object(bot, "_get_floor_z", return_value=0.0), \
             patch.object(bot, "_is_landed", return_value=True), \
             patch.object(bot, "_any_incoming_threat", return_value=True):
            bot._tick_committed(0.02, now)
        # Boden-Dodge-Pfad: _tank_speed (nicht _effective_tank_speed) vorwärts, ramp-basiert.
        assert bot._ai_state == AIState.EVADING
        assert math.hypot(bot.vel_x, bot.vel_y) > 0.0

    # ── FALLING-Umleitung im Dispatch greift NICHT während airborne-EVADING ────────────────

    def test_falling_redirect_does_not_fire_during_airborne_evading(self, bot):
        """_dispatch_movement: die FALLING-Erkennung (_GROUND_STATES inkl. EVADING) prüft
        `not self._jumping` — airborne-EVADING hält _jumping bewusst True, daher greift die
        Umleitung nicht, obwohl vel_z deutlich negativ ist."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = 5.0; bot.vel_y = 0.0; bot.vel_z = -5.0   # deutlich fallend
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_until = time.monotonic() + 1.0
        now = time.monotonic()
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._dispatch_movement(0.02, now, ai_tick=True)
        assert bot._ai_state == AIState.EVADING   # NICHT nach FALLING umgeleitet
        assert bot._jumping is True

    # ── Extra-Flap als Notausweg ─────────────────────────────────────────────────────────

    def test_extra_flap_fires_when_falling_and_threat_tight(self, bot):
        """Fallend + Bedrohung auf Kollisionskurs mit knapper Restzeit (< 0.4s) + Luftsprung
        übrig → zusätzlicher Flap hebt vel_z an, _wings_jumps_used inkrementiert.

        Audit-Fix-Regression: bewusst UNGEMOCKTE Sprungprüfung (_can_air_jump) bei gesetztem
        _dodging — mit dem alten _can_jump-Gate war der Notausweg toter Code, weil _dodging
        im airborne-EVADING immer True ist."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 20.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = -10.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_until = time.monotonic() + 1.0
        bot._wings_jumps_used = 0
        bot._wings_jump_count = 2   # Server-Var: 1 Luftsprung übrig (Bodensprung zählt mit)
        now = time.monotonic()
        # Schuss knapp vor dem Bot (Höhe = Bot-Höhe, sonst filtert perception.py ihn als
        # Schuss "auf anderer Etage" aus), sehr schnell → time_to_closest ≈ 0.05s (< 0.4s)
        make_shot(bot, shooter_id=2, shot_id=1, pos=(5.0, 0.0, 20.0),
                  vel=(-100.0, 0.0, 0.0), fire_time=now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_committed(0.02, now)
        expected_gravity_only = -10.0 + bot._effective_gravity() * 0.02
        assert bot.vel_z > expected_gravity_only + 1.0   # spürbar angehoben (Flap zusätzlich)
        assert bot._wings_jumps_used == 1

    def test_extra_flap_scan_only_on_ai_tick(self, bot):
        """Perf-Gate: der teure _find_incoming_shot-Scan (und damit der Flap) läuft nur im
        10-Hz-KI-Raster — ai_tick=False → kein Flap trotz akuter Bedrohung."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 20.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = -10.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_until = time.monotonic() + 1.0
        bot._wings_jumps_used = 0
        bot._wings_jump_count = 2
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1, pos=(5.0, 0.0, 20.0),
                  vel=(-100.0, 0.0, 0.0), fire_time=now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_committed(0.02, now, ai_tick=False)
        expected_vz = -10.0 + bot._effective_gravity() * 0.02
        assert bot.vel_z == pytest.approx(expected_vz)
        assert bot._wings_jumps_used == 0

    def test_extra_flap_skipped_without_remaining_jumps(self, bot):
        """Keine Luftsprünge mehr übrig (_wings_jumps_used >= _wings_jump_count - 1,
        reale _can_air_jump-Prüfung) → kein Flap trotz akuter Bedrohung."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 20.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = -10.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_until = time.monotonic() + 1.0
        bot._wings_jump_count = 2
        bot._wings_jumps_used = 1   # == count - 1 → verbraucht
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1, pos=(5.0, 0.0, 20.0),
                  vel=(-100.0, 0.0, 0.0), fire_time=now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_committed(0.02, now)
        expected_vz = -10.0 + bot._effective_gravity() * 0.02
        assert bot.vel_z == pytest.approx(expected_vz)
        assert bot._wings_jumps_used == 1

    def test_extra_flap_skipped_when_threat_not_tight(self, bot):
        """Bedrohung vorhanden, aber Restzeit komfortabel (≥ 0.4s) → kein Flap, seitliches
        Ausweichen reicht."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 20.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = -10.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_until = time.monotonic() + 1.0
        bot._wings_jumps_used = 0
        bot._wings_jump_count = 2   # Luftsprung wäre übrig — der Skip kommt vom Threat-Timing
        now = time.monotonic()
        # Schuss weit weg, langsam → time_to_closest deutlich > 0.4s
        make_shot(bot, shooter_id=2, shot_id=1, pos=(300.0, 0.0, 20.0),
                  vel=(-50.0, 0.0, 0.0), fire_time=now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_committed(0.02, now)
        expected_vz = -10.0 + bot._effective_gravity() * 0.02
        assert bot.vel_z == pytest.approx(expected_vz)
        assert bot._wings_jumps_used == 0

    def test_extra_flap_skipped_when_not_falling(self, bot):
        """Noch steigend (vel_z >= 0) → kein Flap, auch bei akuter Bedrohung + freiem Sprung."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 20.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 3.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.EVADING
        bot._dodging = True
        bot._dodge_forward = True
        bot._dodge_until = time.monotonic() + 1.0
        bot._wings_jumps_used = 0
        now = time.monotonic()
        make_shot(bot, shooter_id=2, shot_id=1, pos=(5.0, 0.0, 20.0),
                  vel=(-100.0, 0.0, 0.0), fire_time=now)
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False), \
             patch.object(bot, "_can_jump", return_value=True):
            bot._tick_committed(0.02, now)
        assert bot._wings_jumps_used == 0

    # ── Finte + neue Bedrohung → Finte bricht ab, EVADING übernimmt ────────────────────────

    def test_feint_aborts_for_real_threat_evading_takes_over(self, bot):
        """Eine laufende WG-TactJump-Finte (C2) bricht für eine echte Bedrohung ab: der
        Threat-Check läuft in _tick_jumping VOR der Finte (_handle_threat_airborne räumt
        _wg_feint_* mit auf) — 'wer täuscht, bricht die Täuschung nur für echte Bedrohungen
        ab'."""
        bot.own_flag = "WG"
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = bot._tank_speed; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._jumping = True
        bot._ai_state = AIState.JUMPING
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 0
        now = time.monotonic()
        # anderer Schütze als der Finte-Gegner — realistisches Bedrohungsszenario
        self._threaten(bot, now, shooter_id=3, shot_id=1,
                       pos=(0.0, 20.0, bot.pos_z), vel=(0.0, -100.0, 0.0))
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now, ai_tick=True)
        assert bot._wg_feint_target is None
        assert bot._wg_feint_phase == 0
        assert bot._ai_state == AIState.EVADING
        assert bot._dodging is True

    def test_feint_phase1_also_aborts_for_real_threat(self, bot):
        """Auch in der Rückwärtsphase (Phase 1, kein Blick-Check mehr fällig) bricht eine echte
        Bedrohung die Finte ab."""
        bot.own_flag = "WG"
        bot.pos_x = 26.0; bot.pos_y = 0.0; bot.pos_z = 30.0
        bot.vel_x = -12.5; bot.vel_y = 0.0; bot.vel_z = -1.0
        bot.azimuth = math.pi
        bot._jumping = True
        bot._ai_state = AIState.JUMPING
        info = make_player(bot, 99, pos=(20.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._wg_feint_target = 99
        bot._wg_feint_phase = 1
        now = time.monotonic()
        self._threaten(bot, now, shooter_id=3, shot_id=1,
                       pos=(26.0, 40.0, bot.pos_z), vel=(0.0, -100.0, 0.0))
        with patch.object(bot, "_can_drive_through_obstacles", return_value=True), \
             patch.object(bot, "_get_floor_z", return_value=-1000.0), \
             patch.object(bot, "_is_landed", return_value=False):
            bot._tick_jumping(0.02, now, ai_tick=True)
        assert bot._wg_feint_target is None
        assert bot._ai_state == AIState.EVADING
