import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import Bot


class BotShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_handles_partial_startup_without_twitch_client_state(self):
        calls = []

        async def record(name):
            calls.append(name)

        bot = Bot.__new__(Bot)
        bot._close_started = False
        bot.loyalty = SimpleNamespace(close=lambda: record("loyalty"))
        bot.obs_controller = SimpleNamespace(close=lambda: record("obs"))
        bot._closing = None

        await Bot.close(bot)

        self.assertEqual(calls, ["loyalty", "obs"])
        self.assertTrue(bot._close_started)

    async def test_shutdown_stops_owned_automation_before_other_services(self):
        calls = []

        async def record(name):
            calls.append(name)

        async def close_bot():
            await record("bot")

        bot = Bot.__new__(Bot)
        bot._shutdown_started = False
        bot.loyalty = SimpleNamespace(close=lambda: record("loyalty"))
        bot.websockets = set()
        bot.ws_play_sent = set()
        bot._obs_page_ready = SimpleNamespace(clear=lambda: calls.append("page"))
        bot.site = SimpleNamespace(stop=lambda: record("site"))
        bot.runner = SimpleNamespace(cleanup=lambda: record("runner"))
        bot.close = AsyncMock(side_effect=close_bot)

        await Bot.shutdown(bot)

        self.assertEqual(calls[0], "loyalty")
        self.assertEqual(calls, ["loyalty", "page", "site", "runner", "bot"])
        self.assertTrue(bot._shutdown_started)

        await Bot.shutdown(bot)
        self.assertEqual(calls, ["loyalty", "page", "site", "runner", "bot"])


if __name__ == "__main__":
    unittest.main()
