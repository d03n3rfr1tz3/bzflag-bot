#!/usr/bin/env python3
"""Bot-Manager: überwacht Spielerzahl via Observer-Verbindung und hält Bot-Anzahl dynamisch."""

import argparse
import collections
import json
import logging
import os
import random
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from bzflag.client   import BZFlagClient
from bzflag.protocol import (
    DEFAULT_PORT,
    PLAYER_TYPE_TANK, PLAYER_TYPE_COMPUTER,
    TEAM_OBSERVER, TEAM_AUTOMATIC,
    MsgAddPlayer, MsgRemovePlayer, MSG_INTERNAL_DISCONNECT,
    MsgSuperKill,
    unpack_uint8, unpack_uint16, unpack_string,
    CallSignLen, MGR_STATUS_PREFIX, BOT_EXIT_REJECTED,
    BOT_EXIT_ROUND_OVER, BOT_EXIT_CONN_LOST, ROUND_RESTART_GAP_S,
)

logger = logging.getLogger("bot_manager")

# Pfad zu bzbot.py relativ zu diesem Skript
BZBOT_SCRIPT = Path(__file__).parent / "bzbot.py"


# ---------------------------------------------------------------------------
# Konfigurationsstruktur
# ---------------------------------------------------------------------------

class Config:
    """Konfigurationsparameter für Manager und Bots; Defaults entsprechen config.yaml."""

    def __init__(self):
        self.host             = "localhost"
        self.port             = DEFAULT_PORT
        self.max_bots         = 3
        self.min_bots         = 0
        # Prefix, den JEDER Bot erhält (für die kombinierte Erkennung wichtig).
        self.bot_name_prefix  = "Bot_"
        # Basis-Namen der Bots (OHNE Prefix). In-Game-Callsign = bot_name_prefix + Basisname.
        # Bsp.: bot_name_prefix="[b0t] ", bot_callsigns=["Zwiebel","Tomate"] → "[b0t] Zwiebel"
        self.bot_callsigns: List[str] = []
        # Callsign des Fallback-Observers (nur verbunden, wenn kein Bot Status liefert).
        self.observer_callsign = "Bot-Manager"
        self.team             = 0xFFFE   # automatisch
        self.motto            = ""
        self.token            = ""
        self.world_half       = 200.0
        self.check_interval   = 5.0    # Sekunden zwischen Überprüfungen
        self.reconnect_delay  = 10.0   # Sekunden vor Reconnect-Versuch
        self.log_level        = "INFO"
        self.bot_lifetime_min = 900.0  # Minimale Bot-Lebensdauer in Sekunden (15 min)
        self.bot_lifetime_max = 7200.0 # Maximale Bot-Lebensdauer in Sekunden (2 h)
        # Reject-/Crash-Backoff: ein früh beendeter Bot-Slot wartet zunehmend, statt
        # im Sekundentakt neu zu starten (verhindert Hot-Restart-Loop & Log-Spam).
        self.restart_backoff_base = 10.0   # Basis-Wartezeit (Sekunden) nach dem 1. frühen Ende
        self.restart_backoff_max  = 300.0  # Obergrenze der Wartezeit (Sekunden)
        self.restart_healthy_s    = 30.0   # ab dieser Laufzeit gilt ein Bot als „gesund" → Reset
        # Abräum-Timeout: Wenn kein echter Mensch (Spieler ODER Zuschauer) mehr auf dem
        # Server ist, wird erst nach dieser Zeit auf min_bots reduziert. Bis dahin bleibt
        # das aktuelle Bot-Niveau erhalten. 0 = sofort abräumen.
        self.idle_cleanup_delay   = 300.0  # Sekunden (5 min)
        self.good_flags: Optional[List[str]] = None   # None = Standardliste aus bzbot.py
        self.bad_flags:  Optional[List[str]] = None   # None = Standardliste aus bzbot.py
        # cProfile-Instrumentierung: profile=True startet jeden Bot als
        # `python -m cProfile -o <profile_dir>/bzbot_<name>_<id>_<zeit>.prof bzbot.py …`.
        # Das Profil wird beim regulären Prozessende geschrieben — stop() sendet dafür
        # zuerst SIGINT (sauberer Shutdown statt SIGTERM-Kill, siehe BotProcess.stop).
        self.profile      = False
        self.profile_dir  = "/tmp"   # Zielordner für .prof-Dateien

    def full_bot_callsigns(self) -> List[str]:
        """In-Game-Callsigns aller konfigurierten Bots = bot_name_prefix + Basisname.
        Leer, wenn bot_callsigns nicht gesetzt ist (dann greift die Prefix+Nummer-Fallback-Logik)."""
        prefix = self.bot_name_prefix or ""
        return [prefix + base for base in self.bot_callsigns]

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        """Lädt Konfiguration aus einer YAML-Datei; erfordert pyyaml."""
        if not HAS_YAML:
            raise ImportError("PyYAML ist nicht installiert. 'pip install pyyaml' ausführen.")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = cls()
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg


# ---------------------------------------------------------------------------
# Observer-Verbindung zum Zählen menschlicher Spieler
# ---------------------------------------------------------------------------

