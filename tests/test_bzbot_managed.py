"""
Tests für den Managed-Modus von bzbot.py (IPC mit dem Bot-Manager).

- update_bot_callsigns(): vom Manager gepushte Peer-Liste stuft bekannte Spieler
  von Mensch auf Bot herab (nie umgekehrt).
- _emit_status(): erzeugt nur im Managed-Modus eine @@BZMGR@@-stdout-Zeile.
- _is_bot_callsign(): kombinierte Erkennung (eigener Name / Liste / Prefix).
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bzflag.protocol import MGR_STATUS_PREFIX


def _make_bot(managed=True, prefix="[b0t] ", callsigns=None):
    with patch("bot.core.BZFlagClient"):
        from bot.core import BZBot
        return BZBot(host="localhost", port=5154, callsign="[b0t] Zwiebel",
                     bot_name_prefix=prefix, bot_callsigns=callsigns or [],
                     managed=managed)


def _add_player(bot, pid, callsign, team=2, is_human=True):
    from bot.models import PlayerInfo
    bot.players[pid] = PlayerInfo(callsign=callsign, team=team, is_human=is_human)


# ── update_bot_callsigns ────────────────────────────────────────────────────

def test_update_bot_callsigns_demotes_peer_to_bot():
    bot = _make_bot(prefix="ZZ_", callsigns=[])   # Prefix trifft "Tomate" nicht
    _add_player(bot, 5, "Tomate", is_human=True)
    bot.human_count = 1
    assert bot.players[5].is_human is True
    bot.update_bot_callsigns(["Tomate"])           # Manager: Tomate ist ein Bot
    assert bot.players[5].is_human is False
    assert bot.human_count == 0


def test_update_bot_callsigns_keeps_real_humans():
    bot = _make_bot(prefix="ZZ_", callsigns=[])
    _add_player(bot, 7, "Alice", is_human=True)
    bot.human_count = 1
    bot.update_bot_callsigns(["Tomate"])           # Alice nicht enthalten
    assert bot.players[7].is_human is True
    assert bot.human_count == 1


def test_update_bot_callsigns_never_promotes_bot_to_human():
    bot = _make_bot(prefix="[b0t] ", callsigns=[])
    _add_player(bot, 9, "[b0t] Gurke", is_human=False)  # bereits Bot
    bot.human_count = 0
    bot.update_bot_callsigns([])                    # leere Liste
    assert bot.players[9].is_human is False         # bleibt Bot (Prefix greift)
    assert bot.human_count == 0


# ── _is_bot_callsign ────────────────────────────────────────────────────────

def test_is_bot_callsign_combined():
    bot = _make_bot(prefix="[b0t] ", callsigns=["[b0t] Tomate"])
    assert bot._is_bot_callsign("[b0t] Zwiebel")   # eigener Name
    assert bot._is_bot_callsign("[b0t] Tomate")    # in Liste
    assert bot._is_bot_callsign("[b0t] Gurke")     # über Prefix
    assert not bot._is_bot_callsign("Alice")


# ── _emit_status ────────────────────────────────────────────────────────────

def test_emit_status_managed_emits_line(capsys):
    bot = _make_bot(managed=True)
    _add_player(bot, 3, "Alice", is_human=True)
    _add_player(bot, 4, "Bob",   is_human=True)
    _add_player(bot, 5, "[b0t] Tomate", is_human=False)
    bot._emit_status()
    lines = [l for l in capsys.readouterr().out.splitlines()
             if l.startswith(MGR_STATUS_PREFIX)]
    assert len(lines) == 1
    data = json.loads(lines[0][len(MGR_STATUS_PREFIX):].strip())
    assert data["type"] == "status"
    assert data["humans"] == 2
    assert set(data["players"]) == {"Alice", "Bob"}


def test_emit_status_standalone_is_noop(capsys):
    bot = _make_bot(managed=False)
    _add_player(bot, 3, "Alice", is_human=True)
    bot._emit_status()
    assert MGR_STATUS_PREFIX not in capsys.readouterr().out


def test_emit_status_reports_game_over_flag(capsys):
    """_emit_status meldet game_over (Rundenende-Flanke) an den Manager."""
    bot = _make_bot(managed=True)
    bot._emit_status()
    data = _status(capsys)
    assert data["game_over"] is False
    bot._game_over = True
    bot._emit_status()
    assert _status(capsys)["game_over"] is True


def _status(capsys):
    line = [l for l in capsys.readouterr().out.splitlines()
            if l.startswith(MGR_STATUS_PREFIX)][-1]
    return json.loads(line[len(MGR_STATUS_PREFIX):].strip())


def test_notify_count_emits_status_when_managed(capsys):
    bot = _make_bot(managed=True)
    _add_player(bot, 3, "Alice", is_human=True)
    bot.human_count = 1
    bot._notify_count()
    assert MGR_STATUS_PREFIX in capsys.readouterr().out


# ── Reject-Erkennung für Exit-Code-Klassifizierung ──────────────────────────

def test_start_sets_join_rejected_on_server_reject():
    """start(): wird der Join abgelehnt (last_reject_reason >= 0), merkt sich der Bot
    _join_rejected=True, damit main() BOT_EXIT_REJECTED statt eines Crash-Codes meldet."""
    bot = _make_bot(managed=True)
    bot.client.connect.return_value = True
    bot.client.join_game.return_value = False
    bot.client.last_reject_reason = 0x0005       # ServerFull
    assert bot.start() is False
    assert bot._join_rejected is True


def test_start_join_rejected_false_on_connect_failure():
    """Verbindungsfehler (kein Reject) ⇒ _join_rejected bleibt False (generischer Fehler)."""
    bot = _make_bot(managed=True)
    bot.client.connect.return_value = False       # gar nicht erst verbunden
    assert bot.start() is False
    assert bot._join_rejected is False


# ── Rundenende: _round_over-Flag ────────────────────────────────────────────

def test_round_over_flag_set_after_playing():
    """Echtes Rundenende NACH dem Spielen setzt _round_over (Managed→Exit/Standalone→Gap)."""
    bot = _make_bot(managed=True)
    bot._has_spawned = True
    assert bot._round_over is False
    bot._begin_round_over("Rundenende (timeLeft=0)")
    assert bot._round_over is True
    assert bot._reconnect_needed is True


def test_round_over_flag_not_set_when_joining_between_rounds():
    """Beitritt zwischen Runden (noch nie gespielt) setzt _round_over NICHT (kein Reconnect)."""
    bot = _make_bot(managed=True)
    bot._has_spawned = False
    bot._begin_round_over("Rundenende beim Beitritt")
    assert bot._round_over is False
    assert bot._reconnect_needed is False
