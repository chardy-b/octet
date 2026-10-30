#!/usr/bin/env python3
"""Update the Octet web remote, AirPlay and Bluetooth controllers on the Pi from this checkout.

    python pi/remote/deploy.py                 # deploy the checked-out commit to octet@octet
    python pi/remote/deploy.py --dry-run       # run every read-only check, change nothing
    python pi/remote/deploy.py --rollback      # restore the newest backup

What it changes: the application files in /opt/octet-remote (the list in remote_manifest.py),
a REVISION file naming the commit that is running, and the RuntimeDirectoryMode lines of the
octet-remote drop-ins. What it never touches: MPD and its queue, CamillaDSP, speaker profiles,
volume, Auto Tune, the Shairport configuration, or any media. It restarts only octet-remote
(Shairport follows it), which ends muted. It never plays or unmutes audio.

First-time setup of a new Pi is in docs/DEPLOYMENT.md; this script is for every update after that.
"""
import argparse
import ast
import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from remote_manifest import EXECUTABLE, REMOTE_FILES  # noqa: E402

TARGET = Path('/opt/octet-remote')
DROPINS = Path('/etc/systemd/system/octet-remote.service.d')
BACKUPS = Path('/var/backups/octet-remote-deploy')
STAGING = Path('/var/tmp/octet-deploy')
STATUS_URL = 'http://127.0.0.1:8767/api/status'
SOCKET_DIR = Path('/run/octet-airplay')
RUNTIME_MODE = re.compile(r'(?m)^RuntimeDirectoryMode=0700$')


def say(message):
    print(message, flush=True)


# ---------------------------------------------------------------- development machine ----------

def local_git(*args):
    return subprocess.run(['git', '-C', str(HERE), *args], capture_output=True, text=True, check=True).stdout.strip()


def describe_checkout(allow_dirty):
    """Name the commit being shipped, and refuse to ship files that are not in it."""
    sha = local_git('rev-parse', '--short=12', 'HEAD')
    branch = local_git('rev-parse', '--abbrev-ref', 'HEAD')
    dirty = local_git('status', '--porcelain', '--', str(HERE), str(HERE.parent / 'systemd')).splitlines()
    if dirty and not allow_dirty:
        raise SystemExit('Uncommitted changes in pi/. Commit them so the Pi can be traced to a commit, '
                         'or pass --allow-dirty.\n' + '\n'.join(dirty[:10]))
    return dict(commit=sha, branch=branch, dirty=bool(dirty),
                built=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))


def build_package(revision, destination):
    missing = [name for name in REMOTE_FILES if not (HERE / name).is_file()]
    if missing:
        raise SystemExit(f'Files listed in remote_manifest.py are missing: {missing}')
    with tarfile.open(destination, 'w:gz') as archive:
        for name in ('deploy.py', 'remote_manifest.py', *REMOTE_FILES):
            archive.add(HERE / name, arcname=name)
        data = (json.dumps(revision, indent=2) + '\n').encode()
        info = tarfile.TarInfo('REVISION')
        info.size, info.mtime = len(data), int(time.time())
        archive.addfile(info, io.BytesIO(data))


def ssh(host, command, **kwargs):
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', host, command], text=True, **kwargs)


def deploy_from_here(options):
    revision = describe_checkout(options.allow_dirty)
    say(f"Deploying {revision['commit']} ({revision['branch']}{', with uncommitted changes' if revision['dirty'] else ''}) to {options.host}")
    with tempfile.TemporaryDirectory() as scratch:
        package = Path(scratch) / 'octet-deploy.tgz'
        build_package(revision, package)
        remote_dir = f"{STAGING}/{revision['commit']}"
        ssh(options.host, f'rm -rf {remote_dir} && mkdir -p {remote_dir}', check=True)
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', str(package), f'{options.host}:{remote_dir}/pkg.tgz'], check=True)
    flags = '--dry-run' if options.dry_run else ''
    command = (f'cd {remote_dir} && tar xzf pkg.tgz && rm pkg.tgz && '
               f'sudo -n python3 deploy.py --apply {remote_dir} {flags}')
    result = ssh(options.host, command)
    if result.returncode:
        raise SystemExit(result.returncode)


def rollback_from_here(options):
    result = ssh(options.host, f'sudo -n python3 {TARGET}/deploy.py --rollback-apply')
    if result.returncode:
        raise SystemExit('Rollback did not complete cleanly. Backups are in ' + str(BACKUPS))


# ------------------------------------------------------------------------------ the Pi ---------

def run(*args, check=True, timeout=60):
    return subprocess.run(list(args), capture_output=True, text=True, check=check, timeout=timeout)


