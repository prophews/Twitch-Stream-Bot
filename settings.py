import os
import json
import sys
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError, Field

PROJECT_ROOT = Path(__file__).resolve().parent
APP_VERSION = "2.3.0"

DEFAULT_BUILTIN_RESPONSES = {
    "balance": "@{user}, {target} has {balance} {balance_currency}.",
    "leaderboard": "Top {currency}: {leaderboard}",
    "leaderboard_empty": "No {currency} have been earned yet.",
    "give_points": (
        "@{user} gave {amount} {amount_currency} to @{target} and has "
        "{balance} remaining."
    ),
    "gamble_win": (
        "@{user} won {winnings} {winnings_currency} gambling {amount} "
        "and now has {balance}!"
    ),
    "gamble_loss": (
        "@{user} lost {amount} {amount_currency} and has {balance} remaining."
    ),
    "duel_challenge": (
        "@{opponent}, @{challenger} challenged you to a duel for {amount} "
        "{amount_currency}! Use !{accept_command} or !{decline_command} within "
        "{timeout} seconds."
    ),
    "duel_result": (
        "@{winner} defeated @{loser} and won {amount} {amount_currency}! "
        "Winner balance: {winner_balance}."
    ),
    "duel_decline": "@{user} declined @{challenger}'s duel.",
    "points_adjusted": "{target} now has {balance} {balance_currency}.",
}

PROFILE_EXCLUDED_ALIASES = {
    "OAUTH_TOKEN",
    "SR_QUEUE_SNAPSHOT_PATH",
    "BOT_STATE_PATH",
    "LOYALTY_DATABASE_PATH",
    "PROFILES",
    "OBS_WS_PASSWORD",
}
_CONFIG_WRITE_LOCK = threading.Lock()


def get_runtime_root() -> Path:
    """Return the writable root for user-specific runtime files."""
    if getattr(sys, "frozen", False):
        local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        return local_appdata / "Twitch Song Request Bot"
    return PROJECT_ROOT


def get_runtime_data_dir() -> Path:
    return get_runtime_root() / "data"


def get_config_path() -> Path:
    return get_runtime_root() / "config.json"


def get_legacy_config_paths() -> list[Path]:
    paths: list[Path] = []

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        paths.append(exe_dir / "config.json")
        paths.append(exe_dir / "_internal" / "config.json")

    paths.append(PROJECT_ROOT / "config.json")

    # Preserve order while de-duplicating.
    deduped: list[Path] = []
    seen = set()
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped

