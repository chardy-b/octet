import array
import json
from threading import RLock
import unittest
from unittest.mock import MagicMock, patch

from input_levels import CaptureProbe, InputLevels, pcm_levels


def sources():
    return [dict(id=f'usb:iD14:mono-{channel+1}',group='Audient iD14',device='hw:CARD=iD14,DEV=0',
                 channels=10,mapping=[channel,channel],rates=[48000]) for channel in range(2)]


class InputMeterTests(unittest.TestCase):
    def hub(self, capture='hw:Loopback,1,0'):
        dsp=MagicMock()
        dsp.__enter__.return_value=dsp
        def call(name,*args):
            if name=='GetConfigJson':return json.dumps({'devices':{'capture':{'device':capture},'playback':{'channels':8}}})
            if name=='GetState':return 'Running'
            if name=='GetSignalLevelsSince':return dict(capture_rms=[-20,-100],capture_peak=[-.001,-90],
                                                       playback_rms=[-40-channel for channel in range(8)],
                                                       playback_peak=[-20-channel for channel in range(8)])
        dsp.call.side_effect=call
        probe=MagicMock()
        probe.process.poll.return_value=None
        probe.read.return_value=[dict(rms_db=-30,peak_db=-12,clipped=False),dict(rms_db=-110,peak_db=-95,clipped=False)]
        tuning=MagicMock(return_value=False)
        hub=InputLevels(RLock(),lambda:dsp,sources,tuning,probe_factory=MagicMock(return_value=probe))
        return hub,dsp,probe

    def test_pcm_channel_separation_full_scale_and_silence(self):
        data=array.array('i',[1073741824,0,2147483392,0,-1073741824,0,-2147483648,0]).tobytes()
        levels=pcm_levels(data,2,[0,1])
        self.assertAlmostEqual(levels[0]['peak_db'],0)
        self.assertAlmostEqual(levels[0]['rms_db'],-2.0412,places=3)
        self.assertTrue(levels[0]['clipped'])
        self.assertEqual(levels[1],dict(peak_db=-120,rms_db=-120,clipped=False))
        levels=pcm_levels(array.array('i',[1073741824]*4).tobytes(),1,[0])
        self.assertAlmostEqual(levels[0]['rms_db'],-6.0206,places=3)
        self.assertFalse(levels[0]['clipped'])

    def test_one_reader_for_interface_and_both_inputs_while_tidal_is_selected(self):
        hub,dsp,probe=self.hub()
        for _ in range(3):frame=hub._step()
        hub.probe_factory.assert_called_once()
        self.assertEqual([row['status'] for row in frame['inputs']],['Signal','Quiet'])
        self.assertEqual([row['peak_db'] for row in frame['inputs']],[-12,-95])
        self.assertEqual(len(frame['outputs']),8)
        self.assertEqual([row['peak_db'] for row in frame['outputs']],[-20-channel for channel in range(8)])
        hub.release()
        probe.close.assert_called_once()
        self.assertEqual(hub.probes,{})

    def test_selected_interface_uses_dsp_levels_including_unused_input(self):
        hub,dsp,probe=self.hub('hw:CARD=iD14,DEV=0')
        frame=hub._step()
        hub.probe_factory.assert_not_called()
        self.assertEqual(len(frame['inputs']),2)
        self.assertEqual(frame['inputs'][0]['clip_seq'],1)
        self.assertEqual(frame['inputs'][1]['clip_seq'],0)
        # Muting and output volume are never queried or changed by metering.
        self.assertEqual(set(call.args[0] for call in dsp.call.call_args_list),{'GetConfigJson','GetSignalLevelsSince','GetState'})

    def test_reader_is_released_before_dsp_takes_capture(self):
        hub,dsp,probe=self.hub()
        hub._step()
        dsp.call.side_effect=lambda name,*args: json.dumps({'devices':{'capture':{'device':'hw:CARD=iD14,DEV=0'}}}) if name=='GetConfigJson' else 'Running' if name=='GetState' else dict(capture_rms=[-20,-100],capture_peak=[-10,-90])
        hub.checked=0
        hub._step()
        probe.close.assert_called_once()
        self.assertEqual(hub.probes,{})

    def test_busy_input_is_unavailable_and_retries_are_bounded(self):
        hub,dsp,probe=self.hub()
        hub.probe_factory.side_effect=OSError('busy')
        frame=hub._step()
        hub._step()
        hub.probe_factory.assert_called_once()
        self.assertTrue(all(not row['available'] for row in frame['inputs']))
        self.assertTrue(all(row['peak_db']==-120 for row in frame['inputs']))

    def test_auto_tune_releases_capture_and_does_not_open_a_reader(self):
        hub,dsp,probe=self.hub()
        hub._step()
        hub.audio_owned_callback.return_value=True
        hub.checked=0
        frame=hub._step()
        probe.close.assert_called_once()
        self.assertIn('Auto Tune',frame['message'])
        self.assertTrue(all(not row['available'] for row in frame['inputs']))

    def test_short_clip_is_retained_until_sampler_drains_the_reader(self):
        process=MagicMock()
        clipped=array.array('i',[2147483392,0]*10).tobytes()
        quiet=array.array('i',[0,0]*10).tobytes()
        process.stdout.read.side_effect=[clipped,quiet,b'']
        process.poll.return_value=0
        device=dict(device='hw:CARD=test,DEV=0',channels=2,rates=[48000],inputs=[dict(channel=0),dict(channel=1)])
        with patch('input_levels.subprocess.Popen',return_value=process):
            probe=CaptureProbe(device)
            probe.thread.join(timeout=1)
            levels=probe.read()
            self.assertTrue(levels[0]['clipped'])
            self.assertGreater(levels[0]['peak_db'],-.01)
            self.assertFalse(levels[1]['clipped'])
            self.assertFalse(probe.read()[0]['clipped'])
            probe.close()

    def test_all_eight_output_channels_keep_their_order_and_clip_independently(self):
        hub,_,_=self.hub()
        hub.config={'devices':{'playback':{'channels':8,'labels':['Custom left']}}}
        levels=dict(playback_rms=[-45]*8,playback_peak=[-30,-20,0,-10,-120,-50,-3,-.001])
        rows=hub._outputs(levels,True)
        self.assertEqual([row['label'] for row in rows],[f'Output {channel}' for channel in range(1,9)])
        self.assertEqual(rows[0]['device'],'Custom left')
        self.assertEqual(rows[4]['device'],'Subwoofer · Left')
        self.assertEqual([row['clip_seq'] for row in rows],[0,0,1,0,0,0,0,1])
        self.assertTrue(all(row['available'] for row in rows))
        # A quiet follow-up cannot erase a short overload from the clip sequence.
        later=hub._outputs(dict(playback_rms=[-120]*8,playback_peak=[-120]*8),True)
        self.assertEqual(later[2]['clip_seq'],1)

    def test_stalled_missing_and_nonfinite_output_levels_do_not_show_old_signal(self):
        hub,_,_=self.hub()
        hub.config={'devices':{'playback':{'channels':8}}}
        levels=dict(playback_rms=[float('nan'),-40],playback_peak=[float('inf'),-20])
        rows=hub._outputs(levels,True)
        self.assertEqual(rows[0]['peak_db'],-120)
        self.assertEqual(rows[0]['clip_seq'],0)
        self.assertTrue(all(not row['available'] for row in rows[2:]))
        self.assertTrue(all(not row['available'] for row in hub._outputs(levels,False)))


if __name__=='__main__':unittest.main()
