"""Safety checks for duplicate occurrences, stale plans, and partial API updates."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tidal_cli as cli
from tidalapi.playlist import UserPlaylist


class FakePlaylist(UserPlaylist):
    def __init__(self):
        self.id = cli.AGAPE
        self.ids = ['1', '2', '1', '3']
        self.description = 'original'
        self.events = []

    def factory(self):
        return self

    def current(self):
        return dict(id=self.id, title='Example', description=self.description, public=False,
                    tracks=[dict(id=t, duration=7 if t == '4' else 10) for t in self.ids])

    def remove_by_indices(self, indices):
        self.events.append('remove')
        for i in sorted(indices, reverse=True):
            self.ids.pop(i)
        return True

    def add(self, identifiers, allow_duplicates=False):
        self.events.append('add')
        self.ids.extend(identifiers)
        return identifiers

    def move_by_indices(self, indices, target):
        self.events.append('move')
        selected = [self.ids[i] for i in indices]
        rest = [t for i, t in enumerate(self.ids) if i not in indices]
        self.ids = rest[:target] + selected + rest[target:]
        return True

    def edit(self, description=None):
        self.events.append('edit')
        self.description = description
        return True


def change_plan(playlist):
    current = playlist.current()
    return dict(playlist_id=playlist.id, expected_ids=cli.track_ids(current),
                ordered_ids=['1', '4', '3', '1'], addition_ids=['4'], remove_ids=['2'],
                description='updated', main_tracks=4, main_seconds=37,
                expected_metadata={k: current[k] for k in ('title', 'description', 'public')})


class PlanSafety(unittest.TestCase):
    def setUp(self):
        self.playlist = FakePlaylist()
        self.plan = change_plan(self.playlist)
        self.session = SimpleNamespace(playlist=lambda _: self.playlist,
            track=lambda _: SimpleNamespace(available=True, duration=7))

    def test_occurrence_preservation_and_complete_apply(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cli, 'snapshot', lambda p: p.current()):
            result = cli.apply_plan(self.session, Path(folder), self.plan)
            self.assertEqual(result['status'], 'applied-and-verified')
            self.assertEqual(self.playlist.ids, self.plan['ordered_ids'])
            self.assertEqual(self.playlist.ids.count('1'), 2)
            saved = cli.read_json(Path(result['backup']) / 'after.json')
            self.assertEqual(cli.track_ids(saved), self.plan['ordered_ids'])
            self.assertEqual(self.playlist.events, ['remove', 'add', 'move', 'move', 'edit'])

    def test_stale_order_and_metadata_rejected(self):
        for field in ('expected_ids', 'expected_metadata'):
            plan = deepcopy(self.plan)
            if field == 'expected_ids':
                plan[field].reverse()
            else:
                plan[field]['description'] = 'outdated'
            with self.subTest(field=field), self.assertRaises(ValueError):
                cli.validate_plan(plan, self.playlist.current())

    def test_undeclared_loss_of_duplicate_rejected(self):
        self.plan['ordered_ids'].pop()
        with self.assertRaisesRegex(ValueError, 'explicit additions/removals'):
            cli.validate_plan(self.plan, self.playlist.current())

    def test_ambiguous_removal_rejected(self):
        self.plan['remove_ids'] = ['1']
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            cli.validate_plan(self.plan, self.playlist.current())

    def test_dry_run_does_not_write_or_mutate(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cli, 'snapshot', lambda p: p.current()):
            result = cli.apply_plan(self.session, Path(folder), self.plan, dry_run=True)
            self.assertEqual(result['status'], 'dry-run-validated')
            self.assertEqual(self.playlist.events, [])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_invalid_runtime_rejected_before_mutation_even_on_dry_run(self):
        self.plan['main_seconds'] = 99
        for dry_run in (True, False):
            with tempfile.TemporaryDirectory() as folder, patch.object(cli, 'snapshot', lambda p: p.current()):
                with self.assertRaisesRegex(ValueError, 'runtime'):
                    cli.apply_plan(self.session, Path(folder), self.plan, dry_run=dry_run)
                self.assertEqual(self.playlist.events, [])
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_unavailable_addition_rejected_before_mutation(self):
        self.session.track = lambda _: SimpleNamespace(available=False)
        with tempfile.TemporaryDirectory() as folder, patch.object(cli, 'snapshot', lambda p: p.current()):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                cli.apply_plan(self.session, Path(folder), self.plan)
            self.assertEqual(self.playlist.events, [])

    def test_readback_mismatch_stops_before_next_edit(self):
        self.playlist.remove_by_indices = lambda indices: self.playlist.events.append('remove') or True
        with tempfile.TemporaryDirectory() as folder, patch.object(cli, 'snapshot', lambda p: p.current()), patch.object(cli.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Order differs'):
                cli.apply_plan(self.session, Path(folder), self.plan)
            self.assertEqual(self.playlist.events, ['remove'])
            self.assertEqual(len(list(Path(folder).glob('backups/*/before.json'))), 1)


class ReadConsistency(unittest.TestCase):
    def make_playlist(self, mismatch=False):
        rows = [SimpleNamespace(id=i, name=f'Track {i}', artists=[SimpleNamespace(name='Artist')],
                  album=SimpleNamespace(name='Album'), duration=10, available=i != 150) for i in range(205)]
        playlist = SimpleNamespace(id=cli.AGAPE, name='Example', description='description', public=False)
        offsets = []

        def request(method, path, params=None, headers=None):
            if path.endswith('/tracks'):
                offsets.append(params['offset'])
                value = rows[params['offset']:params['offset']+params['limit']]
                etag = 'other' if mismatch else 'revision'
            else:
                value, etag = {}, 'revision'
            return SimpleNamespace(json=lambda: value, headers={'etag': etag})

        def parse(_):
            playlist.num_tracks = len(rows)
            return playlist

        playlist.parse = parse
        playlist.session = SimpleNamespace(parse_track=lambda t: t)
        playlist.request = SimpleNamespace(request=request, map_json=lambda value, parse:
            [parse(t) for t in value] if isinstance(value, list) else parse(value))
        return playlist, offsets

    def test_all_pages_and_unavailable_occurrences_preserved(self):
        playlist, offsets = self.make_playlist()
        current = cli.snapshot(playlist)
        self.assertEqual(offsets, [0, 100, 200])
        self.assertEqual(len(current['tracks']), 205)
        self.assertFalse(current['tracks'][150]['available'])

    def test_mixed_metadata_and_track_revisions_never_accepted(self):
        playlist, _ = self.make_playlist(mismatch=True)
        with patch.object(cli.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'consistent playlist revision'):
            cli.snapshot(playlist)


if __name__ == '__main__':
    unittest.main()
