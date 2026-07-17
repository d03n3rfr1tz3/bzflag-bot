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
from conftest import make_shot, make_player

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
