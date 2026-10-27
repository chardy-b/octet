"""Standalone WIL-249 bridge supervision; deliberately not wired to activation.

run() blocks and owns its child through cleanup. The caller must already have
established the muted route and supply a fail-closed callback (mute/recovery),
which runs before child shutdown on EVERY exit. Nothing here unmutes, loads DSP,
changes services or enables BluetoothControl.start. Readiness is observation only.
Bus/proc reads are sequential observations, not an atomic identity/audio lease.
"""
from dataclasses import dataclass
import math
from pathlib import Path
import re
import subprocess
import threading
import time

from bluetooth_contract import bridge_arguments
from bluetooth_identity import local_policy, verify_identity


@dataclass(frozen=True)
class PCM:
    rate: int
    sample_format: str
    channels: int
    owner: int
    hw_ptr: int
    appl_ptr: int

    @property
    def format(self):
        return self.rate, self.sample_format, self.channels


def _fields(raw):
    if len(raw) > 8192:
        raise ValueError('Oversized PCM observation')
    result = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if ':' not in line:
            raise ValueError('Malformed PCM observation')
        key, value = (part.strip() for part in line.split(':', 1))
        if not key or not value or key in result:
            raise ValueError('Invalid or duplicate PCM field')
        result[key] = value
    return result


def _integer(value):
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('Invalid PCM integer')
    return int(value)


def parse_pcm(hw_params, status):
    """Return None only for a fully closed endpoint during bounded startup."""
    if hw_params.strip() == status.strip() == 'closed':
        return None
    hw, state = _fields(hw_params), _fields(status)
    rate = re.fullmatch(r'(44100|48000)(?: \((44100|48000)/1\))?', hw['rate'])
    if (not rate or (rate[2] and rate[1] != rate[2])
            or hw['format'] not in ('S16_LE', 'S32_LE') or hw['channels'] != '2'):
        raise ValueError('Unsupported native PCM format')
    if state['state'] != 'RUNNING':
        raise ValueError('PCM is not RUNNING')
    owner = _integer(state['owner_pid'])
    if owner <= 0:
        raise ValueError('Invalid PCM owner')
    return PCM(int(rate[1]), hw['format'], 2, owner,
               _integer(state['hw_ptr']), _integer(state['appl_ptr']))


def read_endpoints():
    """Read only the real snd_aloop card and its two dedicated sub2 endpoints."""
    root = Path('/proc/asound')
    card = (root / 'Loopback').resolve(strict=True)
    if card.parent != root or not re.fullmatch(r'card[0-9]+', card.name):
        raise ValueError('Invalid Loopback card')
    index = str(int(card.name[4:]))
    if (card / 'id').read_text().strip() != 'Loopback':
        raise ValueError('Invalid Loopback identity')
    rows = [line.split() for line in (root / 'modules').read_text().splitlines()]
    if [row for row in rows if row and row[0] == index] != [[index, 'snd_aloop']]:
        raise ValueError('Loopback is not snd_aloop')
    result = []
    for endpoint, device, direction in [('pcm0p', '0', 'PLAYBACK'), ('pcm1c', '1', 'CAPTURE')]:
        sub = card / endpoint / 'sub2'
        values = {}
        for name in ('info', 'hw_params', 'status'):
            path = sub / name
            if path.resolve(strict=True) != path:
                raise ValueError('Redirected sub2 observation')
            with path.open() as stream:
                values[name] = stream.read(8193)
        info = _fields(values['info'])
        if any(info.get(k) != v for k, v in dict(
                card=index, device=device, subdevice='2', stream=direction).items()):
            raise ValueError('Incorrect sub2 endpoint')
        result.append(parse_pcm(values['hw_params'], values['status']))
    return tuple(result)


def bus_owner(name):
    result = subprocess.run(
        ['/usr/bin/busctl', '--system', '--no-pager', '--timeout=2s',
         'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus',
         'org.freedesktop.DBus', 'GetNameOwner', 's', name],
        capture_output=True, timeout=3, shell=False)
    if (result.returncode != 0 or result.stderr != b''
            or not re.fullmatch(rb's ":[0-9]+\.[0-9]+"\n', result.stdout)):
        raise RuntimeError('Bus owner unavailable: ' + name)
    return result.stdout


