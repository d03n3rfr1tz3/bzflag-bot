"""Hit-Detection: Aufloesung eingehender Schuesse (60 Hz), Steamroller-Check und Schuss-Cleanup (W5, FABLE-PLAN Teil 3)."""

import math
import struct
import time
import logging
from typing import Tuple

from bzflag.shot_physics import (_segment_hits_obb_3d, _extend_segment)
from bzflag.protocol import (
    MsgKilled,
)
from bot.constants import (
    TANK_RADIUS,
    RESPAWN_DELAY,
    HIT_RADIUS,
    KILL_REASON_SHOT,
    KILL_REASON_RUNOVER,
    KILL_REASON_GENOCIDED,
)
from bot.models import Shot, AIState
from bot.util import _wrap, _segment_point_dist3d

logger = logging.getLogger("bzbot")


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class HitDetectionMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot.py verschoben (Track 4/W5)."""

    def _hitbox_half_dims(self) -> Tuple[float, float, float]:
        """Halbe Hitbox-Maße (len, w, h) des eigenen Tanks inkl. Flaggen-Skalierung
        (O/T/TH), N-Sonderfall (schmale Quer-Hitbox) und Schussradius-Aufschlag.
        Einzige Quelle für die OBB-Treffertests (F9 — vorher 3× dupliziert)."""
        if   self.own_flag == "O":  sc = self._obese_factor
        elif self.own_flag == "T":  sc = self._tiny_factor
        elif self.own_flag == "TH": sc = self._thief_tiny_factor
        else:                       sc = 1.0
        half_w   = (self._narrow_hw if self.own_flag == "N"
                    else self._tank_width / 2 * sc) + self._shot_radius
        half_len = self._tank_length / 2 * sc + self._shot_radius
        half_h   = self._tank_height / 2 * sc + self._shot_radius
        return half_len, half_w, half_h

    def _instant_shot_hits(self, shooter: int, shot_id: int,
                           px: float, py: float, pz: float,
                           vx: float, vy: float, vz: float,
                           lifetime: float) -> bool:
        """True wenn ein Instant-Schuss (Laser/Thief) den eigenen Tank trifft.

        Mit Pfad-Cache: OBB-Test über alle Segmente (Abpraller/Teleporter);
        ohne Cache: Punkt-zu-Strahl-Abstand über die gerade Linie.
        (F9: vorher zwei nahezu identische Blöcke für Laser und Thief.)"""
        segs = self._ricochet_paths.get((shooter, shot_id))
        if segs:
            half_len, half_w, half_h = self._hitbox_half_dims()
            tcx = self.pos[0]; tcy = self.pos[1]
            tcz = self.pos[2] + self._tank_height / 2
            for seg in segs:
                if _segment_hits_obb_3d(seg.px, seg.py, seg.pz,
                                         seg.ex, seg.ey, seg.ez,
                                         tcx, tcy, tcz, self.azimuth,
                                         half_len, half_w, half_h):
                    return True
            return False
        speed_xyz = math.sqrt(vx**2 + vy**2 + vz**2)
        if speed_xyz <= 0:
            return False
        dnx, dny, dnz = vx / speed_xyz, vy / speed_xyz, vz / speed_xyz
        shot_range = speed_xyz * lifetime
        cx = self.pos[0]; cy = self.pos[1]; cz = self.pos[2] + self._tank_height / 2
        dx, dy, dz = cx - px, cy - py, cz - pz
        t_proj = dx * dnx + dy * dny + dz * dnz
        if not (0.0 <= t_proj <= shot_range):
            return False
        perpx = dx - dnx * t_proj
        perpy = dy - dny * t_proj
        perpz = dz - dnz * t_proj
        return math.sqrt(perpx**2 + perpy**2 + perpz**2) < HIT_RADIUS

    def _resolve_incoming_shots(self, now: float, dt: float) -> None:
        """Prüft alle aktiven Schüsse auf Treffer; behandelt auch SH-Absorption
        (überlebt) und TH-Flaggendiebstahl (kein Kill). Siehe DEVELOPER.md §7."""
        # N2: Leerlauf-Early-Out (läuft 60 Hz) — Truthiness-Check ist GIL-sicher
        # ohne Lock. Die Check-Referenz muss weiterlaufen, damit Prüf-Fenster
        # und Relativ-Sweep beim ersten Schuss genauso aufsetzen wie bisher.
        if not self._shots:
            self._last_hit_check_t   = now
            self._last_hit_check_pos = (self.pos[0], self.pos[1], self.pos[2])
            return
        tank_cx = self.pos[0]
        tank_cy = self.pos[1]
        tank_cz = self.pos[2] + self._tank_height / 2
        eff_r   = self._effective_hit_radius()
        # Teil C: echtes Prüf-Fenster [letzter Check, now] statt des per Stall-Clamp
        # geklemmten dt — der Segment-Test ist für beliebig lange Segmente exakt,
        # der Clamp (Hauptschleife :525) würde hier nur Abdeckung abschneiden.
        # Dazu die Eigenbewegung im Fenster (Relativ-Sweep wie Client-relativeRay):
        # Schuss-Startpunkt wird in den Tank-Frame von `now` verschoben, die OBB
        # bleibt bei `now` stehen. Guard: unplausibel großer Sprung (eigener
        # Teleport) → statisch testen wie bisher.
        # Mindestens dt zurückschauen UND alles seit dem letzten Check abdecken:
        # Überlappung ist harmlos (idempotenter Segment-Test), Lücken nicht.
        win_start = min(self._last_hit_check_t, now - dt)
        if win_start < now - 60.0:
            # Sanity-Cap (fremde Zeitbasis/sehr alte Referenz, z.B. Test-Uhren)
            win_start = now - 60.0
            self._last_hit_check_pos = None
        window    = now - win_start
        inv_window = 1.0 / window if window > 1.0e-6 else 0.0
        own_dx = own_dy = own_dz = 0.0
        if self._last_hit_check_pos is not None and inv_window > 0.0:
            own_dx = self.pos[0] - self._last_hit_check_pos[0]
            own_dy = self.pos[1] - self._last_hit_check_pos[1]
            own_dz = self.pos[2] - self._last_hit_check_pos[2]
            _max_plaus = 2.0 * self._tank_speed * window + 5.0
            if own_dx*own_dx + own_dy*own_dy + own_dz*own_dz > _max_plaus*_max_plaus:
                own_dx = own_dy = own_dz = 0.0
        self._last_hit_check_t   = now
        self._last_hit_check_pos = (self.pos[0], self.pos[1], self.pos[2])
        # Einmal pro Tick statt pro Schuss: die eigene Flagge (und damit die Hitbox-Masse)
        # ändert sich nicht innerhalb eines Ticks.
        _half_len, _half_w, _half_h = self._hitbox_half_dims()
        with self._shots_lock:
            to_remove = []; hit_shot = None
            for key, shot in self._shots.items():
                if shot.is_expired(now):
                    to_remove.append(key); continue
                if shot.is_thief:
                    to_remove.append(key); continue
                if shot.is_sw:
                    if shot.shooter_id == self.player_id:
                        continue
                    sx, sy, sz = shot.pos[0], shot.pos[1], shot.pos[2]
                    _sw_dist = math.sqrt((sx-tank_cx)**2 + (sy-tank_cy)**2 + (sz-tank_cz)**2)
                    sw_elapsed = now - shot.fire_time
                    # sw_front: wie weit die Shockwave-Kugelfront inzwischen expandiert ist
                    sw_front = self._shock_in_radius + sw_elapsed * self._sw_expand_speed
                    # Treffer: Bot liegt zwischen Innenkugel und der aktuellen Außenfront
                    hit = self._shock_in_radius < _sw_dist < min(sw_front, self._shock_out_radius)
                else:
                    if shot.is_gm:
                        if shot.shooter_id == self.player_id:
                            # Bewusste Vereinfachung (F8): Im echten BZFlag kann die
                            # eigene Rakete einen treffen — praktisch aber nur mit
                            # stark veränderten Server-Variablen oder extrem
                            # ungünstigen Teleporter-Schüssen erreichbar → ignoriert.
                            continue
                        prev_x, prev_y, prev_z = shot.pos[0], shot.pos[1], shot.pos[2]
                        gm_tx = gm_ty = gm_tz = None
                        if shot.gm_target_pid is not None and shot.gm_target_pid != 255:
                            if shot.gm_target_pid == self.player_id:
                                gm_tx, gm_ty, gm_tz = tank_cx, tank_cy, tank_cz
                            else:
                                _p = self.players.get(shot.gm_target_pid)
                                if _p is not None:
                                    gm_tx = _p.pos[0]; gm_ty = _p.pos[1]
                                    gm_tz = _p.pos[2] + self._tank_height / 2
                        # gm_tx/gm_ty/gm_tz werden immer gemeinsam gesetzt (Tupel oben) — alle
                        # drei prüfen, damit mypy die Narrowing-Lücke schließt (Track 5 M2a:
                        # players jetzt typisiert, _p.pos[i] kein Any mehr).
                        if gm_tx is not None and gm_ty is not None and gm_tz is not None:
                            _dx = gm_tx - prev_x; _dy = gm_ty - prev_y; _dz = gm_tz - prev_z
                            _dist = math.sqrt(_dx*_dx + _dy*_dy + _dz*_dz)
                            if _dist > 1e-6:
                                _spd = math.sqrt(shot.vel[0]**2 + shot.vel[1]**2 + shot.vel[2]**2)
                                if _spd < 1e-6: _spd = self._shot_speed
                                # GM dreht pro Tick maximal GM_TURN_RATE Richtung Ziel
                                # (BZFlag: ≈ 36°/s — langsam genug zum Ausweichen)
                                cur_az = math.atan2(shot.vel[1], shot.vel[0])
                                cur_el = math.atan2(shot.vel[2], math.hypot(shot.vel[0], shot.vel[1]))
                                tgt_az = math.atan2(_dy, _dx)
                                tgt_el = math.atan2(_dz, math.hypot(_dx, _dy))
                                max_turn = self._gm_turn_angle * dt
                                # Winkelabstand clampen: nicht schneller drehen als erlaubt
                                d_az = _wrap(tgt_az - cur_az)
                                new_az = cur_az + max(-max_turn, min(max_turn, d_az))
                                d_el = _wrap(tgt_el - cur_el)
                                new_el = cur_el + max(-max_turn, min(max_turn, d_el))
                                # Neue Flugrichtung: gleiche Geschwindigkeit, neuer Winkel
                                shot.vel[0] = math.cos(new_az) * math.cos(new_el) * _spd
                                shot.vel[1] = math.sin(new_az) * math.cos(new_el) * _spd
                                shot.vel[2] = math.sin(new_el) * _spd
                        shot.pos[0] += shot.vel[0] * dt
                        shot.pos[1] += shot.vel[1] * dt
                        shot.pos[2] += shot.vel[2] * dt
                        # Wand-Occlusion (Sicherheitsnetz): die lokale GM-Integration kennt keine
                        # Wände. Steckt eine solide Wand zwischen Rakete und Tank, zählt kein
                        # Treffer (rundet die Rakete die Wand später, greift er dann). Ohne NavGraph
                        # liefert _segment_clear True → Verhalten wie bisher.
                        hit = (_segment_point_dist3d(prev_x, prev_y, prev_z,
                                                     shot.pos[0], shot.pos[1], shot.pos[2],
                                                     tank_cx, tank_cy, tank_cz) < eff_r
                               and self._segment_clear(shot.pos[0], shot.pos[1], shot.pos[2],
                                                       tank_cx, tank_cy, tank_cz))
                    else:
                        prev_t = max(shot.fire_time, win_start)
                        # Teil B: SB-Längskapsel — Segment beidseitig um _shotRadius
                        # verlängern (Längsreichweite 2×, seitlich unverändert).
                        _sb_extra = (self._shot_radius
                                     if shot.flag_abbr == b"SB" else 0.0)
                        segs = self._ricochet_paths.get(
                            (shot.shooter_id, shot.shot_id))
                        if segs:
                            # Abpraller/Teleporter: gecachte Segmente gegen Tank-OBB prüfen
                            hit = False
                            for _seg_idx, seg in enumerate(segs):
                                if shot.shooter_id == self.player_id and _seg_idx == 0:
                                    continue  # Direktsegment eigener Schuss nie Selbsttreffer
                                t0 = max(seg.t_start, prev_t)
                                t1 = min(seg.t_end, now)
                                if t1 <= t0:
                                    continue
                                dur = seg.t_end - seg.t_start
                                if dur < 1.0e-9:
                                    continue
                                f0 = (t0 - seg.t_start) / dur
                                f1 = (t1 - seg.t_start) / dur
                                ax = seg.px + (seg.ex - seg.px) * f0
                                ay = seg.py + (seg.ey - seg.py) * f0
                                az = seg.pz + (seg.ez - seg.pz) * f0
                                bx = seg.px + (seg.ex - seg.px) * f1
                                by = seg.py + (seg.ey - seg.py) * f1
                                bz = seg.pz + (seg.ez - seg.pz) * f1
                                # Relativ-Sweep: Endpunkt zur Zeit t um die seither
                                # erfolgte Eigenbewegung mitschieben (linear im Fenster)
                                _fa = (now - t0) * inv_window
                                _fb = (now - t1) * inv_window
                                ax += own_dx * _fa; ay += own_dy * _fa; az += own_dz * _fa
                                bx += own_dx * _fb; by += own_dy * _fb; bz += own_dz * _fb
                                if _sb_extra > 0.0:
                                    ax, ay, az, bx, by, bz = _extend_segment(
                                        ax, ay, az, bx, by, bz, _sb_extra)
                                if _segment_hits_obb_3d(
                                        ax, ay, az, bx, by, bz,
                                        tank_cx, tank_cy, tank_cz, self.azimuth,
                                        _half_len, _half_w, _half_h):
                                    hit = True
                                    break
                        else:
                            if shot.shooter_id == self.player_id:
                                continue  # Nicht-Rico eigener Schuss kann sich nicht selbst treffen
                            ax, ay, az = shot.position_at(prev_t)
                            bx, by, bz = shot.position_at(now)
                            # Relativ-Sweep: Startpunkt um die Eigenbewegung seit prev_t
                            # mitschieben (Endpunkt ist bereits im Tank-Frame von `now`)
                            _fa = (now - prev_t) * inv_window
                            ax += own_dx * _fa; ay += own_dy * _fa; az += own_dz * _fa
                            # Skip wenn Schuss sich vom Bot wegbewegt (past closest approach).
                            # Schuss bleibt in _shots — könnte bei Ricochet zurückkommen.
                            # BEIDE Segment-Enden prüfen: entfernt sich nur der Endpunkt,
                            # kann das Segment den Tank in diesem Tick DURCHquert haben —
                            # dann muss der OBB-Test entscheiden (kein Geister-Durchflug).
                            # Läuft auf den relativkorrigierten Zentren VOR der SB-
                            # Verlängerung: weg ist weg (auch der SB-Schweif).
                            _rel_bx = bx - tank_cx; _rel_by = by - tank_cy
                            _rel_ax = ax - tank_cx; _rel_ay = ay - tank_cy
                            if (shot.vel[0] * _rel_bx + shot.vel[1] * _rel_by > 0
                                    and shot.vel[0] * _rel_ax + shot.vel[1] * _rel_ay > 0):
                                continue
                            if _sb_extra > 0.0:
                                ax, ay, az, bx, by, bz = _extend_segment(
                                    ax, ay, az, bx, by, bz, _sb_extra)
                            hit = _segment_hits_obb_3d(ax, ay, az, bx, by, bz,
                                                        tank_cx, tank_cy, tank_cz, self.azimuth,
                                                        _half_len, _half_w, _half_h)
                if hit:
                    hit_shot = shot; to_remove.append(key); break
            for k in to_remove:
                self._shots.pop(k, None)
                self._ricochet_paths.pop(k, None)
        if hit_shot:
            if self.own_flag == "SH":
                # Shield absorbiert Treffer: Flag droppen, Bot überlebt
                logger.info("[%s] SH-Schild absorbiert Treffer von Spieler %d",
                            self.callsign, hit_shot.shooter_id)
                self._try_drop_flag()
            else:
                logger.info("[%s] Getroffen von Spieler %d (Shot %d)",
                            self.callsign, hit_shot.shooter_id, hit_shot.shot_id)
                self._report_killed(hit_shot)

    def _report_killed(self, shot: Shot) -> None:
        """Setzt Bot auf tot und sendet MsgKilled an den Server."""
        if not self.alive:
            return
        self._jumping   = False
        self.alive      = False
        self.death_time = time.monotonic()
        self._start_explosion(self.death_time)   # Explosions-Bogen wie der echte Client
        self._jump_pending = False
        self._last_jump_at = 0.0
        self._tactical_jump_until = 0.0
        self._escape_jump_ang_vel = None
        self._dodging = False
        self._dodge_forward = self._dodge_reverse = False
        self._gm_need_update = False
        self._gm_send_at = self._gm_resend_at = None
        self._transition_to(AIState.DEAD)
        _reason = KILL_REASON_GENOCIDED if shot.flag_abbr == b"G\x00" else KILL_REASON_SHOT
        payload = (
            struct.pack(">B", shot.shooter_id)
            + struct.pack(">H", _reason)
            + struct.pack(">H", shot.shot_id & 0xFFFF)   # untere 16 Bit, wie int16_t(shotId) im echten Client
            + shot.flag_abbr
        )
        self.client.send(MsgKilled, payload)
        logger.info("[%s] MsgKilled gesendet – Respawn in %.1fs",
                    self.callsign, RESPAWN_DELAY)

    def _check_steamroller(self, now: float) -> None:
        """Tötet Bot via GotRunOver wenn ein SR-Spieler in Kill-Radius-Nähe ist."""
        # list()-Snapshot: players wird im Recv-Thread mutiert (Join/Leave),
        # diese Iteration läuft im Game-Loop → ohne Kopie droht
        # "RuntimeError: dictionary changed size during iteration".
        for pid, info in list(self.players.items()):
            if not info.alive: continue
            # BU wirkt nur eingegraben (pos[2] < 0), wie überall sonst im Code
            # (_effective_tank_speed, _find_incoming_shot, _effective_optimal_range).
            if info.flag != "SR" and not (self.own_flag == "BU" and self.pos[2] < 0.0): continue
            if now - info.last_seen > 1.0: continue
            dx = info.pos[0] - self.pos[0]
            dy = info.pos[1] - self.pos[1]
            dz = info.pos[2] - self.pos[2]
            # quadrierter Vergleich statt sqrt (mathematisch identisch, spart die Wurzel pro Kandidat)
            dist_sq = dx*dx + dy*dy + 4.0*dz*dz
            thr = TANK_RADIUS * (1.0 + self._sr_radius_mult)
            if dist_sq < thr * thr:
                logger.info("[%s] Überrollt von Spieler %d (SR)", self.callsign, pid)
                self._report_steamrolled(pid)
                return

    def _report_steamrolled(self, killer_id: int) -> None:
        """Setzt Bot auf tot (reason=GotRunOver) und sendet MsgKilled."""
        if not self.alive: return
        self._jumping = False
        self.alive = False
        self.death_time = time.monotonic()
        self._start_explosion(self.death_time)   # Explosions-Bogen wie der echte Client
        self._dodging = False; self._jump_pending = False
        self._last_jump_at = 0.0
        self._tactical_jump_until = 0.0
        self._escape_jump_ang_vel = None
        self._dodge_forward = self._dodge_reverse = False
        self._gm_need_update = False
        self._gm_send_at = self._gm_resend_at = None
        self._transition_to(AIState.DEAD)
        payload = (
            struct.pack(">B", killer_id)
            + struct.pack(">H", KILL_REASON_RUNOVER)
            + struct.pack(">h", 0)
            + b"SR"
        )
        self.client.send(MsgKilled, payload)
        logger.info("[%s] MsgKilled (GotRunOver, Killer=%d) gesendet", self.callsign, killer_id)

    def _cleanup_shots(self, now: float) -> None:
        """Entfernt abgelaufene Schüsse aus _shots UND _ricochet_paths.

        _resolve_incoming_shots räumt beide Dicts nur solange self.alive —
        während Tod/Respawn ablaufende Schüsse würden ihre Pfad-Segmente
        sonst dauerhaft in _ricochet_paths hinterlassen (Leak, wächst mit
        der Uptime und verteuert jeden _find_incoming_shot-Scan).
        """
        # N2: Leerlauf-Early-Out (läuft 60 Hz) — die Rico-Pops hängen an den
        # _shots-Keys, ohne Schüsse gibt es also nichts zu räumen.
        if not self._shots:
            return
        with self._shots_lock:
            for k in [k for k, s in self._shots.items() if s.is_expired(now)]:
                del self._shots[k]
                self._ricochet_paths.pop(k, None)
