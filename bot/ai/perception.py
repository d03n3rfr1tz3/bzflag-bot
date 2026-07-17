"""Wahrnehmung: FoV/LoS-Praedikate, Radar-Aufmerksamkeit, Sichtbarkeits-Gates und Bedrohungserkennung eingehender Schuesse (W4, FABLE-PLAN Teil 3)."""

import math
import random
import time
from typing import Optional

from bzflag.shot_physics import (ray_teleporter_crossing)
from bot.constants import (
    NAV_WALL_STEEP_DEG,
    DODGE_DIST,
    RICO_DODGE_LOOKAHEAD,
    HIT_RADIUS,
    AHEAD_HALF_ANGLE,
    RADAR_SKIP_DEFAULT,
    RADAR_SKIP_CL,
    RADAR_COOLDOWN_DEFAULT,
    RADAR_COOLDOWN_CL,
    PLAYER_LOS_TTL_S,
    TARGET_FOV,
    TANK_HALF_DIAG,
    COVER_EDGE_PROBE_DIST,
)
from bot.util import _angle_diff


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class PerceptionMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

    def _effective_fov(self) -> float:
        """Halbwinkel des Fenster-Sichtkegels (was der Spieler „out the window" sieht). WA verbreitert
        ihn (Server-Var _wideAngleAng). EINZIGER Sicht-FoV — Wahrnehmung UND Ziel-Erfassung nutzen ihn."""
        return (self._wide_angle_ang / 2.0) if self.own_flag == "WA" else (TARGET_FOV / 2.0)

    def _in_fov(self, px: float, py: float) -> bool:
        """True wenn (px, py) im Fenster-Sichtkegel des Bots liegt (Halbwinkel = _effective_fov())."""
        if math.hypot(px - self.pos_x, py - self.pos_y) < 1.0:
            return True
        angle_to = math.atan2(py - self.pos_y, px - self.pos_x)
        return abs(_angle_diff(angle_to, self.azimuth)) < self._effective_fov()

    def _is_ahead(self, px: float, py: float) -> bool:
        """Geometrie „liegt vor mir" (±90°), KEIN Sicht-FoV: für Nav-WP-Skip (Startzelle hinter dem
        Bot) und Flag-Grab (nicht rückwärts greifen). Bewusst weiter als der Sicht-FoV."""
        if math.hypot(px - self.pos_x, py - self.pos_y) < 1.0:
            return True
        angle_to = math.atan2(py - self.pos_y, px - self.pos_x)
        return abs(_angle_diff(angle_to, self.azimuth)) < AHEAD_HALF_ANGLE

    # ── Sichtbarkeit: zwei Kanäle (Radar / Fenster), zentral statt verstreut ──────
    def _enemy_visible_radar(self, info) -> bool:
        """Grundsätzliche Radar-Sicht auf den Gegner (Reichweite separat). Nur Stealth ist
        radar-unsichtbar; eigenes JM stört das eigene Radar komplett; eigenes SE deckt alles auf."""
        if self.own_flag == "SE": return True
        if self.own_flag == "JM": return False     # Radar gestört
        return info.flag != "ST"

    def _enemy_visible_window(self, info) -> bool:
        """Grundsätzliche Fenster-Sicht auf den Gegner (FoV/LoS separat). Nur Cloaking ist
        fenster-unsichtbar; eigenes B (Blind) macht fensterblind; eigenes SE deckt alles auf.
        (MQ ist sichtbar — nur die Team-Zugehörigkeit täuscht, s. _is_foe.)"""
        if self.own_flag == "SE": return True
        if self.own_flag == "B":  return False     # blind
        return info.flag != "CL"

    def _sees_in_window(self, info, x: float, y: float, z: float, now: Optional[float] = None) -> bool:
        """Voller Fenster-Sichtkontakt: Flagge erlaubt Fenster-Sicht UND im FoV UND unverdeckt (LoS).

        now: nur der heiße Update-Pfad (_should_update_player, pro MsgPlayerUpdate im Recv-Thread)
        übergibt now und cached damit den LoS-Raycast pro Spieler für PLAYER_LOS_TTL_S (P7).
        Das ist ein reines Wahrnehmungs-Gate — Staleness analog zur Radar-Aufmerksamkeit, nur
        länger; Flag- und FoV-Check bleiben exakt (billig, drehen sich mit dem Bot). Ohne now
        (Targeting/Game-Loop) wird exakt gerechnet und der Cache weder gelesen noch geschrieben.
        Thread-Sicherheit: einfache Attribut-Zuweisungen auf info, GIL-atomar — kein Lock nötig."""
        if not (self._enemy_visible_window(info) and self._in_fov(x, y)):
            return False
        if now is not None and now < info.los_cache_until:
            return info.los_cache
        los = self._has_los_to_point(x, y, z + self._tank_height * 0.5)
        if now is not None:
            info.los_cache_until = now + PLAYER_LOS_TTL_S
            info.los_cache = los
        return los

    # Schuss-Sichtbarkeit = Spiegelbild der Tank-Sichtbarkeit (SE betrifft nur Tanks, nicht Schüsse).
    def _shot_visible_radar(self, shooter) -> bool:
        if self.own_flag == "JM": return False     # eigenes Radar gestört → keine Schuss-Blips
        return shooter.flag != "IB"                # IB-Schüsse erscheinen nicht auf dem Radar

    def _shot_visible_window(self, shooter) -> bool:
        if self.own_flag == "B": return False      # blind
        return shooter.flag != "CS"                # CS-Schüsse sind out-the-window unsichtbar

    def _shot_reveals_shooter(self, shooter, ox: float, oy: float, oz: float) -> bool:
        """Ein wahrnehmbarer Schuss verrät die Schützen-Position: auf Radar ODER out-the-window
        (FoV + LoS zum Schuss-Ursprung)."""
        return (self._shot_visible_radar(shooter)
                or (self._shot_visible_window(shooter)
                    and self._in_fov(ox, oy) and self._has_los_to_point(ox, oy, oz)))

    def _should_update_player(self, info, px: float, py: float, pz: float, now: float) -> bool:
        """Übernimmt der Bot diese Gegnerposition jetzt?
        - Direkter Sichtkontakt (Fenster: FoV+LoS) → immer aktuell (man schaut ihn an).
          Der LoS-Raycast ist hier pro Spieler für PLAYER_LOS_TTL_S gecacht (P7, s. _sees_in_window).
        - Nur Radar → Radar-Aufmerksamkeit: pro EINGEHENDEM MsgPlayerUpdate mit (1-skip)
          hinschauen; bei Fehlschlag für einen Cooldown ganz wegschauen (CL stärker).
          Achtung (F8, dokumentiert): der Würfelwurf hängt damit an der Server-Update-Rate
          (Standard 30 Hz × Spieler) — bei abweichender Rate verschiebt sich die effektive
          Aufmerksamkeit; der Cooldown (zeitbasiert) dämpft das. Bewusst so belassen.
        - Weder Fenster noch Radar (ST/eigenes JM) → nie."""
        if self._sees_in_window(info, px, py, pz, now):   # now → LoS-Cache aktiv (P7)
            return True
        if not self._enemy_visible_radar(info):
            return False
        if now < info.radar_blind_until:               # seit letztem Fehlschlag noch weggeschaut
            return False
        cl = info.flag == "CL" and self.own_flag != "SE"
        if random.random() >= (RADAR_SKIP_CL if cl else RADAR_SKIP_DEFAULT):
            return True                                # hingeschaut → Update
        info.radar_blind_until = now + (RADAR_COOLDOWN_CL if cl else RADAR_COOLDOWN_DEFAULT)
        return False                                   # weggeschaut → Cooldown

    def _find_incoming_shot(self, now: float, bot_vel=None):
        """Findet den gefährlichsten anfliegenden Schuss.
        Gibt (shot, t_threat) zurück; (None, inf) wenn kein Treffer.
        bot_vel: optionales (vx, vy)-Tupel für hypothetische Bot-Velocity (Standard: self.vel).
        Prüft sowohl direkte Schüsse als auch gecachte Ricochet-Pfade."""
        # N2: Leerlauf-Early-Out — auch der Rico-Zweig verlangt den Schuss in
        # _shots (Lookup unten), ohne Schüsse kann es also keine Bedrohung geben.
        if not self._shots:
            return None, float("inf")
        bvx = self.vel_x if bot_vel is None else bot_vel[0]
        bvy = self.vel_y if bot_vel is None else bot_vel[1]
        best = None
        best_t = float("inf")
        with self._shots_lock:
            for shot in self._shots.values():
                if shot.is_expired(now): continue
                if shot.shooter_id == self.player_id: continue
                # Phantom-Schüsse (Wire-Flag PZ) treffen nur gezonede Ziele → keine Bedrohung
                if self._phantom_shot_harmless(shot): continue
                # Ricochet-Schüsse: Richtung nach Bounce unklar → nur Segment-Cache prüfen
                if (shot.shooter_id, shot.shot_id) in self._ricochet_paths:
                    continue
                if shot.is_gm:
                    # GM: shot.pos wird laufend nachgeführt (Integration in
                    # _resolve_incoming_shots + MsgGMUpdate) — position_at() würde
                    # die bisherige Flugzeit ein ZWEITES Mal aufaddieren und die
                    # Rakete weit vor ihrer echten Position sehen (Phantom-Position,
                    # meist „entfernt sich" → GM wurde beim Ausweichen ignoriert).
                    sx, sy, sz = shot.pos
                else:
                    sx, sy, sz = shot.position_at(now)
                if shot.is_sw:
                    _sw_dist = math.hypot(sx - self.pos_x, sy - self.pos_y)
                    if self._shock_in_radius < _sw_dist < self._shock_out_radius:
                        sw_elapsed = max(0.0, now - shot.fire_time)
                        t = max(0.0,
                                (_sw_dist - self._shock_in_radius) / self._sw_expand_speed - sw_elapsed)
                        if t < best_t:
                            best_t = t; best = shot
                    continue  # SW hat vel≈0, normales d/t-Verfahren nicht anwendbar
                # Eingegrabener BU-Bot: nur SW und GM können ihn treffen
                if self.own_flag == "BU" and self.pos_z < 0.0 and not shot.is_gm:
                    continue
                if abs(sz - self.pos_z) > HIT_RADIUS * 2:
                    continue  # Schuss auf anderer Etage → keine Bedrohung
                # Relativgeschwindigkeit: Schuss minus Bot-Eigengeschwindigkeit
                # (ermöglicht Voraussage ob der Schuss den Bot trotz Ausweichen noch trifft)
                rvx = shot.vel[0] - bvx
                rvy = shot.vel[1] - bvy
                rx  = sx - self.pos_x
                ry  = sy - self.pos_y
                rel_spd_sq = rvx * rvx + rvy * rvy
                if rel_spd_sq > 1e-6:
                    t_rel_raw = -(rx * rvx + ry * rvy) / rel_spd_sq
                    if t_rel_raw < 0:
                        continue  # Schuss entfernt sich im rel. Bezugssystem → keine Bedrohung
                    t_rel = t_rel_raw
                    d = math.hypot(rx + rvx * t_rel, ry + rvy * t_rel)
                    t = t_rel
                else:
                    d = math.hypot(rx, ry)
                    t = 0.0
                if d < DODGE_DIST and t < best_t:
                    best_t = t; best = shot

            # --- Ricochet-Pfade: segmentweise prüfen ---
            for (pid, sid), segs in self._ricochet_paths.items():
                if pid == self.player_id:
                    continue
                # Track 5 (mypyc): eigener Name statt `shot` — _shots.get() liefert
                # Optional[Shot], das `for shot in self._shots.values()` oben (nicht-optional)
                # widerspräche einer Wiederverwendung derselben Variable.
                rico_shot = self._shots.get((pid, sid))
                if rico_shot is None or rico_shot.is_expired(now):
                    continue
                # Phantom-Schüsse: auch der Phase-Pfad-Cache ist keine Bedrohung
                if self._phantom_shot_harmless(rico_shot):
                    continue
                if self.own_flag == "BU" and self.pos_z < 0.0:
                    continue
                for seg in segs:
                    if seg.t_end <= now:
                        continue  # Segment bereits abgelaufen
                    seg_dt = seg.t_end - seg.t_start
                    if seg_dt < 1.0e-9:
                        continue
                    t_from = max(seg.t_start, now)
                    frac = (t_from - seg.t_start) / seg_dt
                    sx = seg.px + (seg.ex - seg.px) * frac
                    sy = seg.py + (seg.ey - seg.py) * frac
                    sz = seg.pz + (seg.ez - seg.pz) * frac
                    if abs(sz - self.pos_z) > HIT_RADIUS * 2:
                        continue
                    svx = (seg.ex - seg.px) / seg_dt
                    svy = (seg.ey - seg.py) / seg_dt
                    rvx = svx - bvx
                    rvy = svy - bvy
                    rx = sx - self.pos_x
                    ry = sy - self.pos_y
                    rel_spd_sq = rvx * rvx + rvy * rvy
                    seg_rem = seg.t_end - t_from
                    if rel_spd_sq > 1e-6:
                        t_rel = -(rx * rvx + ry * rvy) / rel_spd_sq
                        if t_rel < 0:
                            continue
                        t_rel = min(t_rel, seg_rem)
                        d = math.hypot(rx + rvx * t_rel, ry + rvy * t_rel)
                        t_threat = (t_from - now) + t_rel
                    else:
                        d = math.hypot(rx, ry)
                        t_threat = t_from - now
                    if d < DODGE_DIST and t_threat < best_t and t_threat < RICO_DODGE_LOOKAHEAD:
                        best_t = t_threat
                        best = rico_shot
        return best, best_t

    def _any_incoming_threat(self, now: float, vels) -> bool:
        """Wie _find_incoming_shot, aber prüft mehrere Velocity-Hypothesen (vels: Sequenz von
        (bvx, bvy)-Tupeln) in EINEM Durchlauf über die Schüsse statt eines separaten Aufrufs
        pro Hypothese — spart pro EVADING-Tick drei zusätzliche Shots-Lock-Scans (P3). Liefert
        True bei der ERSTEN gefundenen Bedrohung (kein best/best_t-Tracking nötig, anders als
        _find_incoming_shot dessen Aufrufer den konkreten Schuss/Zeitpunkt brauchen). Gleiche
        Skip-Bedingungen wie dort: expired, eigener Schuss, Rico-Cache-Skip im Direktzweig,
        GM-Live-Position statt position_at, SW-Sonderfall, BU-eingegraben-Skip, z-Etagen-Check,
        d < DODGE_DIST, sowie im Segment-Zweig zusätzlich t_threat < RICO_DODGE_LOOKAHEAD."""
        if not self._shots:
            return False
        with self._shots_lock:
            for shot in self._shots.values():
                if shot.is_expired(now): continue
                if shot.shooter_id == self.player_id: continue
                # Phantom-Schüsse (Wire-Flag PZ) treffen nur gezonede Ziele → keine Bedrohung
                if self._phantom_shot_harmless(shot): continue
                # Ricochet-Schüsse: Richtung nach Bounce unklar → nur Segment-Cache prüfen (unten)
                if (shot.shooter_id, shot.shot_id) in self._ricochet_paths:
                    continue
                if shot.is_gm:
                    sx, sy, sz = shot.pos          # Live-Position (siehe _find_incoming_shot)
                else:
                    sx, sy, sz = shot.position_at(now)
                if shot.is_sw:
                    _sw_dist = math.hypot(sx - self.pos_x, sy - self.pos_y)
                    if self._shock_in_radius < _sw_dist < self._shock_out_radius:
                        return True                # SW-Bedrohung ist velocity-unabhängig
                    continue
                if self.own_flag == "BU" and self.pos_z < 0.0 and not shot.is_gm:
                    continue
                if abs(sz - self.pos_z) > HIT_RADIUS * 2:
                    continue
                rx = sx - self.pos_x
                ry = sy - self.pos_y
                for bvx, bvy in vels:
                    rvx = shot.vel[0] - bvx
                    rvy = shot.vel[1] - bvy
                    rel_spd_sq = rvx * rvx + rvy * rvy
                    if rel_spd_sq > 1e-6:
                        t_rel_raw = -(rx * rvx + ry * rvy) / rel_spd_sq
                        if t_rel_raw < 0:
                            continue               # Schuss entfernt sich → keine Bedrohung
                        d = math.hypot(rx + rvx * t_rel_raw, ry + rvy * t_rel_raw)
                    else:
                        d = math.hypot(rx, ry)
                    if d < DODGE_DIST:
                        return True

            # --- Ricochet-Pfade: segmentweise prüfen ---
            for (pid, sid), segs in self._ricochet_paths.items():
                if pid == self.player_id:
                    continue
                # Track 5 (mypyc): eigener Name statt `shot` (s. _find_incoming_shot).
                rico_shot = self._shots.get((pid, sid))
                if rico_shot is None or rico_shot.is_expired(now):
                    continue
                # Phantom-Schüsse: auch der Phase-Pfad-Cache ist keine Bedrohung
                if self._phantom_shot_harmless(rico_shot):
                    continue
                if self.own_flag == "BU" and self.pos_z < 0.0:
                    continue
                for seg in segs:
                    if seg.t_end <= now:
                        continue
                    seg_dt = seg.t_end - seg.t_start
                    if seg_dt < 1.0e-9:
                        continue
                    t_from = max(seg.t_start, now)
                    frac = (t_from - seg.t_start) / seg_dt
                    sx = seg.px + (seg.ex - seg.px) * frac
                    sy = seg.py + (seg.ey - seg.py) * frac
                    sz = seg.pz + (seg.ez - seg.pz) * frac
                    if abs(sz - self.pos_z) > HIT_RADIUS * 2:
                        continue
                    svx = (seg.ex - seg.px) / seg_dt
                    svy = (seg.ey - seg.py) / seg_dt
                    rx = sx - self.pos_x
                    ry = sy - self.pos_y
                    seg_rem = seg.t_end - t_from
                    for bvx, bvy in vels:
                        rvx = svx - bvx
                        rvy = svy - bvy
                        rel_spd_sq = rvx * rvx + rvy * rvy
                        if rel_spd_sq > 1e-6:
                            t_rel = -(rx * rvx + ry * rvy) / rel_spd_sq
                            if t_rel < 0:
                                continue
                            t_rel = min(t_rel, seg_rem)
                            d = math.hypot(rx + rvx * t_rel, ry + rvy * t_rel)
                            t_threat = (t_from - now) + t_rel
                        else:
                            d = math.hypot(rx, ry)
                            t_threat = t_from - now
                        if d < DODGE_DIST and t_threat < RICO_DODGE_LOOKAHEAD:
                            return True
        return False

    def _effective_radar_range(self) -> float:
        """Liefert effektive Radar-Reichweite (25% wenn BU + pos[2] < 0).

        F6, bewusste Design-Entscheidung: Reichweite = HALBE Weltgröße
        (self.world_half, via _worldSize nachgeführt), NICHT die volle. Das
        echte Client-Radar hat Zoomstufen und dreht mit der Blickrichtung —
        ein Mensch sieht nie permanent die ganze Karte. Da der Bot über
        FoV+LoS bereits kartenweit schauen darf, wäre ein Voll-Radar
        Allwissenheit; das Halbe-Welt-Limit hält ihn fair. Nicht „fixen"!"""
        base = self.world_half
        if self.own_flag == "BU" and self.pos_z < 0.0:
            return base * 0.25
        return base

    def _segment_clear(self, ox: float, oy: float, oz: float,
                       ex: float, ey: float, ez: float) -> bool:
        """True, wenn keine solide Box (nav._los_obs, shoot_through=False) das Segment
        (ox,oy,oz)→(ex,ey,ez) schneidet. Generischer Slab-Test mit frei wählbarem Ursprung —
        Basis für _has_los_to_point (Bot-Auge) und das GM-Aktivierungspunkt-Gate (beliebige Punkte).
        Teleporter zählen bewusst NICHT als Blocker (Schuss-/Kurven-Routing s. _has_los_to_enemy)."""
        nav = self._nav_graph
        if nav is None:
            return True
        dx = ex - ox; dy = ey - oy; dz = ez - oz
        # Broad-Phase: nur Boxen entlang des Strahls (DDA) statt linear über alle _los_obs.
        _grid = nav._los_grid
        _boxes = _grid.query_ray(ox, oy, ex, ey) if _grid is not None else nav._los_obs
        for box in _boxes:
            cos_a = box.cos_a; sin_a = box.sin_a
            rx = ox - box.cx; ry = oy - box.cy
            lox =  rx * cos_a + ry * sin_a
            loy = -rx * sin_a + ry * cos_a
            ldx =  dx * cos_a + dy * sin_a
            ldy = -dx * sin_a + dy * cos_a
            t_min = 0.0; t_max = 1.0; hit = True

            # Track 5 (mypyc): Tupel-Loop ausgerollt — drei explizite Achsen-Blöcke statt
            # heterogener Tupel-Iteration (Closures/Tupel-Loops sind unter mypyc teuer).
            # Achse x (lokal, Box-Breite)
            if abs(ldx) < 1e-9:
                if lox < -box.half_w or lox > box.half_w:
                    hit = False
            else:
                t1 = (-box.half_w - lox) / ldx; t2 = (box.half_w - lox) / ldx
                t_min = max(t_min, min(t1, t2))
                t_max = min(t_max, max(t1, t2))

            # Achse y (lokal, Box-Tiefe)
            if hit:
                if abs(ldy) < 1e-9:
                    if loy < -box.half_d or loy > box.half_d:
                        hit = False
                else:
                    t1 = (-box.half_d - loy) / ldy; t2 = (box.half_d - loy) / ldy
                    t_min = max(t_min, min(t1, t2))
                    t_max = min(t_max, max(t1, t2))

            # Achse z (Höhe)
            if hit:
                hi_z = box.bottom_z + box.height
                if abs(dz) < 1e-9:
                    if oz < box.bottom_z or oz > hi_z:
                        hit = False
                else:
                    t1 = (box.bottom_z - oz) / dz; t2 = (hi_z - oz) / dz
                    t_min = max(t_min, min(t1, t2))
                    t_max = min(t_max, max(t1, t2))

            if hit and t_min <= t_max:
                return False
        return True

    def _steep_wall_ahead(self, az: float, max_dist: float) -> Optional[float]:
        """COMBAT-Direktmodus-Vorausschau: castet einen horizontalen Strahl der Länge max_dist von
        der Tank-Mitte (Augenhöhe) entlang az gegen die soliden LoS-Boxen. Trifft er die NÄCHSTE
        Wand in steilem Winkel (Einfallswinkel zur Oberfläche > NAV_WALL_STEEP_DEG → der Wall-Slide
        nullt dann fast den ganzen Vortrieb), liefert er die nach vorn gerichtete Wand-Tangente
        (Azimut) zum Entlanggleiten/Abdrehen. Sonst None: flacher Winkel (Gleiten ist ok) oder
        freie Bahn. Slab-Mathematik wie _segment_clear; Box-angle deckt gedrehte Wände ab.

        P8: Ergebnis 0.1s gecacht — läuft im sichtlosen COMBAT-Direktmodus bei 60 Hz (DDA-
        Raycast + Slab-Tests), teils 2× pro Tick mit gleichem az (_execute_combat_move). 0.1s
        ≈ 6 Ticks, der Bot bewegt sich dabei ≤2.5u — für eine 20u-Vorausschau tolerierbar."""
        now = time.monotonic()
        cache = self._steep_wall_cache
        if cache is not None:
            expires_at, az_cached, max_dist_cached, result = cache
            if (now < expires_at
                    and abs(_angle_diff(az, az_cached)) < math.radians(3)
                    and abs(max_dist - max_dist_cached) < 1.0):
                return result
        result = self._steep_wall_ahead_raycast(az, max_dist)
        self._steep_wall_cache = (now + 0.1, az, max_dist, result)
        return result

    def _steep_wall_ahead_raycast(self, az: float, max_dist: float,
                                  min_steep_deg: float = NAV_WALL_STEEP_DEG) -> Optional[float]:
        """Unveränderte Raycast-Logik von _steep_wall_ahead, jetzt hinter dessen 0.1s-Cache.

        min_steep_deg: Einfallswinkel-Schwelle zur Wandfläche, ab der die Tangente geliefert wird.
        Default = NAV_WALL_STEEP_DEG (Combat-Wall-Slide). P4-TAC-02 ruft mit 0.0 auf: dort ist JEDE
        vorausliegende Wand relevant (Kanten-Probe entlang der Schleif-Richtung), nicht nur steile."""
        nav = self._nav_graph
        if nav is None or max_dist <= 0.0:
            return None
        ox = self.pos_x; oy = self.pos_y; oz = self.pos_z + self._tank_height * 0.5
        dx = math.cos(az) * max_dist; dy = math.sin(az) * max_dist
        best_t = 2.0; best_axis = -1; best_box = None
        # Broad-Phase: nur Boxen entlang des Strahls (DDA).
        _grid = nav._los_grid
        _boxes = _grid.query_ray(ox, oy, ox + dx, oy + dy) if _grid is not None else nav._los_obs
        for box in _boxes:
            cos_a = box.cos_a; sin_a = box.sin_a
            rx = ox - box.cx; ry = oy - box.cy
            lox =  rx * cos_a + ry * sin_a
            loy = -rx * sin_a + ry * cos_a
            ldx =  dx * cos_a + dy * sin_a
            ldy = -dx * sin_a + dy * cos_a
            t_min = 0.0; t_max = 1.0; hit = True; t_min_axis = -1

            # Track 5 (mypyc): Tupel-Loop ausgerollt (wie _segment_clear). Achsindex-Semantik
            # exakt erhalten: Achse 2 (z) zählt NIE als Eintritts-Achse (t_min_axis bleibt bei
            # ihr unverändert) — sie ist hier nur ein statisches Höhen-Gate (d_v ist immer 0.0,
            # der Ray ist horizontal), kein Sonderfall der Iteration.
            # Achse 0: x (lokal, Box-Breite)
            if abs(ldx) < 1e-9:
                if lox < -box.half_w or lox > box.half_w:
                    hit = False
            else:
                t1 = (-box.half_w - lox) / ldx; t2 = (box.half_w - lox) / ldx
                t_near = min(t1, t2)
                if t_near > t_min:
                    t_min = t_near; t_min_axis = 0
                t_max = min(t_max, max(t1, t2))

            # Achse 1: y (lokal, Box-Tiefe)
            if hit:
                if abs(ldy) < 1e-9:
                    if loy < -box.half_d or loy > box.half_d:
                        hit = False
                else:
                    t1 = (-box.half_d - loy) / ldy; t2 = (box.half_d - loy) / ldy
                    t_near = min(t1, t2)
                    if t_near > t_min:
                        t_min = t_near; t_min_axis = 1
                    t_max = min(t_max, max(t1, t2))

            # Achse 2: z (Höhe, statisches Gate — d_v == 0.0)
            if hit:
                hi_z = box.bottom_z + box.height
                if oz < box.bottom_z or oz > hi_z:
                    hit = False

            if not hit or t_min > t_max or t_min >= best_t or t_min_axis not in (0, 1):
                continue   # kein Treffer / weiter weg / Eintritt über Z-Ebene (Dach/Boden)
            best_t = t_min; best_axis = t_min_axis; best_box = box
        if best_axis < 0 or best_box is None:
            return None
        # Einfallswinkel zur getroffenen Fläche: Normalkomponente der (normierten) Fahrtrichtung
        # entlang der Eintritts-Achse. dz=0 → |Richtung| == max_dist. Steil ⇔ Komponente > sin(60°).
        cos_a = best_box.cos_a; sin_a = best_box.sin_a
        ndx = ( dx * cos_a + dy * sin_a) / max_dist
        ndy = (-dx * sin_a + dy * cos_a) / max_dist
        normal_comp = abs(ndx) if best_axis == 0 else abs(ndy)
        if normal_comp <= math.sin(math.radians(min_steep_deg)):
            return None   # flacher Winkel → der Bot gleitet sauber an der Wand entlang
        # Wand-Tangente in Weltkoordinaten (Fläche ⟂ Eintritts-Normale), nach vorn gerichtet.
        if best_axis == 0:      # x-Fläche getroffen → Tangente entlang lokaler y-Achse
            tx, ty = -sin_a, cos_a
        else:                   # y-Fläche getroffen → Tangente entlang lokaler x-Achse
            tx, ty = cos_a, sin_a
        if dx * tx + dy * ty < 0.0:
            tx, ty = -tx, -ty
        return math.atan2(ty, tx)

    def _has_los_to_point(self, ex: float, ey: float, ez: float) -> bool:
        """Reine Sicht-LoS: True, wenn keine solide Box zwischen Bot-Auge und (ex,ey,ez) liegt.
        Teleporter blockieren KEINE Sicht (das ist nur Schuss-LoS, s. _has_los_to_enemy)."""
        return self._segment_clear(self.pos_x, self.pos_y, self.pos_z + self._tank_height * 0.5,
                                   ex, ey, ez)

    def _muzzle_clear(self, az: float) -> bool:
        """True, wenn die Mündung (pos + Richtung az * _muzzle_front, auf Mündungshöhe) NICHT
        hinter/in einer soliden Wand steckt. Der reale Schuss spawnt an der Mündung (s. _send_shot);
        liegt eine dünne Wand zwischen Tank-Mitte und Mündung, würde bzfs den Schuss serverseitig
        'fressen' bzw. er ginge unfair durch die Wand → solche Schüsse unterdrücken (s. _maybe_shoot)."""
        # P4a: Per-Tick-Memo — wird pro Tick mit identischem az mehrfach geprüft.
        memo = self._tick_memo
        key = ("muzzle", az, self.pos_x, self.pos_y, self.pos_z)
        if memo is not None:
            cached = memo.get(key)
            if cached is not None:
                return cached
        mz = self.pos_z + self._muzzle_height
        mx = self.pos_x + math.cos(az) * self._muzzle_front
        my = self.pos_y + math.sin(az) * self._muzzle_front
        result = self._segment_clear(self.pos_x, self.pos_y, mz, mx, my, mz)
        if memo is not None:
            memo[key] = result
        return result

    def _cover_silhouette_blocked(self, ex: float, ey: float, oz: float,
                                  cx: float, cy: float, cz: float) -> bool:
        """True, wenn BEIDE Silhouetten-Ränder eines an (cx,cy) stehenden Tanks gegen einen Schützen
        an (ex,ey,oz) verdeckt sind (P4-TAC-02). Statt der Tank-Mitte werden die beiden Punkte
        ±TANK_HALF_DIAG senkrecht zur Linie Schütze→(cx,cy) geprüft — Rand- statt Zentrumstest, damit
        eine 'herausschauende Nase' als exponiert gilt (konservativ für jede Drehlage). cz = Ziel-
        Mündungshöhe des betrachteten Standpunkts."""
        dx = cx - ex; dy = cy - ey
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return False
        px = -dy / d; py = dx / d   # Einheitsnormale zur Sichtlinie
        for s in (TANK_HALF_DIAG, -TANK_HALF_DIAG):
            if self._segment_clear(ex, ey, oz, cx + px * s, cy + py * s, cz):
                return False   # mind. ein Silhouetten-Rand beschießbar → nicht gedeckt
        return True

    def _covered_from(self, pid: int, now: float = 0.0) -> bool:
        """True, wenn der Bot JETZT gegenüber Gegner pid in Deckung steht (beide Silhouetten-Ränder
        verdeckt). Ursprung ist die Gegner-MÜNDUNG (Schuss-, nicht Sichtlinie); für einen springenden
        Gegner zählt dessen aktuelle z. Per-Tick memoized (mehrfach pro Tick möglich)."""
        memo = self._tick_memo
        key = ("covered", pid)
        if memo is not None:
            cached = memo.get(key)
            if cached is not None:
                return cached
        ep = self._get_enemy_pos(pid)
        info = self.players.get(pid)
        if ep is None or info is None or not info.alive:
            result = False
        else:
            oz = info.pos[2] + self._muzzle_height
            result = self._cover_silhouette_blocked(
                ep[0], ep[1], oz, self.pos_x, self.pos_y, self.pos_z + self._muzzle_height)
        if memo is not None:
            memo[key] = result
        return result

    def _cover_edge_ahead(self, pid: int, now: float = 0.0) -> bool:
        """True, wenn der Bot an einer Deckungskante steht: JETZT gedeckt (_covered_from), eine
        Tanklänge in Bewegungsrichtung aber exponiert. Die Probe-Richtung ist die EFFEKTIVE
        Bewegungsrichtung inkl. Wall-Slide — beim Herausschleifen zeigt der Azimut leicht in die
        Wand, eine Probe stur entlang des Azimuts läge dann im Gebäude (P4-TAC-02)."""
        memo = self._tick_memo
        key = ("cover_edge", pid)
        if memo is not None:
            cached = memo.get(key)
            if cached is not None:
                return cached
        result = False
        if self._covered_from(pid, now):
            ep = self._get_enemy_pos(pid)
            if ep is not None:
                # Basisrichtung: fährt der Bot, steckt der Slide schon im Geschwindigkeitsvektor;
                # sonst der (ggf. bei Rückwärtsfahrt invertierte) Azimut.
                if math.hypot(self.vel_x, self.vel_y) > 1.0:
                    direction = math.atan2(self.vel_y, self.vel_x)
                else:
                    direction = self.azimuth + math.pi if self._move_reverse else self.azimuth
                # Wand-Korrektur: liegt eine Wand voraus, ist die Tangente die tatsächliche Schleif-
                # Richtung (min_steep_deg=0 → jede Wand zählt, nicht nur steile).
                tan = self._steep_wall_ahead_raycast(direction, COVER_EDGE_PROBE_DIST, 0.0)
                if tan is not None:
                    direction = tan
                probe_x = self.pos_x + math.cos(direction) * COVER_EDGE_PROBE_DIST
                probe_y = self.pos_y + math.sin(direction) * COVER_EDGE_PROBE_DIST
                info = self.players.get(pid)   # Recv-Thread kann pid zwischenzeitlich entfernen
                if info is not None:
                    oz = info.pos[2] + self._muzzle_height
                    # Exponiert am Probepunkt (mind. ein Rand frei) → Kante. Läge der Probepunkt selbst
                    # in einer Box, sind beide Ränder blockiert → 'keine Kante' (tief in Deckung), gewollt.
                    result = not self._cover_silhouette_blocked(
                        ep[0], ep[1], oz, probe_x, probe_y, self.pos_z + self._muzzle_height)
        if memo is not None:
            memo[key] = result
        return result

    def _has_los_to_enemy(self, target_pid: int) -> bool:
        """True wenn weder eine undurchschießbare Box noch ein verlinktes Teleporter-Feld zwischen
        Bot und Gegner liegt (Schuss-LoS — ein Direktschuss durch ein Tor würde wegteleportiert)."""
        info = self.players.get(target_pid) if target_pid else None
        if info is None or not info.alive:
            return True
        ex = info.pos[0]; ey = info.pos[1]; ez = info.pos[2] + self._tank_height * 0.5
        # P4a: Per-Tick-Memo — bis zu 3× pro Tick identisch aufgerufen
        # (_execute_combat_move 2×, _maybe_shoot_standard 1×). Key enthält
        # beide Positionen → bewegt sich der Gegner mittendrin (Recv-Thread),
        # gibt es schlicht einen Miss statt eines stalen Treffers.
        memo = self._tick_memo
        key = ("los", target_pid, self.pos_x, self.pos_y, self.pos_z, ex, ey, ez)
        if memo is not None:
            cached = memo.get(key)
            if cached is not None:
                return cached
        result = True
        if not self._has_los_to_point(ex, ey, ez):
            result = False
        else:
            # Teleporter-Feld zwischen Bot und Ziel → ein Direktschuss würde wegteleportiert,
            # also kein sauberer Direktschuss (der indirekte Aim-Sweep übernimmt dann, s. A4).
            wm = self._world_map
            if wm and wm.teleporters:
                ox = self.pos_x; oy = self.pos_y; oz = self.pos_z + self._tank_height * 0.5
                dx = ex - ox; dy = ey - oy; dz = ez - oz
                lmap = self._link_map
                for ti, tele in enumerate(wm.teleporters):
                    res = ray_teleporter_crossing(ox, oy, oz, dx, dy, dz, tele)
                    if res is None:
                        continue
                    t_cross, face = res
                    if 0.0 < t_cross < 1.0 and (ti * 2 + face) in lmap:
                        result = False
                        break
        if memo is not None:
            memo[key] = result
        return result
