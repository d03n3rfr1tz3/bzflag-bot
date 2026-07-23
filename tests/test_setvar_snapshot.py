"""Snapshot-Test für _on_set_var (Track 4/W3).

Geschrieben VOR dem Tabellen-Refactor gegen die if/elif-Implementierung:
schickt jede behandelte Server-Variable einmal durch den Handler und friert
das resultierende Attribut-Verhalten ein (Wert, Guard, abgeleitete Größen).
Der Refactor auf Dispatch-Tabellen muss diesen Test unverändert bestehen.
"""
import struct

import pytest

from conftest import bot  # noqa: F401


def setvar_payload(pairs):
    """Baut ein MsgSetVar-Payload: count(u16), dann je Var nlen(u8)+name+vlen(u8)+value."""
    out = struct.pack(">H", len(pairs))
    for name, val in pairs:
        nb = name.encode("utf-8")
        vb = str(val).encode("utf-8")
        out += struct.pack(">B", len(nb)) + nb + struct.pack(">B", len(vb)) + vb
    return out


def send(bot, name, val):  # noqa: F811
    bot._on_set_var(0, setvar_payload([(name, val)]))


# (Server-Var, Instanz-Attribut, gültiger Testwert)
SIMPLE_FLOAT_VARS = [
    ("_reloadTime",        "_reload_time",        4.5),
    ("_shotSpeed",         "_shot_speed",         200.0),
    ("_shotRange",         "_shot_range",         700.0),
    ("_tankSpeed",         "_tank_speed",         30.0),
    ("_tankAngVel",        "_tank_turn_rate",     1.0),
    ("_jumpVelocity",      "_jump_velocity",      20.0),
    ("_gravity",           "_gravity",            -12.0),
    ("_wingsGravity",      "_wings_gravity",      -5.0),
    ("_wingsJumpVelocity", "_wings_jump_velocity", 15.0),
    ("_dropBadFlagDelay",  "_drop_bad_flag_delay", 2.5),
    ("_shockInRadius",     "_shock_in_radius",    7.0),
    ("_shockOutRadius",    "_shock_out_radius",   80.0),
    ("_shockAdLife",       "_shock_ad_life",      0.3),
    ("_gmTurnAngle",       "_gm_turn_angle",      0.9),
    ("_gmActivationTime",  "_gm_activation_time", 0.7),
    ("_gmAdLife",          "_gm_ad_life",         0.8),
    ("_lockOnAngle",       "_lock_on_angle",      0.2),
    ("_obeseFactor",       "_obese_factor",       3.0),
    ("_flagRadius",        "_flag_radius",        4.0),
    ("_velocityAd",        "_velocity_ad",        1.8),
    ("_agilityAdVel",      "_agility_ad_vel",     2.5),
    ("_lgGravity",         "_lg_gravity",         15.0),
    ("_burrowDepth",       "_burrow_depth",       -2.0),
    ("_burrowSpeedAd",     "_burrow_speed_ad",    0.9),
    ("_burrowAngularAd",   "_burrow_ang_ad",      0.6),
    ("_angularAd",         "_angular_ad",         1.7),
    ("_wideAngleAng",      "_wide_angle_ang",     2.0),
    ("_shieldFlight",      "_shield_flight",      3.0),
    ("_identifyRange",     "_identify_range",     60.0),
    ("_mGunAdRate",        "_mgun_ad_rate",       12.0),
    ("_mGunAdLife",        "_mgun_ad_life",       1.8),
    ("_mGunAdVel",         "_mgun_ad_vel",        0.2),
    ("_rFireAdRate",       "_rfire_ad_rate",      3.0),
    ("_rFireAdVel",        "_rfire_ad_vel",       1.8),
    ("_rFireAdLife",       "_rfire_ad_life",      0.4),
    ("_laserAdVel",        "_laser_ad_vel",       900.0),
    ("_laserAdRate",       "_laser_ad_rate",      0.6),
    ("_laserAdLife",       "_laser_ad_life",      0.2),
    ("_thiefAdShotVel",    "_thief_ad_shot_vel",  9.0),
    ("_thiefAdLife",       "_thief_ad_life",      0.1),
    ("_muzzleHeight",      "_muzzle_height",      1.8),
    ("_muzzleFront",       "_muzzle_front",       5.0),
    ("_tankLength",        "_tank_length",        7.0),
    ("_tankWidth",         "_tank_width",         3.0),
    ("_tankHeight",        "_tank_height",        2.5),
    ("_wallHeight",        "_wall_height",        9.0),
    ("_shotRadius",        "_shot_radius",        0.7),
    ("_tinyFactor",        "_tiny_factor",        0.5),
    ("_thiefTinyFactor",   "_thief_tiny_factor",  0.6),
    ("_thiefVelAd",        "_thief_vel_ad",       1.9),
    ("_narrowHW",          "_narrow_hw",          0.4),
    # W3/F8: Nachführung neu ergänzt (vorher rechnete der Steamroller-Radius
    # immer mit dem 0.8-Default)
    ("_srRadiusMult",      "_sr_radius_mult",     1.2),
    # P4-MOV-02: M-Flaggen-Trägheit (Momentum-Beschleunigungsgrenzen)
    ("_momentumLinAcc",    "_momentum_lin_acc",   5.0),
    ("_momentumAngAcc",    "_momentum_ang_acc",   3.0),
]


