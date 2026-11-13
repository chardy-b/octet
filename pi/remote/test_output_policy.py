"""Regression coverage for stale native mute and removed controls."""
import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml
import output_policy
import server
import dsp_client


class OutputPolicy(unittest.TestCase):
    def test_stale_native_mute_is_cleared_and_reported(self):
        dsp = Mock()
        dsp.call.side_effect = [True, None, False]
        with self.assertLogs(level='WARNING') as log:
            output_policy.reconcile(dsp)
        self.assertIn('obsolete native master mute', log.output[0])
        self.assertEqual([c.args for c in dsp.call.call_args_list],
                         [('GetMute',), ('SetMute', False), ('GetMute',)])

    def test_failed_native_clear_is_an_error(self):
        dsp = Mock()
        dsp.call.side_effect = [True, None, True]
        with self.assertRaisesRegex(RuntimeError, 'retained'):
            output_policy.reconcile(dsp)

    def test_normal_connections_never_change_gain_or_processing(self):
        dsp = Mock()
        dsp.call.return_value = False
        output_policy.reconcile(dsp)
        dsp.call.assert_called_once_with('GetMute')

    def test_both_clients_reject_master_mute_before_sending(self):
        for client in (server.DSP(), dsp_client.DSP()):
            client.send_frame = Mock()
            for value in (True, 1, None):
                with self.assertRaises(ValueError):
                    client.call('SetMute', value)
            client.send_frame.assert_not_called()

    def test_old_music_clients_cannot_interrupt_a_cast(self):
        with patch.object(server.AIRPLAY_CONTROL, 'invalidate') as invalidate:
            for value in (True, False):
                with self.assertRaisesRegex(ValueError, 'removed'):
                    server.action('mute', {'muted': value})
            invalidate.assert_not_called()

    def test_boot_discards_saved_latches_and_preserves_profile_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.yml'
            original = dict(config_path='/saved/profile.yml', volume=[-24, 0, 0, 0, 0],
                            mute=[True] * 5, other='preserved')
            state.write_text(yaml.safe_dump(original))
            state.chmod(0o640)
            output_policy.clear_saved_mute(state)
            self.assertEqual(yaml.safe_load(state.read_text()), dict(original, mute=[False] * 5))
            self.assertEqual(state.stat().st_mode & 0o777, 0o640)

    def test_no_production_code_sets_master_mute(self):
        roots = [Path(__file__).parent, Path(__file__).parents[2] / 'measure/octetmeasure']
        for root in roots:
            for path in root.rglob('*.py'):
                if path.name.startswith('test_'):
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    if (isinstance(node, ast.Call) and len(node.args) > 1
                            and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'SetMute'):
                        self.assertIsInstance(node.args[1], ast.Constant, str(path))
                        self.assertIs(node.args[1].value, False, str(path))

    def test_master_buttons_removed_from_both_pages(self):
        root = Path(__file__).parent
        self.assertNotIn('id="mute"', (root / 'index.html').read_text())
        self.assertNotIn('id="global-mute"', (root / 'dsp.html').read_text())
