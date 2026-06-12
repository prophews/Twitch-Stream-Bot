import unittest
from unittest.mock import Mock, patch

from release_updates import (
    fetch_latest_release,
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
    def test_latest_release_prefers_setup_asset(self, get):
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
            ],
        }
        get.return_value = response

        release = fetch_latest_release()

        response.raise_for_status.assert_called_once()
        self.assertEqual(release.version, "2.4.0")
        self.assertEqual(release.installer_url, "https://example/setup")


if __name__ == "__main__":
    unittest.main()
