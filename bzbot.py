#!/usr/bin/env python3
"""BZFlag-2.4-Bot: Protokoll, Netzwerk, Spielmechanik, Hit-Detection.

Bewegungs- und KI-Logik → bzbot_ai.py (BZBotAI-Mixin).
Konstanten, AIState-Enum und Hilfsfunktionen ebenfalls in bzbot_ai.py.
"""

import argparse, collections, json, logging, math, random, re, struct, sys, time, threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from bzflag.client       import BZFlagClient
from bzflag.shot_physics import (simulate_shot_path,
                                  can_ricochet as _can_ricochet_shot,
                                  build_link_map,
                                  _segment_hits_obb_3d)
from bzflag.world_map import teleporter_solid_boxes
from bzflag.protocol import (
    DEFAULT_PORT, PLAYER_TYPE_TANK, PLAYER_TYPE_COMPUTER,
    TEAM_AUTOMATIC, TEAM_OBSERVER,
    MsgAlive, MsgKilled, MsgAddPlayer, MsgRemovePlayer,
    MsgPlayerUpdate, MsgPlayerUpdateSmall,
    MsgShotBegin, MsgShotEnd, MsgSetVar,
    MsgPlayerInfo, MsgTeamUpdate, MsgFlagUpdate,
    MsgTimeUpdate, MsgScore, MsgMessage, MsgGameTime,
    MsgHandicap, MsgLagState, MsgGMUpdate, MsgNearFlag,
    MsgGrabFlag, MsgDropFlag, MsgCaptureFlag, MsgTransferFlag,
    MsgSuperKill, MsgScoreOver, MsgTeleport, MsgPause,
    MSG_INTERNAL_DISCONNECT, MGR_STATUS_PREFIX, BOT_EXIT_REJECTED,
    BOT_EXIT_ROUND_OVER, ROUND_RESTART_GAP_S,
    PS_ALIVE, PS_FALLING, PS_EXPLODING, PS_FLAG_ACTIVE, PS_TELEPORTING,
    build_player_update,
    unpack_uint8, unpack_uint16, unpack_int16, unpack_uint32,
    unpack_vec3, unpack_float, unpack_string,
    CallSignLen,
)

from bzbot_ai import (
    BZBotAI, AIState,
    _angle_diff, _wrap,
    # Konstanten (re-exportiert damit bestehende Tests/Imports weiter funktionieren)
    TANK_LENGTH, TANK_RADIUS, TANK_SPEED, TANK_TURN_RATE, FLAG_GRAB_RADIUS,
    SHOT_SPEED_DEFAULT, SHOT_RANGE, SHOT_LIFETIME, MAX_SHOTS_DEFAULT,
    JUMP_VELOCITY, GRAVITY, MUZZLE_FRONT, MUZZLE_HEIGHT,
    SHOCK_IN_RADIUS, SHOCK_OUT_RADIUS, SHOCK_AD_LIFE, SW_EXPAND_SPEED, OPTIMAL_RANGE, JUMP_COOLDOWN,
    TANK_HALF_LENGTH, GM_TURN_RATE, GM_ACTIVATION_TIME, GM_AD_LIFE, GM_LOCK_ON_ANGLE,
    FLAG_RADIUS, VELOCITY_AD, AGILITY_AD_VEL, LG_GRAVITY, BURROW_DEPTH, BURROW_SPEED_AD, BURROW_ANG_AD,
    ANGULAR_AD, SHIELD_FLIGHT, IDENTIFY_RANGE,
    MGUN_AD_RATE, MGUN_AD_LIFE, MGUN_AD_VEL,
    RFIRE_AD_RATE, RFIRE_AD_VEL, RFIRE_AD_LIFE,
    LASER_AD_VEL, LASER_AD_RATE, LASER_AD_LIFE,
    UPDATE_RATE_HZ, SERVER_UPDATE_RATE_HZ, AI_RATE_HZ,
    SHOOT_INTERVAL_RANDOM_MAX, RELOAD_TIME_DEFAULT, RESPAWN_DELAY, EXPLODE_TIME, ROUND_END_LINGER,
    STUCK_WINDOW, STUCK_MIN_DIST, WORLD_HALF_DEFAULT,
    SMALL_SCALE, SMALL_MAX_DIST, SMALL_MAX_VEL, SMALL_MAX_ANGV,
    DODGE_DIST, TANK_HEIGHT, WALL_HEIGHT_DEFAULT, SR_RADIUS_MULT,
    KILL_REASON_SHOT, KILL_REASON_RUNOVER, KILL_REASON_GENOCIDED,
    OBESITY_FACTOR, AHEAD_HALF_ANGLE,
    SHOT_RADIUS, HIT_RADIUS, DODGE_REACT_DELAY, IB_REACT_MULTIPLIER,
    RADAR_RANGE, TARGET_FOV, WIDE_ANGLE_ANG,
    GOOD_FLAGS_DEFAULT, BAD_FLAGS_DEFAULT,
    FLAG_NAME_TO_ABBR,
    TANK_WIDTH, _TINY_FACTOR, THIEF_TINY_FACTOR, THIEF_VEL_AD, _NARROW_HW,
    THIEF_AD_SHOT_VEL, THIEF_AD_LIFE,
)

logger = logging.getLogger("bzbot")


# ── Daten-Klassen ─────────────────────────────────────────────────────────

@dataclass
class Shot:
    """Zustand eines aktiven Schusses auf dem Spielfeld."""

    shooter_id: int
    shot_id:    int
    pos:        List[float]
    vel:        List[float]
    fire_time:  float
    lifetime:   float
    team:       int
    is_sw:      bool = False
    is_gm:      bool = False
    is_laser:   bool = False
    is_thief:   bool = False
    flag_abbr:  bytes = b"\x00\x00"
    gm_target_pid: Optional[int] = None
    last_gm_update: float = 0.0

    def is_expired(self, now: float) -> bool:
        return now - self.fire_time >= self.lifetime

    def position_at(self, t: float) -> Tuple[float, float, float]:
        dt = t - self.fire_time
        return (self.pos[0] + self.vel[0] * dt,
                self.pos[1] + self.vel[1] * dt,
                self.pos[2] + self.vel[2] * dt)

    def time_to_closest(self, px: float, py: float) -> float:
        """Zeit bis zur nächsten Annäherung an (px, py), ausgehend von self.pos."""
        rvx = self.vel[0]; rvy = self.vel[1]
        rx = self.pos[0] - px; ry = self.pos[1] - py
        denom = rvx * rvx + rvy * rvy
        # Schuss steht (quasi) still → kommt nie näher
        if denom < 1e-6:
            return float("inf")
        # Zeitpunkt des nächsten Annäherns: negativer Anteil des Schuss-Richtungsvektors
        # in Richtung des Abstands-Vektors (negativ → Schuss fährt auf Ziel zu)
        return max(0.0, -(rx * rvx + ry * rvy) / denom)

    def closest_approach_dist(self, px: float, py: float) -> float:
        """Minimaler Abstand des Schusses zu (px, py) über seine Lebenszeit."""
        t = self.time_to_closest(px, py)
        if t == float("inf"):
            return math.hypot(self.pos[0] - px, self.pos[1] - py)
        ex = self.pos[0] + self.vel[0] * t
        ey = self.pos[1] + self.vel[1] * t
        return math.hypot(ex - px, ey - py)


