import subprocess
import unittest
import plistlib
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class MacOSAppLauncherTests(unittest.TestCase):
    def test_release_script_uses_current_version_and_generates_checksum(self):
        script = (ROOT_DIR / "scripts" / "build_macos_release.sh").read_text()

        self.assertIn('DEFAULT_VERSION="macos-enhanced-v0.1.3"', script)
        self.assertIn('APP_VERSION="${BASH_REMATCH[1]}"', script)
        self.assertIn('shasum -a 256', script)
        self.assertIn('hdiutil verify "$DMG_PATH"', script)

    def test_build_creates_native_arm64_launcher(self):
        subprocess.run(
            ["bash", str(ROOT_DIR / "scripts" / "build_macos_app.sh")],
            cwd=ROOT_DIR,
            check=True,
            capture_output=True,
            text=True,
        )

        launcher = (
            ROOT_DIR
            / "dist"
            / "VideoCaptioner.app"
            / "Contents"
            / "MacOS"
            / "VideoCaptioner"
        )
        result = subprocess.run(
            ["file", str(launcher)],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Mach-O 64-bit executable arm64", result.stdout)
        self.assertNotIn("shell script", result.stdout)

        plist_path = ROOT_DIR / "dist" / "VideoCaptioner.app" / "Contents" / "Info.plist"
        icon_path = (
            ROOT_DIR
            / "dist"
            / "VideoCaptioner.app"
            / "Contents"
            / "Resources"
            / "AppIcon.icns"
        )
        with plist_path.open("rb") as plist_file:
            plist = plistlib.load(plist_file)

        self.assertEqual(plist["CFBundleIconFile"], "AppIcon")
        self.assertNotIn("CFBundleShortVersionString", plist)
        self.assertNotIn("CFBundleVersion", plist)
        self.assertTrue(icon_path.exists())


if __name__ == "__main__":
    unittest.main()
