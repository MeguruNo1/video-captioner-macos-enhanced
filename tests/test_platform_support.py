import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.utils import platform_utils as platform


class PlatformSupportTests(unittest.TestCase):
    def test_windows_paths_and_shell_open(self):
        with patch.object(platform, 'IS_WINDOWS', True), patch.object(platform, 'IS_MACOS', False), patch.dict(os.environ, {'LOCALAPPDATA': '/local-app-data'}):
            self.assertEqual(platform.app_data_dir('VideoCaptioner'), Path('/local-app-data/VideoCaptioner'))
            self.assertEqual(platform.default_work_dir('VideoCaptioner'), Path.home() / 'Videos/VideoCaptioner')
            self.assertEqual(platform.bundled_bin_dir(), 'windows')
            with patch.object(os, 'startfile', create=True) as startfile, patch.object(platform.subprocess, 'run') as run:
                self.assertTrue(platform.open_path('some folder'))
                startfile.assert_called_once_with(str(Path('some folder').resolve()))
                run.assert_not_called()

    def test_open_path_reports_failure(self):
        with patch.object(platform, 'IS_WINDOWS', True), patch.object(os, 'startfile', create=True, side_effect=OSError):
            self.assertFalse(platform.open_path('missing'))
        with patch.object(platform, 'IS_WINDOWS', False), patch.object(platform.subprocess, 'run') as run:
            run.return_value.returncode = 1
            self.assertFalse(platform.open_path('missing'))

    def test_non_mac_config_excludes_apple_backends_and_browser(self):
        # Patch capability inputs before importing config in a fresh interpreter.
        import subprocess
        import sys
        code = '''
from app.core.utils import platform_utils as p
p.MLX_SUPPORTED = False
p.COOKIE_BROWSER_OPTIONS = ["Edge", "Chrome"]
p.DEFAULT_COOKIE_BROWSER_LABEL = "Edge"
from app.common.config import Config, TRANSCRIBE_MODEL_OPTIONS
from app.core.entities import TranscribeModelEnum
assert TRANSCRIBE_MODEL_OPTIONS == [TranscribeModelEnum.WHISPER_X]
assert Config.download_cookie_browser.defaultValue == "Edge"
assert "Safari" not in Config.download_cookie_browser.options
'''
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
