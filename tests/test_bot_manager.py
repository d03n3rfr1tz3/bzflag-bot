"""
Tests für Bot-Lifetime-Rotation im BotManager.
"""
import json
import sys
import os
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Config: neue Lifetime-Felder
# ---------------------------------------------------------------------------

class TestConfigLifetimeDefaults:

    def test_lifetime_defaults(self):
        from bot_manager import Config
        cfg = Config()
        assert cfg.bot_lifetime_min == pytest.approx(900.0)
        assert cfg.bot_lifetime_max == pytest.approx(7200.0)

    def test_lifetime_yaml_override(self, tmp_path):
        yaml = pytest.importorskip("yaml", reason="PyYAML nicht installiert")
        from bot_manager import Config
        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text("bot_lifetime_min: 300\nbot_lifetime_max: 1800\n")
        cfg = Config.from_yaml(str(cfg_file))
        assert cfg.bot_lifetime_min == pytest.approx(300.0)
        assert cfg.bot_lifetime_max == pytest.approx(1800.0)


# ---------------------------------------------------------------------------
# BotProcess: lifetime-Attribut
# ---------------------------------------------------------------------------

class TestBotProcessLifetime:

    def test_botprocess_has_lifetime_attribute(self):
        from bot_manager import BotProcess, Config
        bp = BotProcess(callsign="TestBot", config=Config())
        assert hasattr(bp, "lifetime")
        assert bp.lifetime == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# BotManager: _start_bot setzt Lifetime
# ---------------------------------------------------------------------------

class TestStartBotSetsLifetime:

    def _make_manager(self):
        from bot_manager import BotManager, Config
        cfg = Config()
        cfg.bot_name_prefix = ""          # In-Game-Name == Basisname (Test ohne Prefix)
        cfg.bot_callsigns = ["Alpha", "Beta", "Gamma"]
        cfg.bot_lifetime_min = 60.0
        cfg.bot_lifetime_max = 120.0
        mgr = BotManager(cfg)
        return mgr

    def test_start_bot_assigns_lifetime(self):
        mgr = self._make_manager()
        from bot_manager import BotProcess
        # Echtes __init__ (spawnt nichts; process bleibt None → is_alive False),
        # nur start() mocken.
        with patch.object(BotProcess, "start", return_value=True):
            mgr._start_bot()
        # Lifetime wird zwischen min/max liegen (Zufallswert)
        started = [b for b in mgr.bots]
        assert started
        assert 60.0 <= started[0].lifetime <= 120.0

    def test_exclude_name_avoids_same_callsign(self):
        """Nach Rotation: exclude_name verhindert Wiederverwendung desselben Namens."""
        mgr = self._make_manager()
        # Simuliere: Beta ist aktiv
        mock_alive = MagicMock()
        mock_alive.is_alive = True
        mock_alive.callsign = "Beta"  # Beta bereits aktiv
        mgr.bots = [mock_alive]

        from bot_manager import BotProcess
        with patch.object(BotProcess, "start", return_value=True):
            mgr._start_bot(exclude_name="Alpha")

        new_bots = [b for b in mgr.bots if b is not mock_alive]
        assert new_bots
        # Neuer Bot darf nicht "Alpha" heißen (war excluded) und nicht "Beta" (aktiv)
        assert new_bots[0].callsign not in ("Alpha", "Beta")


# ---------------------------------------------------------------------------
# BotManager: _rotate_expired_bots
# ---------------------------------------------------------------------------

class TestRotateExpiredBots:

    def test_expired_bot_is_replaced(self):
        from bot_manager import BotManager, Config, BotProcess
        cfg = Config()
        cfg.bot_callsigns = ["Alpha", "Beta"]
        cfg.bot_lifetime_min = 60.0
        cfg.bot_lifetime_max = 120.0
        mgr = BotManager(cfg)

        # Bot mit abgelaufener Lebensdauer simulieren
        expired = MagicMock(spec=BotProcess)
        expired.is_alive = True
        expired.start_time = time.monotonic() - 200.0  # 200s alt
        expired.lifetime = 60.0                        # Lifetime 60s → abgelaufen
        expired.callsign = "Alpha"
        mgr.bots = [expired]

        with patch.object(mgr, "_start_bot") as mock_start:
            mgr._rotate_expired_bots()

        expired.stop.assert_called_once()
        mock_start.assert_called_once_with(exclude_name="Alpha")
        assert expired not in mgr.bots

    def test_active_bot_not_rotated(self):
        from bot_manager import BotManager, Config, BotProcess
        cfg = Config()
        cfg.bot_lifetime_min = 3600.0
        cfg.bot_lifetime_max = 7200.0
        mgr = BotManager(cfg)

        fresh = MagicMock(spec=BotProcess)
        fresh.is_alive = True
        fresh.start_time = time.monotonic() - 30.0  # erst 30s alt
        fresh.lifetime = 3600.0                     # Lifetime 1h → nicht abgelaufen
        fresh.callsign = "Beta"
        mgr.bots = [fresh]

        with patch.object(mgr, "_start_bot") as mock_start:
            mgr._rotate_expired_bots()

        fresh.stop.assert_not_called()
        mock_start.assert_not_called()
        assert fresh in mgr.bots


