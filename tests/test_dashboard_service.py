"""Offline supervisor regressions: reload only an idle, verified worker."""
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dashboard_service as service


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(service, 'TEMP_DIR', root / 'temp'))
        self.stack.enter_context(patch.object(service, 'LOGS_DIR', root / 'logs'))
        self.stack.enter_context(patch.object(service, 'validate_sources'))
        self.revision = self.stack.enter_context(patch.object(service, 'revision', return_value=('initial',)))
        self.port = self.stack.enter_context(patch.object(service, 'local_port_in_use', return_value=False))
        self.worker = Mock(pid=12345)
        self.worker.poll.return_value = None
        self.api_state=self.stack.enter_context(patch.object(service,'worker_state',return_value={
            'server_pid':self.worker.pid,'service_pid':service.os.getpid(),
            'project_root':str(service.ROOT),'data_root':str(service.DATA_ROOT),
            'running':True,'login_windows_open':False,'postprocessing':False,
        }))
        self.prepare=self.stack.enter_context(patch.object(service,'request_worker_reload',return_value=(True,'')))
        self.start = self.stack.enter_context(patch.object(service, 'start_worker', return_value=self.worker))
        self.publish = self.stack.enter_context(patch.object(service, 'publish'))
        self.sleep = self.stack.enter_context(patch.object(service.time, 'sleep', side_effect=KeyboardInterrupt))
        self.run = self.stack.enter_context(patch.object(service.subprocess, 'run'))
        self.popen = self.stack.enter_context(patch.object(service.subprocess, 'Popen'))
        self.kill = self.stack.enter_context(patch.object(service.os, 'kill'))

    def assert_no_termination(self):
        self.run.assert_not_called()
        self.popen.assert_not_called()
        self.kill.assert_not_called()
        self.worker.terminate.assert_not_called()
        self.worker.kill.assert_not_called()
        self.worker.wait.assert_not_called()

    def test_manager_exit_leaves_active_worker_running(self):
        with self.assertRaises(KeyboardInterrupt):
            service.main()
        self.start.assert_called_once()
        self.assert_no_termination()

    def test_code_update_waits_for_active_worker_to_become_idle(self):
        self.revision.side_effect = [('initial',), ('initial',), ('updated',), ('updated',)]
        self.sleep.side_effect = [None, None, None, KeyboardInterrupt]
        self.prepare.side_effect=lambda *_:(False,'工作台仍有运行中的任务或窗口，等待空闲后重载')
        with self.assertRaises(KeyboardInterrupt):
            service.main()
        self.start.assert_called_once()
        fields = self.publish.call_args.kwargs
        self.assertTrue(fields['reload_pending'])
        self.assertIn('仍有运行中的任务', fields['error'])
        self.assert_no_termination()

    def test_code_update_restarts_only_the_verified_idle_worker(self):
        self.revision.side_effect = [('initial',), ('initial',), ('updated',), ('updated',), ('updated',)]
        self.sleep.side_effect = [None, None, None, KeyboardInterrupt]
        self.api_state.return_value.update(running=False)
        poll_values=iter([None,None,None,0])
        self.worker.poll.side_effect=lambda:next(poll_values,0)
        self.worker.wait.return_value = 0
        with self.assertRaises(KeyboardInterrupt):
            service.main()
        self.assertEqual(self.start.call_count,2)
        self.prepare.assert_called_once()
        self.worker.terminate.assert_called_once_with()
        self.worker.wait.assert_called_once_with(timeout=service.RELOAD_WAIT_SECONDS)
        self.assertFalse(self.worker.kill.called)
        fields = self.publish.call_args.kwargs
        self.assertFalse(fields['reload_pending'])
        self.assertIsNone(fields['error'])

    def test_existing_listener_is_neither_started_over_nor_stopped(self):
        self.port.return_value = True
        service.main()
        self.start.assert_not_called()
        self.assert_no_termination()

    def test_finished_worker_does_not_kill_reused_pid_or_existing_listener(self):
        self.worker.poll.return_value = 0
        self.worker.returncode = 0
        self.port.side_effect = [False, True]
        self.sleep.side_effect = None
        service.main()
        self.start.assert_called_once()
        self.assertIn('不会关闭该服务', self.publish.call_args.kwargs['error'])
        self.assert_no_termination()


if __name__=='__main__':unittest.main()
