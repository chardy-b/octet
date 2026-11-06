import os
from pathlib import Path
import tempfile
import unittest

from source_route import provision_audio_lock


@unittest.skipUnless(hasattr(os, 'fchown'), 'Pi installer requires POSIX')
class LockProvisioning(unittest.TestCase):
    def test_creates_once_and_preserves_existing_inode_bytes_and_held_lease(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tuning/audio.lock'
            provision_audio_lock(path, os.getuid(), os.getgid())
            before = path.stat()
            with path.open('rb') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                provision_audio_lock(path, os.getuid(), os.getgid())
                self.assertEqual(path.stat().st_ino, before.st_ino)
                self.assertEqual(path.read_bytes(), b'0')
                with path.open('rb') as other, self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_refuses_symlink_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'
            target.write_bytes(b'preserve')
            path = root / 'audio.lock'
            path.symlink_to(target)
            with self.assertRaises(RuntimeError):
                provision_audio_lock(path, os.getuid(), os.getgid())
            self.assertEqual(target.read_bytes(), b'preserve')
