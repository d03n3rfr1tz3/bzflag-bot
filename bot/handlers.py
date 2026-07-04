"""Message-Handler: alle _on_*-Callbacks des BZFlag-Protokolls inkl. _on_set_var und Rundenende-Logik (W5, FABLE-PLAN Teil 3)."""

import math
import re
import struct
import time
import logging

from bzflag.shot_physics import (simulate_shot_path, build_link_map, can_ricochet as _can_ricochet_shot)
from bzflag.world_map import teleporter_solid_boxes
from bzflag.obstacle_grid import ObstacleGrid, LOS_GRID_PAD
from bzflag.protocol import (
    PLAYER_TYPE_COMPUTER, TEAM_OBSERVER, MsgKilled, MsgAddPlayer,
    MsgPlayerUpdate, MsgPlayerUpdateSmall, MsgSetVar, MsgFlagUpdate,
    MsgMessage, MsgGameTime, MsgGMUpdate, MsgNearFlag,
    MsgTransferFlag, MsgScoreOver, MsgTeleport, MsgPause,
    PS_ALIVE, PS_FALLING, PS_EXPLODING, PS_FLAG_ACTIVE,
    unpack_uint8, unpack_uint16, unpack_int16, unpack_uint32,
    unpack_vec3, unpack_float, unpack_string, CallSignLen,
)
from bot.constants import *  # noqa: F401,F403
from bot.models import Shot, PlayerInfo, FlagInfo, AIState

logger = logging.getLogger("bzbot")