class BotSettings(BaseModel):
    """Configuration for the Standalone SR Bot."""
    
    oauth_token: str = Field("", alias="OAUTH_TOKEN")
    channel: str = Field("", alias="CHANNEL")

    vlc_sr_volume: int = Field(60, alias="VLC_SR_VOLUME")
    sr_avg_download_mbps: float = Field(5.0, alias="SR_AVG_DOWNLOAD_MBPS")
    sr_queue_snapshot_path: Path = Field(default_factory=lambda: get_runtime_data_dir() / "sr_queue.json", alias="SR_QUEUE_SNAPSHOT_PATH")
    bot_state_path: Path = Field(default_factory=lambda: get_runtime_data_dir() / "bot_state.json", alias="BOT_STATE_PATH")

    # New Features
    use_fair_queue: bool = Field(True, alias="USE_FAIR_QUEUE")
    sr_window_position: str = Field("Bottom Left", alias="SR_WINDOW_POSITION")
    sr_bg_opacity: int = Field(100, alias="SR_BG_OPACITY")
    sr_window_width: int = Field(640, alias="SR_WINDOW_WIDTH")
    sr_window_height: int = Field(360, alias="SR_WINDOW_HEIGHT")
    sr_title_font_size: int = Field(11, alias="SR_TITLE_FONT_SIZE")
    sr_time_font_size: int = Field(11, alias="SR_TIME_FONT_SIZE")
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict, alias="PROFILES")
    web_server_port: int = Field(8081, alias="WEB_SERVER_PORT")
    local_library_enabled: bool = Field(False, alias="LOCAL_LIBRARY_ENABLED")
    local_library_root: str = Field("", alias="LOCAL_LIBRARY_ROOT")

    # General-purpose bot / loyalty settings (2.0)
    loyalty_enabled: bool = Field(False, alias="LOYALTY_ENABLED")
    automation_enabled: bool = Field(False, alias="AUTOMATION_ENABLED")
    loyalty_database_path: Path = Field(
        default_factory=lambda: get_runtime_data_dir() / "loyalty.sqlite3",
        alias="LOYALTY_DATABASE_PATH",
    )
    currency_name: str = Field("points", alias="CURRENCY_NAME")
    currency_singular: str = Field("point", alias="CURRENCY_SINGULAR")
    starting_balance: int = Field(0, alias="STARTING_BALANCE")
    points_per_message: int = Field(1, alias="POINTS_PER_MESSAGE")
    subscriber_points_multiplier: float = Field(
        1.0, alias="SUBSCRIBER_POINTS_MULTIPLIER"
    )
    vip_points_multiplier: float = Field(1.0, alias="VIP_POINTS_MULTIPLIER")
    mod_points_multiplier: float = Field(1.0, alias="MOD_POINTS_MULTIPLIER")
    loyalty_excluded_users: str = Field("", alias="LOYALTY_EXCLUDED_USERS")
    message_reward_cooldown_seconds: int = Field(60, alias="MESSAGE_REWARD_COOLDOWN_SECONDS")
    active_bonus_points: int = Field(5, alias="ACTIVE_BONUS_POINTS")
    active_bonus_interval_minutes: int = Field(10, alias="ACTIVE_BONUS_INTERVAL_MINUTES")
    active_user_window_minutes: int = Field(15, alias="ACTIVE_USER_WINDOW_MINUTES")
    reward_command_messages: bool = Field(False, alias="REWARD_COMMAND_MESSAGES")
    cmd_balance: str = Field("points", alias="CMD_BALANCE")
    cmd_leaderboard: str = Field("top", alias="CMD_LEADERBOARD")
    cmd_give_points: str = Field("givepoints", alias="CMD_GIVE_POINTS")
    cmd_add_points: str = Field("addpoints", alias="CMD_ADD_POINTS")
    cmd_remove_points: str = Field("removepoints", alias="CMD_REMOVE_POINTS")
    gambling_enabled: bool = Field(True, alias="GAMBLING_ENABLED")
    cmd_gamble: str = Field("gamble", alias="CMD_GAMBLE")
    gamble_minimum: int = Field(1, alias="GAMBLE_MINIMUM")
    gamble_maximum: int = Field(10000, alias="GAMBLE_MAXIMUM")
    gamble_win_chance_percent: float = Field(
        50.0, alias="GAMBLE_WIN_CHANCE_PERCENT"
    )
    gamble_payout_multiplier: float = Field(
        2.0, alias="GAMBLE_PAYOUT_MULTIPLIER"
    )
    gamble_cooldown_seconds: int = Field(10, alias="GAMBLE_COOLDOWN_SECONDS")
    duels_enabled: bool = Field(True, alias="DUELS_ENABLED")
    cmd_duel: str = Field("duel", alias="CMD_DUEL")
    cmd_duel_accept: str = Field("accept", alias="CMD_DUEL_ACCEPT")
    cmd_duel_decline: str = Field("decline", alias="CMD_DUEL_DECLINE")
    duel_minimum: int = Field(1, alias="DUEL_MINIMUM")
    duel_maximum: int = Field(10000, alias="DUEL_MAXIMUM")
    duel_timeout_seconds: int = Field(60, alias="DUEL_TIMEOUT_SECONDS")
    duel_cooldown_seconds: int = Field(30, alias="DUEL_COOLDOWN_SECONDS")
    builtin_responses: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_BUILTIN_RESPONSES),
        alias="BUILTIN_RESPONSES",
    )
    custom_commands: list[dict[str, Any]] = Field(default_factory=list, alias="CUSTOM_COMMANDS")
    timed_messages: list[dict[str, Any]] = Field(default_factory=list, alias="TIMED_MESSAGES")
    streamerbot_http_enabled: bool = Field(False, alias="STREAMERBOT_HTTP_ENABLED")
    streamerbot_http_url: str = Field(
        "http://127.0.0.1:7474/DoAction",
        alias="STREAMERBOT_HTTP_URL",
    )

    # OBS automation settings
    obs_ws_enabled: bool = Field(False, alias="OBS_WS_ENABLED")
    obs_ws_host: str = Field("127.0.0.1", alias="OBS_WS_HOST")
    obs_ws_port: int = Field(4455, alias="OBS_WS_PORT")
    obs_ws_password: str = Field("", alias="OBS_WS_PASSWORD")
    obs_browser_source_name: str = Field("", alias="OBS_BROWSER_SOURCE_NAME")
    obs_browser_scene_name: str = Field("", alias="OBS_BROWSER_SCENE_NAME")
    obs_force_show_on_play: bool = Field(False, alias="OBS_FORCE_SHOW_ON_PLAY")
    obs_hide_when_idle: bool = Field(False, alias="OBS_HIDE_WHEN_IDLE")
    obs_auto_refresh: bool = Field(False, alias="OBS_AUTO_REFRESH")

    # Configurable Twitch command aliases
    cmd_sr: str = Field("sr", alias="CMD_SR")
    cmd_skip: str = Field("skip", alias="CMD_SKIP")
    cmd_pause: str = Field("pause", alias="CMD_PAUSE")
    cmd_play: str = Field("play", alias="CMD_PLAY")
    cmd_hide: str = Field("hide", alias="CMD_HIDE")
    cmd_show: str = Field("show", alias="CMD_SHOW")
    cmd_queue: str = Field("queue", alias="CMD_QUEUE")
    cmd_wrongsong: str = Field("wrongsong", alias="CMD_WRONGSONG")
    cmd_clearqueue: str = Field("clearqueue", alias="CMD_CLEARQUEUE")
    cmd_full: str = Field("full", alias="CMD_FULL")
    cmd_info: str = Field("info", alias="CMD_INFO")
    cmd_sron: str = Field("sron", alias="CMD_SRON")
    cmd_sroff: str = Field("sroff", alias="CMD_SROFF")

    @property
    def oauth_token_prefixed(self) -> str:
        token = self.oauth_token or ""
        if token and not token.startswith("oauth:"):
            return f"oauth:{token}"
        return token

    def normalize_runtime_paths(self) -> None:
        runtime_root = get_runtime_root()
        if not self.sr_queue_snapshot_path.is_absolute():
            self.sr_queue_snapshot_path = runtime_root / self.sr_queue_snapshot_path
        if not self.bot_state_path.is_absolute():
            self.bot_state_path = runtime_root / self.bot_state_path
        if not self.loyalty_database_path.is_absolute():
            self.loyalty_database_path = runtime_root / self.loyalty_database_path

    def ensure_runtime_paths(self) -> None:
        self.normalize_runtime_paths()
        self.sr_queue_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.bot_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.loyalty_database_path.parent.mkdir(parents=True, exist_ok=True)

