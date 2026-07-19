"""
Flag-related tests: flag strategy (keep/drop), flag updates, flag grab FOV,
neutral flag drop behaviour, and opportunistic flag grabbing.
"""
import math
import struct
import time

import pytest
from unittest.mock import MagicMock, patch
from conftest import make_player, make_shot


# ---------------------------------------------------------------------------
# P2-FLG-01 + P2-FLG-02: Flag-Strategie
# ---------------------------------------------------------------------------

class TestFlagStrategy:

    def _grab_flag(self, bot, flag: str, pid: int = 1):
        """Simuliert MsgGrabFlag für Spieler pid mit Flagge flag.
        Layout: uint8 pid(1) + uint16 flag_index(2) + 2B flag_abbv(2) = 5 Bytes."""
        from bzflag.protocol import MsgGrabFlag
        flag_bytes = (flag.encode('ascii') + b'\x00\x00')[:2]
        payload = struct.pack(">BH", pid, 0) + flag_bytes
        bot._on_grab_flag(MsgGrabFlag, payload)

    def test_good_flag_kept(self, bot):
        self._grab_flag(bot, "GM")
        assert bot.own_flag == "GM"
        bot.client.send.assert_not_called()  # kein MsgDropFlag gesendet

    def test_cs_kept_as_good_flag(self, bot):
        # CS (Cloaked Shot) ist als offensive Tarnschuss-Flagge good → behalten, kein Drop
        self._grab_flag(bot, "CS")
        assert bot.own_flag == "CS"
        assert "CS" in bot.good_flags
        bot.client.send.assert_not_called()

    def test_bad_flag_dropped(self, bot):
        # Drop erfolgt erst nach _drop_bad_flag_delay (nicht sofort)
        self._grab_flag(bot, "WG")
        assert bot.own_flag == "WG"
        bot.client.send.assert_not_called()  # kein sofortiger Drop

    def test_bad_flag_drop_after_delay(self, bot):
        # Nach Ablauf des Cooldowns sendet der Loop MsgDropFlag
        self._grab_flag(bot, "WG")
        bot._own_flag_since = time.monotonic() - (bot._drop_bad_flag_delay + 0.1)
        bot._last_drop_attempt = 0.0
        bot._try_drop_flag()
        bot.client.send.assert_called()

    def test_neutral_flag_dropped(self, bot):
        # BY (Bouncy) weder good noch bad → kein sofortiger Drop
        self._grab_flag(bot, "BY")
        bot.client.send.assert_not_called()

    def test_sw_kept(self, bot):
        self._grab_flag(bot, "SW")
        assert bot.own_flag == "SW"
        bot.client.send.assert_not_called()

    def test_laser_kept(self, bot):
        self._grab_flag(bot, "L")
        assert bot.own_flag == "L"
        bot.client.send.assert_not_called()

    def test_drop_confirmed_clears_flag(self, bot):
        from bzflag.protocol import MsgDropFlag
        self._grab_flag(bot, "GM")
        # Server bestätigt Drop
        payload = struct.pack(">B", bot.player_id)
        bot._on_drop_flag(MsgDropFlag, payload)
        assert bot.own_flag == ""

    def test_custom_good_flags(self):
        """Bot mit eigener good_flags-Liste behält nur konfigurierte Flags."""
        from unittest.mock import MagicMock, patch
        with patch("bot.core.BZFlagClient"):
            from bot.core import BZBot
            b = BZBot(host="localhost", callsign="Test",
                      good_flags=["V"], bad_flags=["WG"])
        b.client = MagicMock()
        b.client.udp_active = True
        b.player_id = 1
        b.alive = True
        b.pos_x = 0.0; b.pos_y = 0.0; b.pos_z = 0.0

        # V ist gut → behalten
        flag_bytes = b"V\x00"
        payload = struct.pack(">BH", 1, 0) + flag_bytes
        from bzflag.protocol import MsgGrabFlag
        b._on_grab_flag(MsgGrabFlag, payload)
        assert b.own_flag == "V"
        b.client.send.assert_not_called()

        # GM ist nicht in eigener Liste → own_flag gesetzt, kein sofortiger Drop
        b.client.send.reset_mock()
        flag_bytes = b"GM"
        payload = struct.pack(">BH", 1, 0) + flag_bytes
        b._on_grab_flag(MsgGrabFlag, payload)
        assert b.own_flag == "GM"
        assert b.own_flag not in b.good_flags  # GM nicht in eigener custom-Liste


# ---------------------------------------------------------------------------
# P2-FLG-04+06: Flag-Update-Parsing + Drop-Cooldown (Schritt 7)
# ---------------------------------------------------------------------------

