# PRTG monitor development guide

## Architecture

`prtg_monitor.py` polls PRTG's XML API, validates a complete snapshot, compares active failures with persisted state, and delivers macOS notifications. `config.json` is deployment-specific configuration.

## Development

```bash
python3 -m unittest discover
python3 prtg_monitor.py
```

Use a redacted, local `config.json` for manual checks. Never commit a real PRTG API token.

## Reliability invariants

- An API failure or malformed snapshot is not an empty alarm set and must not resolve active alerts.
- Reconciliation must be atomic: calculate the complete next state before persisting it.
- Keep notification types `banner`, `alert`, and `both` behaviorally distinct.
- LaunchAgent instructions must use paths valid for the target Mac, not a developer checkout path.
