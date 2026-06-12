# Security and Privacy

## Reporting a Vulnerability

Do not post OAuth tokens, passwords, private paths, or loyalty databases in a
public issue. Report security concerns through GitHub's private vulnerability
reporting feature when available.

## Local Data

Installed versions store user-specific data under:

```text
%LOCALAPPDATA%\Twitch Song Request Bot
```

The public release must not contain `config.json`, OAuth tokens, queue state,
logs, SQLite databases, temporary downloads, or local media.

## Network Access

- Twitch chat uses the configured OAuth token.
- YouTube extraction is performed by `yt-dlp`.
- The OBS player is served only from the loopback interface.
- Streamer.bot integration defaults to `127.0.0.1`.
- Update checks query the official GitHub Releases API.

Only download installers from this repository's Releases page.