class HandlersMixin:
    """Mixin für BZBot — Methoden unverändert aus bzbot.py verschoben (Track 4/W5)."""

    def _on_game_settings(self, payload: bytes) -> None:
        """Empfängt MsgGameSettings; liest worldSize, gameOptions, maxShots, Beschleunigung, shakeTimeout."""
        logger.debug("[%s] MsgGameSettings (%d B)", self.callsign, len(payload))
        if len(payload) >= 4:
            world_size = unpack_float(payload, 0)
            if world_size > 0:
                new_half = world_size / 2.0
                if new_half != self.world_half:
                    self.world_half = new_half
                    self.client._world_half_cache = new_half
                    logger.info("[%s] worldSize=%.0f (MsgGameSettings)", self.callsign, world_size)
        if len(payload) >= 8:
            game_options = unpack_uint16(payload, 6)
            self._server_ricochet = bool(game_options & 0x0020)  # RicochetGameStyle
            if self._server_ricochet:
                logger.info("[%s] Ricochet serverseitig aktiv", self.callsign)
            self._server_jumping = bool(game_options & 0x0008)  # JumpingGameStyle
            if self._server_jumping:
                logger.info("[%s] Springen serverseitig aktiv", self.callsign)
        if len(payload) >= 12:
            ms = unpack_uint16(payload, 10)
            if ms >= 1:
                self._max_shots = ms
                logger.info("[%s] maxShots=%d (MsgGameSettings)", self.callsign, ms)
        if len(payload) >= 22:
            self._linear_acceleration  = unpack_float(payload, 14)
            self._angular_acceleration = unpack_float(payload, 18)
            logger.debug("[%s] linearAccel=%.3f angularAccel=%.3f",
                         self.callsign, self._linear_acceleration, self._angular_acceleration)
        if len(payload) >= 24:
            shake = unpack_uint16(payload, 22)
            if shake > 0:
                self._drop_bad_flag_delay = shake / 10.0
                logger.info("[%s] shakeTimeout=%.1fs", self.callsign, self._drop_bad_flag_delay)

    def _on_world_ready(self, world_map) -> None:
        """Callback nach Welt-Download: speichert WorldMap und baut NavGraph."""
        self._world_map = world_map
        self._shot_grid = None   # nie stale zur alten Welt (Rebuild unten)
        if world_map is None:
            logger.warning("[%s] Karten-Wissen nicht verfügbar (Parse-Fehler)", self.callsign)
            return
        self._link_map = build_link_map(world_map.links)
        # P1: Broad-Phase-Grid für simulate_shot_path — einmalig pro Weltladen aus
        # den soliden Obstacles. Kleines Pad genügt: die Ray-Narrow-Phase
        # (ray_box_hit/ray_pyramid_hit) hat Margin 0, das Pad dient nur der
        # Float-Robustheit an Zellgrenzen (wie beim LoS-Grid).
        self._shot_grid = ObstacleGrid(world_map.solid_obstacles(), pad=LOS_GRID_PAD)
        # P3-NAV-02: solide Teleporter-Teile (Posts + Crossbar) für die reaktive Kollision cachen.
        self._tele_solid_boxes = [box for t in world_map.teleporters
                                  for box in teleporter_solid_boxes(t)]
        if self._debug_log_tele:
            teles = world_map.teleporters
            logger.debug("[%s] Tele: %d Teleporter, %d Links geparst",
                         self.callsign, len(teles), len(world_map.links))
            for i, t in enumerate(teles):
                logger.debug("[%s] Tele:   t%d %-8s pos=(%.1f,%.1f,%.1f) ang=%.0f° "
                             "breadth=%.2f height=%.2f border=%.2f",
                             self.callsign, i, t.name or f"/t{i}", t.cx, t.cy, t.bottom_z,
                             math.degrees(t.angle), t.half_d, t.height, t.border)
            for src, dst in world_map.links:
                sn = teles[src // 2].name or f"/t{src // 2}"
                dn = teles[dst // 2].name or f"/t{dst // 2}"
                logger.debug("[%s] Tele:   Link %s:%s → %s:%s",
                             self.callsign, sn, "fb"[src % 2], dn, "fb"[dst % 2])
        from bzflag.nav_graph import get_nav_graph
        # Konservative Normalsprung-Basislinie: der Graph wird einmalig beim Weltladen ohne
        # Flaggenkontext gebaut und gecacht/geteilt; der Bot kann immer mindestens einen
        # Normalsprung. Der WG/LG-Vorteil wird zur Laufzeit über die _effective_jump_*()-Helfer
        # in den Combat-/Z-Attack-/NAV_JUMP-Checks genutzt (ein wings-bewusster A*-Graph bräuchte
        # Rebuild-on-Flag → bewusst out of scope).
        # Sprungphysik aus den globalen Server-Variablen (_jumpVelocity/_gravity). set_physics()
        # gleicht einen ggf. schon von einem anderen Bot mit Defaults gebauten Cache-Graph an
        # (No-Op bei gleichen Werten) → korrekt auch wenn MsgSetVar erst nach dem Weltladen kam.
        _v0 = self._jump_velocity
        _g  = abs(self._gravity)
        self._nav_graph = get_nav_graph(world_map, v0=_v0, g=_g)
        self._nav_graph.set_physics(_v0, _g)
        self._nav_graph._debug_path = self._debug_log_path
        logger.info("[%s] NavGraph bereit (id=%d)", self.callsign, id(self._nav_graph))
        if self._debug_log_tele:
            edges = self._nav_graph._teleport_edges
            logger.debug("[%s] Tele: %d vorberechnete Portal-Kante(n)", self.callsign, len(edges))
            for entry, (exit_node, cost) in edges.items():
                ez = self._nav_graph.layers[entry[0]].z
                xz = self._nav_graph.layers[exit_node[0]].z
                logger.debug("[%s] Tele:   Kante z=%.0f → z=%.0f (cost=%.1f)%s",
                             self.callsign, ez, xz, cost,
                             " [cross-floor]" if abs(xz - ez) > 1.5 else "")

    def _on_add_player(self, code: int, payload: bytes) -> None:
        """Registriert neuen Spieler; inkrementiert human_count wenn Mensch."""
        if len(payload) < 1+2+2+2+2+2+CallSignLen: return
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("MsgAddPlayer raw[0:16]: %s",
                         " ".join(f"{b:02x}" for b in payload[:16]))
        off = 0
        pid      = unpack_uint8( payload, off); off += 1
        ptype    = unpack_uint16(payload, off); off += 2
        team     = unpack_uint16(payload, off); off += 2
        off += 6
        callsign = unpack_string(payload, off, CallSignLen)
        logger.debug("MsgAddPlayer: id=%d type=%d team=%d callsign=%r",
                     pid, ptype, team, callsign)
        is_bot = (ptype == PLAYER_TYPE_COMPUTER) or self._is_bot_callsign(callsign)
        is_obs = (team == TEAM_OBSERVER)
        self.players[pid] = PlayerInfo(
            callsign=callsign, team=team,
            is_human=not is_bot and not is_obs)
        if pid == self.player_id:
            self.team = team
        if is_obs:
            self.observer_count += 1
            logger.info("[%s] Observer beigetreten: %r (Beobachter: %d)",
                        self.callsign, callsign, self.observer_count)
        if self.players[pid].is_human:
            self.human_count += 1
            logger.info("[%s] Spieler beigetreten: %r (Menschen: %d)",
                        self.callsign, callsign, self.human_count)
            self._notify_count()

    def _on_remove_player(self, code: int, payload: bytes) -> None:
        """Entfernt Spieler; dekrementiert human_count wenn Mensch."""
        if len(payload) < 1: return
        pid  = unpack_uint8(payload, 0)
        info = self.players.pop(pid, None)
        with self._shots_lock:
            for k in [k for k in self._shots if k[0] == pid]:
                del self._shots[k]
                self._ricochet_paths.pop(k, None)
        self._evade_cleared_shots = {
            k: v for k, v in self._evade_cleared_shots.items() if k[0] != pid
        }
        if self.target_player == pid:
            self.target_player = None
        if info and info.team == TEAM_OBSERVER:
            self.observer_count = max(0, self.observer_count - 1)
            logger.info("[%s] Observer verlassen: %r (Beobachter: %d)",
                        self.callsign, info.callsign, self.observer_count)
        if info and info.is_human:
            self.human_count = max(0, self.human_count - 1)
            logger.info("[%s] Spieler verlassen: %r (Menschen: %d)",
                        self.callsign, info.callsign, self.human_count)
            self._notify_count()

    def _on_grab_flag(self, code: int, payload: bytes) -> None:
        """Aktualisiert Flag-Zustand."""
        if len(payload) < 5: return
        pid        = unpack_uint8( payload, 0)
        flag_index = unpack_uint16(payload, 1)
        flag_abbv  = payload[3:5].rstrip(b'\x00').decode('ascii', errors='replace')
        self.flags.pop(flag_index, None)
        if pid == self.player_id:
            self.own_flag = flag_abbv
            self._own_flag_since = time.monotonic()
            self._last_drop_attempt = 0.0
            self._shots_remaining = -1
            if flag_abbv in self._limited_flags:
                logger.info("[%s] Flag %r: limitiert (Schuss-Limit aktiv)", self.callsign, flag_abbv)
            if flag_abbv in self.good_flags:
                logger.info("[%s] Gute Flag aufgesammelt: %r – behalten", self.callsign, flag_abbv)
            else:
                _drop_delay = self._drop_bad_flag_delay if flag_abbv in self.bad_flags else 0.0
                logger.info("[%s] Flag aufgesammelt: %r – ablegen nach %.1fs",
                            self.callsign, flag_abbv, _drop_delay)
        elif pid in self.players:
            self.players[pid].flag = flag_abbv
            logger.debug("[%s] Spieler %d hält Flag %s", self.callsign, pid, flag_abbv)

    def _on_drop_flag(self, code: int, payload: bytes) -> None:
        """Aktualisiert Flag-Zustand nach Ablegen."""
        if len(payload) < 1: return
        pid = unpack_uint8(payload, 0)
        if pid == self.player_id:
            if (self.own_flag
                    and self.own_flag not in self.good_flags
                    and self.own_flag not in self.bad_flags):
                self._dropped_neutrals.append((self.own_flag, self.pos[0], self.pos[1]))
            if self.own_flag == "BU":
                self.pos[2] = 0.0
                self.vel[2] = 0.0
            logger.info("[%s] Flag %r erfolgreich abgelegt", self.callsign, self.own_flag)
            self.own_flag = ""
        elif pid in self.players:
            self.players[pid].flag = ""

    def _on_transfer_flag(self, code: int, payload: bytes) -> None:
        """Flagge via Thief-Schuss übertragen (Payload: from, to, flag_index, Flag::unpack())."""
        if len(payload) < 6:
            return
        from_id    = unpack_uint8( payload, 0)
        to_id      = unpack_uint8( payload, 1)
        flag_index = unpack_uint16(payload, 2)
        flag_abbv  = payload[4:6].rstrip(b'\x00').decode('ascii', errors='replace')
        if from_id in self.players:
            self.players[from_id].flag = ""
        if to_id in self.players:
            self.players[to_id].flag = flag_abbv
        if from_id == self.player_id:
            if self.own_flag == "BU":
                self.pos[2] = 0.0
                self.vel[2] = 0.0
            logger.info("[%s] MsgTransferFlag: Flagge '%s' gestohlen → Spieler %d",
                        self.callsign, self.own_flag, to_id)
            self.own_flag = ""
        elif to_id == self.player_id:
            self.flags.pop(flag_index, None)
            self.own_flag = flag_abbv
            self._own_flag_since = time.monotonic()
            self._last_drop_attempt = 0.0
            self._shots_remaining = -1
            if flag_abbv in self._limited_flags:
                logger.info("[%s] Flag %r: limitiert (TH-Diebstahl)", self.callsign, flag_abbv)
            if flag_abbv in self.good_flags:
                logger.info("[%s] TH-Diebstahl: Gute Flag %r – behalten", self.callsign, flag_abbv)
            else:
                _drop_delay = self._drop_bad_flag_delay if flag_abbv in self.bad_flags else 0.0
                logger.info("[%s] TH-Diebstahl: Flag %r – ablegen nach %.1fs",
                            self.callsign, flag_abbv, _drop_delay)

    def _on_capture_flag(self, code: int, payload: bytes) -> None:
        """Löscht Flag-Zustand nach Flaggenübernahme."""
        if len(payload) < 1: return
        pid = unpack_uint8(payload, 0)
        if pid in self.players:
            self.players[pid].flag = ""

    def _on_alive(self, code: int, payload: bytes) -> None:
        """Empfängt Spawn-Bestätigung; initialisiert Bot-Zustand."""
        if len(payload) < 13: return
        pid = unpack_uint8(payload, 0)
        if pid != self.player_id:
            if pid in self.players: self.players[pid].alive = True
            return
        x, y, z     = unpack_vec3(payload, 1)
        self.azimuth = unpack_float(payload, 13) if len(payload) >= 17 else 0.0
        self.pos     = [x, y, z]; self.vel = [0.0, 0.0, 0.0]
        self.alive   = True; self.death_time = None
        self._has_spawned = True          # hat diese Session gespielt → Rundenende darf reconnecten
        self._exploding_until = 0.0       # evtl. laufende Explosion beim Respawn beenden
        self._spawn_sent_at = None
        now = time.monotonic()
        self._last_pos_check_time = now
        self._last_pos_check      = [x, y]
        self._next_shoot          = now + SHOOT_INTERVAL_RANDOM_MAX
        self._slot_reload_at      = [now] * max(self._max_shots, 1)
        self._dodging             = False
        self._jumping             = False
        self._jump_ang_vel        = 0.0
        self._last_threat_id      = None
        self._evade_cleared_shots = {}
        self._threat_detected_at  = 0.0
        self._tick_count          = 0
        self._active_gm           = None
        self._jump_pending        = False
        self._tactical_jump_until = 0.0
        self._escape_jump_ang_vel = None
        self._dodge_forward       = False
        self._dodge_reverse       = False
        self._gm_need_update      = False
        self._gm_send_at          = None
        self._gm_resend_at        = None
        self._landing_shot_until  = 0.0
        self._landing_hit_z       = 0.0
        self._rico_aim_cache      = None
        self._indirect_hold_until = None
        with self._shots_lock:
            self._shots.clear()
            self._ricochet_paths.clear()
        # Hit-Fenster-Referenz auf den Spawn setzen: sonst würde das gesamte
        # Totliege-Fenster gegen die NEUE Position getestet (Geister-Treffer)
        # bzw. der Relativ-Sweep den Spawn-Sprung als Eigenbewegung werten.
        self._last_hit_check_t   = now
        self._last_hit_check_pos = (x, y, z)
        self._new_target()
        # Sicherheitsnetz gegen Zähler-Drift: human_count aus der Spielerliste neu ableiten.
        # Driftet er (Add/Remove-Asymmetrie) auf 0, würde der Bot sonst dauerhaft — auch über
        # Respawn — nicht mehr schießen (_maybe_shoot: human_count==0 → kein Schuss).
        self.human_count = sum(1 for p in self.players.values() if p.is_human)
        # State Machine: Spawn → Seeking oder Idle je nach Anwesenheit (Mitspieler ODER Zuschauer)
        self._ai_state = AIState.SEEKING if self._has_presence() else AIState.IDLE
        logger.info("[%s] Gespawnt bei (%.1f, %.1f, %.1f) → %s",
                    self.callsign, x, y, z, self._ai_state.name)

    def _on_killed(self, code: int, payload: bytes) -> None:
        """Empfängt Server-Kill-Meldung; aktualisiert alive-Status."""
        if len(payload) < 1: return
        victim = unpack_uint8(payload, 0)
        if victim == self.player_id:
            if self.alive:
                self.alive = False
                self.death_time = time.monotonic()
                self._start_explosion(self.death_time)   # Explosions-Bogen wie der echte Client
                self._dodging             = False
                self._jumping             = False
                self._jump_ang_vel        = 0.0
                self._last_threat_id      = None
                self._evade_cleared_shots = {}
                self._threat_detected_at  = 0.0
                self._active_gm           = None
                self._jump_pending        = False
                self._tactical_jump_until = 0.0
                self._escape_jump_ang_vel = None
                self._dodge_forward = self._dodge_reverse = False
                self._gm_need_update      = False
                self._gm_send_at = self._gm_resend_at = None
                self._dropped_neutrals.clear()
                self._recent_flag_targets.clear()
                self._transition_to(AIState.DEAD)
                if len(payload) >= 8:
                    killer = unpack_uint8(payload, 1)
                    flag_abbv = payload[6:8].rstrip(b'\x00').decode('ascii', errors='replace')
                    logger.info("[%s] Vom Server als getötet gemeldet: Killer=%d Flag=%r",
                                self.callsign, killer, flag_abbv)
        elif victim in self.players:
            self.players[victim].alive = False
            if self.target_player == victim: self.target_player = None
            # Genocide: wenn Teamkamerad mit G-Flagge getötet → Bot stirbt mit
            if (len(payload) >= 8 and self.alive and self.player_id is not None
                    and self.team not in (0, 0xFFFF, 0xFFFE)):  # nicht Rogue/Observer/Automatic
                flag_b = payload[6:8]
                victim_info = self.players.get(victim)
                if flag_b == b"G\x00" and victim_info and victim_info.team == self.team:
                    killer_id = unpack_uint8(payload, 1) if len(payload) >= 2 else 0
                    shot_id   = struct.unpack_from(">h", payload, 4)[0] if len(payload) >= 6 else -1
                    logger.info("[%s] Genocide: Teamkamerad %d gestorben — Bot stirbt mit",
                                self.callsign, victim)
                    self._jumping = False
                    self.alive = False
                    self.death_time = time.monotonic()
                    self._start_explosion(self.death_time)   # Explosions-Bogen wie der echte Client
                    self._transition_to(AIState.DEAD)
                    self.client.send(MsgKilled,
                        struct.pack(">B", killer_id)
                        + struct.pack(">H", KILL_REASON_GENOCIDED)
                        + struct.pack(">h", shot_id)
                        + b"G\x00"
                    )

    def _on_player_update_full(self, code: int, payload: bytes) -> None:
        """MsgPlayerUpdate (0x7075) von anderen Spielern."""
        if len(payload) < 4+1+4+2+12: return
        off = 0
        _ts  = unpack_float( payload, off); off += 4
        pid  = unpack_uint8( payload, off); off += 1
        if pid == self.player_id: return
        _ord = struct.unpack_from(">i", payload, off)[0]; off += 4
        _st  = unpack_int16(  payload, off); off += 2
        if len(payload) < off + 12: return
        x, y, z = unpack_vec3(payload, off); off += 12
        vx = vy = vz = 0.0
        az = 0.0
        if len(payload) >= off + 12:
            vx, vy, vz = unpack_vec3(payload, off); off += 12
        if len(payload) >= off + 4:
            az = unpack_float(payload, off)
        if pid in self.players:
            p = self.players[pid]
            if p.last_order >= 0 and _ord <= p.last_order:
                return
            p.last_order = _ord
            now_t = time.monotonic()
            if not self._should_update_player(p, x, y, z, now_t):
                return
            p.pos = [x, y, z]; p.vel = [vx, vy, vz]; p.azimuth = az
            p.alive           = bool(_st & PS_ALIVE)
            p.is_airborne      = bool(_st & PS_FALLING)
            p.is_phantom_zoned = bool(_st & PS_FLAG_ACTIVE) and p.flag == "PZ"
            p.last_seen  = now_t

    def _on_player_update_small(self, code: int, payload: bytes) -> None:
        """MsgPlayerUpdateSmall (0x7073) – komprimiertes Positions-Update."""
        if len(payload) < 4+1+4+2+6: return
        off = 0
        _ts  = unpack_float(  payload, off); off += 4
        pid  = unpack_uint8(  payload, off); off += 1
        if pid == self.player_id: return
        _ord = struct.unpack_from(">i", payload, off)[0]; off += 4
        _st  = unpack_int16(  payload, off); off += 2
        if len(payload) < off + 6: return
        px = struct.unpack_from(">h", payload, off)[0]; off += 2
        py = struct.unpack_from(">h", payload, off)[0]; off += 2
        pz = struct.unpack_from(">h", payload, off)[0]; off += 2
        # Skalierung zurückrechnen: int16 → Weltkoordinate (Faktor aus BZFlag-Protokoll)
        x = (px * SMALL_MAX_DIST) / SMALL_SCALE
        y = (py * SMALL_MAX_DIST) / SMALL_SCALE
        z = (pz * SMALL_MAX_DIST) / SMALL_SCALE
        vx = vy = vz = 0.0; az = 0.0
        if len(payload) >= off + 6:
            vxs = struct.unpack_from(">h", payload, off)[0]; off += 2
            vys = struct.unpack_from(">h", payload, off)[0]; off += 2
            vzs = struct.unpack_from(">h", payload, off)[0]; off += 2
            vx = (vxs * SMALL_MAX_VEL) / SMALL_SCALE
            vy = (vys * SMALL_MAX_VEL) / SMALL_SCALE
            vz = (vzs * SMALL_MAX_VEL) / SMALL_SCALE
        if len(payload) >= off + 2:
            azs = struct.unpack_from(">h", payload, off)[0]; off += 2
            az  = (azs * math.pi) / SMALL_SCALE
        if pid in self.players:
            p = self.players[pid]
            if p.last_order >= 0 and _ord <= p.last_order:
                return
            p.last_order = _ord
            now_t = time.monotonic()
            if not self._should_update_player(p, x, y, z, now_t):
                return
            p.pos = [x, y, z]; p.vel = [vx, vy, vz]; p.azimuth = az
            p.alive           = bool(_st & PS_ALIVE)
            p.is_airborne      = bool(_st & PS_FALLING)
            p.is_phantom_zoned = bool(_st & PS_FLAG_ACTIVE) and p.flag == "PZ"
            p.last_seen  = now_t

    def _on_pause(self, code: int, payload: bytes) -> None:
        """MsgPause (0x7061): Spieler pausiert/un-pausiert. Payload [pid:uint8][paused:uint8].
        Pausiert = unverwundbar → KI feuert nicht und wartet (s. _tick_combat)."""
        if len(payload) < 2: return
        pid    = unpack_uint8(payload, 0)
        paused = bool(unpack_uint8(payload, 1))
        p = self.players.get(pid)
        if p is not None:
            p.paused = paused
            logger.debug("[%s] Spieler %d %spausiert", self.callsign, pid, "" if paused else "un")

    def _on_shot_begin(self, code: int, payload: bytes) -> None:
        """Registriert neuen Schuss; Sofort-Check für Laser und SW."""
        if len(payload) < 43: return
        off = 0
        _ts        = unpack_float(  payload, off); off += 4
        shooter    = unpack_uint8(  payload, off); off += 1
        shot_id    = unpack_uint16( payload, off); off += 2
        px, py, pz = unpack_vec3(   payload, off); off += 12
        vx, vy, vz = unpack_vec3(   payload, off); off += 12
        _dt        = unpack_float(  payload, off); off += 4
        team       = unpack_int16(  payload, off); off += 2
        flag_type  = payload[off:off+2];           off += 2
        lifetime   = unpack_float(  payload, off)
        shot = Shot(shooter_id=shooter, shot_id=shot_id,
                    pos=[px, py, pz], vel=[vx, vy, vz],
                    fire_time=time.monotonic(),
                    lifetime=lifetime if lifetime > 0 else self._shot_lifetime,
                    team=team,
                    is_gm=(flag_type == b"GM"),
                    is_laser=(flag_type == b"L\x00"),
                    is_sw=(flag_type == b"SW"),
                    is_thief=(flag_type == b"TH"),
                    flag_abbr=flag_type)
        # P3: Schusspfad-Simulation VOR dem Lock — sie liest nur statische
        # Weltdaten und lokale Schussparameter. Im Lock würde sie die 60-Hz-
        # Schleife (_resolve_incoming_shots/_find_incoming_shot) bei Schuss-
        # Bursts millisekundenlang blockieren; so bleibt die Haltezeit bei
        # Mikrosekunden und Shot+Pfad landen weiterhin atomar in beiden Dicts.
        _is_pz = bool(self.players.get(shooter, None) and self.players[shooter].is_phantom_zoned)
        # Teleporter transportieren jeden Schuss (auch Nicht-Ricochet) → Pfad auch dann
        # vorberechnen, wenn die Karte Teleporter hat. Sonst landet ein nicht-ricochet
        # Laser/Thief im geraden-Linien-else-Zweig und „sieht" den Teleporter nie.
        # Wand-durchdringende Schüsse (PhantomZone, Super Bullet) teleportieren
        # EBENFALLS (makeSegments: Teleporter-Lookup läuft unabhängig vom
        # ObstacleEffect; live verifiziert) — sie laufen im phase_walls-Modus
        # durch die Pfad-Sim, der Wände/Weltgrenzen ignoriert, Teleporter nicht.
        _phases_walls = _is_pz or shot.flag_abbr == b"SB"
        # GM bekommt KEINEN Pfad-Cache: die Rakete lenkt, ein vorberechneter
        # gerader Pfad wäre falsch — und _find_incoming_shot würde den GM wegen
        # des Cache-Eintrags im Direkt-Zweig überspringen und stattdessen den
        # falschen Pfad bewerten. Die live nachgeführte shot.pos (Integration
        # in _resolve_incoming_shots + MsgGMUpdate) ist für GM die Wahrheit.
        # SW hat keinen Pfad (Explosion am Ort) → ebenfalls kein Cache.
        _has_teles = (self._world_map is not None
                      and bool(self._world_map.teleporters))
        _tele_route = _has_teles and not _phases_walls and not shot.is_gm
        _tele_phase = (_has_teles and _phases_walls
                       and not shot.is_gm and not shot.is_sw)
        path_segs = None
        _tlog = [] if self._debug_log_tele else None
        if _tele_phase or (self._world_map is not None
                and (_tele_route
                     or _can_ricochet_shot(shot.flag_abbr, shot.is_gm, shot.is_sw,
                                           self._server_ricochet, is_phantom_zoned=_is_pz))):
            path_segs = simulate_shot_path(
                (px, py, pz), (vx, vy, vz), shot.fire_time, shot.lifetime,
                shot.flag_abbr, self._world_map.boxes,
                self.world_half, self._server_ricochet,
                wall_height=self._wall_height,
                teleporters=self._world_map.teleporters,
                link_map=self._link_map,
                tele_log=_tlog,
                solid_obs=self._world_map.solid_obstacles(),
                obs_grid=self._shot_grid,
                phase_walls=_tele_phase,
            )
        with self._shots_lock:
            self._shots[(shooter, shot_id)] = shot
            if path_segs is not None:
                self._ricochet_paths[(shooter, shot_id)] = path_segs
        if path_segs is not None and self._debug_log_tele:
            if _tlog:
                teles = self._world_map.teleporters
                for e_ti, e_face, d_ti, d_face, ep, xp, ain, aout in _tlog:
                    en = teles[e_ti].name or f"/t{e_ti}"
                    xn = teles[d_ti].name or f"/t{d_ti}"
                    logger.debug(
                        "[%s] Tele: Schuss %d/%d  %s:%s → %s:%s  "
                        "ein(%.1f,%.1f,%.1f)@%.0f°  aus(%.1f,%.1f,%.1f)@%.0f°",
                        self.callsign, shooter, shot_id,
                        en, "fb"[e_face], xn, "fb"[d_face],
                        ep[0], ep[1], ep[2], math.degrees(ain),
                        xp[0], xp[1], xp[2], math.degrees(aout))
            else:
                logger.debug("[%s] Tele: Schuss %d/%d NICHT teleportiert",
                             self.callsign, shooter, shot_id)
        # (d) Wahrnehmbarer Schuss verrät die Schützen-Position zu Schussbeginn: erzwungenes
        # Einmal-Update auf den Schuss-Ursprung (x,y; z ist Mündungshöhe) — umgeht die Radar-
        # Aufmerksamkeit, deckt so auch sonst unsichtbare (ST/CL) Gegner kurz auf.
        _shooter_info = self.players.get(shooter)
        if (_shooter_info is not None and shooter != self.player_id
                and self._shot_reveals_shooter(_shooter_info, px, py, pz)):
            _shooter_info.pos[0] = px; _shooter_info.pos[1] = py
            _shooter_info.last_seen = time.monotonic()
            _shooter_info.radar_blind_until = 0.0
        if shot.is_laser and self.alive and self.player_id is not None:
            # Laser ist effektiv instant → Sofortcheck (Segmente falls gecacht, sonst Gerade)
            if self._instant_shot_hits(shooter, shot_id, px, py, pz, vx, vy, vz, shot.lifetime):
                _rico = (shooter, shot_id) in self._ricochet_paths
                logger.info("[%s] Laser-Treffer %svon Spieler %d",
                            self.callsign, "(Abpraller) " if _rico else "", shooter)
                self._report_killed(shot)
        if shot.is_thief and self.alive and self.player_id is not None:
            # Thief ist effektiv instant → Sofortcheck; Treffer stiehlt die Flagge (kein Kill)
            if self._instant_shot_hits(shooter, shot_id, px, py, pz, vx, vy, vz, shot.lifetime):
                if self.own_flag:
                    _rico = (shooter, shot_id) in self._ricochet_paths
                    logger.info("[%s] Flagge '%s' durch TH-%s von %d gestohlen — sende MsgTransferFlag",
                                self.callsign, self.own_flag,
                                "Abpraller" if _rico else "Schuss", shooter)
                    self.client.send(MsgTransferFlag,
                                     struct.pack(">BB", self.player_id, shooter))
                elif getattr(self, '_debug_log_shot', False):
                    logger.debug("[%s] Schuss: TH-Treffer von %d – keine eigene Flagge vorhanden",
                                 self.callsign, shooter)
        if shot.is_sw and self.alive and self.player_id is not None:
            tank_cz_sw = self.pos[2] + self._tank_height / 2
            _sw_dist = math.sqrt(
                (px - self.pos[0])**2 +
                (py - self.pos[1])**2 +
                (pz - tank_cz_sw)**2
            )

    def _on_shot_end(self, code: int, payload: bytes) -> None:
        """Entfernt abgelaufenen Schuss."""
        if len(payload) < 3: return
        shooter = unpack_uint8( payload, 0)
        shot_id = unpack_uint16(payload, 1)
        with self._shots_lock:
            self._shots.pop((shooter, shot_id), None)
            self._ricochet_paths.pop((shooter, shot_id), None)

    def _on_teleport(self, code: int, payload: bytes) -> None:
        """MsgTeleport: ein Spieler hat sich teleportiert (playerIndex, from_face, to_face).

        noch kein aktiver Konsument — wir merken den letzten Teleport am Spieler
        und beenden den „Unbehandelt 0x7470 'tp'"-Log. Die folgenden PlayerUpdates liefern
        die neue Position ohnehin.
        """
        if len(payload) < 5:
            return
        pid       = unpack_uint8(payload, 0)
        from_face = unpack_uint16(payload, 1)
        to_face   = unpack_uint16(payload, 3)
        p = self.players.get(pid)
        if p is not None:
            p.last_teleport = (time.monotonic(), from_face, to_face)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[%s] Teleport: Spieler %d Face %d → %d",
                         self.callsign, pid, from_face, to_face)

    def _on_gm_update(self, code: int, payload: bytes) -> None:
        """Aktualisiert GM-Position und prüft direkt auf Treffer."""
        if len(payload) < 34: return
        off = 0
        shooter  = unpack_uint8( payload, off); off += 1
        shot_id  = unpack_uint16(payload, off); off += 2
        px, py, pz = unpack_vec3(payload, off); off += 12
        vx, vy, vz = unpack_vec3(payload, off); off += 12
        _dt      = unpack_float( payload, off); off += 4
        _team    = unpack_int16( payload, off); off += 2
        target   = unpack_uint8( payload, off)
        if shooter == self.player_id: return
        with self._shots_lock:
            key = (shooter, shot_id)
            if key in self._shots:
                s = self._shots[key]
                s.pos = [px, py, pz]; s.vel = [vx, vy, vz]
                s.last_gm_update = time.monotonic()
                s.gm_target_pid  = target
        tank_cz = self.pos[2] + self._tank_height / 2
        dist3d  = math.sqrt((px-self.pos[0])**2 + (py-self.pos[1])**2 + (pz-tank_cz)**2)
        if self.alive and self.player_id is not None and dist3d < HIT_RADIUS:
            shot_obj = None
            with self._shots_lock:
                shot_obj = self._shots.get((shooter, shot_id))
            if shot_obj is not None:
                logger.debug("[%s] GM-Treffer von Spieler %d erkannt", self.callsign, shooter)
                self._report_killed(shot_obj)
        logger.debug("GMUpdate [%d/%d] pos=(%.0f,%.0f) vel=(%.0f,%.0f) target=%d",
                     shooter, shot_id, px, py, vx, vy, target)

    def _on_flag_update(self, code: int, payload: bytes) -> None:
        """Aktualisiert Flag-Positionen aus MsgFlagUpdate."""
        if len(payload) < 2: return
        off = 0
        count = unpack_uint16(payload, off); off += 2
        for _ in range(count):
            flag_start = off
            if flag_start + 57 > len(payload): break
            flag_id = unpack_uint16(payload, off); off += 2
            abbr_b  = payload[off:off+2];          off += 2
            status  = unpack_uint16(payload, off);  off += 2
            off += 2 + 1
            x, y, z = unpack_vec3(payload, off)
            off = flag_start + 57
            abbr = abbr_b.rstrip(b'\x00').decode('ascii', errors='replace')
            if abbr == "PZ":
                abbr = ""  # Server-seitiger Fake-Placeholder für unidentifizierte Flaggen
            if status in (0, 2):
                self.flags.pop(flag_id, None)
            else:
                self.flags[flag_id] = FlagInfo(flag_id=flag_id, abbr=abbr,
                                               status=status, pos=[x, y, z])

    def _on_near_flag(self, code: int, payload: bytes) -> None:
        """Identifiziert Flaggentyp via MsgNearFlag (Server sendet nur bei ID-Flagge)."""
        if self.own_flag != "ID" or len(payload) < 16:
            return
        x = unpack_float(payload, 0)
        y = unpack_float(payload, 4)
        z = unpack_float(payload, 8)
        name_len = unpack_uint32(payload, 12)
        if len(payload) < 16 + name_len:
            return
        flag_name = payload[16:16 + name_len].decode('ascii', errors='replace')
        abbr = FLAG_NAME_TO_ABBR.get(flag_name, "")
        if not abbr:
            if getattr(self, '_debug_log_flag', False):
                logger.debug("[%s] Flagge: MsgNearFlag – unbekannter Flagname %r", self.callsign, flag_name)
            return
        best_fi = None
        best_d2 = 25.0  # 5u² Toleranz
        for fi in self.flags.values():
            d2 = (fi.pos[0] - x) ** 2 + (fi.pos[1] - y) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_fi = fi
        if best_fi is None:
            return
        if getattr(self, '_debug_log_flag', False):
            logger.debug("[%s] Flagge: MsgNearFlag – Flag %d bei (%.0f,%.0f) = %r (%s)",
                         self.callsign, best_fi.flag_id, x, y, abbr, flag_name)
        best_fi.abbr = abbr
        if abbr in self.good_flags and time.monotonic() - self._last_drop_attempt > 1.0:
            logger.info("[%s] MsgNearFlag: Gute Flagge %r bei (%.0f,%.0f) — ID ablegen",
                        self.callsign, abbr, x, y)
            self._try_drop_flag()

    def _on_game_time(self, code: int, payload: bytes) -> None:
        """Synchronisiert Server-Zeitbasis aus MsgGameTime."""
        if len(payload) >= 8:
            msb = struct.unpack_from(">I", payload, 0)[0]
            lsb = struct.unpack_from(">I", payload, 4)[0]
            server_s = ((msb << 32) | lsb) / 1_000_000.0
            self._server_time_offset = server_s - time.monotonic()

    def _begin_round_over(self, reason: str) -> None:
        """Rundenende wie der echte Client (explodeTank, KEINE MsgKilled): Bot explodiert und
        bleibt ROUND_END_LINGER s sichtbar in der Endstand-Tabelle, dann Reconnect. Während der
        Explosion sendet _send_update PS_EXPLODING (Alive gelöscht → kein [nr]).

        Beim Beitritt zwischen Runden (noch nie gespielt) NICHT reconnecten/explodieren — sonst
        Dauerschleife: nur _game_over merken und auf den Rundenstart (timeLeft>0) warten."""
        self._game_over = True
        if not self._has_spawned:
            # Zwischen Runden beigetreten: evtl. schon abgesetzte (vom Server ignorierte) Spawn-
            # Anfrage vergessen, damit der Rundenstart-Spawn (timeLeft>0) sauber feuert.
            self._spawn_sent_at = None
            self.death_time = None
            logger.info("[%s] %s beim Beitritt — warte auf Rundenstart", self.callsign, reason)
            return
        if self._round_over_until is not None:
            return  # schon ausgelöst (z. B. TimeUpdate + ScoreOver kurz nacheinander)
        now = time.monotonic()
        logger.info("[%s] %s — explodiere, %.0fs sichtbar, dann reconnect",
                    self.callsign, reason, ROUND_END_LINGER)
        self._start_explosion(now)
        self.alive = False
        self._reconnect_needed = True
        self._round_over = True   # echtes Rundenende: Managed→Exit/Manager-Rejoin, Standalone→5s-Gap
        self._round_over_until = now + max(ROUND_END_LINGER, EXPLODE_TIME)

    def _on_time_update(self, code: int, payload: bytes) -> None:
        """timeLeft>0: Runde aktiv (ggf. spawnen). =0: Rundenende. <0: Countdown-Pause (kein Ende)."""
        if len(payload) < 4:
            return
        time_left = struct.unpack_from('!i', payload)[0]
        if time_left > 0:
            if (self._game_over and not self.alive
                    and self.death_time is None and self._spawn_sent_at is None):
                logger.info("[%s] Rundenstart (timeLeft=%d) — spawnen", self.callsign, time_left)
                self._spawn()
            self._game_over = False
        elif time_left == 0:
            self._begin_round_over("Rundenende (timeLeft=0)")
        else:
            logger.info("[%s] Countdown pausiert (timeLeft=%d)", self.callsign, time_left)

    def _on_score_over(self, code: int, payload: bytes) -> None:
        """Rundenende durch Score-Limit oder Admin-/gameover — wie _begin_round_over."""
        winner_id = payload[0] if payload else 255
        self._begin_round_over("MsgScoreOver (winner=%d)" % winner_id)

    def _on_set_var(self, code: int, payload: bytes) -> None:
        """Liest physikalische Server-Variablen aus MsgSetVar."""
        try:
            off = 0
            count = unpack_uint16(payload, off); off += 2
            for _ in range(count):
                if off+1 > len(payload): break
                nlen = unpack_uint8(payload, off); off += 1
                if off+nlen > len(payload): break
                name = payload[off:off+nlen].decode("utf-8", "?"); off += nlen
                if off+1 > len(payload): break
                vlen = unpack_uint8(payload, off); off += 1
                if off+vlen > len(payload): break
                val  = payload[off:off+vlen].decode("utf-8", "?"); off += vlen
                if name == "_worldSize":
                    try:
                        new_half = float(val) / 2.0
                        old_half = self.world_half
                        self.world_half = new_half
                        self.client._world_half_cache = new_half
                        if self._world_map is not None and abs(new_half - old_half) > 0.1:
                            from bzflag.nav_graph import invalidate_nav_cache
                            invalidate_nav_cache(self._world_map.world_hash)
                            logger.debug("[%s] _worldSize=%.0f → NavGraph-Rebuild (vorher %.0fu, jetzt %.0fu)",
                                        self.callsign, float(val), old_half, new_half)
                            self.client._deliver_world()
                    except ValueError: pass
                elif name == "_maxShots":
                    try:
                        ms = int(float(val))
                        if ms > 0:
                            self._max_shots = ms
                            logger.debug("[%s] _maxShots=%d", self.callsign, ms)
                    except ValueError: pass
                elif name == "_reloadTime":
                    try:
                        rt = float(val)
                        if rt > 0:
                            self._reload_time = rt
                            self._sw_expand_speed = (self._shock_out_radius - self._shock_in_radius) / (self._reload_time * self._shock_ad_life)
                            logger.debug("[%s] _reloadTime=%.2f", self.callsign, rt)
                    except ValueError: pass
                elif name == "_shotSpeed":
                    try:
                        v = float(val)
                        if v > 0:
                            self._shot_speed = v
                            self._shot_lifetime = self._shot_range / v
                            self._recompute_gm_min_range()
                            logger.debug("[%s] _shotSpeed=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_shotRange":
                    try:
                        v = float(val)
                        if v > 0:
                            self._shot_range = v
                            self._shot_lifetime = v / self._shot_speed
                            logger.debug("[%s] _shotRange=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_tankSpeed":
                    try:
                        v = float(val)
                        if v > 0:
                            self._tank_speed = v
                            logger.debug("[%s] _tankSpeed=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_tankAngVel":
                    try:
                        v = float(val)
                        if v > 0:
                            self._tank_turn_rate = v
                            logger.debug("[%s] _tankAngVel=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_jumpVelocity":
                    try:
                        v = float(val)
                        if v > 0:
                            self._jump_velocity = v
                            self._sync_nav_physics()
                            logger.debug("[%s] _jumpVelocity=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_updateThrottleRate":
                    try:
                        v = float(val)
                        if v > 0:
                            self._server_update_interval = max(1.0 / SERVER_UPDATE_RATE_HZ, 1.0 / v)
                            logger.debug("[%s] _updateThrottleRate=%.1f → Intervall=%.3fs",
                                        self.callsign, v, self._server_update_interval)
                    except ValueError: pass
                elif name == "_gravity":
                    try:
                        v = float(val)
                        if v != 0:
                            self._gravity = v
                            self._sync_nav_physics()
                            logger.debug("[%s] _gravity=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_wingsGravity":
                    try:
                        v = float(val)
                        if v != 0:
                            self._wings_gravity = v
                            logger.debug("[%s] _wingsGravity=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_wingsJumpVelocity":
                    try:
                        v = float(val)
                        if v > 0:
                            self._wings_jump_velocity = v
                            logger.debug("[%s] _wingsJumpVelocity=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_dropBadFlagDelay":
                    try:
                        v = float(val)
                        if v >= 0:
                            self._drop_bad_flag_delay = v
                            logger.debug("[%s] _dropBadFlagDelay=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_shockInRadius":
                    try:
                        v = float(val)
                        if v >= 0:
                            self._shock_in_radius = v
                            self._sw_expand_speed = (self._shock_out_radius - self._shock_in_radius) / (self._reload_time * self._shock_ad_life)
                            logger.debug("[%s] _shockInRadius=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_shockOutRadius":
                    try:
                        v = float(val)
                        if v > 0:
                            self._shock_out_radius = v
                            self._sw_expand_speed = (self._shock_out_radius - self._shock_in_radius) / (self._reload_time * self._shock_ad_life)
                            logger.debug("[%s] _shockOutRadius=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_shockAdLife":
                    try:
                        v = float(val)
                        if v > 0:
                            self._shock_ad_life = v
                            self._sw_expand_speed = (self._shock_out_radius - self._shock_in_radius) / (self._reload_time * self._shock_ad_life)
                            logger.debug("[%s] _shockAdLife=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_gmTurnAngle":
                    try:
                        v = float(val)
                        if v > 0: self._gm_turn_angle = v; logger.debug("[%s] _gmTurnAngle=%.4f", self.callsign, v)
                    except ValueError: pass
                elif name == "_gmActivationTime":
                    try:
                        v = float(val)
                        if v >= 0:
                            self._gm_activation_time = v
                            self._recompute_gm_min_range()
                            logger.debug("[%s] _gmActivationTime=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_gmAdLife":
                    try:
                        v = float(val)
                        if v > 0: self._gm_ad_life = v; logger.debug("[%s] _gmAdLife=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_lockOnAngle":
                    try:
                        v = float(val)
                        if v > 0: self._lock_on_angle = v; logger.debug("[%s] _lockOnAngle=%.4f", self.callsign, v)
                    except ValueError: pass
                elif name == "_obeseFactor":
                    try:
                        v = float(val)
                        if v > 0: self._obese_factor = v; logger.debug("[%s] _obeseFactor=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_flagRadius":
                    try:
                        v = float(val)
                        if v > 0: self._flag_radius = v; logger.debug("[%s] _flagRadius=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_velocityAd":
                    try:
                        v = float(val)
                        if v > 0: self._velocity_ad = v; logger.debug("[%s] _velocityAd=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_agilityAdVel":
                    try:
                        v = float(val)
                        if v > 0: self._agility_ad_vel = v; logger.debug("[%s] _agilityAdVel=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_lgGravity":
                    try:
                        v = float(val)
                        if v > 0: self._lg_gravity = v; logger.debug("[%s] _lgGravity=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_burrowDepth":
                    try:
                        v = float(val)
                        self._burrow_depth = v; logger.debug("[%s] _burrowDepth=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_burrowSpeedAd":
                    try:
                        v = float(val)
                        if v > 0: self._burrow_speed_ad = v; logger.debug("[%s] _burrowSpeedAd=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_burrowAngularAd":
                    try:
                        v = float(val)
                        if v > 0: self._burrow_ang_ad = v; logger.debug("[%s] _burrowAngularAd=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_angularAd":
                    try:
                        v = float(val)
                        if v > 0: self._angular_ad = v; logger.debug("[%s] _angularAd=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_wideAngleAng":
                    try:
                        v = float(val)
                        if v > 0: self._wide_angle_ang = v; logger.debug("[%s] _wideAngleAng=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_shieldFlight":
                    try:
                        v = float(val)
                        if v >= 0: self._shield_flight = v; logger.debug("[%s] _shieldFlight=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_identifyRange":
                    try:
                        v = float(val)
                        if v >= 0: self._identify_range = v; logger.debug("[%s] _identifyRange=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_mGunAdRate":
                    try:
                        v = float(val)
                        if v > 0: self._mgun_ad_rate = v; logger.debug("[%s] _mGunAdRate=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_mGunAdLife":
                    try:
                        v = float(val)
                        if v > 0: self._mgun_ad_life = v; logger.debug("[%s] _mGunAdLife=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_mGunAdVel":
                    try:
                        v = float(val)
                        if v > 0: self._mgun_ad_vel = v; logger.debug("[%s] _mGunAdVel=%.3f", self.callsign, v)
                    except ValueError: pass
                elif name == "_rFireAdRate":
                    try:
                        v = float(val)
                        if v > 0: self._rfire_ad_rate = v; logger.debug("[%s] _rFireAdRate=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_rFireAdVel":
                    try:
                        v = float(val)
                        if v > 0: self._rfire_ad_vel = v; logger.debug("[%s] _rFireAdVel=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_rFireAdLife":
                    try:
                        v = float(val)
                        if v > 0: self._rfire_ad_life = v; logger.debug("[%s] _rFireAdLife=%.3f", self.callsign, v)
                    except ValueError: pass
                elif name == "_laserAdVel":
                    try:
                        v = float(val)
                        if v > 0: self._laser_ad_vel = v; logger.debug("[%s] _laserAdVel=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_laserAdRate":
                    try:
                        v = float(val)
                        if v > 0: self._laser_ad_rate = v; logger.debug("[%s] _laserAdRate=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_laserAdLife":
                    try:
                        v = float(val)
                        if v > 0: self._laser_ad_life = v; logger.debug("[%s] _laserAdLife=%.3f", self.callsign, v)
                    except ValueError: pass
                elif name == "_thiefAdShotVel":
                    try:
                        v = float(val)
                        if v > 0: self._thief_ad_shot_vel = v; logger.debug("[%s] _thiefAdShotVel=%.1f", self.callsign, v)
                    except ValueError: pass
                elif name == "_thiefAdLife":
                    try:
                        v = float(val)
                        if v > 0: self._thief_ad_life = v; logger.debug("[%s] _thiefAdLife=%.3f", self.callsign, v)
                    except ValueError: pass
                elif name == "_wingsJumpCount":
                    try:
                        v = int(float(val))
                        if v >= 0: self._wings_jump_count = v; logger.debug("[%s] _wingsJumpCount=%d", self.callsign, v)
                    except ValueError: pass
                elif name == "_muzzleHeight":
                    try:
                        v = float(val)
                        if v >= 0: self._muzzle_height = v; logger.debug("[%s] _muzzleHeight=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_muzzleFront":
                    try:
                        v = float(val)
                        if v >= 0: self._muzzle_front = v; logger.debug("[%s] _muzzleFront=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_tankLength":
                    try:
                        v = float(val)
                        if v > 0: self._tank_length = v; logger.debug("[%s] _tankLength=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_tankWidth":
                    try:
                        v = float(val)
                        if v > 0: self._tank_width = v; logger.debug("[%s] _tankWidth=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_tankHeight":
                    try:
                        v = float(val)
                        if v > 0:
                            self._tank_height = v
                            self._wall_height = 3.0 * v
                            logger.debug("[%s] _tankHeight=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_wallHeight":
                    try:
                        v = float(val)
                        if v > 0: self._wall_height = v
                    except ValueError: pass
                elif name == "_shotRadius":
                    try:
                        v = float(val)
                        if v >= 0: self._shot_radius = v; logger.debug("[%s] _shotRadius=%.2f", self.callsign, v)
                    except ValueError: pass
                elif name == "_tinyFactor":
                    try:
                        v = float(val)
                        if v > 0: self._tiny_factor = v; logger.debug("[%s] _tinyFactor=%.3f", self.callsign, v)
                    except ValueError: pass
                elif name == "_thiefTinyFactor":
                    try:
                        v = float(val)
                        if v > 0: self._thief_tiny_factor = v; logger.debug("[%s] _thiefTinyFactor=%.3f", self.callsign, v)
                    except ValueError: pass
                elif name == "_thiefVelAd":
                    try:
                        v = float(val)
                        if v > 0: self._thief_vel_ad = v; logger.debug("[%s] _thiefVelAd=%.3f", self.callsign, v)
                    except ValueError: pass
                elif name == "_narrowHW":
                    try:
                        v = float(val)
                        if v >= 0: self._narrow_hw = v; logger.debug("[%s] _narrowHW=%.3f", self.callsign, v)
                    except ValueError: pass
        except Exception: pass

    def _on_disconnect(self, code: int, payload: bytes) -> None:
        """Stoppt Spielschleife bei Verbindungsverlust."""
        logger.warning("[%s] Verbindung verloren", self.callsign)
        self._running = False; self._stop_event.set()

    def _on_message(self, code: int, payload: bytes) -> None:
        """Verarbeitet MsgMessage: erkennt Server-Schusslimit-Benachrichtigungen."""
        if len(payload) < 4:
            return
        src = payload[0]
        if src != 253:   # 253 = ServerPlayer (BZFlag include/Address.h:75)
            return
        text = payload[3:].decode("utf-8", errors="replace").rstrip("\x00")
        m = re.match(r'^(\d+) shots? left$', text)
        if m and self.own_flag:
            self._shots_remaining = int(m.group(1))
            if self.own_flag not in self._limited_flags:
                self._limited_flags.add(self.own_flag)
                logger.info("[%s] Flag %r als limitiert erkannt (MsgMessage)",
                            self.callsign, self.own_flag)
            logger.info("[%s] %s: noch %d Schüsse verbleibend",
                        self.callsign, self.own_flag, self._shots_remaining)

    def _ignored(self, code: int, payload: bytes) -> None:
        """Leerer Handler für bekannte, nicht ausgewertete Message-Typen."""
