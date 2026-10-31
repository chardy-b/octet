import tempfile
import unittest
from pathlib import Path

import dac_watch


class DacWatchTests(unittest.TestCase):
    def watch(self, pointers):
        times = iter(range(100))
        values = iter(pointers)
        return dac_watch.DacWatch(clock=lambda: float(next(times)), pointer=lambda: next(values))

    def test_advancing_pointer_is_not_stalled(self):
        watch = self.watch([10, 20, 30, 40, 50])
        self.assertEqual([watch.stalled(True) for _ in range(5)], [False] * 5)

    def test_frozen_pointer_is_reported_after_the_grace_period(self):
        watch = self.watch([3] * 6)
        self.assertEqual([watch.stalled(True) for _ in range(6)], [False, False, False, True, True, True])

    def test_idle_dsp_or_closed_stream_resets(self):
        watch = self.watch([3, 3, None, 3, 3])
        self.assertEqual([watch.stalled(True) for _ in range(5)], [False] * 5)
        self.assertFalse(self.watch([3, 3, 3, 3, 3]).stalled(False))

    def test_reads_the_hifiberry_card_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, card, state in (('card0', 'Loopback', 'RUNNING'), ('card1', dac_watch.CARD_ID, 'RUNNING')):
                path = root / name
                (path / 'pcm0p/sub0').mkdir(parents=True)
                (path / 'id').write_text(card + '\n')
                (path / 'pcm0p/sub0/status').write_text(f'state: {state}\nhw_ptr      : 4242\n')
            self.assertEqual(dac_watch.dac_pointer(root), 4242)
            (root / 'card1/pcm0p/sub0/status').write_text('state: closed\n')
            self.assertIsNone(dac_watch.dac_pointer(root))


if __name__ == '__main__':
    unittest.main()
