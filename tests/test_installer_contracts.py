from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InstallerContractTests(unittest.TestCase):
    def test_app_update_uses_full_installer_identity(self):
        full_installer = (PROJECT_ROOT / "installer.iss").read_text(encoding="utf-8")
        app_update = (PROJECT_ROOT / "app_update.iss").read_text(encoding="utf-8")

        app_id_line = next(
            line for line in full_installer.splitlines() if line.startswith("AppId=")
        )
        self.assertIn(app_id_line, app_update)

    def test_app_update_preserves_media_binaries_and_requires_existing_install(self):
        app_update = (PROJECT_ROOT / "app_update.iss").read_text(encoding="utf-8")

        self.assertIn('Excludes: "ffmpeg.exe,ffprobe.exe"', app_update)
        self.assertIn(r"{app}\_internal\ffmpeg.exe", app_update)
        self.assertIn(r"{app}\_internal\ffprobe.exe", app_update)
        self.assertIn("PrepareToInstall", app_update)
        self.assertIn("CreateUninstallRegKey=no", app_update)
        self.assertIn("UpdateUninstallLogAppName=no", app_update)

    def test_build_and_release_workflow_include_app_update(self):
        build_script = (PROJECT_ROOT / "build_release.ps1").read_text(encoding="utf-8")
        verifier = (PROJECT_ROOT / "verify_release.ps1").read_text(encoding="utf-8")
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("app_update.iss", build_script)
        self.assertIn("Twitch Stream Bot App Update", verifier)
        self.assertIn("Twitch Stream Bot App Update *.exe", workflow)


if __name__ == "__main__":
    unittest.main()