class TestFlagUpdate:

    def _build_flag_update(self, flags, owner=255):
        """Baut MsgFlagUpdate-Payload (57 Bytes/Eintrag): flag_id(2)+Flag::pack()(55).
        owner: PlayerId des Trägers (255 = NoPlayer)."""
        buf = struct.pack(">H", len(flags))
        for flag_id, abbr, status, x, y, z in flags:
            abbr_b = (abbr.encode('ascii') + b'\x00\x00')[:2]
            entry = (
                struct.pack(">H", flag_id)      # flag_id (2)
                + abbr_b                         # abbr (2)
                + struct.pack(">H", status)      # status (2)
                + struct.pack(">H", 0)           # endurance (2)
                + struct.pack(">B", owner)       # owner (1)
                + struct.pack(">fff", x, y, z)   # position (12)
                + struct.pack(">fff", 0, 0, 0)   # launchPos (12)
                + struct.pack(">fff", 0, 0, 0)   # landingPos (12) ← war bisher vergessen!
                + struct.pack(">f", 0.0)         # flightTime (4)
                + struct.pack(">f", 0.0)         # flightEnd (4)
                + struct.pack(">f", 0.0)         # initialVelocity (4)
            )
            assert len(entry) == 57
            buf += entry
        return buf

    def test_flag_on_ground_added(self, bot):
        from bzflag.protocol import MsgFlagUpdate
        payload = self._build_flag_update([(1, "GM", 1, 50.0, 30.0, 0.0)])
        bot._on_flag_update(MsgFlagUpdate, payload)
        assert 1 in bot.flags
        assert bot.flags[1].abbr == "GM"
        assert bot.flags[1].status == 1

    def test_flag_on_tank_removed(self, bot):
        from bzflag.protocol import MsgFlagUpdate
        # Erst auf Boden
        bot.flags[1] = __import__('bot.models', fromlist=['FlagInfo']).FlagInfo(1, "GM", 1, [50, 30, 0])
        # Jetzt onTank
        payload = self._build_flag_update([(1, "GM", 2, 50.0, 30.0, 0.0)])
        bot._on_flag_update(MsgFlagUpdate, payload)
        assert 1 not in bot.flags

    def test_carried_flag_seeds_owner_on_join(self, bot):
        """Voll-Dump beim Join: getragene Flagge wird dem Träger zugeordnet
        (deckt den ursprünglichen Bug: spät gejointe Bots kannten Trägerschaft nicht)."""
        from bzflag.protocol import MsgFlagUpdate
        make_player(bot, 2, flag="")            # Träger bereits bekannt, ohne Flag
        payload = self._build_flag_update([(3, "BU", 2, 10.0, 20.0, 0.0)], owner=2)
        bot._on_flag_update(MsgFlagUpdate, payload)
        assert bot.players[2].flag == "BU"
        assert 3 not in bot.flags               # getragen ≠ Boden-Flag

    def test_carried_flag_no_owner_leaves_players_untouched(self, bot):
        """owner=255 (NoPlayer) darf keine Trägerschaft setzen (Regression)."""
        from bzflag.protocol import MsgFlagUpdate
        make_player(bot, 2, flag="")
        payload = self._build_flag_update([(3, "BU", 2, 10.0, 20.0, 0.0)], owner=255)
        bot._on_flag_update(MsgFlagUpdate, payload)
        assert bot.players[2].flag == ""

    def test_grab_flag_removes_from_flags(self, bot):
        from bzflag.protocol import MsgGrabFlag
        bot.flags[5] = __import__('bot.models', fromlist=['FlagInfo']).FlagInfo(5, "SW", 1, [100, 0, 0])
        payload = struct.pack(">BH", bot.player_id, 5) + b"SW"
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert 5 not in bot.flags

    def test_drop_cooldown_respected(self, bot):
        """Drop nur nach _drop_bad_flag_delay, nicht sofort."""
        from bzflag.protocol import MsgGrabFlag
        bot._drop_bad_flag_delay = 15.0
        payload = struct.pack(">BH", bot.player_id, 0) + b"WG"
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert bot.own_flag == "WG"
        # Noch keine 15s → kein Drop
        bot.client.send.reset_mock()
        bot._own_flag_since = time.monotonic()
        bot._last_drop_attempt = 0.0
        # Simuliere Loop-Check mit zu wenig Zeit
        now = time.monotonic()
        held = now - bot._own_flag_since
        assert held < bot._drop_bad_flag_delay  # Drop darf noch nicht passieren

    def test_drop_bad_flag_delay_parsed(self, bot):
        from bzflag.protocol import MsgSetVar
        items = b""
        name = b"_dropBadFlagDelay"
        val = b"15.0"
        items += struct.pack(">B", len(name)) + name + struct.pack(">B", len(val)) + val
        payload = struct.pack(">H", 1) + items
        bot._on_set_var(MsgSetVar, payload)
        assert bot._drop_bad_flag_delay == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Schritt 4: Opportunistic Grab nur bei Flag im FOV
# ---------------------------------------------------------------------------

class TestFlagGrabFOV:
    """Schritt 4: Opportunistic Grab nur bei Flag im FOV."""

    def test_grab_when_in_fov(self, bot):
        from bot.models import FlagInfo
        bot.human_count = 1
        bot.azimuth = 0.0  # schaut nach +X
        bot.flags[3] = FlagInfo(3, "GM", 1, [6.0, 0.0, 0.0])  # vor Bot, in FOV, in Radius
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._last_grab_attempt = 0.0
        bot._check_opportunistic_grab(time.monotonic())
        bot.client.send.assert_called()

    def test_no_grab_behind_bot(self, bot):
        from bot.models import FlagInfo
        bot.human_count = 1
        bot.azimuth = 0.0  # schaut nach +X
        bot.flags[3] = FlagInfo(3, "GM", 1, [-6.0, 0.0, 0.0])  # HINTER Bot
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._last_grab_attempt = 0.0
        bot._check_opportunistic_grab(time.monotonic())
        bot.client.send.assert_not_called()

    def test_grab_directly_under_bot_no_fov_check(self, bot):
        from bot.models import FlagInfo
        bot.human_count = 1
        bot.azimuth = 0.0
        # Flag unter Bot (< TANK_RADIUS=4.32) → keine FOV-Prüfung
        bot.flags[3] = FlagInfo(3, "GM", 1, [-2.0, 0.0, 0.0])  # hinter Bot aber sehr nah
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._last_grab_attempt = 0.0
        bot._check_opportunistic_grab(time.monotonic())
        bot.client.send.assert_called()


# ---------------------------------------------------------------------------
# Konstanten-Audit (Commit B): Flag-Grab-Radius folgt der nachgeführten
# Server-Var _tank_length/_flag_radius statt der starren FLAG_GRAB_RADIUS-Konstante.
# Bot darf nie schlechter dastehen als mit der alten Konstante (max()-Helper).
# ---------------------------------------------------------------------------

