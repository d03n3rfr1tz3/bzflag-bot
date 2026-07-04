import math
import time
import pytest


def import_symbols():
    from bot.models import Shot
    from bot.util import _angle_diff, _wrap, _segment_point_dist3d
    return _angle_diff, _wrap, _segment_point_dist3d, Shot


# ── _angle_diff ───────────────────────────────────────────────────────────────

def test_angle_diff_same():
    _angle_diff, _, _, _ = import_symbols()
    assert _angle_diff(0.0, 0.0) == pytest.approx(0.0)


def test_angle_diff_quarter_ccw():
    _angle_diff, _, _, _ = import_symbols()
    assert _angle_diff(math.pi / 2, 0.0) == pytest.approx(math.pi / 2)


def test_angle_diff_quarter_cw():
    _angle_diff, _, _, _ = import_symbols()
    assert _angle_diff(0.0, math.pi / 2) == pytest.approx(-math.pi / 2)


def test_angle_diff_wrap_over_pi():
    _angle_diff, _, _, _ = import_symbols()
    # 350° → 10°: shortest path is +20° (CCW)
    target  = math.radians(10)
    current = math.radians(350)
    result  = _angle_diff(target, current)
    assert result == pytest.approx(math.radians(20), abs=1e-6)


def test_angle_diff_wrap_under_neg_pi():
    _angle_diff, _, _, _ = import_symbols()
    # 10° → 350°: shortest path is -20° (CW)
    target  = math.radians(350)
    current = math.radians(10)
    result  = _angle_diff(target, current)
    assert result == pytest.approx(math.radians(-20), abs=1e-6)


def test_angle_diff_half_circle():
    _angle_diff, _, _, _ = import_symbols()
    result = _angle_diff(math.pi, 0.0)
    assert abs(result) == pytest.approx(math.pi)


# ── _wrap ──────────────────────────────────────────────────────────────────────

def test_wrap_over_pi():
    _, _wrap, _, _ = import_symbols()
    assert _wrap(1.2 * math.pi) == pytest.approx(-0.8 * math.pi, abs=1e-6)


def test_wrap_under_neg_pi():
    _, _wrap, _, _ = import_symbols()
    assert _wrap(-1.2 * math.pi) == pytest.approx(0.8 * math.pi, abs=1e-6)


def test_wrap_zero():
    _, _wrap, _, _ = import_symbols()
    assert _wrap(0.0) == pytest.approx(0.0)


def test_wrap_identity():
    _, _wrap, _, _ = import_symbols()
    assert _wrap(math.pi / 4) == pytest.approx(math.pi / 4)


# ── _segment_point_dist3d ─────────────────────────────────────────────────────

def test_segment_point_dist_midpoint():
    _, _, seg, _ = import_symbols()
    # Point IS on the midpoint of the segment → dist = 0
    d = seg(0, 0, 0,  10, 0, 0,  5, 0, 0)
    assert d == pytest.approx(0.0, abs=1e-6)


def test_segment_point_dist_perpendicular():
    _, _, seg, _ = import_symbols()
    # Segment along x-axis; point at (5, 7, 0) → dist = 7
    d = seg(0, 0, 0,  10, 0, 0,  5, 7, 0)
    assert d == pytest.approx(7.0, abs=1e-6)


def test_segment_point_dist_3d():
    _, _, seg, _ = import_symbols()
    # Point at (5, 0, 4) offset in z → dist = 4
    d = seg(0, 0, 0,  10, 0, 0,  5, 0, 4)
    assert d == pytest.approx(4.0, abs=1e-6)


def test_segment_point_dist_before_start():
    _, _, seg, _ = import_symbols()
    # Point is "behind" A → closest point is A
    d = seg(0, 0, 0,  10, 0, 0,  -3, 4, 0)
    assert d == pytest.approx(5.0, abs=1e-6)  # hypot(3, 4) = 5


def test_segment_point_dist_after_end():
    _, _, seg, _ = import_symbols()
    # Point is beyond B → closest point is B
    d = seg(0, 0, 0,  10, 0, 0,  13, 4, 0)
    assert d == pytest.approx(5.0, abs=1e-6)  # hypot(3, 4) = 5


def test_segment_point_dist_degenerate():
    _, _, seg, _ = import_symbols()
    # A == B (degenerate segment) → dist = |A - C|
    d = seg(3, 4, 0,  3, 4, 0,  0, 0, 0)
    assert d == pytest.approx(5.0, abs=1e-6)  # hypot(3, 4) = 5