class ServerObserver:
    """
    Verbindet sich als Observer (nicht-spielender Beobachter) zum BZFlag-Server
    und verfolgt die Anzahl menschlicher Spieler.
    """

    def __init__(self, config: Config, on_count_changed=None):
        self.config = config
        self.on_count_changed = on_count_changed

        self.client  = BZFlagClient(config.host, config.port)
        # Beobachter handhabt nur Add/Remove; den 60-Hz-Restverkehr nicht als
        # „Unbehandelt" loggen (sonst Log-Flut).
        self.client.log_unhandled = False
        self.players: Dict[int, dict] = {}
        self.human_count = 0
        self.observer_count = 0    # echte Zuschauer (ohne die eigene Observer-Verbindung)
        self._own_callsign = config.observer_callsign or f"__mgr_obs_{os.getpid()}"

        self.client.add_handler(MsgAddPlayer,           self._on_add_player)
        self.client.add_handler(MsgRemovePlayer,        self._on_remove_player)
        self.client.add_handler(MSG_INTERNAL_DISCONNECT, self._on_disconnect)
        self.client.add_handler(MsgSuperKill,           self._on_disconnect)

    def connect(self) -> bool:
        """Verbindet als Observer: TankPlayer-Typ + ObserverTeam.

        Observer ist serverseitig KEIN Player-Typ, sondern ein Team – der echte
        Client tritt auch als Observer mit TankPlayer bei (ComputerPlayer würde
        bei -disableBots als BadType abgelehnt)."""
        if not self.client.connect():
            return False
        ok = self.client.join_game(
            callsign    = self._own_callsign,
            player_type = PLAYER_TYPE_TANK,
            team        = TEAM_OBSERVER,
            timeout     = 20.0,
        )
        if not ok:
            logger.warning("Observer-Join fehlgeschlagen – Playercount-Monitoring eingeschränkt")
        return ok

    def disconnect(self):
        """Trennt Observer-Verbindung."""
        self.client.disconnect()

    @property
    def is_connected(self):
        """True wenn Observer mit dem Server verbunden ist."""
        return self.client.connected

    def _on_add_player(self, code, payload):
        """Registriert neuen Spieler; inkrementiert human_count wenn Mensch.

        Layout wie in bzbot._on_add_player (verifiziert): pid(1) ptype(2) team(2),
        dann 6 Bytes (wins/losses/tks), danach der Callsign."""
        if len(payload) < 1+2+2+6+CallSignLen:
            return
        off  = 0
        pid  = unpack_uint8(payload, off);  off += 1
        ptype = unpack_uint16(payload, off); off += 2
        team  = unpack_uint16(payload, off); off += 2
        off += 6
        callsign = unpack_string(payload, off, CallSignLen)

        is_observer = (team == TEAM_OBSERVER)
        full = self.config.full_bot_callsigns()
        is_bot = (
            ptype == PLAYER_TYPE_COMPUTER
            or callsign == self._own_callsign
            or (full and callsign in full)
            or (self.config.bot_name_prefix and callsign.startswith(self.config.bot_name_prefix))
        )
        # Echter Zuschauer = Observer-Team, aber weder Bot noch die eigene Observer-Verbindung.
        is_real_observer = is_observer and not is_bot

        self.players[pid] = {
            "callsign":       callsign,
            "team":           team,
            "player_type":    ptype,
            "is_human":       not is_bot and not is_observer,
            "is_real_observer": is_real_observer,
        }

        if self.players[pid]["is_human"]:
            self.human_count += 1
            logger.info("Spieler beigetreten: '%s' (Menschen: %d)", callsign, self.human_count)
            self._report_counts()
        elif is_real_observer:
            self.observer_count += 1
            logger.info("Zuschauer beigetreten: '%s' (Zuschauer: %d)", callsign, self.observer_count)
            self._report_counts()

    def _report_counts(self):
        """Meldet aktuelle Spieler- und Zuschauerzahl an den Manager-Callback."""
        if self.on_count_changed:
            self.on_count_changed(self.human_count, self.observer_count)

    def _on_remove_player(self, code, payload):
        """Entfernt Spieler; dekrementiert human_count/observer_count entsprechend."""
        if len(payload) < 1:
            return
        pid  = unpack_uint8(payload, 0)
        info = self.players.pop(pid, None)
        if info and info["is_human"]:
            self.human_count = max(0, self.human_count - 1)
            logger.info("Spieler verlassen: '%s' (Menschen: %d)",
                        info["callsign"], self.human_count)
            self._report_counts()
        elif info and info.get("is_real_observer"):
            self.observer_count = max(0, self.observer_count - 1)
            logger.info("Zuschauer verlassen: '%s' (Zuschauer: %d)",
                        info["callsign"], self.observer_count)
            self._report_counts()

    def _on_disconnect(self, code, payload):
        """Loggt Verbindungsverlust des Observers."""
        logger.warning("Observer-Verbindung verloren")


# ---------------------------------------------------------------------------
# Bot-Prozessverwaltung
# ---------------------------------------------------------------------------