class TestRotateExpiredBotsLockNotHeldDuringStop:
    """R3: _rotate_expired_bots darf self._lock nicht über den blockierenden
    bot.stop()-Aufruf halten, sonst blockieren Status-/Exit-Callbacks anderer Bots
    (_on_bot_status/_on_bot_exit, nehmen dasselbe Lock) für die Rotationsdauer."""

    def test_lock_free_during_blocking_stop(self):
        import threading
        from bot_manager import BotManager, Config, BotProcess

        cfg = Config()
        cfg.bot_callsigns = ["Alpha", "Beta"]
        cfg.bot_lifetime_min = 60.0
        cfg.bot_lifetime_max = 120.0
        mgr = BotManager(cfg)

        expired = MagicMock(spec=BotProcess)
        expired.is_alive = True
        expired.start_time = time.monotonic() - 200.0  # 200s alt
        expired.lifetime = 60.0                         # Lifetime 60s → abgelaufen
        expired.callsign = "Alpha"
        mgr.bots = [expired]

        started_stop = threading.Event()
        release_stop = threading.Event()

        def _blocking_stop(*args, **kwargs):
            started_stop.set()
            release_stop.wait(timeout=5.0)

        expired.stop.side_effect = _blocking_stop

        with patch.object(mgr, "_start_bot"):
            t = threading.Thread(target=mgr._rotate_expired_bots)
            t.start()
            try:
                assert started_stop.wait(timeout=2.0), "bot.stop() wurde nicht aufgerufen"
                # Während stop() blockiert, darf self._lock NICHT gehalten sein
                acquired = mgr._lock.acquire(timeout=0.5)
                assert acquired, "self._lock war während des blockierenden bot.stop() gehalten"
                mgr._lock.release()
            finally:
                release_stop.set()
                t.join(timeout=5.0)
            assert not t.is_alive()

    def test_rebalance_serialized_against_rotation(self):
        """R3-Gegenprobe: _rebalance_lock serialisiert Management-Operationen. Läuft
        eine Rotation (blockiert in stop()), darf ein paralleles _rebalance (z. B. vom
        Log-Thread via _on_bot_status) NICHT gleichzeitig das Defizit sehen und selbst
        starten — sonst Bot-Überschuss und Join/Leave-Churn auf dem Server."""
        import threading
        from bot_manager import BotManager, Config, BotProcess

        cfg = Config()
        cfg.bot_callsigns = ["Alpha", "Beta"]
        mgr = BotManager(cfg)

        expired = MagicMock(spec=BotProcess)
        expired.is_alive = True
        expired.start_time = time.monotonic() - 200.0
        expired.lifetime = 60.0
        expired.callsign = "Alpha"
        mgr.bots = [expired]

        started_stop = threading.Event()
        release_stop = threading.Event()

        def _blocking_stop(*args, **kwargs):
            started_stop.set()
            release_stop.wait(timeout=5.0)

        expired.stop.side_effect = _blocking_stop

        with patch.object(mgr, "_start_bot") as start_bot, \
             patch.object(mgr, "_desired_bot_count", return_value=1):
            rot = threading.Thread(target=mgr._rotate_expired_bots)
            rot.start()
            try:
                assert started_stop.wait(timeout=2.0)
                # Paralleles _rebalance sieht active=0 (Bot bereits aus self.bots
                # entfernt) und würde ohne Serialisierung sofort starten.
                reb = threading.Thread(target=mgr._rebalance)
                reb.start()
                time.sleep(0.3)
                assert start_bot.call_count == 0, \
                    "_rebalance hat während laufender Rotation gestartet (Überschuss)"
            finally:
                release_stop.set()
                rot.join(timeout=5.0)
            reb.join(timeout=5.0)
            assert not rot.is_alive() and not reb.is_alive()
            # Nach der Serialisierung starten beide: Rotation ersetzt den rotierten
            # Bot, das nachfolgende _rebalance füllt auf desired=1 auf (start_bot ist
            # gemockt → self.bots bleibt leer → auch _rebalance sieht ein Defizit).
            assert start_bot.call_count == 2


# ---------------------------------------------------------------------------
# BotProcess._log_output: MsgReject-Grund auf WARNING heben
# ---------------------------------------------------------------------------

class _FakeStdin:
    """Minimaler Ersatz für Popen.stdin: sammelt geschriebene Zeilen."""
    def __init__(self):
        self.lines = []
        self.closed = False

    def write(self, s):
        if self.closed:
            raise ValueError("I/O operation on closed file")
        self.lines.append(s)

    def flush(self):
        pass


class _FakeProc:
    """Minimaler Ersatz für subprocess.Popen mit stdout-Iterator, stdin und poll()."""
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.stdin = _FakeStdin()
        self.returncode = returncode

    def poll(self):
        return self.returncode


