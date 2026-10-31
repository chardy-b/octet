"""The Bluetooth source is selectable from the web page and described while active."""
import unittest
from unittest.mock import MagicMock, patch

import server
from test_source_switching import SourceSwitching


def fixture():
    return SourceSwitching('test_unapproved_configuration_cannot_switch_sources_or_unmute').system()


class BluetoothUiTests(unittest.TestCase):
    def control(self, **status):
        control = MagicMock()
        control.owns_audio.return_value = False
        control.status.return_value = dict(active=False, preparing=False, recovery_pending=False, error=None, **status)
        return control

    def test_source_list_offers_bluetooth_only_with_an_enabled_policy(self):
        with fixture(), patch.object(server, 'BLUETOOTH_CONTROL', self.control()):
            with patch.object(server, 'bluetooth_addresses', return_value=()):
                self.assertNotIn('bluetooth', [s['id'] for s in server.current_status()['sources']])
            with patch.object(server, 'bluetooth_addresses', return_value=('AA:BB:CC:DD:EE:FF',)):
                self.assertIn('bluetooth', [s['id'] for s in server.current_status()['sources']])

    def test_choosing_bluetooth_starts_the_session_muted_first(self):
        control = self.control()
        with fixture() as (origin, baseline, tuning, dsp, *rest), \
                patch.object(server, 'BLUETOOTH_CONTROL', control), \
                patch.object(server, 'bluetooth_addresses', return_value=('AA:BB:CC:DD:EE:FF',)):
            server.action('source', {'id': 'bluetooth'})
        control.start.assert_called_once_with('AA:BB:CC:DD:EE:FF')
        self.assertIn(('SetMute', True), dsp.calls)
        self.assertNotIn(('SetMute', False), dsp.calls)

    def test_start_tries_each_allowed_phone_and_reports_the_last_refusal(self):
        control = self.control()
        control.start.side_effect = [RuntimeError('first not connected'), RuntimeError('second not connected')]
        with patch.object(server, 'BLUETOOTH_CONTROL', control), \
                patch.object(server, 'bluetooth_addresses', return_value=('A', 'B')):
            with self.assertRaisesRegex(RuntimeError, 'second not connected'):
                server.start_bluetooth()
        with patch.object(server, 'bluetooth_addresses', return_value=()), \
                self.assertRaisesRegex(RuntimeError, 'not enabled'):
            server.start_bluetooth()

    def test_active_session_is_the_selected_source(self):
        with fixture(), patch.object(server, 'BLUETOOTH_CONTROL', self.control()), \
                patch.object(server, 'bluetooth_addresses', return_value=('AA:BB:CC:DD:EE:FF',)):
            server.BLUETOOTH_CONTROL.status.return_value = dict(
                active=True, preparing=False, recovery_pending=False, error=None)
            status = server.current_status()
        self.assertEqual((status['source'], status['ready'], status['source_switching_available']),
                         ('bluetooth', True, True))
        self.assertTrue(status['bluetooth']['active'])


if __name__ == '__main__':
    unittest.main()
