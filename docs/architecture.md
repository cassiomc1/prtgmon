# PRTG monitor architecture and operations

## Polling and atomic reconciliation

`prtg_monitor.py` loads and validates local configuration, restores previously persisted alert state (`state.json`) when available, fetches a complete paginated PRTG XML sensor snapshot (`/api/table.xml`), checks bounded rotating recovery status for missing alerts (`/api/getobjectstatus.htm`), persists the complete next state atomically, and only then delivers macOS notifications.

Each `run_cycle()` follows four strict phases:

1. **Compute next state without mutation**: Collect all pages (`sortby=objid`, validating `totalcount`, `listend`, response size limit `MAX_RESPONSE_BYTES`, page limit `MAX_PAGES`, and duplicate/missing `objid` values). Build `next_alerts` and `new_alerts`, then probe up to `status_lookup_limit` missing sensors via `/api/getobjectstatus.htm`. A sensor is resolved only when its status equals `STATUS_UP` (`3`). Rotation tracks the next unchecked sensor ID so resolving an entry mid-window does not skip subsequent sensors.
2. **Persist atomically**: Write the complete next `active_alerts` and `status_lookup_offset` payload via `tempfile.mkstemp(..., dir=state_dir)`, `chmod 0600`, `fsync`, and `os.replace` before mutating in-memory state. If persistence fails, `run_cycle()` aborts and keeps the previous state.
3. **Commit in-memory state**: Update `self.active_alerts`, `self.status_lookup_offset`, and `self.first_run` together.
4. **Deliver notifications**: Dispatch `notify_alert()` for new failures and `notify_resolution()` for confirmed recoveries (suppressed on an uninitialized `first_run` without prior persisted state).

The central reliability rule is that a failed HTTP/network request, HTTP error status, malformed XML document, PRTG `<error>` payload, or incomplete pagination result is **never** treated as an empty snapshot. The previous active state is preserved to avoid false recovery storms.

## Configuration and security

Copy `config.json.example` to a local, untracked `config.json` (`chmod 0600`). When `config.json` is absent, `PRTGMonitor` writes a default template with `0600` permissions and raises `ConfigError` so it never polls placeholder hosts.

`validate_config()` rejects non-object JSON, invalid or placeholder `prtg_url`/`apitoken`, non-positive `poll_interval_seconds`, empty or non-integer `filter_statuses`, non-boolean flags, invalid `notification_type`, and invalid timeout/limit settings.

- `verify_ssl` defaults to `true`. `verify_ssl=false` is strictly a controlled-lab option for self-signed certificates and emits a warning.
- API tokens are never logged (not even a prefix) and `redact_secrets()` strips `apitoken=...` query parameters and raw token occurrences from HTTP/network/PRTG error logs.
- XML parsing prefers `<message_raw>` / `<message_RAW>` over HTML-formatted `<message>`.

## Notification behavior

AppleScript execution passes all dynamic text (`message`, `title`, `subtitle`, `sound_name`) via `osascript -e ... -- <args>` (`on run argv`), eliminating string interpolation injection, stripping NUL bytes, enforcing `OSASCRIPT_TIMEOUT_SECONDS`, and reaping background dialog processes.

The `notification_type` modes remain strictly distinct:

| `notification_type` | Startup notice | New failure (`notify_alert`) | Confirmed recovery (`notify_resolution`) |
| --- | --- | --- | --- |
| `banner` | macOS banner (if `notify_on_startup`) | macOS banner | macOS banner (if `notify_on_resolve`) |
| `alert` | None | Modal dialog (`caution` icon) | Modal dialog (`note` icon, if `notify_on_resolve`) |
| `both` | macOS banner (if `notify_on_startup`) | macOS banner + modal dialog | macOS banner + modal dialog (if `notify_on_resolve`) |

## CLI, lifecycle, and LaunchAgent deployment

- `python3 prtg_monitor.py --check-config <config>` validates config and exits without polling.
- `python3 prtg_monitor.py --once <config>` runs one poll/reconciliation cycle and exits `0` on success or `1` on failure.
- `python3 prtg_monitor.py --no-startup-notification <config>` suppresses the startup banner.
- `start()` handles `SIGINT` and `SIGTERM` via a `threading.Event` so `launchctl bootout` terminates cleanly without interrupting an active write, and `log()` disables ANSI colors automatically when stdout is not a TTY (or when `NO_COLOR` is set).

For a LaunchAgent deployment, use a dedicated installation directory, absolute paths valid on the target Mac for Python and the script, and writable log paths (such as `~/Library/Logs/prtgmon/`). Never copy a developer checkout path into a reusable launchd file.

## Terminal UI (`prtg_tui.py`, `prtg_api.py`) and macOS App Bundle

`prtg_api.py` implements `PRTGClient`, a read-only JSON API client supporting `/api/getstatus.htm`, paginated `/api/table.json` queries (`sensors`, `devices`, `groups`, `channels`, `messages`), `/api/getsensordetails.json`, and `/api/historicdata.json`, redacting tokens from all HTTP/network errors.

`prtg_tui.py` provides an interactive, strictly read-only `curses` explorer across five tabs (`alerts`, `sensors`, `devices`, `groups`, `logs`) plus a full-screen Sensor Inspector with Unicode Braille time-series charts (`1h`–`30d`), multi-channel switching, full metadata, and 30-day sensor event/data history. It installs a custom log callback via `set_log_handler()` so `PRTGMonitor` logs never corrupt the `curses` display, primes initial rows from `state.json` if present, and never writes `state.json` or emits macOS notifications.

`scripts/install_macos_app.py` packages `prtg_tui.py`, `prtg_api.py`, `prtg_monitor.py`, and an `.icns` icon built via `sips`/`iconutil` into a self-contained macOS `.app` bundle (e.g., `/Applications/PRTG Monitor TUI.app`) that opens in `Terminal.app` via `/usr/bin/open -a Terminal` without Automation permission prompts or developer checkout path references.

## Validation

```bash
python3 -m unittest discover -v
/usr/bin/python3 -m unittest discover -v
python3 prtg_monitor.py --check-config config.json
python3 prtg_monitor.py --once config.json
```
