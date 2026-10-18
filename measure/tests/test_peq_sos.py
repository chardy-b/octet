import numpy as np
import pytest
from scipy.signal import sosfreqz

from octetmeasure.peq import Peq, biquad_coeffs, biquad_db, eq_db, sos_for

FS = 48000


def test_coefficients_are_normalised():
    for p in (Peq("PK", 1000, 6, 2), Peq("LS", 100, 4, 0.7), Peq("HS", 8000, -3, 0.7), Peq("HP", 80, 0, 0.71), Peq("LP", 3000, 0, 0.71)):
        b, a = biquad_coeffs(FS, p)
        assert a[0] == 1.0 and len(b) == 3 and len(a) == 3


def test_sos_response_equals_the_analytic_response():
    filters = [Peq("HP", 110, 0, 0.71), Peq("PK", 300, -8, 1.9), Peq("PK", 5500, 3, 1.7), Peq("LP", 12000, 0, 0.71)]
    f = np.geomspace(30, 20000, 300)
    w, h = sosfreqz(sos_for(filters, FS), worN=2 * np.pi * f / FS)
    assert np.allclose(20 * np.log10(np.abs(h)), eq_db(f, FS, filters), atol=1e-9)


def test_single_filter_still_matches():
    p = Peq("PK", 1000, 6.0, 2.0)
    assert biquad_db([1000.0], FS, p)[0] == pytest.approx(6.0, abs=1e-6)
