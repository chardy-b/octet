import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dac_ladder
import dac_reboot

REAL_STALLED = dac_ladder.stalled


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        boot = self.root / 'boot'
        boot.write_text('boot')
        loop = self.root / 'pcm0p/sub0/status'
        loop.parent.mkdir(parents=True)
        loop.write_text('closed')
        self.broker = SimpleNamespace(STATE=self.root, BOOT_ID=boot, COOLDOWN=3600,
                    run=Mock(return_value='--mute'), fault_evidence=Mock(return_value=['dma errors']),
                    save_json=dac_reboot.save_json, LOOPBACK=self.root,
                    pcm_sample=Mock(side_effect=[('123','RUNNING',n) for n in range(100)]))
        self.mute = patch('dac_ladder.muted_dsp').start()
        self.stall = patch('dac_ladder.stalled').start()
        self.graph = patch('dac_ladder.graph').start()
        self.addCleanup(patch.stopall)

    def record(self):
        return json.loads((self.root/'ladder.json').read_text())

    def test_no_driver_step_without_a_prior_stream_attempt(self):
        dac_ladder.handle(self.broker,b'begin')
        with self.assertRaisesRegex(RuntimeError,'out of order'):
            dac_ladder.handle(self.broker,b'driver')
        self.assertEqual(self.record()['stage'],'begin')

    def test_restart_does_not_reset_driver_and_finish_does_not_restart_dsp(self):
        dac_ladder.handle(self.broker,b'begin')
        self.broker.run.reset_mock()
        dac_ladder.handle(self.broker,b'stream')
        self.assertIn(('/usr/bin/systemctl','restart','camilladsp.service'),
                      [c.args for c in self.broker.run.call_args_list])
        self.assertEqual(self.record()['stage'],'stream')
        self.broker.run.reset_mock()
        with patch('dac_ladder.time.sleep'):
            dac_ladder.handle(self.broker,b'finish')
        self.assertFalse(any('restart' in c.args for c in self.broker.run.call_args_list))
        self.assertEqual(self.record()['stage'],'finish')

    def test_repeated_transactions_are_rate_limited_even_when_first_step_fails(self):
        dac_ladder.handle(self.broker,b'begin')
        self.broker.run.side_effect=lambda *args,**kw: (_ for _ in ()).throw(OSError('systemd unavailable')) if 'stop' in args else '--mute'
        with self.assertRaises(OSError):dac_ladder.handle(self.broker,b'stream')
        self.assertEqual(self.record()['stage'],'stream')
        with self.assertRaisesRegex(RuntimeError,'cooldown'):dac_ladder.handle(self.broker,b'begin')

    def test_missing_kernel_errors_allow_local_recovery_but_evidence_is_explicit(self):
        self.broker.fault_evidence.side_effect=RuntimeError('no DMA errors')
        dac_ladder.handle(self.broker,b'begin')
        self.assertEqual(self.record()['kernel'],[])

    def test_still_running_mpd_or_lost_mute_prevents_privileged_steps(self):
        dac_ladder.handle(self.broker,b'begin')
        (self.root/'pcm0p/sub0/status').write_text('state: RUNNING')
        with self.assertRaisesRegex(RuntimeError,'MPD'):dac_ladder.handle(self.broker,b'stream')
        self.assertEqual(self.record()['stage'],'begin')
        (self.root/'pcm0p/sub0/status').write_text('closed')
        self.mute.side_effect=RuntimeError('not muted')
        with self.assertRaisesRegex(RuntimeError,'not muted'):dac_ladder.handle(self.broker,b'stream')
        self.assertEqual(self.record()['stage'],'begin')

    def test_cast_teardown_can_recover_without_a_restart(self):
        dac_ladder.handle(self.broker,b'begin')
        with patch('dac_ladder.time.sleep'):
            dac_ladder.handle(self.broker,b'finish')
        self.assertEqual(self.record()['stage'],'finish')
        self.assertFalse(any('restart' in call.args for call in self.broker.run.call_args_list))

    def test_driver_validation_is_required_before_dsp_stop_or_sysfs_writes(self):
        dac_ladder.handle(self.broker,b'begin')
        dac_ladder.handle(self.broker,b'stream')
        self.broker.run.reset_mock()
        with patch('dac_ladder.require_driver_validation',side_effect=RuntimeError('unvalidated')),\
             patch('dac_ladder.rebind') as rebind:
            with self.assertRaisesRegex(RuntimeError,'unvalidated'):dac_ladder.handle(self.broker,b'driver')
        rebind.assert_not_called()
        self.assertEqual(self.record()['stage'],'driver')
        self.assertNotIn(('/usr/bin/systemctl','stop','camilladsp.service'),
                         [call.args for call in self.broker.run.call_args_list])

    def test_driver_reattaches_both_devices_even_after_partial_unbind_failure(self):
        machine=self.root/'machine';i2s=self.root/'i2s'
        for driver,device,module in ((machine,dac_ladder.MACHINE_ID,'snd_soc_rpi_simple_soundcard'),
                                      (i2s,dac_ladder.I2S_ID,'designware_i2s')):
            driver.mkdir()
            target=self.root/device;target.mkdir()
            (driver/device).symlink_to(target,target_is_directory=True)
            (driver/'module').symlink_to(self.root/module,target_is_directory=True)
            (driver/'bind').touch();(driver/'unbind').touch()
        real=Path.write_text
        writes=[]
        def write(path,data,*args,**kwargs):
            if path.parent not in (machine,i2s):return real(path,data,*args,**kwargs)
            writes.append((path.parent.name,path.name,data))
            link=path.parent/data
            if path.name=='unbind':
                link.unlink()
                if path.parent==i2s:raise OSError('partial unbind')
            else:link.symlink_to(self.root/data,target_is_directory=True)
            return len(data)
        with patch.object(dac_ladder,'MACHINE',machine),patch.object(dac_ladder,'I2S',i2s),\
             patch('dac_ladder.owner_free'),patch.object(Path,'write_text',write):
            with self.assertRaisesRegex(OSError,'partial unbind'):dac_ladder.rebind(self.broker)
        self.assertTrue((machine/dac_ladder.MACHINE_ID).exists())
        self.assertTrue((i2s/dac_ladder.I2S_ID).exists())
        self.assertEqual([w[1] for w in writes],['unbind','unbind','bind','bind'])

    def test_busy_pcm_prevents_sysfs_writes(self):
        self.broker.DAC_STATUS=self.root/'dac'
        self.broker.DAC_STATUS.write_text('state: RUNNING')
        with patch.object(Path,'write_text') as write:
            with self.assertRaisesRegex(RuntimeError,'owner-free'):dac_ladder.rebind(self.broker)
        write.assert_not_called()

    def test_stalled_pcm_must_belong_to_the_systemd_camilla_thread_group(self):
        self.broker.pcm_sample=Mock(return_value=('123','RUNNING','3'))
        self.broker.run.return_value='999\n'
        with patch('dac_ladder.Path') as path,patch('dac_ladder.time.sleep'):
            path.return_value.read_text.return_value='Pid:\t123\nTgid:\t999\n'
            REAL_STALLED(self.broker)
            self.broker.run.return_value='888\n'
            with self.assertRaisesRegex(RuntimeError,'not owned'):REAL_STALLED(self.broker)


if __name__=='__main__':unittest.main()