class TestSetVarSnapshot:

    @pytest.mark.parametrize("var,attr,val", SIMPLE_FLOAT_VARS,
                             ids=[v for v, _a, _x in SIMPLE_FLOAT_VARS])
    def test_float_var_tracked(self, bot, var, attr, val):  # noqa: F811
        send(bot, var, val)
        assert getattr(bot, attr) == pytest.approx(val)

    def test_int_vars_tracked(self, bot):  # noqa: F811
        send(bot, "_maxShots", "3")
        assert bot._max_shots == 3
        send(bot, "_maxShots", "4.0")   # int(float(val)) — auch "4.0" ist gültig
        assert bot._max_shots == 4
        send(bot, "_wingsJumpCount", "2")
        assert bot._wings_jump_count == 2
        send(bot, "_wingsJumpCount", "0")   # >= 0 erlaubt
        assert bot._wings_jump_count == 0

    def test_multiple_vars_in_one_message(self, bot):  # noqa: F811
        bot._on_set_var(0, setvar_payload([("_tankSpeed", 27.5), ("_maxShots", 5)]))
        assert bot._tank_speed == pytest.approx(27.5)
        assert bot._max_shots == 5

    # ── Abgeleitete Größen ────────────────────────────────────────────────

    def test_shot_speed_recomputes_lifetime_and_gm_range(self, bot):  # noqa: F811
        send(bot, "_shotSpeed", 200.0)
        assert bot._shot_lifetime == pytest.approx(bot._shot_range / 200.0)
        assert bot._gm_min_range == pytest.approx(bot._gm_activation_time * 200.0)

    def test_shot_range_recomputes_lifetime(self, bot):  # noqa: F811
        send(bot, "_shotRange", 700.0)
        assert bot._shot_lifetime == pytest.approx(700.0 / bot._shot_speed)

    def test_gm_activation_time_recomputes_gm_range(self, bot):  # noqa: F811
        send(bot, "_gmActivationTime", 0.7)
        assert bot._gm_min_range == pytest.approx(0.7 * bot._shot_speed)

    @pytest.mark.parametrize("var,val", [
        ("_reloadTime", 4.0), ("_shockInRadius", 10.0),
        ("_shockOutRadius", 90.0), ("_shockAdLife", 0.25),
    ])
    def test_sw_expand_speed_recomputed(self, bot, var, val):  # noqa: F811
        send(bot, var, val)
        expected = (bot._shock_out_radius - bot._shock_in_radius) / (bot._reload_time * bot._shock_ad_life)
        assert bot._sw_expand_speed == pytest.approx(expected)

    def test_tank_height_updates_wall_height(self, bot):  # noqa: F811
        send(bot, "_tankHeight", 2.5)
        assert bot._wall_height == pytest.approx(7.5)
        # explizite _wallHeight überschreibt danach
        send(bot, "_wallHeight", 9.0)
        assert bot._wall_height == pytest.approx(9.0)

    def test_update_throttle_rate_sets_interval(self, bot):  # noqa: F811
        send(bot, "_updateThrottleRate", 20.0)
        assert bot._server_update_interval == pytest.approx(1.0 / 20.0)
        # schneller als 30 Hz wird auf 30 Hz geklemmt
        send(bot, "_updateThrottleRate", 60.0)
        assert bot._server_update_interval == pytest.approx(1.0 / 30.0)

    def test_world_size_updates_half_and_client_cache(self, bot):  # noqa: F811
        send(bot, "_worldSize", 1600.0)
        assert bot.world_half == pytest.approx(800.0)
        assert bot.client._world_half_cache == pytest.approx(800.0)

    # ── Guards ────────────────────────────────────────────────────────────

    @pytest.mark.parametrize("var,attr,bad", [
        ("_tankSpeed",   "_tank_speed",   0.0),     # > 0 verlangt
        ("_tankSpeed",   "_tank_speed",   -5.0),
        ("_gravity",     "_gravity",      0.0),     # != 0 verlangt
        ("_shotSpeed",   "_shot_speed",   0.0),
        ("_gmTurnAngle", "_gm_turn_angle", -1.0),
        ("_shockInRadius", "_shock_in_radius", -1.0),  # >= 0 verlangt
        ("_narrowHW",    "_narrow_hw",    -0.1),
    ])
    def test_guard_rejects_value(self, bot, var, attr, bad):  # noqa: F811
        before = getattr(bot, attr)
        send(bot, var, bad)
        assert getattr(bot, attr) == before

    def test_max_shots_zero_rejected(self, bot):  # noqa: F811
        before = bot._max_shots
        send(bot, "_maxShots", 0)
        assert bot._max_shots == before

    def test_burrow_depth_has_no_guard(self, bot):  # noqa: F811
        send(bot, "_burrowDepth", -5.0)
        assert bot._burrow_depth == pytest.approx(-5.0)

    def test_non_numeric_value_ignored(self, bot):  # noqa: F811
        before = bot._tank_speed
        send(bot, "_tankSpeed", "abc")
        assert bot._tank_speed == before

    def test_unknown_var_ignored(self, bot):  # noqa: F811
        bot._on_set_var(0, setvar_payload([("_unknownVar", 1.0)]))  # darf nicht werfen

    def test_truncated_payload_no_crash(self, bot):  # noqa: F811
        payload = setvar_payload([("_tankSpeed", 30.0)])
        for cut in (1, 3, 5, len(payload) - 1):
            bot._on_set_var(0, payload[:cut])
