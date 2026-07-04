#!/usr/bin/env python3
"""BZFlag-2.4-Bot: Protokoll, Netzwerk, Spielmechanik, Hit-Detection.

Bewegungs- und KI-Logik → bzbot_ai.py (BZBotAI-Mixin).
Konstanten → bot/constants.py; Shot/PlayerInfo/FlagInfo/AIState → bot/models.py;
Geometrie-Helfer → bot/util.py (Track 4/W1+W2).
"""

import argparse, collections, json, logging, math, random, re, struct, sys, time, threading
from typing import Dict, List, Optional, Tuple

from bzflag.client       import BZFlagClient
from bzflag.shot_physics import (simulate_shot_path,
                                  can_ricochet as _can_ricochet_shot,
                                  build_link_map,
                                  _segment_hits_obb_3d, _extend_segment)
from bzflag.world_map import teleporter_solid_boxes
from bzflag.obstacle_grid import ObstacleGrid, LOS_GRID_PAD
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


# Daten-Klassen (Shot/PlayerInfo/FlagInfo) → bot/models.py, Geometrie-Helfer →
# bot/util.py (Track 4/W2); Re-Import hält den bzbot-Namespace stabil.
from bot.models import Shot, PlayerInfo, FlagInfo  # noqa: F401
from bot.util import _segment_point_dist3d  # noqa: F401
from bot.handlers import HandlersMixin
from bot.hit_detection import HitDetectionMixin


# ── Bot ───────────────────────────────────────────────────────────────────

# Managed-Modus: Intervall des Status-Heartbeats an den Bot-Manager (Sekunden).
STATUS_HEARTBEAT_S = 2.0


