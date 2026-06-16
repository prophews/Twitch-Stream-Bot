import asyncio
import logging
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientError, ClientSession

from settings import DEFAULT_BUILTIN_RESPONSES


logger = logging.getLogger("TwitchBot.Loyalty")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_user(value: str) -> str:
    return value.strip().lstrip("!@").lower()


def normalize_custom_command_rule(rule: Any) -> Optional[dict[str, Any]]:
    if not isinstance(rule, dict):
        return None
    name = _clean_user(str(rule.get("name", "")))
    if not name or " " in name:
        return None
    raw_aliases = rule.get("aliases", [])
    if isinstance(raw_aliases, str):
        raw_aliases = raw_aliases.split(",")
    if not isinstance(raw_aliases, list):
        raw_aliases = []
    aliases = [
        _clean_user(str(alias))
        for alias in raw_aliases
        if _clean_user(str(alias)) and " " not in _clean_user(str(alias))
    ]

    def non_negative_integer(key: str) -> int:
        try:
            return max(0, int(rule.get(key, 0)))
        except (TypeError, ValueError):
            return 0

    permission = str(rule.get("permission", "everyone")).lower()
    if permission not in {"everyone", "subscriber", "vip", "mod", "broadcaster"}:
        permission = "everyone"
    return {
        "enabled": bool(rule.get("enabled", True)),
        "name": name,
        "aliases": list(dict.fromkeys(aliases)),
        "permission": permission,
        "cost": non_negative_integer("cost"),
        "cooldown_seconds": non_negative_integer("cooldown_seconds"),
        "user_cooldown_seconds": non_negative_integer("user_cooldown_seconds"),
        "streamerbot_action": str(rule.get("streamerbot_action", "")).strip(),
        "streamerbot_action_id": str(rule.get("streamerbot_action_id", "")).strip(),
        "response": str(rule.get("response", "")).strip(),
        "insufficient_funds_response": str(
            rule.get(
                "insufficient_funds_response",
                "@{user}, you need {cost} {currency} to use !{command}.",
            )
        ).strip(),
    }


def normalize_timed_message_rule(rule: Any) -> Optional[dict[str, Any]]:
    if not isinstance(rule, dict):
        return None
    name = str(rule.get("name", "")).strip()
    if not name:
        return None

    def integer(key: str, minimum: int) -> int:
        try:
            return max(minimum, int(rule.get(key, minimum)))
        except (TypeError, ValueError):
            return minimum

    return {
        "enabled": bool(rule.get("enabled", True)),
        "name": name[:60],
        "interval_minutes": integer("interval_minutes", 1),
        "minimum_chat_messages": integer("minimum_chat_messages", 0),
        "message": str(rule.get("message", "")).strip(),
        "streamerbot_action": str(rule.get("streamerbot_action", "")).strip(),
        "streamerbot_action_id": str(rule.get("streamerbot_action_id", "")).strip(),
    }


