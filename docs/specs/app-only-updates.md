# Spec: App-Only Windows Updates

## Objective

Provide two Windows installation paths:

- A full installer and portable ZIP containing FFmpeg and FFprobe for new users.
- A smaller app-only installer for existing installed copies that updates the
  application while preserving the existing FFmpeg and FFprobe binaries.

The in-app update check should prefer the app-only installer when a release
provides one, while retaining compatibility with older releases that only
provide the full installer.

## Commands

- Tests: `python -m unittest discover -s tests -v`
- Syntax: `python -m py_compile release_updates.py run_gui.py`
- Full release build, only when explicitly requested:
  `powershell -ExecutionPolicy Bypass -File .\build_release.ps1`

## Project Structure

- `release_updates.py`: GitHub release asset selection.
- `run_gui.py`: Update-check user flow.
- `installer.iss`: Full self-contained installer.
- `app_update.iss`: Existing-install-only app updater.
- `build_release.ps1`: Produces both installers.
- `verify_release.ps1`: Verifies both installation paths.
- `tests/`: Unit and packaging-contract tests.

## Code Style

Use explicit asset names and conservative fallbacks:

```python
download_url = release.app_update_url or release.installer_url
```

## Testing Strategy

- Unit-test release asset selection and legacy-release fallback.
- Contract-test that both installers share the same `AppId`.
- Contract-test that the app-only installer excludes FFmpeg and FFprobe.
- During an explicitly requested release build, verify that a full install
  followed by an app-only update retains byte-identical media binaries.
- Verify that the app-only installer refuses a clean installation.

## Boundaries

- Always preserve existing FFmpeg and FFprobe during app-only updates.
- Always keep the full installer and portable ZIP self-contained.
- Never offer the app-only package as a new-user installer.
- Never build or publish release artifacts without explicit user approval.

## Success Criteria

- GitHub releases can contain full, portable, and app-only packages.
- The app prefers the app-only package when checking for updates.
- Older releases without an app-only package still open the full installer.
- App-only installation requires an existing valid installation.
- Full installation remains usable on a clean Windows PC.