class TestLogOutputRejectVisible:
    """_log_output: Bot-Problemzeilen (WARNING/ERROR/CRITICAL) → manager-WARNING,
    Bot-INFO → manager-INFO (sichtbar), unmarkierte/DEBUG-Zeilen → DEBUG;
    unerwarteter Exit (Code≠0) → WARNING-Dump der letzten Zeilen."""

    def _run(self, caplog, lines, returncode=0, stopping=False):
        import logging
        from bot_manager import BotProcess, Config
        bp = BotProcess(callsign="RejTest", config=Config())
        bp.process = _FakeProc(lines, returncode=returncode)
        bp._stopping = stopping
        with caplog.at_level(logging.DEBUG, logger="bot.RejTest"):
            bp._log_output()
        return caplog.records

    def test_reject_line_is_warning(self, caplog):
        import logging
        records = self._run(caplog, [
            "12:00:00 [bzflag.client] ERROR: Server hat abgelehnt: ServerFull\n",
        ])
        rej = [r for r in records if "Server hat abgelehnt" in r.getMessage()]
        assert len(rej) == 1
        assert rej[0].levelno == logging.WARNING
        assert "ServerFull" in rej[0].getMessage()

    def test_error_line_is_warning(self, caplog):
        """Allgemeine ERROR-Zeile (z. B. Timeout) wird sichtbar (WARNING)."""
        import logging
        records = self._run(caplog, [
            "12:00:00 [bzflag.client] ERROR: Timeout (60s) – kein MsgAccept\n",
        ])
        assert any(r.levelno == logging.WARNING and "Timeout" in r.getMessage()
                   for r in records)

    def test_warning_line_is_warning(self, caplog):
        import logging
        records = self._run(caplog, [
            "12:00:00 [bzbot] WARNING: [Bot] Verbindung verloren\n",
        ])
        assert any(r.levelno == logging.WARNING and "Verbindung verloren" in r.getMessage()
                   for r in records)

    def test_info_line_is_forwarded_as_info(self, caplog):
        """Bot-INFO-Zeilen (z.B. Beitritte/Zustandswechsel) sind im Manager als INFO sichtbar."""
        import logging
        records = self._run(caplog, [
            "12:00:00 [bzbot] INFO: Observer beigetreten: 'Zuschauer'\n",
        ])
        info_recs = [r for r in records if "Observer beigetreten" in r.getMessage()]
        assert info_recs and all(r.levelno == logging.INFO for r in info_recs)

    def test_unmarked_line_stays_debug(self, caplog):
        """Zeilen ohne Level-Token (DEBUG/Rohausgabe) bleiben verborgen (DEBUG)."""
        import logging
        records = self._run(caplog, [
            "irgendeine unmarkierte Rohzeile\n",
        ])
        recs = [r for r in records if "unmarkierte Rohzeile" in r.getMessage()]
        assert recs and all(r.levelno == logging.DEBUG for r in recs)

    def test_crash_dump_on_nonzero_exit(self, caplog):
        """Unerwarteter Exit (Code≠0, kein bewusster Stop) → WARNING-Dump inkl. Tail."""
        import logging
        records = self._run(caplog, [
            "Traceback (most recent call last):\n",
            "  File \"bzbot.py\", line 1, in <module>\n",
            "RuntimeError: boom\n",
        ], returncode=1)
        dump = [r for r in records
                if r.levelno == logging.WARNING and "unerwartet beendet" in r.getMessage()]
        assert len(dump) == 1
        msg = dump[0].getMessage()
        assert "Exit-Code 1" in msg
        assert "RuntimeError: boom" in msg

    def test_no_crash_dump_on_intentional_stop(self, caplog):
        import logging
        records = self._run(caplog, [
            "irgendeine Zeile\n",
        ], returncode=1, stopping=True)
        assert not any("unerwartet beendet" in r.getMessage() for r in records)

    def test_no_crash_dump_on_clean_exit(self, caplog):
        records = self._run(caplog, [
            "12:00:00 [bzbot] INFO: Stoppe …\n",
        ], returncode=0)
        assert not any("unerwartet beendet" in r.getMessage() for r in records)

    def test_reject_exit_is_info_not_crash(self, caplog):
        """Exit-Code BOT_EXIT_REJECTED ⇒ erwartete Server-Ablehnung (INFO), KEIN Absturz."""
        import logging
        from bzflag.protocol import BOT_EXIT_REJECTED
        records = self._run(caplog, [
            "12:00:00 [bzflag.client] WARNING: MsgReject: ServerFull (0x0005)\n",
        ], returncode=BOT_EXIT_REJECTED)
        assert not any("unerwartet beendet" in r.getMessage() for r in records)
        assert any(r.levelno == logging.INFO and "abgelehnt" in r.getMessage()
                   for r in records)

    def test_reject_exit_records_last_rc_and_calls_on_exit(self):
        from bot_manager import BotProcess, Config
        from bzflag.protocol import BOT_EXIT_REJECTED
        bp = BotProcess(callsign="RejTest", config=Config())
        bp.process = _FakeProc(["egal\n"], returncode=BOT_EXIT_REJECTED)
        seen = []
        bp.on_exit = lambda b, rc: seen.append(rc)
        bp._log_output()
        assert bp.last_rc == BOT_EXIT_REJECTED
        assert seen == [BOT_EXIT_REJECTED]

    def test_conn_lost_exit_is_not_crash_but_shows_tail(self, caplog):
        """Exit-Code BOT_EXIT_CONN_LOST ⇒ kein Absturz, aber der Grund (Tail) bleibt sichtbar."""
        import logging
        from bzflag.protocol import BOT_EXIT_CONN_LOST
        records = self._run(caplog, [
            "12:00:00 [bzbot] WARNING: [Bot] Server-Nachricht: You were kicked\n",
            "12:00:00 [bzbot] WARNING: [Bot] Verbindung verloren nach 59s\n",
        ], returncode=BOT_EXIT_CONN_LOST)
        # Nicht als "unerwartet beendet" (=Absturz) markiert …
        assert not any("unerwartet beendet" in r.getMessage() for r in records)
        # … aber ein Verbindungsverlust-Hinweis inkl. Tail (Kick-Grund) ist da.
        dump = [r for r in records
                if "Verbindung verloren" in r.getMessage() and "You were kicked" in r.getMessage()]
        assert len(dump) == 1


# ---------------------------------------------------------------------------
# IPC: Bot→Manager-Status (@@BZMGR@@) und Manager→Bot-Kommandos (stdin)
# ---------------------------------------------------------------------------

class TestStatusIPC:

    def test_status_line_calls_on_status_and_is_not_logged(self, caplog):
        import logging
        from bot_manager import BotProcess, Config
        from bzflag.protocol import MGR_STATUS_PREFIX
        bp = BotProcess(callsign="S", config=Config())
        received = []
        bp.on_status = lambda b, d: received.append(d)
        status = MGR_STATUS_PREFIX + ' {"type":"status","humans":2,"players":["A","B"]}\n'
        bp.process = _FakeProc([status], returncode=0)
        with caplog.at_level(logging.DEBUG, logger="bot.S"):
            bp._log_output()
        assert received and received[0]["humans"] == 2
        # IPC-Zeile darf NICHT als Log-Rauschen erscheinen
        assert not any(MGR_STATUS_PREFIX in r.getMessage() for r in caplog.records)

    def test_status_callback_failure_is_swallowed(self, caplog):
        from bot_manager import BotProcess, Config
        from bzflag.protocol import MGR_STATUS_PREFIX
        bp = BotProcess(callsign="S", config=Config())
        def _boom(b, d):
            raise RuntimeError("boom")
        bp.on_status = _boom
        bp.process = _FakeProc([MGR_STATUS_PREFIX + ' {"humans":1}\n'])
        bp._log_output()  # darf nicht werfen

    def test_send_command_writes_json_line(self):
        from bot_manager import BotProcess, Config
        bp = BotProcess(callsign="X", config=Config())
        bp.process = _FakeProc([])
        bp.send_command({"type": "bots", "callsigns": ["[b0t] A", "[b0t] B"]})
        assert len(bp.process.stdin.lines) == 1
        line = bp.process.stdin.lines[0]
        assert line.endswith("\n")
        assert json.loads(line) == {"type": "bots", "callsigns": ["[b0t] A", "[b0t] B"]}

    def test_send_command_tolerates_closed_pipe(self):
        from bot_manager import BotProcess, Config
        bp = BotProcess(callsign="X", config=Config())
        bp.process = _FakeProc([])
        bp.process.stdin.closed = True
        bp.send_command({"type": "bots", "callsigns": []})  # darf nicht werfen

    def test_on_bot_status_updates_count(self):
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        bot = MagicMock(spec=BotProcess); bot.callsign = "X"
        with patch.object(mgr, "_rebalance"):
            mgr._on_bot_status(bot, {"humans": 3})
        assert mgr._human_count == 3
        assert mgr._last_status_at > 0

    def test_broadcast_bot_list_sends_active_callsigns(self):
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        b1 = MagicMock(spec=BotProcess); b1.is_alive = True;  b1.callsign = "[b0t] A"
        b2 = MagicMock(spec=BotProcess); b2.is_alive = True;  b2.callsign = "[b0t] B"
        b3 = MagicMock(spec=BotProcess); b3.is_alive = False; b3.callsign = "[b0t] C"
        mgr.bots = [b1, b2, b3]
        mgr._broadcast_bot_list()
        for b in (b1, b2):
            b.send_command.assert_called_once_with(
                {"type": "bots", "callsigns": ["[b0t] A", "[b0t] B"]})
        b3.send_command.assert_not_called()


