import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class WindowsReleaseScriptTests(unittest.TestCase):
    def test_release_script_builds_x64_archive_with_runtime_tools(self):
        script = (ROOT_DIR / "scripts" / "build_windows_release.ps1").read_text()

        self.assertIn("[string]$Version = '1.4.1'", script)
        self.assertIn("struct.calcsize('P') * 8", script)
        self.assertIn("resource\\bin\\windows", script)
        self.assertIn("'ffmpeg.exe'", script)
        self.assertIn("'ffprobe.exe'", script)
        self.assertIn("'--onedir'", script)
        self.assertIn("'--windowed'", script)
        self.assertIn("'--collect-all', 'whisperx'", script)
        self.assertIn("Compress-Archive", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("[IO.File]::WriteAllText", script)
        self.assertIn('"$Hash  $ArchiveName`n"', script)


if __name__ == "__main__":
    unittest.main()
