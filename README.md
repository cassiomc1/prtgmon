# PRTG Alert Monitor for macOS 🚀

## Technical guide

See [AGENTS.md](AGENTS.md) for the reliability contracts, validation rules, and secure configuration requirements.

The [technical architecture](docs/architecture.md) documents atomic alert reconciliation, state persistence, configuration validation, and LaunchAgent operation.

This project is a lightweight Python script that actively monitors your PRTG server through the XML API and displays native macOS notifications whenever a sensor enters an error state (Down) or returns to normal (Up).

> **Why this exists:** the official PRTG Desktop app for macOS is, frankly, terrible — slow, bloated, and unreliable. This project was created as a fast, dependency-free alternative that does the one thing that actually matters: alerting you when something breaks.

## ✨ Features

- **Zero Dependencies:** Built purely with Python standard libraries (`urllib`, `xml.etree`, `argparse`, `json`, etc.), compatible with the macOS `/usr/bin/python3` — no `pip install` required.
- **Native, Safe macOS Notifications:** Uses `osascript` (AppleScript) with safe parameter passing (`on run argv`) to trigger system notification banners and/or persistent modal dialogs with distinct behavior for `banner`, `alert`, and `both`.
- **Atomic Reconciliation and Persisted State:** Computes the entire next state after complete pagination and status verification before atomically persisting to `state.json` (with `0600` permissions) and sending notifications, avoiding duplicate alerts or false resolutions after restarts or API failures.
- **Quick Links and Safe Logs:** Shows the direct link to the failing sensor in the console (with colors only on TTY terminals), masks tokens in logs/errors, and strips HTML messages when `message_raw` is available.
- **Strict Configuration Validation:** Validates URL, token, interval, monitored statuses, and notification options before starting the loop (`--check-config`).
- **Single Cycle and Graceful Shutdown:** Supports `--once` for manual inspections and responds to `SIGTERM`/`SIGINT` for clean shutdown under LaunchAgent.

---

## 📁 File Structure

- [prtg_monitor.py](prtg_monitor.py) - Main monitoring, reconciliation, and CLI/LaunchAgent notification script.
- [prtg_api.py](prtg_api.py) - Read-only JSON client for exploring PRTG sensors, devices, groups, channels, history, and logs.
- [prtg_tui.py](prtg_tui.py) - Interactive terminal UI (`curses`) with tabs, Braille charts, and a sensor inspector.
- [scripts/install_macos_app.py](scripts/install_macos_app.py) - Packager and installer for the macOS app (`PRTG Monitor TUI.app`).
- [assets/prtg-monitor-tui.png](assets/prtg-monitor-tui.png) - Source icon for the macOS app.
- [config.json.example](config.json.example) - Configuration template (copy to `config.json`, ignored by Git).
- [docs/architecture.md](docs/architecture.md) - Architecture, reconciliation, and operation details.
- [AGENTS.md](AGENTS.md) - Technical contracts and reliability invariants.
- [tests/](tests/) - Unit test suite (`test_prtg_monitor.py`, `test_prtg_api.py`, `test_prtg_tui.py`, `test_install_macos_app.py`).

---

## ⚙️ Configuration (`config.json`)

Copy the example file to a local `config.json` and restrict its permissions:

```bash
cp config.json.example config.json
chmod 600 config.json
```

Edit `config.json` with your PRTG server URL and API token:

```json
{
  "prtg_url": "https://your-prtg-server.example.com",
  "apitoken": "YOUR_API_TOKEN_HERE",
  "poll_interval_seconds": 60,
  "filter_statuses": [5, 14],
  "verify_ssl": true,
  "notification_type": "banner",
  "sound_name": "Glass",
  "notify_on_resolve": true,
  "notify_on_startup": true,
  "state_file": "state.json"
}
```

> [!WARNING]
> Never commit `config.json` or paste real API tokens into documentation, shell history, or commits. If a token has been exposed before, revoke it and generate a new one in PRTG.

### Option Details

* `prtg_url` *(required)*: Base `https://` URL (or `http://` in a controlled lab) of the PRTG server.
* `apitoken` *(required)*: PRTG API token.
* `poll_interval_seconds` *(default: `60`)*: Positive interval in seconds between check cycles.
* `filter_statuses` *(default: `[5, 14]`)*: Non-empty list of PRTG status codes to monitor:
  * `5`: Down (total outage)
  * `14`: Down Partial (partial outage)
  * `13`: Down Acknowledged (acknowledged outage)
  * `4`: Warning
* `verify_ssl` *(default: `true`)*: Validates the PRTG server TLS certificate. Set to `false` **only** in controlled/lab environments with self-signed certificates.
* `notification_type` *(default: `"banner"`)*: macOS notification mode:
  * `"banner"`: Sends Notification Center banners (on startup if enabled, on new alerts, and on resolutions).
  * `"alert"`: Shows only modal dialog boxes (`display dialog`) for new alerts (`caution` icon) and resolutions (`note` icon), without banners or a startup banner.
  * `"both"`: Shows system banners **and** modal dialogs on alerts and resolutions.
* `sound_name` *(default: `"Glass"`)*: macOS sound name for alert banners (or `null` for silent mode).
* `resolve_sound_name` *(optional)*: Sound name for resolution banners. When omitted, defaults to `"Blow"` if `sound_name` is set, or `null` in silent mode.
* `notify_on_resolve` *(default: `true`)*: If `true`, notifies when a previously active sensor returns to status `3` (`Up`).
* `notify_on_startup` *(default: `true`)*: If `true` (and `notification_type` is `"banner"` or `"both"`), sends a banner confirming that monitoring has started.
* `state_file` *(default: `"state.json"`)*: State persistence file path (relative to the `config.json` directory, or absolute). Use `null` to disable on-disk persistence.
* `request_timeout_seconds` *(optional, default: `15`)*: HTTP timeout in seconds for paginated queries.
* `status_lookup_limit` *(optional, default: `10`)*: Maximum number of individual recovery status checks per cycle (with starvation-free rotation).
* `status_lookup_timeout_seconds` *(optional, default: `5`)*: HTTP timeout in seconds for each individual `/api/getobjectstatus.htm` query.