# ── Shot.position_at ──────────────────────────────────────────────────────────

def test_shot_position_at_zero():
    _, _, _, Shot = import_symbols()
    ft = time.monotonic()
    s = Shot(1, 1, [10.0, 20.0, 0.0], [100.0, 0.0, 0.0], ft, 3.5, 1)
    x, y, z = s.position_at(ft)
    assert x == pytest.approx(10.0)
    assert y == pytest.approx(20.0)


def test_shot_position_at_one_second():
    _, _, _, Shot = import_symbols()
    ft = 1000.0
    s = Shot(1, 1, [10.0, 0.0, 0.0], [100.0, 50.0, 0.0], ft, 3.5, 1)
    x, y, z = s.position_at(ft + 1.0)
    assert x == pytest.approx(110.0)
    assert y == pytest.approx(50.0)


def test_shot_position_at_negative_delta():
    _, _, _, Shot = import_symbols()
    ft = 1000.0
    s = Shot(1, 1, [50.0, 0.0, 0.0], [-100.0, 0.0, 0.0], ft, 3.5, 1)
    # t < fire_time returns extrapolated backwards
    x, y, z = s.position_at(ft - 0.5)
    assert x == pytest.approx(100.0)


# ── Shot.is_expired ───────────────────────────────────────────────────────────

def test_shot_expired():
    _, _, _, Shot = import_symbols()
    ft = 1000.0
    s = Shot(1, 1, [0.0, 0.0, 0.0], [100.0, 0.0, 0.0], ft, 3.5, 1)
    assert s.is_expired(ft + 4.0) is True


def test_shot_not_expired():
    _, _, _, Shot = import_symbols()
    ft = 1000.0
    s = Shot(1, 1, [0.0, 0.0, 0.0], [100.0, 0.0, 0.0], ft, 3.5, 1)
    assert s.is_expired(ft + 2.0) is False


def test_shot_expired_exactly_at_lifetime():
    _, _, _, Shot = import_symbols()
    ft = 1000.0
    s = Shot(1, 1, [0.0, 0.0, 0.0], [100.0, 0.0, 0.0], ft, 3.5, 1)
    assert s.is_expired(ft + 3.5) is True


# ── Shot.closest_approach_dist ────────────────────────────────────────────────

def test_closest_approach_direct_hit():
    _, _, _, Shot = import_symbols()
    ft = time.monotonic()
    # Shot at (10, 0) flying toward (0, 0)
    s = Shot(1, 1, [10.0, 0.0, 0.0], [-100.0, 0.0, 0.0], ft, 3.5, 1)
    d = s.closest_approach_dist(0.0, 0.0)
    assert d == pytest.approx(0.0, abs=1e-6)


def test_closest_approach_miss():
    _, _, _, Shot = import_symbols()
    ft = time.monotonic()
    # Shot at (10, 20) flying horizontally → passes 20u from (0,0)
    s = Shot(1, 1, [10.0, 20.0, 0.0], [-100.0, 0.0, 0.0], ft, 3.5, 1)
    d = s.closest_approach_dist(0.0, 0.0)
    assert d == pytest.approx(20.0, abs=1e-6)


def test_closest_approach_stationary_shot():
    _, _, _, Shot = import_symbols()
    ft = time.monotonic()
    s = Shot(1, 1, [3.0, 4.0, 0.0], [0.0, 0.0, 0.0], ft, 3.5, 1)
    d = s.closest_approach_dist(0.0, 0.0)
    assert d == pytest.approx(5.0, abs=1e-6)  # hypot(3, 4)


# ── Shot.time_to_closest ──────────────────────────────────────────────────────

def test_time_to_closest_approaching():
    _, _, _, Shot = import_symbols()
    ft = time.monotonic()
    # Shot at (50, 0) flying at -100 u/s → reaches x=0 in 0.5s
    s = Shot(1, 1, [50.0, 0.0, 0.0], [-100.0, 0.0, 0.0], ft, 3.5, 1)
    t = s.time_to_closest(0.0, 0.0)
    assert t == pytest.approx(0.5, abs=1e-6)


def test_time_to_closest_moving_away():
    _, _, _, Shot = import_symbols()
    ft = time.monotonic()
    # Shot moving away → time_to_closest clamped to 0
    s = Shot(1, 1, [50.0, 0.0, 0.0], [100.0, 0.0, 0.0], ft, 3.5, 1)
    t = s.time_to_closest(0.0, 0.0)
    assert t == pytest.approx(0.0)
