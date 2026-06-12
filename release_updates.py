from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


GITHUB_REPOSITORY = "prophews/Twitch-Stream-Bot"
LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
)
LATEST_RELEASE_PAGE = (
    f"https://github.com/{GITHUB_REPOSITORY}/releases/latest"
)


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    page_url: str
    installer_url: str = ""


def version_tuple(value: str) -> tuple[int, ...]:
    cleaned = (value or "").strip().lower().lstrip("v")
    numeric = cleaned.split("-", 1)[0]
    parts = numeric.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError(f"Invalid version: {value}")
    return tuple(int(part) for part in parts)


def is_newer_version(latest: str, current: str) -> bool:
    latest_parts = version_tuple(latest)
    current_parts = version_tuple(current)
    width = max(len(latest_parts), len(current_parts))
    return latest_parts + (0,) * (width - len(latest_parts)) > (
        current_parts + (0,) * (width - len(current_parts))
    )


def fetch_latest_release(timeout: int = 8) -> ReleaseInfo:
    response = requests.get(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Twitch-Stream-Bot-Update-Check",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    version = str(payload.get("tag_name", "")).strip()
    if not version:
        raise ValueError("GitHub's latest release did not include a version tag.")

    installer_url = ""
    for asset in payload.get("assets", []):
        name = str(asset.get("name", "")).lower()
        if name.endswith(".exe") and "setup" in name:
            installer_url = str(asset.get("browser_download_url", "")).strip()
            break

    return ReleaseInfo(
        version=version.lstrip("v"),
        page_url=str(payload.get("html_url", "")).strip() or LATEST_RELEASE_PAGE,
        installer_url=installer_url,
    )