@dataclass
class PlayerInfo:
    """Zustand eines anderen Spielers."""
    callsign:   str
    team:       int
    is_human:   bool
    pos:        List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    vel:        List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    azimuth:    float = 0.0
    alive:      bool  = False
    flag:       str   = ""
    is_airborne: bool  = False  # aus PS_FALLING: True bei Sprung UND Fall (nicht nur Springen)
    last_seen:  float = 0.0
    last_order: int   = -1
    radar_blind_until: float = 0.0   # Radar-Aufmerksamkeit: bis dahin keine Radar-Updates (Cooldown)
    is_phantom_zoned: bool = False
    paused:     bool  = False  # aus MsgPause: pausiert = unverwundbar, nicht beschießen
    last_teleport: Optional[Tuple[float, int, int]] = None  # (zeit, from_face, to_face), letzter Teleport


@dataclass
class FlagInfo:
    """Eine Flag auf dem Spielfeld."""
    flag_id: int
    abbr:    str
    status:  int
    pos:     List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


# ── Bot ───────────────────────────────────────────────────────────────────

# Managed-Modus: Intervall des Status-Heartbeats an den Bot-Manager (Sekunden).
STATUS_HEARTBEAT_S = 2.0


class BZBot(BZBotAI):
    """BZFlag-2.4-Bot: verbindet sich per TCP/UDP und spielt autonom."""

    def __init__(self, host, port=DEFAULT_PORT, callsign="Bot",
                 team=TEAM_AUTOMATIC, motto="", token="",
                 world_half=WORLD_HALF_DEFAULT,
                 bot_name_prefix: str = "Bot_",
                 bot_callsigns: Optional[List[str]] = None,
                 managed: bool = False,
                 good_flags: Optional[List[str]] = None,
                 bad_flags:  Optional[List[str]] = None,
                 limited_flags: Optional[List[str]] = None,
                 debug_no_shoot: bool = False,
                 debug_target_flag: str = "",
                 debug_no_jump: bool = False,
                 debug_log_path: bool = False,
                 debug_log_shot: bool = False,
                 debug_log_dodge: bool = False,
                 debug_log_flag: bool = False,
                 debug_log_tele: bool = False):
        self.host             = host
        self.port             = port
        self.callsign         = callsign
        self.team             = team
        self.motto            = motto
        self.token            = token
        self.world_half       = world_half
        self.bot_name_prefix  = bot_name_prefix
        self.bot_callsigns    = set(bot_callsigns) if bot_callsigns else set()
        # Managed-Modus: vom Bot-Manager gestartet → IPC über stdin/stdout aktiv.
        # Standalone (False) bleibt komplett unverändert: kein stdin-Reader, keine Statusausgabe.
        self.managed          = managed
        self._last_status_emit = 0.0
        # True, wenn der letzte Join vom Server abgelehnt wurde (Kapazität/Callsign):
        # erlaubt main(), einen erwarteten Reject (BOT_EXIT_REJECTED) von einem Crash zu trennen.
        self._join_rejected   = False

        self.client = BZFlagClient(host, port)
        self.client._on_game_settings  = self._on_game_settings
        self.client._world_half_cache  = world_half
        self.client.on_world_ready     = self._on_world_ready

        # Eigener Zustand
        self.player_id: Optional[int] = None
        self.pos       = [0.0, 0.0, 0.0]
        self.vel       = [0.0, 0.0, 0.0]
        self.azimuth   = 0.0
        self.ang_vel   = 0.0
        self.alive     = False
        self.death_time: Optional[float] = None
        self._order    = 0       # GLOBAL, niemals zurücksetzen!

        # Shot-ID: low byte = slot (0..maxShots-1), high byte = generation
        self._shot_slot = 0
        self._shot_gen  = 0
        self._max_shots = MAX_SHOTS_DEFAULT
        self._next_shoot = 0.0
        self._slot_reload_at: list[float] = []
        self._spawn_sent_at: Optional[float] = None
        self._reload_time = RELOAD_TIME_DEFAULT

        # Physik-Konstanten (Defaults; werden via MsgSetVar / MsgGameSettings überschrieben)
        self._shot_speed     = SHOT_SPEED_DEFAULT
        self._shot_range     = SHOT_RANGE
        self._shot_lifetime  = SHOT_LIFETIME
        self._tank_speed     = TANK_SPEED
        self._tank_turn_rate = TANK_TURN_RATE
        self._jump_velocity  = JUMP_VELOCITY
        self._gravity        = GRAVITY
        self._linear_acceleration  = 0.0   # aus MsgGameSettings; Nutzung in Phase 3
        self._angular_acceleration = 0.0   # aus MsgGameSettings; Nutzung in Phase 3
        self._server_ricochet      = False  # aus MsgGameSettings gameOptions Bit 0x0020
        self._server_jumping       = False  # aus MsgGameSettings gameOptions Bit 0x0008 (JumpingGameStyle)

        # Shockwave (via _shockInRadius / _shockOutRadius / _shockAdLife)
        self._shock_in_radius  = SHOCK_IN_RADIUS
        self._shock_out_radius = SHOCK_OUT_RADIUS
        self._shock_ad_life    = SHOCK_AD_LIFE
        self._sw_expand_speed  = SW_EXPAND_SPEED

        # GM (via _gmTurnAngle / _gmActivationTime / _gmAdLife / _lockOnAngle)
        self._gm_turn_angle      = GM_TURN_RATE
        self._gm_activation_time = GM_ACTIVATION_TIME
        self._gm_ad_life         = GM_AD_LIFE
        self._lock_on_angle      = GM_LOCK_ON_ANGLE
        # Geradeaus-/Homing-Grenze: ab dieser Distanz wird die GM zielsuchend. Nachgeführt aus
        # _gmActivationTime × _shotSpeed (s. _recompute_gm_min_range), folgt geänderten Server-Vars.
        self._gm_min_range       = 0.0
        self._recompute_gm_min_range()

        # Flaggen-Physik (via MsgSetVar)
        self._obese_factor    = OBESITY_FACTOR
        self._flag_radius     = FLAG_RADIUS
        self._velocity_ad     = VELOCITY_AD
        self._agility_ad_vel  = AGILITY_AD_VEL
        self._lg_gravity      = LG_GRAVITY
        self._burrow_depth    = BURROW_DEPTH
        self._burrow_speed_ad = BURROW_SPEED_AD
        self._burrow_ang_ad   = BURROW_ANG_AD
        self._muzzle_height      = MUZZLE_HEIGHT       # via MsgSetVar _muzzleHeight
        self._muzzle_front       = MUZZLE_FRONT        # via MsgSetVar _muzzleFront
        self._tank_length        = TANK_LENGTH         # via MsgSetVar _tankLength
        self._tank_width         = TANK_WIDTH          # via MsgSetVar _tankWidth
        self._tank_height        = TANK_HEIGHT         # via MsgSetVar _tankHeight
        self._wall_height        = WALL_HEIGHT_DEFAULT # via MsgSetVar _wallHeight / _tankHeight
        self._shot_radius        = SHOT_RADIUS         # via MsgSetVar _shotRadius
        self._tiny_factor        = _TINY_FACTOR        # via MsgSetVar _tinyFactor
        self._thief_tiny_factor  = THIEF_TINY_FACTOR   # via MsgSetVar _thiefTinyFactor
        self._thief_vel_ad       = THIEF_VEL_AD        # via MsgSetVar _thiefVelAd
        self._thief_ad_shot_vel  = THIEF_AD_SHOT_VEL   # via MsgSetVar _thiefAdShotVel
        self._thief_ad_life      = THIEF_AD_LIFE        # via MsgSetVar _thiefAdLife
        self._narrow_hw          = _NARROW_HW          # via MsgSetVar _narrowHW
        self._angular_ad      = ANGULAR_AD
        self._shield_flight   = SHIELD_FLIGHT
        self._identify_range  = IDENTIFY_RANGE
        self._wide_angle_ang  = WIDE_ANGLE_ANG  # via MsgSetVar _wideAngleAng (WA-Sicht-FoV)
        self._wings_jump_count = 1   # _wingsJumpCount via MsgSetVar
        self._wings_jumps_used = 0   # Luftsprünge seit letzter Landung
        # _wingsGravity / _wingsJumpVelocity via MsgSetVar. None = kein Server-Override →
        # _effective_*() fallen faithful auf _gravity / _jump_velocity zurück (BZDB-Defaults
        # sind die Ausdrücke "_gravity" bzw. "_jumpVelocity").
        self._wings_gravity = None
        self._wings_jump_velocity = None
        self._dropped_neutrals: collections.deque = collections.deque(maxlen=5)

        # Schuss-Typ-Multiplikatoren (via MsgSetVar)
        self._mgun_ad_rate  = MGUN_AD_RATE
        self._mgun_ad_life  = MGUN_AD_LIFE
        self._mgun_ad_vel   = MGUN_AD_VEL
        self._rfire_ad_rate = RFIRE_AD_RATE
        self._rfire_ad_vel  = RFIRE_AD_VEL
        self._rfire_ad_life = RFIRE_AD_LIFE
        self._laser_ad_vel  = LASER_AD_VEL
        self._laser_ad_rate = LASER_AD_RATE
        self._laser_ad_life = LASER_AD_LIFE

        # Welt
        self.players:  Dict[int, PlayerInfo] = {}
        self.human_count    = 0
        self.observer_count = 0
        self._world_map = None   # Optional[WorldMap] — gesetzt nach Welt-Download
        self._link_map = {}      # face-Index → Ziel-face-Index (Teleporter), aus _world_map.links
        self._tele_solid_boxes: list = []  # Teleporter-Posts+Crossbar als BoxObstacle (Kollision)
        self._teleporting_until = 0.0      # P3-NAV-02: PS_TELEPORTING-Ende + Re-Trigger-Sperre
        self._nav_graph = None   # Optional[NavGraph] — aus _world_map gebaut
        self._nav_path: list = []      # [(wx, wy, layer_z), ...] — aktuelle Pfad-Queue
        self._nav_goal = None          # Optional[Tuple[float, float]] — aktuelles Ziel
        self._nav_jump_return_state = None   # AIState nach NAV_JUMP-Landung
        self._nav_jump_target_z: float = 0.0  # Erwartet-Landeebene für NAV_JUMP-Fehlschlagerkennung
        self._nav_jump_cooldowns: dict = {}  # (round_x, round_y, z) → expiry_time (NAV-14)
        # NAV_TELE: direkter Endanflug in die Teleporter-Mitte (P3-NAV-02)
        self._nav_tele_cooldowns: dict = {}  # (round_cx, round_cy) → expiry_time
        self._nav_tele_center = None         # Optional[Tuple[float, float]] — aktive Tor-Mitte
        self._nav_tele_start: float = 0.0    # Engage-Zeit (monotonic) für Timeout
        self._nav_tele_return_state = None   # AIState nach Querung/Abbruch
        # COMBAT-Eskalation bei per Sprung unerreichbarem (zu hohem) Gegner ohne A*-Pfad
        self._unreach_target: Optional[int] = None   # pid der laufenden Episode (None = keine)
        self._unreach_phase: int = 0                 # 0=Re-Target 1=Direkt 2/3=Reposition
        self._unreach_until: float = 0.0             # Phasen-Deadline (time.monotonic)
        self._unreach_replan_at: float = 0.0         # Drossel für Hintergrund-Replan
        self._combat_avoid: dict = {}                # pid → expiry_time (gemiedene Ziele)
        self._recent_flag_targets: collections.deque = collections.deque(maxlen=10)  # (round_x, round_y)
        self._wp_start_time: Optional[float] = None  # Zeit seit aktuellem WP-Ziel
        self._wp_fail_count: int = 0                 # aufeinanderfolgende WP-Timeouts
        self._wp_timeout: float = 3.0                # per-WP Timeout (berechnet in bzbot_ai.py)

        # Shot-Tracking
        self._shots:          Dict[Tuple[int, int], Shot] = {}
        self._ricochet_paths: Dict[Tuple[int, int], List] = {}
        self._shots_lock = threading.Lock()

        # P4-INF-01: Asynchrone Pfadplanung (Zweit-Thread). Der gecachte NavGraph ist nach der
        # Reentranz-Umstellung parallel beplanbar; der Worker bekommt nur Plain-Value-Snapshots.
        self._async_plan_lock   = threading.Lock()
        self._async_plan_thread: Optional[threading.Thread] = None
        self._async_plan_result: Optional[tuple] = None   # (gen,gx,gy,gz,cap,sx,sy,path)
        self._async_plan_goal:   Optional[Tuple[float, float]] = None  # in-flight-Ziel (Cancel-Vergleich)
        self._async_cancel       = threading.Event()      # kooperatives Cancel der laufenden Vollsuche
        self._plan_gen           = 0                      # monoton; invalidiert veraltete Ergebnisse

        # KI — Grundzustand
        self.target_pos:    Optional[Tuple[float, float]] = None
        self.target_player: Optional[int] = None
        self._target_paused_since: Optional[float] = None  # seit wann das aktuelle Ziel pausiert (Warte-Timer)
        self._dodging        = False
        self._dodge_until    = 0.0
        self._dodge_dir      = 0.0
        self._threat_detected_at: float = 0.0
        self._last_threat_id:      Optional[Tuple[int, int]] = None
        self._evade_cleared_shots: dict                       = {}
        self._jumping        = False
        self._jump_ang_vel   = 0.0
        self._pre_fall_state: "AIState" = AIState.IDLE
        self._bounce_next:   float = 0.0
        self._z_attack_mode: bool = False
        self._z_attack_fire_z: float = 0.0
        self._z_attack_retry_after:  float = 0.0
        self._tact_jump_retry_after: float = 0.0

        # Debug-Flags (undokumentiert, nur für manuelle Tests)
        self._debug_no_shoot  = debug_no_shoot
        self._debug_no_jump   = debug_no_jump
        self._debug_log_path  = debug_log_path
        self._debug_log_shot  = debug_log_shot
        self._debug_log_dodge = debug_log_dodge
        self._debug_log_flag  = debug_log_flag
        self._debug_log_tele  = debug_log_tele

        # Flag-Strategie
        self.own_flag: str = ""
        self.good_flags = set(good_flags) if good_flags is not None else set(GOOD_FLAGS_DEFAULT)
        self.bad_flags  = set(bad_flags)  if bad_flags  is not None else set(BAD_FLAGS_DEFAULT)
        self._limited_flags: set[str] = set(limited_flags) if limited_flags else set()
        self._shots_remaining: int = -1
        self._last_notschuss_threat: tuple | None = None
        if debug_target_flag:
            self.good_flags = {debug_target_flag}
            self.bad_flags.discard(debug_target_flag)
            if self._debug_log_flag:
                logger.debug("[%s] Flagge: Ziel-Flag: %s", self.callsign, debug_target_flag)
        self._own_flag_since: float = 0.0
        self._last_drop_attempt: float = 0.0
        self._drop_bad_flag_delay: float = 1.0

        # Flag-Tracking
        self.flags: Dict[int, FlagInfo] = {}
        self._last_grab_attempt: float = 0.0

        # GM-Tracking
        self._active_gm: Optional[dict] = None
        self._gm_need_update: bool = False
        self._gm_send_at:    Optional[float] = None
        self._gm_resend_at:  Optional[float] = None

        # Server-Zeitbasis
        self._server_time_offset: float = 0.0

        # Bewegungs-Flags
        self._move_reverse: bool = False

        # Taktischer Übersprung
        self._jump_pending: bool = False
        self._tactical_jump_until: float = 0.0
        self._last_jump_at: float = 0.0
        self._escape_jump_ang_vel: Optional[float] = None
        self._dodge_forward: bool = False
        self._dodge_reverse: bool = False

        # Stuck-Erkennung
        self._last_pos_check_time = 0.0
        self._last_pos_check      = [0.0, 0.0]

        # State Machine
        self._ai_state: AIState = AIState.IDLE
        self._landing_shot_until: float = 0.0
        self._landing_aim_pos: Optional[Tuple[float, float]] = None
        self._landing_hit_z: float = 0.0   # Interzeptionshöhe für LANDING_SHOT (P4-TAC-07)
        self._rico_aim_cache: Optional[Tuple[float, int, Optional[Tuple[float, bool]]]] = None
        self._indirect_hold_until: Optional[float] = None   # Zeit-Cap fürs Indirekt-Schuss-Halten (C)

        self._next_server_update: float = 0.0
        self._server_update_interval: float = 1.0 / SERVER_UPDATE_RATE_HZ
        self._tick_count: int = 0

        self._running          = False
        self._stop_event       = threading.Event()
        self._reconnect_needed = False
        self._round_over_until: float | None = None  # Endzeit der Endstand-Linger-Phase
        self._exploding_until: float = 0.0   # Endzeit der Explosions-Animation (PS_EXPLODING senden)
        self._game_over: bool = False        # Server-Rundenende-Zustand (aus MsgTimeUpdate/MsgScoreOver)
        self._round_over: bool = False       # echtes Rundenende NACH dem Spielen (Managed: Exit→Manager-Rejoin)
        self._has_spawned: bool = False      # hat der Bot diese Session schon gespielt? (Reconnect-Gate)
        self.on_player_count_changed = None

        # Handler
        self.client.add_handler(MsgAddPlayer,       self._on_add_player)
        self.client.add_handler(MsgRemovePlayer,    self._on_remove_player)
        self.client.add_handler(MsgAlive,           self._on_alive)
        self.client.add_handler(MsgKilled,          self._on_killed)
        self.client.add_handler(MsgShotBegin,       self._on_shot_begin)
        self.client.add_handler(MsgShotEnd,         self._on_shot_end)
        self.client.add_handler(MsgTeleport,        self._on_teleport)
        self.client.add_handler(MsgGMUpdate,        self._on_gm_update)
        self.client.add_handler(MsgPlayerUpdate,    self._on_player_update_full)
        self.client.add_handler(MsgPlayerUpdateSmall, self._on_player_update_small)
        self.client.add_handler(MsgPause,           self._on_pause)
        self.client.add_handler(MsgSetVar,          self._on_set_var)
        self.client.add_handler(MsgSuperKill,             self._on_disconnect)
        self.client.add_handler(MsgScoreOver,             self._on_score_over)
        self.client.add_handler(MSG_INTERNAL_DISCONNECT, self._on_disconnect)
        self.client.add_handler(MsgGrabFlag,      self._on_grab_flag)
        self.client.add_handler(MsgDropFlag,      self._on_drop_flag)
        self.client.add_handler(MsgCaptureFlag,   self._on_capture_flag)
        self.client.add_handler(MsgTransferFlag,  self._on_transfer_flag)
        self.client.add_handler(MsgFlagUpdate,    self._on_flag_update)
        self.client.add_handler(MsgNearFlag,      self._on_near_flag)
        self.client.add_handler(MsgGameTime,      self._on_game_time)
        self.client.add_handler(MsgTimeUpdate,    self._on_time_update)
        for code in (MsgPlayerInfo, MsgTeamUpdate,
                     MsgScore,
                     MsgHandicap, MsgLagState):
            self.client.add_handler(code, self._ignored)
        self.client.add_handler(MsgMessage, self._on_message)

    # ── Lebenszyklus ──────────────────────────────────────────────────────

    def start(self) -> bool:
        """Verbindet zum Server, startet UDP-Handshake und blockiert in der Spielschleife."""
        logger.info("[%s] Verbinde mit %s:%d …", self.callsign, self.host, self.port)
        if not self.client.connect():
            return False
        self.player_id = self.client.player_id
        if not self.client.join_game(callsign=self.callsign,
                                     player_type=PLAYER_TYPE_TANK,
                                     team=self.team, motto=self.motto,
                                     token=self.token):
            # Server-Ablehnung (voll/Callsign belegt) vor disconnect() merken, damit
            # main() einen erwarteten Reject von einem Verbindungsfehler unterscheiden kann.
            self._join_rejected = self.client.last_reject_reason >= 0
            self.client.disconnect(); return False
        self.player_id = self.client.player_id
        logger.info("[%s] Player-ID nach Accept: %d", self.callsign, self.player_id)
        if self.client.initiate_udp():
            logger.info("[%s] UDP-Handshake initiiert", self.callsign)
        else:
            logger.warning("[%s] UDP nicht verfügbar – nur TCP", self.callsign)
        self._running = True
        self._stop_event.clear()
        self._run_game_loop()
        return True

    def stop(self) -> None:
        """Stoppt die Spielschleife und trennt die Serververbindung."""
        logger.info("[%s] Stoppe …", self.callsign)
        self._running = False
        self._stop_event.set()
        self._async_cancel.set()        # laufende Hintergrund-Pfadplanung kooperativ abbrechen
        self.client.disconnect()

    # ── Spielschleife ─────────────────────────────────────────────────────

    def _run_game_loop(self) -> None:
        """60-Hz-Spielschleife (Physik); KI 10 Hz, Server-Update 30 Hz: Hit-Detection, Bewegung, Schuss, PlayerUpdate."""
        dt        = 1.0 / UPDATE_RATE_HZ
        last_tick = time.monotonic()
        self._stop_event.wait(timeout=0.5)
        if not self._game_over:        # nicht in eine laufende Rundenende-Phase spawnen
            self._spawn()
        while self._running and not self._stop_event.is_set():
            now  = time.monotonic()
            dt_r = now - last_tick
            last_tick = now
            if not self.client.connected:
                logger.warning("[%s] Verbindung verloren", self.callsign)
                break
            # UDP-Handshake notfalls wiederholen, bis udp_active (sonst schießt der Bot nie —
            # _can_shoot gatet auf udp_active, weil TCP-Shots gekickt werden). Selbst-gedrosselt.
            if not self.client.udp_active:
                self.client.retry_udp_link()
            self._tick_count += 1
            ai_tick = (self._tick_count % (UPDATE_RATE_HZ // AI_RATE_HZ) == 0)
            if self.alive:
                self._resolve_incoming_shots(now, dt_r)
            if self.alive:
                self._check_steamroller(now)
            if self.alive:
                self._update_movement(dt_r, now, ai_tick=ai_tick)
                self._maybe_shoot(now)
                if self._active_gm is not None:
                    if self._gm_need_update and (self._gm_send_at is None or now >= self._gm_send_at):
                        self._send_gm_update(now)
                        self._gm_need_update = False
                        self._gm_send_at = None
                if self.own_flag and self.own_flag not in self.good_flags:
                    held = now - self._own_flag_since
                    required = self._drop_bad_flag_delay if self.own_flag in self.bad_flags else 0.0
                    if held >= required and now - self._last_drop_attempt > 2.0:
                        self._try_drop_flag()
                elif self.own_flag == "G" and not self._genocide_multikill_possible():
                    if now - self._last_drop_attempt > 2.0:
                        self._try_drop_flag()
            elif now < self._exploding_until:
                self._tick_explosion(dt_r)   # Explosions-Bogen (tot); gatet implizit den Respawn
            elif not self._game_over and self._round_over_until is None and self.death_time is not None:
                if now - self.death_time >= RESPAWN_DELAY:
                    self.death_time = None
                    self._spawn()
            elif (not self._game_over and self._round_over_until is None
                    and self.death_time is None and self._spawn_sent_at is not None):
                if now - self._spawn_sent_at > 5.0:
                    logger.warning("[%s] Keine Spawn-Antwort – wiederhole MsgAlive", self.callsign)
                    self._spawn()
            # Server-Update bei Kadenz – lebend: Position; Explosion: PS_EXPLODING (kein [nr]); sonst still
            if now >= self._next_server_update:
                self._send_update()
                self._next_server_update += self._server_update_interval
            # Rundenende: nach der Endstand-Linger-Phase die Schleife verlassen → Reconnect
            if self._round_over_until is not None and time.monotonic() >= self._round_over_until:
                break
            # Managed-Modus: regelmäßiger Status-Heartbeat an den Manager (auch tot/respawnend)
            if self.managed and now - self._last_status_emit >= STATUS_HEARTBEAT_S:
                self._emit_status()
            self._cleanup_shots(now)
            self._stop_event.wait(timeout=max(0.0, dt - (time.monotonic() - now)))

    # ── Treffer-Erkennung ─────────────────────────────────────────────────

    def _resolve_incoming_shots(self, now: float, dt: float) -> None:
        """Prüft alle aktiven Schüsse auf Treffer; behandelt auch SH-Absorption
        (überlebt) und TH-Flaggendiebstahl (kein Kill). Siehe DEVELOPER.md §7."""
        tank_cx = self.pos[0]
        tank_cy = self.pos[1]
        tank_cz = self.pos[2] + self._tank_height / 2
        eff_r   = self._effective_hit_radius()
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
                                    gm_tz = _p.pos[2] + TANK_HEIGHT / 2
                        if gm_tx is not None:
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
                        hit = _segment_point_dist3d(prev_x, prev_y, prev_z,
                                                    shot.pos[0], shot.pos[1], shot.pos[2],
                                                    tank_cx, tank_cy, tank_cz) < eff_r
                    else:
                        prev_t = max(shot.fire_time, now - min(dt, 0.2))
                        if   self.own_flag == "O":  _sc = self._obese_factor
                        elif self.own_flag == "T":  _sc = self._tiny_factor
                        elif self.own_flag == "TH": _sc = self._thief_tiny_factor
                        else:                       _sc = 1.0
                        _half_w   = (self._narrow_hw if self.own_flag == "N"
                                     else self._tank_width / 2 * _sc) + self._shot_radius
                        _half_len = self._tank_length / 2 * _sc + self._shot_radius
                        _half_h   = self._tank_height / 2 * _sc + self._shot_radius
                        segs = self._ricochet_paths.get(
                            (shot.shooter_id, shot.shot_id))
                        if segs:
                            # Abpraller: gecachte Segmente gegen Tank-OBB prüfen
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
                            # Skip wenn Schuss sich vom Bot wegbewegt (past closest approach).
                            # Schuss bleibt in _shots — könnte bei Ricochet zurückkommen.
                            _rel_x = bx - tank_cx; _rel_y = by - tank_cy
                            if shot.vel[0] * _rel_x + shot.vel[1] * _rel_y > 0:
                                continue
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
        for pid, info in self.players.items():
            if not info.alive: continue
            if info.flag != "SR" and self.own_flag != "BU": continue
            if now - info.last_seen > 1.0: continue
            dx = info.pos[0] - self.pos[0]
            dy = info.pos[1] - self.pos[1]
            dz = info.pos[2] - self.pos[2]
            dist = math.sqrt(math.hypot(dx, dy)**2 + (dz * 2.0)**2)
            if dist < TANK_RADIUS * (1.0 + SR_RADIUS_MULT):
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

    # ── Netzwerk senden ───────────────────────────────────────────────────

    def _send_update(self) -> None:
        """Sendet MsgPlayerUpdate (Position + Velocity + Status) via UDP — spiegelt den echten Client.

        Explosion (`now < _exploding_until`): PS_EXPLODING gesetzt, Alive gelöscht — wie nach
        `setExplode`. Das Update kommt durchs Relay-Gate (bzfs.cxx:5493) und der Observer zeigt die
        Explosions-Animation; Alive gelöscht → `notResponding=false` (kein [nr]). Lebend: normaler
        Status. Tot ohne laufende Explosion: nichts senden (der echte Client schweigt im Tod)."""
        if self.player_id is None:
            return
        # PS_FALLING = echter Luftzustand: Sprung, Abstieg, freier Fall (über Boden) und Explosions-Bogen
        falling = self._jumping or self.pos[2] > self._get_floor_z() + 1e-6
        if time.monotonic() < self._exploding_until:
            status = PS_EXPLODING | (PS_FALLING if falling else 0)
            vel, ang_vel = tuple(self.vel), 0.0
        elif self.alive:
            status = PS_ALIVE | (PS_FALLING if falling else 0) | (PS_FLAG_ACTIVE if self.own_flag else 0)
            if time.monotonic() < self._teleporting_until:
                status |= PS_TELEPORTING   # P3-NAV-02: laufender Teleport
            vel, ang_vel = tuple(self.vel), self.ang_vel
        else:
            return   # tot und keine laufende Explosion → kein Update (wie der echte Client)
        self._order += 1
        payload = build_player_update(
            player_id=self.player_id, order=self._order, status=status,
            pos=tuple(self.pos), vel=vel,
            azimuth=self.azimuth, ang_vel=ang_vel,
            timestamp=time.monotonic() + self._server_time_offset)
        self.client.send(MsgPlayerUpdate, payload)

    def _start_explosion(self, now: float) -> None:
        """Startet die Explosions-Animation wie der echte Client (LocalPlayer.cxx explodeTank):
        Aufwärts-Velocity (gedeckelt auf zMax≈49u), Horizontal-Momentum bleibt erhalten. Während
        `_exploding_until` sendet `_send_update` PS_EXPLODING; `_tick_explosion` integriert den Bogen."""
        g = self._gravity
        if g < 0:
            vz = min(-0.5 * g * EXPLODE_TIME, math.sqrt(-2.0 * 49.0 * g))
        else:
            vz = self.vel[2]
        self.vel = [self.vel[0], self.vel[1], vz]
        self._exploding_until = now + EXPLODE_TIME

    def _send_teleport(self, source: int, target: int) -> None:
        """Sendet MsgTeleport (eigener Teleport: playerIndex, from_face, to_face). Port von
        ServerLink::sendTeleport, gespiegeltes Layout zu _on_teleport (P3-NAV-02)."""
        if self.player_id is None:
            return
        self.client.send(MsgTeleport,
                         struct.pack(">BHH", self.player_id, source, target))

    def _spawn(self) -> None:
        """Sendet MsgAlive (Spawn-Anfrage) an den Server."""
        if self.player_id is None:
            logger.debug("[%s] Spawn: player_id unbekannt", self.callsign)
            return
        # Frischer Spawn → eine ggf. noch laufende Hintergrund-Pfadplanung der alten Position
        # abbrechen und ihr Ergebnis invalidieren (P4-INF-01).
        self._async_cancel.set()
        self._plan_gen += 1
        logger.info("[%s] Sende MsgAlive (Spawn-Anfrage)", self.callsign)
        self._spawn_sent_at = time.monotonic()
        self.client.send(MsgAlive, b"")

    # ── Message-Handler ───────────────────────────────────────────────────

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
        if world_map is None:
            logger.warning("[%s] Karten-Wissen nicht verfügbar (Parse-Fehler)", self.callsign)
            return
        self._link_map = build_link_map(world_map.links)
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
        max_jump_h = self._jump_velocity ** 2 / (2.0 * abs(self._gravity))
        self._nav_graph = get_nav_graph(world_map, max_jump_h=max_jump_h)
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

    def _is_bot_callsign(self, callsign: str) -> bool:
        """True, wenn der Callsign zu einem Bot gehört: eigener Name, in der vom
        Manager gepushten Liste, oder beginnt mit dem Bot-Prefix (kombinierte Erkennung)."""
        return (
            callsign == self.callsign
            or (bool(self.bot_callsigns) and callsign in self.bot_callsigns)
            or (bool(self.bot_name_prefix) and callsign.startswith(self.bot_name_prefix))
        )

    def update_bot_callsigns(self, names) -> None:
        """Übernimmt die vom Manager gepushte Liste aktiver Bot-Callsigns und stuft
        bereits bekannte Spieler bei Bedarf von Mensch auf Bot herab (nie umgekehrt),
        damit Peers nicht als Menschen wahrgenommen/gezählt werden."""
        self.bot_callsigns = set(names) if names else set()
        for info in list(self.players.values()):
            if info.is_human and self._is_bot_callsign(info.callsign):
                info.is_human = False
        self.human_count = sum(1 for p in self.players.values() if p.is_human)
        self._notify_count()

    def _emit_status(self) -> None:
        """Managed-Modus: sendet den aktuellen Spielerstand als getaggte stdout-Zeile
        an den Bot-Manager. Standalone (managed=False) ein No-Op."""
        if not self.managed:
            return
        try:
            humans = [p.callsign for p in list(self.players.values()) if p.is_human]
            msg = {
                "type":      "status",
                "humans":    len(humans),
                "observers": self.observer_count,
                "players":   humans,
                "game_over": bool(self._game_over or self._round_over_until is not None),
            }
            sys.stdout.write(f"{MGR_STATUS_PREFIX} {json.dumps(msg)}\n")
            sys.stdout.flush()
            self._last_status_emit = time.monotonic()
        except Exception:
            pass

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

    def _try_drop_flag(self) -> None:
        """Sendet MsgDropFlag um eigene Flag abzulegen."""
        if self.player_id is None or not self.own_flag: return
        self._last_drop_attempt = time.monotonic()
        self._last_grab_attempt = time.monotonic()  # 0.5s Grab-Cooldown nach Drop
        self.client.send(MsgDropFlag, struct.pack(">fff", *self.pos))
        if getattr(self, '_debug_log_flag', False):
            logger.debug("[%s] Flagge: MsgDropFlag gesendet (Flag=%r)", self.callsign, self.own_flag)

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
        with self._shots_lock:
            self._shots[(shooter, shot_id)] = shot
            _is_pz = bool(self.players.get(shooter, None) and self.players[shooter].is_phantom_zoned)
            # Teleporter transportieren jeden Schuss (auch Nicht-Ricochet) → Pfad auch dann
            # vorberechnen, wenn die Karte Teleporter hat. Sonst landet ein nicht-ricochet
            # Laser/Thief im geraden-Linien-else-Zweig und „sieht" den Teleporter nie.
            # wand-durchdringende Schüsse (PhantomZone, Super Bullet) bleiben außen
            # vor — die wandbewusste Simulation würde ihren Pfad fälschlich an Wänden stoppen;
            # ihr alter Pfad (kein Cache → gerade Linie) ist für sie korrekt.
            _phases_walls = _is_pz or shot.flag_abbr == b"SB"
            _tele_route = (self._world_map is not None
                           and bool(self._world_map.teleporters) and not _phases_walls)
            if (self._world_map is not None
                    and (_tele_route
                         or _can_ricochet_shot(shot.flag_abbr, shot.is_gm, shot.is_sw,
                                               self._server_ricochet, is_phantom_zoned=_is_pz))):
                _tlog = [] if self._debug_log_tele else None
                self._ricochet_paths[(shooter, shot_id)] = simulate_shot_path(
                    (px, py, pz), (vx, vy, vz), shot.fire_time, shot.lifetime,
                    shot.flag_abbr, self._world_map.boxes,
                    self.world_half, self._server_ricochet,
                    wall_height=self._wall_height,
                    teleporters=self._world_map.teleporters,
                    link_map=self._link_map,
                    tele_log=_tlog,
                )
                if self._debug_log_tele:
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
            segs = self._ricochet_paths.get((shooter, shot_id))
            if segs:
                # Abgeprallter Laser: Sofortcheck über alle Segmente (effektiv instant)
                if   self.own_flag == "O":  _sc = self._obese_factor
                elif self.own_flag == "T":  _sc = self._tiny_factor
                elif self.own_flag == "TH": _sc = self._thief_tiny_factor
                else:                       _sc = 1.0
                _half_w   = (self._narrow_hw if self.own_flag == "N"
                             else self._tank_width / 2 * _sc) + self._shot_radius
                _half_len = self._tank_length / 2 * _sc + self._shot_radius
                _half_h   = self._tank_height / 2 * _sc + self._shot_radius
                _tcx = self.pos[0]; _tcy = self.pos[1]
                _tcz = self.pos[2] + self._tank_height / 2
                for seg in segs:
                    if _segment_hits_obb_3d(seg.px, seg.py, seg.pz,
                                             seg.ex, seg.ey, seg.ez,
                                             _tcx, _tcy, _tcz, self.azimuth,
                                             _half_len, _half_w, _half_h):
                        logger.info("[%s] Laser-Treffer (Abpraller) von Spieler %d",
                                    self.callsign, shooter)
                        self._report_killed(shot)
                        break
            else:
                speed_xyz = math.sqrt(vx**2 + vy**2 + vz**2)
                if speed_xyz > 0:
                    dnx, dny, dnz = vx / speed_xyz, vy / speed_xyz, vz / speed_xyz
                    laser_range = speed_xyz * shot.lifetime
                    cx = self.pos[0]; cy = self.pos[1]; cz = self.pos[2] + TANK_HEIGHT / 2
                    dx, dy, dz = cx - px, cy - py, cz - pz
                    t_proj = dx * dnx + dy * dny + dz * dnz
                    if 0.0 <= t_proj <= laser_range:
                        perpx = dx - dnx * t_proj
                        perpy = dy - dny * t_proj
                        perpz = dz - dnz * t_proj
                        if math.sqrt(perpx**2 + perpy**2 + perpz**2) < HIT_RADIUS:
                            logger.info("[%s] Laser-Treffer von Spieler %d", self.callsign, shooter)
                            self._report_killed(shot)
        if shot.is_thief and self.alive and self.player_id is not None:
            segs = self._ricochet_paths.get((shooter, shot_id))
            if segs:
                # Abgeprallter Thief: Sofortcheck über alle Segmente (effektiv instant)
                if   self.own_flag == "O":  _sc = self._obese_factor
                elif self.own_flag == "T":  _sc = self._tiny_factor
                elif self.own_flag == "TH": _sc = self._thief_tiny_factor
                else:                       _sc = 1.0
                _half_w   = (self._narrow_hw if self.own_flag == "N"
                             else self._tank_width / 2 * _sc) + self._shot_radius
                _half_len = self._tank_length / 2 * _sc + self._shot_radius
                _half_h   = self._tank_height / 2 * _sc + self._shot_radius
                _tcx = self.pos[0]; _tcy = self.pos[1]
                _tcz = self.pos[2] + self._tank_height / 2
                for seg in segs:
                    if _segment_hits_obb_3d(seg.px, seg.py, seg.pz,
                                             seg.ex, seg.ey, seg.ez,
                                             _tcx, _tcy, _tcz, self.azimuth,
                                             _half_len, _half_w, _half_h):
                        if self.own_flag:
                            logger.info("[%s] Flagge '%s' durch TH-Abpraller von %d gestohlen",
                                        self.callsign, self.own_flag, shooter)
                            self.client.send(MsgTransferFlag,
                                             struct.pack(">BB", self.player_id, shooter))
                        break
            else:
                speed_xyz = math.sqrt(vx**2 + vy**2 + vz**2)
                if speed_xyz > 0:
                    dnx, dny, dnz = vx / speed_xyz, vy / speed_xyz, vz / speed_xyz
                    thief_range = speed_xyz * shot.lifetime
                    cx = self.pos[0]; cy = self.pos[1]; cz = self.pos[2] + TANK_HEIGHT / 2
                    dx, dy, dz = cx - px, cy - py, cz - pz
                    t_proj = dx * dnx + dy * dny + dz * dnz
                    if 0.0 <= t_proj <= thief_range:
                        perpx = dx - dnx * t_proj
                        perpy = dy - dny * t_proj
                        perpz = dz - dnz * t_proj
                        if math.sqrt(perpx**2 + perpy**2 + perpz**2) < HIT_RADIUS:
                            if self.own_flag:
                                logger.info("[%s] Flagge '%s' durch TH-Schuss von %d gestohlen — sende MsgTransferFlag",
                                            self.callsign, self.own_flag, shooter)
                                self.client.send(MsgTransferFlag,
                                                 struct.pack(">BB", self.player_id, shooter))
                            else:
                                if getattr(self, '_debug_log_shot', False):
                                    logger.debug("[%s] Schuss: TH-Treffer von %d – keine eigene Flagge vorhanden",
                                                 self.callsign, shooter)
        if shot.is_sw and self.alive and self.player_id is not None:
            tank_cz_sw = self.pos[2] + TANK_HEIGHT / 2
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
        tank_cz = self.pos[2] + TANK_HEIGHT / 2
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

    def _send_gm_update(self, now: float) -> None:
        """Sendet MsgGMUpdate um eigenen GM-Schuss auf aktuelles Ziel zu lenken."""
        gm = self._active_gm
        if gm is None or self.player_id is None: return
        dt = now - gm["fire_time"]
        if dt > self._shot_lifetime:
            self._active_gm = None
            self._gm_need_update = False
            self._gm_send_at = None
            self._gm_resend_at = None
            return
        px = gm["pos"][0] + gm["vel"][0] * dt
        py = gm["pos"][1] + gm["vel"][1] * dt
        pz = gm["pos"][2] + gm["vel"][2] * dt
        initial = gm.pop("initial_target", None)
        target_id = initial if initial is not None else (
            self.target_player if self.target_player is not None else 255
        )
        payload = (
            struct.pack(">B",   self.player_id)
            + struct.pack(">H",   gm["shot_id"])
            + struct.pack(">fff", px, py, pz)
            + struct.pack(">fff", *gm["vel"])
            + struct.pack(">f",   dt)
            + struct.pack(">h",   gm["team"])
            + struct.pack(">B",   target_id & 0xFF)
        )
        self.client.send(MsgGMUpdate, payload)
        logger.info("[%s] GMUpdate gesendet: target=%d (dt=%.2fs)",
                    self.callsign, target_id, dt)

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

    def _recompute_gm_min_range(self) -> None:
        """GM-Geradeaus-/Homing-Grenze = Strecke der geraden Flugphase vor dem Zielsuchen
        (_gmActivationTime × _shotSpeed; GM nutzt die Basis-Schussgeschwindigkeit). Wird bei jeder
        Änderung von _gmActivationTime oder _shotSpeed neu berechnet, statt fest auf 50u zu hängen."""
        self._gm_min_range = self._gm_activation_time * self._shot_speed

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

    def _cleanup_shots(self, now: float) -> None:
        """Entfernt abgelaufene Schüsse aus _shots."""
        with self._shots_lock:
            for k in [k for k, s in self._shots.items() if s.is_expired(now)]:
                del self._shots[k]

    def _notify_count(self) -> None:
        """Ruft on_player_count_changed-Callback auf und meldet den Stand an den Manager."""
        if self.on_player_count_changed:
            try: self.on_player_count_changed(self.human_count)
            except Exception: pass
        self._emit_status()


# ── Hilfsfunktionen (Spielmechanik, nur in bzbot.py benötigt) ─────────────

def _segment_point_dist3d(ax: float, ay: float, az: float,
                           bx: float, by: float, bz: float,
                           cx: float, cy: float, cz: float) -> float:
    """Minimaler Abstand von Punkt C zum 3D-Liniensegment A→B."""
    abx, aby, abz = bx-ax, by-ay, bz-az
    acx, acy, acz = cx-ax, cy-ay, cz-az
    ab2 = abx**2 + aby**2 + abz**2
    # Segment hat Länge 0 → Abstand ist direkte Distanz A→C
    if ab2 < 1e-10:
        return math.sqrt(acx**2 + acy**2 + acz**2)
    # t=0 → Punkt liegt am Anfang A, t=1 → am Ende B; clampen hält t im Segment
    t = max(0.0, min(1.0, (acx*abx + acy*aby + acz*abz) / ab2))
    dx, dy, dz = acx - t*abx, acy - t*aby, acz - t*abz
    return math.sqrt(dx**2 + dy**2 + dz**2)


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
        if dump_path:
            _orig_ready = b._on_world_ready
            def _dump_and_ready(wm):
                _orig_ready(wm)
                if wm is not None:
                    _dump_world_map(wm, dump_path, nav=getattr(b, '_nav_graph', None))
            b._on_world_ready = _dump_and_ready
            b.client.on_world_ready = b._on_world_ready
        if raw_dump_prefix:
            _orig_ready2 = b._on_world_ready
            def _raw_dump_and_ready(wm):
                _orig_ready2(wm)
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
            b._on_world_ready = _raw_dump_and_ready
            b.client.on_world_ready = b._on_world_ready
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
        cos_a = abs(_math.cos(b.angle))
        sin_a = abs(_math.sin(b.angle))
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
