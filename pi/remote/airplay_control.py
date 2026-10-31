"""Local Shairport lifecycle IPC. No client identity or metadata is accepted."""
import json
import grp
import logging
import os
from pathlib import Path
import pwd
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

from airplay_baseline import BaselineRoute
from audio_sources import loopback_available, source_metadata, meter_channels, pcm_fields, pcm_params_match

SOCKET = '/run/octet-airplay/control.sock'
PREPARATION_TIMEOUT = 20.0
PLAYBACK = Path('/proc/asound/Loopback/pcm0p/sub1/status')


CAPTURE = Path('/proc/asound/Loopback/pcm1c/sub1/status')


def pcm_running():
    """Require matching, running endpoints and actual capture consumption."""
    def sample():
        states = []
        for status in (PLAYBACK, CAPTURE):
            fields = {k.strip(): v.strip() for k, v in pcm_fields(status).items()}
            if fields.get('state') != 'RUNNING' or not pcm_params_match(status.with_name('hw_params')):
                raise ValueError('PCM is not ready')
            states.append(fields)
        capture = states[1]
        return capture['owner_pid'], int(capture['appl_ptr']), int(capture['hw_ptr'])
    try:
        before = sample()
        time.sleep(.05)
        after = sample()
        return before[0] == after[0] and after[1] > before[1] and after[2] > before[2]
    except (OSError, ValueError, KeyError):
        return False


def receiver_running():
    return subprocess.run(['systemctl', 'is-active', '--quiet', 'shairport-sync'],
                          timeout=2, capture_output=True).returncode == 0


