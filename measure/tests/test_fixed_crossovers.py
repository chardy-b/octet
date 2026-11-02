"""Fixed crossovers keep the same frequencies and slope; only delay, gain and polarity are fitted."""
import numpy as np
import pytest

from octetmeasure.autotune import config, dsp


def setup(pinned):
    cfg = config.example()
    cfg.update(selected_pairs=['a', 'sub', 'tweeter'], fixed_crossovers=pinned)
    cfg = config.validate(cfg)
    f = np.geomspace(20, 20000, 600)
    responses = {d['id']: dict(h=np.ones(len(f), dtype=complex)) for d in config.active_drivers(cfg)}
    return cfg, f, responses, {d['id']: [] for d in config.active_drivers(cfg)}


def test_both_sides_use_the_pinned_crossovers():
    cfg, f, responses, eq = setup(dict(low=290, high=4100, order=8))
    for side in ('L', 'R'):
        candidates = dsp.search(cfg, f, responses, eq, side)
        assert candidates
        for c in candidates:
            assert (c['low'], c['high'], c['order']) == (290, 4100, 8)
            assert set(c['delays']) == {'sub', 'a', 'tweeter'}


@pytest.mark.parametrize('pinned', [dict(low=100), dict(high=3000), dict(order=6), dict(split=900), []])
def test_unsafe_or_unknown_pins_are_rejected(pinned):
    with pytest.raises(ValueError, match='fixed_crossovers'):
        setup(pinned)
