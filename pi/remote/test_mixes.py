from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import server
from test_playlists import Player, Playlists, track


class Mix:
    def __init__(self, identifier='daily_mix_1', title='My Mix 1', items=None, mix_type='DAILY_MIX'):
        self.id, self.title, self.mix_type = identifier, title, mix_type
        self.rows = items or []

    def items(self):
        return self.rows

    def image(self, size):
        return 'https://resources.tidal.com/images/cover.jpg'


class Mixes(unittest.TestCase):
    def setUp(self):
        sdk = SimpleNamespace(Track=SimpleNamespace, Mix=Mix, MixV2=Mix)
        patcher = patch.dict('sys.modules', tidalapi=sdk)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mix_ids_are_validated_before_requesting_tidal(self):
        for value in ('mix:../other', 'mix:', 'mix:abc\nstop', 'mix:abc/path', 'mix:' + 'a' * 129):
            with self.subTest(value=value), patch.object(server, 'tidal') as tidal:
                with self.assertRaises(ValueError):
                    server.playlist_contents(value)
                tidal.assert_not_called()
        self.assertEqual(server.playlist_id('mix:daily_mix-1'), 'mix:daily_mix-1')

    def test_my_mixes_keeps_order_deduplicates_and_omits_video_mixes(self):
        daily, personal = Mix('discovery', 'My Daily Discovery'), Mix()
        video = Mix('video', mix_type='VIDEO_DAILY_MIX')
        page = SimpleNamespace(categories=[SimpleNamespace(items=[daily, personal, video, object()]),
                                           SimpleNamespace(items=[personal])])
        session = SimpleNamespace(mixes=Mock(return_value=page))
        with patch.object(server, 'tidal', return_value=session):
            rows = server.my_mixes()['mixes']
        self.assertEqual([row['id'] for row in rows], ['mix:discovery', 'mix:daily_mix_1'])
        self.assertIsNone(rows[0]['tracks'])

    def test_mix_tracks_filter_unavailable_audio_and_video_and_cache(self):
        mix = Mix(items=[track(1), object(), track(2, available=False), track(3)])
        session = SimpleNamespace(mix=Mock(return_value=mix))
        with (patch.object(server, 'tidal', return_value=session),
              patch.object(server, 'PLAYLIST_CACHE', {}),
              patch.object(server, 'CATALOG', {})):
            result = server.playlist_contents('mix:daily_mix_1')
            self.assertEqual([row['id'] for row in result['tracks']], ['1', '3'])
            self.assertEqual(result['unavailable'], 2)
            self.assertEqual(result['playlist']['kind'], 'mix')
            self.assertIs(server.playlist_contents('mix:daily_mix_1'), result)
            session.mix.assert_called_once_with('daily_mix_1')
            server.playlist_contents('mix:daily_mix_1', refresh=True)
            self.assertEqual(session.mix.call_count, 2)

    def test_mix_uses_existing_playlist_playback_and_marks_context(self):
        contents = dict(playlist=dict(id='mix:daily_mix_1', kind='mix', title='My Mix 1'),
                        tracks=[dict(id='1'), dict(id='2')])
        with (patch.object(server, 'playlist_contents', return_value=contents),
              patch.object(server, 'play_context') as play):
            server.play_playlist(dict(id='mix:daily_mix_1', position=1, track_id='2'))
        play.assert_called_once_with(contents['tracks'], 1,
                                     dict(kind='mix', id='mix:daily_mix_1', title='My Mix 1'))

    def test_song_radio_starts_with_seed_and_deduplicates_available_recommendations(self):
        seed = track(9)
        seed.get_radio_mix = Mock(return_value=Mix('radio_9', items=[
            track(2), track(9), track(2), track(3, available=False), track(4)]))
        session = SimpleNamespace(track=Mock(return_value=seed))
        with (patch.object(server, 'tidal', return_value=session),
              patch.object(server, 'CATALOG', {}),
              patch.object(server, 'play_context') as play):
            server.play_radio(dict(id='9'))
        rows, position, source = play.call_args.args
        self.assertEqual([row['id'] for row in rows], ['9', '2', '4'])
        self.assertEqual((position, source['kind'], source['id']), (0, 'radio', '9'))

    def test_radio_preserves_manual_queue_before_recommendations(self):
        player, dsp = Player(), Playlists().dsp()
        seed = track(9)
        seed.get_radio_mix = Mock(return_value=Mix('radio_9', items=[track(2), track(4)]))
        with (tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)),
              patch.object(server, 'CATALOG', {}), patch.object(server, 'mpd', side_effect=player),
              patch.object(server, 'DSP', return_value=dsp), patch.object(server, 'current_status', return_value={}),
              patch.object(server.TUNING, 'status', return_value={'audio_owned': False}),
              patch.object(server, 'tidal', return_value=SimpleNamespace(track=Mock(return_value=seed)))):
            server.save_queue_context(dict(source={}, manual={'72': 'old-two'}, autoplay={}))
            server.action('play-radio', dict(id='9'))
            self.assertEqual([url.split('/stream/')[-1].split('?')[0] for _, url in player.queue],
                             ['9', 'old-two', '2', '4'])
            self.assertEqual(server.queue_context()['source']['kind'], 'radio')

    def test_radio_without_recommendations_leaves_playback_alone(self):
        seed = track(9)
        seed.get_radio_mix = Mock(return_value=Mix('radio_9', items=[track(9)]))
        with (patch.object(server, 'tidal', return_value=SimpleNamespace(track=Mock(return_value=seed))),
              patch.object(server, 'CATALOG', {}),
              patch.object(server, 'mpd') as mpd, patch.object(server, 'DSP') as dsp):
            with self.assertRaisesRegex(RuntimeError, 'no available radio recommendations'):
                server.play_radio(dict(id='9'))
            mpd.assert_not_called()
            dsp.assert_not_called()

    def test_unavailable_seed_does_not_start_radio(self):
        seed = track(9, available=False)
        seed.get_radio_mix = Mock()
        with (patch.object(server, 'tidal', return_value=SimpleNamespace(track=Mock(return_value=seed))),
              patch.object(server, 'play_context') as play):
            with self.assertRaisesRegex(RuntimeError, 'song is unavailable'):
                server.play_radio(dict(id='9'))
            seed.get_radio_mix.assert_not_called()
            play.assert_not_called()


if __name__ == '__main__':
    unittest.main()