---

## 🚀 How to Run and Verify

1. Validate the configuration before starting:
```bash
python3 prtg_monitor.py --check-config config.json
```

2. Run a single test cycle to validate connectivity and parsing:
```bash
python3 prtg_monitor.py --once config.json
```

3. Start continuous monitoring:
```bash
python3 prtg_monitor.py config.json
```

4. Run the unit test suite:
```bash
python3 -m unittest discover -v
```

Available CLI flags:
- `--check-config`: Validates the configuration file and exits without querying PRTG.
- `--once`: Runs a single collection/reconciliation cycle and exits with code `0` (success) or `1` (failure).
- `--no-startup-notification`: Suppresses the startup banner for this run.

---

## 🖥️ Terminal UI (TUI) and macOS App

You can explore the entire PRTG server directly in the terminal with [prtg_tui.py](prtg_tui.py) (or via the `prtgmon-tui` command / `PRTG Monitor TUI.app` application):

```bash
python3 prtg_tui.py
# or following only the LaunchAgent's state.json without extra API queries:
python3 prtg_tui.py --from-state
```

### Install as a macOS App (`/Applications/PRTG Monitor TUI.app`)

```bash
python3 scripts/install_macos_app.py
# or to install into the user's ~/Applications folder:
python3 scripts/install_macos_app.py --dest ~/Applications
```

### TUI Features
- **Top-Right Analog Clock (São Paulo GMT-3):** high-resolution Unicode Braille analog clock dial with hour, minute, and second hands plus a live `São Paulo / GMT-3` readout in the top-right header box.
- **5 Tabs (`1`–`5` or `Tab`):**
  1. `Alerts`: sensors with active alerts (loads `state.json` instantly on open).
  2. `Sensors`: all PRTG sensors with status filter (`f`: All/Alerts/Down/Warning/Up/Paused/Other) and sorting (`s`).
  3. `Devices`: lists all devices and health counters; `Enter` opens the sensors for that device.
  4. `Groups`: lists groups/probes; `Enter` opens the sensors for that group.
  5. `Logs`: global PRTG event history (`t` toggles between Today, 7 days, and 30 days).
- **Global search (`/`):** real-time filtering by name, device, group, probe, tags, ID, or message (`Enter` applies, `Esc` cancels, `c` clears filters).
- **Sensor Inspector (`Enter` on a sensor):**
  - **Braille Chart + Channels (`1`):** high-resolution Unicode Braille line chart with statistics (`Last`, `Min`, `Avg`, `Max`), channel switching (`n`/`p` or `←`/`→`), and time window (`t`: `1h`, `2h`, `24h`, `7d`, `30d`).
  - **Full Details (`2`):** complete metadata, uptime/downtime, interval, tags, and all live channels.
  - **History & Events (`3`):** recent sensor events (30 days) and chronological table of historical points.
  - `Esc` or `b` returns to the list; `o` opens in the browser.

Command-line options:
- `--from-state`: starts observing the `state.json` updated by the LaunchAgent.
- `--interval <seconds>`: sets the auto-refresh interval in `LIVE` mode.
- `--no-color`: disables terminal colors.

Main shortcuts:
- `1`–`5` / `Tab`: switch tabs | `↑`/`↓` or `j`/`k`: navigate (`g`/`G` top/bottom, `PgUp`/`PgDn` pages)
- `/`: search | `c`: clear filters | `f`: filter status | `s`: sort | `r`: refresh
- `Enter`: open sensor inspector (or filter sensors by device/group)
- `m`: toggle `LIVE` / `STATE` mode | `o`: open in browser | `?`: help | `q`: quit

---

## 🔄 Running in the Background on macOS (LaunchAgent)

To keep the monitor running continuously in the background on the target Mac:

1. Install the files in a dedicated user directory (for example `/Users/<YOUR_USER>/prtgmon`) and verify the absolute Python path with `which python3` (for example `/usr/bin/python3` or `/opt/homebrew/bin/python3`).
2. Create the log directory:
```bash
mkdir -p ~/Library/Logs/prtgmon
```
3. Create `~/Library/LaunchAgents/com.prtg.monitor.plist`, adjusting `<YOUR_USER>` and the `python3` path to be valid on the target Mac:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.prtg.monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/&lt;YOUR_USER&gt;/prtgmon/prtg_monitor.py</string>
        <string>/Users/&lt;YOUR_USER&gt;/prtgmon/config.json</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>/Users/&lt;YOUR_USER&gt;/prtgmon</string>
    <key>StandardOutPath</key>
    <string>/Users/&lt;YOUR_USER&gt;/Library/Logs/prtgmon/monitor.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/&lt;YOUR_USER&gt;/Library/Logs/prtgmon/monitor_error.log</string>
</dict>
</plist>
```

4. Validate the plist and start the LaunchAgent:
```bash
plutil -lint ~/Library/LaunchAgents/com.prtg.monitor.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.prtg.monitor.plist
```

5. Verify execution and logs:
```bash
launchctl print gui/$(id -u)/com.prtg.monitor
tail -f ~/Library/Logs/prtgmon/monitor.log ~/Library/Logs/prtgmon/monitor_error.log
```

6. To stop or restart after updates:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.prtg.monitor.plist
```
