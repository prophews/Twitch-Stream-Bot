import logging
import asyncio
import json
import sys
import hashlib
from pathlib import Path
from typing import Callable, Optional, Dict
from twitchio.ext import commands
from aiohttp import web, WSCloseCode
import weakref

from settings import load_settings, save_settings, apply_profile_settings
from obs_controller import OBSController
from loyalty_engine import LoyaltyEngine

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
bot_logger = logging.getLogger("StandaloneBot")

class Bot(commands.Bot):
    # Priorities (Simplified)
    # For now, SR is the only priority system.
    IDLE_PRIORITY = "IDLE"
    VLC_SR_PRIORITY = "VLC_SR"

    def __init__(self, logger=bot_logger, profile_applied_callback: Optional[Callable] = None):
        self.settings = load_settings()
        self.settings.ensure_runtime_paths()
        self.profile_applied_callback = profile_applied_callback

        super().__init__(
            token=self.settings.oauth_token_prefixed,
            prefix='!',
            initial_channels=[self.settings.channel]
        )

        # State
        self.current_media_priority = self.IDLE_PRIORITY # Default to Idle mode
        self.main_vlc_content_type: Optional[str] = None
        self.main_vlc_now_playing_details: Optional[Dict] = None

        self.is_sr_enabled = True
        self.is_fullscreen_playing = False
        self.is_progress_visible = False
        self.is_title_visible = False
        self.is_time_visible = False
        self.main_vlc_is_paused = False
        self.main_vlc_current_time = 0
        self.main_vlc_duration = 0

        self.sr_volume_level = self.settings.vlc_sr_volume
        self.sr_avg_download_mbps = self.settings.sr_avg_download_mbps

        if getattr(sys, 'frozen', False):
            self.base_path = Path(sys._MEIPASS)
        else:
            self.base_path = Path(__file__).parent

        # Web Server State
        self.app = web.Application()
        self.app.add_routes([
            web.get('/player', self.handle_player_html),
            web.get('/ws', self.websocket_handler),
            web.get('/media/{media_id}', self.handle_media_file),
            web.get('/art/{art_id}', self.handle_art_file),
            web.get('/api/health', self.handle_api_health),
            web.get('/api/profiles', self.handle_api_profiles),
            web.get('/api/profiles/apply', self.handle_api_apply_profile),
            web.post('/api/profiles/apply', self.handle_api_apply_profile),
            web.get('/api/loyalty/balance', self.handle_api_loyalty_balance),
            web.get('/api/loyalty/leaderboard', self.handle_api_loyalty_leaderboard),
            web.post('/api/loyalty/adjust', self.handle_api_loyalty_adjust),
        ])
        media_path = self.base_path / 'temp_sr'
        media_path.mkdir(exist_ok=True)
        
        self.websockets = weakref.WeakSet()
        self.ws_play_sent = set()  # tracks which WS connections have received a play command
        self.approved_media_paths: Dict[str, Path] = {}
        self.approved_art_paths: Dict[str, Path] = {}
        self._obs_page_ready = asyncio.Event()
        self._obs_reload_done = False  # force fresh page load once per startup
        self._startup_time = asyncio.get_event_loop().time()  # guard against stale media_ended
        self._STARTUP_GRACE = 8.0  # ignore media_ended for this many seconds after start
        self._processing_media_ended = False  # prevent flood of media_ended from multi-WS
        self.runner = None
        self.site = None
        self._shutdown_started = False
        self._close_started = False
        self.obs_controller = OBSController(self.settings, bot_logger)
        self.loyalty = LoyaltyEngine(self.settings)

    async def start_web_server(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', self.settings.web_server_port)
        await self.site.start()
        bot_logger.info(f"Local Server running on http://127.0.0.1:{self.settings.web_server_port}/player")

    @property
    def player_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.web_server_port}/player"

    async def handle_player_html(self, request):
        player_static_path = self.base_path / 'player.html'
        if player_static_path.exists():
            response = web.FileResponse(player_static_path)
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response
        return web.Response(
            text="Player HTML not found at " + str(player_static_path),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )

    async def handle_media_file(self, request):
        media_id = request.match_info.get("media_id", "")
        media_path = self.approved_media_paths.get(media_id)

        if media_path is None:
            raise web.HTTPNotFound(text=f"Media is not approved for playback: {media_id}")

        media_path = Path(media_path)
        if not media_path.exists() or not media_path.is_file():
            raise web.HTTPNotFound(text=f"Media file not found for id: {media_id}")

        response = web.FileResponse(media_path)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    async def handle_art_file(self, request):
        art_id = request.match_info.get("art_id", "")
        art_path = self.approved_art_paths.get(art_id)

        if art_path is None:
            raise web.HTTPNotFound(text=f"Artwork is not approved for playback: {art_id}")

        art_path = Path(art_path)
        if not art_path.exists() or not art_path.is_file():
            raise web.HTTPNotFound(text=f"Artwork file not found for id: {art_id}")

        response = web.FileResponse(art_path)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    async def handle_api_health(self, request):
        return web.json_response({
            "ok": True,
            "service": "Twitch Song Request Bot",
            "profile_count": len(self.settings.profiles),
        })

    async def handle_api_profiles(self, request):
        return web.json_response({
            "ok": True,
            "profiles": sorted(self.settings.profiles, key=str.casefold),
        })

    async def handle_api_apply_profile(self, request):
        name = request.query.get("name", "").strip()
        if not name and request.can_read_body:
            try:
                payload = await request.json()
            except (json.JSONDecodeError, web.HTTPBadRequest):
                payload = {}
            name = str(payload.get("name", "")).strip()

        if not name:
            return web.json_response(
                {"ok": False, "error": "Missing profile name."},
                status=400,
            )

        profile = await self.apply_stream_profile(name)
        if profile is None:
            return web.json_response(
                {"ok": False, "error": f"Unknown profile: {name}"},
                status=404,
            )

        return web.json_response({"ok": True, "profile": name, "settings": profile})

    async def apply_stream_profile(self, name: str) -> Optional[dict]:
        profile = self.settings.profiles.get(name)
        if not profile:
            return None

        previous_channel = self.settings.channel
        previous_port = self.settings.web_server_port
        self.settings = apply_profile_settings(
            self.settings, profile.get("settings")
        )
        self.loyalty.settings = self.settings
        self.obs_controller.settings = self.settings
        normalized = {
            "accept_requests": bool(profile.get("accept_requests", True)),
            "show_title": bool(profile.get("show_title", False)),
            "show_time": bool(profile.get("show_time", False)),
            "show_progress": bool(profile.get("show_progress", False)),
            "window_position": str(
                profile.get("window_position", self.settings.sr_window_position)
            ),
            "window_width": max(
                160,
                min(
                    1920,
                    int(profile.get("window_width", self.settings.sr_window_width)),
                ),
            ),
            "window_height": max(
                90,
                min(
                    1080,
                    int(profile.get("window_height", self.settings.sr_window_height)),
                ),
            ),
            "background_opacity": max(
                0,
                min(
                    100,
                    int(
                        profile.get(
                            "background_opacity", self.settings.sr_bg_opacity
                        )
                    ),
                ),
            ),
            "title_font_size": max(
                8,
                min(
                    48,
                    int(
                        profile.get(
                            "title_font_size", self.settings.sr_title_font_size
                        )
                    ),
                ),
            ),
            "time_font_size": max(
                8,
                min(
                    48,
                    int(
                        profile.get(
                            "time_font_size", self.settings.sr_time_font_size
                        )
                    ),
                ),
            ),
        }

        self.settings.sr_window_position = normalized["window_position"]
        self.settings.sr_window_width = normalized["window_width"]
        self.settings.sr_window_height = normalized["window_height"]
        self.settings.sr_bg_opacity = normalized["background_opacity"]
        self.settings.sr_title_font_size = normalized["title_font_size"]
        self.settings.sr_time_font_size = normalized["time_font_size"]
        self.is_sr_enabled = normalized["accept_requests"]

        await self.vlc_set_position(normalized["window_position"])
        await self.vlc_set_bg_opacity(normalized["background_opacity"])
        await self.vlc_set_window_size(normalized["window_width"], normalized["window_height"])
        await self.vlc_set_hud_font_sizes(
            normalized["title_font_size"],
            normalized["time_font_size"],
        )
        await self.vlc_set_title_visible(normalized["show_title"])
        await self.vlc_set_time_visible(normalized["show_time"])
        await self.vlc_set_progress_visible(normalized["show_progress"])

        save_settings(self.settings)
        self.save_bot_state()
        if self.profile_applied_callback:
            self.profile_applied_callback(
                name,
                {
                    **profile,
                    **normalized,
                },
            )
        bot_logger.info(f"Applied stream profile '{name}' through local automation API.")
        if (
            self.settings.channel != previous_channel
            or self.settings.web_server_port != previous_port
        ):
            bot_logger.warning(
                "Profile changed the Twitch channel or localhost port. Restart the "
                "bot before those connection settings take effect."
            )
        return {**profile, **normalized}

    async def handle_api_loyalty_balance(self, request):
        username = request.query.get("user", "").strip()
        if not username:
            return web.json_response({"ok": False, "error": "Missing user."}, status=400)
        return web.json_response({
            "ok": True,
            "user": username.lstrip("@").lower(),
            "balance": self.loyalty.get_balance(username),
            "currency": self.settings.currency_name,
        })

    async def handle_api_loyalty_leaderboard(self, request):
        try:
            limit = int(request.query.get("limit", "5"))
        except ValueError:
            limit = 5
        return web.json_response({
            "ok": True,
            "currency": self.settings.currency_name,
            "leaders": self.loyalty.leaderboard(limit),
        })

    async def handle_api_loyalty_adjust(self, request):
        try:
            payload = await request.json()
            username = str(payload.get("user", "")).strip()
            amount = int(payload.get("amount", 0))
        except (json.JSONDecodeError, TypeError, ValueError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "Expected JSON with user and whole-number amount."},
                status=400,
            )
        if not username:
            return web.json_response({"ok": False, "error": "Missing user."}, status=400)
        balance = self.loyalty.adjust_balance(
            username,
            amount,
            str(payload.get("reason", "local API adjustment")),
        )
        return web.json_response({
            "ok": True,
            "user": username.lstrip("@").lower(),
            "balance": balance,
            "currency": self.settings.currency_name,
        })

    def _build_media_id(self, song_details: dict, file_path: str) -> str:
        source_type = song_details.get("source_type", "downloaded")
        stable_value = (
            song_details.get("library_path")
            or song_details.get("id")
            or song_details.get("original_url")
            or file_path
        )
        digest = hashlib.sha256(f"{source_type}:{stable_value}".encode("utf-8")).hexdigest()[:24]
        prefix = "local" if source_type == "local_library" else "sr"
        return f"{prefix}_{digest}"

    def register_song_media(self, song_details: Optional[dict]) -> Optional[str]:
        if not song_details:
            return None

        file_path = song_details.get("filepath")
        if not file_path:
            return None

        media_id = song_details.get("media_id") or self._build_media_id(song_details, file_path)
        song_details["media_id"] = media_id
        self.approved_media_paths[media_id] = Path(file_path)
        return media_id

    def _build_art_id(self, song_details: dict, art_path: str) -> str:
        stable_value = (
            song_details.get("library_path")
            or song_details.get("id")
            or art_path
        )
        digest = hashlib.sha256(f"art:{stable_value}:{art_path}".encode("utf-8")).hexdigest()[:24]
        return f"art_{digest}"

    def register_song_artwork(self, song_details: Optional[dict]) -> Optional[str]:
        if not song_details:
            return None

        art_path = song_details.get("artwork_path")
        if not art_path:
            return None

        art_path_obj = Path(art_path)
        if not art_path_obj.exists() or not art_path_obj.is_file():
            return None

        art_id = song_details.get("artwork_id") or self._build_art_id(song_details, art_path)
        song_details["artwork_id"] = art_id
        self.approved_art_paths[art_id] = art_path_obj
        return art_id

    def get_art_url(self, song_details: Optional[dict]) -> Optional[str]:
        art_id = self.register_song_artwork(song_details)
        if not art_id:
            return None
        return f"/art/{art_id}"

    def get_media_url(self, song_details: Optional[dict]) -> Optional[str]:
        media_id = self.register_song_media(song_details)
        if not media_id:
            return None
        return f"/media/{media_id}"

    def refresh_media_registry(self, queued_items=None):
        fresh: Dict[str, Path] = {}
        fresh_art: Dict[str, Path] = {}
        sources = []
        if queued_items:
            sources.extend(list(queued_items))
        if self.main_vlc_now_playing_details:
            sources.append(self.main_vlc_now_playing_details)

        for song in sources:
            file_path = song.get("filepath") if isinstance(song, dict) else None
            if not file_path:
                continue
            media_id = song.get("media_id") or self._build_media_id(song, file_path)
            song["media_id"] = media_id
            fresh[media_id] = Path(file_path)

            art_path = song.get("artwork_path")
            if art_path and Path(art_path).exists():
                art_id = song.get("artwork_id") or self._build_art_id(song, art_path)
                song["artwork_id"] = art_id
                fresh_art[art_id] = Path(art_path)

        self.approved_media_paths = fresh
        self.approved_art_paths = fresh_art

    async def websocket_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.websockets.add(ws)
        self._obs_page_ready.set()
        bot_logger.info("OBS Browser Source Connected!")

        # Send a stop immediately to clear any stale playback state in the browser
        await ws.send_json({"action": "stop"})

        # Force OBS to reload player.html once per startup so our latest JS is always running.
        # OBS keeps its page alive across WS reconnects, so we must explicitly trigger a reload.
        if not self._obs_reload_done:
            self._obs_reload_done = True
            bot_logger.info("Sending reload to OBS browser source to pick up latest player.html...")
            await ws.send_json({"action": "reload"})
            # OBS will reload and immediately reconnect — handle it in the next connection
            self.websockets.discard(ws)
            return ws

        # --- Fresh page connected (post-reload) ---
        # Sync current settings
        await ws.send_json({
            "action": "set_position",
            "position": self.settings.sr_window_position
        })
        await ws.send_json({
            "action": "set_bg_opacity",
            "opacity": self.settings.sr_bg_opacity
        })
        await ws.send_json({
            "action": "set_window_size",
            "width": self.settings.sr_window_width,
            "height": self.settings.sr_window_height
        })
        await ws.send_json({
            "action": "set_hud_font_sizes",
            "title_size": self.settings.sr_title_font_size,
            "time_size": self.settings.sr_time_font_size
        })
        
        # Unconditionally sync the fullscreen state on every new browser source connection
        await ws.send_json({
            "action": "set_fullscreen",
            "fullscreen": getattr(self, "is_fullscreen_playing", False)
        })

        await ws.send_json({
            "action": "set_progress_visible",
            "visible": getattr(self, "is_progress_visible", False)
        })
        await ws.send_json({
            "action": "set_title_visible",
            "visible": getattr(self, "is_title_visible", False)
        })
        await ws.send_json({
            "action": "set_time_visible",
            "visible": getattr(self, "is_time_visible", False)
        })

        # If a queue was restored on startup, kick it off now that we have a fresh JS environment
        sr_cog = self.get_cog("SRCog")
        if sr_cog and sr_cog.song_request_deque and self.main_vlc_now_playing_details is None:
            bot_logger.info("Fresh OBS page connected — auto-starting restored queue.")
            self.loop.create_task(self.transition_to_vlc_sr_mode())

        # If something is already playing when they connect, sync it up
        if self.main_vlc_now_playing_details and self.current_media_priority == self.VLC_SR_PRIORITY:
            media_url = self.get_media_url(self.main_vlc_now_playing_details)
            art_url = self.get_art_url(self.main_vlc_now_playing_details)
            await ws.send_json({
                "action": "play",
                "media_url": media_url,
                "art_url": art_url,
                "media_kind": self.main_vlc_now_playing_details.get("media_kind", "video"),
                "title": self.main_vlc_now_playing_details['title'],
                "requested_by": self.main_vlc_now_playing_details['requested_by'],
                "volume": self.sr_volume_level,
                "start_time": self.main_vlc_now_playing_details.get('_resume_time', 0) or 0
            })
            self.ws_play_sent.add(id(ws))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("event") == "timeupdate":
                        self.main_vlc_current_time = data.get("time", 0)
                        self.main_vlc_duration = data.get("duration", 0)
                    elif data.get("event") == "media_ended":
                        # Ignore stale ended events that arrive during startup grace period
                        elapsed = asyncio.get_event_loop().time() - self._startup_time
                        if elapsed < self._STARTUP_GRACE:
                            bot_logger.info(f"Ignoring stale media_ended ({elapsed:.1f}s after start, grace={self._STARTUP_GRACE}s).")
                            continue
                        if id(ws) not in self.ws_play_sent:
                            bot_logger.info("Ignoring media_ended from WS that never received a play command.")
                            continue
                        # Deduplicate: only one handler at a time across all connections
                        if self._processing_media_ended:
                            bot_logger.debug("Ignoring duplicate media_ended from secondary WS connection.")
                            continue
                        self._processing_media_ended = True
                        try:
                            # The video finished playing in OBS
                            bot_logger.info("OBS reported song finished.")
                            finished_details = self.main_vlc_now_playing_details
                            self.main_vlc_content_type = None
                            self.main_vlc_now_playing_details = None

                            sr_cog = self.get_cog("SRCog")
                            if sr_cog:
                                await sr_cog.cleanup_played_song(finished_details)
                                await sr_cog.play_next_in_queue()
                            else:
                                await self.transition_to_idle()
                        finally:
                            self._processing_media_ended = False
        finally:
            self.websockets.discard(ws)
            self.ws_play_sent.discard(id(ws))
            if not any(not sock.closed for sock in set(self.websockets)):
                self._obs_page_ready.clear()
            bot_logger.info("OBS Browser Source Disconnected.")
        return ws

    def save_bot_state(self):
        """Persist now-playing, playback position, and SR enabled state to disk for restart recovery."""
        np = self.main_vlc_now_playing_details
        if np is not None:
            # Embed current playback time so we can resume from here
            np = dict(np)  # shallow copy, don't mutate original
            np["_resume_time"] = self.main_vlc_current_time
        state = {
            "now_playing": np,
            "is_sr_enabled": self.is_sr_enabled,
            "is_fullscreen_playing": self.is_fullscreen_playing,
            "is_progress_visible": self.is_progress_visible,
            "is_title_visible": self.is_title_visible,
            "is_time_visible": self.is_time_visible,
        }
        try:
            state_path = self.settings.bot_state_path
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            bot_logger.info(f"Bot state saved. Resume position: {self.main_vlc_current_time:.1f}s")
        except Exception as e:
            bot_logger.warning(f"Could not save bot state: {e}")


    async def event_ready(self):
        bot_logger.info(f"--- Bot Ready: {self.nick} ---")
        try:
            self.load_module("cogs.sr_cog")
            bot_logger.info("Loaded SR Cog.")
        except Exception as e:
            bot_logger.error(f"Failed to load SR Cog: {e}")
            
        await self.start_web_server()
        self.loyalty.start()
        # Auto-start is now triggered from websocket_handler after OBS reloads

    async def event_message(self, message):
        if self._shutdown_started:
            return
        content = (getattr(message, "content", "") or "").strip()

        # If the bot is logged in as the same account that's sending chat messages,
        # Twitch marks those messages as echo=True. We still want to process real
        # commands like !sr from that account, while ignoring the bot's own normal replies.
        if message.echo and not content.startswith("!"):
            return

        if message.echo and content.startswith("!"):
            bot_logger.info(f"Processing echoed command from authenticated account: {content}")

        handled_by_loyalty = await self.loyalty.handle_message(message)
        if not handled_by_loyalty:
            await self.handle_commands(message)

    async def event_command_error(self, context, error):
        command_name = getattr(getattr(context, "command", None), "name", "<unknown>")
        message_content = getattr(getattr(context, "message", None), "content", "<unknown>")
        bot_logger.error(
            f"Command '{command_name}' failed for message '{message_content}': {error}",
            exc_info=True
        )

    # --- Video Player Control Methods ---
    async def vlc_play_local_file(self, file_path: str, content_type: str, song_details: dict, start_offset: Optional[int] = 0) -> bool:
        self.main_vlc_content_type = content_type
        self.main_vlc_now_playing_details = song_details
        self.main_vlc_is_paused = False
        self.main_vlc_current_time = 0
        self.main_vlc_duration = 0
        sr_cog = self.get_cog("SRCog")
        self.refresh_media_registry(sr_cog.song_request_deque if sr_cog else None)

        media_url = self.get_media_url(song_details)
        if not media_url:
            bot_logger.error(f"Could not register media path for playback: {file_path}")
            return False
        art_url = self.get_art_url(song_details)

        if getattr(self.settings, "obs_auto_refresh", True):
            self._obs_page_ready.clear()

        obs_prep = await self.obs_controller.prepare_for_playback(self.player_url)
        if obs_prep.attempted and (obs_prep.shown or obs_prep.refreshed):
            try:
                await asyncio.wait_for(self._obs_page_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                bot_logger.warning(
                    "OBS automation tried to prepare the browser source, but the page did not reconnect in time."
                )

        active_websockets = [ws for ws in set(self.websockets) if not ws.closed]
        if not active_websockets:
            bot_logger.info(
                "No OBS Browser Source is connected yet. Waiting up to 15 seconds before sending playback."
            )
            try:
                await asyncio.wait_for(self._obs_page_ready.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass
            active_websockets = [ws for ws in set(self.websockets) if not ws.closed]

        if not active_websockets:
            if obs_prep.attempted:
                bot_logger.warning(
                    "SR playback is ready, but the OBS Browser Source still is not connected. "
                    "Keep the OBS Browser Source visible, turn off 'Shutdown source when not visible', "
                    "and refresh the source once after changing the player URL or port."
                )
            else:
                bot_logger.warning(
                    "Tried to start SR playback, but no active OBS Browser Source is connected. "
                    "Keep the OBS Browser Source visible, turn off 'Shutdown source when not visible', "
                    "and refresh the source once after changing the player URL or port."
                )
            self.main_vlc_content_type = None
            self.main_vlc_now_playing_details = None
            self.main_vlc_is_paused = False
            self.main_vlc_current_time = 0
            self.main_vlc_duration = 0
            self.refresh_media_registry(sr_cog.song_request_deque if sr_cog else None)
            return False
        else:
            bot_logger.info(f"Sending SR playback to {len(active_websockets)} OBS Browser Source connection(s).")

        # Broadcast to OBS via Websocket
        resume_time = song_details.get("_resume_time", 0) or 0
        for ws in active_websockets:
            await ws.send_json({
                "action": "play",
                "media_url": media_url,
                "art_url": art_url,
                "media_kind": song_details.get("media_kind", "video"),
                "title": song_details.get("title", 'Unknown'),
                "requested_by": song_details.get("requested_by", 'Unknown'),
                "volume": self.sr_volume_level,
                "start_time": resume_time
            })
            self.ws_play_sent.add(id(ws))
        return True

    async def vlc_set_volume(self, volume_percent: int) -> bool:
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({
                    "action": "set_volume",
                    "volume": volume_percent
                })
        return True

    async def vlc_stop_all(self, clear_state: bool = True):
        if clear_state:
            self.main_vlc_content_type = None
            self.main_vlc_now_playing_details = None
            self.main_vlc_is_paused = False
            self.main_vlc_current_time = 0
            self.main_vlc_duration = 0
            self.refresh_media_registry()
        
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": "stop"})

    async def vlc_toggle_pause(self) -> bool:
        self.main_vlc_is_paused = not getattr(self, "main_vlc_is_paused", False)
        action = "pause" if self.main_vlc_is_paused else "resume"
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": action})
        return True

    async def vlc_seek(self, time: float) -> bool:
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": "seek", "time": time})
        return True

    async def vlc_toggle_title(self) -> bool:
        return await self.vlc_set_title_visible(not self.is_title_visible)

    async def vlc_set_title_visible(self, visible: bool) -> bool:
        self.is_title_visible = bool(visible)
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({
                    "action": "set_title_visible",
                    "visible": self.is_title_visible
                })
        return True

    async def vlc_show_overlay(self) -> bool:
        return await self.vlc_toggle_title()

    async def vlc_toggle_time(self) -> bool:
        return await self.vlc_set_time_visible(not self.is_time_visible)

    async def vlc_set_time_visible(self, visible: bool) -> bool:
        self.is_time_visible = bool(visible)
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({
                    "action": "set_time_visible",
                    "visible": self.is_time_visible
                })
        return True

    async def vlc_toggle_window_hidden(self) -> bool:
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": "toggle_window_hidden"})
        return True

    async def vlc_toggle_progress_bar(self) -> bool:
        return await self.vlc_set_progress_visible(not self.is_progress_visible)

    async def vlc_set_progress_visible(self, visible: bool) -> bool:
        self.is_progress_visible = bool(visible)
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({
                    "action": "set_progress_visible",
                    "visible": self.is_progress_visible
                })
        return True

    async def vlc_show_window(self) -> bool:
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": "show_window"})
        return True

    async def vlc_toggle_fullscreen(self, is_fullscreen: bool):
        self.is_fullscreen_playing = is_fullscreen
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": "set_fullscreen", "fullscreen": is_fullscreen})
        return True

    async def vlc_set_position(self, position: str) -> bool:
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": "set_position", "position": position})
        return True

    async def vlc_set_bg_opacity(self, opacity: int) -> bool:
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": "set_bg_opacity", "opacity": opacity})
        return True

    async def vlc_set_window_size(self, width: int, height: int) -> bool:
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({"action": "set_window_size", "width": width, "height": height})
        return True

    async def vlc_set_hud_font_sizes(self, title_size: int, time_size: int) -> bool:
        for ws in set(self.websockets):
            if not ws.closed:
                await ws.send_json({
                    "action": "set_hud_font_sizes",
                    "title_size": title_size,
                    "time_size": time_size
                })
        return True

    # --- Mode Transitions ---
    async def transition_to_idle(self):
        """Returns to idle/stopped state. Stops OBS video."""
        if self.current_media_priority == self.IDLE_PRIORITY:
            return  # Already idle, don't send stop again
        await self.vlc_stop_all(clear_state=True)
        self.refresh_media_registry()
        await self.obs_controller.handle_idle(self.player_url)
        self.current_media_priority = self.IDLE_PRIORITY
        bot_logger.info("Bot transitioned to Idle mode.")

    async def transition_to_vlc_sr_mode(self):
        self.current_media_priority = self.VLC_SR_PRIORITY
        bot_logger.info("Bot transitioned to SR mode.")
        sr_cog = self.get_cog("SRCog")
        if sr_cog: await sr_cog.play_next_in_queue()

    async def close(self):
        if self._close_started:
            return
        self._close_started = True
        await self.loyalty.close()
        await self.obs_controller.close()
        # TwitchIO internals are not fully initialized until the client has
        # actually started. During failed or partial startups, calling close()
        # can otherwise raise inside TwitchIO and leave the GUI in a bad state.
        if getattr(self, "_closing", None) is None:
            return
        try:
            await asyncio.wait_for(super().close(), timeout=3)
        except asyncio.TimeoutError:
            bot_logger.warning("Timed out while closing Twitch connection.")
        except AttributeError as exc:
            if "_closing" in str(exc):
                bot_logger.debug("Twitch client was not fully initialized during shutdown.")
            else:
                raise

    async def shutdown(self):
        """Shut down the bot and embedded web services without hanging the GUI."""
        if self._shutdown_started:
            return
        self._shutdown_started = True

        # Stop this app's timers and Streamer.bot dispatches first. This does not
        # stop or modify Streamer.bot itself or any independent Streamer.bot actions.
        await self.loyalty.close()

        active_websockets = [ws for ws in set(self.websockets) if not ws.closed]

        for ws in active_websockets:
            try:
                await asyncio.wait_for(
                    ws.close(code=WSCloseCode.GOING_AWAY, message=b"bot shutdown"),
                    timeout=1,
                )
            except Exception:
                pass

        self.websockets.clear()
        self.ws_play_sent.clear()
        self._obs_page_ready.clear()

        if self.site:
            try:
                await asyncio.wait_for(self.site.stop(), timeout=2)
            except Exception:
                pass
            self.site = None

        if self.runner:
            try:
                await asyncio.wait_for(self.runner.cleanup(), timeout=2)
            except Exception:
                pass
            self.runner = None

        await asyncio.wait_for(self.close(), timeout=4)

if __name__ == "__main__":
    bot = Bot()
    bot.run()