def status():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=5) as response:
            return json.load(response)
    except Exception as error:  # noqa: BLE001 - any failure means "not healthy"
        return dict(unreachable=str(error))


def repaired_dropin(text):
    """AirPlay's Shairport joins group octet-airplay, which needs x on the shared directory.

    systemd applies the LAST RuntimeDirectoryMode to every RuntimeDirectory of the unit, so a
    0700 in any drop-in silently locks Shairport out. Returns (new_text, changed)."""
    new = RUNTIME_MODE.sub('RuntimeDirectoryMode=0750', text)
    return new, new != text


def import_closure(root, entry):
    """Every first-party module reachable from entry (static), so a missing file is caught before install."""
    found, pending = set(), [entry]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        for node in ast.walk(ast.parse((root / name).read_text())):
            modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
            pending.extend(f"{m.split('.')[0]}.py" for m in modules if (root / f"{m.split('.')[0]}.py").is_file())
    return found


def preflight(stage):
    """Everything here is read-only. Returns a list of problems; empty means safe to proceed."""
    problems = []
    for unit in ('octet-remote', 'camilladsp', 'mpd'):
        if run('systemctl', 'is-active', unit, check=False).stdout.strip() != 'active':
            problems.append(f'{unit} is not active; refusing to deploy over an unhealthy system')
    current = status()
    if 'unreachable' in current:
        problems.append('The running web remote does not answer: ' + current['unreachable'])
    elif current.get('airplay', {}).get('active') or current.get('airplay', {}).get('preparing'):
        problems.append('An AirPlay cast is active or starting; try again when it has ended')
    elif current.get('playback') == 'play' and not current.get('muted'):
        problems.append('Music is playing; stop it first (the restart would cut it off)')
    for name in ('server.py', 'airplay_control.py', 'remote_manifest.py'):
        compiled = run(sys.executable, '-m', 'py_compile', str(stage / name), check=False)
        if compiled.returncode:
            problems.append(f'{name} does not compile: {compiled.stderr.strip()[-200:]}')
    missing = sorted(import_closure(stage, 'server.py') - set(REMOTE_FILES))
    if missing:
        problems.append(f'server.py imports modules that are not in remote_manifest.py: {missing}')
    return problems


def take_backup():
    backup = BACKUPS / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    (backup / 'files').mkdir(parents=True, mode=0o700)
    for path in TARGET.iterdir():
        if path.is_file():
            shutil.copy2(path, backup / 'files' / path.name)
    (backup / 'dropins').mkdir()
    for path in DROPINS.glob('*.conf'):
        shutil.copy2(path, backup / 'dropins' / path.name)
    return backup


def install_files(stage):
    for name in (*REMOTE_FILES, 'REVISION', 'deploy.py', 'remote_manifest.py'):
        source, destination = stage / name, TARGET / name
        temporary = TARGET / f'.{name}.new'
        shutil.copyfile(source, temporary)
        temporary.chmod(0o755 if name in EXECUTABLE else 0o644)
        os.replace(temporary, destination)


def repair_dropins():
    changed = []
    for path in sorted(DROPINS.glob('*.conf')):
        text, was_changed = repaired_dropin(path.read_text())
        if was_changed:
            path.write_text(text)
            changed.append(path.name)
    if changed:
        run('systemctl', 'daemon-reload')
    return changed


def verify(expected_commit, deadline=90):
    """Poll until the new revision is healthy; return a list of problems (empty means healthy)."""
    end = time.monotonic() + deadline
    problems = ['no check ran']
    while time.monotonic() < end:
        problems = []
        if run('systemctl', 'is-active', 'octet-remote', check=False).stdout.strip() != 'active':
            problems.append('octet-remote is not active')
        current = status()
        if 'unreachable' in current:
            problems.append('web remote not answering yet')
        else:
            if not current.get('ready'):
                problems.append('DSP is not ready')
            if not current.get('muted'):
                problems.append('output is NOT muted after the restart')
            if current.get('airplay', {}).get('error'):
                problems.append('AirPlay controller reports: ' + str(current['airplay']['error']))
        if not problems:
            break
        time.sleep(3)
    if problems:
        return problems
    if run('systemctl', 'is-active', 'shairport-sync', check=False).stdout.strip() != 'active':
        problems.append('shairport-sync is not active')
    mode = oct(SOCKET_DIR.stat().st_mode & 0o777) if SOCKET_DIR.exists() else None
    if mode != '0o750':
        problems.append(f'{SOCKET_DIR} mode is {mode}, expected 0o750; Shairport cannot reach the controller')
    socket_path = SOCKET_DIR / 'control.sock'
    if not socket_path.exists():
        problems.append('AirPlay control socket is missing')
    else:
        access = run('setpriv', '--reuid=shairport-sync', '--regid=shairport-sync', '--groups=octet-airplay',
                     'test', '-x', str(SOCKET_DIR), check=False)
        if access.returncode:
            problems.append('The Shairport user cannot traverse ' + str(SOCKET_DIR))
    since = run('systemctl', 'show', 'octet-remote', '-p', 'ActiveEnterTimestamp', '--value').stdout.strip()
    log = run('journalctl', '-u', 'octet-remote', '--since', since, '--no-pager', check=False).stdout
    if 'Traceback' in log:
        say('  warning: octet-remote logged a traceback since the restart (a dropped browser connection also does this); '
            'read it with: journalctl -u octet-remote --since "' + since + '"')
    running = (TARGET / 'REVISION').read_text() if (TARGET / 'REVISION').exists() else ''
    if expected_commit and expected_commit not in running:
        problems.append('REVISION does not name the deployed commit')
    return problems