def matches(actual, expected):
    """Camilla adds defaults; require all requested values, including protections."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and matches(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches(a, b) for a, b in zip(actual, expected))
    return actual == expected


class AirPlayController:
    def __init__(self, server):
        self.server = server
        self.baseline = BaselineRoute(server)
        self.active = False
        self.pending = None
        self.stop_at = None
        self.error = None
        self.previous = None
        self.expected = None
        self.silent_since = None
        self.suppressed = False
        self.needs_mute = False
        self.restore_pending = None
        self.cancelled = None
        self.acknowledged = None
        self.hook_busy = threading.Lock()

    def mute(self):
        with self.server.DSP() as dsp:
            dsp.call('SetMute', True)

    def invalidate(self, manual=True):
        # Called under the shared control lock for explicit user ownership changes.
        if manual and self.baseline.exists():
            # Finish recovery before permitting a manual mutation/Auto Tune request.
            self.active = False
            self.pending = None
            self.needs_mute = True
            self.recover()
        self.suppressed = manual
        if manual:
            self.error = None
            self.restore_pending = None
        self.active = False
        self.pending = self.stop_at = self.previous = self.expected = None
        self.silent_since = None

    def fail(self, message, rollback=True):
        previous = self.previous
        self.invalidate(manual=False)
        self.error = message
        logging.warning('AirPlay: %s', message)
        self.needs_mute = True
        if rollback and previous is not None:
            self.restore_pending = previous
        self.recover()

    def recover(self):
        self.needs_mute = True
        self.mute()
        if self.baseline.exists():
            self.baseline.recover()
        if self.restore_pending is not None:
            path = self.server.CHANNEL_PROFILE
            candidate = None
            try:
                # Never truncate the live profile, even when retrying a restore.
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.rollback-',
                                                 delete=False) as stream:
                    candidate = Path(stream.name)
                    stream.write(self.restore_pending)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o640)
                    os.fsync(stream.fileno())
                candidate.replace(path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                with self.server.DSP() as dsp:
                    try:
                        dsp.call('Reload')
                    finally:
                        dsp.call('SetMute', True)
                self.restore_pending = None
            finally:
                if candidate is not None:
                    candidate.unlink(missing_ok=True)
        self.needs_mute = False

    def switch_profile(self, profile):
        """Carry the cast onto another saved profile without interrupting it."""
        with self.server.LOCK:
            try:
                self.expected = self.baseline.retarget(profile)
            except Exception as error:
                self.fail(str(error) if isinstance(error, RuntimeError) else 'AirPlay profile change failed')
                raise

    def end(self):
        self.invalidate(manual=False)
        self.error = None
        self.needs_mute = True
        self.recover()

    def event(self, event, now=None, cancelled=None, acknowledged=None):
        supplied_now = now
        now = time.monotonic() if now is None else now
        with self.server.LOCK:
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError('AirPlay preparation timed out')
            if self.needs_mute or (self.baseline.exists() and not self.active and self.pending is None):
                self.recover()
            if event == 'stop':
                if self.active or self.pending is not None:
                    self.stop_at = now + 1.0
                return
            if event != 'start':
                raise ValueError('Invalid AirPlay event')
            bluetooth = getattr(self.server, 'BLUETOOTH_CONTROL', None)
            if bluetooth is not None and bluetooth.owns_audio():
                # First owner wins: refuse before muting so Bluetooth is undisturbed.
                self.error = 'Bluetooth owns audio; AirPlay start refused'
                raise RuntimeError(self.error)
            if self.suppressed:
                if pcm_running():
                    return
                self.suppressed = False
            if self.stop_at is not None and self.active:
                # A stop/start pair is a new cast even within the debounce window.
                self.end()
            if self.active or self.pending is not None:
                self.stop_at = None
                return
            self.error = None
            self.cancelled = cancelled
            self.acknowledged = acknowledged
            def check_deadline():
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeError('AirPlay preparation timed out; output remains muted')
            try:
                self.mute()
                check_deadline()
                if self.server.TUNING.status().get('audio_owned'):
                    raise RuntimeError('Auto Tune owns the audio outputs')
                with self.server.DSP() as dsp:
                    loaded = json.loads(dsp.call('GetConfigJson'))
                if source_metadata(loaded)['id'] != 'tidal':
                    # Another input was picked on the Music page: back to the profile's
                    # Tidal input first; the cast is then built from that.
                    self.server.select_source('tidal')
                check_deadline()
                self.baseline.acquire()
                with self.server.DSP() as dsp:
                    path = dsp.call('GetConfigFilePath')
                    self.server.approved(dsp)
                    loaded = json.loads(dsp.call('GetConfigJson'))
                if not loopback_available() or not receiver_running():
                    raise RuntimeError('AirPlay receiver or dedicated loopback is unavailable')
                check_deadline()
                self.server.mpd('pause 1')
                check_deadline()
                self.expected = self.baseline.begin(path, loaded)
                check_deadline()
                self.pending = time.monotonic() if supplied_now is None else now
            except Exception as error:
                message = str(error) if isinstance(error, RuntimeError) else 'AirPlay preparation failed'
                self.fail(message)
                raise
            finally:
                self.baseline.release_if_idle()

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        with self.server.LOCK:
            if self.needs_mute:
                self.recover()
            if self.suppressed and not pcm_running():
                self.suppressed = False
            if not self.active and self.pending is None:
                return
            try:
                if self.cancelled is not None and self.cancelled.is_set():
                    self.fail('AirPlay preparation timed out; output remains muted')
                    return
                if self.server.TUNING.status().get('audio_owned'):
                    self.fail('Auto Tune took audio ownership', rollback=False)
                    return
                if self.acknowledged is not None and not self.acknowledged.is_set():
                    return
                if not receiver_running():
                    self.fail('AirPlay receiver stopped')
                    return
                if self.active and self.baseline.exists():
                    with self.server.DSP() as dsp:
                        loaded = json.loads(dsp.call('GetConfigJson'))
                        if not self.baseline.ready(dsp, loaded, self.expected):
                            self.fail('AirPlay baseline route changed')
                            return
                live = pcm_running()
                if self.stop_at is not None and now >= self.stop_at:
                    self.stop_at = None
                    if not live:
                        self.end()
                        return
                if self.pending is not None:
                    with self.server.DSP() as dsp:
                        loaded = json.loads(dsp.call('GetConfigJson'))
                        ready = (source_metadata(loaded)['id'] == 'airplay'
                                 and self.baseline.exists()
                                 and dsp.call('GetConfigFilePath') == str(self.baseline.candidate)
                                 and dsp.call('GetState') == 'Running'
                                 and loaded['devices']['capture']['device'] == 'hw:Loopback,1,1'
                                 and loaded['devices'].get('capture_samplerate') == 44100)
                        if self.baseline.exists():
                            ready = ready and self.baseline.ready(dsp, loaded, self.expected)
                        if self.expected is not None:
                            ready = ready and matches(loaded, self.expected)
                        if live and ready:
                            # Recheck ownership immediately before the only automatic unmute.
                            if self.server.TUNING.status().get('audio_owned'):
                                self.fail('Auto Tune took audio ownership', rollback=False)
                                return
                            self.server.ACTIVE_CAPTURE_CHANNELS = meter_channels(loaded)
                            dsp.call('SetVolume', -18.0)
                            if self.cancelled is not None and self.cancelled.is_set():
                                self.fail('AirPlay preparation timed out; output remains muted')
                                return
                            dsp.call('SetMute', False)
                            self.active = True
                            self.pending = self.previous = None
                            if not self.baseline.exists():
                                self.expected = None
                            return
                    if now - self.pending >= 10:
                        self.fail('AirPlay capture did not become ready; previous source restored')
                elif not live:
                    if self.silent_since is None:
                        self.silent_since = now
                    elif now - self.silent_since >= 1:
                        self.end()
                else:
                    self.silent_since = None
            except Exception:
                self.fail('AirPlay readiness check failed')

    def status(self):
        with self.server.LOCK:
            return dict(active=self.active, preparing=self.pending is not None, error=self.error)


def serve(controller):
    """Group grants socket access; SO_PEERCRED restricts mutations to Shairport UID."""
    uid = pwd.getpwnam('shairport-sync').pw_uid
    gid = grp.getgrnam('octet-airplay').gr_gid
    path = Path(SOCKET)
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(SOCKET)
        os.chown(SOCKET, -1, gid)
        os.chmod(SOCKET, 0o660)
    except BaseException:
        listener.close()
        path.unlink(missing_ok=True)
        raise
    listener.listen(4)
    listener.settimeout(.25)

    def monitor():
        while True:
            try:
                controller.tick()
            except Exception:
                logging.exception('AirPlay monitor could not mute DSP')
            time.sleep(.25)

    threading.Thread(target=monitor, daemon=True).start()
    while True:
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        with connection:
            connection.settimeout(2)
            handle_connection(connection, controller, uid)


def handle_connection(connection, controller, uid):
    try:
        _, peer_uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer_uid != uid:
            raise PermissionError('AirPlay hook user required')
        with connection.makefile('rb') as stream:
            event = stream.readline(16)
        if event not in (b'start\n', b'stop\n'):
            raise ValueError('Invalid AirPlay event')
        if not controller.hook_busy.acquire(blocking=False):
            raise RuntimeError('AirPlay preparation is still in progress')
        cancelled = threading.Event()
        acknowledged = threading.Event()
        done = threading.Event()
        errors = []
        def prepare():
            try:
                controller.event(event.decode().strip(), cancelled=cancelled, acknowledged=acknowledged)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
                controller.hook_busy.release()
        threading.Thread(target=prepare, daemon=True).start()
        if not done.wait(PREPARATION_TIMEOUT):
            cancelled.set()
            raise TimeoutError('AirPlay preparation timed out')
        if errors:
            raise errors[0]
        try:
            connection.sendall(b'OK\n')
            acknowledged.set()
        except OSError:
            cancelled.set()
            raise
    except Exception:
        logging.warning('AirPlay hook rejected or failed')
        try:
            connection.sendall(b'ERROR\n')
        except OSError:
            pass


def hook(event):
    if event not in ('start', 'stop'):
        raise ValueError('Expected start or stop')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(25)
        connection.connect(SOCKET)
        connection.sendall((event + '\n').encode())
        if connection.recv(16) != b'OK\n':
            raise RuntimeError('AirPlay controller rejected hook; output remains muted')


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['mute-on-exit']:
            from server import DSP
            with DSP() as dsp:
                dsp.call('SetMute', True)
        else:
            hook(sys.argv[1] if len(sys.argv) == 2 else '')
    except Exception:
        print('Octet AirPlay hook failed', file=sys.stderr)
        sys.exit(1)