# ---------------------------------------------------------------------------
# Reject-/Crash-Backoff
# ---------------------------------------------------------------------------

class TestRejectBackoff:

    def _mgr(self):
        from bot_manager import BotManager, Config
        cfg = Config()
        cfg.restart_backoff_base = 10.0
        cfg.restart_backoff_max  = 300.0
        cfg.restart_healthy_s    = 30.0
        return BotManager(cfg)

    def test_early_exit_increments_backoff(self):
        from bot_manager import BotProcess
        mgr = self._mgr()
        bot = BotProcess(callsign="X", config=mgr.config)
        bot.start_time = time.monotonic() - 5.0   # nur 5s gelaufen → früh
        mgr._on_bot_exit(bot, 1)
        assert mgr._failure_count == 1
        assert mgr._next_start_allowed > time.monotonic()

    def test_backoff_grows(self):
        from bot_manager import BotProcess
        mgr = self._mgr()
        bot = BotProcess(callsign="X", config=mgr.config)
        bot.start_time = time.monotonic() - 1.0
        mgr._on_bot_exit(bot, 1)
        d1 = mgr._next_start_allowed - time.monotonic()
        bot.start_time = time.monotonic() - 1.0
        mgr._on_bot_exit(bot, 1)
        d2 = mgr._next_start_allowed - time.monotonic()
        assert d2 > d1

    def test_backoff_capped(self):
        from bot_manager import BotProcess
        mgr = self._mgr()
        bot = BotProcess(callsign="X", config=mgr.config)
        for _ in range(20):
            bot.start_time = time.monotonic() - 1.0
            mgr._on_bot_exit(bot, 1)
        assert (mgr._next_start_allowed - time.monotonic()) <= mgr.config.restart_backoff_max + 1.0

    def test_healthy_exit_resets(self):
        from bot_manager import BotProcess
        mgr = self._mgr()
        mgr._failure_count = 3
        bot = BotProcess(callsign="X", config=mgr.config)
        bot.start_time = time.monotonic() - 100.0  # lange gelaufen → gesund
        mgr._on_bot_exit(bot, 1)
        assert mgr._failure_count == 0

    def test_start_bot_gated_by_backoff(self):
        from bot_manager import BotProcess
        mgr = self._mgr()
        mgr.config.bot_name_prefix = ""
        mgr.config.bot_callsigns = ["A", "B"]
        mgr._next_start_allowed = time.monotonic() + 1000.0
        with patch.object(BotProcess, "start", return_value=True) as mock_start:
            mgr._start_bot()
        mock_start.assert_not_called()
        assert mgr.bots == []

    def test_reset_backoff_if_healthy(self):
        from bot_manager import BotProcess
        mgr = self._mgr()
        mgr._failure_count = 2
        bot = MagicMock(spec=BotProcess)
        bot.is_alive = True
        bot.start_time = time.monotonic() - 100.0
        bot.callsign = "X"
        mgr.bots = [bot]
        mgr._reset_backoff_if_healthy(time.monotonic())
        assert mgr._failure_count == 0


# ---------------------------------------------------------------------------
# Observer nur als Fallback
# ---------------------------------------------------------------------------

class TestObserverFallback:

    def test_bots_reporting_false_when_no_bots(self):
        from bot_manager import BotManager, Config
        mgr = BotManager(Config())
        assert mgr._bots_reporting(time.monotonic()) is False

    def test_bots_reporting_true_when_recent_status(self):
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        bot = MagicMock(spec=BotProcess); bot.is_alive = True
        mgr.bots = [bot]
        mgr._last_status_at = time.monotonic()
        assert mgr._bots_reporting(time.monotonic()) is True

    def test_bots_reporting_false_when_status_stale(self):
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        bot = MagicMock(spec=BotProcess); bot.is_alive = True
        mgr.bots = [bot]
        mgr._last_status_at = time.monotonic() - 60.0
        assert mgr._bots_reporting(time.monotonic()) is False

    def test_disconnect_observer_if_any(self):
        from bot_manager import BotManager, Config
        mgr = BotManager(Config())
        obs = MagicMock()
        mgr._observer = obs
        mgr._disconnect_observer_if_any()
        obs.disconnect.assert_called_once()
        assert mgr._observer is None


# ---------------------------------------------------------------------------
# Config: observer_callsign + full_bot_callsigns
# ---------------------------------------------------------------------------