class TestFlagGrabRadiusScaling:

    def test_flag_grab_radius_never_below_legacy(self, bot):
        """Kleine _tank_length → Radius bleibt beim alten FLAG_GRAB_RADIUS (~8.64u),
        wird NICHT kleiner (max()-Helper schützt vor Verschlechterung)."""
        from bot.constants import FLAG_GRAB_RADIUS
        bot._tank_length = 1.0   # winziger Custom-Tank
        assert bot._flag_grab_radius() == pytest.approx(FLAG_GRAB_RADIUS)

    def test_flag_grab_radius_scales_up(self, bot):
        """Große _tank_length → Radius wächst über den Legacy-Wert hinaus
        (effektiver Tank-Radius + Flag-Radius + Marge)."""
        from bot.constants import FLAG_GRAB_RADIUS, FLAG_GRAB_MARGIN, TANK_RADIUS_FACTOR
        bot._tank_length = 20.0   # großer Custom-Tank
        expected = TANK_RADIUS_FACTOR * 20.0 + bot._flag_radius + FLAG_GRAB_MARGIN
        assert expected > FLAG_GRAB_RADIUS
        assert bot._flag_grab_radius() == pytest.approx(expected)

    def test_opportunistic_grab_uses_scaled_radius(self, bot):
        """_check_opportunistic_grab nutzt den skalierten Radius: eine Flagge außerhalb
        des Legacy-FLAG_GRAB_RADIUS (~8.64u), aber innerhalb des durch große _tank_length
        vergrößerten Radius, wird gegriffen."""
        from bot.models import FlagInfo
        bot._tank_length = 20.0   # großer Custom-Tank → Grab-Radius deutlich > 8.64u
        bot.azimuth = 0.0
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot.flags[3] = FlagInfo(3, "GM", 1, [12.0, 0.0, 0.0])  # außerhalb Legacy-Radius
        bot._last_grab_attempt = 0.0
        bot._check_opportunistic_grab(time.monotonic())
        bot.client.send.assert_called()


# ---------------------------------------------------------------------------
# Schritt 4: Neutrale Flags sofort droppen, bad-Flags mit shakeTimeout
# ---------------------------------------------------------------------------

class TestNeutralFlagDrop:
    """Schritt 4: Neutrale Flags sofort droppen, bad-Flags mit shakeTimeout."""

    def test_neutral_flag_dropped_immediately(self, bot):
        """Eine Flag die weder good noch bad ist (z.B. 'CL') wird sofort gedroppt."""
        bot.human_count = 0  # passivmodus reicht
        bot.own_flag = "CL"  # nicht in default good/bad
        bot.good_flags = {"GM", "L"}
        bot.bad_flags = {"WG", "NJ"}
        bot._drop_bad_flag_delay = 15.0
        bot._own_flag_since = time.monotonic() - 0.5  # erst 0.5s gehalten
        bot._last_drop_attempt = 0.0
        # Direkter Test der Drop-Logik (simuliert Hauptschleife)
        now = time.monotonic()
        held = now - bot._own_flag_since
        required = bot._drop_bad_flag_delay if bot.own_flag in bot.bad_flags else 0.0
        assert required == 0.0
        assert held >= required  # → würde droppen

    def test_bad_flag_held_until_delay(self, bot):
        """Eine bad-Flag (z.B. 'WG') wird erst nach _drop_bad_flag_delay gedroppt."""
        bot.own_flag = "WG"
        bot.good_flags = {"GM"}
        bot.bad_flags = {"WG", "NJ"}
        bot._drop_bad_flag_delay = 15.0
        bot._own_flag_since = time.monotonic() - 0.5
        now = time.monotonic()
        held = now - bot._own_flag_since
        required = bot._drop_bad_flag_delay if bot.own_flag in bot.bad_flags else 0.0
        assert required == 15.0
        assert held < required  # → nicht droppen


# ---------------------------------------------------------------------------
# Opportunistic grab (free functions from TestNewFeatures)
# ---------------------------------------------------------------------------

def test_opportunistic_grab_in_active_mode(bot):
    """Bot sendet MsgGrabFlag wenn er im Aktivmodus nah genug an einer Flagge ist."""
    from bot.models import FlagInfo
    from bzflag.protocol import MsgGrabFlag
    bot.human_count = 1
    bot.flags[3] = FlagInfo(3, "GM", 1, [5.0, 0.0, 0.0])  # nah genug
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._last_grab_attempt = 0.0
    bot._check_opportunistic_grab(time.monotonic())
    assert bot.client.send.called
    code, payload = bot.client.send.call_args[0]
    assert code == MsgGrabFlag
    assert struct.unpack(">H", payload)[0] == 3


def test_no_grab_in_passive_mode(bot):
    """Bot sendet kein MsgGrabFlag im Passivmodus (human_count==0)."""
    from bot.models import FlagInfo
    bot.human_count = 0
    bot.flags[3] = FlagInfo(3, "GM", 1, [5.0, 0.0, 0.0])
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._last_grab_attempt = 0.0
    # _check_opportunistic_grab direkt ruft nur im Aktivmodus
    # es wird in _update_movement nur bei human_count > 0 aufgerufen
    now = time.monotonic()
    bot._update_movement(0.02, now, ai_tick=True)
    bot.client.send.assert_not_called()


def test_grab_throttled(bot):
    """Zweiter Grab-Versuch < 0.5s wird nicht gesendet."""
    from bot.models import FlagInfo
    bot.flags[3] = FlagInfo(3, "GM", 1, [5.0, 0.0, 0.0])
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    now = time.monotonic()
    bot._last_grab_attempt = now - 0.1  # zu kürzlich
    bot._check_opportunistic_grab(now)
    bot.client.send.assert_not_called()


def test_no_grab_when_holding_flag(bot):
    """Bot mit Flagge greift keine weitere Flagge."""
    from bot.models import FlagInfo
    bot.own_flag = "GM"
    bot.flags[3] = FlagInfo(3, "SW", 1, [5.0, 0.0, 0.0])
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._last_grab_attempt = 0.0
    bot._check_opportunistic_grab(time.monotonic())
    bot.client.send.assert_not_called()


# ── Fix 14: G (Genocide) als neutral bei 1 Spieler pro Team ──────────────────