def restore(backup):
    for path in (backup / 'files').iterdir():
        temporary = TARGET / f'.{path.name}.restore'
        shutil.copyfile(path, temporary)
        os.replace(temporary, TARGET / path.name)
    for path in (backup / 'dropins').iterdir():
        shutil.copy2(path, DROPINS / path.name)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'restart', 'octet-remote')


def apply(stage, options):
    stage = Path(stage)
    revision = json.loads((stage / 'REVISION').read_text())
    say(f"On the Pi: {revision['commit']} ({revision['branch']}), built {revision['built']}")
    previous = (TARGET / 'REVISION').read_text().strip() if (TARGET / 'REVISION').exists() else 'no REVISION file (untracked install)'
    say('Currently running: ' + previous.replace('\n', ' '))
    problems = preflight(stage)
    for problem in problems:
        say('  BLOCKED: ' + problem)
    if problems:
        raise SystemExit(2)
    say('Preflight passed: services healthy, nothing playing, files compile, manifest complete.')
    drift = [path.name for path in sorted(DROPINS.glob('*.conf')) if repaired_dropin(path.read_text())[1]]
    say('Drop-in drift to repair: ' + (', '.join(drift) if drift else 'none'))
    if options.dry_run:
        say('Dry run: nothing was changed.')
        return
    backup = take_backup()
    say(f'Backed up the current files and drop-ins to {backup}')
    try:
        repair_dropins()
        install_files(stage)
        say('Installed files. Restarting octet-remote (output ends muted)...')
        run('systemctl', 'restart', 'octet-remote', timeout=120)
        if run('systemctl', 'is-active', 'shairport-sync', check=False).stdout.strip() != 'active':
            run('systemctl', 'start', 'shairport-sync', check=False)
        problems = verify(revision['commit'])
    except Exception as error:  # noqa: BLE001 - any failure must roll back
        problems = [f'deployment step failed: {error}']
    if problems:
        say('VERIFICATION FAILED, rolling back:')
        for problem in problems:
            say('  - ' + problem)
        restore(backup)
        again = verify(None, deadline=60)
        say('Rolled back. Health after rollback: ' + ('OK' if not again else '; '.join(again)))
        raise SystemExit(3)
    say(f"Deployed {revision['commit']}. Healthy and muted. Backup kept at {backup}")
    say('Nothing was played; unmute and test with a cast when you are ready.')


def rollback_apply():
    backups = sorted(path for path in BACKUPS.glob('*') if (path / 'files').is_dir())
    if not backups:
        raise SystemExit('No backups found in ' + str(BACKUPS))
    say('Restoring ' + str(backups[-1]))
    restore(backups[-1])
    problems = verify(None, deadline=60)
    say('Restored. Health: ' + ('OK' if not problems else '; '.join(problems)))
    raise SystemExit(1 if problems else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='octet@octet', help='ssh target (default: octet@octet)')
    parser.add_argument('--dry-run', action='store_true', help='run the read-only checks and stop')
    parser.add_argument('--allow-dirty', action='store_true', help='deploy even with uncommitted changes in pi/')
    parser.add_argument('--rollback', action='store_true', help='restore the newest backup on the Pi')
    parser.add_argument('--apply', metavar='STAGE', help=argparse.SUPPRESS)
    parser.add_argument('--rollback-apply', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.apply or options.rollback_apply:
        if os.geteuid() != 0:
            raise SystemExit('The Pi-side step must run as root (sudo).')
        return rollback_apply() if options.rollback_apply else apply(options.apply, options)
    return rollback_from_here(options) if options.rollback else deploy_from_here(options)


if __name__ == '__main__':
    main()