class LoyaltyEngine:
    def __init__(self, settings):
        self.settings = settings
        self.database_path = Path(settings.loyalty_database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.active_users: dict[str, tuple[str, float, float]] = {}
        self.command_cooldowns: dict[tuple[str, str], float] = {}
        self.game_cooldowns: dict[tuple[str, str], float] = {}
        self.pending_duels: dict[str, dict[str, Any]] = {}
        self.active_raffle: Optional[dict[str, Any]] = None
        self._random = random.random
        self._active_bonus_task: Optional[asyncio.Task] = None
        self._timer_task: Optional[asyncio.Task] = None
        self._raffle_countdown_task: Optional[asyncio.Task] = None
        self._streamerbot_tasks: set[asyncio.Task] = set()
        self._accepting_actions = True
        self.latest_channel = None
        self.chat_messages_since_timer = 0
        self.timer_last_run: dict[str, float] = {}
        self.started_at = time.time()
        self._initialize_database()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    active_bonus_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT,
                    last_message_reward_at REAL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ledger_user_created
                ON ledger(username, created_at DESC);
                """
            )

    def start(self):
        self._accepting_actions = True
        if self._active_bonus_task is None or self._active_bonus_task.done():
            self._active_bonus_task = asyncio.create_task(self._active_bonus_loop())
        if self._timer_task is None or self._timer_task.done():
            self._timer_task = asyncio.create_task(self._timed_message_loop())

    async def close(self):
        # Disable dispatch before awaiting anything so shutdown cannot race a timer.
        self._accepting_actions = False
        self.latest_channel = None

        owned_tasks = [
            task
            for task in (
                self._active_bonus_task,
                self._timer_task,
                self._raffle_countdown_task,
                *self._streamerbot_tasks,
            )
            if task is not None and not task.done()
        ]
        for task in owned_tasks:
            task.cancel()
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)

        self._active_bonus_task = None
        self._timer_task = None
        self._raffle_countdown_task = None
        self._streamerbot_tasks.clear()
        self.active_raffle = None

    def _ensure_user(self, username: str, display_name: str):
        username = _clean_user(username)
        if not username:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    username, display_name, balance, last_seen_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    username,
                    display_name or username,
                    max(0, int(self.settings.starting_balance)),
                    _utc_now(),
                    _utc_now(),
                ),
            )

    def get_balance(self, username: str) -> int:
        username = _clean_user(username)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT balance FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return int(row["balance"]) if row else 0

    def adjust_balance(self, username: str, delta: int, reason: str, display_name: str = "") -> int:
        username = _clean_user(username)
        if not username:
            raise ValueError("A username is required.")
        self._ensure_user(username, display_name or username)
        with self._connect() as connection:
            before_row = connection.execute(
                "SELECT balance FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            before_balance = int(before_row["balance"])
            connection.execute(
                "UPDATE users SET balance = MAX(0, balance + ?) WHERE username = ?",
                (int(delta), username),
            )
            row = connection.execute(
                "SELECT balance FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            actual_balance = int(row["balance"])
            actual_delta = actual_balance - before_balance
            connection.execute(
                "INSERT INTO ledger (username, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                (username, actual_delta, reason[:200], _utc_now()),
            )
        return actual_balance

    def spend(self, username: str, amount: int, reason: str) -> Optional[int]:
        amount = max(0, int(amount))
        username = _clean_user(username)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT balance FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None or int(row["balance"]) < amount:
                return None
            connection.execute(
                "UPDATE users SET balance = balance - ? WHERE username = ?",
                (amount, username),
            )
            connection.execute(
                "INSERT INTO ledger (username, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                (username, -amount, reason[:200], _utc_now()),
            )
            return int(row["balance"]) - amount

    def transfer(self, sender: str, recipient: str, amount: int) -> Optional[tuple[int, int]]:
        sender = _clean_user(sender)
        recipient = _clean_user(recipient)
        amount = int(amount)
        if not sender or not recipient or sender == recipient or amount <= 0:
            return None
        self._ensure_user(sender, sender)
        self._ensure_user(recipient, recipient)
        with self._connect() as connection:
            sender_row = connection.execute(
                "SELECT balance FROM users WHERE username = ?",
                (sender,),
            ).fetchone()
            if sender_row is None or int(sender_row["balance"]) < amount:
                return None
            connection.execute(
                "UPDATE users SET balance = balance - ? WHERE username = ?",
                (amount, sender),
            )
            connection.execute(
                "UPDATE users SET balance = balance + ? WHERE username = ?",
                (amount, recipient),
            )
            timestamp = _utc_now()
            connection.execute(
                "INSERT INTO ledger (username, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                (sender, -amount, f"transfer to {recipient}", timestamp),
            )
            connection.execute(
                "INSERT INTO ledger (username, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                (recipient, amount, f"transfer from {sender}", timestamp),
            )
            recipient_row = connection.execute(
                "SELECT balance FROM users WHERE username = ?",
                (recipient,),
            ).fetchone()
            return int(sender_row["balance"]) - amount, int(recipient_row["balance"])

    def settle_gamble(
        self, username: str, amount: int, won: bool
    ) -> Optional[tuple[int, int]]:
        username = _clean_user(username)
        amount = int(amount)
        if not username or amount <= 0:
            return None
        self._ensure_user(username, username)
        payout_multiplier = max(
            1.0, float(self.settings.gamble_payout_multiplier)
        )
        delta = (
            max(0, round(amount * (payout_multiplier - 1.0)))
            if won
            else -amount
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT balance FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None or int(row["balance"]) < amount:
                return None
            new_balance = int(row["balance"]) + delta
            connection.execute(
                "UPDATE users SET balance = ? WHERE username = ?",
                (new_balance, username),
            )
            connection.execute(
                "INSERT INTO ledger (username, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                (
                    username,
                    delta,
                    f"gamble {'win' if won else 'loss'} ({amount} wagered)",
                    _utc_now(),
                ),
            )
        return new_balance, delta

    def settle_duel(
        self, challenger: str, opponent: str, amount: int, winner: str
    ) -> Optional[dict[str, int | str]]:
        challenger = _clean_user(challenger)
        opponent = _clean_user(opponent)
        winner = _clean_user(winner)
        amount = int(amount)
        if (
            not challenger
            or not opponent
            or challenger == opponent
            or winner not in {challenger, opponent}
            or amount <= 0
        ):
            return None
        loser = opponent if winner == challenger else challenger
        self._ensure_user(challenger, challenger)
        self._ensure_user(opponent, opponent)
        with self._connect() as connection:
            rows = {
                row["username"].lower(): int(row["balance"])
                for row in connection.execute(
                    "SELECT username, balance FROM users WHERE username IN (?, ?)",
                    (challenger, opponent),
                ).fetchall()
            }
            if rows.get(challenger, 0) < amount or rows.get(opponent, 0) < amount:
                return None
            connection.execute(
                "UPDATE users SET balance = balance + ? WHERE username = ?",
                (amount, winner),
            )
            connection.execute(
                "UPDATE users SET balance = balance - ? WHERE username = ?",
                (amount, loser),
            )
            timestamp = _utc_now()
            connection.execute(
                "INSERT INTO ledger (username, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                (winner, amount, f"duel win against {loser}", timestamp),
            )
            connection.execute(
                "INSERT INTO ledger (username, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                (loser, -amount, f"duel loss against {winner}", timestamp),
            )
            winner_balance = rows[winner] + amount
            loser_balance = rows[loser] - amount
        return {
            "winner": winner,
            "loser": loser,
            "winner_balance": winner_balance,
            "loser_balance": loser_balance,
        }

    async def start_raffle(
        self,
        title: str = "",
        entry_command: str = "",
        duration_seconds: int = 0,
        countdown_interval_seconds: int = 0,
        reward_points: int = 0,
        channel=None,
    ) -> bool:
        if not self.settings.raffle_enabled or self.active_raffle is not None:
            return False

        title = (title or self.settings.raffle_default_title or "Raffle").strip()[:80]
        entry_command = _clean_user(
            entry_command or self.settings.cmd_raffle_enter or "raffle"
        )
        if not entry_command or " " in entry_command:
            entry_command = "raffle"
        duration = max(
            10,
            min(
                3600,
                int(duration_seconds or self.settings.raffle_duration_seconds),
            ),
        )
        countdown_interval = max(
            0,
            min(
                duration,
                int(
                    countdown_interval_seconds
                    if countdown_interval_seconds is not None
                    else self.settings.raffle_countdown_interval_seconds
                ),
            ),
        )
        reward = max(
            0,
            int(
                reward_points
                if reward_points is not None
                else self.settings.raffle_reward_points
            ),
        )
        self.active_raffle = {
            "title": title,
            "entry_command": entry_command,
            "started_at": time.time(),
            "ends_at": time.time() + duration,
            "duration_seconds": duration,
            "countdown_interval_seconds": countdown_interval,
            "reward_points": reward,
            "entries": {},
        }
        channel = channel or self.latest_channel
        if channel is not None:
            await self._send_builtin(
                channel,
                "raffle_started",
                title=title,
                entry_command=entry_command,
                duration=duration,
                reward=reward,
                reward_currency=self._currency_for(reward),
            )
            if countdown_interval:
                self._start_raffle_countdown(channel)
        return True

    def _start_raffle_countdown(self, channel) -> None:
        if self._raffle_countdown_task is not None and not self._raffle_countdown_task.done():
            self._raffle_countdown_task.cancel()
        self._raffle_countdown_task = asyncio.create_task(
            self._raffle_countdown_loop(channel)
        )

    async def _raffle_countdown_loop(self, channel) -> None:
        try:
            while self._accepting_actions and self.active_raffle is not None:
                interval = int(self.active_raffle.get("countdown_interval_seconds", 0))
                if interval <= 0:
                    return
                await asyncio.sleep(interval)
                raffle = self.active_raffle
                if raffle is None:
                    return
                remaining = max(0, round(float(raffle["ends_at"]) - time.time()))
                if remaining <= 0:
                    await self.draw_raffle(channel)
                    return
                await self._send_builtin(
                    channel,
                    "raffle_countdown",
                    title=raffle["title"],
                    entry_command=raffle["entry_command"],
                    remaining=remaining,
                    reward=raffle["reward_points"],
                    reward_currency=self._currency_for(raffle["reward_points"]),
                )
        except asyncio.CancelledError:
            raise

    async def cancel_raffle(self, channel=None) -> bool:
        raffle = self.active_raffle
        if raffle is None:
            return False
        self._clear_raffle_task()
        self.active_raffle = None
        channel = channel or self.latest_channel
        if channel is not None:
            await self._send_builtin(channel, "raffle_cancelled", title=raffle["title"])
        return True

    async def draw_raffle(self, channel=None) -> Optional[dict[str, Any]]:
        raffle = self.active_raffle
        if raffle is None:
            return None
        self._clear_raffle_task()
        self.active_raffle = None
        channel = channel or self.latest_channel
        entries = list(raffle.get("entries", {}).values())
        if not entries:
            if channel is not None:
                await self._send_builtin(channel, "raffle_no_entries", title=raffle["title"])
            return None

        index = min(len(entries) - 1, int(self._random() * len(entries)))
        winner = entries[index]
        reward = max(0, int(raffle.get("reward_points", 0)))
        balance = self.get_balance(winner["username"])
        awarded_reward = self.settings.loyalty_enabled and reward
        if awarded_reward:
            balance = self.adjust_balance(
                winner["username"],
                reward,
                f"raffle winner: {raffle['title']}",
                winner.get("display_name", winner["username"]),
            )
        if channel is not None:
            response_name = "raffle_winner" if awarded_reward else "raffle_winner_no_reward"
            await self._send_builtin(
                channel,
                response_name,
                winner=winner.get("display_name") or winner["username"],
                user=winner["username"],
                title=raffle["title"],
                reward=reward,
                reward_currency=self._currency_for(reward),
                balance=balance,
                balance_currency=self._currency_for(balance),
            )
        return {**winner, "reward": reward if awarded_reward else 0, "balance": balance}

    def _clear_raffle_task(self) -> None:
        if self._raffle_countdown_task is not None and not self._raffle_countdown_task.done():
            if self._raffle_countdown_task is not asyncio.current_task():
                self._raffle_countdown_task.cancel()
        self._raffle_countdown_task = None

    async def _handle_raffle_entry(
        self,
        command_name: str,
        username: str,
        display_name: str,
        channel,
    ) -> bool:
        raffle = self.active_raffle
        if (
            raffle is None
            or not self.settings.raffle_enabled
            or command_name != str(raffle.get("entry_command", "")).lower()
        ):
            return False
        if time.time() >= float(raffle.get("ends_at", 0)):
            await self.draw_raffle(channel)
            return True
        clean_username = _clean_user(username)
        if not clean_username:
            return True
        entries = raffle.setdefault("entries", {})
        if clean_username in entries:
            return True
        entries[clean_username] = {
            "username": clean_username,
            "display_name": display_name or clean_username,
            "entered_at": _utc_now(),
        }
        await self._send_builtin(
            channel,
            "raffle_entry",
            user=clean_username,
            display_name=display_name or clean_username,
            title=raffle["title"],
            entry_count=len(entries),
            entry_command=raffle["entry_command"],
        )
        return True

    @staticmethod
    def _parse_wager(
        value: str, balance: int, minimum: int, maximum: int
    ) -> Optional[int]:
        value = str(value).strip().lower()
        minimum = max(1, int(minimum))
        maximum = max(minimum, int(maximum))
        if value == "all":
            amount = min(balance, maximum)
        else:
            try:
                amount = int(value)
            except (TypeError, ValueError):
                return None
        if amount < minimum or amount > maximum or amount > balance:
            return None
        return amount

    def _game_on_cooldown(self, game: str, username: str, seconds: int) -> bool:
        key = (game, _clean_user(username))
        now = time.time()
        if now < self.game_cooldowns.get(key, 0):
            return True
        self.game_cooldowns[key] = now + max(0, int(seconds))
        return False

    def _expire_duels(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        timeout = max(10, int(self.settings.duel_timeout_seconds))
        for opponent, challenge in list(self.pending_duels.items()):
            if now - float(challenge.get("created_at", 0)) >= timeout:
                self.pending_duels.pop(opponent, None)

    def _has_pending_duel(self, username: str) -> bool:
        username = _clean_user(username)
        return username in self.pending_duels or any(
            challenge.get("challenger") == username
            for challenge in self.pending_duels.values()
        )

    def leaderboard(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT username, display_name, balance
                FROM users
                ORDER BY balance DESC, username ASC
                LIMIT ?
                """,
                (max(1, min(25, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def backup_database(self, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.database_path, timeout=10)
        target = sqlite3.connect(destination, timeout=10)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def restore_database(self, source_path: Path) -> None:
        source_path = Path(source_path)
        if not source_path.exists() or not source_path.is_file():
            raise ValueError("The selected loyalty backup does not exist.")
        source = sqlite3.connect(source_path, timeout=10)
        target = sqlite3.connect(self.database_path, timeout=10)
        try:
            integrity = source.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValueError("The selected SQLite file failed its integrity check.")
            tables = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not {"users", "ledger"}.issubset(tables):
                raise ValueError("The selected file is not a loyalty database backup.")
            source.backup(target)
        finally:
            target.close()
            source.close()
        self._initialize_database()

    def _excluded_users(self) -> set[str]:
        return {
            _clean_user(value)
            for value in self.settings.loyalty_excluded_users.split(",")
            if _clean_user(value)
        }

    def _points_multiplier(self, author) -> float:
        if bool(getattr(author, "is_broadcaster", False)) or bool(
            getattr(author, "is_mod", False)
        ):
            return max(0.0, float(self.settings.mod_points_multiplier))
        if bool(getattr(author, "is_vip", False)):
            return max(0.0, float(self.settings.vip_points_multiplier))
        if bool(getattr(author, "is_subscriber", False)):
            return max(0.0, float(self.settings.subscriber_points_multiplier))
        return 1.0

    def record_message(
        self,
        username: str,
        display_name: str,
        is_command: bool = False,
        author=None,
    ):
        if not self.settings.loyalty_enabled:
            return
        username = _clean_user(username)
        if not username or username in self._excluded_users():
            return

        now = time.time()
        multiplier = self._points_multiplier(author)
        self.active_users[username] = (display_name or username, now, multiplier)
        self._ensure_user(username, display_name)
        should_reward = self.settings.reward_command_messages or not is_command

        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_message_reward_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            last_reward = float(row["last_message_reward_at"] or 0)
            connection.execute(
                """
                UPDATE users
                SET message_count = message_count + 1,
                    last_seen_at = ?
                WHERE username = ?
                """,
                (_utc_now(), username),
            )
            cooldown = max(0, int(self.settings.message_reward_cooldown_seconds))
            points = max(0, round(int(self.settings.points_per_message) * multiplier))
            if should_reward and points and now - last_reward >= cooldown:
                connection.execute(
                    """
                    UPDATE users
                    SET balance = balance + ?, last_message_reward_at = ?
                    WHERE username = ?
                    """,
                    (points, now, username),
                )
                connection.execute(
                    "INSERT INTO ledger (username, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                    (username, points, "chat message", _utc_now()),
                )

    async def _active_bonus_loop(self):
        while True:
            interval = max(1, int(self.settings.active_bonus_interval_minutes)) * 60
            await asyncio.sleep(interval)
            self.award_active_users()

    def award_active_users(self) -> int:
        if not self.settings.loyalty_enabled:
            return 0
        cutoff = time.time() - max(1, int(self.settings.active_user_window_minutes)) * 60
        bonus = max(0, int(self.settings.active_bonus_points))
        if not bonus:
            return 0
        awarded = 0
        for username, (display_name, last_seen, multiplier) in list(
            self.active_users.items()
        ):
            if last_seen < cutoff:
                self.active_users.pop(username, None)
                continue
            adjusted_bonus = max(0, round(bonus * multiplier))
            self.adjust_balance(
                username,
                adjusted_bonus,
                "active chat bonus",
                display_name,
            )
            with self._connect() as connection:
                connection.execute(
                    "UPDATE users SET active_bonus_count = active_bonus_count + 1 WHERE username = ?",
                    (username,),
                )
            awarded += 1
        return awarded

    async def _timed_message_loop(self):
        while True:
            await asyncio.sleep(10)
            await self.run_due_timers()

    async def run_due_timers(self, now: Optional[float] = None) -> int:
        if (
            not self._accepting_actions
            or not self.settings.automation_enabled
            or self.latest_channel is None
        ):
            return 0
        now = now or time.time()
        triggered = 0
        for raw_rule in self.settings.timed_messages:
            rule = normalize_timed_message_rule(raw_rule)
            if rule is None or not rule["enabled"]:
                continue
            last_run = self.timer_last_run.get(rule["name"], self.started_at)
            if now - last_run < rule["interval_minutes"] * 60:
                continue
            if self.chat_messages_since_timer < rule["minimum_chat_messages"]:
                continue
            event_args = {
                "event": "timer",
                "timerName": rule["name"],
                "chatMessages": self.chat_messages_since_timer,
                "currency": self.settings.currency_name,
            }
            await self._trigger_streamerbot(rule, event_args)
            message = self._format(
                rule["message"],
                timer=rule["name"],
                chat_messages=self.chat_messages_since_timer,
            )
            if message:
                await self.latest_channel.send(message)
            self.timer_last_run[rule["name"]] = now
            triggered += 1
        if triggered:
            self.chat_messages_since_timer = 0
        return triggered

    @staticmethod
    def _has_permission(author, permission: str) -> bool:
        permission = (permission or "everyone").lower()
        is_broadcaster = bool(getattr(author, "is_broadcaster", False))
        is_mod = bool(getattr(author, "is_mod", False)) or is_broadcaster
        is_vip = bool(getattr(author, "is_vip", False)) or is_mod
        is_subscriber = bool(getattr(author, "is_subscriber", False)) or is_vip
        return {
            "everyone": True,
            "subscriber": is_subscriber,
            "vip": is_vip,
            "mod": is_mod,
            "broadcaster": is_broadcaster,
        }.get(permission, False)

    def _currency_for(self, amount: int) -> str:
        return self.settings.currency_singular if abs(int(amount)) == 1 else self.settings.currency_name

    def _format(self, template: str, **values) -> str:
        safe_values = {
            "currency": self.settings.currency_name,
            "currency_singular": self.settings.currency_singular,
            **values,
        }
        try:
            return (template or "").format_map(safe_values)
        except (KeyError, ValueError):
            return template or ""

    async def _send_builtin(self, channel, response_name: str, **values) -> None:
        responses = getattr(self.settings, "builtin_responses", {})
        if isinstance(responses, dict) and response_name in responses:
            template = str(responses[response_name])
        else:
            template = DEFAULT_BUILTIN_RESPONSES[response_name]
        response = self._format(template, **values)
        if response:
            await channel.send(response)

    def _find_custom_command(self, command_name: str) -> Optional[dict[str, Any]]:
        command_name = command_name.lower()
        for raw_rule in self.settings.custom_commands:
            rule = normalize_custom_command_rule(raw_rule)
            if rule is None:
                continue
            if not rule.get("enabled", True):
                continue
            names = [str(rule.get("name", "")), *rule.get("aliases", [])]
            if command_name in {_clean_user(name) for name in names if name}:
                return rule
        return None

    async def _trigger_streamerbot(self, rule: dict[str, Any], args: dict[str, Any]) -> bool:
        action_name = str(rule.get("streamerbot_action", "")).strip()
        action_id = str(rule.get("streamerbot_action_id", "")).strip()
        if not action_name and not action_id:
            return True
        if not self._accepting_actions:
            logger.debug(
                "Ignored Streamer.bot action '%s' because the Twitch bot is stopped.",
                action_name or action_id,
            )
            return False
        if not self.settings.streamerbot_http_enabled:
            logger.warning(
                "Custom command '%s' has a Streamer.bot action but integration is disabled.",
                rule.get("name", ""),
            )
            return False

        action = {}
        if action_id:
            action["id"] = action_id
        elif action_name:
            action["name"] = action_name
        payload = {"action": action, "args": args}
        action_label = action_name or action_id
        current_task = asyncio.current_task()
        if current_task is not None:
            self._streamerbot_tasks.add(current_task)
        try:
            async with ClientSession() as session:
                if not self._accepting_actions:
                    return False
                async with session.post(
                    self.settings.streamerbot_http_url,
                    json=payload,
                    timeout=5,
                ) as response:
                    if response.status not in (200, 204):
                        logger.warning(
                            "Streamer.bot action '%s' returned HTTP %s.",
                            action_label,
                            response.status,
                        )
                        return False
            return True
        except asyncio.CancelledError:
            logger.debug(
                "Cancelled Streamer.bot action '%s' during Twitch bot shutdown.",
                action_label,
            )
            raise
        except (ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Could not trigger Streamer.bot action '%s': %s", action_label, exc)
            return False
        finally:
            if current_task is not None:
                self._streamerbot_tasks.discard(current_task)

    def _streamerbot_endpoint(self, path: str) -> str:
        parsed = urlsplit(self.settings.streamerbot_http_url)
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    async def get_streamerbot_actions(self) -> list[dict[str, str]]:
        endpoint = self._streamerbot_endpoint("/GetActions")
        try:
            async with ClientSession() as session:
                async with session.get(endpoint, timeout=5) as response:
                    if response.status != 200:
                        raise ConnectionError(
                            f"Streamer.bot returned HTTP {response.status}."
                        )
                    payload = await response.json()
        except (ClientError, asyncio.TimeoutError) as exc:
            raise ConnectionError(f"Could not connect to Streamer.bot: {exc}") from exc
        actions = payload.get("actions", [])
        if not isinstance(actions, list):
            raise ConnectionError("Streamer.bot returned an invalid GetActions response.")
        return [
            {"id": str(action.get("id", "")), "name": str(action.get("name", ""))}
            for action in actions
            if isinstance(action, dict) and action.get("name")
        ]

    async def test_streamerbot_action(
        self, action_name: str, action_id: str = ""
    ) -> bool:
        return await self._trigger_streamerbot(
            {
                "name": "connection test",
                "streamerbot_action": action_name,
                "streamerbot_action_id": action_id,
            },
            {
                "event": "connectionTest",
                "source": "Twitch Stream Bot",
                "currency": self.settings.currency_name,
            },
        )

    async def handle_message(self, message) -> bool:
        content = (getattr(message, "content", "") or "").strip()
        author = getattr(message, "author", None)
        username = getattr(author, "name", "") or ""
        display_name = getattr(author, "display_name", "") or username
        is_command = content.startswith("!")
        self.record_message(username, display_name, is_command, author)

        channel = getattr(message, "channel", None)
        should_process_chat = (
            self.settings.loyalty_enabled
            or self.settings.automation_enabled
            or bool(self.settings.raffle_enabled and self.active_raffle)
        )
        if should_process_chat and channel is not None:
            self.latest_channel = channel
            self.chat_messages_since_timer += 1

        if not should_process_chat or not is_command:
            return False

        parts = content[1:].split()
        if not parts:
            return False
        command_name = parts[0].lower()
        arguments = parts[1:]
        if channel is None:
            return False

        if await self._handle_raffle_entry(command_name, username, display_name, channel):
            return True

        if self.settings.loyalty_enabled and command_name == self.settings.cmd_balance.lower():
            target = _clean_user(arguments[0]) if arguments else _clean_user(username)
            self._ensure_user(target, target)
            balance = self.get_balance(target)
            await self._send_builtin(
                channel,
                "balance",
                user=username,
                target=target,
                balance=balance,
                balance_currency=self._currency_for(balance),
            )
            return True

        if (
            self.settings.loyalty_enabled
            and command_name == self.settings.cmd_leaderboard.lower()
        ):
            leaders = self.leaderboard(5)
            if not leaders:
                await self._send_builtin(channel, "leaderboard_empty")
            else:
                summary = " | ".join(
                    f"{index}. {row['display_name']}: {row['balance']}"
                    for index, row in enumerate(leaders, 1)
                )
                await self._send_builtin(
                    channel,
                    "leaderboard",
                    leaderboard=summary,
                )
            return True

        if (
            self.settings.loyalty_enabled
            and command_name == self.settings.cmd_give_points.lower()
        ):
            if len(arguments) < 2:
                await channel.send(
                    f"Usage: !{command_name} <user> <amount>"
                )
                return True
            try:
                amount = int(arguments[1])
            except ValueError:
                await channel.send("Amount must be a positive whole number.")
                return True
            target = _clean_user(arguments[0])
            result = self.transfer(username, target, amount)
            if result is None:
                await channel.send(
                    f"@{username}, that transfer could not be completed."
                )
                return True
            sender_balance, _ = result
            await self._send_builtin(
                channel,
                "give_points",
                user=username,
                target=target,
                amount=amount,
                amount_currency=self._currency_for(amount),
                balance=sender_balance,
            )
            return True

        if (
            self.settings.loyalty_enabled
            and self.settings.gambling_enabled
            and command_name == self.settings.cmd_gamble.lower()
        ):
            if not arguments:
                await channel.send(
                    f"Usage: !{command_name} <amount|all> "
                    f"(min {self.settings.gamble_minimum}, max {self.settings.gamble_maximum})"
                )
                return True
            balance = self.get_balance(username)
            amount = self._parse_wager(
                arguments[0],
                balance,
                self.settings.gamble_minimum,
                self.settings.gamble_maximum,
            )
            if amount is None:
                await channel.send(
                    f"@{username}, choose a wager between "
                    f"{self.settings.gamble_minimum} and {self.settings.gamble_maximum} "
                    f"that you can afford."
                )
                return True
            if self._game_on_cooldown(
                "gamble", username, self.settings.gamble_cooldown_seconds
            ):
                return True
            won = self._random() < max(
                0.0,
                min(100.0, float(self.settings.gamble_win_chance_percent)),
            ) / 100.0
            result = self.settle_gamble(username, amount, won)
            if result is None:
                await channel.send(f"@{username}, that wager could not be completed.")
                return True
            new_balance, delta = result
            if won:
                await self._send_builtin(
                    channel,
                    "gamble_win",
                    user=username,
                    amount=amount,
                    amount_currency=self._currency_for(amount),
                    winnings=delta,
                    winnings_currency=self._currency_for(delta),
                    balance=new_balance,
                )
            else:
                await self._send_builtin(
                    channel,
                    "gamble_loss",
                    user=username,
                    amount=amount,
                    amount_currency=self._currency_for(amount),
                    balance=new_balance,
                )
            return True

        self._expire_duels()
        if (
            self.settings.loyalty_enabled
            and self.settings.duels_enabled
            and command_name == self.settings.cmd_duel.lower()
        ):
            if len(arguments) < 2:
                await channel.send(f"Usage: !{command_name} <user> <amount|all>")
                return True
            challenger = _clean_user(username)
            opponent = _clean_user(arguments[0])
            if not opponent or opponent == challenger:
                await channel.send(f"@{username}, choose another viewer to duel.")
                return True
            balance = self.get_balance(challenger)
            amount = self._parse_wager(
                arguments[1],
                balance,
                self.settings.duel_minimum,
                self.settings.duel_maximum,
            )
            if amount is None or self.get_balance(opponent) < amount:
                await channel.send(
                    f"@{username}, both duelists must be able to wager between "
                    f"{self.settings.duel_minimum} and {self.settings.duel_maximum}."
                )
                return True
            if self._has_pending_duel(challenger) or self._has_pending_duel(opponent):
                await channel.send(
                    f"@{username}, one of you already has a pending duel."
                )
                return True
            if self._game_on_cooldown(
                "duel", challenger, self.settings.duel_cooldown_seconds
            ):
                return True
            self.pending_duels[opponent] = {
                "challenger": challenger,
                "amount": amount,
                "created_at": time.time(),
            }
            await self._send_builtin(
                channel,
                "duel_challenge",
                opponent=opponent,
                challenger=challenger,
                amount=amount,
                amount_currency=self._currency_for(amount),
                accept_command=self.settings.cmd_duel_accept,
                decline_command=self.settings.cmd_duel_decline,
                timeout=max(10, int(self.settings.duel_timeout_seconds)),
            )
            return True

        if (
            self.settings.loyalty_enabled
            and self.settings.duels_enabled
            and command_name == self.settings.cmd_duel_accept.lower()
        ):
            opponent = _clean_user(username)
            challenge = self.pending_duels.pop(opponent, None)
            if challenge is None:
                await channel.send(f"@{username}, you have no pending duel.")
                return True
            challenger = str(challenge["challenger"])
            amount = int(challenge["amount"])
            winner = challenger if self._random() < 0.5 else opponent
            result = self.settle_duel(challenger, opponent, amount, winner)
            if result is None:
                await channel.send(
                    f"@{username}, the duel was cancelled because both viewers no "
                    "longer have enough points."
                )
                return True
            await self._send_builtin(
                channel,
                "duel_result",
                winner=result["winner"],
                loser=result["loser"],
                amount=amount,
                amount_currency=self._currency_for(amount),
                winner_balance=result["winner_balance"],
                loser_balance=result["loser_balance"],
            )
            return True

        if (
            self.settings.loyalty_enabled
            and self.settings.duels_enabled
            and command_name == self.settings.cmd_duel_decline.lower()
        ):
            opponent = _clean_user(username)
            challenge = self.pending_duels.pop(opponent, None)
            if challenge is None:
                await channel.send(f"@{username}, you have no pending duel.")
            else:
                await self._send_builtin(
                    channel,
                    "duel_decline",
                    user=username,
                    challenger=challenge["challenger"],
                    amount=challenge["amount"],
                    amount_currency=self._currency_for(challenge["amount"]),
                )
            return True

        admin_commands = {
            self.settings.cmd_add_points.lower(): 1,
            self.settings.cmd_remove_points.lower(): -1,
        }
        if self.settings.loyalty_enabled and command_name in admin_commands:
            if not self._has_permission(author, "mod"):
                return True
            if len(arguments) < 2:
                await channel.send(f"Usage: !{command_name} <user> <amount>")
                return True
            try:
                amount = abs(int(arguments[1])) * admin_commands[command_name]
            except ValueError:
                await channel.send("Amount must be a whole number.")
                return True
            target = _clean_user(arguments[0])
            balance = self.adjust_balance(target, amount, f"manual adjustment by {username}")
            await self._send_builtin(
                channel,
                "points_adjusted",
                user=username,
                target=target,
                amount=amount,
                balance=balance,
                balance_currency=self._currency_for(balance),
            )
            return True

        if not self.settings.automation_enabled:
            return False

        rule = self._find_custom_command(command_name)
        if not rule:
            return False
        if not self._has_permission(author, str(rule.get("permission", "everyone"))):
            return True

        now = time.time()
        global_key = (command_name, "*")
        user_key = (command_name, _clean_user(username))
        if now < self.command_cooldowns.get(global_key, 0):
            return True
        if now < self.command_cooldowns.get(user_key, 0):
            return True

        cost = max(0, int(rule.get("cost", 0)))
        balance = self.get_balance(username)
        if balance < cost:
            template = rule.get(
                "insufficient_funds_response",
                "@{user}, you need {cost} {currency} to use !{command}.",
            )
            await channel.send(
                self._format(
                    template,
                    user=username,
                    command=command_name,
                    cost=cost,
                    balance=balance,
                    args=" ".join(arguments),
                )
            )
            return True

        event_args = {
            "user": username,
            "displayName": display_name,
            "command": command_name,
            "arguments": " ".join(arguments),
            "balance": balance,
            "cost": cost,
            "currency": self.settings.currency_name,
        }
        if not await self._trigger_streamerbot(rule, event_args):
            if rule.get("streamerbot_action") or rule.get("streamerbot_action_id"):
                await channel.send(f"@{username}, that action is temporarily unavailable.")
            return True

        if cost:
            balance = self.spend(username, cost, f"custom command !{command_name}")
            if balance is None:
                return True

        self.command_cooldowns[global_key] = now + max(0, int(rule.get("cooldown_seconds", 0)))
        self.command_cooldowns[user_key] = now + max(
            0, int(rule.get("user_cooldown_seconds", 0))
        )

        response = self._format(
            str(rule.get("response", "")),
            user=username,
            command=command_name,
            args=" ".join(arguments),
            balance=balance,
            cost=cost,
        )
        if response:
            await channel.send(response)
        return True
