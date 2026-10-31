"""WIL-249 bridge supervision, driven by BluetoothControl for one manual session.

run() blocks and owns its child through cleanup. The caller must already have
established mute and supply a fail-closed callback, which runs on EVERY exit
before the supervisor's own child shutdown. That callback is the emergency mute:
it must not restore or reload the DSP unless stop_child() has first confirmed
the owned child terminated and was reaped (BluetoothControl.fail does exactly
mute -> stop_child() -> restore). stop_child() is idempotent, may be called from
any thread including the callback, and forbids any later spawn. A failed reap
keeps the Popen so the caller can retry. The supervisor never unmutes or
changes services; BluetoothControl alone performs the guarded unmute after
ready, matching negotiation and fresh progress_age(). The optional prepare callback
loads a muted route on the first valid playback observation, before capture is
required. Without it the caller must have prepared the route already.
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
from bluetooth_identity import paired_addresses, verify_identity


@dataclass(frozen=True)
class PCM:
    rate: int
    sample_format: str
    channels: int
    owner: int
    hw_ptr: int
    appl_ptr: int
    state: str = 'RUNNING'

    @property
    def format(self):
        return self.rate, self.sample_format, self.channels


def _fields(raw, *, status=False):
    if len(raw) > 8192:
        raise ValueError('Oversized PCM observation')
    result = {}
    separator_seen = False
    after_separator = False
    for line in raw.splitlines():
        if not line.strip():
            continue
        if status and line == '-----':
            if separator_seen or not result or next(reversed(result)) != 'avail_max':
                raise ValueError('Invalid PCM status separator')
            separator_seen = after_separator = True
            continue
        if ':' not in line:
            raise ValueError('Malformed PCM observation')
        key, value = (part.strip() for part in line.split(':', 1))
        if after_separator and key != 'hw_ptr':
            raise ValueError('Invalid PCM status separator')
        after_separator = False
        if not key or not value or key in result:
            raise ValueError('Invalid or duplicate PCM field')
        result[key] = value
    if after_separator:
        raise ValueError('Incomplete PCM status separator')
    return result


def _integer(value):
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('Invalid PCM integer')
    return int(value)


def parse_pcm(hw_params, status):
    """Return None only for a fully closed endpoint during bounded startup.

    PREPARED is reported, not accepted: only the supervisor may tolerate it,
    and only for playback before it has ever been observed RUNNING.
    """
    if hw_params.strip() == status.strip() == 'closed':
        return None
    hw, state = _fields(hw_params), _fields(status, status=True)
    rate = re.fullmatch(r'(44100|48000)(?: \((44100|48000)/1\))?', hw['rate'])
    if (not rate or (rate[2] and rate[1] != rate[2])
            or hw['format'] not in ('S16_LE', 'S32_LE') or hw['channels'] != '2'):
        raise ValueError('Unsupported native PCM format')
    if state['state'] not in ('RUNNING', 'PREPARED'):
        raise ValueError('PCM is not RUNNING')
    owner = _integer(state['owner_pid'])
    if owner <= 0:
        raise ValueError('Invalid PCM owner')
    return PCM(int(rate[1]), hw['format'], 2, owner,
               _integer(state['hw_ptr']), _integer(state['appl_ptr']), state['state'])


def verify_task(pid, tid, *, root=Path('/proc')):
    """Require ALSA owner tid to be a live task of thread group pid.

    bluealsa-aplay opens the PCM from an IO worker thread, so owner_pid is that
    task's ID, not the Popen PID. Both views must agree: the task's own status
    names pid as its Tgid, and pid's task directory lists tid. The caller must
    hold pid unreaped (Popen) so it cannot be reused during the check.
    """
    try:
        if type(pid) is not int or type(tid) is not int or pid <= 0 or tid <= 0:
            raise ValueError('Invalid task ID')
        fields = {}
        for line in (root / str(tid) / 'status').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('Tgid', 'Pid'):
                if key in fields:
                    raise ValueError('Duplicate task status field')
                fields[key] = _integer(value.strip())
        if fields.get('Pid') != tid or fields.get('Tgid') != pid:
            raise ValueError('Task is not in bridge thread group')
        if not (root / str(pid) / 'task' / str(tid)).is_dir():
            raise ValueError('Task is not listed by bridge process')
    except (OSError, ValueError) as exc:
        raise RuntimeError('Playback is not owned by bridge') from exc


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

    Phone startup may wait up to 120 seconds on the already-muted route. This
    window never grants playback authorization or extends the lifetime deadline.
    Injection points are for offline tests/trusted embedding, never request data.
    ready and negotiated are invalidated before any exit callback. No callback
    receives playback authorization. The callback must itself be bounded; Python
    cannot impose a safe timeout on arbitrary in-process recovery code.
    """
    def __init__(self, fail_closed, *, prepare=None, policy=paired_addresses, identity=verify_identity,
                 owner=bus_owner, observe=read_endpoints, spawn=subprocess.Popen, task=verify_task,
                 clock=time.monotonic, interval=0.1, startup_timeout=120.0,
                 stall_timeout=2.0, lifetime=300.0, stop_timeout=2.0, deadline=None):
        if not callable(fail_closed):
            raise ValueError('Fail-closed callback required')
        if prepare is not None and not callable(prepare):
            raise ValueError('Prepare callback must be callable')
        self.prepare = prepare
        # lifetime=None is an explicit opt-in for callers with their own watchdog.
        for value in (interval, startup_timeout, stall_timeout, stop_timeout,
                      *(() if lifetime is None else (lifetime,))):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('Deadlines must be finite and positive')
        if startup_timeout > 120.0:
            raise ValueError('Phone startup window cannot exceed 120 seconds')
        if deadline is not None and (type(deadline) not in (int, float)
                                     or not math.isfinite(deadline)):
            raise ValueError('Operation deadline must be a finite clock value')
        # Absolute clock value shared with the caller's whole start operation.
        self.deadline = deadline
        self.fail_closed = fail_closed
        self.policy, self.identity, self.owner = policy, identity, owner
        self.observe, self.spawn, self.clock, self.task = observe, spawn, clock, task
        self.interval, self.startup_timeout = interval, startup_timeout
        self.stall_timeout, self.lifetime, self.stop_timeout = stall_timeout, lifetime, stop_timeout
        self.ready = False
        self.negotiated = None
        self._progress = None
        self._child = None
        # Serializes spawn against stop_child(); _closed forbids any later spawn.
        self._child_lock = threading.Lock()
        self._closed = False
        self._killed = False
        self._lock = threading.Lock()
        self._used = False

    def progress_age(self):
        """Seconds since the stalest of the four pointers last advanced; inf unless ready."""
        progress = self._progress
        if not self.ready or progress is None:
            return math.inf
        return self.clock() - min(progress)

    @property
    def pid(self):
        child = self._child
        return child.pid if child is not None else None

    @property
    def stopped(self):
        """True only once no child can run: closed and any spawned child reaped."""
        return self._closed and self._child is None

    def stop_child(self):
        """Terminate and reap only our own Popen child; raises if not confirmed.

        Popen retains ownership/reaps the child. Never signal a stored raw PID,
        a process group, another instance, or a name-matched system process.
        On failure the Popen is kept, so a later call retries the same child.
        """
        with self._child_lock:
            self._closed = True
            child = self._child
            if child is None:
                return
            if self._killed:
                # Retry after SIGKILL: never block the caller (which may hold
                # server.LOCK) on a child that already failed to exit in time.
                if child.poll() is None:
                    raise RuntimeError('Owned bridge child has not exited after SIGKILL')
            elif child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    self._killed = True
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
            startup_deadline = start + self.startup_timeout
            if self.deadline is not None:
                startup_deadline = min(startup_deadline, self.deadline)
            if self.lifetime is not None:
                startup_deadline = min(startup_deadline, start + self.lifetime)
            if start >= startup_deadline:
                raise RuntimeError('Bridge startup deadline')
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
            if self.clock() >= startup_deadline:
                raise RuntimeError('Bridge startup deadline')
            with self._child_lock:
                # A concurrent stop_child() must never be followed by a spawn.
                if stop_event.is_set() or self._closed:
                    return
                child = self._child = self.spawn(
                    argv, executable='/usr/bin/bluealsa-aplay', stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    shell=False, close_fds=True)
            prepared = None
            previous = None
            playback_owner = None
            running_seen = False
            progress = None
            while not stop_event.is_set():
                verify()
                if child.poll() is not None:
                    raise RuntimeError('Bridge process exited')
                pair = self.observe()
                now = self.clock()
                if self.lifetime is not None and now - start >= self.lifetime:
                    raise RuntimeError('Bridge lifetime deadline')
                if not self.ready and now >= startup_deadline:
                    raise RuntimeError('Bridge startup deadline')
                playback, capture = pair
                if capture is not None and capture.state != 'RUNNING':
                    raise RuntimeError('Capture PCM is not RUNNING')
                if playback is not None:
                    if playback_owner is None:
                        playback_owner = playback.owner
                    elif playback.owner != playback_owner:
                        raise RuntimeError('Playback owner changed')
                    # owner_pid is bluealsa-aplay's IO worker task, not the Popen PID.
                    self.task(child.pid, playback.owner)
                    if child.poll() is not None:
                        raise RuntimeError('Bridge process exited during owner check')
                    if (playback.rate not in (44100, 48000)
                            or playback.sample_format not in ('S16_LE', 'S32_LE')
                            or playback.channels != 2):
                        raise RuntimeError('Unsupported native PCM format')
                    if playback.state != 'RUNNING':
                        if running_seen:
                            raise RuntimeError('Playback PCM is not RUNNING')
                        # Transient startup PREPARED: never prepares, never progress;
                        # the startup deadline above still bounds it.
                        stop_event.wait(self.interval)
                        continue
                    running_seen = True
                    if prepared is None:
                        prepared = playback.format
                        if self.prepare is not None:
                            self.prepare(playback.rate, playback.sample_format)
                            # Discard observations made before the route reload.
                            verify()
                            if child.poll() is not None:
                                raise RuntimeError('Bridge process exited during preparation')
                            if self.clock() >= startup_deadline:
                                raise RuntimeError('Bridge startup deadline during preparation')
                            continue
                    elif playback.format != prepared:
                        raise RuntimeError('PCM negotiation changed after preparation')
                elif prepared is not None:
                    raise RuntimeError('PCM endpoint closed')
                if any(pcm is None for pcm in pair):
                    if previous is not None:
                        raise RuntimeError('PCM endpoint closed')
                else:
                    playback, capture = pair
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
                        self._progress = tuple(progress)
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
            self.ready, self.negotiated, self._progress = False, None, None
            failures = []
            try:
                # Emergency mute first; the callback may itself call stop_child().
                self.fail_closed(reason)
            except BaseException as exc:
                failures.append(('fail-closed callback', exc))
            try:
                self.stop_child()
            except BaseException as exc:
                failures.append(('owned bridge stop', exc))
            if failures:
                raise RuntimeError(reason + '; ' + '; '.join(
                    f'{stage}: {exc}' for stage, exc in failures)) from (error or failures[0][1])
        if error is not None:
            raise error
