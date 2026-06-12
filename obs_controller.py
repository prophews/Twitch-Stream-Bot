import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import uuid4

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType


class OBSRequestError(Exception):
    pass


@dataclass
class OBSPlaybackPreparation:
    attempted: bool = False
    refreshed: bool = False
    shown: bool = False


class OBSController:
    def __init__(self, settings, logger):
        self.settings = settings
        self.logger = logger
        self._session: Optional[ClientSession] = None
        self._ws: Optional[ClientWebSocketResponse] = None
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._source_name_cache: Optional[str] = None
        self._scene_name_cache: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "obs_ws_enabled", True))

    async def close(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._source_name_cache = None
        self._scene_name_cache = None

    async def ensure_connected(self) -> bool:
        if not self.enabled:
            return False

        if self._ws and not self._ws.closed:
            return True

        async with self._connect_lock:
            if self._ws and not self._ws.closed:
                return True

            await self.close()

            try:
                self._session = ClientSession()
                self._ws = await self._session.ws_connect(
                    f"ws://{self.settings.obs_ws_host}:{self.settings.obs_ws_port}",
                    protocols=("obswebsocket.json",),
                    timeout=3,
                )

                hello = await self._receive_json()
                if hello.get("op") != 0:
                    raise OBSRequestError("OBS did not send a Hello message.")

                auth_data = (hello.get("d") or {}).get("authentication")
                identify = {
                    "op": 1,
                    "d": {
                        "rpcVersion": 1,
                        "eventSubscriptions": 0,
                    },
                }
                if auth_data:
                    identify["d"]["authentication"] = self._build_auth_string(
                        self.settings.obs_ws_password,
                        auth_data["salt"],
                        auth_data["challenge"],
                    )

                await self._ws.send_json(identify)
                identified = await self._receive_json()
                if identified.get("op") != 2:
                    raise OBSRequestError("OBS did not accept Identify.")

                self.logger.info(
                    f"Connected to OBS WebSocket at {self.settings.obs_ws_host}:{self.settings.obs_ws_port}."
                )
                return True
            except Exception as exc:
                self.logger.warning(
                    f"OBS automation unavailable at {self.settings.obs_ws_host}:{self.settings.obs_ws_port}: {exc}"
                )
                await self.close()
                return False

    async def prepare_for_playback(self, player_url: str) -> OBSPlaybackPreparation:
        result = OBSPlaybackPreparation()
        if not await self.ensure_connected():
            return result

        source_name = await self._resolve_browser_source_name(player_url)
        if not source_name:
            return result

        scene_name = await self._resolve_scene_name(source_name)
        if not scene_name:
            return result

        scene_item_id = await self._get_scene_item_id(scene_name, source_name)
        if scene_item_id is None:
            return result

        result.attempted = True

        if getattr(self.settings, "obs_force_show_on_play", True):
            try:
                await self.request(
                    "SetSceneItemEnabled",
                    {
                        "sceneName": scene_name,
                        "sceneItemId": scene_item_id,
                        "sceneItemEnabled": True,
                    },
                )
                result.shown = True
                self.logger.info(
                    f"OBS automation enabled browser source '{source_name}' in scene '{scene_name}'."
                )
            except OBSRequestError as exc:
                self.logger.warning(f"Could not show OBS browser source '{source_name}': {exc}")

        if getattr(self.settings, "obs_auto_refresh", True):
            try:
                await self.request(
                    "PressInputPropertiesButton",
                    {
                        "inputName": source_name,
                        "propertyName": "refreshnocache",
                    },
                )
                result.refreshed = True
                self.logger.info(f"OBS automation refreshed browser source '{source_name}'.")
            except OBSRequestError as exc:
                self.logger.warning(
                    f"Could not trigger OBS browser source refresh for '{source_name}': {exc}"
                )

        return result

    async def handle_idle(self, player_url: str) -> bool:
        if not getattr(self.settings, "obs_hide_when_idle", False):
            return False

        if not await self.ensure_connected():
            return False

        source_name = await self._resolve_browser_source_name(player_url)
        if not source_name:
            return False

        scene_name = await self._resolve_scene_name(source_name)
        if not scene_name:
            return False

        scene_item_id = await self._get_scene_item_id(scene_name, source_name)
        if scene_item_id is None:
            return False

        try:
            await self.request(
                "SetSceneItemEnabled",
                {
                    "sceneName": scene_name,
                    "sceneItemId": scene_item_id,
                    "sceneItemEnabled": False,
                },
            )
            self.logger.info(
                f"OBS automation hid browser source '{source_name}' in scene '{scene_name}'."
            )
            return True
        except OBSRequestError as exc:
            self.logger.warning(f"Could not hide OBS browser source '{source_name}': {exc}")
            return False

    async def request(self, request_type: str, request_data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not await self.ensure_connected():
            raise OBSRequestError("OBS WebSocket is not connected.")

        assert self._ws is not None

        async with self._request_lock:
            request_id = str(uuid4())
            payload = {
                "op": 6,
                "d": {
                    "requestType": request_type,
                    "requestId": request_id,
                    "requestData": request_data or {},
                },
            }

            try:
                await self._ws.send_json(payload)
                while True:
                    message = await self._receive_json()
                    if message.get("op") != 7:
                        continue

                    data = message.get("d") or {}
                    if data.get("requestId") != request_id:
                        continue

                    status = data.get("requestStatus") or {}
                    if not status.get("result"):
                        comment = status.get("comment") or "unknown error"
                        code = status.get("code")
                        raise OBSRequestError(f"{request_type} failed ({code}): {comment}")

                    return data.get("responseData") or {}
            except OBSRequestError:
                raise
            except Exception as exc:
                await self.close()
                raise OBSRequestError(f"{request_type} failed: {exc}") from exc

    async def _receive_json(self) -> dict[str, Any]:
        assert self._ws is not None

        while True:
            msg = await self._ws.receive(timeout=5)
            if msg.type == WSMsgType.TEXT:
                return json.loads(msg.data)
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                raise OBSRequestError("OBS WebSocket closed unexpectedly.")
            if msg.type == WSMsgType.ERROR:
                raise OBSRequestError(f"OBS WebSocket error: {self._ws.exception()}")

    def _build_auth_string(self, password: str, salt: str, challenge: str) -> str:
        secret = base64.b64encode(
            hashlib.sha256((password + salt).encode("utf-8")).digest()
        ).decode("utf-8")
        return base64.b64encode(
            hashlib.sha256((secret + challenge).encode("utf-8")).digest()
        ).decode("utf-8")

    async def _resolve_browser_source_name(self, player_url: str) -> Optional[str]:
        explicit_name = (getattr(self.settings, "obs_browser_source_name", "") or "").strip()
        if explicit_name:
            self._source_name_cache = explicit_name
            return explicit_name

        if self._source_name_cache:
            return self._source_name_cache

        try:
            response = await self.request("GetInputList")
        except OBSRequestError as exc:
            self.logger.warning(f"Could not list OBS inputs for browser source auto-detection: {exc}")
            return None

        inputs = response.get("inputs") or []
        browser_sources = [item for item in inputs if item.get("inputKind") == "browser_source"]
        if not browser_sources:
            self.logger.warning("OBS automation could not find any browser source inputs.")
            return None

        matching_names: list[str] = []
        for item in browser_sources:
            input_name = item.get("inputName")
            if not input_name:
                continue

            try:
                settings_response = await self.request("GetInputSettings", {"inputName": input_name})
            except OBSRequestError:
                continue

            input_settings = settings_response.get("inputSettings") or {}
            if self._url_matches_player(input_settings.get("url"), player_url):
                matching_names.append(input_name)

        if len(matching_names) == 1:
            self._source_name_cache = matching_names[0]
            self.logger.info(
                f"OBS automation auto-detected browser source '{self._source_name_cache}' from the player URL."
            )
            return self._source_name_cache

        if len(browser_sources) == 1:
            self._source_name_cache = browser_sources[0].get("inputName")
            self.logger.info(
                f"OBS automation is using the only browser source input it found: '{self._source_name_cache}'."
            )
            return self._source_name_cache

        self.logger.warning(
            "OBS automation found multiple browser sources and could not choose one automatically. "
            "Set the Browser Source Name in the app settings."
        )
        return None

    async def _resolve_scene_name(self, source_name: str) -> Optional[str]:
        explicit_scene = (getattr(self.settings, "obs_browser_scene_name", "") or "").strip()
        if explicit_scene:
            self._scene_name_cache = explicit_scene
            return explicit_scene

        if self._scene_name_cache:
            return self._scene_name_cache

        scene_candidates: list[str] = []

        try:
            current_scene = (await self.request("GetCurrentProgramScene")).get("currentProgramSceneName")
            if current_scene:
                scene_candidates.append(current_scene)
        except OBSRequestError:
            current_scene = None

        try:
            scene_list = await self.request("GetSceneList")
            for scene in scene_list.get("scenes") or []:
                scene_name = scene.get("sceneName")
                if scene_name and scene_name not in scene_candidates:
                    scene_candidates.append(scene_name)
        except OBSRequestError as exc:
            self.logger.warning(f"Could not inspect OBS scenes for browser source auto-detection: {exc}")
            return current_scene

        for scene_name in scene_candidates:
            scene_item_id = await self._get_scene_item_id(scene_name, source_name)
            if scene_item_id is not None:
                self._scene_name_cache = scene_name
                if scene_name == current_scene:
                    self.logger.info(
                        f"OBS automation found browser source '{source_name}' in the current program scene '{scene_name}'."
                    )
                else:
                    self.logger.info(
                        f"OBS automation found browser source '{source_name}' in scene '{scene_name}'."
                    )
                return scene_name

        self.logger.warning(
            f"OBS automation could not find browser source '{source_name}' in any scene. "
            "Set the Scene Name in the app settings if needed."
        )
        return None

    async def _get_scene_item_id(self, scene_name: str, source_name: str) -> Optional[int]:
        try:
            response = await self.request(
                "GetSceneItemId",
                {
                    "sceneName": scene_name,
                    "sourceName": source_name,
                },
            )
            return response.get("sceneItemId")
        except OBSRequestError:
            return None

    def _url_matches_player(self, candidate_url: Optional[str], player_url: str) -> bool:
        if not candidate_url:
            return False

        try:
            candidate = urlparse(str(candidate_url))
            player = urlparse(player_url)
        except Exception:
            return False

        candidate_host = (candidate.hostname or "").lower()
        player_host = (player.hostname or "").lower()
        host_matches = candidate_host == player_host or {candidate_host, player_host} <= {"127.0.0.1", "localhost"}

        return (
            host_matches
            and candidate.port == player.port
            and candidate.path.rstrip("/") == player.path.rstrip("/")
        )
