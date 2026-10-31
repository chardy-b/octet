"""The one list of files that make up /opt/octet-remote.

Every installer and the deploy script import this list, so they cannot drift apart.
test_install_manifest.py fails if server.py imports a module that is not listed here.
"""

REMOTE_FILES = (
    'server.py',
    'network_access.py',
    'airplay_control.py',
    'airplay_baseline.py',
    'bluetooth_control.py',
    'bluetooth_route.py',
    'bluetooth_bridge.py',
    'bluetooth_identity.py',
    'bluetooth_pairing.py',
    'bluetooth_contract.py',
    'bluetooth_preflight.py',
    'bluetooth_admin.py',
    'bluetooth_auto.py',
    'dac_watch.py',
    'bluetooth_transport.py',
    'install_bluetooth.py',
    'install_airplay.py',
    'audio_sources.py',
    'dsp_inputs.py',
    'input_levels.py',
    'input-levels.js',
    'visualizer_state.py',
    'tuning_proxy.py',
    'now_playing_bridge.py',
    'auto.js',
    'measurements.js',
    'index.html',
    'app.js',
    'dsp.html',
    'dsp.js',
    'style.css',
    'visualizer-sync.js',
    'visualizer.html',
    'visualizer.js',
    'visualizer-themes.js',
    'visualizer-solar.js',
    'visualizer-audio.js',
    'visualizer.css',
    'README.md',
    'source_profiles.py',
    'volume.js',
    'visualizer_kiosk.py',
    'launch-visualizer.sh',
)

# Files that must keep the executable bit.
EXECUTABLE = ('launch-visualizer.sh',)

# Full-resolution visualizer media. The *-playback.mp4 files are Git LFS objects and are not
# required to start the server, so the deploy script treats them as optional.
MEDIA = (
    'sky.jpg',
    'water.jpg',
    'stars.jpg',
    'solar.jpg',
    'sun.jpg',
    'lunar.jpg',
    'sky.mp4',
    'water.mp4',
    'stars.mp4',
    'solar.mp4',
    'sun.mp4',
    'lunar.mp4',
    'sky-playback.mp4',
    'water-playback.mp4',
    'stars-playback.mp4',
    'lunar-playback.mp4',
    'edm.mp3',
    'classical.mp3',
    'README.md',
    'visualizer_kiosk.py',
    'launch-visualizer.sh',
)
