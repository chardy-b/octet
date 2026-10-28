"""Effective-property tests for the distinct Auto Tune identity and remote DSP group access.

SOURCE-ONLY: these units and the drop-in template are not deployable. They require a reviewed
atomic installer that first creates the octet-autotune UID/GID and octet-dsp-control GID, backs up
and migrates /var/lib/octet-autotune ownership from octet:audio, and proves rollback restores the
original octet ownership, all before any unit restart.
"""
from pathlib import Path
import sys
import unittest

# install_bluetooth imports its sibling install_airplay as a top-level module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_airplay
import install_bluetooth

SYSTEMD = Path(__file__).resolve().parents[1] / 'systemd'
AUTOTUNE = SYSTEMD / 'octet-autotune.service'
REMOTE_BASE = Path(__file__).resolve().parent / 'octet-remote.service'
REMOTE_DSP = SYSTEMD / 'octet-remote-dsp.conf'
ACCUMULATING = {'SupplementaryGroups', 'ReadWritePaths', 'Environment'}
LISTS = {'ExecStart', 'ExecStopPost'}
DSP_GROUP = 'octet-dsp-control'


def compose(*sources):
    """Merge unit text/paths in order. Accumulating keys append; an empty assignment resets."""
    unit = {}
    for source in sources:
        text = source.read_text() if isinstance(source, Path) else source
        section = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line[0] in '#;':
                continue
            if line.startswith('['):
                section = unit.setdefault(line[1:-1], {})
                continue
            key, _, value = line.partition('=')
            values = section.setdefault(key.strip(), [])
            value = value.strip()
            if key.strip() in ACCUMULATING | LISTS:
                if value == '':
                    values.clear()
                else:
                    values.append(value)
            else:
                values[:] = [value]
    return unit


def groups(unit):
    return [g for v in unit['Service'].get('SupplementaryGroups', []) for g in v.split()]


class AutoTuneIdentityTest(unittest.TestCase):
    def setUp(self):
        self.svc = compose(AUTOTUNE)['Service']

    def test_distinct_user_and_audio_group(self):
        self.assertEqual(self.svc['User'], ['octet-autotune'])
        self.assertNotIn(self.svc['User'][0], {'octet', 'upmpdcli', 'octet-dsp-proxy'})
        self.assertEqual(self.svc['Group'], ['audio'])

    def test_dsp_group_is_only_supplementary_group(self):
        self.assertEqual(groups(dict(Service=self.svc)), [DSP_GROUP])

    def test_state_directory_and_paths_preserved(self):
        self.assertEqual(self.svc['StateDirectory'], ['octet-autotune'])
        self.assertEqual(self.svc['StateDirectoryMode'], ['0750'])
        self.assertEqual(self.svc['UMask'], ['0027'])
        self.assertEqual(self.svc['WorkingDirectory'], ['/opt/octet-measure'])
        self.assertEqual(self.svc['Environment'][0], 'OCTET_TUNE_DATA=/var/lib/octet-autotune')

    def test_exec_start_untouched(self):
        self.assertEqual(self.svc['ExecStart'],
                         ['/opt/octet-tune-venv/bin/python -m octetmeasure.autotune.web'])

    def test_no_broad_access(self):
        text = AUTOTUNE.read_text()
        for bad in ('chmod', '777', 'sudo', 'User=octet\n', 'octet-airplay', 'octet-bluetooth'):
            self.assertNotIn(bad, text)


class RemoteDspDropinTest(unittest.TestCase):
    def setUp(self):
        self.dsp = compose(REMOTE_DSP)['Service']
        self.base = REMOTE_BASE
        self.airplay = install_airplay.REMOTE_OVERRIDE
        self.bluetooth = install_bluetooth.dropin()

    def test_only_supplements_dsp_group(self):
        self.assertEqual(list(self.dsp), ['SupplementaryGroups'])
        self.assertEqual(self.dsp['SupplementaryGroups'], [DSP_GROUP])

    def test_never_resets_or_touches_runtime_or_identity(self):
        text = '\n'.join(l for l in REMOTE_DSP.read_text().splitlines() if not l.startswith('#'))
        self.assertNotIn('SupplementaryGroups=\n', text + '\n')
        for key in ('RuntimeDirectory', 'Group=', 'User=', 'ExecStop'):
            self.assertNotIn(key, text)

    def test_effective_compose_order_independent_of_dsp_position(self):
        # systemd applies drop-ins in filename order: airplay, bluetooth, then octet-remote-dsp.
        for order in ((self.airplay, self.bluetooth, REMOTE_DSP),
                      (REMOTE_DSP, self.airplay, self.bluetooth)):
            svc = compose(self.base, *order)
            have = set(groups(svc))
            expected = {'audio', 'upmpdcli', 'octet-bluetooth', DSP_GROUP}
            self.assertTrue(expected <= have, have)
            self.assertEqual(svc['Service']['User'], ['upmpdcli'])
            self.assertEqual(svc['Service']['Group'], ['octet-airplay'])

    def test_runtime_directory_unchanged_by_dsp(self):
        without = compose(self.base, self.airplay, self.bluetooth)['Service']
        with_dsp = compose(self.base, self.airplay, self.bluetooth, REMOTE_DSP)['Service']
        for key in ('RuntimeDirectory', 'RuntimeDirectoryMode', 'ExecStopPost', 'Group', 'User'):
            self.assertEqual(without.get(key), with_dsp.get(key), key)

    def test_dropin_sorts_after_existing_dropins_and_is_not_installed_here(self):
        names = sorted(['airplay.conf', 'bluetooth.conf', REMOTE_DSP.name])
        self.assertEqual(names[-1], REMOTE_DSP.name)
        for mod in (install_airplay, install_bluetooth):
            self.assertNotIn('octet-remote-dsp', Path(mod.__file__).read_text())


class SourceOnlyNoticeTest(unittest.TestCase):
    def test_files_document_installer_requirements(self):
        for path in (AUTOTUNE, REMOTE_DSP):
            text = path.read_text()
            self.assertIn('SOURCE-ONLY', text)
            for needle in ('atomic installer', 'UID/GID', 'backing up', 'rollback', 'before any unit restart'):
                self.assertIn(needle, text, (path.name, needle))


if __name__ == '__main__':
    unittest.main()