class BridgeSupervisor:
    """Single-use, bounded blocking monitor. stop_event requests graceful shutdown.

    Injection points are for offline tests/trusted embedding, never request data.
    ready and negotiated are invalidated before any exit callback. No callback
    receives playback authorization. The callback must itself be bounded; Python
    cannot impose a safe timeout on arbitrary in-process recovery code.
    """
    def __init__(self, fail_closed, *, policy=local_policy, identity=verify_identity,
                 owner=bus_owner, observe=read_endpoints, spawn=subprocess.Popen,
                 clock=time.monotonic, interval=0.1, startup_timeout=5.0,
                 stall_timeout=2.0, lifetime=300.0, stop_timeout=2.0):
        if not callable(fail_closed):
            raise ValueError('Fail-closed callback required')
        for value in (interval, startup_timeout, stall_timeout, lifetime, stop_timeout):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('Deadlines must be finite and positive')
        self.fail_closed = fail_closed
        self.policy, self.identity, self.owner = policy, identity, owner
        self.observe, self.spawn, self.clock = observe, spawn, clock
        self.interval, self.startup_timeout = interval, startup_timeout
        self.stall_timeout, self.lifetime, self.stop_timeout = stall_timeout, lifetime, stop_timeout
        self.ready = False
        self.negotiated = None
        self._child = None
        self._lock = threading.Lock()
        self._used = False

    @property
    def pid(self):
        return self._child.pid if self._child is not None else None

    def _stop(self):
        child = self._child
        if child is None:
            return
        # Popen retains ownership/reaps the child. Never signal a stored raw PID,
        # a process group, another instance, or a name-matched system process.
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
                child.wait(timeout=self.stop_timeout)
        else:
            child.wait(timeout=self.stop_timeout)
        self._child = None

    def run(self, address, stop_event):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Supervisor already running')
        try:
            if self._used:
                raise RuntimeError('Supervisor is single-use')
            self._used = True
            self._run(address, stop_event)
        finally:
            self._lock.release()

    def _run(self, address, stop_event):
        error = None
        reason = 'Bridge stopped'
        try:
            start = self.clock()
            argv = bridge_arguments(address, self.policy())
            address = argv[-1]
            owners = tuple(self.owner(name) for name in ('org.bluez', 'org.bluealsa'))

            def verify():
                allowed = self.policy()
                bridge_arguments(address, allowed)
                before = tuple(self.owner(name) for name in ('org.bluez', 'org.bluealsa'))
                if before != owners:
                    raise RuntimeError('Bluetooth bus owner changed')
                if self.identity(address, allowed) != address:
                    raise RuntimeError('Bluetooth identity changed')
                after = tuple(self.owner(name) for name in ('org.bluez', 'org.bluealsa'))
                if after != owners:
                    raise RuntimeError('Bluetooth bus owner changed during identity check')

            verify()
            if self.clock() - start >= min(self.startup_timeout, self.lifetime):
                raise RuntimeError('Bridge startup deadline')
            if stop_event.is_set():
                return
            self._child = self.spawn(argv, executable='/usr/bin/bluealsa-aplay',
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, shell=False, close_fds=True)
            previous = None
            progress = None
            while not stop_event.is_set():
                verify()
                if self._child.poll() is not None:
                    raise RuntimeError('Bridge process exited')
                pair = self.observe()
                now = self.clock()
                if now - start >= self.lifetime:
                    raise RuntimeError('Bridge lifetime deadline')
                if not self.ready and now - start >= self.startup_timeout:
                    raise RuntimeError('Bridge startup deadline')
                if any(pcm is None for pcm in pair):
                    if previous is not None:
                        raise RuntimeError('PCM endpoint closed')
                else:
                    playback, capture = pair
                    if playback.owner != self._child.pid:
                        raise RuntimeError('Playback is not owned by bridge')
                    if playback.format != capture.format:
                        raise RuntimeError('PCM endpoint format mismatch')
                    pointers = tuple(n for pcm in pair for n in (pcm.hw_ptr, pcm.appl_ptr))
                    if previous is None:
                        progress = [now] * 4
                    else:
                        if any(pcm.format != old.format or pcm.owner != old.owner
                               for pcm, old in zip(pair, previous)):
                            raise RuntimeError('PCM negotiation or owner changed')
                        old_pointers = tuple(n for pcm in previous for n in (pcm.hw_ptr, pcm.appl_ptr))
                        # Unknown wrap/reset is rejected, never treated as progress.
                        for i, (new, old) in enumerate(zip(pointers, old_pointers)):
                            if new < old:
                                raise RuntimeError('PCM pointer regressed')
                            if now - progress[i] >= self.stall_timeout:
                                raise RuntimeError('PCM pointer stalled or observation late')
                            if new > old:
                                progress[i] = now
                        if all(t > start_progress for t in progress):
                            self.ready = True
                            self.negotiated = playback.format
                    if previous is None:
                        start_progress = now
                    previous = pair
                stop_event.wait(self.interval)
        except BaseException as exc:
            error = exc
            reason = str(exc)
        finally:
            self.ready, self.negotiated = False, None
            failures = []
            try:
                self.fail_closed(reason)
            except BaseException as exc:
                failures.append(('fail-closed callback', exc))
            try:
                self._stop()
            except BaseException as exc:
                failures.append(('owned bridge stop', exc))
            if failures:
                raise RuntimeError(reason + '; ' + '; '.join(
                    f'{stage}: {exc}' for stage, exc in failures)) from (error or failures[0][1])
        if error is not None:
            raise error
