"""Focused tests for fail-closed requested-source persistence."""

import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from source_intent import SourceIntent, SourceIntentError


def _select(root, mode):
    SourceIntent(root).select(mode)


class SourceIntentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "intent"
        self.root.mkdir(mode=0o700)
        self.intent = SourceIntent(self.root)

    def initialize(self, mode="auto"):
        self.intent.initialize(mode, attended=True, server_stopped=True)

    def raw(self, value):
        self.root.joinpath("source-intent.json").write_text(value)
        self.root.joinpath("source-intent.json").chmod(0o600)

    def test_all_modes_round_trip(self):
        self.initialize()
        for mode in ("auto", "tidal", "airplay", "bluetooth"):
            self.assertEqual(self.intent.select(mode), mode)
            self.assertEqual(SourceIntent(self.root).load(), mode)

    def test_missing_corrupt_duplicate_list_and_oversize_are_blocked(self):
        with self.assertRaises(SourceIntentError):
            self.intent.load()
        bad = ("not json", '["auto"]', '{"version":1,"version":1,"mode":"auto"}',
               '{"version":1,"mode":"auto","extra":1}', "x" * 4097)
        for value in bad:
            self.raw(value)
            with self.assertRaises(SourceIntentError, msg=value[:30]):
                SourceIntent(self.root).load()

    def test_schema_rejects_wrong_version_and_nonstring_modes(self):
        for value in ({"version": 2, "mode": "auto"}, {"version": 1, "mode": ["auto"]},
                      {"version": True, "mode": "auto"}, {"version": 1, "mode": "usb"}):
            self.raw(json.dumps(value))
            with self.assertRaises(SourceIntentError):
                SourceIntent(self.root).load()

    def test_symlink_and_permissions_are_blocked(self):
        target = self.root.parent / "target"
        target.write_text('{"version":1,"mode":"auto"}')
        target.chmod(0o600)
        self.root.joinpath("source-intent.json").symlink_to(target)
        with self.assertRaises(SourceIntentError):
            self.intent.load()
        self.root.joinpath("source-intent.json").unlink()
        self.raw('{"version":1,"mode":"auto"}')
        self.root.joinpath("source-intent.json").chmod(0o640)
        with self.assertRaises(SourceIntentError):
            self.intent.load()
        self.root.joinpath("source-intent.json").chmod(0o600)
        self.root.chmod(0o750)
        with self.assertRaises(SourceIntentError):
            self.intent.load()

    def test_wrong_owner_is_blocked(self):
        self.initialize()
        with patch("source_intent.os.fstat") as fstat:
            real = os.fstat
            fstat.side_effect = lambda fd: _different_owner(real(fd))
            with self.assertRaises(SourceIntentError):
                SourceIntent(self.root).load()

    def test_short_writes_are_completed(self):
        self.initialize()
        real_write = os.write
        with patch("source_intent.os.write", side_effect=lambda fd, data: real_write(fd, data[:2])):
            self.intent.select("airplay")
        self.assertEqual(SourceIntent(self.root).load(), "airplay")

    def test_concurrent_writers_leave_complete_state(self):
        self.initialize()
        processes = [multiprocessing.Process(target=_select, args=(self.root, mode))
                     for mode in ("tidal", "airplay", "bluetooth", "auto")]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertIn(SourceIntent(self.root).load(), SourceIntent.MODES)

    def test_file_fsync_and_replace_fail_before_publication(self):
        self.initialize("tidal")
        with patch("source_intent.os.fsync", side_effect=OSError("file sync")):
            with self.assertRaises(SourceIntentError):
                self.intent.select("airplay")
        self.assertEqual(SourceIntent(self.root).load(), "tidal")
        with patch("source_intent.os.replace", side_effect=OSError("rename")):
            with self.assertRaises(SourceIntentError):
                self.intent.select("airplay")
        self.assertEqual(SourceIntent(self.root).load(), "tidal")

    def test_post_replace_failure_latches_and_load_cannot_clear_it(self):
        self.initialize("tidal")
        real_fsync = os.fsync
        calls = 0

        def fail_directory(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("directory sync")
            return real_fsync(fd)
        with patch("source_intent.os.fsync", side_effect=fail_directory):
            with self.assertRaises(SourceIntentError):
                self.intent.select("airplay")
        self.assertTrue(self.intent.uncertain)
        self.assertIsNone(self.intent.mode)
        with self.assertRaises(SourceIntentError):
            self.intent.load()
        self.assertTrue(self.intent.uncertain)

    def test_post_replace_readback_failure_is_uncertain(self):
        self.initialize("tidal")
        original = self.intent._read_locked
        with patch.object(self.intent, "_read_locked", side_effect=["tidal", SourceIntentError("readback")]):
            with self.assertRaises(SourceIntentError):
                self.intent.select("airplay")
        self.assertTrue(self.intent.uncertain)
        self.assertIsNone(self.intent.mode)
        with self.assertRaises(SourceIntentError):
            self.intent.load()
        # Maintenance verification, not ordinary load, can establish the published mode.
        with patch.object(self.intent, "_read_locked", side_effect=original):
            self.assertEqual(self.intent.verify_after_uncertainty(), "airplay")

    def test_explicit_verification_clears_only_after_sync_and_consistent_readback(self):
        self.initialize("tidal")
        self.intent._mark_uncertain()
        with patch("source_intent.os.fsync", side_effect=OSError("sync")):
            with self.assertRaises(SourceIntentError):
                self.intent.verify_after_uncertainty()
        self.assertTrue(self.intent.uncertain)
        original = self.intent._read_locked
        reads = iter(("tidal", "airplay"))
        with patch.object(self.intent, "_read_locked", side_effect=lambda fd: next(reads)):
            with self.assertRaises(SourceIntentError):
                self.intent.verify_after_uncertainty()
        self.assertTrue(self.intent.uncertain)
        self.assertEqual(self.intent.verify_after_uncertainty(), "tidal")
        self.assertFalse(self.intent.uncertain)

    def test_fresh_load_performs_durable_startup_gate(self):
        self.initialize("bluetooth")
        fresh = SourceIntent(self.root)
        with patch("source_intent.os.fsync", side_effect=OSError("directory sync")):
            with self.assertRaises(SourceIntentError):
                fresh.load()
        self.assertTrue(fresh.uncertain)
        self.assertIsNone(fresh.mode)

    def test_initialize_is_guarded_and_has_no_routing_side_effects(self):
        with self.assertRaises(SourceIntentError):
            self.intent.initialize("auto", attended=False, server_stopped=True)
        with self.assertRaises(SourceIntentError):
            self.intent.initialize("auto", attended=True, server_stopped=False)
        self.initialize()
        self.assertEqual(set(self.root.iterdir()), {self.root / "source-intent.json"})
        with self.assertRaises(SourceIntentError):
            SourceIntent(self.root).initialize("tidal", attended=True, server_stopped=True)


def _different_owner(info):
    values = list(info)
    values[4] = info.st_uid + 1
    return os.stat_result(values)


if __name__ == "__main__":
    unittest.main()