class BotProcess:
    """Repräsentiert einen einzelnen laufenden Bot-Prozess."""
    _id_counter = 0

    def __init__(self, callsign: str, config: Config):
        """Legt Prozess-Objekt an; startet den Prozess erst via start()."""
        BotProcess._id_counter += 1
        self.id       = BotProcess._id_counter
        self.callsign = callsign
        self.process: Optional[subprocess.Popen] = None
        self.config   = config
        self.start_time: Optional[float] = None
        self.lifetime: float = 0.0  # Zufällige Lebensdauer in Sekunden, gesetzt vom Manager
        self._stopping = False      # True = bewusster Stop (terminate); kein „Absturz"
        self.last_rc: Optional[int] = None  # Exit-Code des Prozesses (BOT_EXIT_REJECTED = abgelehnt)
        self.game_over = False      # letzter gemeldeter Rundenende-Zustand (aus Status-IPC)
        # Vom Manager gesetzte Callbacks (aus dem Log-Thread aufgerufen):
        #   on_status(bot, dict) bei @@BZMGR@@-Statuszeile, on_exit(bot, rc) bei unerwartetem Ende.
        self.on_status = None
        self.on_exit   = None

    def _profile_outfile(self) -> str:
        """Eindeutiger cProfile-Ausgabepfad: <profile_dir>/bzbot_<name>_<id>_<zeit>.prof.
        Callsign wird dateinamen-sicher bereinigt; id+Zeitstempel verhindern, dass
        Restarts (Lebensdauer/Rundenende) ein früheres Profil überschreiben."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.callsign).strip("_") or f"bot{self.id}"
        out_dir = self.config.profile_dir or "/tmp"
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            logger.warning("Profil-Ordner '%s' nicht anlegbar: %s", out_dir, exc)
        fname = f"bzbot_{safe}_{self.id}_{time.strftime('%Y%m%d-%H%M%S')}.prof"
        return os.path.join(out_dir, fname)

    def start(self) -> bool:
        """Startet bzbot.py als Subprozess und beginnt Log-Weiterleitung in eigenem Thread."""
        cmd = [sys.executable]
        profile_out = None
        if self.config.profile:
            # cProfile umhüllt den Bot-Prozess; -o schreibt das Profil erst beim
            # regulären Prozessende (stop() erzwingt das per SIGINT-first).
            profile_out = self._profile_outfile()
            cmd += ["-m", "cProfile", "-o", profile_out]
        cmd += [
            str(BZBOT_SCRIPT),
            "--managed",                       # IPC über stdin/stdout aktivieren
            "--host",      self.config.host,
            "--port",      str(self.config.port),
            "--callsign",  self.callsign,
            "--team",      str(self.config.team),
            "--world-half", str(self.config.world_half),
            "--log-level", self.config.log_level,
        ]
        if self.config.motto:
            cmd += ["--motto", self.config.motto]
        if self.config.token:
            cmd += ["--token", self.config.token]
        # Bot-Erkennung: Prefix UND volle (geprefixte) Namensliste kombiniert weitergeben.
        if self.config.bot_name_prefix:
            cmd += ["--bot-name-prefix", self.config.bot_name_prefix]
        full = self.config.full_bot_callsigns()
        if full:
            cmd += ["--bot-callsigns", ",".join(full)]
        if self.config.good_flags is not None:
            cmd += ["--good-flags", ",".join(self.config.good_flags)]
        if self.config.bad_flags is not None:
            cmd += ["--bad-flags", ",".join(self.config.bad_flags)]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin  = subprocess.PIPE,   # Manager→Bot: JSON-Kommandozeilen
                stdout = subprocess.PIPE,
                stderr = subprocess.STDOUT,
                text   = True,
                bufsize = 1,
            )
            self.start_time = time.monotonic()
            logger.info("Bot '%s' gestartet (PID %d)", self.callsign, self.process.pid)
            if profile_out:
                logger.info("Bot '%s' läuft unter cProfile → %s", self.callsign, profile_out)

            # Ausgabe-Logging in eigenem Thread
            t = threading.Thread(
                target  = self._log_output,
                daemon  = True,
                name    = f"bot-log-{self.callsign}",
            )
            t.start()
            return True
        except Exception as exc:
            logger.error("Fehler beim Starten von '%s': %s", self.callsign, exc)
            return False

    def stop(self, timeout: float = 5.0):
        """Beendet Bot-Prozess gestaffelt: SIGINT → SIGTERM → SIGKILL.

        SIGINT zuerst, weil es im Bot-Hauptthread KeyboardInterrupt auslöst und
        main() sich damit regulär beendet — nur so schreibt ein unter cProfile
        gestarteter Bot sein -o-Profil (SIGTERM tötet den Interpreter ohne
        finally). Unter Windows unterstützt Popen kein SIGINT (ValueError) →
        Sprung direkt zu terminate()."""
        if self.process is None:
            return
        if self.process.poll() is not None:
            return  # bereits beendet
        logger.info("Stoppe Bot '%s' (PID %d)...", self.callsign, self.process.pid)
        self._stopping = True  # signalisiert _log_output: kein Absturz, sondern bewusster Stop
        try:
            try:
                self.process.send_signal(signal.SIGINT)
                self.process.wait(timeout=timeout)
                return
            except (ValueError, OSError):
                pass  # SIGINT nicht zustellbar (z.B. Windows) → SIGTERM versuchen
            except subprocess.TimeoutExpired:
                logger.warning("Bot '%s' reagiert nicht auf SIGINT – sende SIGTERM",
                               self.callsign)
            self.process.terminate()
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("Bot '%s' reagiert nicht – sende SIGKILL", self.callsign)
            self.process.kill()
        except Exception as exc:
            logger.warning("Fehler beim Stoppen von '%s': %s", self.callsign, exc)
        finally:
            self.process = None

    def send_command(self, obj: dict) -> None:
        """Sendet ein JSON-Kommando (eine Zeile) an den Bot-stdin. Fehlertolerant
        (geschlossene/kaputte Pipe wird stillschweigend ignoriert)."""
        proc = self.process
        if not (proc and proc.stdin):
            return
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    @property
    def is_alive(self) -> bool:
        """True wenn der Bot-Subprozess noch läuft."""
        return self.process is not None and self.process.poll() is None

    # Erkennt das im Bot-Log eingebettete Level (Format des Bots:
    # "%(asctime)s [%(name)s] %(levelname)s: %(message)s"). Greift nach dem
    # ersten "] <LEVEL>:" – der Name kann Leerzeichen/Padding enthalten.
    _LEVEL_RE = re.compile(r"\]\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL):")

    def _handle_status_line(self, line: str) -> None:
        """Verarbeitet eine @@BZMGR@@-Statuszeile des Bots → on_status-Callback
        (wird NICHT geloggt, damit das IPC kein Log-Rauschen erzeugt)."""
        try:
            data = json.loads(line[len(MGR_STATUS_PREFIX):].strip())
        except Exception:
            return
        cb = self.on_status
        if cb:
            try:
                cb(self, data)
            except Exception:
                pass

    def _log_output(self):
        """Leitet stdout des Bot-Prozesses zeilenweise an den Logger weiter.

        @@BZMGR@@-Statuszeilen werden als IPC abgefangen (on_status, kein Log).
        Problem-Zeilen des Bots (WARNING/ERROR/CRITICAL, inkl. Ablehnungsgründen)
        werden auf manager-WARNING gehoben, alles andere bleibt DEBUG (kein
        generelles Anheben des Log-Levels). Endet der Stream durch einen
        unerwarteten Exit (Code ≠ 0, kein bewusster Stop), werden Exit-Code und
        die letzten Ausgabezeilen als WARNING ausgegeben (Tracebacks/„Verbindung
        verloren" ohne Level-Token) und on_exit für den Reject-Backoff aufgerufen."""
        proc = self.process
        if not (proc and proc.stdout):
            return
        bot_logger = logging.getLogger(f"bot.{self.callsign}")
        recent: "collections.deque[str]" = collections.deque(maxlen=15)
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith(MGR_STATUS_PREFIX):
                self._handle_status_line(line)
                continue
            recent.append(line)
            m = self._LEVEL_RE.search(line)
            level = m.group(1) if m else None
            if level in ("WARNING", "ERROR", "CRITICAL"):
                bot_logger.warning("%s", line)
            elif level == "INFO":
                bot_logger.info("%s", line)   # Bot-INFO sichtbar (Beitritte/Zustandswechsel)
            else:
                bot_logger.debug("%s", line)  # DEBUG & unmarkierte Zeilen bleiben verborgen

        # Stream zu Ende → Prozess beendet sich. Exit klassifizieren.
        rc = proc.poll()
        self.last_rc = rc
        if rc in (None, 0) or self._stopping:
            return
        if rc == BOT_EXIT_REJECTED:
            # Erwartete Server-Ablehnung (Kapazität/Callsign) – kein Absturz. Der konkrete
            # Grund wurde bereits als WARNING (Client-„MsgReject: …") durchgereicht.
            bot_logger.info(
                "Bot '%s' vom Server abgelehnt (z.B. Server/Team voll) – wird zurückgestellt",
                self.callsign,
            )
        elif rc == BOT_EXIT_ROUND_OVER:
            # Bewusstes Rundenende-Exit – kein Absturz. Der Manager koordiniert das Rejoin.
            bot_logger.info(
                "Bot '%s' bei Rundenende beendet – Manager koordiniert Rejoin",
                self.callsign,
            )
        elif rc == BOT_EXIT_CONN_LOST:
            # Unerwarteter Verbindungsverlust nach erfolgreichem Join – KEIN Absturz, aber
            # der Grund (Server-Nachricht/Socket-Fehler) steht in den letzten Zeilen → Tail
            # mit ausgeben, damit die Ursache sichtbar ist.
            tail = "\n".join(recent)
            bot_logger.warning(
                "Bot '%s' hat die Verbindung verloren (kein Absturz). Letzte Ausgabe:\n%s",
                self.callsign, tail,
            )
        else:
            tail = "\n".join(recent)
            bot_logger.warning(
                "Bot '%s' unerwartet beendet (Exit-Code %s). Letzte Ausgabe:\n%s",
                self.callsign, rc, tail,
            )
        cb = self.on_exit
        if cb:
            try:
                cb(self, rc)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Manager-Hauptklasse
# ---------------------------------------------------------------------------

class BotManager:
    """
    Überwacht den BZFlag-Server und verwaltet die Anzahl aktiver Bots.
    """

    def __init__(self, config: Config):
        self.config   = config
        self.bots:    List[BotProcess] = []
        self._lock    = threading.Lock()
        # R3: serialisiert die Management-Operationen (_rebalance/_rotate_expired_bots)
        # UNTEREINANDER inkl. ihrer blockierenden stop()/_start_bot()-Phasen — _rebalance
        # läuft auch auf Log-Threads (_on_bot_status) und würde sonst parallel zum
        # Manager-Thread doppelt starten/stoppen. self._lock schützt weiterhin nur die
        # Daten und wird NIE über blockierende Aufrufe gehalten.
        self._rebalance_lock = threading.Lock()
        self._running = False
        self._observer: Optional[ServerObserver] = None
        self._human_count = 0       # aktive Spieler (Team ≠ Observer)
        self._observer_count = 0    # echte Zuschauer (menschliche Observer)
        # Abräum-Timeout: Zeitpunkt (monotonic), an dem die echte Präsenz zuletzt auf 0
        # fiel; None = aktuell Präsenz vorhanden. Steuert das verzögerte Abräumen auf min_bots.
        self._presence_lost_at: Optional[float] = None
        # Bot-Status als primäre Info-Quelle: Zeitpunkt der letzten Bot-Statusmeldung.
        self._last_status_at = 0.0
        # Reject-/Crash-Backoff gegen Hot-Restart-Loop.
        self._failure_count = 0
        self._next_start_allowed = 0.0
        # Rundenende-Koordination: _round_over_seen wird bei der Rundenende-Flanke gesetzt
        # (Bot meldet game_over ODER endet mit BOT_EXIT_ROUND_OVER); _round_restart_active
        # unterdrückt während des Zyklus normale Rebalance/Restart-Logik und Re-Trigger.
        self._round_over_seen = False
        self._round_restart_active = False

    def run(self):
        """Startet den Manager (blockiert bis SIGINT/SIGTERM)."""
        self._running = True
        signal.signal(signal.SIGINT,  self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info(
            "BotManager gestartet – Server: %s:%d | max_bots=%d min_bots=%d",
            self.config.host, self.config.port,
            self.config.max_bots, self.config.min_bots,
        )

        while self._running:
            now = time.monotonic()

            # Rundenende-Flanke: koordinierter Neustart aller Bots (Server kurz leeren →
            # erster Rejoin startet via checkGameOn() einen neuen Zeit-Countdown). Läuft
            # bedingungslos auf die Flanke, ohne Humans-Check; bei Menschen erreicht der
            # Server schlicht nicht count()==0 (kein neuer Timer, Spiel läuft weiter).
            if self._consume_round_over_flag():
                self._round_restart()
                continue

            # Info-Quelle wählen: liefert ein Bot aktuelle Spielerzahlen, brauchen wir
            # KEINEN Observer (und trennen ihn). Sonst (kein/kein meldender Bot) verbindet
            # der Observer als Fallback.
            if self._bots_reporting(now):
                self._disconnect_observer_if_any()
            elif self._observer is None or not self._observer.is_connected:
                self._connect_observer()

            # Backoff zurücksetzen, wenn ein Bot lange genug „gesund" läuft
            self._reset_backoff_if_healthy(now)

            # Bot-Anzahl anpassen
            self._rebalance()

            # Abgestürzte Bots erkennen und neu starten
            self._restart_crashed_bots()

            # Bots mit abgelaufener Lebensdauer rotieren
            self._rotate_expired_bots()

            # Jedem Bot die aktuelle Peer-Liste mitteilen
            self._broadcast_bot_list()

            time.sleep(self.config.check_interval)

        self._shutdown()

    def _consume_round_over_flag(self) -> bool:
        """True (einmalig) wenn seit der letzten Abfrage eine Rundenende-Flanke auftrat."""
        with self._lock:
            if self._round_over_seen and not self._round_restart_active:
                self._round_over_seen = False
                return True
            return False

    def _round_restart(self):
        """Koordinierter Neustart bei Rundenende: ALLE Bots trennen, kurz warten (Server
        registriert die Trennungen → count()==0), dann gewünschte Anzahl frisch starten.
        Der erste Rejoin trifft auf count()==0 → checkGameOn() startet den Zeit-Countdown
        und nullt die Team-Scores (Team 0→1). Während des Zyklus pausieren Observer-Fallback
        und normale Rebalance-/Restart-Logik (Guards via _round_restart_active)."""
        self._round_restart_active = True
        try:
            logger.info("Rundenende erkannt – koordinierter Neustart aller Bots (Server leeren)")

            # Observer trennen – er zählt sonst als Spieler und blockiert count()==0.
            self._disconnect_observer_if_any()

            # Gewünschte Anzahl VOR dem Stoppen bestimmen (Präsenz-Modell, Spieler behalten Vorrang).
            desired = self._desired_bot_count(time.monotonic())

            # Snapshot VOR dem Leeren ziehen — sonst prüft die Warteschleife unten
            # eine bereits geleerte Liste und wartet nie auf echte Prozess-Enden.
            procs = list(self.bots)

            # Alle Bots herunterfahren (bereits beendete überspringt stop() von selbst).
            for bot in procs:
                bot.stop()
            with self._lock:
                self.bots.clear()

            # Sicherstellen, dass wirklich kein Bot mehr läuft, bevor wir die Lücke öffnen.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and any(b.is_alive for b in procs):
                time.sleep(0.1)

            # Lücke: dem Server Zeit geben, die Trennungen zu registrieren (count()==0).
            logger.info("Alle Bots getrennt – warte %.1fs, dann Rejoin", ROUND_RESTART_GAP_S)
            time.sleep(ROUND_RESTART_GAP_S)

            # Frisch starten: erster MsgEnter → checkGameOn → neuer Countdown.
            logger.info("Starte %d Bot(s) neu für die nächste Runde", desired)
            for _ in range(desired):
                with self._lock:
                    self._start_bot()
        finally:
            # Während des Zyklus evtl. gesetzte Flanke verwerfen (kein Sofort-Re-Trigger).
            with self._lock:
                self._round_over_seen = False
            self._round_restart_active = False

    def _bots_reporting(self, now: float) -> bool:
        """True, wenn mindestens ein Bot lebt und kürzlich Status gemeldet hat
        (Bot ist dann die Info-Quelle, der Observer wird nicht benötigt)."""
        if not any(b.is_alive for b in self.bots):
            return False
        return (now - self._last_status_at) < max(10.0, 3 * 2.0)

    def _disconnect_observer_if_any(self):
        """Trennt den Fallback-Observer, sobald ein Bot die Info-Quelle übernimmt."""
        if self._observer is not None:
            logger.info("Bot liefert Spielerzahl – Observer wird getrennt")
            self._observer.disconnect()
            self._observer = None

    def _signal_handler(self, sig, frame):
        """Setzt _running=False bei SIGINT/SIGTERM."""
        logger.info("Signal %d empfangen – beende Manager...", sig)
        self._running = False

    def _connect_observer(self):
        """Stellt Observer-Verbindung her; wartet reconnect_delay Sekunden bei Fehler."""
        if self._observer is not None:
            self._observer.disconnect()

        obs = ServerObserver(
            config            = self.config,
            on_count_changed  = self._on_observer_counts,
        )
        if obs.connect():
            self._observer = obs
            # Initiale Spieler-/Zuschauerzahl lesen
            self._human_count    = obs.human_count
            self._observer_count = obs.observer_count
            logger.info("Observer verbunden – aktuelle Spieler: %d, Zuschauer: %d",
                        self._human_count, self._observer_count)
        else:
            logger.warning(
                "Observer-Verbindung fehlgeschlagen – versuche erneut in %.0fs",
                self.config.reconnect_delay,
            )
            time.sleep(self.config.reconnect_delay)

    def _on_observer_counts(self, human_count: int, observer_count: int):
        """Callback des Fallback-Observers: neue Spieler-/Zuschauerzahl übernehmen und
        Rebalancing auslösen."""
        with self._lock:
            self._human_count    = human_count
            self._observer_count = observer_count
        logger.info("Präsenz geändert: %d Spieler, %d Zuschauer",
                    human_count, observer_count)
        self._rebalance()

    def _on_bot_status(self, bot: "BotProcess", data: dict):
        """Callback eines Bots (Managed-IPC): aktuelle Spielerzahl übernehmen.
        Bots sind die primäre Info-Quelle; alle sehen dieselbe Serversicht."""
        try:
            humans = int(data.get("humans", 0))
        except (TypeError, ValueError):
            return
        try:
            observers = int(data.get("observers", 0))
        except (TypeError, ValueError):
            observers = self._observer_count
        game_over = bool(data.get("game_over", False))
        bot.game_over = game_over
        with self._lock:
            changed = (humans != self._human_count) or (observers != self._observer_count)
            self._human_count = humans
            self._observer_count = observers
            self._last_status_at = time.monotonic()
            # Rundenende-Flanke (bedingungslos, kein Humans-Check): koordinierter Neustart.
            # Während eines laufenden Zyklus nicht erneut setzen (verhindert Re-Trigger).
            if game_over and not self._round_restart_active:
                self._round_over_seen = True
        if changed:
            logger.info("Präsenz (von Bot '%s'): %d Spieler, %d Zuschauer",
                        bot.callsign, humans, observers)
            self._rebalance()

    def _on_bot_exit(self, bot: "BotProcess", rc: int):
        """Callback bei unerwartetem Bot-Ende: Reject-/Crash-Backoff hochzählen,
        damit ein dauerhaft abgelehnter Slot nicht im Sekundentakt neu startet."""
        if rc == BOT_EXIT_ROUND_OVER:
            # Bewusstes Rundenende-Exit: KEIN Backoff. Stattdessen koordinierten Neustart
            # anstoßen (Fallback, falls die game_over-Statusflanke nicht ankam).
            with self._lock:
                if not self._round_restart_active:
                    self._round_over_seen = True
            return
        with self._lock:
            alive_dur = (time.monotonic() - bot.start_time) if bot.start_time else 0.0
            if alive_dur >= self.config.restart_healthy_s:
                # Lief lange genug → war kein Sofort-Reject; Backoff zurücksetzen.
                self._failure_count = 0
                return
            self._failure_count += 1
            delay = min(
                self.config.restart_backoff_base * (2 ** (self._failure_count - 1)),
                self.config.restart_backoff_max,
            )
            self._next_start_allowed = time.monotonic() + delay
        logger.warning(
            "Bot '%s' früh beendet (nach %.0fs, Code %s) – Neustart-Backoff %.0fs (Fehler #%d)",
            bot.callsign, alive_dur, rc, delay, self._failure_count,
        )

    def _reset_backoff_if_healthy(self, now: float):
        """Setzt den Backoff zurück, sobald ein Bot lange genug stabil läuft."""
        if self._failure_count == 0:
            return
        for bot in self.bots:
            if bot.is_alive and bot.start_time is not None \
                    and (now - bot.start_time) >= self.config.restart_healthy_s:
                logger.debug("Backoff zurückgesetzt – Bot '%s' läuft stabil", bot.callsign)
                self._failure_count = 0
                self._next_start_allowed = 0.0
                return

    def _broadcast_bot_list(self):
        """Schickt jedem lebenden Bot die aktuelle Liste aktiver In-Game-Bot-Callsigns,
        damit jeder Bot seine Peers als Bots (nicht Menschen) erkennt."""
        names = sorted(b.callsign for b in self.bots if b.is_alive)
        for b in self.bots:
            if b.is_alive:
                b.send_command({"type": "bots", "callsigns": names})

    def _desired_bot_count(self, now: float) -> int:
        """Gewünschte Bot-Anzahl nach Präsenz-Modell (nimmt self._lock selbst — NICHT
        unter gehaltenem Lock aufrufen).

        - Keine echte Präsenz (weder Spieler noch Zuschauer): nach idle_cleanup_delay
          auf min_bots abräumen; bis dahin aktuelles Niveau halten.
        - Mit Präsenz: min_bots + max_bots − aktive Spieler (nur Team ≠ Observer zählen
          ab; ein einzelner Zuschauer ⇒ volle Arena min_bots+max_bots).
        Als Nebeneffekt wird der Abräum-Timer (_presence_lost_at) nachgeführt."""
        with self._lock:
            human     = self._human_count
            observers = self._observer_count
            active    = sum(1 for b in self.bots if b.is_alive)
            full_pool = self.config.min_bots + self.config.max_bots

            if human + observers > 0:
                self._presence_lost_at = None
                return max(self.config.min_bots, min(full_pool, full_pool - human))

            # Keine echte Präsenz → Abräum-Timer starten/prüfen.
            if self._presence_lost_at is None:
                self._presence_lost_at = now
            if now - self._presence_lost_at >= self.config.idle_cleanup_delay:
                return self.config.min_bots
            # Gnadenfrist: aktuelles Niveau halten (kein Hoch-/Runterskalieren).
            return max(self.config.min_bots, active)

    def _rebalance(self):
        """Passt die Bot-Anzahl an die aktuelle Präsenz an (siehe _desired_bot_count).

        R3: Unter self._lock wird nur entschieden/self.bots mutiert (Auswahl der zu
        stoppenden Bots via _select_bot_to_stop). Die blockierenden bot.stop()- und
        _start_bot()-Aufrufe laufen danach AUSSERHALB des Locks, damit Status-/Exit-
        Callbacks anderer Bots (_on_bot_status/_on_bot_exit, nehmen dasselbe Lock)
        währenddessen nicht blockieren. _rebalance_lock serialisiert die gesamte
        Operation gegen parallele _rebalance-/Rotations-Läufe (z. B. Manager-Thread
        vs. Log-Thread) — sonst berechnen beide dasselbe Defizit und starten doppelt."""
        if self._round_restart_active:
            return  # koordinierter Neustart läuft – nicht dazwischenfunken
        with self._rebalance_lock:
            desired = self._desired_bot_count(time.monotonic())
            to_stop: List[BotProcess] = []
            with self._lock:
                active_bots = [b for b in self.bots if b.is_alive]
                diff = desired - len(active_bots)

                if diff < 0:
                    for _ in range(-diff):
                        bot = self._select_bot_to_stop()
                        if bot is None:
                            break
                        to_stop.append(bot)

            if diff > 0:
                for _ in range(diff):
                    self._start_bot()
            for bot in to_stop:
                bot.stop()

    def _start_bot(self, exclude_name: Optional[str] = None):
        """Startet einen neuen Bot-Prozess.

        exclude_name: diesen (vollen) In-Game-Namen bei Rotation überspringen, damit der
        neue Bot einen anderen Namen bekommt als der gerade ersetzte.

        Respektiert den Reject-/Crash-Backoff: vor Ablauf von _next_start_allowed wird
        nichts gestartet (verhindert Hot-Restart-Loop bei dauerhafter Server-Ablehnung).
        """
        now = time.monotonic()
        if now < self._next_start_allowed:
            return

        prefix   = self.config.bot_name_prefix or ""
        existing = {b.callsign for b in self.bots if b.is_alive}   # volle In-Game-Namen
        full     = self.config.full_bot_callsigns()

        if full:
            # Explizite (geprefixte) Namen: nächsten freien wählen, exclude_name meiden
            candidates = [c for c in full if c not in existing and c != exclude_name]
            if not candidates:
                candidates = [c for c in full if c not in existing]
            if not candidates:
                logger.warning("Alle bot_callsigns belegt – kein Bot gestartet")
                return
            name = candidates[0]
        else:
            # Fallback: Prefix + fortlaufende Nummer
            idx = 1
            while True:
                name = f"{prefix}{idx:02d}"
                if name not in existing:
                    break
                idx += 1

        bot = BotProcess(callsign=name, config=self.config)
        bot.on_status = self._on_bot_status
        bot.on_exit   = self._on_bot_exit
        if bot.start():
            bot.lifetime = random.uniform(
                self.config.bot_lifetime_min, self.config.bot_lifetime_max
            )
            logger.debug("Bot '%s' Lebensdauer: %.0fmin",
                         name, bot.lifetime / 60)
            self.bots.append(bot)
            # Neuer Bot + Peers sofort synchronisieren
            self._broadcast_bot_list()
        time.sleep(1.0)  # Kurze Pause, damit Server nicht überflutet wird

    def _select_bot_to_stop(self) -> Optional["BotProcess"]:
        """Wählt den zuletzt gestarteten lebenden Bot zum Stoppen aus und entfernt ihn
        sofort aus self.bots (unter self._lock aufzurufen) — bewusst gestoppt, damit
        _restart_crashed_bots den Eintrag nicht fälschlich als „Absturz" meldet. Das
        eigentliche (blockierende) bot.stop() liegt beim Aufrufer, AUSSERHALB des Locks
        (R3, siehe _rebalance)."""
        # Bots in umgekehrter Reihenfolge wählen (neuester zuerst)
        for bot in reversed(self.bots):
            if bot.is_alive:
                self.bots.remove(bot)
                return bot
        return None

    def _rotate_expired_bots(self):
        """Ersetzt Bots, deren Lebensdauer abgelaufen ist, durch neue mit anderem Namen.

        R3: Unter self._lock wird nur entschieden, welche Bots rotieren, und self.bots
        mutiert (Snapshot der abgelaufenen Bots). Die blockierenden bot.stop()- und
        _start_bot()-Aufrufe laufen danach AUSSERHALB des Locks; _rebalance_lock
        serialisiert die gesamte Rotation gegen parallele _rebalance-Läufe
        (Begründung siehe _rebalance)."""
        if self._round_restart_active:
            return  # koordinierter Neustart läuft – nicht dazwischenfunken
        with self._rebalance_lock:
            now = time.monotonic()
            expired: List[BotProcess] = []
            with self._lock:
                for bot in list(self.bots):
                    if not bot.is_alive: continue
                    if bot.start_time is None: continue
                    if bot.lifetime <= 0.0: continue
                    if now - bot.start_time < bot.lifetime: continue
                    logger.info("Bot '%s' Lebensdauer abgelaufen (%.0fmin) – rotiere",
                                bot.callsign, bot.lifetime / 60)
                    self.bots.remove(bot)
                    expired.append(bot)

            for bot in expired:
                old_name = bot.callsign
                bot.stop()
                self._start_bot(exclude_name=old_name)

    def _restart_crashed_bots(self):
        """Startet abgestürzte Bots neu (falls sie noch benötigt werden)."""
        if self._round_restart_active:
            return  # koordinierter Neustart läuft – nicht dazwischenfunken
        desired = self._desired_bot_count(time.monotonic())

        crashed = [b for b in self.bots if not b.is_alive]
        for bot in crashed:
            self.bots.remove(bot)
            if bot._stopping:
                logger.debug("Bot '%s' wurde bewusst gestoppt – entfernt", bot.callsign)
            elif bot.last_rc == BOT_EXIT_REJECTED:
                logger.info("Bot '%s' vom Server abgelehnt – entfernt (kein Absturz)", bot.callsign)
            elif bot.last_rc == BOT_EXIT_CONN_LOST:
                logger.info("Bot '%s' hat die Verbindung verloren – entfernt (kein Absturz)",
                            bot.callsign)
            else:
                logger.warning("Bot '%s' war abgestürzt (Exit-Code %s) – entfernt",
                               bot.callsign, bot.last_rc)

        active = sum(1 for b in self.bots if b.is_alive)
        if active < desired:
            logger.info("Starte %d Bot(s) nach", desired - active)
            for _ in range(desired - active):
                with self._lock:
                    self._start_bot()

    def _shutdown(self):
        """Beendet alle Bots sauber."""
        logger.info("Beende alle %d Bots...", sum(1 for b in self.bots if b.is_alive))
        for bot in self.bots:
            bot.stop(timeout=3.0)
        if self._observer:
            self._observer.disconnect()
        logger.info("BotManager beendet.")


# ---------------------------------------------------------------------------
# Kommandozeilen-Einstiegspunkt
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description = "BZFlag Bot-Manager – startet und beendet Bots automatisch",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", metavar="YAML",
                   help="Pfad zur YAML-Konfigurationsdatei")
    p.add_argument("--host",            help="BZFlag-Server-Hostname oder IP")
    p.add_argument("--port",            type=int,   help="Server-Port")
    p.add_argument("--max_bots",        type=int,   help="Maximale Anzahl gleichzeitiger Bots")
    p.add_argument("--min_bots",        type=int,   help="Mindestanzahl aktiver Bots")
    p.add_argument("--bot_name_prefix", help="Präfix, den jeder Bot erhält (auch zur Erkennung)")
    p.add_argument("--bot_callsigns",   help="Kommagetrennte Liste der Bot-Basisnamen (z.B. Zwiebel,Tomate)")
    p.add_argument("--observer_callsign", help="Callsign des Fallback-Observers")
    p.add_argument("--team",            type=int,   help="Team für alle Bots")
    p.add_argument("--motto",           help="Motto für alle Bots")
    p.add_argument("--token",           help="BZFlag-Auth-Token")
    p.add_argument("--world_half",      type=float, help="Halbe Weltgröße")
    p.add_argument("--check_interval",  type=float, help="Sekunden zwischen Rebalance-Prüfungen")
    p.add_argument("--idle_cleanup_delay", type=float,
                   help="Sekunden ohne echte Menschen bis Abräumen auf min_bots (0 = sofort)")
    p.add_argument("--good_flags", help="Kommagetrennte Liste zu behaltender Flags")
    p.add_argument("--bad_flags",  help="Kommagetrennte Liste sofort abzulegender Flags")
    p.add_argument("--log_level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Log-Level")
    return p.parse_args()


def main():
    args = parse_args()

    # Konfiguration laden: YAML-Datei als Basis, dann CLI-Werte überschreiben
    config = Config.from_yaml(args.config) if args.config else Config()

    for attr in ("host", "port", "max_bots", "min_bots", "bot_name_prefix",
                 "observer_callsign",
                 "team", "motto", "token", "world_half", "check_interval",
                 "idle_cleanup_delay", "log_level"):
        cli_val = getattr(args, attr, None)
        if cli_val is not None:
            setattr(config, attr, cli_val)

    # bot_callsigns / good_flags / bad_flags: Komma-String aus CLI → Liste
    if getattr(args, "bot_callsigns", None):
        config.bot_callsigns = [s.strip() for s in args.bot_callsigns.split(",") if s.strip()]
    if getattr(args, "good_flags", None):
        config.good_flags = [s.strip() for s in args.good_flags.split(",") if s.strip()]
    if getattr(args, "bad_flags", None):
        config.bad_flags = [s.strip() for s in args.bad_flags.split(",") if s.strip()]

    logging.basicConfig(
        level   = getattr(logging, config.log_level, logging.INFO),
        format  = "%(asctime)s [%(name)-16s] %(levelname)s: %(message)s",
        datefmt = "%H:%M:%S",
    )

    manager = BotManager(config)
    manager.run()


if __name__ == "__main__":
    main()
