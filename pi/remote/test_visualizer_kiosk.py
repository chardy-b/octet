import subprocess
import signal
import unittest
from unittest.mock import MagicMock, patch

import visualizer_kiosk as kiosk


class KioskRecovery(unittest.TestCase):
    def setUp(self):
        group_signal = patch.object(kiosk.os, 'killpg', create=True)
        self.group_signal = group_signal.start()
        self.addCleanup(group_signal.stop)

    def test_cold_start_has_time_to_load_then_crashed_page_recovers(self):
        health = kiosk.DisplayHealth()
        self.assertFalse(health.failed(False, 0))
        self.assertFalse(health.failed(False, 59))
        self.assertTrue(health.failed(False, 60))
        self.assertFalse(health.failed(True, 61))
        self.assertFalse(health.failed(False, 70))
        self.assertFalse(health.failed(False, 99))
        self.assertTrue(health.failed(False, 100))

    def test_healthy_heartbeat_resets_the_failure_window(self):
        health = kiosk.DisplayHealth()
        for now in range(0, 600, 5):
            self.assertFalse(health.failed(True, now))
        self.assertFalse(health.failed(False, 600))
        self.assertFalse(health.failed(True, 625))
        self.assertFalse(health.failed(False, 630))
        self.assertFalse(health.failed(False, 655))

    def test_web_outages_do_not_restart_a_healthy_browser(self):
        health = kiosk.DisplayHealth()
        health.failed(True, 0)
        health.failed(False, 5)
        self.assertFalse(health.failed(None, 40))
        self.assertFalse(health.failed(None, 600))
        self.assertFalse(health.failed(False, 605))
        self.assertFalse(health.failed(True, 610))

    def test_only_valid_server_health_counts(self):
        with patch.object(kiosk, 'urlopen') as opening:
            response = opening.return_value.__enter__.return_value
            for payload, expected in [(b'{"settings":{"theme":"off"},"display":{"online":true}}', (False, True)),
                                      (b'{"settings":{"theme":"water"},"display":{"online":false}}', (True, False)),
                                      (b'{"display":{"online":"false"}}', None),
                                      (b'{}', None), (b'not json', None)]:
                response.read.return_value = payload
                self.assertEqual(kiosk.display_status('http://localhost/settings'), expected)
            opening.side_effect = OSError('web service restarting')
            self.assertIsNone(kiosk.display_status('http://localhost/settings'))

    def test_lost_page_stops_browser_and_requests_service_restart(self):
        browser = MagicMock()
        browser.poll.return_value = None
        with patch.object(kiosk.subprocess, 'Popen', return_value=browser), \
                patch.object(kiosk, 'display_status', side_effect=[(True, True), (True, False), (True, False)]), \
                patch.object(kiosk.time, 'monotonic', side_effect=[0, 5, 35]), \
                patch.object(kiosk.time, 'sleep'):
            self.assertEqual(kiosk.supervise('http://localhost/settings', ['chromium']), 1)
        self.group_signal.assert_called_once_with(browser.pid, signal.SIGTERM)
        browser.wait.assert_called_once_with(timeout=10)

    def test_hung_browser_is_killed_after_graceful_stop_times_out(self):
        browser = MagicMock()
        browser.poll.return_value = None
        browser.wait.side_effect = [subprocess.TimeoutExpired('chromium', 10), 0]
        kiosk.stop_browser(browser)
        self.assertEqual(self.group_signal.call_count, 2)
        self.group_signal.assert_called_with(browser.pid, getattr(signal, 'SIGKILL', signal.SIGTERM))

    def test_browser_exit_propagates_without_restarting_audio(self):
        browser = MagicMock(returncode=7)
        browser.poll.return_value = 7
        with patch.object(kiosk.subprocess, 'Popen', return_value=browser), \
                patch.object(kiosk, 'display_status', return_value=(True, False)):
            self.assertEqual(kiosk.supervise('http://localhost/settings', ['chromium']), 7)
        browser.terminate.assert_not_called()
        self.group_signal.assert_not_called()

    def test_off_and_unavailable_settings_never_launch_chromium(self):
        with patch.object(kiosk.subprocess, 'Popen') as opening, \
                patch.object(kiosk, 'display_status', side_effect=[None, (False, False), None]), \
                patch.object(kiosk.time, 'sleep', side_effect=[None, None, SystemExit(0)]):
            with self.assertRaises(SystemExit):
                kiosk.supervise('http://localhost/settings', ['chromium'])
        opening.assert_not_called()

    def test_selection_starts_browser_off_stops_it_and_selection_restarts_it(self):
        first, second = MagicMock(), MagicMock()
        first.poll.return_value = second.poll.return_value = None
        with patch.object(kiosk.subprocess, 'Popen', side_effect=[first, second]) as opening, \
                patch.object(kiosk, 'stop_browser') as stopping, \
                patch.object(kiosk, 'display_status', side_effect=[
                    (False, False), (True, False), None, (False, True), (False, False), (True, False)]), \
                patch.object(kiosk.time, 'sleep', side_effect=[None]*5 + [SystemExit(0)]):
            with self.assertRaises(SystemExit):
                kiosk.supervise('http://localhost/settings', ['chromium'])
        self.assertEqual(opening.call_count, 2)
        opening.assert_called_with(['chromium'], start_new_session=True)
        self.assertEqual([call.args[0] for call in stopping.call_args_list], [first, second])


if __name__ == '__main__':
    unittest.main()
