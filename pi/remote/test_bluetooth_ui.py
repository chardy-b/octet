"""The Bluetooth source is selectable from the web page and described while active."""
from pathlib import Path
import tempfile
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

    def test_source_list_offers_bluetooth_only_with_a_paired_phone(self):
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

    def test_bluetooth_start_runs_without_holding_the_server_lock(self):
        control = self.control()
        held = []
        control.start.side_effect = lambda address: held.append(self.lock_free())
        with fixture(), patch.object(server, 'BLUETOOTH_CONTROL', control),                 patch.object(server, 'bluetooth_addresses', return_value=('AA:BB:CC:DD:EE:FF',)):
            server.action('source', {'id': 'bluetooth'})
        self.assertEqual(held, [True])

    @staticmethod
    def lock_free():
        # Another thread can take LOCK only if this thread is not holding it.
        import threading
        got = []
        def probe():
            got.append(server.LOCK.acquire(timeout=1))
            if got[-1]:
                server.LOCK.release()
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return got[0]

    def test_start_tries_each_allowed_phone_and_reports_the_last_refusal(self):
        control = self.control()
        control.start.side_effect = [RuntimeError('first not connected'), RuntimeError('second not connected')]
        with patch.object(server, 'BLUETOOTH_CONTROL', control), \
                patch.object(server, 'bluetooth_addresses', return_value=('A', 'B')):
            with self.assertRaisesRegex(RuntimeError, 'second not connected'):
                server.start_bluetooth()
        with patch.object(server, 'bluetooth_addresses', return_value=()), \
                self.assertRaisesRegex(RuntimeError, 'Pair a phone'):
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


class DiscoverableToggleTests(unittest.TestCase):
    """The Music page switch opens or closes pairing; paired phones always reconnect."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter = dict(discoverable=False)
        self.pairing = dict(server.BLUETOOTH_PAIRING)
        self.addCleanup(server.BLUETOOTH_PAIRING.update, self.pairing)
        def set_discoverable(enabled):
            self.adapter['discoverable'] = enabled
            return enabled
        for item in (patch.object(server, 'STATE', Path(directory.name)),
                     patch.object(server, 'DISCOVERABLE_SETTING', Path(directory.name) / 'bluetooth-discoverable.json'),
                     patch.object(server.bluetooth_pairing, 'trust_paired', return_value=[]),
                     patch.object(server.bluetooth_pairing, 'discoverable', side_effect=lambda: self.adapter['discoverable']),
                     patch.object(server.bluetooth_pairing, 'set_discoverable', side_effect=set_discoverable),
                     patch.object(server, 'bluetooth_devices', return_value=[
                         dict(address='AA:BB:CC:DD:EE:FF', name='Phone', paired=True, trusted=True, connected=True),
                         dict(address='11:22:33:44:55:66', name='Speaker', paired=False, trusted=False, connected=False)])):
            item.start()
            self.addCleanup(item.stop)

    def test_new_phones_can_pair_by_default_and_the_setting_is_reapplied(self):
        server.refresh_bluetooth_pairing()
        self.assertTrue(self.adapter['discoverable'])
        self.assertEqual(server.BLUETOOTH_PAIRING['devices'],
                         [dict(address='AA:BB:CC:DD:EE:FF', name='Phone', connected=True)])
        self.assertEqual(server.bluetooth_addresses(), ('AA:BB:CC:DD:EE:FF',))

    def test_toggle_persists_and_never_stops_bluetooth_playback(self):
        control = MagicMock()
        control.owns_audio.return_value = True
        with patch.object(server, 'BLUETOOTH_CONTROL', control), \
                patch.object(server, 'current_status', return_value={}):
            server.action('bluetooth-discoverable', {'enabled': False})
        control.stop.assert_not_called()
        self.assertFalse(self.adapter['discoverable'])
        # A reboot or bluetoothd restart resets the adapter; the saved choice wins.
        self.adapter['discoverable'] = True
        server.refresh_bluetooth_pairing()
        self.assertFalse(self.adapter['discoverable'])
        self.assertFalse(server.BLUETOOTH_PAIRING['discoverable'])
        with self.assertRaises(ValueError):
            server.action('bluetooth-discoverable', {'enabled': 'yes'})

    def test_bluez_failure_is_reported_not_raised(self):
        with patch.object(server.bluetooth_pairing, 'trust_paired', side_effect=RuntimeError('BlueZ is unavailable')):
            server.refresh_bluetooth_pairing()
        self.assertFalse(server.BLUETOOTH_PAIRING['available'])
        self.assertEqual(server.BLUETOOTH_PAIRING['error'], 'BlueZ is unavailable')


if __name__ == '__main__':
    unittest.main()
