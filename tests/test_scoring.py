import numpy as np

import clipper
from clipper import BIN, Signals, _even, _local_z, _moving, score, select


def test_even_rounds_to_even_pixels():
    assert _even(1079.4) == 1080
    assert _even(3) == 4
    assert _even(0) == 2  # never collapse to zero width


def test_moving_average_keeps_length():
    x = np.arange(10, dtype=float)
    y = _moving(x, 3)
    assert y.shape == x.shape
    assert np.isclose(y[5], 5.0)


def test_local_z_flags_a_spike():
    x = np.zeros(1000)
    x[500] = 10
    z = _local_z(x)
    assert int(np.argmax(z)) == 500


def _synthetic_signals(duration: float, spikes: list[float]) -> Signals:
    nb = int(duration / BIN)
    rng = np.random.default_rng(0)
    loud = -30 + rng.normal(0, 1, nb).astype(np.float32)
    for s in spikes:
        i = int(s / BIN)
        loud[i : i + 6] = -5  # three seconds of shouting
    return Signals(
        t=np.arange(nb) * BIN,
        loudness=loud,
        motion=rng.random(nb).astype(np.float32) * 0.05,
        heat=np.zeros(nb, dtype=np.float32),
    )


def test_select_finds_the_loud_moments():
    duration = 600.0
    spikes = [120.0, 300.0, 450.0]
    clips = select(score(_synthetic_signals(duration, spikes)), duration, count=3)

    assert len(clips) == 3
    assert [c.index for c in clips] == [1, 2, 3]  # sorted by time, numbered from 1
    for c, s in zip(clips, spikes, strict=True):
        assert c.start <= s <= c.end
        assert 10 <= c.length <= 15
        assert c.reason == clipper.REASONS["audio"] or c.reason == clipper.REASONS["burst"]


def test_select_returns_nothing_for_a_flat_video():
    duration = 300.0
    nb = int(duration / BIN)
    flat = Signals(
        t=np.arange(nb) * BIN,
        loudness=np.full(nb, -30, dtype=np.float32),
        motion=np.zeros(nb, dtype=np.float32),
        heat=np.zeros(nb, dtype=np.float32),
    )
    assert select(score(flat), duration, count=5) == []
