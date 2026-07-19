"""Zielwahl: Gegner-Scoring, Ziel-Validierung/Staleness und Flaggen-Route (W4, FABLE-PLAN Teil 3)."""

import math
import random
import struct
import time
import logging

from bzflag.protocol import MsgGrabFlag
from bot.constants import (
    TANK_RADIUS,
    WP_TIMEOUT_BASE,
    WP_TIMEOUT_SCALE,
    UNREACH_AVOID_PENALTY,
    ST_GM_PENALTY,
    ENEMY_STALE_S,
)
from bot.models import PlayerInfo
from bot.util import _angle_diff

logger = logging.getLogger("bzbot")


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class TargetingMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

    def _is_foe(self, info: "PlayerInfo", in_sight: bool) -> bool:
        """Wahrnehmungsbasierte Feind-Erkennung — respektiert CB und MQ wie ein echter Spieler."""
        my = self.team if self.team not in (0xFFFF, 0xFFFE) else 0
        if my == 0:
            return True
        if self.own_flag == "CB":
            return True
        if not in_sight and info.flag == "MQ" and self.own_flag != "SE":
            return False
        return my != info.team

    def _genocide_multikill_possible(self) -> bool:
        """True wenn min. ein Feind-Team > 1 lebenden Spieler hat (G-Flagge lohnt sich)."""
        team_alive: dict = {}
        # list()-Snapshot: players/flags werden im Recv-Thread mutiert (Join/Leave,
        # Flag-Updates), die KI läuft im Game-Loop-Thread → Iterationen über diese
        # Dicts hier und im Rest der Datei immer über eine Kopie (Konvention s. DEVELOPER.md).
        for pid, info in list(self.players.items()):
            if pid == self.player_id or not info.alive:
                continue
            if not self._is_foe(info, True):
                continue
            team_alive[info.team] = team_alive.get(info.team, 0) + 1
        return any(c > 1 for c in team_alive.values())

    def _pz_worth_keeping(self) -> bool:
        """P4-FLG-03-Keep-Gate für die PZ-Flagge: behalten nur wenn (a) Teleporter existieren
        (Erreichbarkeit prüft das Manöver per Async-Validierung — schlagen ALLE Tore fehl, setzt
        es _pz_unreachable_until und das Gate fällt hier) und (b) mindestens ein Gegner in
        Radar-Reichweite mehr Punkte (wins − losses, MsgScore) hat als der Bot. Defensive
        Nutzung: vor dem stärkeren Gegner per Zoning in Sicherheit bringen."""
        world_map = self._world_map
        if world_map is None or not world_map.teleporters:
            return False
        _now = time.monotonic()
        if _now < self._pz_unreachable_until:
            return False
        my_score = self._own_wins - self._own_losses
        radar_r = self._effective_radar_range()
        for pid, info in list(self.players.items()):
            if pid == self.player_id or not info.alive or info.paused:
                continue
            if _now - info.last_seen > ENEMY_STALE_S:
                continue
            if info.wins - info.losses <= my_score:
                continue
            if not self._is_foe(info, False):
                continue
            if math.hypot(info.pos[0] - self.pos_x, info.pos[1] - self.pos_y) >= radar_r:
                continue
            if not self._enemy_visible_radar(info):
                continue
            return True
        return False

    def _validate_and_find_target(self) -> None:
        """Validiert target_player (Reichweite/Sicht), sucht neues Ziel falls nötig."""
        if self.target_player is not None:
            ep = self._get_enemy_pos(self.target_player)
            if ep is None:
                self.target_player = None
            else:
                _tx, _ty = ep
                _d = math.hypot(_tx - self.pos_x, _ty - self.pos_y)
                _in_r = _d < self._effective_radar_range()
                _in_s = False
                # F5: Server-Basiswert (_shotRange) statt Konstante. Bewusst OHNE eigene
                # Flaggen-Multiplikatoren (_effective_shot_range): das ist ein Sicht-/
                # Halte-Fenster, kein Waffenwert — Laser (AdVel×1000) würde es sonst
                # auf die ganze Karte ausdehnen. Deckungsgleich zum Zielwahl-Fenster
                # in _find_target_player.
                if _d < self._shot_range:
                    _ang = math.atan2(_ty - self.pos_y, _tx - self.pos_x)
                    _in_s = abs(_angle_diff(_ang, self.azimuth)) < self._effective_fov()
                if not _in_r and not _in_s:
                    self.target_player = None
        if self.target_player is None:
            self.target_player = self._find_target_player()

    def _threat_unseen(self, shooter) -> bool:
        """IB (Geschoss radar-unsichtbar — auch mit SE) bzw. ST (Tank radar-unsichtbar; SE sieht ihn):
        solchen Bedrohungen nur ausweichen, wenn der Bot den Schützen wahrnimmt (Radar mit SE, sonst
        Fenster: FoV + Sicht-LoS)."""
        if shooter is None:
            return False
        if shooter.flag == "ST":
            return not (self._enemy_visible_radar(shooter)          # = SE → sieht Stealth-Tank
                        or self._sees_in_window(shooter, *shooter.pos))
        if shooter.flag == "IB":
            return not self._sees_in_window(shooter, *shooter.pos)  # Geschoss radar-unsichtbar, SE hilft nicht
        return False

    def _find_target_player(self):
        """Wählt das nächste Ziel; None im Passivmodus."""
        # Kampf ist aktiv, sobald ein Mensch anwesend ist (Mitspieler ODER Zuschauer) — nicht nur
        # bei zielbaren Menschen. Peer-Bots auf Gegner-Teams sind gültige Ziele, damit die Bots
        # für Zuschauer lebendig wirken; ohne jeden Menschen (nur eigene Bots) bleibt es passiv.
        if not self._has_presence():
            return None
        best_id = None
        best_score = float("inf")
        _now = time.monotonic()
        for pid, info in list(self.players.items()):
            if pid == self.player_id or not info.alive:
                continue
            if info.paused:                             # pausiert = unverwundbar → kein Neu-Lock
                continue
            if _now - info.last_seen > ENEMY_STALE_S:   # zu lange nicht wahrgenommen → kein Re-Lock
                continue                                # (Gegenstück zu _get_enemy_pos: kein Geist-Lock)
            d = math.hypot(info.pos[0] - self.pos_x, info.pos[1] - self.pos_y)
            in_radar = d < self._effective_radar_range() and self._enemy_visible_radar(info)
            in_sight = False
            # F5: Server-Basiswert (_shotRange) statt Konstante — Sicht-Zielfenster,
            # bewusst ohne Flaggen-Multiplikatoren (s. _validate_and_find_target).
            if d < self._shot_range and self._enemy_visible_window(info):
                angle_to = math.atan2(
                    info.pos[1] - self.pos_y, info.pos[0] - self.pos_x)
                in_sight = (abs(_angle_diff(angle_to, self.azimuth)) < self._effective_fov()
                            and self._has_los_to_point(info.pos[0], info.pos[1],
                                                       info.pos[2] + self._tank_height * 0.5))
            if not in_radar and not in_sight:
                continue
            if not self._is_foe(info, in_sight):
                continue
            # P4-FLG-03: gezoned invertiert sich die PZ-Abwertung — die eigenen (Phantom-)Schüsse
            # treffen NUR gezonte Gegner, alle anderen sind praktisch untreffbar.
            if self.is_phantom_zoned:
                pz_penalty = 1.0 if info.is_phantom_zoned else 5.0
            else:
                pz_penalty = 5.0 if info.is_phantom_zoned and self.own_flag not in ("SB", "SW") else 1.0
            st_gm_penalty = ST_GM_PENALTY if self.own_flag == "GM" and info.flag == "ST" else 1.0
            # Aktuell als unerreichbar gemiedener Gegner: weich deprioritisieren (nicht hart
            # überspringen) — ein erreichbarer Feind wird bevorzugt, der gemiedene aber weiter
            # gewählt, falls er der einzige ist.
            avoid_penalty = UNREACH_AVOID_PENALTY if self._combat_avoid.get(pid, 0.0) > _now else 1.0
            score = d * (0.8 if info.is_human else 1.0) * pz_penalty * st_gm_penalty * avoid_penalty
            if score < best_score:
                best_score = score; best_id = pid
        return best_id

    def _get_enemy_pos(self, pid: int):
        """Gibt (x, y) eines lebenden, kürzlich gesehenen Gegners zurück; sonst None."""
        info = self.players.get(pid)
        if info is None or not info.alive: return None
        if info.paused: return (info.pos[0], info.pos[1])  # Pausierte: Position bekannt → kein Geist-Drop
        if time.monotonic() - info.last_seen > ENEMY_STALE_S: return None
        return (info.pos[0], info.pos[1])

    def _dist_to_target(self) -> float:
        """Euklidische Distanz zum aktuellen Wegpunkt; inf wenn kein Wegpunkt."""
        if not self.target_pos: return float("inf")
        return math.hypot(self.target_pos[0] - self.pos_x,
                          self.target_pos[1] - self.pos_y)

    def _flags_on_route_all(self, gx: float, gy: float,
                            detour: float = 40.0) -> list[tuple[float, float]]:
        """Wie _flags_on_route, aber ohne good_flags-Filter (alle on-ground Flags).
        Für flagless-Modus mit breiterem Detour-Radius."""
        cx, cy = self.pos_x, self.pos_y
        dx, dy = gx - cx, gy - cy
        dist2 = dx * dx + dy * dy
        if dist2 < 1.0:
            return []
        result: list[tuple[float, float, float]] = []
        for fi in list(self.flags.values()):
            if fi.status != 1:
                continue
            fx, fy = fi.pos[0], fi.pos[1]
            t = ((fx - cx) * dx + (fy - cy) * dy) / dist2
            if t <= 0.05 or t >= 0.95:
                continue
            proj_x = cx + t * dx
            proj_y = cy + t * dy
            if math.hypot(fx - proj_x, fy - proj_y) <= detour:
                result.append((t, fx, fy))
        result.sort()
        return [(fx, fy) for _, fx, fy in result]

    def _effective_flag_abbr(self, fi) -> str:
        """P4-FLG-05: Typ einer Boden-Flagge — live (fi.abbr, z.B. bereits per ID
        identifiziert) ODER aus dem Gedächtnis (_flag_knowledge), falls fi.abbr noch
        maskiert ('' — PZ-Platzhalter für unidentifizierte Flaggen) ist."""
        return fi.abbr or self._flag_knowledge.get(fi.flag_id, "")

    def _new_target(self) -> None:
        """Setzt Navigationsziel abhängig vom eigenen Flag-Zustand.
        Kein Flag: bekannt-beste > bekannt-gute > nächste on-ground Flag (P4-FLG-05).
        ID-Flag: gute Flags in _identify_range bevorzugen, ID ggf. ablegen.
        Andere Flag: zufälliger Wegpunkt."""
        self.target_player = None

        # ── Fall A: Bot hat keine Flagge ─────────────────────────────────
        if self.own_flag == "":
            # P4-FLG-05: dreistufige Priorität — bekannt-best > bekannt-gut > nächste-unbekannt.
            best_d: float = float("inf")
            best_pos: tuple[float, ...] | None = None        # nächste beliebige (unbekannt)
            pri_good_d: float = float("inf")
            pri_good_pos: tuple[float, ...] | None = None     # nächste bekannt-gute (nicht best)
            pri_best_d: float = float("inf")
            pri_best_pos: tuple[float, ...] | None = None     # nächste bekannt-beste
            _dropped = self._dropped_neutrals
            _recent  = self._recent_flag_targets
            for fi in list(self.flags.values()):
                if fi.status != 1:
                    continue
                if (round(fi.pos[0]), round(fi.pos[1])) in _recent:
                    continue
                abbr = self._effective_flag_abbr(fi)
                if any(abbr == a and math.hypot(fi.pos[0]-dx, fi.pos[1]-dy) < 20.0
                       for a, dx, dy in _dropped):
                    continue
                # P4-FLG-05: bekannt nicht-gut (schlecht ODER neutral) gar nicht ansteuern.
                if abbr and abbr not in self.good_flags:
                    continue
                d = math.hypot(fi.pos[0] - self.pos_x, fi.pos[1] - self.pos_y)
                if abbr and abbr in self.best_flags:
                    if d < pri_best_d:
                        pri_best_d = d
                        pri_best_pos = (fi.pos[0], fi.pos[1], fi.pos[2])
                elif abbr and abbr in self.good_flags:
                    if d < pri_good_d:
                        pri_good_d = d
                        pri_good_pos = (fi.pos[0], fi.pos[1], fi.pos[2])
                if d < best_d:
                    best_d = d
                    best_pos = (fi.pos[0], fi.pos[1], fi.pos[2])
            if pri_best_pos is not None:
                best_pos = pri_best_pos          # best schlägt alles
            elif pri_good_pos is not None:
                best_pos = pri_good_pos          # gut schlägt unbekannt
            if best_pos is not None:
                self._recent_flag_targets.append((round(best_pos[0]), round(best_pos[1])))
                via = self._flags_on_route_all(best_pos[0], best_pos[1], detour=40.0)
                if via:
                    nav = self._nav_graph
                    blocked = {k for k, v in self._nav_jump_cooldowns.items()
                               if v > time.monotonic()}
                    all_wps: list = []
                    px, py, pz = self.pos_x, self.pos_y, self.pos_z
                    for fx, fy in via:
                        if nav:
                            seg = nav.plan_path(px, py, pz, fx, fy,
                                                blocked_jump_wps=blocked,
                                                lin_accel_eff=self._eff_linear_accel())
                            if seg:
                                all_wps.extend(seg)
                                px, py, pz = seg[-1][0], seg[-1][1], seg[-1][2]
                    if nav:
                        seg = nav.plan_path(px, py, pz,
                                            best_pos[0], best_pos[1],
                                            blocked_jump_wps=blocked,
                                            goal_z=best_pos[2],
                                            lin_accel_eff=self._eff_linear_accel())
                        if seg:
                            all_wps.extend(seg)
                    if all_wps:
                        if len(all_wps) > 1 and not self._is_ahead(all_wps[0][0], all_wps[0][1]):
                            all_wps.pop(0)
                        self._nav_path  = all_wps
                        self._nav_goal  = (best_pos[0], best_pos[1])
                        self.target_pos = (all_wps[0][0], all_wps[0][1])
                        self._wp_start_time = time.monotonic()
                        self._wp_fail_count = 0
                        self._wp_timeout = (WP_TIMEOUT_BASE
                                            + math.hypot(all_wps[0][0] - self.pos_x,
                                                         all_wps[0][1] - self.pos_y)
                                            * WP_TIMEOUT_SCALE)
                        return
                self._plan_path(best_pos[0], best_pos[1], best_pos[2])
                return

        # ── Fall B: Bot hat ID-Flagge ─────────────────────────────────────
        elif self.own_flag == "ID":
            _recent = self._recent_flag_targets
            best_d_good: float = float("inf")
            best_pos_good = None
            for fi in list(self.flags.values()):
                if fi.status != 1:
                    continue
                abbr = self._effective_flag_abbr(fi)   # P4-FLG-05: Live-Typ oder Gedächtnis
                d = math.hypot(fi.pos[0] - self.pos_x, fi.pos[1] - self.pos_y)
                if d < self._identify_range and abbr in self.good_flags and d < best_d_good:
                    if self._debug_log_flag:
                        logger.debug("[%s] Flagge: ID-B1 – gute Flagge %r d=%.1fu (< %.0fu)",
                                     self.callsign, abbr, d, self._identify_range)
                    best_d_good = d
                    best_pos_good = (fi.pos[0], fi.pos[1])
                elif abbr:
                    if self._debug_log_flag:
                        logger.debug("[%s] Flagge: ID-B1 – keine gute Flagge %r d=%.1fu",
                                     self.callsign, abbr, d)
            if best_pos_good is not None:
                d_to_good = math.hypot(best_pos_good[0] - self.pos_x,
                                       best_pos_good[1] - self.pos_y)
                if self._debug_log_flag:
                    logger.debug("[%s] Flagge: ID-B1 – Drop-Kandidat (%.0f,%.0f) d=%.1fu cooldown=%.1fs",
                                 self.callsign, best_pos_good[0], best_pos_good[1], d_to_good,
                                 time.monotonic() - self._last_drop_attempt)
                if time.monotonic() - self._last_drop_attempt > 1.0:
                    self._try_drop_flag()  # ID ablegen, damit gute Flag aufgesammelt werden kann
                if d_to_good > self._wp_reach_radius():
                    self._plan_path(best_pos_good[0], best_pos_good[1])
                # else: bereits am Ziel, warten bis Drop vom Server bestätigt wird
                return
            # Keine gute Flag in Erkennungsradius → nächste unbekannte Flag ansteuern
            if self._debug_log_flag:
                logger.debug("[%s] Flagge: ID-B2 – kein Ziel in %.0fu, scanne %d Flaggen (%d recent)",
                             self.callsign, self._identify_range, len(self.flags), len(_recent))
            best_d = float("inf")
            best_pos = None
            for fi in list(self.flags.values()):
                if fi.status != 1:
                    continue
                if (round(fi.pos[0]), round(fi.pos[1])) in _recent:
                    continue
                d = math.hypot(fi.pos[0] - self.pos_x, fi.pos[1] - self.pos_y)
                abbr = self._effective_flag_abbr(fi)   # P4-FLG-05: Live-Typ oder Gedächtnis
                # Innerhalb _identify_range bereits als nicht-gut erkannte Flags überspringen
                if d < self._identify_range and abbr and abbr not in self.good_flags:
                    continue
                if d < best_d:
                    best_d = d
                    best_pos = (fi.pos[0], fi.pos[1])
            if best_pos is not None:
                if self._debug_log_flag:
                    logger.debug("[%s] Flagge: ID-B2 – Ziel (%.0f,%.0f) d=%.1fu",
                                 self.callsign, best_pos[0], best_pos[1], best_d)
                self._recent_flag_targets.append((round(best_pos[0]), round(best_pos[1])))
                if best_d > self._wp_reach_radius():
                    self._plan_path(best_pos[0], best_pos[1])
                return

        # ── Fall PZ: aktives Zoning-Manöver — Ziel bleibt das gewählte Tor ─
        # (P4-FLG-03; sonst würde jeder _new_target-Aufruf — Pfad-Ende/Stuck — das Manöver mit
        # einem Zufallsziel überschreiben und _pz_maneuver_tick müsste es zurückdrehen.)
        elif self.own_flag == "PZ" and self._pz_escape_active():
            ti = self._pz_target_gate
            world_map = self._world_map
            if (ti is not None and world_map is not None
                    and ti < len(world_map.teleporters)):
                tele = world_map.teleporters[ti]
                self._plan_path(tele.cx, tele.cy)
            # kein Tor gewählt → der nächste _pz_maneuver_tick (10 Hz) wählt eines
            return

        # ── Fall C: Bot hat andere Flagge — zufälliger Wegpunkt ───────────
        h = self.world_half * 0.85
        best_gx = best_gy = 0.0
        best_score = -2.0
        for _ in range(5):
            cx_ = random.uniform(-h, h)
            cy_ = random.uniform(-h, h)
            cand_az = math.atan2(cy_ - self.pos_y, cx_ - self.pos_x)
            score = math.cos(abs(_angle_diff(cand_az, self.azimuth)))
            if score > best_score:
                best_score, best_gx, best_gy = score, cx_, cy_
        self._plan_path(best_gx, best_gy)

    def _check_opportunistic_grab(self, now: float) -> None:
        """Sendet MsgGrabFlag wenn Bot nah an einer onGround-Flag ist."""
        if self.own_flag or self.player_id is None: return
        if now - self._last_grab_attempt < 0.5: return
        grab_r  = self._flag_grab_radius()                          # schleifeninvariant (60-Hz-Pfad)
        ahead_r = max(TANK_RADIUS, self._effective_tank_radius())
        for fi in list(self.flags.values()):
            if fi.status != 1: continue
            if abs(fi.pos[2] - self.pos_z) > 0.5: continue
            # P4-FLG-05: bekannt schlechte Flagge nicht greifen (kein Grab-dann-sofort-Drop).
            abbr = self._effective_flag_abbr(fi)
            if abbr and abbr in self.bad_flags: continue
            d = math.hypot(fi.pos[0] - self.pos_x, fi.pos[1] - self.pos_y)
            if d >= grab_r: continue
            if d > ahead_r and not self._is_ahead(fi.pos[0], fi.pos[1]): continue
            self._last_grab_attempt = now
            self._try_grab_flag(fi.flag_id)
            return

    def _try_grab_flag(self, flag_id: int) -> None:
        """Sendet MsgGrabFlag."""
        if self.player_id is None: return
        self.client.send(MsgGrabFlag, struct.pack(">H", flag_id))
        if self._debug_log_flag:
            logger.debug("[%s] Flagge: MsgGrabFlag gesendet (flag_id=%d)", self.callsign, flag_id)
