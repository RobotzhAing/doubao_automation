"""Video library presentation uses disposable files and state only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard
from doubao_automation import video_archive


class VideoLibraryDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='video-library-display-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.videos = self.root / 'videos'
        self.videos.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.patches = [patch.object(dashboard, 'VIDEOS_DIR', self.videos),
                        patch.object(dashboard, 'STATE_DIR', self.state),
                        patch.object(dashboard, 'WORKSPACE', self.root),
                        patch.object(dashboard, 'LOGS_DIR', self.root / 'logs'),
                        patch.object(dashboard, 'SCRIPTS_DIR', self.root / 'scripts'),
                        patch.object(dashboard, 'FRAMES_DIR', self.root / 'frames'),
                        patch.object(dashboard.postprocess, 'STORE_PATH', self.state / 'postprocess.json'),
                        patch.object(dashboard.postprocess, 'JOBS_DIR', self.root / 'postprocess'),
                        patch.object(dashboard, 'proc', None),
                        patch.object(dashboard, 'readonly_postprocess_snapshots', return_value={})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = dashboard.app.test_client()
        self.client.environ_base.update(HTTP_ORIGIN='http://localhost', HTTP_X_CSRF_TOKEN=dashboard.CSRF_TOKEN)

    def save(self, value):
        (self.state / 'pipeline_state.json').write_text(json.dumps(value))

    def video(self, name):
        path = self.videos / name
        path.write_bytes(b'disposable fixture, not playable video')
        return path

    def listing(self):
        response = self.client.get('/api/videos', headers={'Host': 'localhost'})
        self.assertEqual(response.status_code, 200)
        return response.json['videos']

    def test_finished_clip_hides_only_its_raw_copy_and_preserves_both_files(self):
        raw = self.video('current_seg0.raw.mp4')
        final = self.video('current_seg0.mp4')
        pending = self.video('pending_seg0.raw.mp4')
        result = self.listing()
        self.assertEqual({v['name'] for v in result}, {final.name, pending.name})
        self.assertTrue(raw.is_file())
        self.assertEqual(raw.read_bytes(), b'disposable fixture, not playable video')

    def test_current_clips_have_script_and_segment_labels_without_renaming(self):
        first = self.video('long_run_script_one_seg0.mp4')
        second = self.video('long_run_script_two_seg1.raw.mp4')
        self.save({'scripts': [{'id': 'long_run_script_one', 'keyword': 'OWALA保温杯'},
                               {'id': 'long_run_script_two', 'keyword': 'OWALA保温杯'}],
                   'tasks': [{'script_id': 'long_run_script_one', 'seg_index': 0,
                              'seg_total': 1, 'video': str(first)},
                             {'script_id': 'long_run_script_two', 'seg_index': 1,
                              'seg_total': 3}]})
        result = {v['name']: v for v in self.listing()}
        self.assertEqual(result[first.name]['label'], 'OWALA保温杯 · 脚本1 · 片段1/1')
        self.assertEqual(result[second.name]['label'], 'OWALA保温杯 · 脚本2 · 片段2/3（原始下载）')
        self.assertEqual(result[second.name]['path'], str(second.resolve()))
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_unmapped_history_keeps_its_actual_filename(self):
        old = self.video('历史成片.mp4')
        self.save({'scripts': [], 'tasks': []})
        result = self.listing()
        self.assertEqual(result[0]['label'], old.name)
        self.assertEqual(result[0]['name'], old.name)

    def test_same_minute_runs_have_separate_clean_folders(self):
        a, b = 'r20261113_120002_abcd', 'r20261113_120057_2345'
        first = self.video(a + '_s0_1_seg0.mp4')
        raw = self.video(a + '_s0_1_seg0.raw.mp4')
        binding = self.video(raw.name + '.asset.json')
        self.video(a + '_s0_1_seg0.rendering_abc.mp4')
        second = self.video(b + '_s0_1_seg0.mp4')
        batches = self.client.get('/api/videos').json['batches']
        self.assertEqual([x['id'] for x in batches], [b, a])
        self.assertTrue(all(x['label'] == '2026-11-13 12:00' and x['count'] == 1 for x in batches))
        self.assertFalse((self.videos / 'batches').exists(), 'list requests are read-only')
        self.assertEqual(video_archive.backfill(self.videos), 2)
        with patch.object(dashboard, 'open_video_folder') as shell:
            result = self.client.post('/api/video-batches/open', json={'id': a})
        self.assertEqual(result.status_code, 200, result.json)
        folder = Path(result.json['folder'])
        shell.assert_called_once_with(folder)
        self.assertEqual({f.name for f in folder.iterdir()}, {first.name})
        self.assertTrue((folder / first.name).samefile(first))
        self.assertTrue(all(f.is_file() for f in (second, raw, binding)))

    def test_delete_removes_selected_batch_artifacts_and_history_only(self):
        a, b = 'r20261113_120002_abcd', 'r20261113_120057_2345'
        stem = a + '_s0_1_seg0'
        original = self.video(stem + '.mp4')
        raw = self.video(stem + '.raw.mp4')
        sidecar = self.video(raw.name + '.asset.json')
        other = self.video(b + '_s0_1_seg0.mp4')
        backups = self.videos / '.download-backups'
        backups.mkdir()
        backup = backups / (raw.name + '.abc.unverified')
        backup.write_bytes(b'backup')
        sibling = backups / (other.name + '.abc.unverified')
        sibling.write_bytes(b'other backup')
        logs = self.root / 'logs'
        logs.mkdir()
        log = logs / (a + '.log')
        log.write_text('test')
        request_id = 'request_' + 'a' * 32
        diagnostic = self.root / 'generation_requests' / request_id / 'message_structure.json'
        diagnostic.parent.mkdir(parents=True)
        diagnostic.write_text('{}')
        self.save(dict(run_id=a, tasks=[dict(id=a+'_s0_1#seg0', status='done')], scripts=[dict(id=a+'_s0_1')],
                       archived_runs=[dict(run_id=a), dict(run_id=b)], products=[{'keep': True}]))
        ledger = dict(accounts={'a': {'quota_days': {'today': {'entries': {
            'e': dict(task_id=a+'_s0_1#seg0', request_id=request_id, state='charged')}}}}})
        ledger_file = self.state / 'accounts.json'
        ledger_file.write_text(json.dumps(ledger))
        video_archive.backfill(self.videos)
        batches = video_archive.inventory(self.videos)
        folder = batches[a]['folder']
        response = self.client.post('/api/video-batches/delete', json={'id': a})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(all(not f.exists() for f in (original, raw, sidecar, backup, log, folder, diagnostic)))
        self.assertTrue(all(f.exists() for f in (other, sibling, batches[b]['folder'])))
        state = dashboard.store().snapshot()
        self.assertEqual(state['archived_runs'], [dict(run_id=b)])
        self.assertEqual((state['run_id'], state['tasks'], state['products']), ('', [], [{'keep': True}]))
        self.assertEqual(json.loads(ledger_file.read_text()), ledger)
        self.assertEqual([x['id'] for x in self.client.get('/api/videos').json['batches']], [b])

    def test_unresolved_or_processing_batch_and_redirected_folder_are_protected(self):
        batch = 'r20261113_120002_abcd'
        finished = self.video(batch + '_s0_1_seg0.mp4')
        ledger = self.state / 'accounts.json'
        ledger.write_text(json.dumps({'accounts': {'a': {'quota_days': {'today': {'entries': {
            'e': dict(task_id=batch+'_s0_1#seg0', state='unknown')}}}}}}))
        url = '/api/video-batches/delete'
        self.assertEqual(self.client.post(url, json={'id': batch}).status_code, 409)
        ledger.unlink()
        with dashboard.postprocess.store().txn() as jobs:
            jobs['jobs'] = [dict(source=str(finished.resolve()), status='running')]
        self.assertEqual(self.client.post(url, json={'id': batch}).status_code, 409)
        with dashboard.postprocess.store().txn() as jobs:
            jobs['jobs'] = []
        outside = self.root / 'outside'
        outside.mkdir()
        (self.videos / 'batches').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.client.post(url, json={'id': batch}).status_code, 409)
        self.assertEqual(self.client.post(url, json={'id': '../outside'}).status_code, 404)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(finished.is_file())


if __name__ == '__main__':
    unittest.main()
