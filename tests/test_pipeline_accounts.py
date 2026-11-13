"""Account requirements apply to generation, not preparation or status commands."""
import copy
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import run_pipeline
from doubao_automation import orchestrator
from doubao_automation.config import require_doubao_accounts


class PipelineAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='doubao-pipeline-accounts-')
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(orchestrator, 'STATE_DIR', self.root),
                        patch.object(orchestrator, 'ensure_dirs')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_validation_distinguishes_missing_accounts_from_disabled_accounts(self):
        cases = [({}, 'accounts.doubao 为空'),
                 ({'accounts': {'doubao': []}}, 'accounts.doubao 为空'),
                 ({'accounts': {'doubao': None}}, 'accounts.doubao 为空'),
                 ({'accounts': {'doubao': [dict(name='fixture', enabled=False)]}}, '均未启用')]
        for cfg, message in cases:
            with self.subTest(cfg=cfg):
                before = copy.deepcopy(cfg)
                with self.assertRaisesRegex(ValueError, message) as error:
                    require_doubao_accounts(cfg)
                self.assertIn('账号库', str(error.exception))
                self.assertEqual(cfg, before)

    def test_validation_preserves_legacy_enabled_default_and_filters_disabled(self):
        accounts = [dict(name='disabled', enabled=False), dict(name='legacy'),
                    dict(name='enabled', enabled=True)]
        self.assertEqual(require_doubao_accounts({'accounts': {'doubao': accounts}}), accounts[1:])

    def test_preparation_and_status_work_without_enabled_doubao_accounts(self):
        for accounts in ([], [dict(name='fixture', enabled=False)]):
            with self.subTest(accounts=accounts):
                pipeline = orchestrator.Pipeline({'accounts': {'doubao': accounts}})
                with pipeline.store.txn() as data:
                    data.update(products=[dict(keyword='fixture', images=['fixture.png'])],
                                sources=[dict(record_id='fixture')],
                                scripts=[dict(id='s1', keyword='fixture', text='fixture script')],
                                tasks=[])
                segment = dict(index=0, total=1, prompt='fixture prompt', narration='', est_seconds=5)
                with patch.object(orchestrator, 'FeishuClient') as feishu, \
                     patch.object(orchestrator, 'BrowserSession') as browser, \
                     patch.object(orchestrator, 'build_segments', return_value=[segment]), \
                     redirect_stdout(io.StringIO()) as output:
                    pipeline.stage_fetch()
                    pipeline.stage_scripts()
                    pipeline.stage_plan()
                    pipeline.report()
                feishu.assert_not_called()
                browser.assert_not_called()
                self.assertEqual(len(pipeline.store.snapshot()['tasks']), 1)
                self.assertIn('暂无启用的豆包账号', output.getvalue())

    def test_generation_rejects_before_mutating_journal_or_running_any_stage(self):
        for accounts in ([], [dict(name='fixture', enabled=False)]):
            for method in ('stage_run', 'run_all'):
                with self.subTest(accounts=accounts, method=method):
                    pipeline = orchestrator.Pipeline({'accounts': {'doubao': accounts}})
                    pipeline.store.reset({'scripts': [{'id': 'saved'}], 'tasks': [{'id': 'saved#0'}]})
                    before = pipeline.store.snapshot()
                    with patch.object(pipeline, '_run_lock') as runner_lock, \
                         patch.object(pipeline, 'stage_fetch') as fetch, \
                         patch.object(pipeline, 'stage_scripts') as scripts:
                        with self.assertRaises(ValueError):
                            getattr(pipeline, method)()
                    runner_lock.assert_not_called()
                    fetch.assert_not_called()
                    scripts.assert_not_called()
                    self.assertEqual(pipeline.store.snapshot(), before)

    def test_enabled_account_reaches_generation_stage(self):
        pipeline = orchestrator.Pipeline({'accounts': {'doubao': [dict(name='fixture')]}})
        with patch.object(pipeline, '_stage_run_locked') as stage:
            pipeline.stage_run()
        stage.assert_called_once_with()

    def test_waiting_runner_retries_only_after_quota_reset_wait(self):
        pipeline = orchestrator.Pipeline({'accounts': {'doubao': [dict(name='fixture')]}})
        with patch.object(pipeline, '_stage_run_locked', return_value='complete') as run, \
             patch.object(pipeline, '_can_wait_for_quota_reset', side_effect=[True, False]), \
             patch.object(pipeline, '_seconds_until_quota_reset', return_value=12.5), \
             patch.object(pipeline, '_wait_for_next_quota_day') as wait:
            self.assertEqual(pipeline.stage_run_waiting_for_quota_reset(), 'complete')
        run.assert_called_once_with()
        wait.assert_called_once_with()

    def test_waiting_runner_rechecks_clean_pending_after_quota_exhaustion(self):
        pipeline = orchestrator.Pipeline({'accounts': {'doubao': [dict(name='fixture')]}})
        quota_exhausted = RuntimeError('流程未完成：完成2/4，待恢复2，失败0；未决请求只恢复原会话，不重新生成')
        with patch.object(pipeline, '_stage_run_locked', side_effect=[quota_exhausted, 'complete']) as run, \
             patch.object(pipeline, '_can_wait_for_quota_reset', side_effect=[False, True, False]), \
             patch.object(pipeline, '_wait_for_next_quota_day') as wait:
            self.assertEqual(pipeline.stage_run_waiting_for_quota_reset(), 'complete')
        self.assertEqual(run.call_count, 2)
        wait.assert_called_once_with()

    def test_waiting_runner_surfaces_non_quota_failures_without_sleeping(self):
        pipeline = orchestrator.Pipeline({'accounts': {'doubao': [dict(name='fixture')]}})
        with patch.object(pipeline, '_stage_run_locked', side_effect=RuntimeError('needs operator action')), \
             patch.object(pipeline, '_can_wait_for_quota_reset', return_value=False), \
             patch.object(pipeline, '_wait_for_next_quota_day') as wait:
            with self.assertRaisesRegex(RuntimeError, 'needs operator action'):
                pipeline.stage_run_waiting_for_quota_reset()
        wait.assert_not_called()

    def test_quota_reset_deadline_uses_beijing_midnight(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 12, 23, 59, 59,
                       tzinfo=timezone(timedelta(hours=8), 'Asia/Shanghai'))
        self.assertEqual(orchestrator.Pipeline._seconds_until_quota_reset(now), 2.0)

    def test_quota_wait_gate_requires_only_clean_pending_tasks_and_zero_quota(self):
        pipeline = orchestrator.Pipeline({'accounts': {'doubao': [dict(name='fixture')]}})
        pipeline.store.reset({'tasks': [dict(id='task-1', status='pending', request_id='')],
                              'requests': {}})
        with patch.object(pipeline.accounts, 'status', return_value={'status': 'available'}), \
             patch.object(pipeline.accounts, 'quota', return_value={
                 'remaining': 0, 'pending_recovery': 0, 'unknown': 0, 'awaiting_approval': 0}):
            self.assertTrue(pipeline._can_wait_for_quota_reset())
        with patch.object(pipeline.accounts, 'status', return_value={'status': 'available'}), \
             patch.object(pipeline.accounts, 'quota', return_value={
                 'remaining': 1, 'pending_recovery': 0, 'unknown': 0, 'awaiting_approval': 0}):
            self.assertFalse(pipeline._can_wait_for_quota_reset())

        pipeline.store.reset({'tasks': [dict(id='task-1', status='awaiting_result', request_id='rid')],
                              'requests': {'rid': dict(phase='unknown', task_ids=['task-1'])}})
        with patch.object(pipeline.accounts, 'status', return_value={'status': 'available'}), \
             patch.object(pipeline.accounts, 'quota', return_value={
                 'remaining': 0, 'pending_recovery': 0, 'unknown': 1, 'awaiting_approval': 0}):
            self.assertFalse(pipeline._can_wait_for_quota_reset())

    def test_cli_exposes_opt_in_wait_for_quota_reset(self):
        cfg = {'accounts': {'doubao': [dict(name='fixture')]}}
        with patch.object(sys, 'argv', ['run_pipeline.py', 'run', '--wait-for-quota-reset']), \
             patch.object(run_pipeline, 'load_config', return_value=cfg), \
             patch.object(run_pipeline, 'ensure_dirs'), \
             patch.object(run_pipeline, 'use_workspace_temp'), \
             patch.object(run_pipeline, 'Pipeline') as pipeline_cls:
            self.assertEqual(run_pipeline.main(), 0)
        pipeline_cls.return_value.stage_run_waiting_for_quota_reset.assert_called_once_with()
        pipeline_cls.return_value.stage_run.assert_not_called()

    def test_cli_explains_disabled_accounts_without_traceback(self):
        cfg = {'accounts': {'doubao': [dict(name='fixture', enabled=False)]}}
        with patch.object(sys, 'argv', ['run_pipeline.py', 'run']), \
             patch.object(run_pipeline, 'load_config', return_value=cfg), \
             patch.object(run_pipeline, 'ensure_dirs'), \
             patch.object(run_pipeline, 'use_workspace_temp'), \
             redirect_stderr(io.StringIO()) as error:
            result = run_pipeline.main()
        self.assertEqual(result, 2)
        self.assertIn('均未启用', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())


if __name__ == '__main__':
    unittest.main()
