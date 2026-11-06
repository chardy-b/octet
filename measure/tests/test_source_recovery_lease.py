from pathlib import Path

import pytest

from octetmeasure.autotune.backend import exclusive


def test_pending_input_recovery_blocks_tuner_even_after_remote_restart(tmp_path):
    lock = tmp_path / 'audio.lock'
    journal = tmp_path / 'source-recovery.json'
    journal.write_text('{}')
    with pytest.raises(RuntimeError, match='input change is still recovering'):
        with exclusive(lock, recovery_journal=journal):
            pytest.fail('Tuner must not enter a pending input restore')
    # Refusal releases the lease so the remote can complete recovery.
    journal.unlink()
    with exclusive(lock, recovery_journal=journal):
        assert lock.exists()
