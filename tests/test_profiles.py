import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import Bot
from settings import BotSettings, profile_settings_payload


class StreamProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_profile_applies_loyalty_commands_and_visuals(self):
        settings = BotSettings(
            CHANNEL="before",
            LOYALTY_ENABLED=False,
            CMD_GAMBLE="gamble",
        )
        profile_settings = settings.model_copy(deep=True)
        profile_settings.channel = "after"
        profile_settings.loyalty_enabled = True
        profile_settings.cmd_gamble = "bet"
        profile_settings.custom_commands = [
            {"name": "hello", "response": "Hello!"}
        ]
        settings.profiles = {
            "Games": {
                "format_version": 2,
                "settings": profile_settings_payload(profile_settings),
                "accept_requests": False,
                "show_title": True,
                "show_time": True,
                "show_progress": True,
                "window_position": "Top Right",
                "window_width": 800,
                "window_height": 450,
                "background_opacity": 75,
                "title_font_size": 16,
                "time_font_size": 14,
            }
        }

        bot = Bot.__new__(Bot)
        bot.settings = settings
        bot.loyalty = SimpleNamespace(settings=settings)
        bot.obs_controller = SimpleNamespace(settings=settings)
        bot.is_sr_enabled = True
        bot.vlc_set_position = AsyncMock()
        bot.vlc_set_bg_opacity = AsyncMock()
        bot.vlc_set_window_size = AsyncMock()
        bot.vlc_set_hud_font_sizes = AsyncMock()
        bot.vlc_set_title_visible = AsyncMock()
        bot.vlc_set_time_visible = AsyncMock()
        bot.vlc_set_progress_visible = AsyncMock()
        bot.save_bot_state = lambda: None
        bot.profile_applied_callback = None

        with (
            patch("bot.save_settings"),
            self.assertLogs("StandaloneBot", level="WARNING") as captured_logs,
        ):
            applied = await bot.apply_stream_profile("Games")

        self.assertEqual(bot.settings.channel, "after")
        self.assertTrue(bot.settings.loyalty_enabled)
        self.assertEqual(bot.settings.cmd_gamble, "bet")
        self.assertEqual(bot.settings.custom_commands[0]["name"], "hello")
        self.assertFalse(bot.is_sr_enabled)
        self.assertEqual(applied["window_position"], "Top Right")
        bot.vlc_set_window_size.assert_awaited_once_with(800, 450)
        self.assertIn(
            "Restart the bot",
            "\n".join(captured_logs.output),
        )


if __name__ == "__main__":
    unittest.main()
