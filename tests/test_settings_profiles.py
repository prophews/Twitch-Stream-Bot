import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from settings import (
    BotSettings,
    PROJECT_ROOT,
    apply_profile_settings,
    get_runtime_root,
    profile_settings_payload,
    save_settings,
)


class ProfileSettingsTests(unittest.TestCase):
    def test_dev_runtime_defaults_to_project_root(self):
        with patch.dict(
            "os.environ",
            {
                "LOCALAPPDATA": str(Path("C:/Temp/LocalAppData")),
                "TWITCH_STREAM_BOT_RUNTIME_MODE": "",
                "TWITCH_STREAM_BOT_RUNTIME_ROOT": "",
            },
            clear=False,
        ), patch("settings.sys.frozen", False, create=True):
            self.assertEqual(get_runtime_root(), PROJECT_ROOT)

    def test_installed_runtime_mode_matches_public_app_data_root(self):
        local_appdata = Path("C:/Temp/LocalAppData")
        with patch.dict(
            "os.environ",
            {
                "LOCALAPPDATA": str(local_appdata),
                "TWITCH_STREAM_BOT_RUNTIME_MODE": "installed",
            },
            clear=False,
        ), patch("settings.sys.frozen", False, create=True):
            self.assertEqual(
                get_runtime_root(),
                local_appdata / "Twitch Song Request Bot",
            )

    def test_explicit_runtime_root_override_wins(self):
        custom_root = Path("C:/Temp/CustomRuntime")
        with patch.dict(
            "os.environ",
            {
                "TWITCH_STREAM_BOT_RUNTIME_MODE": "installed",
                "TWITCH_STREAM_BOT_RUNTIME_ROOT": str(custom_root),
            },
            clear=False,
        ), patch("settings.sys.frozen", False, create=True):
            self.assertEqual(get_runtime_root(), custom_root)

    def test_profile_includes_modifiable_behavior_but_excludes_secrets_and_paths(self):
        settings = BotSettings(
            OAUTH_TOKEN="secret-token",
            CHANNEL="example",
            LOYALTY_ENABLED=True,
            GAMBLING_ENABLED=False,
            CMD_GAMBLE="bet",
            BUILTIN_RESPONSES={"gamble_win": "{user} wins!"},
            CUSTOM_COMMANDS=[{"name": "hello", "response": "Hi"}],
            TIMED_MESSAGES=[{"name": "Reminder", "interval_minutes": 5}],
            STREAMERBOT_HTTP_ENABLED=True,
            OBS_WS_PASSWORD="secret-obs-password",
        )

        payload = profile_settings_payload(settings)

        self.assertNotIn("OAUTH_TOKEN", payload)
        self.assertNotIn("LOYALTY_DATABASE_PATH", payload)
        self.assertNotIn("PROFILES", payload)
        self.assertNotIn("OBS_WS_PASSWORD", payload)
        self.assertEqual(payload["CHANNEL"], "example")
        self.assertTrue(payload["LOYALTY_ENABLED"])
        self.assertFalse(payload["GAMBLING_ENABLED"])
        self.assertEqual(payload["CMD_GAMBLE"], "bet")
        self.assertEqual(payload["BUILTIN_RESPONSES"]["gamble_win"], "{user} wins!")
        self.assertEqual(payload["CUSTOM_COMMANDS"][0]["name"], "hello")
        self.assertEqual(payload["TIMED_MESSAGES"][0]["name"], "Reminder")
        self.assertTrue(payload["STREAMERBOT_HTTP_ENABLED"])

    def test_profile_application_preserves_secret_and_runtime_paths(self):
        settings = BotSettings(
            OAUTH_TOKEN="keep-me",
            CHANNEL="before",
            LOYALTY_ENABLED=False,
        )
        original_database = settings.loyalty_database_path

        updated = apply_profile_settings(
            settings,
            {
                "OAUTH_TOKEN": "replace-me",
                "LOYALTY_DATABASE_PATH": "other.sqlite3",
                "CHANNEL": "after",
                "LOYALTY_ENABLED": True,
                "CMD_DUEL": "battle",
            },
        )

        self.assertEqual(updated.oauth_token, "keep-me")
        self.assertEqual(updated.loyalty_database_path, original_database)
        self.assertEqual(updated.channel, "after")
        self.assertTrue(updated.loyalty_enabled)
        self.assertEqual(updated.cmd_duel, "battle")

    def test_settings_save_persists_game_rules_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            settings = BotSettings(
                GAMBLING_ENABLED=True,
                CMD_GAMBLE="bet",
                GAMBLE_MINIMUM=5,
                DUELS_ENABLED=True,
                CMD_DUEL="battle",
                BUILTIN_RESPONSES={"duel_result": "{winner} wins the battle!"},
            )

            with patch("settings.get_config_path", return_value=config_path):
                save_settings(settings)

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["CMD_GAMBLE"], "bet")
            self.assertEqual(payload["GAMBLE_MINIMUM"], 5)
            self.assertEqual(payload["CMD_DUEL"], "battle")
            self.assertEqual(
                payload["BUILTIN_RESPONSES"]["duel_result"],
                "{winner} wins the battle!",
            )
            self.assertFalse(config_path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
