"""Manual BlueALSA A2DP receiver controller inside octet-remote.

Disabled by default: start requires the root-owned enabled local policy, an
allowlisted Paired+Trusted+Connected phone and a root peer on the local Unix
socket. There is no HTTP endpoint, pairing, trust mutation or auto-reconnect.

One owner at a time. Bluetooth refuses to start while AirPlay (journal or
CHANNEL_PROFILE cast, including unfinished rollback) or Auto Tune owns audio, and
AirPlay refuses to start while Bluetooth owns it. Any manual control is an
override that stops Bluetooth muted. Start, stop, failure and recovery never
resume MPD. The only unmute is the guarded one in _activate (at most -18 dB on a
verified live candidate with fresh PCM progress on both sub2 endpoints).

Automatic sessions (start_auto) reuse the same guarded start with a stream generation
and a cached validator, no cutoff lifetime, and are stopped only by their own generation.

Failure order is fixed: emergency mute, then confirmed termination and reaping of
the session's own bridge child, and only then DSP restore/reload. If termination
is not confirmed, the DSP is never restored or reloaded: mute, journal and flock
are retained and every tick/stop retries the reap before recovery.

DSP writer exclusion (cooperative only). In-process writers (HTTP actions,
AirPlay, Bluetooth) serialize on server.LOCK; every action first stops Bluetooth
and AirPlay refuses to start while Bluetooth owns audio. Auto Tune's DSP writes
that can unmute or reload take the shared audio.lock flock, which BluetoothRoute
holds from prepare until recovery; _activate re-verifies that lease immediately
before unmuting. CamillaDSP's websocket has no compare-and-set, so the final
read-back and SetMute(False) are still separate commands. A non-cooperative
local websocket client (or a CamillaDSP restart) can change state in between;
that is detected by the read-back on the same connection right after the
unmute (immediate re-mute) and by the 0.25 s watchdog, not prevented.
"""
from functools import partial
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import struct
import threading
import time
import uuid

from airplay_baseline import MAX_CONFIG, read_bounded
from audio_sources import meter_channels
from bluetooth_bridge import BridgeSupervisor
from bluetooth_contract import bridge_arguments
from bluetooth_identity import observe_identity, service_policy
from bluetooth_route import BluetoothRoute
from bluetooth_transport import CODECS, StreamGeneration, _TRANSPORT, _UNIQUE

SOCKET = '/run/octet-bluetooth/control.sock'
START_TIMEOUT = 120.0
LIFETIME = 1800.0
MAX_DB = -18.0


def safe_volume(value):
    return (type(value) in (int, float) and math.isfinite(value) and value <= MAX_DB)


class Session:
    def __init__(self, address, deadline, stream=None, is_current=None):
        self.generation = uuid.uuid4().hex
        self.address = address
        self.deadline = deadline
        # Automatic sessions carry the observer's StreamGeneration and its validator.
        self.mode = 'manual' if stream is None else 'auto'
        self.stream = stream
        self.is_current = is_current
        self.stop_event = threading.Event()
        self.supervisor = None
        self.thread = None
        self.expected = None
        self.candidate = None
        self.format = None
        self.active = False


