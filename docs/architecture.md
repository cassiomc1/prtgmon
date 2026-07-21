# PRTG monitor architecture and operations

## Polling and reconciliation

`prtg_monitor.py` loads local configuration, requests a paginated PRTG XML sensor snapshot, parses relevant sensor states, compares that complete snapshot with the active-alert state, persists the next state, and sends macOS notifications for new failures or resolutions.

The central reliability rule is that a failed request, malformed XML document, or incomplete pagination result is **not** an empty snapshot. Keep the previous active state in that case; otherwise an API outage would generate false recoveries.

## Configuration

Copy `config.json.example` to a local, untracked `config.json`. Configure the PRTG URL, API token, poll interval, monitored raw status codes, TLS verification, notification type, sound, and resolution notification behavior. Store the token as a secret and rotate it if it ever reaches a shell history, backup, or repository.

`verify_ssl=false` is a lab-only escape hatch for a controlled, self-signed endpoint. Production monitoring should validate a trusted certificate.

## Notification behavior

The monitor supports `banner`, `alert`, and `both`. Alert de-duplication is keyed by the active sensor occurrence; a sensor should produce one initial failure notice and, if enabled, one recovery notice.

## Validation and deployment

```bash
python3 -m unittest discover -v
python3 prtg_monitor.py
```

For a LaunchAgent deployment, use a dedicated installation directory, absolute paths to Python and the script, and writable log paths. After a change, reload or kickstart the agent, verify its running state, and inspect a full poll cycle for network, XML, and PRTG API errors. Do not copy a developer checkout path into a reusable launchd file.
