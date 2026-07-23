"""
Tactical behaviour tests: tactical jump (wind-up, escape, reverse), jump cooldown,
jump direction snap, landing shot timing/feasibility/oscillation, self-shot filter,
wind-up abort timing, state machine transitions.
"""
import math
import struct
import time

import pytest
from unittest.mock import patch, MagicMock
from conftest import make_player, make_shot


# ---------------------------------------------------------------------------
# Schritt 5: Taktischer Übersprung mit Wind-Up
# ---------------------------------------------------------------------------

class TestTacticalJump:
    """Schritt 5: Taktischer Übersprung mit Wind-Up."""

    def _setup(self, bot, dist=30.0, enemy_vel=(-15.0, 0.0, 0.0)):
        """Setzt Bot+Gegner: Bot bei (0,0,0), Gegner bei (dist,0,0); beide aufeinander zu."""
        from conftest import make_player
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_player = 2
        p = make_player(bot, 2, pos=(dist, 0.0, 0.0))
        p.vel = list(enemy_vel)
        p.alive = True
        p.azimuth = math.pi  # Gegner schaut auf Bot (enemy_faces_bot-Check bestehen)
        return p

    def test_wind_up_triggered(self, bot, monkeypatch):
        self._setup(bot)
        monkeypatch.setattr("random.random", lambda: 0.0)  # bypasse 25%-Sperre
        result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._dodging is True
        assert bot._jump_pending is True

    def test_no_trigger_when_jumping(self, bot, monkeypatch):
        self._setup(bot)
        bot._jumping = True
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert bot._check_tactical_jump(time.monotonic()) is False

    def test_no_trigger_with_nj_flag(self, bot, monkeypatch):
        self._setup(bot)
        bot.own_flag = "NJ"
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert bot._check_tactical_jump(time.monotonic()) is False

    def test_no_trigger_with_bu_flag(self, bot, monkeypatch):
        self._setup(bot)
        bot.own_flag = "BU"
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert bot._check_tactical_jump(time.monotonic()) is False

    def test_no_trigger_dist_too_small(self, bot, monkeypatch):
        self._setup(bot, dist=3.0)  # < 5.0
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert bot._check_tactical_jump(time.monotonic()) is False

    def test_random_throttle_blocks(self, bot, monkeypatch):
        self._setup(bot)
        monkeypatch.setattr("random.random", lambda: 0.8)  # >= 0.7 → blockiert (30% Sperre)
        assert bot._check_tactical_jump(time.monotonic()) is False

    def test_no_jump_overextended_on_closing(self, bot, monkeypatch):
        """Klärungs-Check: weiter Gegner (dist=90), der sich nähert, wird NICHT übersprungen.
        Annäherung zählt nur über die Reaktionszeit (0.5s → 7.5u), nicht über die volle Flugzeit
        → Reichweite ~97u reicht für 1.5×(90−7.5)=124u nicht → kein Sprung (sonst Landung davor)."""
        self._setup(bot, dist=90.0, enemy_vel=(-15.0, 0.0, 0.0))  # nähert sich mit 15 u/s
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert bot._check_tactical_jump(time.monotonic()) is False

    def test_no_jump_retreating_enemy(self, bot, monkeypatch):
        """Klärungs-Check: zurückweichender Gegner (dist=50, vel weg vom Bot) wird über die volle
        Flugzeit projiziert → nötige Strecke wächst auf ~89u, 1.5× davon > Reichweite → kein Sprung."""
        self._setup(bot, dist=50.0, enemy_vel=(10.0, 0.0, 0.0))  # entfernt sich mit 10 u/s
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert bot._check_tactical_jump(time.monotonic()) is False

    def test_jump_near_closing_enemy_still_triggers(self, bot, monkeypatch):
        """Gegenprobe: naher, sich nähernder Gegner (dist=30) bleibt überspringbar
        (enemy_dist_at_land=22.5; 1.5×=33.75 < Reichweite ~97u)."""
        self._setup(bot, dist=30.0, enemy_vel=(-15.0, 0.0, 0.0))
        monkeypatch.setattr("random.random", lambda: 0.0)
        assert bot._check_tactical_jump(time.monotonic()) is True

    def test_phase_two_executes_jump(self, bot):
        """Nach Wind-Up-Ende (JUMP_WINDUP-State, dodge expired) führt _tick_committed den Sprung aus."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._jump_pending = True
        bot._dodging = False
        bot._jumping = False
        bot.ang_vel = 0.5  # Wind-Up-Velocity
        bot._ai_state = AIState.JUMP_WINDUP
        bot._dodge_until = 0.0  # Wind-Up abgelaufen
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._jumping is True
        assert bot.vel_z == pytest.approx(bot._jump_velocity)
        assert bot._jump_ang_vel == pytest.approx(0.5)
        assert bot._jump_pending is False

    def test_phase_two_waits_during_dodge(self, bot):
        """Während Wind-Up-Dodge läuft, Phase 2 noch nicht aktiv."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._jump_pending = True
        bot._dodging = True  # Dodge läuft noch
        bot._jumping = False
        bot._check_tactical_jump(time.monotonic())
        assert bot._jumping is False
        assert bot._jump_pending is True  # bleibt pending


# ---------------------------------------------------------------------------
# Testlauf-Folgebefunde (Iteration 2)
# ---------------------------------------------------------------------------


