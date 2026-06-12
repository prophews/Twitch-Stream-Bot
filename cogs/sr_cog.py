import json
import logging
import asyncio
import time
import hashlib
from collections import deque
import functools
from typing import Optional, Dict, Any, Tuple
import os
import sys
import base64
from pathlib import Path

from twitchio.ext import commands
from settings import get_runtime_data_dir

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

try:
    import yt_dlp
    from yt_dlp.utils import DownloadError
    
    YDL_OPTS_DOWNLOAD = {
        'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        'outtmpl': '%(id)s.%(ext)s',
        'merge_output_format': 'mp4',
        'default_search': 'ytsearch1',
        'quiet': True,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
        'logger': logging.getLogger("TwitchBot.SRCog.yt_dlp"),
        'javascript_run_runtimes': ['node'],
        'extractor_args': {'youtube': {'js_runtimes': ['node'], 'remote_components': ['ejs:github']}},
    }
    
    # Handle PyInstaller Bundle Path for FFmpeg
    if hasattr(sys, '_MEIPASS'):
        ffmpeg_path = os.path.join(sys._MEIPASS, 'ffmpeg.exe')
        if os.path.exists(ffmpeg_path):
            YDL_OPTS_DOWNLOAD['ffmpeg_location'] = ffmpeg_path

except ImportError:
    yt_dlp = None; YDL_OPTS_DOWNLOAD = {}; DownloadError = None
    logging.getLogger("TwitchBot.SRCog").critical("yt_dlp NOT FOUND. SR WILL FAIL.")

cog_logger = logging.getLogger("TwitchBot.SRCog")
LOCAL_LIBRARY_EXTENSIONS = {
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac",
    ".mp4", ".webm", ".mov", ".mkv"
}
LOCAL_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac"}
LOCAL_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv"}

class SRCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.song_request_deque = deque()
        self.song_history_deque = deque(maxlen=50)
        self.pending_downloads = deque()
        self.active_download = None
        self.cancelled_downloads = set()
        self.is_downloading = False
        self._deferred_cleanup_tasks = set()
        self.user_rotation = [] 

        if hasattr(self.bot, 'base_path'):
            self.temp_dir = self.bot.base_path / 'temp_sr'
        else:
            self.temp_dir = Path(os.path.dirname(__file__)).parent / 'temp_sr'

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.art_cache_dir = get_runtime_data_dir() / "art_cache"
        self.art_cache_dir.mkdir(parents=True, exist_ok=True)

        self._queue_snapshot_path = getattr(
            getattr(bot, "settings", None),
            "sr_queue_snapshot_path",
            Path("data/sr_queue.json")
        )
        self._queue_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_queue_snapshot()
        self._restore_now_playing()
        self._cleanup_temp_dir()

    def _is_local_library_song(self, song: Optional[Dict[str, Any]]) -> bool:
        return bool(song and song.get("source_type") == "local_library")

    def _normalize_song_entry(self, song: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(song)
        normalized.setdefault("source_type", "downloaded")
        normalized.setdefault("timestamp", time.time())

        filepath = normalized.get("filepath")
        if filepath:
            normalized["filepath"] = str(Path(filepath))

        artwork_path = normalized.get("artwork_path")
        if artwork_path:
            normalized["artwork_path"] = str(Path(artwork_path))

        if normalized["source_type"] == "local_library":
            library_path = normalized.get("library_path") or normalized.get("filepath")
            if library_path:
                normalized["library_path"] = str(Path(library_path))
            normalized.setdefault(
                "id",
                f"local:{hashlib.sha1(str(normalized.get('library_path', normalized.get('filepath', ''))).encode('utf-8')).hexdigest()[:16]}"
            )

        return normalized

    def _sync_media_registry(self) -> None:
        if hasattr(self.bot, "refresh_media_registry"):
            self.bot.refresh_media_registry(self.song_request_deque)

    def _streamer_request_name(self) -> str:
        channel = getattr(getattr(self.bot, "settings", None), "channel", "")
        return channel or "streamer"

    def _is_supported_local_media(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in LOCAL_LIBRARY_EXTENSIONS

    def _local_media_kind(self, path: Path) -> str:
        return "audio" if path.suffix.lower() in LOCAL_AUDIO_EXTENSIONS else "video"

    def _get_local_media_title(self, path: Path) -> str:
        fallback_title = path.stem
        if MutagenFile is None:
            return fallback_title

        try:
            metadata = MutagenFile(path, easy=True)
            if not metadata:
                return fallback_title

            tags = getattr(metadata, "tags", None) or {}
            title = tags.get("title", [None])[0] if isinstance(tags, dict) else None
            artist = tags.get("artist", [None])[0] if isinstance(tags, dict) else None

            if title and artist:
                return f"{artist} - {title}"
            if title:
                return title
        except Exception:
            pass

        return fallback_title

    def _cache_artwork_bytes(self, source_path: Path, data: bytes, mime_type: str) -> Optional[Path]:
        if not data:
            return None

        extension = ".png" if "png" in mime_type.lower() else ".jpg"
        stat = source_path.stat()
        digest = hashlib.sha1(
            f"{source_path}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
        ).hexdigest()[:20]
        cache_path = self.art_cache_dir / f"{digest}{extension}"
        if not cache_path.exists() or cache_path.stat().st_size != len(data):
            cache_path.write_bytes(data)
        return cache_path

    def _extract_local_artwork(self, path: Path) -> Tuple[Optional[Path], Optional[str]]:
        if MutagenFile is None or self._local_media_kind(path) != "audio":
            return None, None

        try:
            metadata = MutagenFile(path)
            if not metadata:
                return None, None

            pictures = getattr(metadata, "pictures", None)
            if pictures:
                picture = pictures[0]
                mime_type = getattr(picture, "mime", None) or "image/jpeg"
                return self._cache_artwork_bytes(path, picture.data, mime_type), mime_type

            tags = getattr(metadata, "tags", None)
            if tags:
                # MP3/ID3 album art is usually stored in APIC frames.
                for value in tags.values():
                    if hasattr(value, "data") and value.__class__.__name__.upper().startswith("APIC"):
                        mime_type = getattr(value, "mime", None) or "image/jpeg"
                        return self._cache_artwork_bytes(path, value.data, mime_type), mime_type

                # MP4/M4A album art is usually stored in covr atoms.
                cover_values = tags.get("covr") if hasattr(tags, "get") else None
                if cover_values:
                    cover = cover_values[0]
                    image_format = getattr(cover, "imageformat", None)
                    mime_type = "image/png" if image_format == 14 else "image/jpeg"
                    return self._cache_artwork_bytes(path, bytes(cover), mime_type), mime_type

                # Ogg/Opus/Vorbis can store FLAC picture blocks as base64 text.
                block_values = tags.get("metadata_block_picture") if hasattr(tags, "get") else None
                if block_values:
                    raw_block = base64.b64decode(block_values[0])
                    try:
                        from mutagen.flac import Picture
                        picture = Picture(raw_block)
                        mime_type = picture.mime or "image/jpeg"
                        return self._cache_artwork_bytes(path, picture.data, mime_type), mime_type
                    except Exception:
                        return None, None
        except Exception as exc:
            cog_logger.debug(f"Could not extract local artwork from {path.name}: {exc}")

        return None, None

    def _build_local_song_details(self, file_path: str, requested_by: str) -> Dict[str, Any]:
        resolved = Path(file_path).expanduser().resolve()
        title = self._get_local_media_title(resolved)
        media_kind = self._local_media_kind(resolved)
        artwork_path, artwork_mime = self._extract_local_artwork(resolved)
        library_hash = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:16]
        return self._normalize_song_entry({
            "id": f"local:{library_hash}",
            "title": title,
            "thumbnail": None,
            "artwork_path": str(artwork_path) if artwork_path else None,
            "artwork_mime": artwork_mime,
            "media_kind": media_kind,
            "original_url": None,
            "duration": None,
            "filepath": str(resolved),
            "library_path": str(resolved),
            "requested_by": requested_by,
            "timestamp": time.time(),
            "_original_query": title,
            "source_type": "local_library",
        })

    def _load_queue_snapshot(self) -> None:
        if not self._queue_snapshot_path.exists():
            return

        try:
            payload = json.loads(self._queue_snapshot_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            cog_logger.error(f"Failed to decode SR queue snapshot: {exc}")
            return
        except OSError as exc:
            cog_logger.error(f"Failed to read SR queue snapshot: {exc}")
            return

        if isinstance(payload, dict):
            payload = [payload]

        if isinstance(payload, list):
            normalized_payload = []
            dropped_missing = 0
            for item in payload:
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_song_entry(item)
                filepath = normalized.get("filepath")
                if not filepath or not Path(filepath).exists():
                    dropped_missing += 1
                    cog_logger.warning(
                        f"Dropping missing restored media from queue snapshot: {normalized.get('title', 'Unknown Title')}"
                    )
                    continue
                normalized_payload.append(normalized)
            self.song_request_deque = deque(normalized_payload)
            self._sync_media_registry()
            if dropped_missing:
                self._persist_queue()
            if normalized_payload:
                cog_logger.info(f"Restored {len(normalized_payload)} song request(s) from snapshot.")

    def _restore_now_playing(self) -> None:
        """If a song was playing when the bot last shut down, re-queue it at front."""
        bot_state_path = getattr(getattr(self.bot, "settings", None), "bot_state_path", Path("data/bot_state.json"))
        if not bot_state_path.exists():
            return
        try:
            state = json.loads(bot_state_path.read_text(encoding="utf-8"))
            np = state.get("now_playing")
            if isinstance(np, dict):
                np = self._normalize_song_entry(np)
            restored_now_playing = False
            if np and np.get("filepath") and Path(np["filepath"]).exists():
                # Only prepend if not already in the queue (guard against double-restore)
                already_queued = any(
                    s.get("filepath") == np.get("filepath")
                    for s in self.song_request_deque
                )
                if not already_queued:
                    self.song_request_deque.appendleft(np)
                    self._persist_queue()
                    restored_now_playing = True
                    cog_logger.info(f"Restored previously-playing song to front of queue: {np.get('title')}")
            # Restore SR enabled state and Fullscreen state
            is_sr_enabled = state.get("is_sr_enabled", True)
            self.bot.is_sr_enabled = is_sr_enabled
            self.bot.is_fullscreen_playing = state.get("is_fullscreen_playing", False)
            self.bot.is_progress_visible = state.get("is_progress_visible", False)
            self.bot.is_title_visible = state.get("is_title_visible", False)
            self.bot.is_time_visible = state.get("is_time_visible", False)
            cog_logger.info(f"Restored SR enabled state: {is_sr_enabled}")
            if restored_now_playing:
                state["now_playing"] = None
                bot_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            cog_logger.warning(f"Could not restore bot state: {e}")

    def _persist_queue(self) -> None:
        try:
            normalized_queue = [self._normalize_song_entry(song) for song in self.song_request_deque]
            self.song_request_deque = deque(normalized_queue)
            data = json.dumps(normalized_queue, ensure_ascii=False, indent=2)
            # Write atomically via temp file to avoid corruption on crash
            tmp = self._queue_snapshot_path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(self._queue_snapshot_path)
            self._sync_media_registry()
        except OSError as exc:
            cog_logger.error(f"Failed to persist SR queue snapshot: {exc}")

    def replace_queue(self, items: list[Dict[str, Any]]) -> None:
        new_rotation = []
        for item in items:
            u = item.get("requested_by", "Unknown")
            if u not in new_rotation:
                new_rotation.append(u)
        
        self.user_rotation = new_rotation
        self.song_request_deque = deque(self._normalize_song_entry(item) for item in items)
        self._persist_queue()

    def clear_queue(self) -> int:
        removed = len(self.song_request_deque)
        self.song_request_deque.clear()
        self._persist_queue()
        return removed

    def pop_next_song(self) -> Optional[Dict[str, Any]]:
        if not self.song_request_deque:
            return None
        song = self.song_request_deque.popleft()
        self._persist_queue()
        return self._normalize_song_entry(song)

    def move_song(self, index: int, direction: int) -> bool:
        """Move song at index up (-1) or down (+1). Returns True if moved."""
        q = list(self.song_request_deque)
        target = index + direction
        if target < 0 or target >= len(q):
            return False
        q[index], q[target] = q[target], q[index]
        self.song_request_deque = deque(q)
        self._persist_queue()
        return True

    def reorder_queue(self):
        """Fair Play v2: Interleaves the queue by Turn Round and Rotation Index."""
        try:
            if not self.song_request_deque:
                return
            
            # Use raw First-Come, First-Serve if Fair Play is disabled
            if not getattr(getattr(self.bot, 'settings', None), 'use_fair_queue', True):
                return

            cog_logger.info("Fair Play v2: Deterministic rebalancing...")
            
            # 1. Map turn counts (Who is playing right now?)
            now_playing = getattr(self.bot, 'main_vlc_now_playing_details', None)
            active_turns = {}
            if now_playing and self.bot.main_vlc_content_type == getattr(self.bot, 'VLC_SR_PRIORITY', 'VLC_SR'):
                u = now_playing.get('requested_by')
                if u:
                    active_turns[u] = 1
                    if u not in self.user_rotation:
                        self.user_rotation.append(u)

            # 2. Assign rounds/turns to all songs in the queue
            original_list = list(self.song_request_deque)
            for song in original_list:
                u = song.get("requested_by", "Unknown")
                if u not in self.user_rotation:
                    self.user_rotation.append(u)
                
                turn = active_turns.get(u, 0) + 1
                song["_fair_turn"] = turn
                active_turns[u] = turn

            # 3. Sort by (Turn Round, UserRotationIndex)
            def fair_sort_key(song):
                u = song.get("requested_by", "Unknown")
                try:
                    r_idx = self.user_rotation.index(u)
                except ValueError:
                    r_idx = 999
                return (song.get("_fair_turn", 1), r_idx)

            original_list.sort(key=fair_sort_key)
            
            # 4. Finalize
            self.song_request_deque = deque(original_list)
            self._persist_queue()
            
        except Exception as e:
            cog_logger.error(f"Fair Play v2 Reorder Error: {e}", exc_info=True)


    def _cleanup_temp_dir(self):
        cog_logger.info("Cleaning up unused SR files...")
        
        active_files = set()
        for song in self.song_request_deque:
            if self._is_local_library_song(song):
                continue
            fp = song.get('filepath')
            if fp:
                active_files.add(Path(fp).name)
        
        current_song = getattr(self.bot, 'main_vlc_now_playing_details', None)
        if current_song and not self._is_local_library_song(current_song) and current_song.get('filepath'):
            active_files.add(Path(current_song.get('filepath')).name)

        # Also protect any file referenced in bot_state.json (resume target)
        bot_state_path = getattr(getattr(self.bot, "settings", None), "bot_state_path", Path("data/bot_state.json"))
        try:
            if bot_state_path.exists():
                state = json.loads(bot_state_path.read_text(encoding="utf-8"))
                np = state.get("now_playing")
                if np and np.get("filepath") and not self._is_local_library_song(np):
                    active_files.add(Path(np["filepath"]).name)
        except Exception:
            pass

        count = 0
        if self.temp_dir.exists():
            for item in self.temp_dir.iterdir():
                if item.is_file() and item.name not in active_files:
                    try: 
                        item.unlink()
                        count += 1
                    except Exception as e:
                        cog_logger.warning(f"Could not delete old temp file {item.name}: {e}")
        
        if count > 0:
            cog_logger.info(f"Cleanup complete. Deleted {count} unused files.")

    async def cleanup_played_song(self, song_details: Optional[Dict]):
        if not song_details: return
        
        # Add to history if not already the newest item
        if not self.song_history_deque or self.song_history_deque[-1].get('filepath') != song_details.get('filepath'):
            self.song_history_deque.append(song_details)
            
        filepath_str = song_details.get('filepath')
        current_song = getattr(self.bot, "main_vlc_now_playing_details", None)
        if current_song and current_song.get("filepath") == filepath_str:
            self.bot.main_vlc_now_playing_details = None
            self.bot.main_vlc_content_type = None

        # Clear bot_state.json now that this song has genuinely finished
        bot_state_path = getattr(getattr(self.bot, "settings", None), "bot_state_path", Path("data/bot_state.json"))
        try:
            if bot_state_path.exists():
                state = json.loads(bot_state_path.read_text(encoding="utf-8"))
                saved_np = state.get("now_playing")
                if saved_np and saved_np.get("filepath") == filepath_str:
                    bot_state_path.write_text("{}", encoding="utf-8")
        except Exception:
            pass

        if self._is_local_library_song(song_details):
            self._sync_media_registry()
            return

        if not filepath_str: return
        
        filepath = Path(filepath_str)
        if filepath.exists() and filepath.is_file():
            # OBS/Chromium can hold the media request briefly after skip/stop.
            for attempt in range(2):
                try:
                    cog_logger.info(f"Cleaning up played SR file (attempt {attempt+1}): {filepath.name}")
                    filepath.unlink()
                    self._sync_media_registry()
                    return
                except Exception as e:
                    if attempt == 0:
                        cog_logger.warning(f"Cleanup deferred: {filepath.name} is locked. Retrying shortly...")
                        await asyncio.sleep(0.75)
                    else:
                        cog_logger.warning(f"Cleanup deferred for {filepath.name}; will retry in the background: {e}")
                        self._schedule_deferred_cleanup(filepath)
        self._sync_media_registry()

    def _schedule_deferred_cleanup(self, filepath: Path):
        task = self.bot.loop.create_task(self._deferred_delete_file(filepath))
        self._deferred_cleanup_tasks.add(task)
        task.add_done_callback(self._deferred_cleanup_tasks.discard)

    async def _deferred_delete_file(self, filepath: Path):
        for attempt in range(1, 11):
            await asyncio.sleep(2)
            try:
                if not filepath.exists():
                    return
                filepath.unlink()
                cog_logger.info(f"Deferred cleanup deleted SR file: {filepath.name}")
                return
            except Exception as e:
                if attempt == 10:
                    cog_logger.error(f"Deferred cleanup failed for {filepath.name}: {e}")

    async def fetch_metadata(self, query: str) -> Tuple[Optional[dict], Optional[str]]:
        if not yt_dlp: return None, "SR system is not properly installed."
        
        ydl_opts = YDL_OPTS_DOWNLOAD.copy()
        ydl_opts['quiet'] = True
        
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(
                None, 
                functools.partial(yt_dlp.YoutubeDL(ydl_opts).extract_info, query, download=False)
            )

            if not info:
                return None, "I couldn't find a video for that search."

            entries = info.get('entries')
            if entries is not None:
                if len(entries) == 0:
                    return None, "I couldn't find a video for that search."
                entry = entries[0]
            else:
                entry = info
            
            if 'id' not in entry or 'title' not in entry:
                return None, "The link or search result was not a valid video."
                
            duration = entry.get('duration')
            if duration and duration > 7200:
                hours = int(duration // 3600)
                mins = int((duration % 3600) // 60)
                length_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
                return None, f"That video is too long ({length_str}). The maximum allowed is 2 hours."
                
            return {
                "id": entry['id'],
                "title": entry.get('title', "Unknown Title"),
                "thumbnail": entry.get('thumbnail'),
                "original_url": entry.get('webpage_url') or entry.get('url'),
                "duration": duration
            }, None
            
        except DownloadError as e:
            cog_logger.error(f"yt-dlp metadata error: {e}")
            return None, "Sorry, I couldn't download that song. It might be private or unavailable."
        except Exception as e:
            cog_logger.error(f"yt-dlp generic metadata error: {e}", exc_info=True)
            return None, "An unexpected error occurred while finding that song."

    async def download_video(self, video_id: str, original_url: str) -> Tuple[Optional[str], Optional[str]]:
        if not yt_dlp: return None, "SR system offline."
        
        ydl_opts = YDL_OPTS_DOWNLOAD.copy()
        ydl_opts['paths'] = {'home': str(self.temp_dir)}
        download_stem = f"{video_id}-{int(time.time() * 1000)}"
        ydl_opts['outtmpl'] = f"{download_stem}.%(ext)s"
        
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, 
                functools.partial(yt_dlp.YoutubeDL(ydl_opts).download, [original_url])
            )
            
            filepath = self.temp_dir / f"{download_stem}.mp4"
            if not filepath.exists():
                matches = sorted(self.temp_dir.glob(f"{download_stem}.*"))
                filepath = matches[0] if matches else filepath

            if filepath.exists() and filepath.is_file():
                return str(filepath), None
            else:
                return None, "The download completed but the file is missing."
                
        except DownloadError as e:
            cog_logger.error(f"yt-dlp download error: {e}")
            return None, "Download failed."
        except Exception as e:
            cog_logger.error(f"yt-dlp generic download error: {e}")
            return None, "An unexpected error occurred during download."

    async def enqueue_local_file(self, file_path: str, requested_by: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        resolved = Path(file_path).expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            return None, "The selected local file no longer exists."
        if not self._is_supported_local_media(resolved):
            return None, "That file type is not supported for the local library."

        requested_by = requested_by or self._streamer_request_name()
        loop = asyncio.get_event_loop()
        song_details = await loop.run_in_executor(
            None,
            functools.partial(self._build_local_song_details, str(resolved), requested_by)
        )

        self.song_request_deque.append(song_details)
        self.reorder_queue()
        self._sync_media_registry()

        is_player_occupied = self.bot.main_vlc_now_playing_details is not None
        if self.bot.current_media_priority == getattr(self.bot, 'IDLE_PRIORITY', 'IDLE'):
            await self.bot.transition_to_vlc_sr_mode()
        elif not is_player_occupied and self.bot.current_media_priority == getattr(self.bot, 'VLC_SR_PRIORITY', 'VLC_SR'):
            cog_logger.info("Local library enqueue: Player is idle, kickstarting queue.")
            await self.play_next_in_queue()

        return song_details, None

    async def song_request_command(self, ctx: commands.Context, *, query: str):
        user = ctx.author.name
        cog_logger.info(f"!sr by {user} for '{query[:50]}...'.")

        if not getattr(self.bot, 'is_sr_enabled', True):
            await ctx.send(f"@{user}, song requests are currently disabled.")
            return

        if len(self.pending_downloads) > 15:
            await ctx.send(f"@{user}, the processing queue is full! Please try again in a few minutes.")
            return

        metadata, error_message = await self.fetch_metadata(query)
        if not metadata:
            await ctx.send(f"@{user}, {error_message or 'sorry, I could not get that song.'}")
            return
            
        real_title = metadata['title']

        estimated_pos = 0
        if getattr(getattr(self.bot, 'settings', None), 'use_fair_queue', True):
            simulated_queue = []
            for song in self.song_request_deque:
                simulated_queue.append(song.copy())
            
            for _, p_user, _ in self.pending_downloads:
                simulated_queue.append({"requested_by": p_user})
                
            simulated_queue.append({"requested_by": user, "_is_target": True})
            
            sim_active_turns = {}
            sim_user_rotation = list(self.user_rotation)

            now_playing = getattr(self.bot, 'main_vlc_now_playing_details', None)
            if now_playing and self.bot.main_vlc_content_type == getattr(self.bot, 'VLC_SR_PRIORITY', 'VLC_SR'):
                np_user = now_playing.get('requested_by')
                if np_user:
                    sim_active_turns[np_user] = 1
                    if np_user not in sim_user_rotation:
                        sim_user_rotation.append(np_user)

            for song in simulated_queue:
                u = song.get("requested_by", "Unknown")
                if u not in sim_user_rotation:
                    sim_user_rotation.append(u)
                
                turn = sim_active_turns.get(u, 0) + 1
                song["_fair_turn"] = turn
                sim_active_turns[u] = turn

            def sim_fair_sort_key(song):
                u = song.get("requested_by", "Unknown")
                try:
                    r_idx = sim_user_rotation.index(u)
                except ValueError:
                    r_idx = 999
                return (song.get("_fair_turn", 1), r_idx)

            simulated_queue.sort(key=sim_fair_sort_key)

            for idx, song in enumerate(simulated_queue):
                if song.get("_is_target") == True:
                    estimated_pos = idx + 1
                    break
        else:
            # If fair queue is off, position is simply current queue length + pending downloads + 1
            estimated_pos = len(self.song_request_deque) + len(self.pending_downloads) + 1

        self.pending_downloads.append((ctx, user, metadata))
        
        await ctx.send(f"@{user}, '{real_title[:60]}' added to queue (pos {estimated_pos}).")
        
        if not self.is_downloading:
            self.bot.loop.create_task(self.process_download_queue())

    async def process_download_queue(self):
        self.is_downloading = True
        try:
            while self.pending_downloads:
                self.active_download = self.pending_downloads.popleft()
                ctx, user, metadata = self.active_download
                real_title = metadata['title']
                video_url = metadata['original_url']
                video_id = metadata['id']
                
                try:
                    filepath, error_message = await self.download_video(video_id, video_url)
                    
                    if real_title in self.cancelled_downloads:
                        cog_logger.info(f"Discarding cancelled download for: {real_title}")
                        self.cancelled_downloads.discard(real_title)
                        if filepath:
                            Path(filepath).unlink(missing_ok=True)
                        continue
                        
                    if not filepath:
                        await ctx.send(f"@{user}, {error_message or 'sorry, I could not finish downloading that song.'}")
                        continue
                        
                    song_details = self._normalize_song_entry({
                        **metadata,
                        "filepath": filepath,
                        "requested_by": user,
                        "timestamp": time.time(),
                        "_original_query": real_title,
                        "source_type": "downloaded",
                    })
                    self.song_request_deque.append(song_details)
                    self.reorder_queue()
                    self._sync_media_registry()
                        
                    is_player_occupied = self.bot.main_vlc_now_playing_details is not None
                    
                    if self.bot.current_media_priority == getattr(self.bot, 'IDLE_PRIORITY', 'IDLE'):
                        await self.bot.transition_to_vlc_sr_mode()
                    elif not is_player_occupied and self.bot.current_media_priority == getattr(self.bot, 'VLC_SR_PRIORITY', 'VLC_SR'):
                        cog_logger.info("SR command: Player is truly idle, kickstarting queue.")
                        await self.play_next_in_queue()
                        
                except Exception as e:
                    cog_logger.error(f"Error processing queued download: {e}", exc_info=True)
                finally:
                    self.active_download = None
                    
        finally:
            self.is_downloading = False

    async def play_next_in_queue(self):
        while True:
            if not self.song_request_deque:
                if getattr(self, 'is_downloading', False) or getattr(self, 'pending_downloads', None):
                    cog_logger.info("SR queue empty but downloads are pending. Waiting instead of falling back to Idle mode.")
                    return
                cog_logger.info("SR queue is empty. Transitioning to Idle mode.")
                await self.bot.transition_to_idle()
                return

            next_song = self.pop_next_song()
            if not next_song:
                await self.bot.transition_to_idle()
                return

            filepath = next_song.get('filepath')
            if not filepath or not Path(filepath).exists():
                cog_logger.warning(
                    f"Skipping missing {next_song.get('source_type', 'downloaded')} media: {next_song.get('title', 'Unknown Title')}"
                )
                continue

            cog_logger.info(f"Playing next SR from local file: {filepath}")

            success = await self.bot.vlc_play_local_file(
                file_path=filepath,
                content_type=self.bot.VLC_SR_PRIORITY,
                song_details=next_song
            )

            if not success:
                cog_logger.error(f"Failed to play '{next_song['title']}', transitioning to Idle mode.")
                self.song_request_deque.appendleft(next_song)
                self._persist_queue()
                await self.bot.transition_to_idle()
            return

    async def skip_command(self, ctx: commands.Context):
        user = ctx.author.name
        cog_logger.info(f"!skip from {user}")
        
        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv: return


        if self.bot.main_vlc_content_type == self.bot.VLC_SR_PRIORITY:
            skipped_details = self.bot.main_vlc_now_playing_details
            skipped_title = skipped_details.get('title', 'the current song')
            await ctx.send(f"@{user}, skipping '{skipped_title}'.")
            
            # Manually clean up and increment sequence since skip suppresses `media_ended`
            await self.bot.vlc_stop_all(clear_state=False)
            await self.cleanup_played_song(skipped_details)
            await self.play_next_in_queue()
            
        elif self.song_request_deque:
            skipped_song = self.pop_next_song()
            
            # Add manually skipped unplayed song to history too
            if not self.song_history_deque or self.song_history_deque[-1].get('filepath') != skipped_song.get('filepath'):
                self.song_history_deque.append(skipped_song)
                
            await ctx.send(f"@{user}, removed '{skipped_song.get('title', 'the next song')}' from the queue.")
        else:
            await ctx.send(f"@{user}, there is nothing to skip.")

    async def stop_command(self, ctx: commands.Context):
        user = ctx.author.name
        cog_logger.info(f"!stop from {user}")

        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv:
            return

        if self.bot.main_vlc_content_type == self.bot.VLC_SR_PRIORITY and self.bot.main_vlc_now_playing_details:
            stopped_details = self.bot.main_vlc_now_playing_details
            stopped_title = stopped_details.get('title', 'the current song')
            await ctx.send(f"@{user}, stopped '{stopped_title}'. The queue was not cleared.")

            await self.bot.vlc_stop_all(clear_state=False)
            await self.cleanup_played_song(stopped_details)
            await self.bot.transition_to_idle()
        else:
            await self.bot.transition_to_idle()
            await ctx.send(f"@{user}, the media player is stopped. The queue was not cleared.")

    async def pause_command(self, ctx: commands.Context):
        user = ctx.author.name
        cog_logger.info(f"!pause from {user}")

        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv:
            return

        if self.bot.main_vlc_content_type != self.bot.VLC_SR_PRIORITY or not self.bot.main_vlc_now_playing_details:
            await ctx.send(f"@{user}, there is no song request playing right now.")
            return

        if hasattr(self.bot, 'vlc_toggle_pause'):
            await self.bot.vlc_toggle_pause()

        is_paused = getattr(self.bot, 'main_vlc_is_paused', False)
        current_title = self.bot.main_vlc_now_playing_details.get('title', 'the current song')
        if is_paused:
            await ctx.send(f"@{user}, paused '{current_title}'.")
        else:
            await ctx.send(f"@{user}, resumed '{current_title}'.")

    async def play_command(self, ctx: commands.Context):
        user = ctx.author.name
        cog_logger.info(f"!play from {user}")

        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv:
            return

        has_current_sr = (
            self.bot.main_vlc_content_type == self.bot.VLC_SR_PRIORITY
            and self.bot.main_vlc_now_playing_details
        )
        if has_current_sr and getattr(self.bot, 'main_vlc_is_paused', False):
            if hasattr(self.bot, 'vlc_toggle_pause'):
                await self.bot.vlc_toggle_pause()

            current_title = self.bot.main_vlc_now_playing_details.get('title', 'the current song')
            await ctx.send(f"@{user}, resumed '{current_title}'.")
            return

        if has_current_sr:
            return

        if self.song_request_deque:
            await ctx.send(f"@{user}, resuming the song request queue.")
            await self.bot.transition_to_vlc_sr_mode()
            return

        await ctx.send(f"@{user}, there is nothing queued to play.")

    async def hide_command(self, ctx: commands.Context):
        user = ctx.author.name
        cog_logger.info(f"!hide from {user}")

        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv:
            return

        if self.bot.main_vlc_content_type != self.bot.VLC_SR_PRIORITY or not self.bot.main_vlc_now_playing_details:
            return

        if hasattr(self.bot, 'vlc_toggle_window_hidden'):
            await self.bot.vlc_toggle_window_hidden()

    async def show_command(self, ctx: commands.Context):
        user = ctx.author.name
        cog_logger.info(f"!show from {user}")

        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv:
            return

        if self.bot.main_vlc_content_type != self.bot.VLC_SR_PRIORITY or not self.bot.main_vlc_now_playing_details:
            return

        if hasattr(self.bot, 'vlc_show_window'):
            await self.bot.vlc_show_window()

    async def clear_queue_command(self, ctx: commands.Context):
        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv: return
        
        queue_len = self.clear_queue()
        if queue_len > 0:
            await ctx.send(f"@{ctx.author.name}, the song queue has been cleared ({queue_len} songs removed).")
        else:
            await ctx.send(f"@{ctx.author.name}, the song queue is already empty.")

    async def wrongsong_command(self, ctx: commands.Context):
        user_name = ctx.author.name

        for i in range(len(self.pending_downloads) - 1, -1, -1):
            if self.pending_downloads[i][1] == user_name:
                _, _, metadata = self.pending_downloads[i]
                real_title = metadata['title']
                del self.pending_downloads[i] 
                await ctx.send(f"@{user_name}, removed your request for '{real_title[:30]}'.")
                return

        if self.active_download and self.active_download[1] == user_name:
            _, _, metadata = self.active_download
            real_title = metadata['title']
            self.cancelled_downloads.add(real_title)
            await ctx.send(f"@{user_name}, removed your request for '{real_title[:30]}'.")
            return

        if not self.song_request_deque:
            await ctx.send(f"@{user_name}, the queue is empty and you have no pending songs.")
            return

        for i in range(len(self.song_request_deque) - 1, -1, -1):
            if self.song_request_deque[i].get("requested_by") == user_name:
                removed_song = self.song_request_deque[i]
                del self.song_request_deque[i]
                self._persist_queue()
                await ctx.send(f"@{user_name}, removed your request for '{removed_song.get('title', 'your song')}'.")
                return
        
        await ctx.send(f"@{user_name}, I couldn't find a song requested by you in the queue or pending downloads.")

    async def queue_command(self, ctx: commands.Context):
        parts = []
        now_playing = self.bot.main_vlc_now_playing_details
        
        if now_playing and self.bot.main_vlc_content_type == self.bot.VLC_SR_PRIORITY:
            parts.append(f"Now Playing: '{now_playing.get('title', '?')}' (by @{now_playing.get('requested_by', '?')})")
        else:
            parts.append("Now Playing: Nothing from the SR queue.")
            
        if not self.song_request_deque:
            parts.append("The queue is empty.")
        else:
            queue_list = list(self.song_request_deque)
            next_up = [f"'{s.get('title', '?')}' (by @{s.get('requested_by', '?')})" for s in queue_list[:3]]
            parts.append(f"Up Next ({len(queue_list)} total): " + " | ".join(next_up))
            if len(queue_list) > 3:
                parts[-1] += "..."
                
        await ctx.send(" || ".join(parts))

    async def sroff_command(self, ctx: commands.Context):
        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv: return
        self.bot.is_sr_enabled = False
        cog_logger.info("Song requests disabled via chat command.")
        await ctx.send("Song requests are now DISABLED.")

    async def sron_command(self, ctx: commands.Context):
        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv: return
        self.bot.is_sr_enabled = True
        cog_logger.info("Song requests enabled via chat command.")
        await ctx.send("Song requests are now ENABLED.")

    async def full_command(self, ctx: commands.Context):
        cog_logger.info(f"!full from {ctx.author.name}")
        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv:
            return
        
        current_state = getattr(self.bot, 'is_fullscreen_playing', False)
        new_state = not current_state
        
        if hasattr(self.bot, 'vlc_toggle_fullscreen'):
            await self.bot.vlc_toggle_fullscreen(new_state)
            
        status = "FULLSCREEN" if new_state else "MINIMIZED"
        cog_logger.info(f"Video scaled to {status} via command.")

    async def info_command(self, ctx: commands.Context):
        """Toggle the persistent song title on stream."""
        is_priv = (
            getattr(ctx.author, 'is_broadcaster', False)
            or getattr(ctx.author, 'is_mod', False)
            or getattr(ctx.author, 'is_vip', False)
            or ctx.author.name.lower() == ctx.channel.name.lower()
        )
        if not is_priv:
            return
        if hasattr(self.bot, 'vlc_toggle_title'):
            await self.bot.vlc_toggle_title()
        cog_logger.info("OBS song title toggled via chat command.")


def prepare(bot: commands.Bot):
    if not yt_dlp:
        cog_logger.critical("yt_dlp NOT LOADED. SR WILL FAIL.")
    
    cog = SRCog(bot)
    bot.add_cog(cog)

    s = getattr(bot, 'settings', None)

    def _alias(attr, default):
        return getattr(s, attr, default) if s else default

    # Dynamically register commands with configurable aliases + cooldowns
    sr_cmd = commands.Command(name=_alias('cmd_sr', 'sr'), func=cog.song_request_command)
    sr_cmd._cooldowns = [commands.Cooldown(1, 2, commands.Bucket.user)]
    
    skip_alias = _alias('cmd_skip', 'skip')
    skip_cmd = None
    if skip_alias.lower() != 'stop':
        skip_cmd = commands.Command(name=skip_alias, func=cog.skip_command)
    stop_cmd = commands.Command(name='stop', func=cog.stop_command)
    pause_cmd = commands.Command(name=_alias('cmd_pause', 'pause'), func=cog.pause_command)
    play_cmd = commands.Command(name=_alias('cmd_play', 'play'), func=cog.play_command)
    resume_cmd = commands.Command(name='resume', func=cog.play_command)
    hide_cmd = commands.Command(name=_alias('cmd_hide', 'hide'), func=cog.hide_command)
    show_cmd = commands.Command(name=_alias('cmd_show', 'show'), func=cog.show_command)
    queue_cmd = commands.Command(name=_alias('cmd_queue', 'queue'), func=cog.queue_command)
    wrongsong_cmd = commands.Command(name=_alias('cmd_wrongsong', 'wrongsong'), func=cog.wrongsong_command)
    clearqueue_cmd = commands.Command(name=_alias('cmd_clearqueue', 'clearqueue'), func=cog.clear_queue_command)
    full_cmd = commands.Command(name=_alias('cmd_full', 'full'), func=cog.full_command)
    info_cmd = commands.Command(name=_alias('cmd_info', 'info'), func=cog.info_command)
    sron_cmd = commands.Command(name=_alias('cmd_sron', 'sron'), func=cog.sron_command)
    sroff_cmd = commands.Command(name=_alias('cmd_sroff', 'sroff'), func=cog.sroff_command)

    for cmd in [sr_cmd, skip_cmd, stop_cmd, pause_cmd, play_cmd, resume_cmd, hide_cmd, show_cmd, queue_cmd, wrongsong_cmd, clearqueue_cmd, full_cmd, info_cmd, sron_cmd, sroff_cmd]:
        if cmd:
            bot.add_command(cmd)

    cog_logger.info("SRCog loaded (Standalone Version).")