class TestConfigObserverAndNames:

    def test_observer_callsign_default(self):
        from bot_manager import Config
        assert Config().observer_callsign == "Bot-Manager"

    def test_observer_callsign_yaml(self, tmp_path):
        pytest.importorskip("yaml", reason="PyYAML nicht installiert")
        from bot_manager import Config
        f = tmp_path / "c.yaml"
        f.write_text('observer_callsign: "Spaeher"\n', encoding="utf-8")
        cfg = Config.from_yaml(str(f))
        assert cfg.observer_callsign == "Spaeher"

    def test_full_bot_callsigns(self):
        from bot_manager import Config
        cfg = Config()
        cfg.bot_name_prefix = "[b0t] "
        cfg.bot_callsigns = ["Zwiebel", "Tomate"]
        assert cfg.full_bot_callsigns() == ["[b0t] Zwiebel", "[b0t] Tomate"]

    def test_full_bot_callsigns_empty(self):
        from bot_manager import Config
        cfg = Config()
        cfg.bot_callsigns = []
        assert cfg.full_bot_callsigns() == []

    def test_observer_detects_full_prefixed_names_as_bots(self):
        """Beobachter erkennt einen Bot anhand der vollen (geprefixten) Namensliste,
        auch wenn bot_callsigns nur Basisnamen enthält."""
        import struct
        from bot_manager import ServerObserver, Config
        from bzflag.protocol import PLAYER_TYPE_TANK, CallSignLen
        cfg = Config()
        cfg.bot_name_prefix = "ZZ_"          # kein Treffer über Prefix für die volle Namensliste
        cfg.bot_callsigns = ["Tomate"]        # → full = ["ZZ_Tomate"]
        with patch("bot_manager.BZFlagClient"):
            obs = ServerObserver(cfg)
        # MsgAddPlayer-Payload: pid(1) ptype(2) team(2) +6 Bytes, dann Callsign
        cs = "ZZ_Tomate"
        payload = (bytes([9]) + struct.pack(">H", PLAYER_TYPE_TANK)
                   + struct.pack(">H", 2) + b"\x00" * 6
                   + cs.encode("ascii").ljust(CallSignLen, b"\x00"))
        obs._on_add_player(0x6170, payload)
        assert obs.players[9]["is_human"] is False
        assert obs.human_count == 0

    def test_observer_counts_human_at_correct_offset(self):
        """Mensch (kein Bot-Name) wird korrekt gezählt – validiert das 6-Byte-Layout."""
        import struct
        from bot_manager import ServerObserver, Config
        from bzflag.protocol import PLAYER_TYPE_TANK, CallSignLen
        cfg = Config()
        cfg.bot_name_prefix = "[b0t] "
        cfg.bot_callsigns = ["Tomate"]
        with patch("bot_manager.BZFlagClient"):
            obs = ServerObserver(cfg)
        cs = "Alice"
        payload = (bytes([4]) + struct.pack(">H", PLAYER_TYPE_TANK)
                   + struct.pack(">H", 2) + b"\x00" * 6
                   + cs.encode("ascii").ljust(CallSignLen, b"\x00"))
        obs._on_add_player(0x6170, payload)
        assert obs.players[4]["is_human"] is True
        assert obs.human_count == 1


# ---------------------------------------------------------------------------
# Bewusster Stop vs. Reject vs. Absturz korrekt klassifizieren
# ---------------------------------------------------------------------------

