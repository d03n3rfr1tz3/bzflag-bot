#!/usr/bin/env python3
"""BZFlag-2.4-Bot: Entry-Point (CLI, main, Managed-stdin-Reader, Karten-Dump).

Die Bot-Logik lebt im Paket bot/ (Track 4): core.py (BZBot, Game-Loop),
handlers.py (_on_*-Message-Handler), hit_detection.py, ai/ (BZBotAI-Mixins),
constants.py, models.py, util.py. Engine-Schicht (Protokoll/Welt/Physik/Nav):
bzflag/. Gestartet wird weiterhin `python bzbot.py …` — auch vom Bot-Manager.
"""

import argparse
import json
import logging
import sys
import threading
import time

from bzflag.protocol import (DEFAULT_PORT, BOT_EXIT_REJECTED,
                             BOT_EXIT_ROUND_OVER, BOT_EXIT_CONN_LOST,
                             ROUND_RESTART_GAP_S)
from bot.constants import WORLD_HALF_DEFAULT
from bot.core import BZBot

logger = logging.getLogger("bzbot")


# ── CLI ───────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="BZFlag 2.4 Bot",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--host",             default="localhost")
    p.add_argument("--port",             type=int,   default=DEFAULT_PORT)
    p.add_argument("--callsign",         default="Bot")
    p.add_argument("--team",             type=int,   default=0xFFFE)
    p.add_argument("--motto",            default="")
    p.add_argument("--token",            default="")
    p.add_argument("--world-half",       type=float, default=WORLD_HALF_DEFAULT)
    p.add_argument("--bot-name-prefix",  default="Bot_",
                   help="Prefix für Bot-Callsigns zur eigenen Erkennung")
    p.add_argument("--bot-callsigns",    default="",
                   help="Kommagetrennte Liste aller Bot-Callsigns zur Erkennung")
    p.add_argument("--managed",          action="store_true",
                   help="Vom Bot-Manager gestartet: IPC über stdin/stdout aktivieren")
    p.add_argument("--good-flags",       default="",
                   help="Kommagetrennte Liste zu behaltender Flags (leer = Standardliste)")
    p.add_argument("--bad-flags",        default="",
                   help="Kommagetrennte Liste sofort abzulegender Flags (leer = Standardliste)")
    p.add_argument("--limited-flags",    default="",
                   help="Kommagetrennte Flaggen-Kürzel mit Server-Schusslimit (z.B. GM,L)")
    p.add_argument("--log-level",        default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--dump-map",         default="",
                   help="Pfad zur Ausgabedatei für den Karten-Dump (z.B. navgrid_dump.txt)")
    p.add_argument("--dump-raw",         default="",
                   help="Pfad-Präfix für Roh-Dump (erzeugt <pfad>.bin + <pfad>.meta, "
                        "z.B. tests/fixtures/hix)")
    p.add_argument("--debug-no-shoot",    action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--debug-target-flag", default="", metavar="FLAG", help=argparse.SUPPRESS)
    p.add_argument("--debug-no-jump",     action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--debug-log-path",    action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--debug-log-shot",    action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--debug-log-dodge",   action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--debug-log-flag",    action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--debug-log-tele",    action="store_true", help=argparse.SUPPRESS)
    return p.parse_args()


def _start_managed_stdin_reader(current: dict) -> None:
    """Startet einen Daemon-Thread, der JSON-Kommandozeilen vom Bot-Manager über
    stdin liest und an den aktuellen Bot (current['bot']) weiterreicht.

    Erkannte Kommandos:
      {"type":"bots","callsigns":[...]}  → Bot.update_bot_callsigns(...)

    Robust gegen EOF und fehlerhafte Zeilen (stilles Ignorieren); wird nur im
    Managed-Modus gestartet, sodass Standalone-bzbot stdin nie anfasst."""
    def _reader():
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    cmd = json.loads(line)
                except Exception:
                    continue
                bot = current.get("bot")
                if bot is None or not isinstance(cmd, dict):
                    continue
                try:
                    if cmd.get("type") == "bots":
                        bot.update_bot_callsigns(cmd.get("callsigns", []))
                except Exception:
                    pass
        except Exception:
            pass
    threading.Thread(target=_reader, daemon=True, name="mgr-stdin").start()


def main():
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    bot_callsigns = [s.strip() for s in args.bot_callsigns.split(",") if s.strip()] \
        if args.bot_callsigns else []
    good_flags = [s.strip() for s in args.good_flags.split(",") if s.strip()] \
        if args.good_flags else None
    bad_flags = [s.strip() for s in args.bad_flags.split(",") if s.strip()] \
        if args.bad_flags else None
    limited_flags = [s.strip() for s in args.limited_flags.split(",") if s.strip()] \
        if args.limited_flags else None
    dump_path = args.dump_map.strip() if args.dump_map else ""
    raw_dump_prefix = args.dump_raw.strip() if args.dump_raw else ""
    def _make_bot():
        b = BZBot(
            host=args.host, port=args.port, callsign=args.callsign,
            team=args.team, motto=args.motto, token=args.token,
            world_half=args.world_half,
            bot_name_prefix=args.bot_name_prefix,
            bot_callsigns=bot_callsigns,
            managed=args.managed,
            good_flags=good_flags,
            bad_flags=bad_flags,
            limited_flags=limited_flags,
            debug_no_shoot=args.debug_no_shoot,
            debug_target_flag=args.debug_target_flag.upper() if args.debug_target_flag else "",
            debug_no_jump=args.debug_no_jump,
            debug_log_path=args.debug_log_path,
            debug_log_shot=args.debug_log_shot,
            debug_log_dodge=args.debug_log_dodge,
            debug_log_flag=args.debug_log_flag,
            debug_log_tele=args.debug_log_tele,
        )
        # BZBot ist eine mypyc-kompilierte native Klasse (Track 5) — Methoden sind dort
        # nicht per Instanz-Zuweisung überschreibbar. Statt _on_world_ready zu patchen,
        # hängen wir uns über on_world_ready_extra an (von _on_world_ready an jedem
        # Austrittspunkt aufgerufen). b.client.on_world_ready bleibt wie in
        # _init_network gesetzt.
        if dump_path:
            def _dump_extra(wm):
                if wm is not None:
                    _dump_world_map(wm, dump_path, nav=getattr(b, '_nav_graph', None))
            b.on_world_ready_extra = _dump_extra
        if raw_dump_prefix:
            # Komposition: falls dump_path bereits einen Callback gesetzt hat, ruft
            # der neue ihn mit auf — beide Dumps laufen dann wie bisher verkettet.
            _prev_extra = b.on_world_ready_extra
            def _raw_dump_extra(wm):
                if _prev_extra is not None:
                    _prev_extra(wm)
                try:
                    from pathlib import Path as _Path
                    raw = bytes(b.client._world_buf)
                    _Path(raw_dump_prefix + ".bin").write_bytes(raw)
                    _Path(raw_dump_prefix + ".meta").write_text(
                        f"world_half={b.world_half}\n", encoding="utf-8"
                    )
                    logger.info("[PTH] Raw-Dump: %s.bin (%d B)", raw_dump_prefix, len(raw))
                except Exception as exc:
                    logger.warning("[PTH] Raw-Dump fehlgeschlagen: %s", exc)
            b.on_world_ready_extra = _raw_dump_extra
        return b

    # Managed-Modus: EIN prozesslanger stdin-Reader, der Kommandos an den jeweils
    # aktuellen Bot weiterreicht (überlebt Reconnects, vermeidet konkurrierende Reader).
    current = {"bot": None}
    if args.managed:
        _start_managed_stdin_reader(current)

    while True:
        bot = _make_bot()
        current["bot"] = bot
        try:
            success = bot.start()
        except KeyboardInterrupt:
            print()
            logger.info("Abbruch durch Benutzer")
            bot.stop()
            break
        else:
            # Endstand-Linger passiert in der Spielschleife (Explosion via PS_EXPLODING-Updates,
            # damit der Bot in der Endstand-Tabelle sichtbar bleibt ohne [nr]); start() kehrt
            # erst nach Ablauf der Linger-Phase zurück. Reconnect nur bei _reconnect_needed.
            bot.stop()
            if not success:
                # Server-Ablehnung (Kapazität voll o.ä.) mit eigenem Exit-Code melden, damit
                # der Bot-Manager sie nicht als Absturz wertet; sonst generischer Fehler.
                sys.exit(BOT_EXIT_REJECTED if bot._join_rejected else 1)
            if bot._round_over:
                if args.managed:
                    # Rundenende: NICHT selbst (unsynchronisiert) reconnecten — beenden und dem
                    # Manager das koordinierte Leave-and-Rejoin überlassen (kurzer count()==0-
                    # Moment → neuer Zeit-Countdown). Eigener Exit-Code, damit kein Crash-Backoff.
                    logger.info("Rundenende — beende Prozess (Manager koordiniert Rejoin)")
                    sys.exit(BOT_EXIT_ROUND_OVER)
                # Standalone: dem Server Zeit geben, die Trennung zu registrieren (count()==0),
                # bevor wir wieder beitreten — sonst greift checkGameOn() nicht.
                logger.info("Rundenende — warte %.1fs vor Reconnect (Server leeren)",
                            ROUND_RESTART_GAP_S)
                time.sleep(ROUND_RESTART_GAP_S)
            if bot._connection_lost and not bot._reconnect_needed:
                # Unerwarteter Verbindungsverlust nach erfolgreichem Join (kein Reconnect
                # angefordert): eigener Exit-Code, damit der Manager es als Netz-/Server-
                # Ereignis beschriften kann statt als Absturz (Exit 0 sähe wie ein sauberes
                # Ende aus, Exit 1 wie ein Crash).
                sys.exit(BOT_EXIT_CONN_LOST)
            if not bot._reconnect_needed:
                break


def _dump_world_map(wm, path: str, nav=None) -> None:
    """Schreibt WorldMap als ASCII-Grid (1 Block pro NavGraph-Layer) + Obstacle-Liste."""
    import math as _math
    cell = 5.0
    half = wm.world_half
    n = int(2 * half / cell)

    def world_to_gi(wx, wy):
        xi = int((wx + half) / cell)
        yi = int((wy + half) / cell)
        return max(0, min(n - 1, xi)), max(0, min(n - 1, yi))

    def new_grid():
        return [["." for _ in range(n)] for _ in range(n)]

    def mark_box_aabb(grid, b, char):
        cos_a = abs(b.cos_a)
        sin_a = abs(b.sin_a)
        ext_x = b.half_w * cos_a + b.half_d * sin_a
        ext_y = b.half_w * sin_a + b.half_d * cos_a
        x0, y0 = world_to_gi(b.cx - ext_x, b.cy - ext_y)
        x1, y1 = world_to_gi(b.cx + ext_x, b.cy + ext_y)
        for xi in range(x0, x1 + 1):
            for yi in range(y0, y1 + 1):
                if 0 <= xi < n and 0 <= yi < n:
                    grid[yi][xi] = char

    try:
        with open(path, "w", encoding="utf-8") as f:
            n_z_groups = 1 + (len(set(round(l.z, 1) for l in nav.layers[1:])) if nav else 0)
            f.write(f"# WorldMap-Dump: world_half={half}u  cell={cell}u  grid={n}×{n}"
                    f"  Layer: {n_z_groups}\n")
            f.write(f"# Boxes/Pyramiden: {len(wm.boxes)}  Teleporter: {len(wm.teleporters)}"
                    f"  Links: {len(wm.links)}\n")

            # ── Layer z=0 (Boden) ──
            g0 = new_grid()
            for b in wm.boxes:
                mark_box_aabb(g0, b, "#")
            for t in wm.teleporters:
                xi, yi = world_to_gi(t.cx, t.cy)
                if 0 <= xi < n and 0 <= yi < n:
                    g0[yi][xi] = "T"
            f.write(f"\n# Layer z=0.0u (Boden) — {n}×{n} Zellen ({cell}u/Zelle):\n")
            for row in reversed(g0):
                f.write("".join(row) + "\n")

            # ── NavGraph Dachlayer (nach z-Höhe gruppiert) ──
            if nav is not None:
                from collections import defaultdict as _defaultdict
                z_groups = _defaultdict(list)
                for layer in nav.layers[1:]:
                    z_groups[round(layer.z, 1)].append(layer)
                for z_val in sorted(z_groups):
                    layers_at_z = z_groups[z_val]
                    gr = new_grid()
                    total_walkable = 0
                    for layer in layers_at_z:
                        for iy in range(layer.n_y):
                            for ix in range(layer.n_x):
                                if layer.walkable[iy][ix]:
                                    wx, wy = layer.cell_to_world(ix, iy)
                                    gx, gy = world_to_gi(wx, wy)
                                    if 0 <= gx < n and 0 <= gy < n and gr[gy][gx] == ".":
                                        gr[gy][gx] = "#"
                                        total_walkable += 1
                    n_sub = len(layers_at_z)
                    f.write(f"\n# Layer z={z_val:.1f}u"
                            f" ({total_walkable} begehbare Zellen, {n_sub} Obstacles)"
                            f" — {n}×{n} Zellen ({cell}u/Zelle):\n")
                    for row in reversed(gr):
                        f.write("".join(row) + "\n")

            # ── Obstacle-Liste ──
            f.write("\n# Obstacles:\n")
            for b in wm.boxes:
                f.write(
                    f"  BOX  cx={b.cx:7.1f}  cy={b.cy:7.1f}  z={b.bottom_z:.1f}"
                    f"  hw={b.half_w:.1f}  hd={b.half_d:.1f}  h={b.height:.1f}"
                    f"  angle={_math.degrees(b.angle):.0f}°"
                    f"{'  driveThru' if b.drive_through else ''}\n"
                )
            for t in wm.teleporters:
                f.write(
                    f"  TELE name={t.name!r}  cx={t.cx:7.1f}  cy={t.cy:7.1f}"
                    f"  angle={_math.degrees(t.angle):.0f}°\n"
                )
        logger.info("[PTH] Karten-Dump geschrieben: %s  (%d Layer)", path, n_z_groups)
    except OSError as exc:
        logger.warning("[PTH] Karten-Dump fehlgeschlagen: %s", exc)


if __name__ == "__main__":
    main()
