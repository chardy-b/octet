#!/usr/bin/env python3
"""Standalone Tidal playlist management. No Octet server or SSH dependency."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import functools
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

AGAPE = '18f798bf-9650-417c-9dcd-98ee1c15793f'
MAX_TRACKS = 10000
logging.getLogger('tidalapi').setLevel(logging.ERROR)


def state_directory():
    if os.environ.get('TIDAL_STATE_DIR'):
        return Path(os.environ['TIDAL_STATE_DIR']).expanduser()
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'OctetTidal'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    """Atomic writes; private session files inherit the private state directory ACL."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_session(session, state):
    expiry = getattr(session, 'expiry_time', None)
    write_json(state / 'session.json', dict(
        token_type=session.token_type, access_token=session.access_token,
        refresh_token=session.refresh_token, is_pkce=session.is_pkce,
        expiry_time=expiry.isoformat() if expiry else None))


@contextmanager
def state_lock(state):
    """Serialize local sessions and edits, including refreshed credential writes."""
    if not state.is_dir():
        raise RuntimeError('Run install-local.ps1 to create the private local state directory.')
    with (state / 'session.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + 30
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Another local Tidal command is still running.') from None
                time.sleep(0.2)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def connect(state, login=False):
    import tidalapi
    session = tidalapi.Session(tidalapi.Config(item_limit=100))
    session.request_session.request = functools.partial(session.request_session.request, timeout=30)
    if login:
        session.login_oauth_simple(fn_print=lambda message: print(message, file=sys.stderr, flush=True))
    else:
        path = state / 'session.json'
        if not path.is_file():
            raise RuntimeError('No local Tidal session. Run tidal.cmd login or import the Octet session.')
        credentials = read_json(path)
        credentials = {k: v.get('data') if isinstance(v, dict) else v for k, v in credentials.items()}
        expiry = credentials.get('expiry_time')
        valid = session.load_oauth_session(
            credentials['token_type'], credentials['access_token'], credentials.get('refresh_token'),
            expiry_time=datetime.fromisoformat(expiry) if expiry else None,
            is_pkce=credentials.get('is_pkce', False))
        if not valid:
            raise RuntimeError('Local Tidal login expired. Run tidal.cmd login.')
    save_session(session, state)
    return session


def playlist_id(value):
    return str(uuid.UUID(str(value)))


def info(track):
    return dict(id=str(track.id), title=track.name,
                artist=', '.join(a.name for a in track.artists), album=track.album.name,
                duration=int(track.duration), available=bool(track.available),
                url=f'https://tidal.com/browse/track/{track.id}')


def snapshot(playlist):
    """Read uncached pages from a single revision, including unplayable tracks."""
    for attempt in range(5):
        meta = playlist.request.request('GET', f'playlists/{playlist.id}',
            params={'_': str(time.time_ns())}, headers={'Cache-Control': 'no-cache'})
        playlist.request.map_json(meta.json(), parse=playlist.parse)
        expected, rows, revisions = playlist.num_tracks, [], set()
        if expected > MAX_TRACKS:
            raise ValueError('Playlist exceeds the 10,000-track limit.')
        for offset in range(0, expected, 100):
            response = playlist.request.request('GET', f'playlists/{playlist.id}/tracks',
                params={'limit': 100, 'offset': offset, '_': str(time.time_ns())},
                headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
            revisions.add(response.headers['etag'])
            rows.extend(playlist.request.map_json(response.json(), parse=playlist.session.parse_track))
        if len(rows) == expected and (len(revisions) == 1 or expected == 0):
            revision = revisions.pop() if revisions else meta.headers['etag']
            if revision != meta.headers['etag']:
                time.sleep(0.3)
                continue
            playlist._etag = revision
            return dict(id=str(playlist.id), title=playlist.name,
                        description=playlist.description, public=playlist.public,
                        revision=revision, tracks=[info(t) for t in rows])
        time.sleep(0.3 * (attempt + 1))
    raise RuntimeError('Could not read one consistent playlist revision; no further changes made.')


def track_ids(value):
    return [str(t['id']) for t in value['tracks']]


def validate_plan(plan, before):
    """Explicit additions/removals must explain every occurrence in the final order."""
    if playlist_id(plan['playlist_id']) != playlist_id(before['id']):
        raise ValueError('Plan refers to a different playlist.')
    live = track_ids(before)
    if live != plan['expected_ids']:
        raise ValueError('Live playlist differs from the plan. Export a fresh plan before editing.')
    metadata = {k: before[k] for k in ('title', 'description', 'public')}
    if metadata != plan.get('expected_metadata', metadata):
        raise ValueError('Playlist metadata changed since the plan was prepared.')
    additions = plan.get('addition_ids', [])
    removals = plan.get('remove_ids', [])
    if len(set(additions)) != len(additions) or set(additions).intersection(live):
        raise ValueError('Additions must be unique IDs absent from the current playlist.')
    if len(set(removals)) != len(removals) or any(live.count(t) != 1 for t in removals):
        raise ValueError('Each removal must identify exactly one existing occurrence.')
    final = plan['ordered_ids']
    if not final or len(final) > MAX_TRACKS:
        raise ValueError('Final playlist must contain 1–10,000 tracks.')
    if any(not isinstance(t, str) or not t.isdecimal() for t in live + final + additions + removals):
        raise ValueError('Track IDs must be decimal strings.')
    if Counter(final) != Counter(live) - Counter(removals) + Counter(additions):
        raise ValueError('Final order does not match the explicit additions/removals.')
    count = plan.get('main_tracks')
    if count is not None and (type(count) is not int or not 1 <= count <= len(final)):
        raise ValueError('main_tracks must be a valid positive count.')
    description = plan.get('description', before['description'])
    if not isinstance(description, str) or len(description) > 1000:
        raise ValueError('Description must be a string of at most 1,000 characters.')
    if not description and before['description']:
        raise ValueError('Clearing the description is not supported by this CLI.')
    return live, final, additions, removals


def next_move(live, final, target):
    """Move a matching contiguous block upward; preserve duplicate occurrences."""
    source = live.index(final[target], target + 1)
    length = 1
    while source + length < len(live) and target + length < len(final):
        if live[source + length] != final[target + length]:
            break
        length += 1
    expected = live[:target] + live[source:source+length] + live[target:source] + live[source+length:]
    return list(range(source, source+length)), expected


def verify(playlist, expected):
    for attempt in range(7):
        observed = snapshot(playlist)
        if track_ids(observed) == expected:
            return observed
        time.sleep(0.4 * (attempt + 1))
    raise RuntimeError('Order differs after a change; stopped before further edits. See the backup.')


def apply_plan(session, state, plan, dry_run=False):
    from tidalapi.playlist import UserPlaylist
    playlist = session.playlist(playlist_id(plan['playlist_id'])).factory()
    if not isinstance(playlist, UserPlaylist):
        raise ValueError('The signed-in user does not own this playlist.')
    before = snapshot(playlist)
    live, final, additions, removals = validate_plan(plan, before)
    for identifier in additions:
        if not session.track(int(identifier)).available:
            raise ValueError(f'Approved addition {identifier} is unavailable.')
    # Check timing before reporting a valid dry run or making the first edit.
    known = {t['id']: t['duration'] for t in before['tracks']}
    known.update({t: int(session.track(int(t)).duration) for t in additions})
    count = plan.get('main_tracks', len(final))
    seconds = sum(known[t] for t in final[:count])
    if 'main_seconds' in plan and (type(plan['main_seconds']) is not int or seconds != plan['main_seconds']):
        raise ValueError('Main-program runtime differs from the reviewed plan.')
    preview = dict(playlist_id=before['id'], before_tracks=len(live), after_tracks=len(final),
                   additions=len(additions), removals=len(removals),
                   order_changes=live != final,
                   description_changes=before['description'] != plan.get('description', before['description']))
    if dry_run:
        return dict(status='dry-run-validated', **preview)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    backup = state / 'backups' / stamp
    backup.mkdir(parents=True, mode=0o700)
    write_json(backup / 'before.json', before)
    write_json(backup / 'plan.json', plan)
    print(f'Backup: {backup}', file=sys.stderr, flush=True)
    try:
        if removals:
            positions = sorted(live.index(t) for t in removals)
            expected = [t for i, t in enumerate(live) if i not in positions]
            if not playlist.remove_by_indices(positions):
                raise RuntimeError('Tidal rejected the removals.')
            live = track_ids(verify(playlist, expected))
        if additions:
            expected = live + additions
            playlist.add(additions, allow_duplicates=False)
            live = track_ids(verify(playlist, expected))
        moves = 0
        for target in range(len(final)):
            if live[target] == final[target]:
                continue
            indices, expected = next_move(live, final, target)
            if not playlist.move_by_indices(indices, target):
                raise RuntimeError('Tidal rejected a move.')
            live = track_ids(verify(playlist, expected))
            moves += 1
            print(f'Verified move {moves}; prefix {target + len(indices)}/{len(final)}',
                  file=sys.stderr, flush=True)
        description = plan.get('description', before['description'])
        if description != before['description']:
            if not description:
                raise ValueError('An empty description is not supported by this CLI.')
            if not playlist.edit(description=description):
                raise RuntimeError('Tidal rejected the description update.')
        after = verify(session.playlist(playlist.id).factory(), final)
        if after['description'] != description or any(after[k] != before[k] for k in ('title', 'public')):
            raise RuntimeError('Final playlist metadata did not verify.')
        write_json(backup / 'after.json', after)
        return dict(status='applied-and-verified', backup=str(backup), moves=moves,
                    main_tracks=count, main_seconds=seconds, **preview)
    except Exception:
        print(f'Stopped. Inspect current state before resuming; backup: {backup}', file=sys.stderr)
        raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='Check the local login without printing credentials.')
    sub.add_parser('login', help='Authorize a new local session directly with Tidal.')
    search = sub.add_parser('search', help='Search the Tidal track catalog.')
    search.add_argument('query')
    search.add_argument('--limit', type=int, default=10)
    sub.add_parser('playlists', help='List all owned/favorited playlists, paginated.')
    for name in ('show', 'plan'):
        command = sub.add_parser(name, help='Export a playlist.' if name == 'show' else 'Create an editable change plan.')
        command.add_argument('playlist', nargs='?', default=AGAPE)
        command.add_argument('--output', type=Path, required=name == 'plan')
        if name == 'plan':
            command.add_argument('--main-tracks', type=int)
    apply = sub.add_parser('apply', help='Apply an explicit plan, with automatic backups and verification.')
    apply.add_argument('plan', type=Path)
    apply.add_argument('--dry-run', action='store_true')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'search' and not 1 <= args.limit <= 100:
        raise ValueError('Search limit must be between 1 and 100.')
    state = state_directory()
    with state_lock(state):
        session = connect(state, login=args.command == 'login')
        try:
            if args.command in ('login', 'status'):
                output = dict(connected=True, user_id=str(session.user.id),
                              session_file=str(state / 'session.json'), backend='local tidalapi')
            elif args.command == 'search':
                import tidalapi
                output = dict(tracks=[info(t) for t in session.search(
                    args.query, models=[tidalapi.Track], limit=args.limit).get('tracks', [])])
            elif args.command == 'playlists':
                rows, offset = [], 0
                while True:
                    page = session.user.playlist_and_favorite_playlists(offset=offset, limit=50)
                    rows.extend(dict(id=str(p.id), title=p.name, tracks=p.num_tracks) for p in page)
                    if len(page) < 50:
                        break
                    offset += len(page)
                output = dict(playlists=rows)
            elif args.command in ('show', 'plan'):
                current = snapshot(session.playlist(playlist_id(args.playlist)).factory())
                output = current
                if args.command == 'plan':
                    current_ids = track_ids(current)
                    output = dict(playlist_id=current['id'], expected_ids=current_ids,
                                  ordered_ids=list(current_ids), addition_ids=[], remove_ids=[],
                                  description=current['description'],
                                  expected_metadata={k: current[k] for k in ('title', 'description', 'public')})
                    if args.main_tracks is not None:
                        if not 1 <= args.main_tracks <= len(current_ids):
                            raise ValueError('main-tracks must be a valid positive count.')
                        output.update(main_tracks=args.main_tracks,
                                      main_seconds=sum(t['duration'] for t in current['tracks'][:args.main_tracks]))
                if args.output:
                    if args.output.exists():
                        raise ValueError('Output exists; choose a new file to preserve the original.')
                    write_json(args.output, output)
                    output = dict(saved=str(args.output.resolve()), tracks=len(current['tracks']))
            else:
                output = apply_plan(session, state, read_json(args.plan), args.dry_run)
        finally:
            save_session(session, state)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, TimeoutError) as error:
        if type(error).__module__.startswith(('requests', 'urllib3', 'tidalapi')):
            print(f'Tidal operation failed ({type(error).__name__}). Check connectivity/login.', file=sys.stderr)
        else:
            print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        # Never dump HTTP request details, response bodies, or authentication tokens.
        print(f'Tidal operation failed ({type(error).__name__}). Check connectivity/login.', file=sys.stderr)
        raise SystemExit(1)
