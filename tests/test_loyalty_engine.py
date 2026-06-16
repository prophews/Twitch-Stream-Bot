import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from loyalty_engine import (
    LoyaltyEngine,
    normalize_custom_command_rule,
)
from settings import BotSettings


async def _async_false():
    return False


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeAuthor:
    def __init__(self, name, **roles):
        self.name = name
        self.display_name = roles.pop("display_name", name)
        self.is_broadcaster = roles.get("broadcaster", False)
        self.is_mod = roles.get("mod", False)
        self.is_vip = roles.get("vip", False)
        self.is_subscriber = roles.get("subscriber", False)


class FakeMessage:
    def __init__(self, content, author, channel):
        self.content = content
        self.author = author
        self.channel = channel


class LoyaltyEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "loyalty.sqlite3"
        self.settings = BotSettings(
            LOYALTY_ENABLED=True,
            AUTOMATION_ENABLED=True,
            LOYALTY_DATABASE_PATH=str(self.database_path),
            CURRENCY_NAME="dabloons",
            CURRENCY_SINGULAR="dabloon",
            STARTING_BALANCE=10,
            POINTS_PER_MESSAGE=2,
            MESSAGE_REWARD_COOLDOWN_SECONDS=60,
            ACTIVE_BONUS_POINTS=5,
            ACTIVE_USER_WINDOW_MINUTES=15,
            CUSTOM_COMMANDS=[
                {
                    "enabled": True,
                    "name": "honk",
                    "aliases": ["goose"],
                    "permission": "everyone",
                    "cost": 5,
                    "cooldown_seconds": 0,
                    "user_cooldown_seconds": 0,
                    "response": "@{user} honked and has {balance} {currency}.",
                    "streamerbot_action": "Honk Alert",
                },
                {
                    "enabled": True,
                    "name": "modonly",
                    "permission": "mod",
                    "cost": 0,
                    "response": "allowed",
                    "streamerbot_action": "",
                },
            ],
            STREAMERBOT_HTTP_ENABLED=True,
        )
        self.engine = LoyaltyEngine(self.settings)
        self.channel = FakeChannel()
        self.viewer = FakeAuthor("viewer", display_name="Viewer")

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_message_rewards_cooldown_active_bonus_and_persistence(self):
        await self.engine.handle_message(FakeMessage("hello", self.viewer, self.channel))
        self.assertEqual(self.engine.get_balance("viewer"), 12)

        await self.engine.handle_message(FakeMessage("again", self.viewer, self.channel))
        self.assertEqual(self.engine.get_balance("viewer"), 12)

        self.assertEqual(self.engine.award_active_users(), 1)
        self.assertEqual(self.engine.get_balance("viewer"), 17)

        restored = LoyaltyEngine(self.settings)
        self.assertEqual(restored.get_balance("viewer"), 17)

    async def test_custom_command_spends_points_and_passes_arguments(self):
        self.engine.adjust_balance("viewer", 7, "test setup")
        triggered = []

        async def trigger(rule, arguments):
            triggered.append((rule, arguments))
            return True

        self.engine._trigger_streamerbot = trigger
        handled = await self.engine.handle_message(
            FakeMessage("!goose loudly", self.viewer, self.channel)
        )

        self.assertTrue(handled)
        self.assertEqual(self.engine.get_balance("viewer"), 12)
        self.assertEqual(triggered[0][0]["streamerbot_action"], "Honk Alert")
        self.assertEqual(triggered[0][1]["arguments"], "loudly")
        self.assertEqual(
            self.channel.sent[-1],
            "@viewer honked and has 12 dabloons.",
        )

    async def test_permissions_and_moderator_adjustments(self):
        before = len(self.channel.sent)
        self.assertTrue(
            await self.engine.handle_message(
                FakeMessage("!modonly", self.viewer, self.channel)
            )
        )
        self.assertEqual(len(self.channel.sent), before)

        moderator = FakeAuthor("helper", mod=True)
        await self.engine.handle_message(
            FakeMessage("!addpoints viewer 8", moderator, self.channel)
        )
        self.assertEqual(self.engine.get_balance("viewer"), 18)

        await self.engine.handle_message(
            FakeMessage("!removepoints viewer 50", moderator, self.channel)
        )
        self.assertEqual(self.engine.get_balance("viewer"), 0)

    async def test_viewers_can_transfer_currency_with_configurable_command(self):
        self.engine.adjust_balance("viewer", 10, "test setup")
        await self.engine.handle_message(
            FakeMessage("!givepoints friend 7", self.viewer, self.channel)
        )

        self.assertEqual(self.engine.get_balance("viewer"), 13)
        self.assertEqual(self.engine.get_balance("friend"), 17)
        self.assertIn("gave 7 dabloons to @friend", self.channel.sent[-1])

    async def test_gambling_wins_and_losses_are_atomic(self):
        self.settings.gamble_cooldown_seconds = 0
        self.settings.gamble_win_chance_percent = 50
        self.settings.gamble_payout_multiplier = 2
        self.engine.adjust_balance("viewer", 90, "test setup")

        self.engine._random = lambda: 0.1
        await self.engine.handle_message(
            FakeMessage("!gamble 10", self.viewer, self.channel)
        )
        self.assertEqual(self.engine.get_balance("viewer"), 110)
        self.assertIn("won 10 dabloons", self.channel.sent[-1])

        self.engine._random = lambda: 0.9
        await self.engine.handle_message(
            FakeMessage("!gamble 10", self.viewer, self.channel)
        )
        self.assertEqual(self.engine.get_balance("viewer"), 100)
        self.assertIn("lost 10 dabloons", self.channel.sent[-1])

    async def test_builtin_chat_responses_are_customizable_and_can_be_silent(self):
        self.settings.gamble_cooldown_seconds = 0
        self.settings.gamble_win_chance_percent = 50
        self.settings.builtin_responses["balance"] = (
            "{target} owns {balance} {balance_currency}."
        )
        self.settings.builtin_responses["gamble_win"] = (
            "{user} banked {winnings} and reached {balance} {currency}."
        )
        self.settings.builtin_responses["gamble_loss"] = ""

        await self.engine.handle_message(
            FakeMessage("!points", self.viewer, self.channel)
        )
        self.assertEqual(self.channel.sent[-1], "viewer owns 10 dabloons.")

        self.engine.adjust_balance("viewer", 90, "test setup")
        self.engine._random = lambda: 0.1
        await self.engine.handle_message(
            FakeMessage("!gamble 10", self.viewer, self.channel)
        )
        self.assertEqual(
            self.channel.sent[-1],
            "viewer banked 10 and reached 110 dabloons.",
        )

        message_count = len(self.channel.sent)
        self.engine._random = lambda: 0.9
        await self.engine.handle_message(
            FakeMessage("!gamble 10", self.viewer, self.channel)
        )
        self.assertEqual(len(self.channel.sent), message_count)
        self.assertEqual(self.engine.get_balance("viewer"), 100)

    async def test_duel_templates_receive_duel_specific_placeholders(self):
        self.settings.duel_cooldown_seconds = 0
        self.settings.builtin_responses["duel_challenge"] = (
            "{challenger} vs {opponent} for {amount} {amount_currency}; "
            "!{accept_command} or !{decline_command}."
        )
        self.settings.builtin_responses["duel_result"] = (
            "{winner} beat {loser}; balance {winner_balance}."
        )
        self.engine.adjust_balance("viewer", 40, "test setup")
        self.engine.adjust_balance("opponent", 40, "test setup")
        opponent = FakeAuthor("opponent")

        await self.engine.handle_message(
            FakeMessage("!duel opponent 10", self.viewer, self.channel)
        )
        self.assertEqual(
            self.channel.sent[-1],
            "viewer vs opponent for 10 dabloons; !accept or !decline.",
        )

        self.engine._random = lambda: 0.1
        await self.engine.handle_message(
            FakeMessage("!accept", opponent, self.channel)
        )
        self.assertEqual(self.channel.sent[-1], "viewer beat opponent; balance 60.")

    async def test_duel_acceptance_transfers_wager_without_creating_points(self):
        self.settings.duel_cooldown_seconds = 0
        self.engine.adjust_balance("viewer", 40, "test setup")
        self.engine.adjust_balance("opponent", 40, "test setup")
        opponent = FakeAuthor("opponent")

        await self.engine.handle_message(
            FakeMessage("!duel opponent 10", self.viewer, self.channel)
        )
        self.assertIn("opponent", self.engine.pending_duels)
        self.assertIn("challenged you to a duel", self.channel.sent[-1])

        self.engine._random = lambda: 0.1
        await self.engine.handle_message(
            FakeMessage("!accept", opponent, self.channel)
        )

        self.assertEqual(self.engine.get_balance("viewer"), 60)
        self.assertEqual(self.engine.get_balance("opponent"), 40)
        self.assertEqual(
            self.engine.get_balance("viewer")
            + self.engine.get_balance("opponent"),
            100,
        )
        self.assertNotIn("opponent", self.engine.pending_duels)

    async def test_dashboard_raffle_accepts_entries_and_awards_winner(self):
        self.settings.raffle_enabled = True
        self.settings.raffle_reward_points = 25
        self.settings.cmd_raffle_enter = "raffle"
        self.engine._random = lambda: 0.99

        started = await self.engine.start_raffle(
            title="Movie Night",
            entry_command="",
            duration_seconds=60,
            countdown_interval_seconds=0,
            reward_points=25,
            channel=self.channel,
        )
        self.assertTrue(started)
        self.assertIn("Movie Night", self.channel.sent[-1])
        self.assertIn("!raffle", self.channel.sent[-1])

        await self.engine.handle_message(
            FakeMessage("!raffle", self.viewer, self.channel)
        )
        duplicate_message_count = len(self.channel.sent)
        await self.engine.handle_message(
            FakeMessage("!raffle", self.viewer, self.channel)
        )
        self.assertEqual(len(self.channel.sent), duplicate_message_count)

        other = FakeAuthor("other", display_name="Other")
        await self.engine.handle_message(FakeMessage("!raffle", other, self.channel))
        winner = await self.engine.draw_raffle(self.channel)

        self.assertEqual(winner["username"], "other")
        self.assertEqual(self.engine.get_balance("other"), 35)
        self.assertIsNone(self.engine.active_raffle)
        self.assertIn("Other won Movie Night", self.channel.sent[-1])

    async def test_raffle_without_entries_ends_cleanly(self):
        self.settings.raffle_enabled = True
        await self.engine.start_raffle(
            title="Empty Prize",
            duration_seconds=30,
            countdown_interval_seconds=0,
            channel=self.channel,
        )

        winner = await self.engine.draw_raffle(self.channel)

        self.assertIsNone(winner)
        self.assertIsNone(self.engine.active_raffle)
        self.assertIn("No one entered", self.channel.sent[-1])

    async def test_raffle_reward_is_not_awarded_when_loyalty_is_disabled(self):
        self.settings.loyalty_enabled = False
        self.settings.automation_enabled = False
        self.settings.raffle_enabled = True
        await self.engine.start_raffle(
            title="No Points Prize",
            reward_points=50,
            countdown_interval_seconds=0,
            channel=self.channel,
        )
        await self.engine.handle_message(
            FakeMessage("!raffle", self.viewer, self.channel)
        )

        winner = await self.engine.draw_raffle(self.channel)

        self.assertEqual(winner["reward"], 0)
        self.assertEqual(self.engine.get_balance("viewer"), 0)
        self.assertIn("Viewer won No Points Prize!", self.channel.sent[-1])
        self.assertNotIn("Awarded", self.channel.sent[-1])

    async def test_duelist_cannot_join_multiple_pending_duels(self):
        self.settings.duel_cooldown_seconds = 0
        self.engine.adjust_balance("viewer", 40, "test setup")
        self.engine.adjust_balance("opponent", 40, "test setup")
        self.engine.adjust_balance("third", 40, "test setup")
        opponent = FakeAuthor("opponent")

        await self.engine.handle_message(
            FakeMessage("!duel opponent 10", self.viewer, self.channel)
        )
        await self.engine.handle_message(
            FakeMessage("!duel third 10", opponent, self.channel)
        )

        self.assertEqual(list(self.engine.pending_duels), ["opponent"])
        self.assertIn("already has a pending duel", self.channel.sent[-1])

    async def test_timed_messages_require_interval_and_chat_activity(self):
        self.settings.timed_messages = [
            {
                "enabled": True,
                "name": "Discord Reminder",
                "interval_minutes": 10,
                "minimum_chat_messages": 2,
                "message": "Join us after {chat_messages} chat messages!",
                "streamerbot_action": "",
            }
        ]
        await self.engine.handle_message(FakeMessage("hello", self.viewer, self.channel))
        self.assertEqual(await self.engine.run_due_timers(now=self.engine.started_at + 601), 0)
        await self.engine.handle_message(FakeMessage("again", self.viewer, self.channel))

        self.assertEqual(await self.engine.run_due_timers(now=self.engine.started_at + 601), 1)
        self.assertEqual(self.channel.sent[-1], "Join us after 2 chat messages!")
        self.assertEqual(self.engine.chat_messages_since_timer, 0)

    async def test_timer_chat_message_still_runs_if_streamerbot_is_offline(self):
        self.settings.timed_messages = [
            {
                "enabled": True,
                "name": "Reminder",
                "interval_minutes": 1,
                "minimum_chat_messages": 1,
                "message": "Remember to follow!",
                "streamerbot_action": "Missing Action",
            }
        ]
        self.engine._trigger_streamerbot = lambda rule, args: _async_false()
        await self.engine.handle_message(FakeMessage("hello", self.viewer, self.channel))

        self.assertEqual(await self.engine.run_due_timers(now=self.engine.started_at + 61), 1)
        self.assertEqual(self.channel.sent[-1], "Remember to follow!")

    async def test_response_only_commands_work_without_loyalty_enabled(self):
        self.settings.loyalty_enabled = False
        self.settings.automation_enabled = True
        self.settings.custom_commands = [
            {
                "enabled": True,
                "name": "hello",
                "permission": "everyone",
                "cost": 0,
                "response": "Hello, @{user}!",
            }
        ]

        handled = await self.engine.handle_message(
            FakeMessage("!hello", self.viewer, self.channel)
        )

        self.assertTrue(handled)
        self.assertEqual(self.channel.sent[-1], "Hello, @viewer!")

    async def test_role_multipliers_and_excluded_users_are_configurable(self):
        self.settings.subscriber_points_multiplier = 2.0
        self.settings.vip_points_multiplier = 3.0
        self.settings.mod_points_multiplier = 4.0
        self.settings.loyalty_excluded_users = "ignoredbot"

        subscriber = FakeAuthor("subscriber", subscriber=True)
        vip = FakeAuthor("vipuser", vip=True, subscriber=True)
        moderator = FakeAuthor("moderator", mod=True, vip=True)
        ignored = FakeAuthor("ignoredbot")
        await self.engine.handle_message(FakeMessage("hello", subscriber, self.channel))
        await self.engine.handle_message(FakeMessage("hello", vip, self.channel))
        await self.engine.handle_message(FakeMessage("hello", moderator, self.channel))
        await self.engine.handle_message(FakeMessage("hello", ignored, self.channel))

        self.assertEqual(self.engine.get_balance("subscriber"), 14)
        self.assertEqual(self.engine.get_balance("vipuser"), 16)
        self.assertEqual(self.engine.get_balance("moderator"), 18)
        self.assertEqual(self.engine.get_balance("ignoredbot"), 0)

    async def test_streamerbot_http_action_payload(self):
        received = []

        async def do_action(request):
            received.append(await request.json())
            return web.Response(status=204)

        app = web.Application()
        app.router.add_post("/DoAction", do_action)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.settings.streamerbot_http_url = f"http://127.0.0.1:{port}/DoAction"

        try:
            result = await self.engine._trigger_streamerbot(
                {
                    "name": "honk",
                    "streamerbot_action": "Honk Alert",
                    "streamerbot_action_id": "action-123",
                },
                {"user": "viewer", "arguments": "loudly"},
            )
            legacy_result = await self.engine._trigger_streamerbot(
                {"name": "legacy", "streamerbot_action": "Legacy Alert"},
                {"user": "viewer"},
            )
        finally:
            await runner.cleanup()

        self.assertTrue(result)
        self.assertTrue(legacy_result)
        self.assertEqual(received[0]["action"], {"id": "action-123"})
        self.assertEqual(received[0]["args"]["user"], "viewer")
        self.assertEqual(received[1]["action"], {"name": "Legacy Alert"})

    async def test_close_stops_owned_timers_and_future_streamerbot_dispatches(self):
        received = []

        async def do_action(request):
            received.append(await request.json())
            return web.Response(status=204)

        app = web.Application()
        app.router.add_post("/DoAction", do_action)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.settings.streamerbot_http_url = f"http://127.0.0.1:{port}/DoAction"
        self.settings.timed_messages = [
            {
                "enabled": True,
                "name": "Owned Timer",
                "interval_minutes": 1,
                "minimum_chat_messages": 0,
                "streamerbot_action": "Owned Action",
            }
        ]
        self.engine.latest_channel = self.channel

        try:
            self.assertTrue(
                await self.engine._trigger_streamerbot(
                    {"streamerbot_action": "Before Stop"},
                    {"source": "twitch-bot"},
                )
            )
            await self.engine.close()

            self.assertFalse(
                await self.engine._trigger_streamerbot(
                    {"streamerbot_action": "After Stop"},
                    {"source": "twitch-bot"},
                )
            )
            self.assertEqual(
                await self.engine.run_due_timers(
                    now=self.engine.started_at + 120
                ),
                0,
            )
        finally:
            await runner.cleanup()

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["action"], {"name": "Before Stop"})

    async def test_get_streamerbot_actions_uses_official_http_endpoint(self):
        async def get_actions(_request):
            return web.json_response(
                {"actions": [{"id": "abc", "name": "Honk Alert"}]}
            )

        app = web.Application()
        app.router.add_get("/GetActions", get_actions)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.settings.streamerbot_http_url = f"http://127.0.0.1:{port}/DoAction"

        try:
            actions = await self.engine.get_streamerbot_actions()
        finally:
            await runner.cleanup()

        self.assertEqual(actions, [{"id": "abc", "name": "Honk Alert"}])

    def test_database_backup_and_restore(self):
        self.engine.adjust_balance("viewer", 25, "before backup")
        backup_path = Path(self.temp_dir.name) / "backup.sqlite3"
        self.engine.backup_database(backup_path)
        self.engine.adjust_balance("viewer", 10, "after backup")
        self.assertEqual(self.engine.get_balance("viewer"), 45)

        self.engine.restore_database(backup_path)

        self.assertEqual(self.engine.get_balance("viewer"), 35)

    def test_imported_rules_are_normalized_without_executable_content(self):
        normalized = normalize_custom_command_rule(
            {
                "name": "!HONK",
                "aliases": "goose, bird",
                "cost": "-8",
                "permission": "not-a-role",
                "streamerbot_action": "Honk Alert",
                "streamerbot_action_id": "action-123",
                "response": "{user} honked",
                "unknown_code": "ignored",
            }
        )

        self.assertEqual(normalized["name"], "honk")
        self.assertEqual(normalized["aliases"], ["goose", "bird"])
        self.assertEqual(normalized["cost"], 0)
        self.assertEqual(normalized["permission"], "everyone")
        self.assertEqual(normalized["streamerbot_action_id"], "action-123")
        self.assertNotIn("unknown_code", normalized)


if __name__ == "__main__":
    unittest.main()