class TestStopAndCrashClassification:

    def test_select_bot_to_stop_removes_from_list(self):
        """Zum Stoppen ausgewählter Bot wird sofort aus self.bots entfernt, damit er
        nicht später als „Absturz" auftaucht (R3: das eigentliche bot.stop() liegt
        beim Aufrufer, AUSSERHALB des Locks — siehe _rebalance)."""
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        bot = MagicMock(spec=BotProcess)
        bot.is_alive = True
        bot.callsign = "X"
        mgr.bots = [bot]
        selected = mgr._select_bot_to_stop()
        assert selected is bot
        bot.stop.assert_not_called()
        assert mgr.bots == []

    def test_stopped_bot_not_logged_as_crash(self, caplog):
        """Nach _select_bot_to_stop meldet _restart_crashed_bots keinen Absturz (Bot ist weg)."""
        import logging
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        mgr.config.min_bots = 0
        mgr.config.max_bots = 0          # kein Nachstarten (keine echten Subprozesse)
        bot = MagicMock(spec=BotProcess)
        bot.is_alive = True
        bot.callsign = "X"
        mgr.bots = [bot]
        mgr._select_bot_to_stop()
        with caplog.at_level(logging.DEBUG, logger="bot_manager"):
            mgr._restart_crashed_bots()
        assert not any("abgestürzt" in r.getMessage() for r in caplog.records)

    def test_restart_crashed_classifies_reject_as_info(self, caplog):
        import logging
        from bot_manager import BotManager, Config, BotProcess
        from bzflag.protocol import BOT_EXIT_REJECTED
        mgr = BotManager(Config())
        mgr.config.min_bots = 0
        mgr.config.max_bots = 0          # kein Nachstarten (keine echten Subprozesse)
        bot = MagicMock(spec=BotProcess)
        bot.is_alive = False
        bot._stopping = False
        bot.last_rc = BOT_EXIT_REJECTED
        bot.callsign = "Rej"
        mgr.bots = [bot]
        with caplog.at_level(logging.DEBUG, logger="bot_manager"):
            mgr._restart_crashed_bots()
        assert bot not in mgr.bots
        assert not any("abgestürzt" in r.getMessage() for r in caplog.records)
        assert any(r.levelno == logging.INFO and "abgelehnt" in r.getMessage()
                   for r in caplog.records)

    def test_restart_crashed_classifies_real_crash_as_warning(self, caplog):
        import logging
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        mgr.config.min_bots = 0
        mgr.config.max_bots = 0          # kein Nachstarten (keine echten Subprozesse)
        bot = MagicMock(spec=BotProcess)
        bot.is_alive = False
        bot._stopping = False
        bot.last_rc = 1
        bot.callsign = "Boom"
        mgr.bots = [bot]
        with caplog.at_level(logging.DEBUG, logger="bot_manager"):
            mgr._restart_crashed_bots()
        recs = [r for r in caplog.records
                if r.levelno == logging.WARNING and "abgestürzt" in r.getMessage()]
        assert recs
        assert "Exit-Code 1" in recs[0].getMessage()   # Exit-Code immer sichtbar

    def test_restart_crashed_classifies_conn_lost_as_info(self, caplog):
        """Exit-Code BOT_EXIT_CONN_LOST ⇒ Verbindungsverlust (INFO), NICHT 'abgestürzt'."""
        import logging
        from bot_manager import BotManager, Config, BotProcess
        from bzflag.protocol import BOT_EXIT_CONN_LOST
        mgr = BotManager(Config())
        mgr.config.min_bots = 0
        mgr.config.max_bots = 0          # kein Nachstarten (keine echten Subprozesse)
        bot = MagicMock(spec=BotProcess)
        bot.is_alive = False
        bot._stopping = False
        bot.last_rc = BOT_EXIT_CONN_LOST
        bot.callsign = "Lost"
        mgr.bots = [bot]
        with caplog.at_level(logging.DEBUG, logger="bot_manager"):
            mgr._restart_crashed_bots()
        assert bot not in mgr.bots
        assert not any("abgestürzt" in r.getMessage() for r in caplog.records)
        assert any(r.levelno == logging.INFO and "Verbindung verloren" in r.getMessage()
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# Rundenende-Koordination: koordinierter Neustart (Server leeren → neuer Timer)
# ---------------------------------------------------------------------------

class TestRoundRestart:

    def test_round_over_exit_is_info_not_crash(self, caplog):
        """Exit-Code BOT_EXIT_ROUND_OVER ⇒ bewusstes Rundenende (INFO), KEIN Absturz."""
        import logging
        from bot_manager import BotProcess, Config
        from bzflag.protocol import BOT_EXIT_ROUND_OVER
        bp = BotProcess(callsign="RO", config=Config())
        bp.process = _FakeProc(["egal\n"], returncode=BOT_EXIT_ROUND_OVER)
        with caplog.at_level(logging.DEBUG, logger="bot.RO"):
            bp._log_output()
        assert not any("unerwartet beendet" in r.getMessage() for r in caplog.records)
        assert any(r.levelno == logging.INFO and "Rejoin" in r.getMessage()
                   for r in caplog.records)

    def test_round_over_exit_sets_flag_without_backoff(self):
        """BOT_EXIT_ROUND_OVER setzt die Flanke, erhöht aber KEINEN Crash-Backoff."""
        from bot_manager import BotManager, Config, BotProcess
        from bzflag.protocol import BOT_EXIT_ROUND_OVER
        mgr = BotManager(Config())
        bot = MagicMock(spec=BotProcess)
        bot.start_time = time.monotonic() - 1.0   # früh – würde sonst Backoff auslösen
        mgr._on_bot_exit(bot, BOT_EXIT_ROUND_OVER)
        assert mgr._round_over_seen is True
        assert mgr._failure_count == 0
        assert mgr._next_start_allowed == 0.0

    def test_status_game_over_sets_flag(self):
        """game_over im Status setzt die Flanke (bedingungslos, ohne Humans-Check)."""
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        bot = MagicMock(spec=BotProcess); bot.callsign = "X"
        with patch.object(mgr, "_rebalance"):
            mgr._on_bot_status(bot, {"humans": 0, "game_over": True})
        assert mgr._round_over_seen is True
        assert bot.game_over is True

    def test_status_game_over_ignored_during_active_cycle(self):
        """Während ein Neustart-Zyklus läuft, wird die Flanke NICHT erneut gesetzt."""
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        mgr._round_restart_active = True
        bot = MagicMock(spec=BotProcess); bot.callsign = "X"
        with patch.object(mgr, "_rebalance"):
            mgr._on_bot_status(bot, {"humans": 0, "game_over": True})
        assert mgr._round_over_seen is False

    def test_consume_flag_is_one_shot(self):
        from bot_manager import BotManager, Config
        mgr = BotManager(Config())
        mgr._round_over_seen = True
        assert mgr._consume_round_over_flag() is True
        assert mgr._consume_round_over_flag() is False

    def test_consume_flag_false_while_active(self):
        from bot_manager import BotManager, Config
        mgr = BotManager(Config())
        mgr._round_over_seen = True
        mgr._round_restart_active = True
        assert mgr._consume_round_over_flag() is False

    def test_round_restart_stops_all_and_starts_desired(self):
        """Koordinierter Neustart: Observer trennen, ALLE Bots stoppen, Liste leeren,
        dann gewünschte Anzahl frisch starten – ohne Humans-Check."""
        from bot_manager import BotManager, Config, BotProcess
        cfg = Config(); cfg.min_bots = 2; cfg.max_bots = 2
        mgr = BotManager(cfg)
        b1 = MagicMock(spec=BotProcess); b1.is_alive = False; b1.callsign = "A"
        b2 = MagicMock(spec=BotProcess); b2.is_alive = False; b2.callsign = "B"
        mgr.bots = [b1, b2]
        mgr._human_count = 0
        with patch.object(mgr, "_start_bot") as start, \
             patch.object(mgr, "_disconnect_observer_if_any") as disc, \
             patch("bot_manager.time.sleep"):
            mgr._round_restart()
        b1.stop.assert_called_once()
        b2.stop.assert_called_once()
        disc.assert_called_once()
        assert mgr.bots == []                 # geleert (gemocktes _start_bot hängt nichts an)
        assert start.call_count == 2          # desired = max_bots - humans = 2
        assert mgr._round_restart_active is False
        assert mgr._round_over_seen is False  # am Zyklusende verworfen

    def test_round_restart_waits_for_real_process_end(self):
        """Regression B5: die Warteschleife muss auf einen Snapshot der gestoppten
        Prozesse warten statt auf self.bots, das vorher bereits geleert wird –
        sonst prüft any(...) eine leere Liste und wartet nie (No-Op)."""
        from bot_manager import BotManager, Config, BotProcess
        cfg = Config(); cfg.min_bots = 1; cfg.max_bots = 1
        mgr = BotManager(cfg)
        b1 = MagicMock(spec=BotProcess); b1.is_alive = True; b1.callsign = "A"
        mgr.bots = [b1]
        mgr._human_count = 0

        def _fake_stop():
            # Simuliert Prozessende: erst nach stop() wird is_alive False.
            b1.is_alive = False

        b1.stop.side_effect = _fake_stop

        with patch.object(mgr, "_start_bot"), \
             patch.object(mgr, "_disconnect_observer_if_any"), \
             patch("bot_manager.time.sleep") as sleep_mock:
            mgr._round_restart()
        # is_alive wurde durch stop() bereits vor der Warteschleife False – die
        # Schleife darf daher gar nicht erst pollen (nur der abschließende
        # ROUND_RESTART_GAP_S-Sleep bleibt übrig, kein 0.1s-Poll).
        poll_calls = [c for c in sleep_mock.call_args_list if c.args[0] == 0.1]
        assert poll_calls == []
        assert mgr.bots == []

    def test_round_restart_waits_while_process_still_alive(self):
        """Bleibt ein Prozess im Snapshot länger 'alive', pollt die Warteschleife
        tatsächlich (statt sofort durchzulaufen wie bei der leeren self.bots-Liste)."""
        from bot_manager import BotManager, Config, BotProcess
        cfg = Config(); cfg.min_bots = 1; cfg.max_bots = 1
        mgr = BotManager(cfg)
        b1 = MagicMock(spec=BotProcess); b1.is_alive = True; b1.callsign = "A"
        mgr.bots = [b1]
        mgr._human_count = 0

        polls = {"n": 0}

        def _fake_sleep(secs):
            if secs == 0.1:
                polls["n"] += 1
                if polls["n"] >= 2:
                    b1.is_alive = False   # Prozess "beendet sich" nach 2 Polls

        with patch.object(mgr, "_start_bot"), \
             patch.object(mgr, "_disconnect_observer_if_any"), \
             patch("bot_manager.time.sleep", side_effect=_fake_sleep) as sleep_mock:
            mgr._round_restart()
        poll_calls = [c for c in sleep_mock.call_args_list if c.args[0] == 0.1]
        assert len(poll_calls) == 2
        assert mgr.bots == []

    def test_round_restart_keeps_human_slots_free(self):
        """Bei verbundenen Menschen werden entsprechend weniger Bots neu gestartet."""
        from bot_manager import BotManager, Config
        cfg = Config(); cfg.min_bots = 0; cfg.max_bots = 4
        mgr = BotManager(cfg)
        mgr._human_count = 3
        mgr.bots = []
        with patch.object(mgr, "_start_bot") as start, \
             patch.object(mgr, "_disconnect_observer_if_any"), \
             patch("bot_manager.time.sleep"):
            mgr._round_restart()
        assert start.call_count == 1          # 4 - 3 Menschen = 1 Bot

    def test_guards_noop_during_active_cycle(self):
        """Rebalance/Restart/Rotate funken während eines aktiven Zyklus nicht dazwischen."""
        from bot_manager import BotManager, Config
        mgr = BotManager(Config())
        mgr._round_restart_active = True
        with patch.object(mgr, "_start_bot") as start, \
             patch.object(mgr, "_select_bot_to_stop") as stop:
            mgr._rebalance()
            mgr._restart_crashed_bots()
            mgr._rotate_expired_bots()
        start.assert_not_called()
        stop.assert_not_called()


# ---------------------------------------------------------------------------
# BotProcess: cProfile-Start (profile/profile_dir) + gestaffelter Stop
# ---------------------------------------------------------------------------

class TestBotProcessProfile:

    def _start_capture_cmd(self, bp):
        """start() mit gemocktem Popen ausführen und die Kommandozeile zurückgeben.
        stdout=None lässt den Log-Thread sofort zurückkehren."""
        with patch("bot_manager.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=1234, stdout=None)
            assert bp.start()
        return popen.call_args[0][0]

    def test_profile_defaults(self):
        from bot_manager import Config
        cfg = Config()
        assert cfg.profile is False
        assert cfg.profile_dir == "/tmp"

    def test_default_start_without_cprofile(self):
        from bot_manager import BotProcess, Config
        bp = BotProcess(callsign="X", config=Config())
        cmd = self._start_capture_cmd(bp)
        assert "cProfile" not in cmd
        assert cmd[1].endswith("bzbot.py")

    def test_profile_wraps_cmd_and_writes_to_dir(self, tmp_path):
        from bot_manager import BotProcess, Config
        cfg = Config()
        cfg.profile = True
        cfg.profile_dir = str(tmp_path / "profiles")   # existiert noch nicht → makedirs
        bp = BotProcess(callsign="[b0t] Tank You", config=cfg)
        cmd = self._start_capture_cmd(bp)
        i = cmd.index("cProfile")
        assert cmd[i - 1] == "-m" and cmd[i + 1] == "-o"
        out = cmd[i + 2]
        assert out.startswith(str(tmp_path / "profiles"))
        assert out.endswith(".prof")
        assert os.path.isdir(cfg.profile_dir)
        # Callsign dateinamen-sicher bereinigt
        fname = os.path.basename(out)
        assert " " not in fname and "[" not in fname and "]" not in fname
        # bzbot.py inkl. aller Bot-Argumente folgt NACH dem cProfile-Teil
        assert cmd[i + 3].endswith("bzbot.py")
        assert "--managed" in cmd[i + 3:]

    def test_profile_outfiles_unique_per_botprocess(self, tmp_path):
        from bot_manager import BotProcess, Config
        cfg = Config()
        cfg.profile = True
        cfg.profile_dir = str(tmp_path)
        a = BotProcess(callsign="Same", config=cfg)
        b = BotProcess(callsign="Same", config=cfg)
        assert a._profile_outfile() != b._profile_outfile()  # id im Namen


class TestBotProcessStopStaged:
    """stop() = SIGINT → SIGTERM → SIGKILL (SIGINT-first für cProfile-Dump)."""

    def _bp_with_proc(self):
        from bot_manager import BotProcess, Config
        bp = BotProcess(callsign="X", config=Config())
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 42
        bp.process = proc
        return bp, proc

    def test_sigint_suffices(self):
        import signal as _signal
        bp, proc = self._bp_with_proc()
        bp.stop(timeout=0.1)
        proc.send_signal.assert_called_once_with(_signal.SIGINT)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        assert bp.process is None
        assert bp._stopping is True

    def test_sigint_unsupported_falls_back_to_terminate(self):
        """Windows: Popen.send_signal(SIGINT) wirft ValueError → SIGTERM-Pfad."""
        bp, proc = self._bp_with_proc()
        proc.send_signal.side_effect = ValueError("Unsupported signal: 2")
        bp.stop(timeout=0.1)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        assert bp.process is None

    def test_sigint_timeout_then_terminate(self):
        import subprocess as _subprocess
        bp, proc = self._bp_with_proc()
        proc.wait.side_effect = [_subprocess.TimeoutExpired("x", 0.1), 0]
        bp.stop(timeout=0.1)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        assert bp.process is None

    def test_all_timeouts_end_in_kill(self):
        import subprocess as _subprocess
        bp, proc = self._bp_with_proc()
        proc.wait.side_effect = _subprocess.TimeoutExpired("x", 0.1)
        bp.stop(timeout=0.1)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert bp.process is None


# ---------------------------------------------------------------------------
# Präsenz-basierte Skalierung + Abräum-Timeout (_desired_bot_count)
# ---------------------------------------------------------------------------

class TestPresenceScaling:
    """desired = min_bots ohne echte Präsenz (nach Timeout); mit Präsenz
    min_bots+max_bots − aktive Spieler. Zuschauer triggern, zählen aber nicht ab."""

    def _mgr(self, min_bots=1, max_bots=3, cleanup=300.0):
        from bot_manager import BotManager, Config
        cfg = Config()
        cfg.min_bots = min_bots
        cfg.max_bots = max_bots
        cfg.idle_cleanup_delay = cleanup
        return BotManager(cfg)

    def _alive(self, n):
        from bot_manager import BotProcess
        out = []
        for _ in range(n):
            b = MagicMock(spec=BotProcess); b.is_alive = True
            out.append(b)
        return out

    def test_config_default_idle_cleanup_delay(self):
        from bot_manager import Config
        assert Config().idle_cleanup_delay == pytest.approx(300.0)

    def test_no_presence_after_timeout_returns_min(self):
        mgr = self._mgr(min_bots=1, max_bots=3, cleanup=300.0)
        now = time.monotonic()
        mgr._presence_lost_at = now - 400.0     # Timeout bereits abgelaufen
        assert mgr._desired_bot_count(now) == 1

    def test_single_spectator_gives_full_pool(self):
        """Ein Zuschauer (human=0, obs=1) ⇒ volle Arena min+max, kein Abzug."""
        mgr = self._mgr(min_bots=1, max_bots=3)
        mgr._human_count = 0
        mgr._observer_count = 1
        assert mgr._desired_bot_count(time.monotonic()) == 4

    def test_one_player_subtracts_one(self):
        mgr = self._mgr(min_bots=1, max_bots=3)
        mgr._human_count = 1
        assert mgr._desired_bot_count(time.monotonic()) == 3   # 1+3-1

    def test_many_players_floored_at_min(self):
        mgr = self._mgr(min_bots=1, max_bots=3)
        mgr._human_count = 5
        assert mgr._desired_bot_count(time.monotonic()) == 1   # max(1, 4-5)

    def test_grace_period_holds_current_level(self):
        """Präsenz fällt auf 0, Timeout NICHT abgelaufen → aktuelles Bot-Niveau halten."""
        mgr = self._mgr(min_bots=1, max_bots=3, cleanup=300.0)
        mgr.bots = self._alive(3)
        now = time.monotonic()
        assert mgr._desired_bot_count(now) == 3    # hält 3, räumt nicht ab
        assert mgr._presence_lost_at is not None    # Timer gestartet

    def test_cleanup_after_delay_drops_to_min(self):
        mgr = self._mgr(min_bots=1, max_bots=3, cleanup=300.0)
        mgr.bots = self._alive(3)
        now = time.monotonic()
        mgr._presence_lost_at = now - 301.0
        assert mgr._desired_bot_count(now) == 1

    def test_zero_delay_cleans_immediately(self):
        mgr = self._mgr(min_bots=1, max_bots=3, cleanup=0.0)
        mgr.bots = self._alive(3)
        assert mgr._desired_bot_count(time.monotonic()) == 1

    def test_presence_resets_timer(self):
        mgr = self._mgr(min_bots=1, max_bots=3)
        mgr._desired_bot_count(time.monotonic())          # presence 0 → Timer läuft
        assert mgr._presence_lost_at is not None
        mgr._human_count = 1
        mgr._desired_bot_count(time.monotonic())          # Präsenz da → Timer zurück
        assert mgr._presence_lost_at is None


class TestObserverAwareness:
    """Manager erfährt Zuschauer aus Bot-Status (primär) und Fallback-Observer."""

    def test_on_bot_status_reads_observers(self):
        from bot_manager import BotManager, Config, BotProcess
        mgr = BotManager(Config())
        bot = MagicMock(spec=BotProcess); bot.callsign = "X"
        with patch.object(mgr, "_rebalance") as reb:
            mgr._on_bot_status(bot, {"humans": 0, "observers": 2})
        assert mgr._observer_count == 2
        reb.assert_called_once()          # Zuschauer-Änderung löst Rebalance aus

    def test_server_observer_counts_real_spectator(self):
        import struct
        from bot_manager import ServerObserver, Config
        from bzflag.protocol import PLAYER_TYPE_TANK, TEAM_OBSERVER, CallSignLen
        cfg = Config()
        cfg.bot_name_prefix = "[b0t] "
        seen = []
        with patch("bot_manager.BZFlagClient"):
            obs = ServerObserver(cfg, on_count_changed=lambda h, o: seen.append((h, o)))
        cs = "Watcher"                     # kein Bot-Name, kein eigener Callsign
        payload = (bytes([7]) + struct.pack(">H", PLAYER_TYPE_TANK)
                   + struct.pack(">H", TEAM_OBSERVER) + b"\x00" * 6
                   + cs.encode("ascii").ljust(CallSignLen, b"\x00"))
        obs._on_add_player(0x6170, payload)
        assert obs.observer_count == 1
        assert obs.human_count == 0
        assert seen[-1] == (0, 1)

    def test_server_observer_excludes_own_connection(self):
        """Die eigene Observer-Verbindung zählt NICHT als Zuschauer."""
        import struct
        from bot_manager import ServerObserver, Config
        from bzflag.protocol import PLAYER_TYPE_TANK, TEAM_OBSERVER, CallSignLen
        cfg = Config()
        cfg.observer_callsign = "Bot-Manager"
        with patch("bot_manager.BZFlagClient"):
            obs = ServerObserver(cfg)
        cs = "Bot-Manager"                 # == eigener Observer-Callsign
        payload = (bytes([1]) + struct.pack(">H", PLAYER_TYPE_TANK)
                   + struct.pack(">H", TEAM_OBSERVER) + b"\x00" * 6
                   + cs.encode("ascii").ljust(CallSignLen, b"\x00"))
        obs._on_add_player(0x6170, payload)
        assert obs.observer_count == 0