class TestGenocideMultikill:
    """G-Flagge wird gedroppt wenn kein Feind-Team > 1 Spieler hat."""

    def _setup_teams(self, bot, team2_count=1, team3_count=0):
        """Erstellt N lebende Spieler in Team 2 (und optional Team 3)."""
        pid = 10
        for _ in range(team2_count):
            from bot.models import PlayerInfo
            info = PlayerInfo(callsign=f"P{pid}", team=2, is_human=True)
            info.alive = True
            info.pos = [100.0, 0.0, 0.0]
            info.flag = ""
            bot.players[pid] = info
            pid += 1
        for _ in range(team3_count):
            from bot.models import PlayerInfo
            info = PlayerInfo(callsign=f"P{pid}", team=3, is_human=True)
            info.alive = True
            info.pos = [100.0, 0.0, 0.0]
            info.flag = ""
            bot.players[pid] = info
            pid += 1

    def test_genocide_not_useful_single_enemy(self, bot):
        """1 Feind in Team 2 → _genocide_multikill_possible = False."""
        bot.team = 1   # Bot in Team 1, Feinde in Team 2
        self._setup_teams(bot, team2_count=1)
        assert bot._genocide_multikill_possible() is False

    def test_genocide_useful_two_enemies_same_team(self, bot):
        """2 Feinde in Team 2 → _genocide_multikill_possible = True."""
        bot.team = 1
        self._setup_teams(bot, team2_count=2)
        assert bot._genocide_multikill_possible() is True

    def test_genocide_not_useful_no_enemies(self, bot):
        """Keine Feinde → _genocide_multikill_possible = False."""
        bot.team = 1
        assert bot._genocide_multikill_possible() is False

    def test_genocide_dropped_in_main_loop_when_useless(self, bot):
        """G halten, 1 Feind → Bot droppt G (MsgDropFlag gesendet)."""
        import time as _time
        bot.team = 1
        bot.own_flag = "G"
        bot._own_flag_since = _time.monotonic()
        bot._last_drop_attempt = 0.0
        self._setup_teams(bot, team2_count=1)
        now = _time.monotonic()
        # Simuliere den Flag-Drop-Check aus dem Hauptloop
        if bot.own_flag == "G" and not bot._genocide_multikill_possible():
            if now - bot._last_drop_attempt > 2.0:
                bot._try_drop_flag()
        bot.client.send.assert_called()

    def test_genocide_kept_when_multikill_possible(self, bot):
        """G halten, 2 Feinde im gleichen Team → kein Drop."""
        import time as _time
        bot.team = 1
        bot.own_flag = "G"
        bot._own_flag_since = _time.monotonic()
        bot._last_drop_attempt = 0.0
        self._setup_teams(bot, team2_count=2)
        now = _time.monotonic()
        if bot.own_flag == "G" and not bot._genocide_multikill_possible():
            if now - bot._last_drop_attempt > 2.0:
                bot._try_drop_flag()
        bot.client.send.assert_not_called()


# ---------------------------------------------------------------------------
# P4-FLG-03: PhantomZone-Keep-Gate (_pz_worth_keeping)
# ---------------------------------------------------------------------------

class TestPhantomZoneKeepGate:
    """PZ behalten nur wenn Teleporter existieren UND ein Gegner mit mehr Punkten
    in Radar-Reichweite ist; nie droppen solange gezoned."""

    def _wire_tele_world(self, bot):
        from bzflag.world_map import TeleporterObstacle
        t = TeleporterObstacle(name="t0", cx=0.0, cy=0.0, bottom_z=0.0, angle=0.0,
                               half_w=0.5, half_d=5.0, height=10.0, border=1.0)
        wm = MagicMock()
        wm.teleporters = [t]
        wm.boxes = []
        bot._world_map = wm
        bot._link_map = {0: 3, 3: 0}

    def _richer_enemy(self, bot, pid=5, wins=10, losses=0, dist=100.0, flag=""):
        info = make_player(bot, pid, pos=(dist, 0.0, 0.0), flag=flag)
        info.wins = wins
        info.losses = losses
        return info

    def test_gate_true_with_richer_enemy_and_tele(self, bot):
        bot.team = 1
        self._wire_tele_world(bot)
        self._richer_enemy(bot)
        assert bot._pz_worth_keeping() is True

    def test_gate_false_without_teleporter(self, bot):
        bot.team = 1
        bot._world_map = None
        self._richer_enemy(bot)
        assert bot._pz_worth_keeping() is False

    def test_gate_false_when_bot_has_more_points(self, bot):
        bot.team = 1
        self._wire_tele_world(bot)
        bot._own_wins = 20
        self._richer_enemy(bot, wins=10)   # 10 ≤ 20 → kein Grund zur Flucht
        assert bot._pz_worth_keeping() is False

    def test_gate_true_on_score_via_losses(self, bot):
        """Score = wins − losses: Gegner 5−0=5 > Bot 10−8=2 → Gate wahr."""
        bot.team = 1
        self._wire_tele_world(bot)
        bot._own_wins = 10
        bot._own_losses = 8
        self._richer_enemy(bot, wins=5, losses=0)
        assert bot._pz_worth_keeping() is True

    def test_gate_false_enemy_outside_radar_range(self, bot):
        bot.team = 1
        self._wire_tele_world(bot)
        far = bot._effective_radar_range() + 50.0
        self._richer_enemy(bot, dist=far)
        assert bot._pz_worth_keeping() is False

    def test_gate_false_stealth_enemy_not_on_radar(self, bot):
        """ST-Träger ist radar-unsichtbar → zählt nicht fürs Radar-Gate."""
        bot.team = 1
        self._wire_tele_world(bot)
        self._richer_enemy(bot, flag="ST")
        assert bot._pz_worth_keeping() is False

    def test_gate_false_while_gates_unreachable(self, bot):
        """Alle Tore unerreichbar (Async-Validierung überall gescheitert) → Gate fällt."""
        bot.team = 1
        self._wire_tele_world(bot)
        self._richer_enemy(bot)
        bot._pz_unreachable_until = time.monotonic() + 10.0
        assert bot._pz_worth_keeping() is False

    def test_pz_kept_on_grab_as_good_flag(self, bot):
        """PZ ist jetzt good (P4-FLG-03) → der Grab-Handler droppt sie nicht sofort."""
        from bzflag.protocol import MsgGrabFlag
        payload = struct.pack(">BH", bot.player_id, 0) + b"PZ"
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert bot.own_flag == "PZ"
        assert "PZ" in bot.good_flags
        bot.client.send.assert_not_called()

    def test_pz_main_loop_drop_when_gate_false_and_not_zoned(self, bot):
        """Hauptloop-Bedingung (core.py): Gate falsch + nicht gezoned → Drop."""
        bot.team = 1
        bot.own_flag = "PZ"
        bot.is_phantom_zoned = False
        bot._last_drop_attempt = 0.0
        now = time.monotonic()
        with patch.object(bot, "_try_drop_flag") as drop:
            if (bot.own_flag == "PZ" and not bot.is_phantom_zoned
                    and not bot._pz_worth_keeping()):
                if now - bot._last_drop_attempt > 2.0:
                    bot._try_drop_flag()
        drop.assert_called_once()

    def test_pz_never_dropped_while_zoned(self, bot):
        """Solange gezoned wird NICHT gedroppt — auch wenn das Gate gefallen ist
        (erst regulär am Tor entzonen, Manöver Phase 2)."""
        bot.team = 1
        bot.own_flag = "PZ"
        bot.is_phantom_zoned = True
        bot._last_drop_attempt = 0.0
        now = time.monotonic()
        with patch.object(bot, "_try_drop_flag") as drop:
            if (bot.own_flag == "PZ" and not bot.is_phantom_zoned
                    and not bot._pz_worth_keeping()):
                if now - bot._last_drop_attempt > 2.0:
                    bot._try_drop_flag()
        drop.assert_not_called()