class BZBot(HitDetectionMixin, HandlersMixin, BZBotAI):
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
        # Hit-Detection-Fenster (Teil C): Zeitpunkt und eigene Position des letzten
        # _resolve_incoming_shots-Laufs — entkoppelt die Schusspfad-Abdeckung vom
        # 0,1-s-Stall-Clamp der Hauptschleife und trägt die Eigenbewegung in den
        # Sweep ein (Client-Äquivalent: relativeRay). Reset bei Spawn (_on_alive).
        self._last_hit_check_t   = time.monotonic()
        self._last_hit_check_pos: Optional[Tuple[float, float, float]] = None
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
        self._shot_grid = None   # Optional[ObstacleGrid] — Broad-Phase für simulate_shot_path (P1)
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
        # P4a: Per-Tick-Memo für teure, mehrfach pro Tick identisch aufgerufene
        # Wahrnehmungs-Queries (_get_floor_z/_has_los_to_enemy/_muzzle_clear).
        # Keys enthalten alle Eingaben (Position etc.) → reiner Funktions-Memo;
        # wird am Anfang jedes Game-Loop-Ticks geleert. Nur Main-Thread.
        self._tick_memo: Dict = {}

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
        # Kadenz-Anker HIER setzen, nicht in __init__: der Init-Wert 0.0 läge um die
        # komplette System-Uptime hinter time.monotonic() — der +=-Catch-up in
        # _maybe_send_server_update holt so ein Defizit nur mit 1s/s auf, der Bot
        # sendete also dauerhaft mit Tick-Rate (~60 Hz) statt 30 Hz (Server-Messung
        # 2026-07-04: sendto = 46% der aktiven CPU; FABLE-PLAN.md Teil 1b, N1a).
        self._next_server_update = time.monotonic()
        while self._running and not self._stop_event.is_set():
            now  = time.monotonic()
            # Stall-Clamp (GC, Netz-Hänger, Container-Scheduling): ein ungebremster
            # Einzelschritt tunnelt per Zielpunkt-Kollision durch dünne Wände und
            # überdreht das GM-Steering (max_turn = _gm_turn_angle * dt). 0,1s = 6
            # Nominal-Ticks; jenseits davon läuft die Simulation lieber kurz
            # „zeitlupig" weiter. Die Hit-Detection hängt NICHT am Clamp: sie prüft
            # ihr eigenes echtes Fenster [_last_hit_check_t, now] (lückenlos auch
            # bei langen Ticks, s. _resolve_incoming_shots).
            dt_r = min(now - last_tick, 0.1)
            last_tick = now
            if not self.client.connected:
                logger.warning("[%s] Verbindung verloren", self.callsign)
                break
            # UDP-Handshake notfalls wiederholen, bis udp_active (sonst schießt der Bot nie —
            # _can_shoot gatet auf udp_active, weil TCP-Shots gekickt werden). Selbst-gedrosselt.
            if not self.client.udp_active:
                self.client.retry_udp_link()
            self._tick_count += 1
            self._tick_memo.clear()   # P4a: Wahrnehmungs-Memo gilt genau einen Tick
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
            self._maybe_send_server_update(now)
            # Rundenende: nach der Endstand-Linger-Phase die Schleife verlassen → Reconnect
            if self._round_over_until is not None and time.monotonic() >= self._round_over_until:
                break
            # Managed-Modus: regelmäßiger Status-Heartbeat an den Manager (auch tot/respawnend)
            if self.managed and now - self._last_status_emit >= STATUS_HEARTBEAT_S:
                self._emit_status()
            self._cleanup_shots(now)
            # N3: time.sleep statt Event.wait — spart die Condition/Lock-
            # Maschinerie des Events (Nachmessung tmp4: ≈7% der aktiven CPU
            # plus ~230 Futex-Wakeups/s Kernel-Anteil, den cProfile nicht
            # sieht). stop() greift über _running/_stop_event am nächsten
            # Schleifenkopf — Latenz maximal eine Tick-Dauer (~16ms), akzeptiert.
            time.sleep(max(0.0, dt - (time.monotonic() - now)))

    # ── Treffer-Erkennung ─────────────────────────────────────────────────

    # ── Netzwerk senden ───────────────────────────────────────────────────

    def _maybe_send_server_update(self, now: float) -> None:
        """Sendet MsgPlayerUpdate mit fester Kadenz (30 Hz bzw. `_updateThrottleRate`-Intervall).

        Drift-freie Kadenz über `+= interval`; die Klemme danach verhindert
        Burst-Nachholen: nach einem Stall (GC, Container-Scheduling) wird die
        Kadenz neu aufgesetzt statt jede verpasste Periode nachzusenden — sonst
        ginge pro Tick ein Update raus, bis das Defizit abgetragen ist (genau
        der N1a-Bug aus FABLE-PLAN.md Teil 1b, dort via Init-Wert 0.0)."""
        if now >= self._next_server_update:
            self._send_update()
            self._next_server_update += self._server_update_interval
            if self._next_server_update <= now:   # Stall/Start: neu aufsetzen statt nachholen
                self._next_server_update = now + self._server_update_interval

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

    def _sync_nav_physics(self) -> None:
        """Reicht die globalen Server-Variablen _jumpVelocity/_gravity in den (ggf. schon gebauten)
        NavGraph durch — für MsgSetVar, die erst nach dem Weltladen eintreffen. No-Op wenn noch kein
        Graph existiert (dann greift der Build-Aufruf) oder Werte unverändert."""
        nav = getattr(self, "_nav_graph", None)
        if nav is not None:
            nav.set_physics(self._jump_velocity, abs(self._gravity))

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
        self.human_count = sum(1 for p in list(self.players.values()) if p.is_human)
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

    def _try_drop_flag(self) -> None:
        """Sendet MsgDropFlag um eigene Flag abzulegen."""
        if self.player_id is None or not self.own_flag: return
        self._last_drop_attempt = time.monotonic()
        self._last_grab_attempt = time.monotonic()  # 0.5s Grab-Cooldown nach Drop
        self.client.send(MsgDropFlag, struct.pack(">fff", *self.pos))
        if getattr(self, '_debug_log_flag', False):
            logger.debug("[%s] Flagge: MsgDropFlag gesendet (Flag=%r)", self.callsign, self.own_flag)

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

    def _recompute_gm_min_range(self) -> None:
        """GM-Geradeaus-/Homing-Grenze = Strecke der geraden Flugphase vor dem Zielsuchen
        (_gmActivationTime × _shotSpeed; GM nutzt die Basis-Schussgeschwindigkeit). Wird bei jeder
        Änderung von _gmActivationTime oder _shotSpeed neu berechnet, statt fest auf 50u zu hängen."""
        self._gm_min_range = self._gm_activation_time * self._shot_speed

    def _notify_count(self) -> None:
        """Ruft on_player_count_changed-Callback auf und meldet den Stand an den Manager."""
        if self.on_player_count_changed:
            try: self.on_player_count_changed(self.human_count)
            except Exception: pass
        self._emit_status()




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
