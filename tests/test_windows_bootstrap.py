import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import windows_bootstrap


class WindowsBootstrapTests(unittest.TestCase):
    def test_missing_config_is_created_without_overwriting_local_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            env = {'DOUBAO_DATA_DIR': str(data), 'DOUBAO_CONTAINER_PREVIEW': '1'}
            actual, config = windows_bootstrap.configure_data_environment(env)
            self.assertEqual(actual, data)
            self.assertNotIn('DOUBAO_CONTAINER_PREVIEW', env)
            self.assertTrue(windows_bootstrap.setup.ensure_private_config(windows_bootstrap.ROOT, config))
            config.write_text('local settings', encoding='utf-8')
            self.assertFalse(windows_bootstrap.setup.ensure_private_config(windows_bootstrap.ROOT, config))
            self.assertEqual(config.read_text(encoding='utf-8'), 'local settings')

    def test_relative_config_override_fails_before_preparing(self):
        env = {'DOUBAO_CONFIG_PATH': 'relative.yaml', 'DOUBAO_CONTAINER_PREVIEW': '1'}
        with self.assertRaises(ValueError):
            windows_bootstrap.configure_data_environment(env)
        self.assertIn('DOUBAO_CONTAINER_PREVIEW', env)

    def test_failed_http_check_does_not_open_desktop(self):
        with patch.object(windows_bootstrap.setup, 'ensure_browser'), \
             patch.object(windows_bootstrap.setup, 'verify_dashboard', side_effect=RuntimeError('bad page')), \
             patch('launch_dashboard.main'), patch('launch_dashboard.open_browser') as browser, \
             patch.object(windows_bootstrap.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'bad page'):
                windows_bootstrap.start(None)
        browser.assert_not_called()
        command.assert_not_called()