class TestTacticalJumpReverseOverride:
    """Schritt 1 (Iteration 2): Wind-Up setzt Override-Fenster und Phase 2 setzt Vorwärts-vel."""

    def test_wind_up_sets_override_window(self, bot, monkeypatch):
        from conftest import make_player
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.target_player = 2
        p = make_player(bot, 2, pos=(30.0, 0.0, 0.0))
        p.vel = [-15.0, 0.0, 0.0]
        p.azimuth = math.pi  # Gegner schaut auf Bot
        monkeypatch.setattr("random.random", lambda: 0.0)
        t = time.monotonic()
        assert bot._check_tactical_jump(t) is True
        assert bot._tactical_jump_until >= t + 0.45  # ~0.5s Fenster

    def test_phase_two_sets_forward_velocity(self, bot):
        """Phase 2 (JUMP_WINDUP → JUMPING) setzt vel[0]/vel[1] auf volle Vorwärts-Geschwindigkeit."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0  # blickt nach +X
        bot.vel_x = -10.0; bot.vel_y = 0.0; bot.vel_z = 0.0  # fährt rückwärts
        bot._jump_pending = True
        bot._dodging = False
        bot._jumping = False
        bot._ai_state = AIState.JUMP_WINDUP
        bot._dodge_until = 0.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._jumping is True
        assert bot.vel_x == pytest.approx(bot._tank_speed)
        assert bot.vel_y == pytest.approx(0.0)
        assert bot.vel_z == pytest.approx(bot._jump_velocity)

    def test_random_50_percent_threshold(self, bot, monkeypatch):
        """50%-Sperre (Vorwärtssprung): random=0.1 → Trigger, random=0.6 → Block."""
        from conftest import make_player
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0  # Bot schaut direkt auf Gegner (angle_to_enemy < 45°)
        bot.target_player = 2
        p = make_player(bot, 2, pos=(30.0, 0.0, 0.0))
        p.vel = [-15.0, 0.0, 0.0]
        p.azimuth = math.pi  # Gegner schaut auf Bot
        monkeypatch.setattr("random.random", lambda: 0.1)
        assert bot._check_tactical_jump(time.monotonic()) is True
        # Gegenseite: random=0.6 (>= Schwelle 0.5) → kein Sprung
        bot._dodging = False; bot._jump_pending = False
        bot._ai_state.__class__  # reset not needed, just test the gate
        monkeypatch.setattr("random.random", lambda: 0.6)
        assert bot._check_tactical_jump(time.monotonic()) is False


class TestWindUpInterruptible:
    """State Machine: JUMP_WINDUP ist committed — kein Abbruch durch eingehenden Schuss."""

    def test_windup_committed_no_abort_on_threat(self, bot):
        """In JUMP_WINDUP-State: Schuss bricht Wind-Up nicht mehr ab (Entscheidung steht)."""
        from conftest import make_shot
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.alive = True
        bot.human_count = 1
        bot._dodging = True
        bot._jump_pending = True
        bot._tactical_jump_until = time.monotonic() + 0.5
        bot._dodge_until = time.monotonic() + 0.12
        bot._ai_state = AIState.JUMP_WINDUP
        # Eingehender Schuss (t_impact=0.2s > 0.1s Notfall-Schwelle → kein Notschuss)
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(20.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        # State Machine: Wind-Up läuft weiter
        assert bot._jump_pending is True
        assert bot._dodging is True
        assert bot._tactical_jump_until > 0.0


class TestWindUpAbortTiming:
    """Schritt 4: Wind-Up wird nur bei wirklich imminent eintreffenden Schüssen abgebrochen."""

    def test_windup_committed_continues_despite_threat(self, bot):
        """State Machine: JUMP_WINDUP-State läuft trotz ankommendem Schuss weiter."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.player_id = 1
        bot._dodging = True
        bot._jump_pending = True
        bot._tactical_jump_until = time.monotonic() + 0.5
        bot._dodge_until = time.monotonic() + 0.12
        bot._ai_state = AIState.JUMP_WINDUP
        # Shot bei 20u, vel -100 u/s → t_impact = 0.2s (> 0.1s Notfall-Schwelle → kein Notschuss)
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(20.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        # Kein Abbruch mehr: Wind-Up läuft weiter
        assert bot._jump_pending is True
        assert bot._dodging is True

    def test_distant_shot_does_not_abort_wind_up(self, bot):
        """JUMP_WINDUP-State: auch 0.5s-Schuss unterbricht Wind-Up nicht (kein Abort mehr)."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.player_id = 1
        bot._dodging = True
        bot._jump_pending = True
        bot._tactical_jump_until = time.monotonic() + 0.5
        bot._dodge_until = time.monotonic() + 0.12
        bot._ai_state = AIState.JUMP_WINDUP
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._jump_pending is True
        assert bot._dodging is True


class TestSelfShotFilter:
    """Schritt 2: Eigene Schüsse werden in _find_incoming_shot ignoriert."""

    def test_own_shot_not_detected_as_threat(self, bot):
        """Schuss mit shooter_id=player_id wird nicht als Bedrohung gewertet."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.player_id = 1
        # Eigener Schuss: shooter_id=1 = player_id, direkt auf Bot zufliegend
        make_shot(bot, shooter_id=1, shot_id=99,
                  pos=(5.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        shot, _ = bot._find_incoming_shot(time.monotonic())
        assert shot is None

    def test_enemy_shot_still_detected(self, bot):
        """Schuss eines Gegners (shooter_id≠player_id) wird weiterhin erkannt."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.player_id = 1
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(15.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0))
        shot, _ = bot._find_incoming_shot(time.monotonic())
        assert shot is not None

    def test_wind_up_not_aborted_by_own_shot(self, bot):
        """Eigener Schuss bricht Wind-Up des taktischen Übersprungs nicht ab."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.player_id = 1
        bot._dodging = True
        bot._jump_pending = True
        bot._tactical_jump_until = time.monotonic() + 0.5
        bot._dodge_until = time.monotonic() + 0.12
        # Eigener Schuss bei d≈5 — würde ohne Filter den Wind-Up abbrechen
        make_shot(bot, shooter_id=1, shot_id=42,
                  pos=(5.0, 0.0, 0.0), vel=(-200.0, 0.0, 0.0))
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._jump_pending is True  # Wind-Up läuft weiter


class TestJumpFacingCheck:
    """Schritt 1 Fix A: Sprung nur wenn bot_faces_enemy UND enemy_faces_bot (±70°)."""

    def _setup(self, bot, bot_az, enemy_az, dist=40.0):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = bot_az
        bot.alive = True
        bot.human_count = 1
        bot._last_jump_at = 0.0
        bot.own_flag = ""
        info = make_player(bot, 99, pos=(dist, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.azimuth = enemy_az
        info.is_airborne = False
        bot.target_player = 99

    def test_no_jump_when_enemy_faces_away(self, bot):
        """Gegner schaut weg (az=0, läuft rechts) → kein Sprung."""
        # Bot at (0,0) az=0, enemy at (40,0) az=0 (faces right = away from bot)
        self._setup(bot, bot_az=0.0, enemy_az=0.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is False
        assert bot._jump_pending is False

    def test_no_jump_when_bot_faces_away_without_closing(self, bot):
        """Bot hat Rücken zum Gegner (az=π), aber Gegner steht still → kein Sprung.
        Rückwärtssprung benötigt enemy_closing >= 5 m/s."""
        # Bot at (0,0) az=π (faces left = back to enemy), enemy az=π (faces bot), vel=0
        self._setup(bot, bot_az=math.pi, enemy_az=math.pi)
        # info.vel = [0,0,0] aus _setup → enemy_closing = 0 < 5 → kein Rückwärtssprung
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is False
        assert bot._jump_pending is False

    def test_jump_allowed_when_both_facing(self, bot):
        """Beide schauen sich an: bot az=0, enemy az=π → Sprung startet."""
        # Bot at (0,0) az=0 (faces right), enemy at (40,0) az=π (faces left = toward bot)
        self._setup(bot, bot_az=0.0, enemy_az=math.pi)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._jump_pending is True


class TestJumpCooldown:
    """Schritt 1 Fix A (Sicherheitsnetz): JUMP_COOLDOWN=4s verhindert Re-Jump."""

    def _setup_jump_ready(self, bot):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot.alive = True
        bot.human_count = 1
        bot.own_flag = ""
        info = make_player(bot, 99, pos=(40.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.azimuth = math.pi  # enemy faces bot
        info.is_airborne = False
        bot.target_player = 99

    def test_no_immediate_rejump(self, bot):
        """Sprung vor 1s gelandet (< 4s Cooldown) → kein Re-Jump."""
        self._setup_jump_ready(bot)
        bot._last_jump_at = time.monotonic() - 1.0
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is False

    def test_jump_allowed_after_cooldown(self, bot):
        """Sprung vor 5s gelandet (> 4s Cooldown) → Jump erlaubt."""
        self._setup_jump_ready(bot)
        bot._last_jump_at = time.monotonic() - 5.0
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._jump_pending is True


class TestJumpCooldownResetOnDeath:
    """_last_jump_at wird nach Tod auf 0 gesetzt → kein staler Cooldown nach Respawn."""

    def test_cooldown_reset_on_kill(self, bot):
        """_report_killed setzt _last_jump_at = 0.0."""
        from bzflag.protocol import MsgKilled
        from conftest import make_shot
        # Simuliere: Bot war zuletzt vor 1s gelandet (innerhalb Cooldown)
        bot._last_jump_at = time.monotonic() - 1.0
        # Hit-Detection → report_killed
        shot = make_shot(bot, pos=(5.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0),
                         shooter_id=2)
        bot.alive = True
        bot._report_killed(bot._shots[list(bot._shots.keys())[0]])
        assert bot._last_jump_at == pytest.approx(0.0)

    def test_cooldown_reset_on_steamroller(self, bot):
        """_report_steamrolled setzt _last_jump_at = 0.0."""
        bot._last_jump_at = time.monotonic() - 1.0
        bot.alive = True
        bot._report_steamrolled(killer_id=99)
        assert bot._last_jump_at == pytest.approx(0.0)


class TestJumpDirectionSnap:
    """Schritt 1 Fix B: Azimuth wird auf Zielrichtung eingerastet vor dem Sprung."""

    def test_azimuth_snapped_to_enemy(self, bot):
        """Bot zeigt 90° am Gegner vorbei → nach Phase-2 (JUMP_WINDUP) zeigt vel direkt auf Gegner."""
        from bot.models import AIState
        info = make_player(bot, 99, pos=(40.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.azimuth = math.pi
        bot.target_player = 99
        # Bot zeigt nach oben (90° neben Gegner, der bei az=0 wäre)
        bot.azimuth = math.pi / 2
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._jump_pending = True
        bot._dodging = False
        bot._jumping = False
        bot._ai_state = AIState.JUMP_WINDUP
        bot._dodge_until = 0.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        # Azimuth soll auf (40, 0) gerastet haben → az ≈ 0
        assert bot.azimuth == pytest.approx(0.0, abs=0.01)
        assert bot.vel_x == pytest.approx(bot._tank_speed, abs=0.1)
        assert abs(bot.vel_y) < 0.5
        assert bot._jumping is True


class TestJumpTriggerRound4:
    """Schritt 1 (Runde 4): bot_faces_enemy-Check entfernt. enemy_faces_bot bleibt."""

    def _setup_facing(self, bot, bot_az, enemy_az, dist=40.0, enemy_closing=25.0):
        """Bot bei (0,0) mit bot_az, Gegner bei (dist, 0) mit enemy_az."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = bot_az
        bot.alive = True
        bot.human_count = 1
        bot._last_jump_at = 0.0
        bot.own_flag = ""
        info = make_player(bot, 99, pos=(dist, 0.0, 0.0))
        info.vel = [-enemy_closing, 0.0, 0.0]   # nähert sich von rechts
        info.azimuth = enemy_az
        info.is_airborne = False
        bot.target_player = 99

    def test_jump_triggers_at_80deg_via_escape_jump(self, bot):
        """Bot-Azimuth 80° weg vom Gegner → Escape-Jump (Szenario 5) wenn Gegner schließt.
        Szenario 4 nicht (Zwischenzone 45°-135°), aber Szenario 5 greift: closing>10, dist<60, az>=45°."""
        self._setup_facing(bot, bot_az=math.radians(80), enemy_az=math.pi, dist=40.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._jump_pending is True
        assert bot._escape_jump_ang_vel is not None  # Escape-Jump-Modus

    def test_no_jump_when_enemy_faces_away_and_stationary(self, bot):
        """Gegner dreht Rohr weg (az=0) und steht still → kein Sprung.
        enemy_faces_bot=False (az-Check) + velocity-Fallback greift nicht (closing=0).
        Szenario 5 blockiert da angle_to_enemy=0° < 45°."""
        # Bot bei (0,0) az=0, Gegner bei (40,0) az=0 (Rohr weg), vel=0 (kein Closing)
        self._setup_facing(bot, bot_az=0.0, enemy_az=0.0, dist=40.0, enemy_closing=0.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is False

    def test_jump_allowed_90deg_azimuth_offset(self, bot):
        """Bot-Az 90° (Zwischenzone 45°-135°): Escape-Jump (Szenario 5) greift.
        Szenario 4 nicht (nicht < 45° oder > 135°), aber closing=25>10, dist=40<60, az=90°>=45°."""
        self._setup_facing(bot, bot_az=math.pi / 2, enemy_az=math.pi, dist=40.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._escape_jump_ang_vel is not None  # Escape-Jump-Modus (Szenario 5)


class TestEscapeJump:
    """Schritt 1: Escape-Jump wenn Bot Rücken zum Gegner hat und Gegner aufholt."""

    def _setup_escape(self, bot, enemy_closing=15.0, dist=40.0):
        """Bot zeigt WEG vom Gegner (az=0, Gegner bei (dist,0) = rechts von Bot)."""
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi  # Bot schaut nach links (weg vom Gegner rechts)
        bot.alive = True
        bot.human_count = 1
        bot._last_jump_at = 0.0
        bot.own_flag = ""
        info = make_player(bot, 99, pos=(dist, 0.0, 0.0))
        info.vel = [-enemy_closing, 0.0, 0.0]   # Gegner nähert sich von rechts
        info.azimuth = math.pi   # Gegner schaut auf Bot
        info.is_airborne = False
        bot.target_player = 99

    def test_escape_jump_when_bot_faces_away(self, bot):
        """Bot Rücken zum Gegner (az=π), Gegner schaut auf Bot, closing=15 → Rückwärtssprung.
        Szenario 4 Rückwärts: angle_to_enemy=180°>135°, enemy_faces_bot=True, closing≥5 → Sprung."""
        self._setup_escape(bot, enemy_closing=15.0, dist=40.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._jump_pending is True
        assert bot._dodge_reverse is True        # Rückwärtssprung
        assert bot._escape_jump_ang_vel is None  # kein Flip bei Rückwärtssprung

    def test_no_escape_jump_when_enemy_not_closing(self, bot):
        """Bot Rücken zum Gegner, closing=3 < 5 (Mindestgeschwindigkeit für Rückwärtssprung) → kein Sprung.
        Szenario 4 Rückwärts: enemy_closing < 5 → return False. Szenario 5: closing < 10 → False."""
        self._setup_escape(bot, enemy_closing=3.0, dist=40.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is False

    def test_escape_reverse_jump_vars(self, bot):
        """Rückwärtssprung (closing=15, az=π): _dodge_reverse=True, _escape_jump_ang_vel=None.
        Bot landet automatisch mit Blick auf Gegner — kein Flip nötig."""
        self._setup_escape(bot, enemy_closing=15.0, dist=40.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._dodge_reverse is True
        assert bot._escape_jump_ang_vel is None  # kein Flip bei Rückwärtssprung

    def test_escape_phase2_no_azimuth_snap(self, bot):
        """Phase 2 Escape-Jump (JUMP_WINDUP): Azimuth bleibt, _jump_ang_vel = escape_ang_vel."""
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = math.pi
        bot._jump_pending = True
        bot._dodging = False
        bot._jumping = False
        bot._escape_jump_ang_vel = -bot._tank_turn_rate
        bot._ai_state = AIState.JUMP_WINDUP
        bot._dodge_until = 0.0
        info = make_player(bot, 99, pos=(40.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        bot.target_player = 99
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        # Azimuth soll π bleiben (kein Snap), ang_vel = escape_ang_vel
        assert bot.azimuth == pytest.approx(math.pi, abs=0.01)
        assert bot._jump_ang_vel == pytest.approx(-bot._tank_turn_rate, abs=0.01)
        assert bot._jumping is True


class TestReverseJump:
    """Rückwärtssprung: Bot zeigt Rücken, Gegner schaut auf Bot, Gegner closing ≥ 5."""

    def _setup(self, bot, enemy_closing=10.0):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = math.pi  # Bot zeigt Rücken zu Gegner (bei +x)
        bot.alive = True
        bot.human_count = 1
        bot._last_jump_at = 0.0
        bot.own_flag = ""
        info = make_player(bot, 99, pos=(40.0, 0.0, 0.0))
        info.vel = [-enemy_closing, 0.0, 0.0]  # kommt von rechts
        info.azimuth = math.pi  # Gegner schaut auf Bot (nach links)
        info.is_airborne = False
        bot.target_player = 99

    def test_reverse_jump_triggers(self, bot):
        """Bot Rücken zu Gegner der schaut + closing=10 ≥ 5 → Rückwärtssprung."""
        self._setup(bot, enemy_closing=10.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._dodge_reverse is True
        assert bot._escape_jump_ang_vel is None

    def test_no_reverse_jump_below_min_closing(self, bot):
        """closing=4 < 5 → kein Rückwärtssprung (Mindestgeschwindigkeit nicht erreicht)."""
        self._setup(bot, enemy_closing=4.0)
        from unittest.mock import patch
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is False


class TestStateMachineTransitions:
    """State Machine: Übergänge zwischen AIState-Werten."""

    def test_initial_state_is_idle(self, bot):
        from bot.models import AIState
        assert bot._ai_state == AIState.IDLE

    def test_spawn_transitions_to_seeking_with_humans(self, bot):
        """_on_alive mit menschlichem Spieler → State SEEKING (human_count aus players abgeleitet)."""
        from bot.models import AIState
        make_player(bot, 2, pos=(50.0, 0.0, 0.0))   # ein Mensch in der Spielerliste
        payload = b'\x01' + b'\x00' * 12 + b'\x00' * 4  # pid=1, pos=0, az=0
        bot._on_alive(0, payload)
        assert bot._ai_state == AIState.SEEKING

    def test_spawn_transitions_to_idle_without_humans(self, bot):
        """_on_alive ohne Menschen in der Liste → State IDLE."""
        from bot.models import AIState
        bot.players.clear()
        payload = b'\x01' + b'\x00' * 12 + b'\x00' * 4
        bot._on_alive(0, payload)
        assert bot._ai_state == AIState.IDLE

    def test_death_transitions_to_dead(self, bot):
        """_report_killed setzt State auf DEAD."""
        from bot.models import AIState
        from conftest import make_shot
        shot = make_shot(bot, shooter_id=2, shot_id=1)
        bot._report_killed(shot)
        assert bot._ai_state == AIState.DEAD

    def test_tactical_jump_transitions_to_jump_windup(self, bot):
        """_check_tactical_jump setzt State auf JUMP_WINDUP."""
        from bot.models import AIState
        from unittest.mock import patch
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        info.vel = [-15.0, 0.0, 0.0]
        info.azimuth = math.pi
        bot.target_player = 99
        bot.azimuth = 0.0
        with patch("random.random", return_value=0.0):
            result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._ai_state == AIState.JUMP_WINDUP

    def test_jump_windup_to_jumping_on_execute(self, bot):
        """_tick_committed nach Wind-Up: JUMP_WINDUP → JUMPING."""
        from bot.models import AIState
        bot._jump_pending = True
        bot._dodging = False
        bot._jumping = False
        bot._ai_state = AIState.JUMP_WINDUP
        bot._dodge_until = 0.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._ai_state == AIState.JUMPING
        assert bot._jumping is True

    def test_jumping_to_combat_on_landing(self, bot):
        """Landing (pos[2]=0) aus JUMPING → COMBAT wenn target_player vorhanden."""
        from bot.models import AIState
        info = make_player(bot, 99, pos=(30.0, 0.0, 0.0))
        bot.target_player = 99
        bot.human_count = 1
        bot._ai_state = AIState.JUMPING
        bot._jumping = True
        bot.vel_z = -5.0   # Fallgeschwindigkeit
        bot.pos_z = 0.001  # fast auf Boden
        bot._gravity = -9.8
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._ai_state == AIState.COMBAT
        assert bot._jumping is False

    def test_combat_to_seeking_on_target_lost(self, bot):
        """Ziel verloren, aber Mensch noch anwesend → COMBAT → SEEKING (nicht IDLE)."""
        from bot.models import AIState
        bot._has_presence = lambda: True   # Mensch anwesend (Mitspieler ODER Zuschauer)
        bot._ai_state = AIState.COMBAT
        bot.target_player = 99  # Spieler existiert nicht im Dict
        bot._next_shoot = float("inf")
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.SEEKING

    def test_evading_to_combat_after_timer(self, bot):
        """Dodge-Timer abgelaufen → EVADING → COMBAT."""
        from bot.models import AIState
        info = make_player(bot, 99, pos=(50.0, 0.0, 0.0))
        bot.target_player = 99
        bot.human_count = 1
        bot._dodging = True
        bot._dodge_until = time.monotonic() - 0.1  # bereits abgelaufen
        bot._dodge_dir = math.pi / 2
        bot._ai_state = AIState.EVADING
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot._ai_state == AIState.COMBAT
        assert bot._dodging is False

    def test_no_ai_during_jumping(self, bot):
        """In JUMPING: kein _tick_ai, target_player unverändert."""
        from bot.models import AIState
        bot._ai_state = AIState.JUMPING
        bot._jumping = True
        bot.target_player = 99  # nicht validiert solange JUMPING
        bot.pos_z = 5.0        # in der Luft
        bot.vel_z = -1.0
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        # target_player soll unverändert sein (kein AI-Tick in JUMPING)
        assert bot.target_player == 99


class TestLandingShotTiming:
    """Schritt 2: tof_to_landing-Prüfung verhindert Schuss bei falscher Timing."""

    def _setup_jumping_target(self, bot, z0, vz, target_dist):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._next_shoot = 0.0
        bot._shot_speed = 100.0
        info = make_player(bot, 99, pos=(target_dist, 0.0, z0))
        info.vel = [0.0, 0.0, vz]
        info.is_airborne = True
        bot.target_player = 99
        return info

    def test_holds_fire_when_shot_arrives_too_early(self, bot):
        """t_land≈1.0s, tof_to_landing=0.4s → zu früh, kein Schuss."""
        # Enemy at (40, 0, 10), vel_z=-5 → t_land ≈ 1.0s, tof_to_landing=0.4s
        self._setup_jumping_target(bot, z0=10.0, vz=-5.0, target_dist=40.0)
        bot._maybe_shoot(time.monotonic())
        assert not bot.client.send.called

    def test_fires_when_timing_matches(self, bot):
        """tof_to_landing ≈ t_land → Schuss auf Landeposition."""
        # Enemy at (50, 0, 2), vel_z=-5 → t_land≈0.31s, tof≈0.5s → Fallback normal
        # Falls in Fallback-Zweig (zu spät) → velocity-lead normal, Schuss erlaubt
        self._setup_jumping_target(bot, z0=2.0, vz=-5.0, target_dist=50.0)
        bot._maybe_shoot(time.monotonic())
        assert bot.client.send.called

    def test_fallback_normal_when_enemy_nearly_landed(self, bot):
        """t_land klein, tof >> t_land → Fallback velocity-lead, Bot schießt normal."""
        # Enemy at (100, 0, 0.2), vel_z=-0.5 → t_land sehr klein, tof=1.0s → Fallback
        self._setup_jumping_target(bot, z0=0.2, vz=-0.5, target_dist=100.0)
        bot._maybe_shoot(time.monotonic())
        assert bot.client.send.called


class TestLandingShotOscillation:
    """LANDING_SHOT: kein _move_to_target → kein _new_target() → kein Oszillieren."""

    def test_no_oscillation_landing_shot(self, bot):
        """Bot in LANDING_SHOT, mehrere _update_movement-Aufrufe → bleibt in LANDING_SHOT."""
        from bot.models import AIState
        from conftest import make_player
        info = make_player(bot, 99, pos=(50.0, 0.0, 5.0))
        info.vel = [0.0, 0.0, -3.0]
        info.is_airborne = True
        bot.target_player = 99
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.human_count = 1
        bot._ai_state = AIState.LANDING_SHOT
        bot._landing_shot_until = time.monotonic() + 2.0
        bot._landing_aim_pos = (50.0, 0.0)
        # 5 Ticks — soll nicht zu SEEKING oszillieren
        for _ in range(5):
            bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot._ai_state == AIState.LANDING_SHOT
        assert bot.target_player == 99  # target_player bleibt gesetzt

    def test_landing_shot_vel_zero(self, bot):
        """Im LANDING_SHOT-State: vel[0] = vel[1] = 0 (Position halten)."""
        from bot.models import AIState
        from conftest import make_player
        info = make_player(bot, 99, pos=(50.0, 0.0, 5.0))
        info.vel = [0.0, 0.0, -3.0]
        info.is_airborne = True
        bot.target_player = 99
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 10.0; bot.vel_y = 5.0; bot.vel_z = 0.0  # Startet mit Geschwindigkeit
        bot.human_count = 1
        bot._ai_state = AIState.LANDING_SHOT
        bot._landing_shot_until = time.monotonic() + 2.0
        bot._landing_aim_pos = (50.0, 0.0)
        bot._update_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot.vel_x == pytest.approx(0.0)
        assert bot.vel_y == pytest.approx(0.0)


class TestLandingShotFeasibility:
    """_maybe_shoot: Drehtzeit + tof muss < t_land-0.1 sein für LANDING_SHOT-Entry."""

    def _setup_jumping_target(self, bot, z0, vz, target_dist, bot_az=0.0):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = bot_az
        bot._next_shoot = 0.0
        bot._shot_speed = 100.0
        from conftest import make_player
        info = make_player(bot, 99, pos=(target_dist, 0.0, z0))
        info.vel = [0.0, 0.0, vz]
        info.is_airborne = True
        bot.target_player = 99
        bot.human_count = 1
        from bot.models import AIState
        bot._ai_state = AIState.COMBAT
        return info

    def test_entry_when_small_turn_needed(self, bot):
        """Gegner springt, Landepunkt nah vor Bot (az≈0) → LANDING_SHOT-Entry möglich."""
        from bot.models import AIState
        # Enemy at (50, 0), z=8, vz=-8: t_land ≈ 0.9s. Landepunkt ≈ (50,0).
        # Bot az=0, turn_needed≈0 → turn_time≈0. tof≈0.5s. 0+0.5 < 0.9-0.1=0.8 → feasible.
        self._setup_jumping_target(bot, z0=8.0, vz=-8.0, target_dist=50.0, bot_az=0.0)
        bot._maybe_shoot(time.monotonic())
        assert bot._ai_state == AIState.LANDING_SHOT

    def test_no_entry_when_large_turn_needed(self, bot):
        """Landepunkt ist 90° seitlich → Drehtzeit > verfügbare Zeit → kein LANDING_SHOT."""
        from bot.models import AIState
        # Enemy at (50, 0), z=8, vz=-8. Bot az=π/2 (90° weggedreht vom Landepunkt).
        # turn_needed=π/2, turn_time=π/2/0.785≈2.0s > t_land-0.1≈0.8s → nicht feasible.
        self._setup_jumping_target(bot, z0=8.0, vz=-8.0, target_dist=50.0, bot_az=math.pi / 2)
        bot._maybe_shoot(time.monotonic())
        # Kein LANDING_SHOT-Entry; stattdessen normaler Vorhalteschuss oder kein Schuss
        assert bot._ai_state != AIState.LANDING_SHOT

    def test_no_entry_beyond_max_range(self, bot):
        """Landepunkt > OPTIMAL_RANGE*3=180m → kein LANDING_SHOT."""
        from bot.models import AIState
        # Enemy at (190, 0), z=15, vz=-5 → t_land≈2.3s. Landepunkt ≈ 190m.
        # 190 > 180 → not can_aim → normaler Schuss.
        self._setup_jumping_target(bot, z0=15.0, vz=-5.0, target_dist=190.0, bot_az=0.0)
        bot._maybe_shoot(time.monotonic())
        assert bot._ai_state != AIState.LANDING_SHOT


class TestLandingShotZAwareness:
    """P4-TAC-06/07 (Z-Bewusstsein): Der Flachschuss läuft auf Mündungshöhe. Landet der Gegner
    deutlich höher, ist er unerreichbar (kein LANDING_SHOT); landet er tiefer, wird auf den Moment
    getimt, in dem er die Mündungshöhe durchfällt (Interzeption beim Fallen, t_aim < t_land)."""

    def _setup(self, bot, z0, vz, dist, bot_z=0.0, bot_az=0.0):
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = bot_z
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = bot_az
        bot._next_shoot = 0.0
        bot._shot_speed = 100.0
        bot._ai_state = AIState.COMBAT
        info = make_player(bot, 99, pos=(dist, 0.0, z0))
        info.vel = [0.0, 0.0, vz]
        info.is_airborne = True
        bot.target_player = 99
        return info

    def _nav_floor(self, z_at_landing):
        nav = MagicMock()
        nav.get_floor_z.side_effect = lambda x, y, z, overhang=0.0: z_at_landing
        return nav

    def test_p4_tac_06_suppresses_higher_landing(self, bot):
        """Boden am Landepunkt > Bot-Z + HIT_RADIUS → kein LANDING_SHOT, normaler Vorhalt."""
        from bot.models import AIState
        # Gleicher Sprung wie der Entry-Test (z0=8, vz=-8, dist=50), der ohne Plattform in
        # LANDING_SHOT ginge. Boden=15u am Landepunkt (> 0 + ~5.62) → unterdrückt.
        info = self._setup(bot, z0=8.0, vz=-8.0, dist=50.0)
        bot._nav_graph = self._nav_floor(15.0)
        aim = bot._compute_aim_point(
            (info.pos[0], info.pos[1]), info, 50.0, 0.0, 50.0, time.monotonic())
        assert bot._ai_state == AIState.COMBAT     # kein Entry
        assert aim is not None                      # Fallback-Vorhalt statt None

    def test_p4_tac_06_entry_when_floor_level(self, bot):
        """Gegenprobe: Boden=0 am Landepunkt → LANDING_SHOT-Entry, _landing_hit_z = 0."""
        from bot.models import AIState
        info = self._setup(bot, z0=8.0, vz=-8.0, dist=50.0)
        bot._nav_graph = self._nav_floor(0.0)
        aim = bot._compute_aim_point(
            (info.pos[0], info.pos[1]), info, 50.0, 0.0, 50.0, time.monotonic())
        assert bot._ai_state == AIState.LANDING_SHOT
        assert aim is None
        assert bot._landing_hit_z == pytest.approx(0.0)

    def test_p4_tac_07_intercept_at_muzzle_height(self, bot):
        """Bot erhöht (z=15), Gegner landet tiefer (Boden=0): Interzeption beim Durchfallen der
        Mündungshöhe → _landing_hit_z = Bot-Z + Mündungshöhe (statt 0) → früher feuern."""
        from bot.constants import MUZZLE_HEIGHT
        from bot.models import AIState
        info = self._setup(bot, z0=25.0, vz=-5.0, dist=50.0, bot_z=15.0)
        bot._nav_graph = self._nav_floor(0.0)
        aim = bot._compute_aim_point(
            (info.pos[0], info.pos[1]), info, 50.0, 0.0, 50.0, time.monotonic())
        assert bot._ai_state == AIState.LANDING_SHOT
        assert aim is None
        assert bot._landing_hit_z == pytest.approx(15.0 + MUZZLE_HEIGHT)


class TestLandingShotGMHoming:
    """Fix 3: Mit GM gegen Nicht-ST-Gegner kein LANDING_SHOT (die Rakete lenkt selbst);
    gegen ST bleibt der LANDING_SHOT erhalten (GM kann nicht zielsuchen)."""

    def _setup(self, bot, enemy_flag=""):
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._next_shoot = 0.0
        bot._shot_speed = 100.0
        bot.own_flag = "GM"
        bot._ai_state = AIState.COMBAT
        info = make_player(bot, 99, pos=(50.0, 0.0, 8.0), flag=enemy_flag)
        info.vel = [0.0, 0.0, -8.0]
        info.is_airborne = True
        bot.target_player = 99
        return info

    def test_gm_vs_non_st_no_landing_shot(self, bot):
        """GM-Bot, springender Nicht-ST-Gegner → kein LANDING_SHOT-Entry, Lead-Aim (≠ None)."""
        from bot.models import AIState
        info = self._setup(bot, enemy_flag="")
        aim = bot._compute_aim_point(
            (info.pos[0], info.pos[1]), info, 50.0, 0.0, 50.0, time.monotonic())
        assert bot._ai_state == AIState.COMBAT
        assert aim is not None

    def test_gm_vs_st_keeps_landing_shot(self, bot):
        """Gegenprobe: GM-Bot gegen ST-Gegner → LANDING_SHOT-Entry wie gehabt (GM homt nicht)."""
        from bot.models import AIState
        info = self._setup(bot, enemy_flag="ST")
        aim = bot._compute_aim_point(
            (info.pos[0], info.pos[1]), info, 50.0, 0.0, 50.0, time.monotonic())
        assert bot._ai_state == AIState.LANDING_SHOT
        assert aim is None


class TestLandingShotTickFires:
    """Fix 1: _tick_landing_shot feuert SELBST (analog Z_ATTACK), sobald die Restfallzeit ≈
    Schuss-Flugzeit ist — der Z-Block in _maybe_shoot_* wird umgangen, kein verspäteter Schuss."""

    def _setup_landing_state(self, bot, enemy_z, enemy_vz):
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0
        bot._shot_speed = 100.0
        bot._next_shoot = 0.0
        bot._ai_state = AIState.LANDING_SHOT
        bot._landing_aim_pos = (50.0, 0.0)
        bot._landing_hit_z = 0.0
        bot._landing_shot_until = time.monotonic() + 5.0
        bot.human_count = 1
        info = make_player(bot, 99, pos=(50.0, 0.0, enemy_z))
        info.vel = [0.0, 0.0, enemy_vz]
        info.is_airborne = True
        bot.target_player = 99
        return info

    def test_holds_when_too_early(self, bot):
        """Restfallzeit ≫ tof+0.15 → noch kein Schuss, bleibt in LANDING_SHOT."""
        from bot.models import AIState
        # z=15, vz=-3 → t_rem ≈ 1.47s ≫ tof(0.5)+0.15 → außerhalb Fenster.
        self._setup_landing_state(bot, enemy_z=15.0, enemy_vz=-3.0)
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert not bot.client.send.called
        assert bot._ai_state == AIState.LANDING_SHOT

    def test_fires_in_window_despite_z_block(self, bot):
        """Restfallzeit im Fenster (t_rem ≈ 0.61s ≤ tof+0.15) bei z_diff=8 > HIT_RADIUS:
        der Z-Block würde einen COMBAT-Schuss unterdrücken — der Tick feuert trotzdem.
        maxShots=1 (Fixture-Default) → kein Nachschuss möglich → sofort COMBAT."""
        from bot.models import AIState
        # z=8, vz=-10 → t_rem ≈ 0.614s; tof = 50/100 = 0.5s → im 0.15s-Fenster.
        self._setup_landing_state(bot, enemy_z=8.0, enemy_vz=-10.0)
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot.client.send.called
        assert bot._ai_state == AIState.COMBAT

    def test_double_shot_fires_second(self, bot):
        """Doppelklick-Nachschuss: bei freiem zweiten Slot bleibt der Bot nach dem ersten
        Schuss in LANDING_SHOT und feuert nach LANDING_DOUBLE_SHOT_DELAY erneut."""
        from bot.models import AIState
        from bot.constants import LANDING_DOUBLE_SHOT_DELAY
        self._setup_landing_state(bot, enemy_z=8.0, enemy_vz=-10.0)
        bot._max_shots = 2
        bot._shot_slot = 0
        bot._slot_reload_at = [0.0, 0.0]   # beide Slots frei
        t0 = time.monotonic()
        bot._update_movement(0.02, t0, ai_tick=True)
        assert bot.client.send.called                       # erster Schuss
        assert bot._ai_state == AIState.LANDING_SHOT
        assert bot._landing_second_shot_at is not None
        first_calls = bot.client.send.call_count
        # Zweiter Tick nach Ablauf des Doppelklick-Delays → Nachschuss + Übergang.
        bot._update_movement(0.02, t0 + LANDING_DOUBLE_SHOT_DELAY + 0.01, ai_tick=True)
        assert bot.client.send.call_count > first_calls      # zweiter Schuss
        assert bot._ai_state == AIState.COMBAT
        assert bot._landing_second_shot_at is None

    def test_double_shot_early_out_when_no_slot(self, bot):
        """Early-Out: wird bis zum Doppelklick-Fenster kein Slot frei, transitioniert der Bot
        sofort (wie bei maxShots=1) statt sinnlos in LANDING_SHOT zu verweilen."""
        from bot.models import AIState
        self._setup_landing_state(bot, enemy_z=8.0, enemy_vz=-10.0)
        bot._max_shots = 2
        bot._shot_slot = 0
        t0 = time.monotonic()
        bot._slot_reload_at = [t0 + 1.0, 0.0]   # nächster Slot (0) lädt erst nach 1.0s nach
        bot._update_movement(0.02, t0, ai_tick=True)
        assert bot.client.send.called
        assert bot._ai_state == AIState.COMBAT
        assert bot._landing_second_shot_at is None


# ---------------------------------------------------------------------------
# Jump prediction (free function from TestNewFeatures)
# ---------------------------------------------------------------------------

def test_jump_prediction_landing_point(bot):
    """Sprung-Vorhalt: Landepunkt wird korrekt berechnet."""
    # Einfaches Beispiel: Gegner bei (50, 0, 10), vel=(0, 0, -5), gravity=-9.8
    # t_land = (-(-5) - sqrt(25 + 2*9.8*10)) / (-9.8)
    # disc = 25 + 196 = 221, sqrt(221) ≈ 14.87
    # t_land = (5 - 14.87) / (-9.8) ≈ 1.007s
    g, z0, vz = -9.8, 10.0, -5.0
    disc = vz*vz - 2.0 * g * z0
    t_land = (-vz - math.sqrt(disc)) / g
    assert t_land == pytest.approx(1.007, abs=0.01)
    # landing_x = 50 (keine horizontale Bewegung)
    assert 50.0 + 0.0 * t_land == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Runde 8: Fix J1b — Taktischer Sprung: enemy_faces_bot Velocity-Fallback
# ---------------------------------------------------------------------------

class TestJumpEnemyFacesBot:
    """Fix J1b: enemy_faces_bot greift auch wenn info.azimuth veraltet ist,
    solange der Gegner sich mit > 5 u/s nähert (velocity fallback)."""

    def _setup(self, bot, enemy_az=math.pi, enemy_closing=25.0, dist=40.0):
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
        bot.azimuth = 0.0  # bot schaut direkt auf Gegner
        bot.alive = True
        bot.human_count = 1
        bot._last_jump_at = 0.0
        bot.own_flag = ""
        info = make_player(bot, 99, pos=(dist, 0.0, 0.0))
        info.vel = [-enemy_closing, 0.0, 0.0]
        info.azimuth = enemy_az
        info.is_airborne = False
        bot.target_player = 99
        return info

    def test_closing_enemy_back_to_bot_no_jump(self, bot, monkeypatch):
        """TACT-01: Gegner mit Rücken zum Bot (azimuth=0, weg vom Bot) und hohem
        Closing-Speed → kein TactJump (würde vor Gegner-Nase landen)."""
        self._setup(bot, enemy_az=0.0, enemy_closing=25.0)  # azimuth zeigt weg vom Bot
        monkeypatch.setattr("random.random", lambda: 0.0)
        result = bot._check_tactical_jump(time.monotonic())
        assert result is False  # TACT-01: angle_enemy_to_bot=180° > 120° → geblockt

    def test_stationary_enemy_needs_correct_azimuth(self, bot, monkeypatch):
        """Stehender Gegner (closing=0): Fallback greift nicht; korrekte azimuth=π → Sprung."""
        from bot.models import AIState
        self._setup(bot, enemy_az=math.pi, enemy_closing=0.0)  # azimuth korrekt, kein Closing
        monkeypatch.setattr("random.random", lambda: 0.0)
        result = bot._check_tactical_jump(time.monotonic())
        assert result is True
        assert bot._jump_pending is True


# ---------------------------------------------------------------------------
# Offensiver Ricochet-Sweep
# ---------------------------------------------------------------------------

class TestOffensiveRicochet:
    """Phase 5: 80-Winkel-Sweep — _compute_ricochet_aim und Hilfsfunktionen."""

    def _make_world(self, boxes):
        """Minimale WorldMap-Attrappe mit boxes-Attribut."""
        wmap = MagicMock()
        wmap.boxes = boxes
        return wmap

    def _setup(self, bot, server_ricochet=True):
        from bzflag.world_map import BoxObstacle
        # Bot (0,-90), Gegner (0,90), 30×30-Box bei (0,0) um 45° rotiert → direkte LoS blockiert,
        # aber Schüsse über (±50, 0) prallen an der Box-Fläche und treffen den Gegner.
        bot.pos_x = 0.0; bot.pos_y = -90.0; bot.pos_z = 0.0
        bot.azimuth = math.pi / 2  # schaut Richtung Norden
        bot.world_half = 100.0
        bot._server_ricochet = server_ricochet
        bot.own_flag = ""
        bot.target_player = 2
        box = BoxObstacle(cx=0.0, cy=0.0, bottom_z=0.0, angle=math.pi / 4,
                          half_w=15.0, half_d=15.0, height=10.0,
                          is_pyramid=False, z_flip=False, ricochet=False,
                          shoot_through=False)
        bot._world_map = self._make_world([box])
        p = make_player(bot, 2, pos=(0.0, 90.0, 0.0))
        p.vel = [0.0, 0.0, 0.0]
        p.alive = True
        return p

    def test_finds_ricochet_path_when_blocked(self, bot):
        """Box bei (0,0) um 45° blockiert LoS; Sweep findet Abprall-Azimut."""
        self._setup(bot, server_ricochet=True)
        result = bot._compute_ricochet_aim(2, None)
        assert result is not None, "Sweep soll Ricochet-Azimut finden"
        az, via_tele = result
        assert -math.pi <= az <= math.pi
        assert via_tele is False  # reiner Box-Abprall, kein Teleporter

    def test_no_ricochet_without_server_ricochet(self, bot):
        """Ohne server_ricochet und ohne R-Flag: can_ricochet() = False → Sweep nie aufgerufen."""
        from bzflag.shot_physics import can_ricochet
        bot._server_ricochet = False
        fb = b"\x00\x00"  # Standard-Flag
        assert can_ricochet(fb, False, False, False) is False

    def test_ricochet_with_r_flag(self, bot):
        """R-Flag ermöglicht Ricochet auch ohne server_ricochet."""
        from bzflag.shot_physics import can_ricochet
        assert can_ricochet(b"R\x00", False, False, False) is True

    def test_segment_hits_obb_3d_importable(self):
        """_segment_hits_obb_3d muss aus bzflag.shot_physics importierbar sein (kein zirkulärer Import)."""
        from bzflag.shot_physics import _segment_hits_obb_3d
        # Trivialtest: Segment durch OBB-Mittelpunkt trifft immer
        assert _segment_hits_obb_3d(-10, 0, 1, 10, 0, 1,  0, 0, 1, 0.0,  5.0, 5.0, 2.0) is True
        # Segment klar daneben trifft nicht
        assert _segment_hits_obb_3d(-10, 20, 1, 10, 20, 1,  0, 0, 1, 0.0,  5.0, 5.0, 2.0) is False

    def test_cache_reuses_result(self, bot):
        """_find_ricochet_aim_angle liefert dasselbe Ergebnis beim zweiten Aufruf (Cache)."""
        self._setup(bot, server_ricochet=True)
        bot.own_flag = ""
        r1 = bot._find_ricochet_aim_angle(2, None)
        r2 = bot._find_ricochet_aim_angle(2, None)
        assert r1 == r2

    def test_cache_invalidates_on_target_change(self, bot):
        """Cache verfällt wenn sich target_pid ändert."""
        self._setup(bot, server_ricochet=True)
        bot.own_flag = ""
        bot._find_ricochet_aim_angle(2, None)
        # Anderer Spieler: kein Cache-Treffer → neu berechnet (kein Crash)
        make_player(bot, 3, pos=(60.0, 0.0, 0.0))
        bot._find_ricochet_aim_angle(3, None)  # darf nicht crashen


# ── A1/A4: Cross-Floor-Indirekt-Trigger & Teleporter-Schuss vor Z_ATTACK ──────

def test_cross_floor_indirect_true_for_elevated_with_teleporters(bot):
    """Gegner auf anderer Etage + Teleporter vorhanden → indirekter Schuss kommt in Frage
    (NICHT an die Sprunghöhe gekoppelt)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.own_flag = ""
    bot._server_ricochet = False
    wmap = MagicMock(); wmap.teleporters = [object()]
    bot._world_map = wmap
    info = make_player(bot, 2, pos=(50.0, 0.0, 30.0))
    assert bot._cross_floor_indirect(info) is True


def test_cross_floor_indirect_false_same_height(bot):
    """Gleiche Höhe → Direktschuss reicht, kein Indirekt-Trigger (auch mit Ricochet)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.own_flag = ""
    bot._server_ricochet = True
    info = make_player(bot, 2, pos=(50.0, 0.0, 0.0))
    assert bot._cross_floor_indirect(info) is False


def test_z_attack_skipped_when_teleporter_shot_available(bot):
    """A4: Liegt ein Teleporter-Schuss vor, wird der Z-Sprung übersprungen (sicherer Schuss)."""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot.target_player = 2
    info = make_player(bot, 2, pos=(50.0, 0.0, 5.0))
    info.is_airborne = False
    now = 100.0
    with patch.object(bot, "_can_jump", return_value=True), \
         patch.object(bot, "_effective_jump_height", return_value=100.0), \
         patch.object(bot, "_teleporter_shot_available", return_value=True):
        assert bot._check_z_attack_jump(now) is False


class TestLandingShotMomentumStop:
    """P4-MOV-02b: LANDING_SHOT bremst die Landegeschwindigkeit bei aktivem -a sanft gegen 0
    (Ramp), statt hart zu stoppen; ohne -a bleibt der sofortige Stopp erhalten."""

    def _setup_landed(self, bot):
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.vel_x = 20.0; bot.vel_y = 0.0; bot.vel_z = 0.0   # trägt Landegeschwindigkeit
        bot._jumping = False
        bot._landing_aim_pos = None
        bot._ai_state = AIState.LANDING_SHOT

    def test_ramps_down_with_dash_a(self, bot):
        self._setup_landed(bot)
        bot._linear_acceleration = 10.0    # max_delta = 20×10×0.02 = 4.0 → 20 → 16
        bot._dispatch_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot.vel_x == pytest.approx(16.0, abs=1e-6)   # sanft gebremst, nicht 0
        assert 0.0 < bot.vel_x < 20.0

    def test_hard_stop_without_dash_a(self, bot):
        self._setup_landed(bot)
        bot._linear_acceleration = 0.0
        bot._dispatch_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot.vel_x == pytest.approx(0.0, abs=1e-9)   # sofortiger Stopp wie bisher


class TestLandingShotSlideIntegration:
    """F1: der LANDING_SHOT-Ramp-gegen-0-Zweig integriert die Position jetzt real über
    _apply_bounds (statt vel ungenutzt auf dem Wire zu melden) — Ausrollen wie im echten Client."""

    def _setup_landed(self, bot):
        from bot.models import AIState
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.vel_x = 20.0; bot.vel_y = 0.0; bot.vel_z = 0.0   # trägt Landegeschwindigkeit
        bot._jumping = False
        bot._landing_aim_pos = None
        bot._ai_state = AIState.LANDING_SHOT

    def test_ramps_and_moves_with_dash_a(self, bot):
        self._setup_landed(bot)
        bot._linear_acceleration = 10.0    # max_delta = 20×10×0.02 = 4.0 → 20 → 16
        bot._dispatch_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot.vel_x == pytest.approx(16.0)
        # _apply_bounds integriert mit der NEUEN (gerampten) vel: pos_x = 0 + 16.0*0.02 = 0.32
        assert bot.pos_x == pytest.approx(16.0 * 0.02)
        assert bot.pos_x != 0.0

    def test_no_movement_without_dash_a(self, bot):
        self._setup_landed(bot)
        bot._linear_acceleration = 0.0
        bot._dispatch_movement(0.02, time.monotonic(), ai_tick=False)
        assert bot.pos_x == 0.0
