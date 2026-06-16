import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import Bot
from loyalty_engine import LoyaltyEngine
from settings import BotSettings


class LoyaltyApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = BotSettings(
            LOYALTY_ENABLED=True,
            LOYALTY_DATABASE_PATH=str(Path(self.temp_dir.name) / "loyalty.sqlite3"),
            CURRENCY_NAME="coins",
            CURRENCY_SINGULAR="coin",
        )
        self.bot = Bot.__new__(Bot)
        self.bot.settings = settings
        self.bot.loyalty = LoyaltyEngine(settings)
        app = web.Application()
        app.add_routes(
            [
                web.get("/api/loyalty/balance", self.bot.handle_api_loyalty_balance),
                web.get(
                    "/api/loyalty/leaderboard",
                    self.bot.handle_api_loyalty_leaderboard,
                ),
                web.post("/api/loyalty/adjust", self.bot.handle_api_loyalty_adjust),
                web.get("/api/commands", self.bot.handle_api_commands),
                web.get("/commands", self.bot.handle_commands_page),
            ]
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_adjust_balance_and_leaderboard_endpoints(self):
        response = await self.client.post(
            "/api/loyalty/adjust",
            json={"user": "viewer", "amount": 25, "reason": "test"},
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["balance"], 25)
        self.assertEqual(payload["currency"], "coins")

        response = await self.client.get("/api/loyalty/balance?user=viewer")
        self.assertEqual((await response.json())["balance"], 25)

        response = await self.client.get("/api/loyalty/leaderboard?limit=5")
        leaders = (await response.json())["leaders"]
        self.assertEqual(leaders[0]["username"], "viewer")
        self.assertEqual(leaders[0]["balance"], 25)

    async def test_adjust_rejects_invalid_input(self):
        response = await self.client.post(
            "/api/loyalty/adjust",
            json={"user": "viewer", "amount": "not-a-number"},
        )
        self.assertEqual(response.status, 400)

    async def test_public_commands_endpoint_is_sanitized(self):
        self.bot.settings.automation_enabled = True
        self.bot.settings.custom_commands = [
            {
                "enabled": True,
                "name": "alert",
                "aliases": ["redeem"],
                "cost": 10,
                "description": "Run the alert.",
                "response": "private response F:\\Media\\secret.gif",
                "streamerbot_action": "Private Action",
                "streamerbot_action_id": "private-id",
            }
        ]
        self.bot.loyalty.active_raffle = {
            "title": "Prize Night",
            "entry_command": "raffle",
            "entries": {},
        }

        response = await self.client.get("/api/commands")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        serialized = str(payload)

        self.assertTrue(payload["ok"])
        self.assertIn("commands", payload)
        self.assertIn("alert", serialized)
        self.assertIn("Prize Night", serialized)
        self.assertNotIn("Private Action", serialized)
        self.assertNotIn("private-id", serialized)
        self.assertNotIn("F:\\Media", serialized)

        page = await self.client.get("/commands")
        self.assertEqual(page.status, 200)
        text = await page.text()
        self.assertIn("Twitch Stream Bot Commands", text)
        self.assertNotIn("Private Action", text)


if __name__ == "__main__":
    unittest.main()
