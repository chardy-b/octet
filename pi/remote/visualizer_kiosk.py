"""Run Chromium only for selected artwork, and recover an unresponsive page."""
import json
import os
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from visualizer_state import THEMES


class DisplayHealth:
    def __init__(self):
        self.linked = False
        self.offline_since = None

    def failed(self, online, now):
        if online is True:
            self.linked = True
            self.offline_since = None
        elif online is None:
            # A web-service outage says nothing about Chromium's renderer.
            self.offline_since = None
        elif self.offline_since is None:
            self.offline_since = now
        timeout = 30 if self.linked else 60
        return self.offline_since is not None and now - self.offline_since >= timeout


def display_status(url):
    try:
        with urlopen(url, timeout=3) as response:
            state = json.load(response)
            theme, online = state['settings']['theme'], state['display']['online']
            if theme in THEMES and isinstance(online, bool):
                return theme != 'off', online
    except (OSError, URLError, ValueError, KeyError, TypeError):
        return None


def stop_browser(browser):
    if browser.poll() is None:
        # Chromium has renderer/GPU children; stop the whole private process group.
        def send(signum, force=False):
            try:
                if hasattr(os, 'killpg'):
                    os.killpg(browser.pid, signum)
                elif force:
                    browser.kill()
                else:
                    browser.terminate()
            except ProcessLookupError:
                pass
        send(signal.SIGTERM)
        try:
            browser.wait(timeout=10)
        except subprocess.TimeoutExpired:
            send(getattr(signal, 'SIGKILL', signal.SIGTERM), force=True)
            browser.wait()


def supervise(url, command):
    browser = None
    health = DisplayHealth()
    try:
        while True:
            state = display_status(url)
            if state is not None:
                enabled, online = state
                if not enabled and browser is not None:
                    stop_browser(browser)
                    browser = None
                    health = DisplayHealth()
                elif enabled and browser is None:
                    browser = subprocess.Popen(command, start_new_session=True)
                    health = DisplayHealth()
            if browser is not None:
                if browser.poll() is not None:
                    return browser.returncode
                if health.failed(state[1] if state is not None else None, time.monotonic()):
                    print('HDMI page heartbeat stopped; restarting the visualizer browser.',
                          file=sys.stderr, flush=True)
                    return 1  # systemd also reaps Chromium's remaining children.
            # No settings connection at startup means no browser launch.
            time.sleep(2)
    finally:
        if browser is not None:
            stop_browser(browser)


def main():
    # Ensure systemd's stop/restart also reaps the Chromium child.
    def terminate(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    return supervise(sys.argv[1], sys.argv[2:])


if __name__ == '__main__':
    sys.exit(main())
