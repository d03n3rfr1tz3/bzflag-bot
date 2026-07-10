"""Gemeinsame @trait-Basis für die BZBot-Mixins (Track 5, mypyc).

Auto-generiert/aktualisiert (scratchpad/gen_base.py): deklariert die über die Mixins
geteilten Attribute/Methoden EINMAL, damit mypy/mypyc die cross-mixin-Zugriffe auflösen
und die Mehrfachvererbung (mypyc erlaubt sie nur mit @trait) kompilieren können.
Daten-Attribute sind bewusst `Any` (keine Zuweisungskonflikte über die Mixins; die
Methoden-Bodies kompilieren dennoch nativ). Methoden sind Stubs — die realen Mixins
überschreiben sie (Signaturen exakt aus der Quelle → Liskov-kompatibel).
"""
from typing import Any, Dict, List, Optional, Tuple, Callable
from mypy_extensions import trait

from bot.models import Shot, PlayerInfo, FlagInfo, AIState


@trait
class BZBotBase:
    """Deklariert die geteilten Member. Keine eigene Logik/Instanziierung."""

    # ── Geteilte Daten-Attribute (bewusst Any) ───────────────────────────
    _active_gm: Any
    _agility_ad_vel: Any
    _ai_state: Any
    _angular_acceleration: Any
    _angular_ad: Any
    _async_cancel: Any
    _async_plan_goal: Any
    _async_plan_lock: Any
    _async_plan_result: Any
    _async_plan_thread: Any
    _bounce_next: Any
    _bounce_replan: Any
    _burrow_ang_ad: Any
    _burrow_depth: Any
    _burrow_speed_ad: Any
    _combat_avoid: Any
    _connection_lost: Any
    _debug_log_dodge: Any
    _debug_log_flag: Any
    _debug_log_path: Any
    _debug_log_shot: Any
    _debug_log_tele: Any
    _debug_nav_tele_t: Any
    _debug_no_jump: Any
    _debug_no_shoot: Any
    _debug_obstacle_logged: Any
    _debug_wp_near_t: Any
    _dodge_dir: Any
    _dodge_forward: Any
    _dodge_reverse: Any
    _dodge_until: Any
    _dodging: Any
    _drop_bad_flag_delay: Any
    _dropped_neutrals: Any
    _escape_jump_ang_vel: Any
    _evade_cleared_shots: Any
    _exploding_until: Any
    _flag_radius: Any
    _game_over: Any
    _gm_activation_time: Any
    _gm_ad_life: Any
    _gm_min_range: Any
    _gm_need_update: Any
    _gm_resend_at: Any
    _gm_send_at: Any
    _gm_turn_angle: Any
    _gravity: Any
    _has_spawned: Any
    _identify_range: Any
    _indirect_hold_until: Any
    _join_rejected: Any
    _jump_ang_vel: Any
    _jump_pending: Any
    _jump_velocity: Any
    _jumping: Any
    _landing_aim_pos: Any
    _landing_hit_z: Any
    _landing_second_shot_at: Any
    _landing_shot_until: Any
    _laser_ad_life: Any
    _laser_ad_rate: Any
    _laser_ad_vel: Any
    _last_drop_attempt: Any
    _last_grab_attempt: Any
    _last_hit_check_pos: Any
    _last_hit_check_t: Any
    _last_jump_at: Any
    _last_notschuss_threat: Any
    _last_pos_check: Any
    _last_pos_check_time: Any
    _last_status_emit: Any
    _last_threat_id: Any
    _lg_gravity: Any
    _limited_flags: Any
    _linear_acceleration: Any
    _link_map: Any
    _lock_on_angle: Any
    _max_shots: Any
    _mgun_ad_life: Any
    _mgun_ad_rate: Any
    _mgun_ad_vel: Any
    _move_reverse: Any
    _muzzle_front: Any
    _muzzle_height: Any
    _narrow_hw: Any
    _nav_goal: Any
    _nav_goal_z: Any
    _nav_graph: Any
    _nav_jump_align_return_state: Any
    _nav_jump_align_start: Any
    _nav_jump_align_wp: Any
    _nav_jump_cooldowns: Any
    _nav_jump_return_state: Any
    _nav_jump_target_z: Any
    _nav_path: Any
    _nav_tele_center: Any
    _nav_tele_cooldowns: Any
    _nav_tele_return_state: Any
    _nav_tele_start: Any
    _next_server_update: Any
    _next_shoot: Any
    _obese_factor: Any
    _order: Any
    _own_flag_since: Any
    _plan_gen: Any
    _pre_fall_state: Any
    _presence: Any
    _recent_flag_targets: Any
    _reconnect_needed: Any
    _reload_time: Any
    _rfire_ad_life: Any
    _rfire_ad_rate: Any
    _rfire_ad_vel: Any
    _rico_aim_cache: Any
    _ricochet_paths: Any
    _round_over: Any
    _round_over_until: Any
    _running: Any
    _server_jumping: Any
    _server_ricochet: Any
    _server_time_offset: Any
    _server_update_interval: Any
    _shield_flight: Any
    _shock_ad_life: Any
    _shock_in_radius: Any
    _shock_out_radius: Any
    _shot_gen: Any
    _shot_grid: Any
    _shot_lifetime: Any
    _shot_radius: Any
    _shot_range: Any
    _shot_slot: Any
    _shot_speed: Any
    _shots: Any
    _shots_lock: Any
    _shots_remaining: Any
    _slot_reload_at: Any
    _spawn_sent_at: Any
    _sr_radius_mult: Any
    _stall_anchor: Any
    _stall_check_at: Any
    _stall_mode: Any
    _stall_rev_dist: Any
    _stall_rev_start: Any
    _stall_until: Any
    _stop_event: Any
    _sw_expand_speed: Any
    _tact_jump_retry_after: Any
    _tactical_jump_until: Any
    _tank_height: Any
    _tank_length: Any
    _tank_speed: Any
    _tank_turn_rate: Any
    _tank_width: Any
    _target_paused_since: Any
    _tele_solid_boxes: Any
    _teleporting_until: Any
    _thief_ad_life: Any
    _thief_ad_shot_vel: Any
    _thief_tiny_factor: Any
    _thief_vel_ad: Any
    _threat_detected_at: Any
    _tick_count: Any
    _tick_memo: Any
    _tiny_factor: Any
    _tlog: Any
    _unreach_phase: Any
    _unreach_replan_at: Any
    _unreach_target: Any
    _unreach_until: Any
    _velocity_ad: Any
    _wall_height: Any
    _wide_angle_ang: Any
    _wings_gravity: Any
    _wings_jump_count: Any
    _wings_jump_velocity: Any
    _wings_jumps_used: Any
    _world_map: Any
    _wp_fail_count: Any
    _wp_start_time: Any
    _wp_timeout: Any
    _z_attack_fire_z: Any
    _z_attack_mode: Any
    _z_attack_retry_after: Any
    alive: Any
    ang_vel: Any
    azimuth: Any
    bad_flags: Any
    bot_callsigns: Any
    bot_name_prefix: Any
    callsign: Any
    client: Any
    death_time: Any
    flags: Any
    good_flags: Any
    host: Any
    human_count: Any
    managed: Any
    motto: Any
    observer_count: Any
    on_player_count_changed: Any
    on_world_ready_extra: Any
    own_flag: Any
    player_id: Any
    players: Any
    port: Any
    pos: Any
    target_player: Any
    target_pos: Any
    team: Any
    token: Any
    vel: Any
    world_half: Any

    # ── Geteilte Methoden (Stubs; reale Impl. in den Mixins) ─────────────
    def _advance_path(self, *, timed_out: bool=False) -> None:
        raise NotImplementedError
    def _any_incoming_threat(self, now: float, vels) -> bool:
        raise NotImplementedError
    def _apply_bounds(self, dt: float, half: float) -> None:
        raise NotImplementedError
    def _apply_movement_caps(self, speed: float, ang_vel: float):
        raise NotImplementedError
    def _apply_obstacle_bounds(self, dt: float) -> None:
        raise NotImplementedError
    def _can_drive_through_obstacles(self) -> bool:
        raise NotImplementedError
    def _can_jump(self, now: float) -> bool:
        raise NotImplementedError
    def _can_move_backward(self) -> bool:
        raise NotImplementedError
    def _can_shoot(self) -> bool:
        raise NotImplementedError
    def _can_turn_left(self) -> bool:
        raise NotImplementedError
    def _can_turn_right(self) -> bool:
        raise NotImplementedError
    def _check_advance_path(self) -> bool:
        raise NotImplementedError
    def _check_opportunistic_grab(self, now: float) -> None:
        raise NotImplementedError
    def _check_tactical_jump(self, now: float) -> bool:
        raise NotImplementedError
    def _check_teleport_crossing(self, old: Tuple[float, float, float], now: float) -> None:
        raise NotImplementedError
    def _check_z_attack_jump(self, now: float) -> bool:
        raise NotImplementedError
    def _compute_dodge_dir(self, threat, now: float):
        raise NotImplementedError
    def _cross_floor_indirect(self, info) -> bool:
        raise NotImplementedError
    def _effective_fov(self) -> float:
        raise NotImplementedError
    def _effective_gravity(self) -> float:
        raise NotImplementedError
    def _effective_half_width(self) -> float:
        raise NotImplementedError
    def _effective_hit_radius(self) -> float:
        raise NotImplementedError
    def _effective_jump_height(self) -> float:
        raise NotImplementedError
    def _effective_jump_velocity(self) -> float:
        raise NotImplementedError
    def _effective_optimal_range(self) -> float:
        raise NotImplementedError
    def _effective_radar_range(self) -> float:
        raise NotImplementedError
    def _effective_reload_time(self) -> float:
        raise NotImplementedError
    def _effective_shot_lifetime(self) -> float:
        raise NotImplementedError
    def _effective_shot_range(self) -> float:
        raise NotImplementedError
    def _effective_shot_speed(self) -> float:
        raise NotImplementedError
    def _effective_tank_speed(self) -> float:
        raise NotImplementedError
    def _effective_turn_rate(self) -> float:
        raise NotImplementedError
    def _enemy_visible_radar(self, info) -> bool:
        raise NotImplementedError
    def _enemy_visible_window(self, info) -> bool:
        raise NotImplementedError
    def _execute_combat_move(self, dt: float, half: float, now: float=0.0) -> None:
        raise NotImplementedError
    def _execute_jump(self) -> None:
        raise NotImplementedError
    def _find_incoming_shot(self, now: float, bot_vel=None):
        raise NotImplementedError
    def _find_target_player(self):
        raise NotImplementedError
    def _get_enemy_pos(self, pid: int):
        raise NotImplementedError
    def _get_floor_z(self) -> float:
        raise NotImplementedError
    def _ground_state(self) -> AIState:
        raise NotImplementedError
    def _handle_threat(self, now: float) -> bool:
        raise NotImplementedError
    def _has_los_to_enemy(self, target_pid: int) -> bool:
        raise NotImplementedError
    def _has_los_to_point(self, ex: float, ey: float, ez: float) -> bool:
        raise NotImplementedError
    def _has_presence(self) -> bool:
        raise NotImplementedError
    def _has_teleporters(self) -> bool:
        raise NotImplementedError
    def _indirect_shot_available(self, target_pid: int) -> bool:
        raise NotImplementedError
    def _initiate_nav_jump(self, wp) -> None:
        raise NotImplementedError
    def _instant_shot_hits(self, shooter: int, shot_id: int, px: float, py: float, pz: float, vx: float, vy: float, vz: float, lifetime: float) -> bool:
        raise NotImplementedError
    def _is_ahead(self, px: float, py: float) -> bool:
        raise NotImplementedError
    def _is_bot_callsign(self, callsign: str) -> bool:
        raise NotImplementedError
    def _is_inside_obstacle(self, include_oo: bool=False) -> bool:
        raise NotImplementedError
    def _is_landed(self) -> bool:
        raise NotImplementedError
    def _jump_launch_vz(self, cur_vz: float) -> float:
        raise NotImplementedError
    def _move_to_target(self, dt: float, half: float) -> None:
        raise NotImplementedError
    def _muzzle_clear(self, az: float) -> bool:
        raise NotImplementedError
    def _navigate_wp(self, dt: float, half: float, reverse: bool=False) -> bool:
        raise NotImplementedError
    def _new_target(self) -> None:
        raise NotImplementedError
    def _next_slot_ready(self, now: float) -> bool:
        raise NotImplementedError
    def _notify_count(self) -> None:
        raise NotImplementedError
    def _own_flag_bytes(self) -> bytes:
        raise NotImplementedError
    def _plan_path(self, goal_x: float, goal_y: float, goal_z: float | None=None, *, cap_wps: int | None=None) -> None:
        raise NotImplementedError
    def _poll_async_plan(self) -> None:
        raise NotImplementedError
    def _recompute_gm_min_range(self) -> None:
        raise NotImplementedError
    def _recompute_presence(self) -> None:
        raise NotImplementedError
    def _report_killed(self, shot: Shot) -> None:
        raise NotImplementedError
    def _run_physics(self, dt: float, now: float) -> None:
        raise NotImplementedError
    def _sees_in_window(self, info, x: float, y: float, z: float, now: Optional[float]=None) -> bool:
        raise NotImplementedError
    def _segment_clear(self, ox: float, oy: float, oz: float, ex: float, ey: float, ez: float) -> bool:
        raise NotImplementedError
    def _send_shot(self, now: float, az: float) -> None:
        raise NotImplementedError
    def _send_teleport(self, source: int, target: int) -> None:
        raise NotImplementedError
    def _set_next_shoot_after_fire(self, now: float) -> None:
        raise NotImplementedError
    def _setup_dodge(self, threat, now: float, time_to_impact: float, dodge_dir: float, orig_diff: Optional[float]=None) -> None:
        raise NotImplementedError
    def _shot_reveals_shooter(self, shooter, ox: float, oy: float, oz: float) -> bool:
        raise NotImplementedError
    def _should_reverse_to_wp(self) -> bool:
        raise NotImplementedError
    def _should_update_player(self, info, px: float, py: float, pz: float, now: float) -> bool:
        raise NotImplementedError
    def _spawn(self) -> None:
        raise NotImplementedError
    def _start_explosion(self, now: float) -> None:
        raise NotImplementedError
    def _steep_wall_ahead(self, az: float, max_dist: float) -> Optional[float]:
        raise NotImplementedError
    def _teleporter_shot_available(self, target_pid: int) -> bool:
        raise NotImplementedError
    def _threat_unseen(self, shooter) -> bool:
        raise NotImplementedError
    def _tick_combat(self, now: float) -> None:
        raise NotImplementedError
    def _transition_to(self, state: AIState) -> None:
        raise NotImplementedError
    def _travel_tank_speed(self) -> float:
        raise NotImplementedError
    def _try_drop_flag(self) -> None:
        raise NotImplementedError
    def _turn_toward(self, target_az: float, dt: float) -> float:
        raise NotImplementedError
    def _update_indirect_hold(self, now: float, in_hold_case: bool) -> bool:
        raise NotImplementedError
    def _validate_and_find_target(self) -> None:
        raise NotImplementedError
    def _wp_reach_radius(self) -> float:
        raise NotImplementedError
    def _z_attack_feasible(self, now: float) -> bool:
        raise NotImplementedError
