import pytest

from ptz.trajectory import (BlendIn, MoveTrajectory, Pchip, PlaybackTrajectory, SCurveProfile,
                            VelocityShaper, braking_speed)


@pytest.mark.parametrize("vmax,amax,smooth", [(0.5, 1.0, 0.0), (0.5, 1.0, 0.4),
                                              (10.0, 2.0, 0.3), (0.2, 50.0, 1.0)])
def test_scurve_reaches_target_within_limits(vmax, amax, smooth):
    p = SCurveProfile(vmax, amax, smooth)
    dt = 0.0005
    t, prev_v, peak_v, peak_a = 0.0, 0.0, 0.0, 0.0
    prev_s = 0.0
    while t <= p.duration + 0.01:
        s, v = p.sample(t)
        assert s >= prev_s - 1e-9                 # monotonic
        peak_v = max(peak_v, v)
        peak_a = max(peak_a, abs(v - prev_v) / dt)
        prev_v, prev_s = v, s
        t += dt
    assert p.sample(p.duration) == (1.0, 0.0)
    assert peak_v <= vmax * 1.001 + 1e-9
    assert peak_a <= amax * 1.02 + 1e-6
    # continuity of position at the end (no jump)
    assert p.sample(p.duration - 1e-4)[0] == pytest.approx(1.0, abs=1e-3)


def test_scurve_smoothing_bounds_jerk():
    amax, smooth, dt = 2.0, 0.5, 0.001
    p = SCurveProfile(1.0, amax, smooth)
    accs = []
    for i in range(int(p.duration / dt)):
        v0, v1 = p.sample(i * dt)[1], p.sample((i + 1) * dt)[1]
        accs.append((v1 - v0) / dt)
    jerks = [abs(b - a) / dt for a, b in zip(accs, accs[1:])]
    assert max(jerks) <= amax / smooth * 1.05


def test_velocity_shaper_converges_without_overshoot():
    for jerk in (float("inf"), 200.0, 40.0):
        sh = VelocityShaper()
        vs = []
        for _ in range(3000):
            vs.append(sh.step(30.0, 50.0, jerk, 0.001))
        assert sh.v == pytest.approx(30.0)
        assert max(vs) <= 30.0 * 1.02
        for _ in range(3000):
            sh.step(0.0, 50.0, jerk, 0.001)
        assert sh.v == 0.0


def test_braking_speed_matches_shaper():
    amax, smooth, dt = 50.0, 0.2, 0.001
    v0 = braking_speed(10.0, amax, smooth)
    sh = VelocityShaper()
    sh.reset(v0)
    dist = 0.0
    while sh.v > 0:
        sh.step(0.0, amax, amax / smooth, dt)
        dist += sh.v * dt
    assert dist <= 10.0 * 1.05


def test_pchip_passes_points_and_does_not_overshoot():
    ts, ys = [0, 1, 2, 3, 4], [0, 10, 10, 5, 20]
    sp = Pchip(ts, ys)
    for t, y in zip(ts, ys):
        assert sp(t)[0] == pytest.approx(y)
    assert sp(0)[1] == 0 and sp(4)[1] == 0
    for i in range(401):
        y = sp(i / 100)[0]
        assert -1e-9 <= y <= 20 + 1e-9
    assert all(9.999 <= sp(1 + i / 100)[0] <= 10.001 for i in range(101))   # flat segment


def test_move_trajectory_is_synchronized():
    mv = MoveTrajectory({"pan": 0, "tilt": 0}, {"pan": 90, "tilt": -30},
                        {"pan": 45, "tilt": 45}, {"pan": 50, "tilt": 50}, 0.3)
    half = None
    while not mv.done:
        mv.advance(0.01)
        s = mv.sample()
        if half is None and s["pan"][0] >= 45:
            half = s
    assert half["tilt"][0] == pytest.approx(-15, abs=0.5)
    assert mv.sample()["pan"] == (90, 0.0)


def test_playback_speed_factor_and_velocity_cap():
    pts = [{"t": 0.0, "pos": {"pan": 0.0}}, {"t": 2.0, "pos": {"pan": 20.0}},
           {"t": 4.0, "pos": {"pan": 0.0}}]
    speed = [2.0]
    pb = PlaybackTrajectory(pts, {"pan": 100.0}, lambda: speed[0])
    t = 0.0
    while not pb.done and t < 10:
        pb.advance(0.01)
        t += 0.01
    assert 2.0 < t < 2.6                       # ~2x faster (slewed start)
    slow = PlaybackTrajectory(pts, {"pan": 5.0}, lambda: 1.0)
    peak = 0.0
    while not slow.done:
        slow.advance(0.01)
        peak = max(peak, abs(slow.sample()["pan"][1]))
    assert peak <= 5.0 + 1e-6                  # never faster than the axis allows


def test_velocity_shaper_settles_with_jittery_ticks():
    """Regression: with uneven tick times the shaper could stop with a tiny
    residual speed and a = 0, i.e. the axis kept creeping after a jog."""
    import random
    for seed in range(1500):
        r = random.Random(seed)
        amax = r.choice([10, 50, 400, 4000])
        jerk = amax / r.choice([0.03, 0.06, 0.18, 0.6])
        target = r.choice([0.0, r.uniform(-30, 30)])
        sh = VelocityShaper()
        for _ in range(100):
            sh.step(r.uniform(-1, 1) * r.choice([5, 50, 200]), amax, jerk, r.uniform(0.005, 0.03))
        for _ in range(600):
            sh.step(target, amax, jerk, r.uniform(0.005, 0.03))
        assert sh.v == target, (seed, sh.v, sh.a)


def test_blend_in_starts_at_current_state_and_joins_the_path():
    pts = [{"t": 0.0, "pos": {"pan": 0.0}}, {"t": 2.0, "pos": {"pan": 40.0}},
           {"t": 4.0, "pos": {"pan": 0.0}}]
    lap = PlaybackTrajectory(pts, {"pan": 100.0}, lambda: 1.0, natural_ends=True)
    b = BlendIn(lap, {"pan": 25.0}, {"pan": 12.0}, {"pan": 100.0}, {"pan": 200.0})
    p0, v0 = b.sample()["pan"]
    assert p0 == pytest.approx(25.0) and v0 == pytest.approx(12.0)
    prev_p, prev_v = p0, v0
    dt = 0.01
    while b.t < b.T + 0.2:
        b.advance(dt)
        p, v = b.sample()["pan"]
        assert abs(p - prev_p) < 1.0                          # continuous position
        assert abs(v - prev_v) / dt < 400                     # bounded acceleration
        prev_p, prev_v = p, v
    assert b.sample()["pan"] == lap.sample()["pan"]           # joined the lap exactly
