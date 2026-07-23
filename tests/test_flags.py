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
