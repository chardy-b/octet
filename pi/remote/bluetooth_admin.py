#!/usr/bin/env python3
"""Local administrator flow for the manual Bluetooth receiver. Run with sudo.

  discoverable [SECONDS]  open a timed pairing window (1-180 s, enforced by bluetoothd)
  close                   end discoverability/pairability now
  trust MAC               trust one already-paired phone; closes window
  untrust MAC             revoke trust for an explicit device
  start MAC | stop | status   drive octet-remote over its root-only socket

Any paired phone may play; there is no allowlist. Normally pairing is switched on and
off from the web page (bluetooth_pairing), and octet-remote trusts newly paired phones.
Pairing is plain Just Works with no agent: do not register one, because an interactive
agent makes BlueZ demand a passkey confirmation that nobody can answer. The
octet-bluealsa A2DP sink daemon must be running, or the phone sees no audio sink.
"""
import argparse
import json
import os
import socket
import subprocess
import sys

from bluetooth_contract import bridge_arguments
from bluetooth_identity import paired_addresses

SOCKET = '/run/octet-bluetooth/control.sock'
ADAPTER = '/org/bluez/hci0'
MAX_WINDOW = 180


def require_admin():
    if os.geteuid() != 0:
        raise RuntimeError('Bluetooth administration requires the local administrator')


def canonical(address):
    return bridge_arguments(address, [address])[-1]


def _bus(verb, path, interface, name, *value):
    result = subprocess.run(
        ['/usr/bin/busctl', '--system', '--no-pager', '--timeout=2s', verb,
         'org.bluez', path, interface, name, *value],
        capture_output=True, timeout=3, shell=False)
    if result.returncode != 0 or result.stderr != b'':
        raise RuntimeError(f'BlueZ {verb} failed: {name}')
    return result.stdout


def _adapter(name, *value):
    verb = 'set-property' if value else 'get-property'
    return _bus(verb, ADAPTER, 'org.bluez.Adapter1', name, *value)


def _device(address, name, *value):
    verb = 'set-property' if value else 'get-property'
    path = ADAPTER + '/dev_' + address.replace(':', '_')
    return _bus(verb, path, 'org.bluez.Device1', name, *value)


def close():
    """Attempt both writes even if one fails, then verify the adapter is closed."""
    require_admin()
    errors = []
    for name in ('Discoverable', 'Pairable'):
        try:
            _adapter(name, 'b', 'false')
            if _adapter(name) != b'b false\n':
                raise RuntimeError(f'BlueZ {name} still enabled')
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError('Bluetooth pairing window may still be open: ' + '; '.join(errors))


def discoverable(seconds=120):
    """bluetoothd itself ends the window, even if this process or octet-remote dies."""
    if type(seconds) is not int or not 0 < seconds <= MAX_WINDOW:
        raise ValueError(f'Pairing window must be 1-{MAX_WINDOW} whole seconds')
    require_admin()
    # Read-only: never power the adapter on implicitly; pairing cannot work off.
    if _adapter('Powered') != b'b true\n':
        raise RuntimeError('Bluetooth adapter hci0 is not powered; power it on deliberately '
                           '(bluetoothctl power on) before opening a pairing window')
    try:
        # Timeouts first, so the window can never open unbounded.
        for name in ('PairableTimeout', 'DiscoverableTimeout'):
            _adapter(name, 'u', str(seconds))
            if _adapter(name) != f'u {seconds}\n'.encode():
                raise RuntimeError(f'BlueZ {name} not applied')
        for name in ('Pairable', 'Discoverable'):
            _adapter(name, 'b', 'true')
            if _adapter(name) != b'b true\n':
                raise RuntimeError(f'BlueZ {name} not applied')
    except BaseException:
        close()
        raise


def trust(address):
    """Trust only an explicit, already-paired device on hci0."""
    require_admin()
    address = bridge_arguments(address, paired_addresses())[-1]
    expected = {'Address': f's "{address}"\n'.encode(),
                'Adapter': f'o "{ADAPTER}"\n'.encode(), 'Paired': b'b true\n'}
    for name, value in expected.items():
        if _device(address, name) != value:
            raise RuntimeError('Bluetooth device rejected: ' + name)
    _device(address, 'Trusted', 'b', 'true')
    if _device(address, 'Trusted') != b'b true\n':
        raise RuntimeError('Bluetooth device rejected: Trusted not applied')
    close()
    return address


def untrust(address):
    """Revocation never grants access."""
    require_admin()
    address = canonical(address)
    _device(address, 'Trusted', 'b', 'false')
    if _device(address, 'Trusted') != b'b false\n':
        raise RuntimeError('Bluetooth trust revocation not verified')
    return address


def request(line, path=SOCKET):
    require_admin()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(130)  # start waits up to 120 s for the phone stream
        connection.connect(path)
        connection.sendall(line.encode('ascii') + b'\n')
        reply = connection.makefile('rb').readline(65536)
    status, _, body = reply.rstrip(b'\n').partition(b' ')
    if status == b'OK':
        return json.loads(body)
    raise RuntimeError(json.loads(body) if status == b'ERROR' and body else
                       'Octet Bluetooth controller did not respond')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    window = commands.add_parser('discoverable')
    window.add_argument('seconds', nargs='?', type=int, default=120)
    commands.add_parser('close')
    for name in ('trust', 'untrust', 'start'):
        commands.add_parser(name).add_argument('address')
    commands.add_parser('stop')
    commands.add_parser('status')
    args = parser.parse_args(argv)
    try:
        require_admin()
        if args.command == 'discoverable':
            discoverable(args.seconds)
            result = dict(discoverable=True, seconds=args.seconds)
        elif args.command == 'close':
            close()
            result = dict(discoverable=False)
        elif args.command in ('trust', 'untrust'):
            result = dict(trusted=args.command == 'trust',
                          address=globals()[args.command](args.address))
        elif args.command == 'start':
            result = request('start ' + canonical(args.address))
        else:
            result = request(args.command)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'Octet Bluetooth: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