class BluetoothControl:
    def __init__(self, server, stop_bridge=None, *, policy=service_policy,
                 identity=observe_identity, supervisor=BridgeSupervisor,
                 clock=time.monotonic, timeout=START_TIMEOUT, lifetime=LIFETIME,
                 interval=0.1):
        if not math.isfinite(timeout) or not 0 < timeout <= 120.0:
            raise ValueError('Bluetooth start timeout must be within 120 seconds')
        self.server = server
        self.route = BluetoothRoute(server)
        # Optional extra hook; the session's own supervisor always owns its child.
        self.stop_bridge = stop_bridge
        self.policy, self.identity, self.supervisor = policy, identity, supervisor
        self.clock, self.timeout, self.lifetime, self.interval = clock, timeout, lifetime, interval
        self.session = None
        self._thread = None
        # Supervisors whose owned child is not yet confirmed reaped. Non-empty
        # means the DSP must not be restored and Bluetooth still owns audio.
        self._bridges = []
        self.error = None
        self.errors = {}
        self.mute_verified = False

    def owns_audio(self):
        """Called under server.LOCK by AirPlay before it touches the DSP."""
        return self.session is not None or bool(self._bridges) or self.route.exists()

    def _reap(self):
        """Confirm every retained bridge child exited; keep unconfirmed ones for retry."""
        errors = []
        for supervisor in list(self._bridges):
            try:
                supervisor.stop_child()
            except Exception as error:
                errors.append(error)
            else:
                self._bridges.remove(supervisor)
        if errors:
            raise RuntimeError('Bluetooth bridge termination not confirmed: '
                               + '; '.join(map(str, errors))) from errors[0]

    def fail(self, reason):
        # Also serialize direct fail() calls. Emergency mute precedes disk/lock IO.
        with self.server.LOCK:
            session, self.session = self.session, None
            self.error = reason
            self.errors = {}
            self.mute_verified = False
            try:
                self.route.mute()
                self.mute_verified = True
            except Exception as error:
                self.errors['mute'] = error
            stops = []
            if session is not None:
                session.stop_event.set()
                if session.supervisor is not None and session.supervisor not in self._bridges:
                    self._bridges.append(session.supervisor)
            try:
                # Terminate and reap only the session's own Popen child, now.
                self._reap()
            except Exception as error:
                stops.append(error)
            if self.stop_bridge is not None:
                try:
                    self.stop_bridge()
                except Exception as error:
                    stops.append(error)
            if stops:
                self.errors['stop'] = stops[0] if len(stops) == 1 else RuntimeError(
                    '; '.join(map(str, stops)))
            try:
                if self.route.exists():
                    if 'stop' in self.errors:
                        # The bridge may still write sub2: never restore or reload
                        # while it might be alive. Candidate stays loaded muted;
                        # journal and flock are retained and tick retries.
                        raise RuntimeError('restore deferred until bridge termination '
                                           'is confirmed; mute and journal retained')
                    self.route.recover(cleanup=not self.errors)
            except Exception as error:
                self.errors['recovery'] = error
                if 'stop' not in self.errors:
                    self.mute_verified = False
            if self.errors:
                self.error = reason + '; ' + '; '.join(
                    f'{stage}: {error}' for stage, error in self.errors.items())
                raise RuntimeError(self.error) from next(iter(self.errors.values()))

    def startup(self):
        self.fail('Bluetooth startup recovery')
        self.error = None

    def stop(self, reason='Bluetooth stopped'):
        with self.server.LOCK:
            self.fail(reason)
            self.error = None

    def status(self):
        with self.server.LOCK:
            session = self.session
            return dict(active=bool(session and session.active),
                        preparing=bool(session and not session.active),
                        recovery_pending=session is None and (
                            self.route.exists() or bool(self._bridges)),
                        error=self.error)

    def _idle(self):
        """Non-mutating ownership precheck so a refused start never mutes others."""
        airplay = self.server.AIRPLAY_CONTROL
        if (airplay.active or airplay.pending is not None or airplay.baseline.exists()
                or airplay.restore_pending is not None or airplay.needs_mute
                or airplay.hook_busy.locked()):
            raise RuntimeError('AirPlay owns audio; Bluetooth start refused')
        if self.server.TUNING.status().get('audio_owned') is not False:
            raise RuntimeError('Auto Tune owns audio or ownership is unknown')
        with self.server.DSP() as dsp:
            path = dsp.call('GetConfigFilePath')
            if (path == str(self.server.CHANNEL_PROFILE) or not self.server.TUNING.active(path)
                    or dsp.call('GetState') != 'Running'):
                raise RuntimeError('Bluetooth requires the approved Auto Tune baseline to be loaded')

    def _pause_mpd(self):
        # Pause keeps queue and position; Bluetooth never resumes MPD.
        self.server.mpd('pause 1')
        state = dict(self.server.mpd('status')[0]).get('state')
        if state not in ('pause', 'stop'):
            raise RuntimeError('Music player did not pause; Bluetooth start refused')

    def _check_generation(self, generation, is_current):
        """Shape-check a StreamGeneration before any policy, bus or DSP access."""
        if not isinstance(generation, StreamGeneration):
            raise TypeError('Automatic Bluetooth start requires a stream generation')
        if not callable(is_current):
            raise TypeError('Automatic Bluetooth start requires a generation validator')
        owners = (generation.bluez_owner, generation.bluealsa_owner)
        match = (_TRANSPORT.fullmatch(generation.transport_path)
                 if isinstance(generation.transport_path, str) else None)
        if (not all(isinstance(owner, str) and _UNIQUE.fullmatch(owner) for owner in owners)
                or match is None or generation.device_path != match[1]
                or generation.address != match[2].replace('_', ':')
                or type(generation.codec) is not int or generation.codec not in CODECS
                or type(generation.counter) is not int or generation.counter < 1):
            raise ValueError('Malformed Bluetooth stream generation')

    def start_auto(self, generation, is_current):
        """Start for a validated stream; the phone address comes only from the generation.

        is_current(generation) must be a fast, thread-safe read of cached observer
        state and must never call D-Bus: the controller and bridge workers call it.
        """
        self._check_generation(generation, is_current)
        return self._start(generation.address, generation, is_current)

    @staticmethod
    def _current(session, stream=None, is_current=None):
        """False once an automatic stream is stale or unreadable; manual has none."""
        if session is not None:
            stream, is_current = session.stream, session.is_current
        if stream is None:
            return True
        try:
            return is_current(stream) is True
        except Exception:
            return False

    def stop_auto(self, generation, reason='Bluetooth stream ended'):
        """Stop only the automatic session of exactly this generation, muted."""
        with self.server.LOCK:
            session = self.session
            if session is not None and session.stream == generation:
                self.stop(reason)
            return self.status()

    def start(self, address):
        return self._start(address)

    def _start(self, address, stream=None, is_current=None):
        deadline = self.clock() + self.timeout
        if self.session is not None or self._bridges or self.route.exists():
            raise RuntimeError('Bluetooth session or recovery already pending')
        try:
            allowed = self.policy()
        except (OSError, ValueError, RuntimeError) as error:
            # Missing, unprotected or disabled policy: disabled by default. The cause
            # separates "disabled" from "octet-remote cannot read it" (e.g. not yet
            # restarted into the octet-bluetooth group after install).
            raise RuntimeError(f'Bluetooth is disabled: no enabled local policy ({error})') from error
        address = bridge_arguments(address, allowed)[-1]
        # Bus reads outside LOCK: a disconnected or untrusted phone is refused before
        # anyone else's audio is muted. The supervisor re-verifies continuously.
        self.identity(address, allowed)
        with self.server.LOCK:
            if self.session is not None or self._bridges or self.route.exists():
                raise RuntimeError('Bluetooth session or recovery already pending')
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError('Previous Bluetooth bridge is still stopping')
            if stream is not None and not self._current(None, stream, is_current):
                raise RuntimeError('Bluetooth stream generation is not current')
            self._idle()
            if stream is not None:
                # Automatic playback never takes over a playing or paused queue.
                # A playing or paused queue is paused (position kept) and the phone takes over.
                if dict(self.server.mpd('status')[0]).get('state') not in ('stop', 'pause', 'play'):
                    raise RuntimeError('Music player state is unknown; automatic Bluetooth refused')
            session = Session(address, deadline, stream, is_current)
            self.session = session
            self.error = None
            try:
                self.route.mute()
                self._pause_mpd()
                session.supervisor = self.supervisor(
                    partial(self._exited, session), prepare=partial(self._prepare, session),
                    policy=self.policy, identity=self.identity, deadline=deadline,
                    startup_timeout=self.timeout,
                    lifetime=self.lifetime if stream is None else None)
                session.thread = threading.Thread(target=self._run, args=(session,),
                                                  name='octet-bluetooth', daemon=True)
                self._thread = session.thread
                session.thread.start()
            except Exception as error:
                if self.session is session:
                    self.fail(f'Bluetooth start failed: {error}')
                raise
        # Wait outside LOCK: preparation and exit callbacks need it.
        while True:
            with self.server.LOCK:
                if self.session is not session:
                    raise RuntimeError(self.error or 'Bluetooth session ended before playback')
                try:
                    if self.clock() >= deadline:
                        raise RuntimeError('Bluetooth start deadline; output remains muted')
                    if self._activate(session):
                        return self.status()
                except Exception as error:
                    self.fail(str(error))
                    raise
            session.stop_event.wait(self.interval)

    def _run(self, session):
        try:
            session.supervisor.run(session.address, session.stop_event)
        except Exception as error:
            # Client identifiers are deliberately not logged.
            logging.warning('Bluetooth bridge ended: %s', error)

    def _exited(self, session, reason):
        """Supervisor fail-closed callback; stale sessions were already recovered."""
        with self.server.LOCK:
            if self.session is session:
                self.fail(reason)

    def _prepare(self, session, rate, sample_format):
        with self.server.LOCK:
            if self.session is not session or session.stop_event.is_set():
                raise RuntimeError('Stale Bluetooth session')
            if self.clock() >= session.deadline:
                raise RuntimeError('Bluetooth start deadline before route preparation')
            expected = self.route.prepare(rate, sample_format)
            session.candidate = read_bounded(self.route.candidate, MAX_CONFIG)
            session.expected = expected
            session.format = (rate, sample_format, 2)

    def _streaming(self, session):
        supervisor = session.supervisor
        return (session.thread is not None and session.thread.is_alive()
                and supervisor.ready and supervisor.negotiated == session.format
                and supervisor.progress_age() < supervisor.stall_timeout)

    def _verify(self, session, *, muted, dsp=None, volume=False):
        """Exact live candidate, approvals, audio.lock lease and mute; never mutates."""
        if session.expected is None:
            raise RuntimeError('Bluetooth route is not prepared')
        # ownership() re-checks that our audio.lock flock is still held on the
        # current inode, excluding cooperative Auto Tune DSP writers.
        self.route.ownership()
        self.route.original()
        if read_bounded(self.route.candidate, MAX_CONFIG) != session.candidate:
            raise RuntimeError('Bluetooth candidate changed')
        if dsp is None:
            with self.server.DSP() as dsp:
                return self._verify_live(session, dsp, muted, volume)
        return self._verify_live(session, dsp, muted, volume)

    def _verify_live(self, session, dsp, muted, volume):
        if (dsp.call('GetConfigFilePath') != str(self.route.candidate)
                or json.loads(dsp.call('GetConfigJson')) != session.expected
                or dsp.call('GetState') != 'Running'
                or dsp.call('GetMute') is not muted):
            raise RuntimeError('Bluetooth live DSP route drifted')
        if (volume or not muted) and not safe_volume(dsp.call('GetVolume')):
            raise RuntimeError('Bluetooth output exceeded -18 dB')

    def _activate(self, session):
        """The only unmute. Any exception fails closed (the caller calls fail()).

        Holds server.LOCK itself (re-entrant) so no in-process writer interleaves;
        the audio.lock lease is re-verified inside the final _verify. This is
        mutual exclusion with cooperative writers only, not an atomic DSP
        transaction: see the module docstring.
        """
        with self.server.LOCK:
            if session.active:
                return True
            if session.expected is None or not self._streaming(session):
                return False
            self._verify(session, muted=True)
            with self.server.DSP() as dsp:
                if not safe_volume(dsp.call('GetVolume')):
                    dsp.call('SetVolume', MAX_DB)
                if not safe_volume(dsp.call('GetVolume')):
                    raise RuntimeError('Bluetooth startup volume could not be limited to -18 dB')
            # Final checks, unmute and read-back share one connection to keep the
            # unavoidable read-to-write window as short as the protocol allows.
            with self.server.DSP() as dsp:
                # Re-read volume too: the clamp above was a separate command.
                self._verify(session, muted=True, dsp=dsp, volume=True)
                if not self._streaming(session) or self.clock() >= session.deadline:
                    raise RuntimeError('Bluetooth PCM progress or start deadline lost before unmute')
                if not self._current(session):
                    raise RuntimeError('Bluetooth stream generation lost before unmute')
                self.server.ACTIVE_CAPTURE_CHANNELS = meter_channels(session.expected)
                session.active = True
                try:
                    dsp.call('SetMute', False)
                    self._verify(session, muted=False, dsp=dsp)
                except BaseException:
                    try:
                        # Immediate re-mute on the same connection; fail() follows.
                        dsp.call('SetMute', True)
                    except Exception:
                        pass
                    raise
            return True

    def tick(self):
        """Watchdog for drift, stalls, ownership, deadlines and orphaned recovery."""
        with self.server.LOCK:
            session = self.session
            if session is None:
                if self._bridges or self.route.exists():
                    # Retries the bridge reap first; restore only once confirmed.
                    self.fail('Bluetooth recovery without an owning session')
                return
            try:
                if session.thread is None or not session.thread.is_alive():
                    raise RuntimeError('Bluetooth bridge supervisor exited')
                if not self._current(session):
                    raise RuntimeError('Bluetooth stream generation lost')
                if session.active:
                    if not self._streaming(session):
                        raise RuntimeError('Bluetooth PCM stalled or bridge not ready')
                    self._verify(session, muted=False)
                else:
                    if self.clock() >= session.deadline:
                        raise RuntimeError('Bluetooth start deadline; output remains muted')
                    if session.expected is not None:
                        self._verify(session, muted=True)
            except Exception as error:
                self.fail(str(error))


