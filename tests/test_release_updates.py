import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from release_updates import (
    fetch_latest_release,
    is_installed_copy,
    is_newer_version,
    version_tuple,
)


class ReleaseUpdateTests(unittest.TestCase):
    def test_version_comparison_handles_tags_and_missing_segments(self):
        self.assertEqual(version_tuple("v2.3.0"), (2, 3, 0))
        self.assertTrue(is_newer_version("v2.4.0", "2.3.0"))
        self.assertTrue(is_newer_version("2.3.1", "2.3"))
        self.assertFalse(is_newer_version("2.3.0", "2.3"))

    @patch("release_updates.requests.get")
    def test_latest_release_finds_full_and_app_update_installers(self, get):
        response = Mock()
        response.json.return_value = {
            "tag_name": "v2.4.0",
            "html_url": "https://github.com/example/releases/tag/v2.4.0",
            "assets": [
                {
                    "name": "Twitch Stream Bot 2.4.0.zip",
                    "browser_download_url": "https://example/zip",
                },
                {
                    "name": "Twitch Stream Bot Setup 2.4.0.exe",
                    "browser_download_url": "https://example/setup",
                },
                {
                    "name": "Twitch.Stream.Bot.App.Update.2.4.0.exe",
                    "browser_download_url": "https://example/update",
                },
            ],
        }
        get.return_value = response

        release = fetch_latest_release()

        response.raise_for_status.assert_called_once()
        self.assertEqual(release.version, "2.4.0")
        self.assertEqual(release.installer_url, "https://example/setup")
        self.assertEqual(release.app_update_url, "https://example/update")
        self.assertEqual(release.preferred_update_url, "https://example/update")
        self.assertEqual(
            release.update_url(installed_copy=False),
            "https://github.com/example/releases/tag/v2.4.0",
        )

    @patch("release_updates.requests.get")
    def test_latest_release_falls_back_to_full_installer(self, get):
        response = Mock()
        response.json.return_value = {
            "tag_name": "v2.3.3",
            "html_url": "https://github.com/example/releases/tag/v2.3.3",
            "assets": [
                {
                    "name": "Twitch Stream Bot Setup 2.3.3.exe",
                    "browser_download_url": "https://example/setup",
                },
            ],
        }
        get.return_value = response

        release = fetch_latest_release()

        self.assertEqual(release.app_update_url, "")
        self.assertEqual(release.preferred_update_url, "https://example/setup")

    def test_installed_copy_requires_inno_uninstaller_marker(self):
        with TemporaryDirectory() as temporary_directory:
            app_directory = Path(temporary_directory)
            executable = app_directory / "Twitch Stream Bot.exe"
            executable.touch()

            self.assertFalse(is_installed_copy(executable))

            (app_directory / "unins000.exe").touch()
            (app_directory / "unins000.dat").touch()

            self.assertTrue(is_installed_copy(executable))


if __name__ == "__main__":
    unittest.main()
