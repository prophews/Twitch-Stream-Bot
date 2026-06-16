# Spec: Raffles And Automation Scrollbars

## Objective
Add StreamElements-style raffle support while improving the Loyalty & Automation admin UI scrolling. The feature is for streamer-controlled community automation and must remain safe for public distribution.

## Scope
- Add a streamer-controlled raffle module with chat entry, countdown announcements, winner drawing, cancellation, and optional currency rewards.
- Add dashboard controls for raffle setup and status.
- Add visible scrollbars and mouse-wheel support for Custom Commands, Timed Messages, and Leaderboard tables.

## Privacy And Security Boundaries
- Raffle creation/cancel/draw is dashboard-only for v1; chat users can only enter an active raffle.
- The localhost server continues to bind to `127.0.0.1`.
- OAuth tokens, local media paths, database paths, Streamer.bot URLs, action IDs, OBS details, logs, and config files remain local.

## Success Criteria
- Viewers can enter an active raffle with the configured entry command.
- Duplicate raffle entries are ignored without spam.
- Raffle announcements, countdowns, and winner messages are configurable.
- A winner can optionally receive the configured loyalty currency reward.
- The dashboard can start, cancel, and draw a raffle.
- Admin tables can be mouse-scrolled and show visible vertical scrollbars.

## Verification
- `python -m unittest discover -s tests -v`
- Manual GUI check: Loyalty & Automation scrollbars and raffle controls.