START = re.compile(rb'start ((?:[0-9A-F]{2}:){5}[0-9A-F]{2})\n')


def handle_connection(connection, controller):
    """Exact one-line commands from a root peer: start <MAC>, stop or status."""
    try:
        _, uid, _ = struct.unpack('3i', connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        if uid != 0:
            raise PermissionError('Bluetooth control requires the local administrator')
        connection.settimeout(2)
        with connection.makefile('rb') as stream:
            line = stream.readline(32)
        match = START.fullmatch(line)
        if line == b'status\n':
            result = controller.status()
        elif line == b'stop\n':
            controller.stop()
            result = controller.status()
        elif match:
            connection.settimeout(None)
            result = controller.start(match[1].decode())
        else:
            raise ValueError('Invalid Bluetooth command')
        reply = b'OK ' + json.dumps(result).encode()
    except Exception as error:
        message = (str(error) if isinstance(error, (RuntimeError, ValueError, PermissionError))
                   else 'Bluetooth control failed')
        logging.warning('Bluetooth control request rejected or failed')
        reply = b'ERROR ' + json.dumps(message).encode()
    try:
        connection.sendall(reply + b'\n')
    except OSError:
        pass
    finally:
        connection.close()


def monitor(controller, interval=0.25):
    while True:
        try:
            controller.tick()
        except Exception:
            logging.exception('Bluetooth monitor could not establish mute/recovery')
        time.sleep(interval)


def serve(controller, path=SOCKET):
    """Owner-only socket in the service RuntimeDirectory; root peers only."""
    path = Path(path)
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
    except BaseException:
        listener.close()
        path.unlink(missing_ok=True)
        raise
    listener.listen(4)
    threading.Thread(target=monitor, args=(controller,), daemon=True).start()
    slots = threading.BoundedSemaphore(4)
    while True:
        connection, _ = listener.accept()
        if not slots.acquire(blocking=False):
            connection.close()
            continue
        def handle(connection=connection):
            try:
                handle_connection(connection, controller)
            finally:
                slots.release()
        threading.Thread(target=handle, daemon=True).start()
