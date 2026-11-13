"""Root DAC recovery steps. Only the fixed DAC/I2S devices may be rebound.

Called under dac_reboot's root-owned lock, via its authenticated local socket.
Receipts make progression durable and prevent repeated or out-of-order resets.
Never unload modules or reset the shared RP1 DMA controller.
"""
import json
import os
from pathlib import Path
import time

from dsp_client import DSP

MACHINE = Path('/sys/bus/platform/drivers/snd-rpi-simple')
I2S = Path('/sys/bus/platform/drivers/designware-i2s')
MACHINE_ID = 'soc@107c000000:sound'
I2S_ID = '1f000a0000.i2s'
HELD_UNITS = ('shairport-sync.service', 'octet-bluealsa.service', 'camillagui.service')
WINDOW = 300


def driver_identity():
    return dict(kernel=os.uname().release,
                module=Path('/sys/module/designware_i2s/srcversion').read_text().strip())


def require_driver_validation(broker):
    path = broker.STATE / 'driver-validation.json'
    if path.is_symlink() or path.stat().st_uid != 0:
        raise RuntimeError('Driver reset has not been validated for this kernel')
    if json.loads(path.read_text()) != driver_identity():
        raise RuntimeError('Driver reset validation is from a different kernel/module')


def selected_dac():
    with DSP() as dsp:
        config = json.loads(dsp.call('GetConfigJson'))
        if config.get('devices', {}).get('playback', {}).get('device') != 'hw:sndrpihifiberry,0':
            raise RuntimeError('The expected hardware DAC is not selected')


def stalled(broker):
    before = broker.pcm_sample()
    time.sleep(.4)
    after = broker.pcm_sample()
    if (not before[0] or before[0] != after[0] or before[2] is None or after[2] is None
            or after[1] not in ('RUNNING', 'XRUN') or before[2] != after[2]):
        raise RuntimeError('DAC progress returned, or its owner/state changed')
    # ALSA reports the IO thread ID; verify its thread group against systemd.
    main = broker.run('/usr/bin/systemctl', 'show', 'camilladsp.service', '-p', 'MainPID', '--value').strip()
    fields = dict(line.split(':', 1) for line in Path('/proc', after[0], 'status').read_text().splitlines() if ':' in line)
    if main == '0' or fields.get('Tgid', '').strip() != main:
        raise RuntimeError('The stalled DAC is not owned by CamillaDSP')


def require_output_policy(broker):
    value = broker.run('/usr/bin/systemctl', 'show', 'camilladsp.service', '-p', 'ExecStart', '--value')
    if '--mute' in value.split():
        raise RuntimeError('Obsolete CamillaDSP startup mute is still installed')


def owner_free(broker):
    paths = [*broker.LOOPBACK.glob('pcm*/sub*/status'), broker.DAC_STATUS]
    if len(paths) < 3 or any(path.read_text().strip() != 'closed' for path in paths):
        raise RuntimeError('Loopback and DAC must be owner-free before a driver reset')