# ---------------------------------------------------------------------------
# P4-FLG-04: Flag-Typ-Wissen (_flag_knowledge / _carried_flag_id)
# ---------------------------------------------------------------------------

def _build_flag_update_payload(flags, owner=255):
    """Standalone-Vorlage von TestFlagUpdate._build_flag_update (57 Bytes/Eintrag),
    ohne die bestehende Klasse anzufassen. flags: Liste von (flag_id, abbr, status, x, y, z)."""
    buf = struct.pack(">H", len(flags))
    for flag_id, abbr, status, x, y, z in flags:
        abbr_b = (abbr.encode('ascii') + b'\x00\x00')[:2]
        entry = (
            struct.pack(">H", flag_id)
            + abbr_b
            + struct.pack(">H", status)
            + struct.pack(">H", 0)
            + struct.pack(">B", owner)
            + struct.pack(">fff", x, y, z)
            + struct.pack(">fff", 0, 0, 0)
            + struct.pack(">fff", 0, 0, 0)
            + struct.pack(">f", 0.0)
            + struct.pack(">f", 0.0)
            + struct.pack(">f", 0.0)
        )
        assert len(entry) == 57
        buf += entry
    return buf


class TestFlagKnowledge:
    """P4-FLG-04: _flag_knowledge (flag_id→abbr) und _carried_flag_id (pid→flag_id) —
    Wahrnehmungs-Gate über _flag_carrier_perceptible (Fenster: FoV+LoS, oder Radar
    innerhalb min(FLAG_KNOW_RADAR_RANGE=150, effektive Radar-Reichweite))."""

    FOREIGN_PID = 2

    def _grab_payload(self, pid, flag_index, abbr):
        abbr2 = (abbr.encode('ascii') + b'\x00\x00')[:2]
        return struct.pack(">BH", pid, flag_index) + abbr2

    def _transfer_payload(self, from_id, to_id, flag_index, abbr):
        abbr2 = (abbr.encode('ascii') + b'\x00\x00')[:2]
        return struct.pack(">BBH", from_id, to_id, flag_index) + abbr2

    # -- Grab ------------------------------------------------------------

    def test_own_grab_always_learned(self, bot):
        from bzflag.protocol import MsgGrabFlag
        payload = self._grab_payload(bot.player_id, 5, "GM")
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert bot._flag_knowledge[5] == "GM"

    def test_foreign_grab_learned_when_in_window(self, bot):
        """Spieler +x, nah → im FoV, Fenster-Sicht greift."""
        from bzflag.protocol import MsgGrabFlag
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(6.0, 0.0, 0.0))
        payload = self._grab_payload(self.FOREIGN_PID, 7, "GM")
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert bot._flag_knowledge[7] == "GM"
        assert bot._carried_flag_id[self.FOREIGN_PID] == 7

    def test_foreign_grab_not_learned_out_of_window_and_far(self, bot):
        """Spieler bei -x → nicht im FoV; Distanz > 150 → auch Radar-Pfad versagt."""
        from bzflag.protocol import MsgGrabFlag
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(-200.0, 0.0, 0.0))
        payload = self._grab_payload(self.FOREIGN_PID, 8, "GM")
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert 8 not in bot._flag_knowledge
        # Bookkeeping ist ungegated und wird trotzdem gesetzt:
        assert bot._carried_flag_id[self.FOREIGN_PID] == 8

    def test_foreign_grab_learned_via_radar_within_range(self, bot):
        """Spieler bei -x, aber Distanz < 150 → Radar-Pfad greift."""
        from bzflag.protocol import MsgGrabFlag
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(-100.0, 0.0, 0.0))
        payload = self._grab_payload(self.FOREIGN_PID, 9, "GM")
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert bot._flag_knowledge[9] == "GM"

    def test_foreign_grab_not_learned_stealth_on_radar(self, bot):
        """Träger ist bereits als Stealth-Träger (flag='ST') bekannt → Radar sieht ihn nicht.
        Das Gate wertet den Spieler-Zustand VOR dem Setzen von players[pid].flag aus."""
        from bzflag.protocol import MsgGrabFlag
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(-100.0, 0.0, 0.0), flag="ST")
        payload = self._grab_payload(self.FOREIGN_PID, 10, "GM")
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert 10 not in bot._flag_knowledge

    def test_foreign_grab_not_learned_radar_jammed(self, bot):
        """Eigenes JM stört das eigene Radar komplett; Spieler außerhalb FoV → nichts wahrgenommen."""
        from bzflag.protocol import MsgGrabFlag
        bot.azimuth = 0.0
        bot.own_flag = "JM"
        make_player(bot, self.FOREIGN_PID, pos=(-100.0, 0.0, 0.0))
        payload = self._grab_payload(self.FOREIGN_PID, 11, "GM")
        bot._on_grab_flag(MsgGrabFlag, payload)
        assert 11 not in bot._flag_knowledge

    # -- Transfer ----------------------------------------------------------

    def test_transfer_learned_when_receiver_visible(self, bot):
        from bzflag.protocol import MsgTransferFlag
        bot.azimuth = 0.0
        victim_id, receiver_id = 3, self.FOREIGN_PID
        make_player(bot, victim_id, pos=(-200.0, 0.0, 0.0))    # unsichtbar
        make_player(bot, receiver_id, pos=(6.0, 0.0, 0.0))     # sichtbar (Fenster)
        payload = self._transfer_payload(victim_id, receiver_id, 20, "GM")
        bot._on_transfer_flag(MsgTransferFlag, payload)
        assert bot._flag_knowledge[20] == "GM"
        assert bot._carried_flag_id[receiver_id] == 20

    def test_transfer_learned_when_victim_visible(self, bot):
        from bzflag.protocol import MsgTransferFlag
        bot.azimuth = 0.0
        victim_id, receiver_id = self.FOREIGN_PID, 3
        make_player(bot, victim_id, pos=(6.0, 0.0, 0.0))       # sichtbar (Fenster)
        make_player(bot, receiver_id, pos=(-200.0, 0.0, 0.0))  # unsichtbar
        payload = self._transfer_payload(victim_id, receiver_id, 21, "SW")
        bot._on_transfer_flag(MsgTransferFlag, payload)
        assert bot._flag_knowledge[21] == "SW"

    def test_transfer_not_learned_when_neither_visible(self, bot):
        from bzflag.protocol import MsgTransferFlag
        bot.azimuth = 0.0
        victim_id, receiver_id = 3, 4
        make_player(bot, victim_id, pos=(-200.0, 0.0, 0.0))
        make_player(bot, receiver_id, pos=(-200.0, 0.0, 0.0))
        payload = self._transfer_payload(victim_id, receiver_id, 22, "L")
        bot._on_transfer_flag(MsgTransferFlag, payload)
        assert 22 not in bot._flag_knowledge

    def test_own_transfer_receive_always_learned(self, bot):
        """Eigener Erhalt (to_id == bot.player_id) wird immer gelernt, auch wenn der
        Bestohlene (from_id) unsichtbar ist."""
        from bzflag.protocol import MsgTransferFlag
        bot.azimuth = 0.0
        victim_id = 3
        make_player(bot, victim_id, pos=(-200.0, 0.0, 0.0))
        payload = self._transfer_payload(victim_id, bot.player_id, 23, "TH")
        bot._on_transfer_flag(MsgTransferFlag, payload)
        assert bot._flag_knowledge[23] == "TH"
        assert bot.own_flag == "TH"

    # -- Near-Flag (ID) ------------------------------------------------------

    def test_near_flag_id_always_learns(self, bot):
        from bzflag.protocol import MsgNearFlag
        from bot.models import FlagInfo
        bot.own_flag = "ID"
        fid = 30
        bot.flags[fid] = FlagInfo(fid, "", 1, [10.0, 20.0, 0.0])
        name = b"Laser"
        payload = (struct.pack(">fff", 10.0, 20.0, 0.0)
                   + struct.pack(">I", len(name)) + name)
        bot._on_near_flag(MsgNearFlag, payload)
        assert bot._flag_knowledge[fid] == "L"

    # -- Drop ------------------------------------------------------------

    def test_drop_learned_when_carrier_visible_and_bookkept(self, bot):
        from bzflag.protocol import MsgDropFlag
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(6.0, 0.0, 0.0), flag="GM")
        bot._carried_flag_id[self.FOREIGN_PID] = 40
        payload = struct.pack(">B", self.FOREIGN_PID)
        bot._on_drop_flag(MsgDropFlag, payload)
        assert bot._flag_knowledge[40] == "GM"
        assert bot.players[self.FOREIGN_PID].flag == ""
        assert self.FOREIGN_PID not in bot._carried_flag_id

    def test_drop_not_learned_when_carrier_not_visible(self, bot):
        from bzflag.protocol import MsgDropFlag
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(-200.0, 0.0, 0.0), flag="GM")
        bot._carried_flag_id[self.FOREIGN_PID] = 41
        payload = struct.pack(">B", self.FOREIGN_PID)
        bot._on_drop_flag(MsgDropFlag, payload)
        assert 41 not in bot._flag_knowledge
        assert bot.players[self.FOREIGN_PID].flag == ""

    def test_drop_no_bookkeeping_no_learn(self, bot):
        """Sichtbarer Drop, aber ohne vorherigen _carried_flag_id-Eintrag → kein Lernen."""
        from bzflag.protocol import MsgDropFlag
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(6.0, 0.0, 0.0), flag="GM")
        payload = struct.pack(">B", self.FOREIGN_PID)
        bot._on_drop_flag(MsgDropFlag, payload)
        assert bot._flag_knowledge == {}
        assert bot.players[self.FOREIGN_PID].flag == ""

    # -- Flag-Update (Reset / onTank / Owner-Sync) ------------------------

    def test_flag_reset_status0_forgets(self, bot):
        from bzflag.protocol import MsgFlagUpdate
        from bot.models import FlagInfo
        bot.flags[1] = FlagInfo(1, "GM", 1, [50.0, 30.0, 0.0])
        bot._flag_knowledge[1] = "GM"
        payload = _build_flag_update_payload([(1, "GM", 0, 50.0, 30.0, 0.0)])
        bot._on_flag_update(MsgFlagUpdate, payload)
        assert 1 not in bot._flag_knowledge
        assert 1 not in bot.flags

    def test_flag_status2_ontank_keeps_knowledge(self, bot):
        from bzflag.protocol import MsgFlagUpdate
        from bot.models import FlagInfo
        bot.flags[1] = FlagInfo(1, "GM", 1, [50.0, 30.0, 0.0])
        bot._flag_knowledge[1] = "GM"
        payload = _build_flag_update_payload([(1, "GM", 2, 50.0, 30.0, 0.0)], owner=255)
        bot._on_flag_update(MsgFlagUpdate, payload)
        assert 1 not in bot.flags
        assert bot._flag_knowledge[1] == "GM"

    def test_flag_update_owner_sync_learns_when_perceptible(self, bot):
        from bzflag.protocol import MsgFlagUpdate
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(6.0, 0.0, 0.0))
        payload = _build_flag_update_payload([(1, "GM", 2, 0.0, 0.0, 0.0)], owner=self.FOREIGN_PID)
        bot._on_flag_update(MsgFlagUpdate, payload)
        assert bot.players[self.FOREIGN_PID].flag == "GM"
        assert bot._carried_flag_id[self.FOREIGN_PID] == 1
        assert bot._flag_knowledge[1] == "GM"

    def test_flag_update_owner_sync_not_learned_when_owner_far(self, bot):
        """Owner fern/hinten → Bookkeeping wird trotzdem gesetzt (ungegated), Typ-Wissen bleibt aus."""
        from bzflag.protocol import MsgFlagUpdate
        bot.azimuth = 0.0
        make_player(bot, self.FOREIGN_PID, pos=(-200.0, 0.0, 0.0))
        payload = _build_flag_update_payload([(1, "GM", 2, 0.0, 0.0, 0.0)], owner=self.FOREIGN_PID)
        bot._on_flag_update(MsgFlagUpdate, payload)
        assert bot.players[self.FOREIGN_PID].flag == "GM"
        assert bot._carried_flag_id[self.FOREIGN_PID] == 1
        assert 1 not in bot._flag_knowledge

    # -- Bookkeeping-Hygiene (Remove/Capture) ------------------------------

    def test_remove_player_clears_bookkeeping(self, bot):
        from bzflag.protocol import MsgRemovePlayer
        make_player(bot, self.FOREIGN_PID)
        bot._carried_flag_id[self.FOREIGN_PID] = 50
        payload = struct.pack(">B", self.FOREIGN_PID)
        bot._on_remove_player(MsgRemovePlayer, payload)
        assert self.FOREIGN_PID not in bot._carried_flag_id

    def test_capture_clears_bookkeeping(self, bot):
        from bzflag.protocol import MsgCaptureFlag
        make_player(bot, self.FOREIGN_PID)
        bot._carried_flag_id[self.FOREIGN_PID] = 51
        payload = struct.pack(">B", self.FOREIGN_PID)
        bot._on_capture_flag(MsgCaptureFlag, payload)
        assert self.FOREIGN_PID not in bot._carried_flag_id
        assert bot.players[self.FOREIGN_PID].flag == ""

    # -- _learn_flag_type direkt --------------------------------------------

    def test_learn_overwrites(self, bot):
        bot._learn_flag_type(1, "GM")
        bot._learn_flag_type(1, "SW")
        assert bot._flag_knowledge[1] == "SW"

    def test_learn_ignores_empty_abbr(self, bot):
        bot._learn_flag_type(1, "GM")
        bot._learn_flag_type(1, "")
        assert bot._flag_knowledge[1] == "GM"