def load_settings() -> BotSettings:
    """Loads settings from config.json generated by the GUI."""
    config_path = get_config_path()
    if not config_path.exists():
        legacy_path = next((path for path in get_legacy_config_paths() if path.exists()), None)
        if legacy_path is None:
            settings = BotSettings()
            settings.normalize_runtime_paths()
            return settings
        config_path = legacy_path

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        settings = BotSettings(**data)
        settings.normalize_runtime_paths()
        return settings
    except Exception as e:
        print(f"Error loading config.json: {e}")
        settings = BotSettings()
        settings.normalize_runtime_paths()
        return settings

def save_settings(settings: BotSettings) -> None:
    """Saves the settings to config.json from the GUI."""
    settings.normalize_runtime_paths()
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    data = {
        "OAUTH_TOKEN": settings.oauth_token,
        "CHANNEL": settings.channel,
        "VLC_SR_VOLUME": settings.vlc_sr_volume,
        "SR_AVG_DOWNLOAD_MBPS": settings.sr_avg_download_mbps,
        "SR_QUEUE_SNAPSHOT_PATH": str(settings.sr_queue_snapshot_path),
        "BOT_STATE_PATH": str(settings.bot_state_path),
        
        "USE_FAIR_QUEUE": settings.use_fair_queue,
        "SR_WINDOW_POSITION": settings.sr_window_position,
        "SR_BG_OPACITY": settings.sr_bg_opacity,
        "SR_WINDOW_WIDTH": settings.sr_window_width,
        "SR_WINDOW_HEIGHT": settings.sr_window_height,
        "SR_TITLE_FONT_SIZE": settings.sr_title_font_size,
        "SR_TIME_FONT_SIZE": settings.sr_time_font_size,
        "PROFILES": settings.profiles,
        "WEB_SERVER_PORT": settings.web_server_port,
        "LOCAL_LIBRARY_ENABLED": settings.local_library_enabled,
        "LOCAL_LIBRARY_ROOT": settings.local_library_root,
        "LOYALTY_ENABLED": settings.loyalty_enabled,
        "AUTOMATION_ENABLED": settings.automation_enabled,
        "LOYALTY_DATABASE_PATH": str(settings.loyalty_database_path),
        "CURRENCY_NAME": settings.currency_name,
        "CURRENCY_SINGULAR": settings.currency_singular,
        "STARTING_BALANCE": settings.starting_balance,
        "POINTS_PER_MESSAGE": settings.points_per_message,
        "SUBSCRIBER_POINTS_MULTIPLIER": settings.subscriber_points_multiplier,
        "VIP_POINTS_MULTIPLIER": settings.vip_points_multiplier,
        "MOD_POINTS_MULTIPLIER": settings.mod_points_multiplier,
        "LOYALTY_EXCLUDED_USERS": settings.loyalty_excluded_users,
        "MESSAGE_REWARD_COOLDOWN_SECONDS": settings.message_reward_cooldown_seconds,
        "ACTIVE_BONUS_POINTS": settings.active_bonus_points,
        "ACTIVE_BONUS_INTERVAL_MINUTES": settings.active_bonus_interval_minutes,
        "ACTIVE_USER_WINDOW_MINUTES": settings.active_user_window_minutes,
        "REWARD_COMMAND_MESSAGES": settings.reward_command_messages,
        "CMD_BALANCE": settings.cmd_balance,
        "CMD_LEADERBOARD": settings.cmd_leaderboard,
        "CMD_GIVE_POINTS": settings.cmd_give_points,
        "CMD_ADD_POINTS": settings.cmd_add_points,
        "CMD_REMOVE_POINTS": settings.cmd_remove_points,
        "GAMBLING_ENABLED": settings.gambling_enabled,
        "CMD_GAMBLE": settings.cmd_gamble,
        "GAMBLE_MINIMUM": settings.gamble_minimum,
        "GAMBLE_MAXIMUM": settings.gamble_maximum,
        "GAMBLE_WIN_CHANCE_PERCENT": settings.gamble_win_chance_percent,
        "GAMBLE_PAYOUT_MULTIPLIER": settings.gamble_payout_multiplier,
        "GAMBLE_COOLDOWN_SECONDS": settings.gamble_cooldown_seconds,
        "DUELS_ENABLED": settings.duels_enabled,
        "CMD_DUEL": settings.cmd_duel,
        "CMD_DUEL_ACCEPT": settings.cmd_duel_accept,
        "CMD_DUEL_DECLINE": settings.cmd_duel_decline,
        "DUEL_MINIMUM": settings.duel_minimum,
        "DUEL_MAXIMUM": settings.duel_maximum,
        "DUEL_TIMEOUT_SECONDS": settings.duel_timeout_seconds,
        "DUEL_COOLDOWN_SECONDS": settings.duel_cooldown_seconds,
        "BUILTIN_RESPONSES": settings.builtin_responses,
        "CUSTOM_COMMANDS": settings.custom_commands,
        "TIMED_MESSAGES": settings.timed_messages,
        "STREAMERBOT_HTTP_ENABLED": settings.streamerbot_http_enabled,
        "STREAMERBOT_HTTP_URL": settings.streamerbot_http_url,
        "OBS_WS_ENABLED": settings.obs_ws_enabled,
        "OBS_WS_HOST": settings.obs_ws_host,
        "OBS_WS_PORT": settings.obs_ws_port,
        "OBS_WS_PASSWORD": settings.obs_ws_password,
        "OBS_BROWSER_SOURCE_NAME": settings.obs_browser_source_name,
        "OBS_BROWSER_SCENE_NAME": settings.obs_browser_scene_name,
        "OBS_FORCE_SHOW_ON_PLAY": settings.obs_force_show_on_play,
        "OBS_HIDE_WHEN_IDLE": settings.obs_hide_when_idle,
        "OBS_AUTO_REFRESH": settings.obs_auto_refresh,
        "CMD_SR": settings.cmd_sr,
        "CMD_SKIP": settings.cmd_skip,
        "CMD_PAUSE": settings.cmd_pause,
        "CMD_PLAY": settings.cmd_play,
        "CMD_HIDE": settings.cmd_hide,
        "CMD_SHOW": settings.cmd_show,
        "CMD_QUEUE": settings.cmd_queue,
        "CMD_WRONGSONG": settings.cmd_wrongsong,
        "CMD_CLEARQUEUE": settings.cmd_clearqueue,
        "CMD_FULL": settings.cmd_full,
        "CMD_INFO": settings.cmd_info,
        "CMD_SRON": settings.cmd_sron,
        "CMD_SROFF": settings.cmd_sroff,
    }
    
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with _CONFIG_WRITE_LOCK:
        with open(temporary_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, config_path)


def profile_settings_payload(settings: BotSettings) -> dict[str, Any]:
    """Capture all configurable non-secret settings for a stream profile."""
    payload = settings.model_dump(mode="json", by_alias=True)
    for alias in PROFILE_EXCLUDED_ALIASES:
        payload.pop(alias, None)
    return payload


def apply_profile_settings(
    settings: BotSettings, payload: Any
) -> BotSettings:
    """Validate and apply a versioned profile settings payload."""
    if not isinstance(payload, dict):
        return settings
    merged = settings.model_dump(mode="json", by_alias=True)
    for alias, value in payload.items():
        if alias in PROFILE_EXCLUDED_ALIASES or alias not in merged:
            continue
        merged[alias] = value
    updated = BotSettings(**merged)
    updated.profiles = settings.profiles
    updated.oauth_token = settings.oauth_token
    updated.sr_queue_snapshot_path = settings.sr_queue_snapshot_path
    updated.bot_state_path = settings.bot_state_path
    updated.loyalty_database_path = settings.loyalty_database_path
    updated.normalize_runtime_paths()
    return updated
