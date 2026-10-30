import ast
from pathlib import Path
import unittest

import remote_manifest


ROOT = Path(__file__).parent


def copied_names(script):
    tree = ast.parse((ROOT / script).read_text(encoding='utf-8'))
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
            and isinstance(node.value, str)}


def script_imports(script):
    tree = ast.parse((ROOT / script).read_text(encoding='utf-8'))
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}


class DeploymentAssets(unittest.TestCase):
    def test_both_installers_include_local_server_dependencies(self):
        tree = ast.parse((ROOT / 'server.py').read_text(encoding='utf-8'))
        modules = {node.module + '.py' for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module
                   and (ROOT / (node.module + '.py')).is_file()}
        self.assertFalse(modules - set(remote_manifest.REMOTE_FILES))
        for script in ('install.py', 'install-visualizer.py'):
            with self.subTest(script=script):
                self.assertIn('remote_manifest', copied_names(script) | script_imports(script))

    def test_full_installer_includes_browser_playback_media(self):
        import re
        referenced = set(re.findall(r'([a-z]+-playback\.mp4)',
                                   (ROOT / 'visualizer.html').read_text(encoding='utf-8')))
        self.assertTrue(referenced)
        self.assertFalse(referenced - set(remote_manifest.MEDIA))

