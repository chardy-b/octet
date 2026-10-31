"""Notice when the DAC's I2S output has stopped moving while CamillaDSP says it is running."""
from pathlib import Path
import time

CARD_ID = 'sndrpihifiberry'
STALL_SECONDS = 3.0


def dac_pointer(proc=Path('/proc/asound')):
    """Return the DAC playback hw_ptr, or None when it is not a running stream."""
    for card in proc.glob('card*'):
        try:
            if (card / 'id').read_text().strip() != CARD_ID:
                continue
            fields = {key.strip(): value.strip() for key, _, value in
                      (line.partition(':') for line in (card / 'pcm0p/sub0/status').read_text().splitlines()) if _}
            if fields.get('state') != 'RUNNING':
                return None
            return int(fields['hw_ptr'])
        except (OSError, ValueError, KeyError):
            return None
    return None


class DacWatch:
    def __init__(self, clock=time.monotonic, pointer=dac_pointer):
        self.clock, self.pointer = clock, pointer
        self.last = None
        self.since = None

    def stalled(self, dsp_running):
        """True once the pointer has not advanced for STALL_SECONDS while the DSP is running."""
        now, pointer = self.clock(), self.pointer()
        if not dsp_running or pointer is None:
            self.last = self.since = None
            return False
        if pointer != self.last:
            self.last, self.since = pointer, now
            return False
        return now - self.since >= STALL_SECONDS
