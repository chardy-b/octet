"""Auto Tune commands; headless users normally use the browser over SSH/Tailscale."""
import json
import time

from .. import store
from . import config
from .runner import Runner


def command(args):
    runner = Runner(store.ROOT.parent/'autotune',demo=args.demo)
    if args.action == 'init':
        import yaml
        print(yaml.safe_dump(config.example(),sort_keys=False))
        return
    if args.action == 'status':
        print(json.dumps(runner.status(),indent=2))
        return
    # Muting another process must go through that process's API.
    if args.action == 'mute':
        import urllib.request
        req = urllib.request.Request(args.url.rstrip('/')+'/api/auto/mute',data=b'{}',headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            print(response.read().decode())
        return
    profile = config.load(args.profile) if args.profile else None
    runner.start(args.action,profile)
    previous = None
    try:
        while True:
            while runner.running:
                message = runner.status().get('message')
                if message != previous:
                    print(message,flush=True)
                    previous = message
                time.sleep(.2)
            status = runner.status()
            print(status.get('message',''),flush=True)
            if status['status'] in ('failed','cancelled','interrupted'):
                raise SystemExit(1)
            if status['status'] not in ('await_levels','await_room','await_reference'):
                break
            if not (args.demo and args.unattended):
                input('Press Enter to continue, or Ctrl+C to leave this checkpoint: ')
            runner.start('continue')
    except KeyboardInterrupt:
        runner.mute()
        if runner.thread:
            runner.thread.join(10)
        raise SystemExit(130)


def register(sub):
    p = sub.add_parser('auto',help='eight-output automatic tuning on the Pi')
    p.add_argument('action',choices=['init','start','levels','continue','resume','status','mute','apply','rollback'])
    p.add_argument('--profile',help='speaker.yaml (a snapshot is saved with each new run)')
    p.add_argument('--demo',action='store_true',help='simulate eight outputs and independent audio clocks')
    p.add_argument('--unattended',action='store_true',help='skip manual prompts in demo only')
    p.add_argument('--url',default='http://127.0.0.1:8765',help='server to send an immediate mute command to')
    p.set_defaults(fn=command)