def graph(broker):
    prefix = ('/usr/sbin/runuser', '-u', 'octet', '--', '/usr/bin/env',
              'XDG_RUNTIME_DIR=/run/user/1000', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus')
    for _ in range(12):
        output = broker.run(*prefix, '/usr/bin/wpctl', 'status', '-n', timeout=2)
        if broker.GRAPH <= set(output.split()):
            return
        time.sleep(.25)
    raise RuntimeError('The complete Loopback/DAC graph did not return')


def rebind(broker):
    """No user-supplied sysfs paths, device IDs, module names or shell commands."""
    owner_free(broker)
    pairs = ((MACHINE, MACHINE_ID, 'snd_soc_rpi_simple_soundcard'),
             (I2S, I2S_ID, 'designware_i2s'))
    for driver, device, module in pairs:
        if (not (driver / device).is_symlink() or (driver / 'module').resolve().name != module
                or not (driver / 'bind').is_file() or not (driver / 'unbind').is_file()):
            raise RuntimeError('Unexpected DAC/I2S driver binding; reset refused')
    removed = []
    try:
        for driver, device, _ in pairs:
            # Mark before the write: even a failed syscall can have detached a device.
            removed.append((driver, device))
            (driver / 'unbind').write_text(device)
    finally:
        failures = []
        for driver, device in reversed(removed):
            try:
                if not (driver / device).exists():
                    (driver / 'bind').write_text(device)
                if not (driver / device).exists():
                    raise RuntimeError('Device did not rebind')
            except Exception as error:
                failures.append(f'{device}: {error}')
        if failures:
            raise RuntimeError('Driver reattach incomplete: ' + '; '.join(failures))
    # Re-enumeration is owner-free; PipeWire itself is left running.
    broker.run('/usr/sbin/runuser', '-u', 'octet', '--', '/usr/bin/env',
               'XDG_RUNTIME_DIR=/run/user/1000', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
               '/usr/bin/systemctl', '--user', 'restart', 'wireplumber.service', timeout=8)
    graph(broker)


def handle(broker, operation):
    path = broker.STATE / 'ladder.json'
    boot = broker.BOOT_ID.read_text().strip()
    if operation == b'begin':
        if path.exists():
            previous = json.loads(path.read_text())
            if time.time() - previous['time'] < broker.COOLDOWN:
                raise RuntimeError('The DAC recovery cooldown has not expired')
        require_output_policy(broker)
        selected_dac()
        stalled(broker)
        try:
            evidence = broker.fault_evidence()
        except RuntimeError:
            evidence = []  # Stream/driver recovery can handle stalls without DMA errors.
        units = [unit for unit in HELD_UNITS if broker.run('/usr/bin/systemctl', 'show', unit,
                 '-p', 'ActiveState', '--value').strip() == 'active']
        broker.save_json(path, dict(boot=boot, time=time.time(), stage='begin', units=units, kernel=evidence))
        return
    record = json.loads(path.read_text())
    if record['boot'] != boot or not 0 <= time.time() - record['time'] <= WINDOW:
        raise RuntimeError('No current DAC recovery transaction')
    required = {b'stream': 'begin', b'driver': 'stream', b'finish': ('begin', 'stream', 'driver', 'finish')}
    stages = required.get(operation)
    if stages is None or record['stage'] not in ((stages,) if isinstance(stages, str) else stages):
        raise RuntimeError('DAC recovery steps are out of order')
    require_output_policy(broker)
    selected_dac()
    if operation != b'finish':
        stalled(broker)
    if (broker.LOOPBACK / 'pcm0p/sub0/status').read_text().strip() != 'closed':
        raise RuntimeError('MPD must release Loopback before DAC recovery')
    # Persist before each privileged operation. A crash consumes this step.
    broker.save_json(path, dict(record, stage=operation.decode()))
    if operation == b'finish':
        before = broker.pcm_sample()
        time.sleep(.4)
        after = broker.pcm_sample()
        if not before[0] or before[0] != after[0] or after[1] != 'RUNNING' or before[2] == after[2]:
            raise RuntimeError('DAC progress could not be verified')
        graph(broker)
        if record['units']:
            broker.run('/usr/bin/systemctl', 'start', *record['units'], timeout=15)
        selected_dac()
        return
    broker.run('/usr/bin/systemctl', 'stop', *HELD_UNITS, timeout=15)
    if operation == b'stream':
        broker.run('/usr/bin/systemctl', 'restart', 'camilladsp.service', timeout=15)
    else:
        require_driver_validation(broker)
        broker.run('/usr/bin/systemctl', 'stop', 'camilladsp.service', timeout=15)
        rebind(broker)
        broker.run('/usr/bin/systemctl', 'start', 'camilladsp.service', timeout=15)
    selected_dac()
