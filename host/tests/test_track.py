import math
import random

import pytest

from ptz.track import TrackError, average_laps, split_laps


def true_pan(phase):            # the "ideal" camera path over one lap (0..1)
    return 60 * math.sin(2 * math.pi * phase) - 20 * math.sin(math.pi * phase) ** 2


def make_session(lap_times, noise=0.5, bad_lap=None, seed=1):
    """Simulated learning session: the operator follows cars lap after lap,
    with different lap times, hand jitter, and presses the lap key."""
    rnd = random.Random(seed)
    points, markers, t = [], [], 3.0
    points.append({"t": 0.0, "pos": {"pan": true_pan(0), "tilt": -5.0}})
    for i, lt in enumerate(lap_times):
        markers.append(t + rnd.uniform(-0.05, 0.05))           # human press latency
        for k in range(int(lt * 20)):
            ph = k / (lt * 20)
            off = 25 * math.sin(math.pi * ph) if i == bad_lap else 0.0
            points.append({"t": t + k / 20, "pos": {
                "pan": true_pan(ph) + rnd.gauss(0, noise) + off,
                "tilt": -5 + 2 * math.cos(2 * math.pi * ph) + rnd.gauss(0, noise / 3)}})
        t += lt
    markers.append(t)
    points.append({"t": t + 1, "pos": {"pan": true_pan(0), "tilt": -3.0}})
    return points, markers


def test_average_of_laps_recovers_path_and_mean_lap_time():
    points, markers = make_session([60.0, 62.0, 58.5, 61.0])
    laps = split_laps(points, markers)
    assert len(laps) == 4
    avg, stats = average_laps(laps)
    assert avg[-1]["t"] == pytest.approx(60.4, abs=0.2)
    assert all(s["used"] for s in stats)
    for p in avg[:: len(avg) // 20]:
        ph = p["t"] / avg[-1]["t"]
        assert p["pos"]["pan"] == pytest.approx(true_pan(ph), abs=1.5)
    # starts and ends on the start line
    assert avg[0]["pos"]["pan"] == pytest.approx(true_pan(0), abs=1.5)
    assert avg[-1]["pos"]["pan"] == pytest.approx(true_pan(1), abs=1.5)


def test_outlier_lap_is_rejected_and_can_be_excluded_by_hand():
    points, markers = make_session([60, 61, 59, 60, 62], bad_lap=2)
    laps = split_laps(points, markers)
    avg, stats = average_laps(laps)
    assert [s["used"] for s in stats] == [True, True, False, True, True]
    avg2, stats2 = average_laps(laps, exclude=(1,), auto_reject=False)
    assert [s["used"] for s in stats2] == [False, True, True, True, True]


def test_double_press_ignored_and_errors():
    points, markers = make_session([30, 30])
    markers.insert(1, markers[0] + 0.3)                        # bounce
    assert len(split_laps(points, markers)) == 2
    with pytest.raises(TrackError):
        average_laps(split_laps(points, markers[:1]))          # one mark = no lap
    with pytest.raises(TrackError):
        average_laps(split_laps(points, markers), exclude=(1, 2))
