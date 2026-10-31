import json
from pathlib import Path
import tempfile
import unittest

from source_profiles import SourceProfiles


class SourceProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.origins = self.root / 'presets'
        self.origins.mkdir()
        self.shared = self.root / 'working'
        self.shared.mkdir()
        self.origin = self.origins / 'tuned.yml'
        self.origin.write_text('immutable tuning', encoding='utf-8')
        self.copies = SourceProfiles(self.shared)
        self.working = self.copies.target(self.origin)
        self.working.write_text('tuning with a selected input', encoding='utf-8')

    def test_provenance_survives_restart_without_editing_origin(self):
        before = self.origin.read_bytes()
        self.copies.save(self.working, self.origin)
        approval = dict(name='Saved tuning', kind='tuned')
        restarted = SourceProfiles(self.shared)
        record = restarted.read(self.working, lambda path: approval if path == str(self.origin) else None)
        self.assertEqual(record['origin_path'], str(self.origin))
        self.assertEqual(record['origin_approval'], approval)
        self.assertEqual(restarted.origin(self.working), self.origin)
        self.assertEqual(self.origin.read_bytes(), before)
        stored = json.loads(restarted.manifest_path(self.working).read_text())
        self.assertNotIn('origin_approval', stored)

    def test_each_origin_has_a_stable_separate_copy_and_nested_copies_are_rejected(self):
        other = self.origins / 'another.yml'
        other.write_text('other tuning', encoding='utf-8')
        self.assertEqual(self.copies.target(self.origin), self.working)
        self.assertNotEqual(self.copies.target(other), self.working)
        with self.assertRaises(ValueError):
            self.copies.target(self.working)
        with self.assertRaises(ValueError):
            self.copies.save(self.working, other)
        self.assertIsNone(self.copies.read(self.origin))

    def test_current_origin_approval_is_required_but_contents_are_not_checksummed(self):
        self.copies.save(self.working, self.origin)
        self.assertIsNone(self.copies.read(self.working, lambda path: None))
        self.working.write_text('modified working config', encoding='utf-8')
        self.origin.write_text('modified origin config', encoding='utf-8')
        self.assertEqual(self.copies.origin(self.working), self.origin)
        self.origin.unlink()
        self.assertIsNone(self.copies.read(self.working))

    def test_repeated_source_changes_update_manifest_without_changing_origin(self):
        self.copies.save(self.working, self.origin)
        before = self.origin.read_bytes()
        self.working.write_text('same tuning with Tidal restored', encoding='utf-8')
        self.copies.save(self.working, self.origin)
        self.assertEqual(self.copies.origin(self.working), self.origin)
        self.assertEqual(self.origin.read_bytes(), before)

    def test_missing_malformed_or_misdirected_manifests_are_not_approved(self):
        self.assertIsNone(self.copies.read(self.working))
        self.copies.save(self.working, self.origin)
        manifest = self.copies.manifest_path(self.working)
        record = json.loads(manifest.read_text())
        for changed in ('invalid json', '[]', '{}', json.dumps(dict(record, config_path=str(self.origin))),
                        json.dumps(dict(record, origin_path=str(self.working))),
                        json.dumps(dict(record, origin_path='relative.yml'))):
            with self.subTest(manifest=changed):
                manifest.write_text(changed, encoding='utf-8')
                self.assertIsNone(self.copies.read(self.working))


if __name__ == '__main__':
    unittest.main()