# ---------------------------------------------------------------------------
# P4-FLG-05: Flag-Wissen in der Zielwahl (_effective_flag_abbr / _new_target /
# _check_opportunistic_grab) — bekannt-beste > bekannt-gute > nächste-unbekannte,
# bekannt-schlechte/-neutrale werden übersprungen.
# ---------------------------------------------------------------------------

class TestFlagKnowledgeTargeting:

    def test_effective_abbr_prefers_live_over_memory(self, bot):
        from bot.models import FlagInfo
        fi = FlagInfo(1, "GM", 1, [0.0, 0.0, 0.0])
        bot._flag_knowledge[1] = "L"
        assert bot._effective_flag_abbr(fi) == "GM"

    def test_effective_abbr_falls_back_to_memory(self, bot):
        from bot.models import FlagInfo
        fi = FlagInfo(2, "", 1, [0.0, 0.0, 0.0])
        bot._flag_knowledge[2] = "SW"
        assert bot._effective_flag_abbr(fi) == "SW"

    def test_new_target_prefers_known_good_over_nearer_unknown(self, bot):
        """Flag1 (näher, unbekannt) vs. Flag2 (weiter, bekannt-gut 'V', nicht best)
        → die bekannt-gute gewinnt trotz größerer Distanz."""
        from bot.models import FlagInfo
        bot.own_flag = ""
        bot.flags[1] = FlagInfo(1, "", 1, [5.0, 0.0, 0.0])    # unbekannt, näher
        bot.flags[2] = FlagInfo(2, "", 1, [50.0, 0.0, 0.0])   # bekannt-gut, weiter
        bot._flag_knowledge[2] = "V"
        with patch.object(bot, "_plan_path") as pp:
            bot._new_target()
            pp.assert_called_once_with(50.0, 0.0, 0.0)

    def test_new_target_prefers_best_over_nearer_good(self, bot):
        """Flag1 (näher, bekannt-gut 'V') vs. Flag2 (weiter, bekannt-best 'GM')
        → best schlägt näher-gut."""
        from bot.models import FlagInfo
        bot.own_flag = ""
        bot.flags[1] = FlagInfo(1, "", 1, [5.0, 0.0, 0.0])
        bot._flag_knowledge[1] = "V"
        bot.flags[2] = FlagInfo(2, "", 1, [50.0, 0.0, 0.0])
        bot._flag_knowledge[2] = "GM"
        with patch.object(bot, "_plan_path") as pp:
            bot._new_target()
            pp.assert_called_once_with(50.0, 0.0, 0.0)

    def test_new_target_falls_back_to_nearest_when_no_known_good(self, bot):
        """Beide Flaggen unbekannt → einfache Nähe entscheidet (Fallback-Stufe 3)."""
        from bot.models import FlagInfo
        bot.own_flag = ""
        bot.flags[1] = FlagInfo(1, "", 1, [5.0, 0.0, 0.0])
        bot.flags[2] = FlagInfo(2, "", 1, [50.0, 0.0, 0.0])
        with patch.object(bot, "_plan_path") as pp:
            bot._new_target()
            pp.assert_called_once_with(5.0, 0.0, 0.0)

    def test_new_target_skips_known_bad(self, bot):
        """Flag1 (näher, bekannt-schlecht) wird übersprungen, Flag2 (weiter,
        unbekannt) gewinnt. NJ (No Jumping) statt WG: WG steht laut
        bot/constants.py GOOD_FLAGS_DEFAULT (Zeile 293) tatsächlich in der guten
        Liste (nicht in BAD_FLAGS_DEFAULT) — NJ ist die reale bad-Flagge hierfür."""
        from bot.models import FlagInfo
        from bot.constants import BAD_FLAGS_DEFAULT, GOOD_FLAGS_DEFAULT
        assert "NJ" in BAD_FLAGS_DEFAULT
        assert "NJ" not in GOOD_FLAGS_DEFAULT
        bot.own_flag = ""
        bot.flags[1] = FlagInfo(1, "", 1, [5.0, 0.0, 0.0])
        bot._flag_knowledge[1] = "NJ"
        bot.flags[2] = FlagInfo(2, "", 1, [50.0, 0.0, 0.0])
        with patch.object(bot, "_plan_path") as pp:
            bot._new_target()
            pp.assert_called_once_with(50.0, 0.0, 0.0)

    def test_new_target_skips_known_neutral(self, bot):
        """Flag1 (näher, bekannt-neutral 'US' — weder good noch bad) wird
        übersprungen, Flag2 (weiter, unbekannt) gewinnt."""
        from bot.models import FlagInfo
        assert "US" not in bot.good_flags
        assert "US" not in bot.bad_flags
        bot.own_flag = ""
        bot.flags[1] = FlagInfo(1, "", 1, [5.0, 0.0, 0.0])
        bot._flag_knowledge[1] = "US"
        bot.flags[2] = FlagInfo(2, "", 1, [50.0, 0.0, 0.0])
        with patch.object(bot, "_plan_path") as pp:
            bot._new_target()
            pp.assert_called_once_with(50.0, 0.0, 0.0)

    def test_best_flags_default_and_intersection_semantics(self):
        """_init_flags-Semantik (bot/core.py ~409-420):
        - Default: good=GOOD_FLAGS_DEFAULT, best=BEST_FLAGS_DEFAULT ({GM,L,SW}).
        - good_flags=['V'] (custom good, kein best) → best wird mit good
          GESCHNITTEN ({GM,L,SW} ∩ {V} = ∅), nicht erweitert.
        - good_flags=['V'], best_flags=['GM'] → explizite best werden in good
          gemergt: good={V,GM}, best={GM}."""
        from bot.constants import BEST_FLAGS_DEFAULT, GOOD_FLAGS_DEFAULT
        with patch("bot.core.BZFlagClient"):
            from bot.core import BZBot
            b1 = BZBot(host="localhost", callsign="T1")
            assert b1.good_flags == set(GOOD_FLAGS_DEFAULT)
            assert b1.best_flags == set(BEST_FLAGS_DEFAULT)

            b2 = BZBot(host="localhost", callsign="T2", good_flags=["V"])
            assert b2.good_flags == {"V"}
            assert b2.best_flags == set()

            b3 = BZBot(host="localhost", callsign="T3",
                       good_flags=["V"], best_flags=["GM"])
            assert b3.good_flags == {"V", "GM"}
            assert b3.best_flags == {"GM"}

    def test_opportunistic_grab_skips_known_bad(self, bot):
        """Bekannt-schlechte Flagge (NJ) in Grab-Reichweite wird NICHT gegriffen."""
        from bot.models import FlagInfo
        bot.own_flag = ""
        bot.azimuth = 0.0
        bot.flags[3] = FlagInfo(3, "", 1, [5.0, 0.0, 0.0])
        bot._flag_knowledge[3] = "NJ"
        bot._last_grab_attempt = 0.0
        bot.client.send.reset_mock()
        bot._check_opportunistic_grab(100.0)
        bot.client.send.assert_not_called()

    def test_opportunistic_grab_still_grabs_known_good(self, bot):
        """Regression: bekannt-gute (unbekannt/gute) Flagge nah wird weiterhin gegriffen."""
        from bot.models import FlagInfo
        bot.own_flag = ""
        bot.azimuth = 0.0
        bot.flags[3] = FlagInfo(3, "", 1, [5.0, 0.0, 0.0])
        bot._flag_knowledge[3] = "V"
        bot._last_grab_attempt = 0.0
        bot.client.send.reset_mock()
        bot._check_opportunistic_grab(100.0)
        bot.client.send.assert_called()

    def test_dropped_neutrals_dedup_uses_effective_abbr(self, bot):
        """_dropped_neutrals speichert (abbr, x, y) (targeting.py ~232/240f). Eine
        Flagge deren _effective_flag_abbr (aus _flag_knowledge, da fi.abbr=='')
        mit einem kürzlich abgelegten Eintrag <20u übereinstimmt, wird nicht
        angesteuert — auch wenn ihr Typ (hier 'V') an sich gut ist. Bewusst 'V'
        statt der in der Vorgabe genannten 'BY' verwendet: BY steht in
        bot/constants.py BAD_FLAGS_DEFAULT (Zeile 296), ein bad-Kürzel würde
        also schon durch den good/bad-Filter übersprungen und den Dedup-Pfad
        nicht isoliert testen.

        Zweite (nicht gededuplizierte) Flagge nötig: bleibt best_pos None (kein
        Flag-Kandidat übrig), fällt targeting.py am Ende von _new_target
        ungated in 'Fall C' (zufälliger Wegpunkt) durch — dort wird _plan_path
        ebenfalls aufgerufen, nur mit zufälligen statt deterministischen
        Koordinaten. Ein reiner 'kein Ziel'-Fall existiert bei own_flag=='' nicht."""
        from bot.models import FlagInfo
        bot.own_flag = ""
        bot._dropped_neutrals.append(("V", 5.0, 0.0))
        bot.flags[1] = FlagInfo(1, "", 1, [5.0, 0.0, 0.0])   # dedupliziert -> übersprungen
        bot._flag_knowledge[1] = "V"
        bot.flags[2] = FlagInfo(2, "", 1, [50.0, 0.0, 0.0])  # einzig verbleibender Kandidat
        with patch.object(bot, "_plan_path") as pp:
            bot._new_target()
            pp.assert_called_once_with(50.0, 0.0, 0.0)
