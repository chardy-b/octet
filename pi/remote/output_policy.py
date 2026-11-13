"""Octet has no master-mute state. Native Camilla faders remain an implementation detail.

Reconcile on every connection, including the watchdog's connections, to clear
persisted state and writes from legacy/native clients. Never change channel masks.
"""
import logging
import os
from pathlib import Path
import stat
import tempfile


def reconcile(dsp):
    if dsp.call('GetMute') is True:
        logging.warning('Clearing obsolete native master mute (client=%s)', type(dsp).__module__)
        dsp.call('SetMute', False)
        if dsp.call('GetMute') is not False:
            raise RuntimeError('CamillaDSP retained an obsolete master mute')


def stop(dsp, reason):
    """An output fault is an explicit processing stop, never a mute latch."""
    dsp.call('Stop')
    logging.info('DSP processing stopped: %s', reason)


def clear_saved_mute(path):
    """Before Camilla starts, discard obsolete fader latches without changing gain/config."""
    import yaml
    path = Path(path)
    with path.open('rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise RuntimeError('DSP state must be a regular file')
        state = yaml.safe_load(stream) or {}
    state['mute'] = [False] * 5
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.output-policy-', delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(yaml.safe_dump(state, sort_keys=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    clear_saved_mute('/opt/camilladsp/statefile.yml')
