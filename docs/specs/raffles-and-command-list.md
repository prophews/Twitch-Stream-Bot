# Spec: Raffles And Public Command List

## Objective
Add StreamElements-style raffle support and a viewer-safe command list while improving the Loyalty & Automation admin UI scrolling. The feature is for streamer-controlled community automation and must remain safe for public distribution.

## Scope
- Add a streamer-controlled raffle module with chat entry, countdown announcements, winner drawing, cancellation, and optional currency rewards.
- Add dashboard controls for raffle setup and status.
- Add visible scrollbars and mouse-wheel support for Custom Commands, Timed Messages, and Leaderboard tables.
- Add a local command list page/API that exposes only safe command metadata.

## Privacy And Security Boundaries
- Never expose OAuth tokens, local media paths, database paths, Streamer.bot URLs, action IDs, OBS details, logs, or config files through the command list.
- Do not expose disabled custom commands, disabled timers, or hidden/internal runtime state to viewers.
- Raffle creation/cancel/draw is dashboard-only for v1; chat users can only enter an active raffle.
- The localhost server continues to bind to `127.0.0.1`.

## Success Criteria
- Viewers can enter an active raffle with the configured entry command.
- Duplicate raffle entries are ignored without spam.
- Raffle announcements, countdowns, and winner messages are configurable.
- A winner can optionally receive the configured loyalty currency reward.
- The dashboard can start, cancel, and draw a raffle.
- Admin tables can be mouse-scrolled and show visible vertical scrollbars.
- `/commands` renders a simple viewer-facing command list without sensitive data.
- `/api/commands` returns sanitized JSON only.

## Verification
- `python -m unittest discover -s tests -v`
- Manual GUI check: Loyalty & Automation scrollbars, raffle controls, and command list URL.
