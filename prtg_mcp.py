#!/usr/bin/env python3
"""Read-only, zero-dependency MCP (Model Context Protocol) stdio server for PRTG."""

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta

from prtg_api import (
    HISTORY_WINDOWS,
    PRTGClient,
    format_human_number,
    format_prtg_ole_datetime,
    safe_float,
    safe_int,
    strip_html,
    validate_objid,
)
from prtg_monitor import ConfigError, PRTGMonitor, redact_secrets, set_log_handler

try:
    from prtg_tui import load_state_snapshot, resolve_config_path, summarize_alerts
except ImportError:  # pragma: no cover - fallback when curses is unavailable
    DEFAULT_INSTALLED_CONFIG = os.path.expanduser("~/.local/share/prtgmon/config.json")

    def resolve_config_path(explicit_path=None):
        if explicit_path:
            return os.path.expanduser(explicit_path)
        if os.path.exists("config.json"):
            return "config.json"
        if os.path.exists(DEFAULT_INSTALLED_CONFIG):
            return DEFAULT_INSTALLED_CONFIG
        return "config.json"

    def load_state_snapshot(state_path):
        if not state_path or not os.path.exists(state_path):
            return [], None, f"State file not found: {state_path}"
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_alerts = data.get("active_alerts", {})
            alerts = [dict(v) for v in raw_alerts.values() if isinstance(v, dict)]
            return alerts, os.path.getmtime(state_path), None
        except (OSError, json.JSONDecodeError, ValueError) as e:
            return None, None, f"Failed to read state: {e}"

    def summarize_alerts(alerts):
        total = len(alerts)
        down = sum(1 for a in alerts if a.get("status_raw") == 5)
        partial = sum(1 for a in alerts if a.get("status_raw") == 14)
        return {"total": total, "down": down, "partial": partial, "other": total - down - partial}


SERVER_NAME = "prtg-mcp"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
MAX_MCP_MESSAGE_BYTES = 2 * 1024 * 1024

DOWN_STATUS_CODES = frozenset({5, 13, 14})
ALERT_STATUS_CODES = frozenset({4, 5, 10, 13, 14})
PAUSED_STATUS_CODES = frozenset({7, 8, 9, 11, 12})
UP_STATUS_CODES = frozenset({2, 3})

STATUS_PRIORITY = {
    5: 0,   # Down
    14: 1,  # Down Partial
    13: 2,  # Down Acknowledged
    4: 3,   # Warning
    10: 4,  # Unusual
    1: 5,   # Unknown
    2: 6,   # Scanning
    3: 7,   # Up
    7: 8,   # Paused by user
    8: 9,   # Paused by dependency
    9: 10,  # Paused by schedule
    11: 11, # Paused by license
    12: 12, # Paused until
}

READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

_RELATIVE_AGO_RE = re.compile(
    r"\[(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)\s+ago\]",
    re.IGNORECASE,
)
_TRAILING_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_OLE_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)(?:\s+\[.*\])?\s*$")
_DATETIME_FORMATS = (
    "%d.%m.%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
)
_OUTAGE_KEYWORDS = ("down", "partial", "warning", "error", "erro", "timeout", "unreachable", "failed", "queda")
_RECOVERY_KEYWORDS = ("up", "ok", "normal", "resolved", "recovered")


def install_stderr_logging():
    """Redirects PRTGMonitor logs to stderr so stdout remains strictly reserved for MCP JSON-RPC."""
    def _stderr_sink(message, level="info"):
        sys.stderr.write(f"[{str(level).upper()}] {message}\n")
        sys.stderr.flush()

    return set_log_handler(_stderr_sink)


def _parse_bool_env(raw_val, field_name):
    text = str(raw_val).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"Environment variable '{field_name}' must be a boolean ('true' or 'false').")


def load_mcp_config(config_path=None, environ=None):
    """Loads and validates PRTG configuration from file and optional PRTG_* environment variables."""
    env = os.environ if environ is None else environ
    explicit = config_path or env.get("PRTG_CONFIG")
    resolved_path = resolve_config_path(explicit)

    raw_config = {}
    has_env_credentials = bool(env.get("PRTG_URL") and env.get("PRTG_APITOKEN"))

    if os.path.exists(resolved_path):
        try:
            with open(resolved_path, "r", encoding="utf-8") as f:
                raw_config = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"Failed to read configuration '{resolved_path}': {e}") from e
        if not isinstance(raw_config, dict):
            raise ConfigError("Configuration must be a JSON object.")
    elif not has_env_credentials:
        raise ConfigError(
            f"Configuration file '{resolved_path}' not found and PRTG_URL/PRTG_APITOKEN are not set."
        )

    merged = dict(raw_config)
    if env.get("PRTG_URL"):
        merged["prtg_url"] = env["PRTG_URL"]
    if env.get("PRTG_APITOKEN"):
        merged["apitoken"] = env["PRTG_APITOKEN"]
    if "PRTG_VERIFY_SSL" in env and str(env.get("PRTG_VERIFY_SSL", "")).strip():
        merged["verify_ssl"] = _parse_bool_env(env["PRTG_VERIFY_SSL"], "PRTG_VERIFY_SSL")

    validated = PRTGMonitor.validate_config(merged)
    state_file = validated.get("state_file")
    if state_file:
        expanded = os.path.expanduser(state_file)
        if os.path.isabs(expanded):
            state_path = expanded
        else:
            base_dir = (
                os.path.dirname(os.path.abspath(resolved_path))
                if os.path.exists(resolved_path)
                else os.getcwd()
            )
            state_path = os.path.join(base_dir, expanded)
    else:
        state_path = None

    return validated, state_path, resolved_path


def _clamp_int(value, default, minimum, maximum, name="limit"):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Parameter '{name}' must be an integer.")
    if value < minimum or value > maximum:
        raise ValueError(f"Parameter '{name}' must be between {minimum} and {maximum}.")
    return value


def _require_bool(value, default, name):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"Parameter '{name}' must be a boolean.")
    return value


def _optional_str(value, name):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Parameter '{name}' must be a string.")
    text = value.strip()
    return text or None


def _sensor_url(prtg_url, objid):
    base = str(prtg_url or "").rstrip("/")
    return f"{base}/sensor.htm?id={urllib.parse.quote(str(objid), safe='')}"


def _sensor_matches_query(sensor, query):
    """Multi-term case-insensitive search across all sensor identifying fields."""
    if not query:
        return True
    tokens = [tok for tok in query.lower().split() if tok]
    if not tokens:
        return True
    haystack = " ".join(
        str(sensor.get(field, "") or "").lower()
        for field in ("objid", "sensor", "device", "group", "probe", "tags", "message", "status")
    )
    return all(tok in haystack for tok in tokens)


def _sensor_matches_status(sensor, status_filter):
    code = safe_int(sensor.get("status_raw"), 0)
    if status_filter == "all":
        return True
    if status_filter == "alerts":
        return code in ALERT_STATUS_CODES
    if status_filter == "down":
        return code in DOWN_STATUS_CODES
    if status_filter == "warn":
        return code in (4, 10)
    if status_filter == "up":
        return code in UP_STATUS_CODES
    if status_filter == "paused":
        return code in PAUSED_STATUS_CODES
    if status_filter == "other":
        return code not in ALERT_STATUS_CODES and code not in UP_STATUS_CODES and code not in PAUSED_STATUS_CODES
    raise ValueError(f"Unsupported status filter '{status_filter}'.")


def _score_sensor_match(sensor, query):
    q = query.lower().strip()
    name = str(sensor.get("sensor", "")).lower()
    device = str(sensor.get("device", "")).lower()
    objid = str(sensor.get("objid", "")).lower()
    if objid == q:
        return 0
    if name == q:
        return 1
    if name.startswith(q):
        return 2
    if q in name:
        return 3
    if q in device:
        return 4
    return 5


def _resolve_sensor_target(client, objid=None, query=None):
    """Resolves a target sensor by explicit objid or multi-term query."""
    if objid is not None and str(objid).strip() != "":
        safe_id = validate_objid(objid)
        if safe_id is None:
            return None, None, [], "Invalid 'objid' format."
        return safe_id, None, [], None

    if not query:
        return None, None, [], "Provide either 'objid' or 'query' to identify the sensor."

    sensors, err = client.fetch_all_sensors()
    if err is not None:
        return None, None, [], err

    matches = [s for s in sensors if _sensor_matches_query(s, query)]
    if not matches:
        return None, None, [], f"No PRTG sensor matched query '{query}'."

    matches.sort(key=lambda s: (_score_sensor_match(s, query), s.get("device", "").lower(), s.get("sensor", "").lower()))
    best = matches[0]
    candidates = [
        {
            "objid": m["objid"],
            "sensor": m["sensor"],
            "device": m["device"],
            "group": m["group"],
            "status": m["status"],
            "lastvalue": m["lastvalue"],
        }
        for m in matches[:10]
    ]
    return best["objid"], best, candidates, None


def _parse_prtg_timestamp(text, now=None):
    """Best-effort parser for PRTG datetime strings, OLE dates, and '[N m ago]' suffixes."""
    cleaned = strip_html(text)
    if not cleaned:
        return None

    ref_now = datetime.now() if now is None else now
    ago_match = _RELATIVE_AGO_RE.search(cleaned)
    base_str = _TRAILING_BRACKET_RE.sub("", cleaned).strip()

    ole_match = _OLE_NUMBER_RE.match(cleaned)
    if ole_match:
        converted = format_prtg_ole_datetime(ole_match.group(1))
        if converted and converted != ole_match.group(1):
            base_str = converted

    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(base_str, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except ValueError:
            continue

    if ago_match:
        amount = int(ago_match.group(1))
        unit = ago_match.group(2).lower()
        if unit.startswith("s"):
            return ref_now - timedelta(seconds=amount)
        if unit.startswith("m"):
            return ref_now - timedelta(minutes=amount)
        if unit.startswith("h"):
            return ref_now - timedelta(hours=amount)
        if unit.startswith("d"):
            return ref_now - timedelta(days=amount)

    return None


def _is_incident_message(msg):
    status_raw = safe_int(msg.get("status_raw"), 0)
    if status_raw in ALERT_STATUS_CODES:
        return True
    combined = f"{msg.get('status', '')} {msg.get('type', '')} {msg.get('message', '')}".lower()
    return any(kw in combined for kw in _OUTAGE_KEYWORDS)


def _is_recovery_message(msg):
    status_raw = safe_int(msg.get("status_raw"), 0)
    if status_raw == 3:
        return True
    status_text = str(msg.get("status", "")).strip().lower()
    return any(kw == status_text or kw in status_text for kw in _RECOVERY_KEYWORDS)


def _downsample_indices(total, max_points):
    if total <= max_points:
        return list(range(total))
    if max_points <= 1:
        return [total - 1]
    step = (total - 1) / float(max_points - 1)
    indices = []
    last_idx = -1
    for i in range(max_points):
        idx = int(round(i * step))
        if idx >= total:
            idx = total - 1
        if idx != last_idx:
            indices.append(idx)
            last_idx = idx
    if indices[-1] != total - 1:
        indices[-1] = total - 1
    return indices


class PRTGReadOnlyService:
    """Executes read-only PRTG MCP tools, resources, and prompts."""

    def __init__(self, config, client=None, state_path=None, now_fn=None):
        self.config = dict(config)
        self.client = client or PRTGClient.from_config(self.config)
        self.state_path = state_path
        self.now_fn = now_fn or datetime.now

    def _safe_error(self, err):
        return redact_secrets(err, getattr(self.client, "apitoken", ""))

    def get_tool_definitions(self):
        return [
            {
                "name": "prtg_get_overview",
                "description": (
                    "Read-only overview of PRTG server health, sensor counts by state (Up, Down, Warning, Paused), "
                    "top active alerts, and local prtgmon state.json snapshot. "
                    "Visão geral somente leitura do servidor PRTG e contagem de sensores."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "include_server_status": {
                            "type": "boolean",
                            "description": "Include PRTG /api/getstatus.htm metadata (default: true).",
                            "default": True,
                        },
                        "max_alerts": {
                            "type": "integer",
                            "description": "Maximum active alerts to include in the summary (1..200, default: 25).",
                            "minimum": 1,
                            "maximum": 200,
                            "default": 25,
                        },
                    },
                    "additionalProperties": False,
                },
                "annotations": dict(READ_ONLY_ANNOTATIONS),
            },
            {
                "name": "prtg_list_alerts",
                "description": (
                    "Lists currently failing or warning PRTG sensors (Down, Down Partial, Down Acknowledged, Warning, Unusual) "
                    "with device, group, probe, message, downtime, lastdown, and direct PRTG link. "
                    "Lista os sensores atualmente em alerta ou falha no PRTG."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "include_warnings": {
                            "type": "boolean",
                            "description": "Include Warning/Unusual sensors in addition to Down sensors (default: true).",
                            "default": True,
                        },
                        "include_acknowledged": {
                            "type": "boolean",
                            "description": "Include Down Acknowledged sensors (default: true).",
                            "default": True,
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional search terms to filter alerts across sensor, device, group, probe, tags, or message.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of alerts to return (1..500, default: 100).",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 100,
                        },
                    },
                    "additionalProperties": False,
                },
                "annotations": dict(READ_ONLY_ANNOTATIONS),
            },
            {
                "name": "prtg_outages_report",
                "description": (
                    "Correlates PRTG event logs and sensor timestamps to answer 'quais sensores caíram essa noite?' "
                    "or 'which sensors went down recently?'. Includes sensors still down and sensors that went down and recovered. "
                    "Relatório de quedas recentes ou noturnas correlacionando logs e sensores."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "period": {
                            "type": "string",
                            "enum": ["overnight", "today", "24h", "7days", "30days"],
                            "description": "Time period to inspect: 'overnight' (last 12h, default), 'today', '24h', '7days', or '30days'.",
                            "default": "overnight",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Optional explicit lookback window in hours (1..720), overriding period default.",
                            "minimum": 1,
                            "maximum": 720,
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional filter by sensor, device, group, probe, tags, or message.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of affected sensors to return (1..500, default: 100).",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 100,
                        },
                    },
                    "additionalProperties": False,
                },
                "annotations": dict(READ_ONLY_ANNOTATIONS),
            },
            {
                "name": "prtg_search_sensors",
                "description": (
                    "Searches PRTG sensors by name, device, group, probe, tags, message, or objid with optional status filter. "
                    "Busca sensores por nome, dispositivo, grupo, tag ou ID."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Space-separated search terms matched across sensor, device, group, probe, tags, message, and objid.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["all", "alerts", "down", "warn", "up", "paused", "other"],
                            "description": "Filter by sensor status category (default: 'all').",
                            "default": "all",
                        },
                        "device": {
                            "type": "string",
                            "description": "Optional case-insensitive substring filter on device name.",
                        },
                        "group": {
                            "type": "string",
                            "description": "Optional case-insensitive substring filter on group name.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of sensors to return (1..500, default: 50).",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 50,
                        },
                    },
                    "additionalProperties": False,
                },
                "annotations": dict(READ_ONLY_ANNOTATIONS),
            },
            {
                "name": "prtg_get_sensor_details",
                "description": (
                    "Fetches detailed metadata, uptime/downtime, live channels, and recent event logs for a specific PRTG sensor "
                    "identified by objid or search query. Obtém detalhes, canais e eventos recentes de um sensor."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "objid": {
                            "type": ["string", "integer"],
                            "description": "PRTG sensor object ID (objid).",
                        },
                        "query": {
                            "type": "string",
                            "description": "Search query to resolve the sensor if objid is not provided.",
                        },
                        "include_channels": {
                            "type": "boolean",
                            "description": "Include live sensor channels (default: true).",
                            "default": True,
                        },
                        "include_events": {
                            "type": "boolean",
                            "description": "Include recent 30-day sensor event messages (default: true).",
                            "default": True,
                        },
                    },
                    "additionalProperties": False,
                },
                "annotations": dict(READ_ONLY_ANNOTATIONS),
            },
            {
                "name": "prtg_sensor_history",
                "description": (
                    "Answers 'qual o histórico desse link?' by retrieving historical time-series statistics (min, avg, max, last), "
                    "sampled time points, live channels, sensor metadata, and recent outage events for a link or sensor. "
                    "Consulta o histórico de tráfego, latência, disponibilidade e eventos de um link ou sensor."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "objid": {
                            "type": ["string", "integer"],
                            "description": "PRTG sensor object ID (objid).",
                        },
                        "query": {
                            "type": "string",
                            "description": "Search query (e.g., 'WAN Embratel', 'MPLS') to resolve the link/sensor when objid is omitted.",
                        },
                        "window": {
                            "type": "string",
                            "enum": [w[0] for w in HISTORY_WINDOWS],
                            "description": "Preset time window: '1h', '2h', '24h' (default), '7d', or '30d'.",
                            "default": "24h",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Optional custom lookback duration in hours (1..720), overriding 'window'.",
                            "minimum": 1,
                            "maximum": 720,
                        },
                        "max_points": {
                            "type": "integer",
                            "description": "Maximum number of time-series points to return in the sample (1..500, default: 48).",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 48,
                        },
                    },
                    "additionalProperties": False,
                },
                "annotations": dict(READ_ONLY_ANNOTATIONS),
            },
            {
                "name": "prtg_list_devices",
                "description": (
                    "Lists PRTG monitored devices with Up/Down/Warning/Paused sensor counters. "
                    "Lista os dispositivos monitorados no PRTG com contadores de saúde."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Optional search terms across device, host, group, probe, or tags.",
                        },
                        "only_with_issues": {
                            "type": "boolean",
                            "description": "If true, returns only devices that currently have Down, Partial, Warning, or Unusual sensors.",
                            "default": False,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of devices to return (1..500, default: 100).",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 100,
                        },
                    },
                    "additionalProperties": False,
                },
                "annotations": dict(READ_ONLY_ANNOTATIONS),
            },
            {
                "name": "prtg_list_groups",
                "description": (
                    "Lists PRTG groups and probes with Up/Down/Warning/Paused sensor counters. "
                    "Lista grupos e probes do PRTG com contadores de sensores."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Optional search terms across group or probe name.",
                        },
                        "only_with_issues": {
                            "type": "boolean",
                            "description": "If true, returns only groups with Down or Warning sensors.",
                            "default": False,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of groups to return (1..500, default: 100).",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 100,
                        },
                    },
                    "additionalProperties": False,
                },
                "annotations": dict(READ_ONLY_ANNOTATIONS),
            },
        ]

    def get_resource_definitions(self):
        return [
            {
                "uri": "prtg://status/overview",
                "name": "PRTG Server & Sensor Overview",
                "description": "Current read-only PRTG sensor health summary and top active alerts.",
                "mimeType": "application/json",
            },
            {
                "uri": "prtg://alerts/active",
                "name": "PRTG Active Alerts",
                "description": "Current read-only list of active PRTG alerts.",
                "mimeType": "application/json",
            },
        ]

    def read_resource(self, uri):
        if uri == "prtg://status/overview":
            data = self._tool_get_overview({})
            return [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, ensure_ascii=False, indent=2)}]
        if uri == "prtg://alerts/active":
            data = self._tool_list_alerts({})
            return [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, ensure_ascii=False, indent=2)}]
        raise KeyError(f"Unknown resource URI: {uri}")

    def get_prompt_definitions(self):
        return [
            {
                "name": "relatorio-noturno",
                "description": "Pergunta ao Claude quais sensores caíram essa noite usando dados reais do PRTG.",
                "arguments": [
                    {
                        "name": "hours",
                        "description": "Janela de horas para análise (padrão: 12).",
                        "required": False,
                    }
                ],
            },
            {
                "name": "historico-link",
                "description": "Consulta o histórico recente e estabilidade de um link ou sensor específico no PRTG.",
                "arguments": [
                    {
                        "name": "query",
                        "description": "Nome, dispositivo ou objid do link/sensor.",
                        "required": True,
                    },
                    {
                        "name": "window",
                        "description": "Janela de tempo: 1h, 2h, 24h, 7d ou 30d.",
                        "required": False,
                    },
                ],
            },
        ]

    def get_prompt(self, name, arguments=None):
        args = arguments if isinstance(arguments, dict) else {}
        if name == "relatorio-noturno":
            hours = args.get("hours", "12")
            return {
                "description": "Relatório noturno de quedas de sensores no PRTG",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": (
                                f"Use a ferramenta `prtg_outages_report` (period='overnight', hours={hours}) "
                                "para listar quais sensores caíram essa noite, distinguindo os que ainda estão fora "
                                "dos que já normalizaram, com horários, dispositivos afetados e mensagens de erro."
                            ),
                        },
                    }
                ],
            }
        if name == "historico-link":
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("Prompt argument 'query' is required for 'historico-link'.")
            window = str(args.get("window") or "24h").strip()
            return {
                "description": f"Histórico do link '{query}' no PRTG",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": (
                                f"Use a ferramenta `prtg_sensor_history` com query='{query}' e window='{window}' "
                                "para analisar o histórico desse link (média, pico, mínimo, disponibilidade/uptime e "
                                "eventuais quedas registradas nos eventos recentes)."
                            ),
                        },
                    }
                ],
            }
        raise KeyError(f"Unknown prompt '{name}'.")

    def call_tool(self, name, arguments=None):
        if arguments is None:
            args = {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            raise ValueError("Tool 'arguments' must be a JSON object.")

        handlers = {
            "prtg_get_overview": self._tool_get_overview,
            "prtg_list_alerts": self._tool_list_alerts,
            "prtg_outages_report": self._tool_outages_report,
            "prtg_search_sensors": self._tool_search_sensors,
            "prtg_get_sensor_details": self._tool_get_sensor_details,
            "prtg_sensor_history": self._tool_sensor_history,
            "prtg_list_devices": self._tool_list_devices,
            "prtg_list_groups": self._tool_list_groups,
        }
        handler = handlers.get(name)
        if handler is None:
            raise KeyError(f"Unknown tool '{name}'.")
        return handler(args)

    def _tool_get_overview(self, args):
        include_server_status = _require_bool(args.get("include_server_status"), True, "include_server_status")
        max_alerts = _clamp_int(args.get("max_alerts"), 25, 1, 200, "max_alerts")

        sensors, err = self.client.fetch_all_sensors()
        if err is not None:
            raise RuntimeError(self._safe_error(err))

        counts = {
            "total": len(sensors),
            "up": 0,
            "down": 0,
            "down_partial": 0,
            "down_acknowledged": 0,
            "warning": 0,
            "unusual": 0,
            "paused": 0,
            "other": 0,
        }
        alerts = []
        for s in sensors:
            code = safe_int(s.get("status_raw"), 0)
            if code in UP_STATUS_CODES:
                counts["up"] += 1
            elif code == 5:
                counts["down"] += 1
            elif code == 14:
                counts["down_partial"] += 1
            elif code == 13:
                counts["down_acknowledged"] += 1
            elif code == 4:
                counts["warning"] += 1
            elif code == 10:
                counts["unusual"] += 1
            elif code in PAUSED_STATUS_CODES:
                counts["paused"] += 1
            else:
                counts["other"] += 1

            if code in ALERT_STATUS_CODES:
                alerts.append(s)

        alerts.sort(
            key=lambda s: (
                STATUS_PRIORITY.get(safe_int(s.get("status_raw"), 0), 99),
                str(s.get("device", "")).lower(),
                str(s.get("sensor", "")).lower(),
            )
        )

        server_status = None
        server_status_error = None
        if include_server_status:
            status_payload, status_err = self.client.fetch_server_status()
            if status_err is not None:
                server_status_error = self._safe_error(status_err)
            elif isinstance(status_payload, dict):
                server_status = {
                    k: strip_html(v) if isinstance(v, str) else v
                    for k, v in status_payload.items()
                    if "token" not in str(k).lower() and "pass" not in str(k).lower()
                }

        local_state = None
        if self.state_path:
            st_alerts, st_updated, st_err = load_state_snapshot(self.state_path)
            if st_err is not None:
                local_state = {"state_path": self.state_path, "available": False, "error": st_err}
            else:
                summary = summarize_alerts(st_alerts or [])
                updated_iso = (
                    datetime.fromtimestamp(st_updated).strftime("%Y-%m-%d %H:%M:%S")
                    if st_updated is not None
                    else None
                )
                local_state = {
                    "state_path": self.state_path,
                    "available": True,
                    "updated_at": updated_iso,
                    "summary": summary,
                }

        prtg_url = self.config.get("prtg_url", "")
        return {
            "prtg_url": prtg_url,
            "read_only": True,
            "sensor_counts": counts,
            "active_alerts_total": len(alerts),
            "top_alerts": [
                {
                    "objid": a["objid"],
                    "sensor": a["sensor"],
                    "device": a["device"],
                    "group": a["group"],
                    "status": a["status"],
                    "status_raw": a["status_raw"],
                    "message": a["message"],
                    "lastvalue": a["lastvalue"],
                    "lastdown": a["lastdown"],
                    "sensor_url": _sensor_url(prtg_url, a["objid"]),
                }
                for a in alerts[:max_alerts]
            ],
            "server_status": server_status,
            "server_status_error": server_status_error,
            "local_state_snapshot": local_state,
        }

    def _tool_list_alerts(self, args):
        include_warnings = _require_bool(args.get("include_warnings"), True, "include_warnings")
        include_ack = _require_bool(args.get("include_acknowledged"), True, "include_acknowledged")
        query = _optional_str(args.get("query"), "query")
        limit = _clamp_int(args.get("limit"), 100, 1, 500, "limit")

        allowed_codes = {5, 14}
        if include_ack:
            allowed_codes.add(13)
        if include_warnings:
            allowed_codes.update({4, 10})

        sensors, err = self.client.fetch_all_sensors()
        if err is not None:
            raise RuntimeError(self._safe_error(err))

        matched = [
            s
            for s in sensors
            if safe_int(s.get("status_raw"), 0) in allowed_codes and _sensor_matches_query(s, query)
        ]
        matched.sort(
            key=lambda s: (
                STATUS_PRIORITY.get(safe_int(s.get("status_raw"), 0), 99),
                str(s.get("device", "")).lower(),
                str(s.get("sensor", "")).lower(),
            )
        )
        prtg_url = self.config.get("prtg_url", "")
        return {
            "total_matching": len(matched),
            "returned_count": min(len(matched), limit),
            "alerts": [
                {
                    "objid": s["objid"],
                    "sensor": s["sensor"],
                    "device": s["device"],
                    "group": s["group"],
                    "probe": s["probe"],
                    "status": s["status"],
                    "status_raw": s["status_raw"],
                    "message": s["message"],
                    "lastvalue": s["lastvalue"],
                    "downtime": s["downtime"],
                    "lastdown": s["lastdown"],
                    "lastup": s["lastup"],
                    "tags": s["tags"],
                    "sensor_url": _sensor_url(prtg_url, s["objid"]),
                }
                for s in matched[:limit]
            ],
        }

    def _tool_outages_report(self, args):
        period = _optional_str(args.get("period"), "period") or "overnight"
        period_aliases = {
            "tonight": "overnight",
            "last_12h": "overnight",
        }
        period = period_aliases.get(period.lower(), period.lower())
        valid_periods = ("overnight", "today", "24h", "7days", "30days")
        if period not in valid_periods:
            raise ValueError(f"Parameter 'period' must be one of: {', '.join(valid_periods)}.")

        hours_arg = args.get("hours")
        query = _optional_str(args.get("query"), "query")
        limit = _clamp_int(args.get("limit"), 100, 1, 500, "limit")

        now_dt = self.now_fn()
        if hours_arg is not None:
            lookback_hours = _clamp_int(hours_arg, 12, 1, 720, "hours")
            cutoff_dt = now_dt - timedelta(hours=lookback_hours)
            window_desc = f"last {lookback_hours}h"
        elif period == "overnight":
            lookback_hours = 12
            cutoff_dt = now_dt - timedelta(hours=lookback_hours)
            window_desc = "overnight (last 12h)"
        elif period == "today":
            cutoff_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            lookback_hours = max(1, int((now_dt - cutoff_dt).total_seconds() // 3600) + 1)
            window_desc = "today (since 00:00)"
        elif period == "24h":
            lookback_hours = 24
            cutoff_dt = now_dt - timedelta(hours=24)
            window_desc = "last 24h"
        elif period == "7days":
            lookback_hours = 168
            cutoff_dt = now_dt - timedelta(days=7)
            window_desc = "last 7 days"
        else:
            lookback_hours = 720
            cutoff_dt = now_dt - timedelta(days=30)
            window_desc = "last 30 days"

        if period == "today" and cutoff_dt.date() == now_dt.date():
            drel = "today"
        elif lookback_hours <= 168:
            drel = "7days"
        else:
            drel = "30days"

        sensors, s_err = self.client.fetch_all_sensors()
        if s_err is not None:
            raise RuntimeError(self._safe_error(s_err))

        messages, m_err = self.client.fetch_messages(drel=drel, count=500)
        if m_err is not None:
            raise RuntimeError(self._safe_error(m_err))

        sensors_by_id = {s["objid"]: s for s in sensors}
        incidents_by_key = {}

        def _ensure_entry(key, sensor_obj=None, fallback_msg=None):
            if key in incidents_by_key:
                return incidents_by_key[key]
            if sensor_obj is not None:
                entry = {
                    "objid": sensor_obj["objid"],
                    "sensor": sensor_obj["sensor"],
                    "device": sensor_obj["device"],
                    "group": sensor_obj["group"],
                    "probe": sensor_obj["probe"],
                    "current_status": sensor_obj["status"],
                    "current_status_raw": sensor_obj["status_raw"],
                    "lastvalue": sensor_obj["lastvalue"],
                    "lastdown": sensor_obj["lastdown"],
                    "lastup": sensor_obj["lastup"],
                    "downtime": sensor_obj["downtime"],
                    "current_message": sensor_obj["message"],
                    "tags": sensor_obj["tags"],
                    "sensor_url": _sensor_url(self.config.get("prtg_url", ""), sensor_obj["objid"]),
                    "events": [],
                    "_has_incident_event": False,
                    "_has_recovery_event": False,
                    "_latest_ts": None,
                }
            else:
                entry = {
                    "objid": str(fallback_msg.get("objid", "")),
                    "sensor": str(fallback_msg.get("name", "")),
                    "device": str(fallback_msg.get("parent", "")),
                    "group": "",
                    "probe": "",
                    "current_status": str(fallback_msg.get("status", "")),
                    "current_status_raw": safe_int(fallback_msg.get("status_raw"), 0),
                    "lastvalue": "",
                    "lastdown": "",
                    "lastup": "",
                    "downtime": "",
                    "current_message": str(fallback_msg.get("message", "")),
                    "tags": "",
                    "sensor_url": _sensor_url(self.config.get("prtg_url", ""), fallback_msg.get("objid", "")),
                    "events": [],
                    "_has_incident_event": False,
                    "_has_recovery_event": False,
                    "_latest_ts": None,
                }
            incidents_by_key[key] = entry
            return entry

        # Step 1: Collect matching event log messages inside the time window.
        for msg in messages:
            msg_dt = _parse_prtg_timestamp(msg.get("datetime", ""), now=now_dt)
            if msg_dt is not None and msg_dt < cutoff_dt:
                continue
            if msg_dt is None and drel != "today":
                # Without a parseable timestamp on multi-day drel, only correlate if sensor is currently failing.
                msg_objid = str(msg.get("objid", "")).strip()
                linked = sensors_by_id.get(msg_objid)
                if not linked or safe_int(linked.get("status_raw"), 0) not in ALERT_STATUS_CODES:
                    continue

            is_inc = _is_incident_message(msg)
            is_rec = _is_recovery_message(msg)
            if not is_inc and not is_rec:
                continue

            msg_objid = str(msg.get("objid", "")).strip()
            linked_sensor = sensors_by_id.get(msg_objid) if msg_objid else None
            key = f"id:{msg_objid}" if msg_objid else f"name:{msg.get('parent', '')}|{msg.get('name', '')}"

            # Only attach recovery events if sensor already has an incident or we record it to check later.
            entry = _ensure_entry(key, sensor_obj=linked_sensor, fallback_msg=msg)
            entry["events"].append({
                "datetime": msg.get("datetime", ""),
                "status": msg.get("status", ""),
                "status_raw": msg.get("status_raw", 0),
                "type": msg.get("type", ""),
                "message": msg.get("message", ""),
            })
            if is_inc:
                entry["_has_incident_event"] = True
            if is_rec:
                entry["_has_recovery_event"] = True
            if msg_dt is not None and (entry["_latest_ts"] is None or msg_dt > entry["_latest_ts"]):
                entry["_latest_ts"] = msg_dt

        # Drop entries that only logged routine "Up" messages without any outage/down indicator.
        for key in list(incidents_by_key.keys()):
            entry = incidents_by_key[key]
            if not entry["_has_incident_event"] and safe_int(entry["current_status_raw"], 0) not in ALERT_STATUS_CODES:
                del incidents_by_key[key]

        # Step 2: Also include sensors currently in Down/Warning state or with lastdown inside the window.
        for s in sensors:
            code = safe_int(s.get("status_raw"), 0)
            lastdown_dt = _parse_prtg_timestamp(s.get("lastdown", ""), now=now_dt)
            in_window_lastdown = lastdown_dt is not None and lastdown_dt >= cutoff_dt
            if code in DOWN_STATUS_CODES or in_window_lastdown:
                key = f"id:{s['objid']}"
                entry = _ensure_entry(key, sensor_obj=s)
                if lastdown_dt is not None and (entry["_latest_ts"] is None or lastdown_dt > entry["_latest_ts"]):
                    entry["_latest_ts"] = lastdown_dt

        # Step 3: Filter by query, classify, sort, and build clean output.
        filtered = []
        for entry in incidents_by_key.values():
            if query:
                searchable = {
                    "objid": entry["objid"],
                    "sensor": entry["sensor"],
                    "device": entry["device"],
                    "group": entry["group"],
                    "probe": entry["probe"],
                    "tags": entry["tags"],
                    "message": entry["current_message"] + " " + " ".join(ev["message"] for ev in entry["events"]),
                    "status": entry["current_status"],
                }
                if not _sensor_matches_query(searchable, query):
                    continue

            code = safe_int(entry["current_status_raw"], 0)
            if code in DOWN_STATUS_CODES:
                classification = "currently_down"
            elif code in ALERT_STATUS_CODES:
                classification = "currently_warning"
            else:
                classification = "down_and_recovered"

            entry["classification"] = classification
            filtered.append(entry)

        filtered.sort(
            key=lambda item: (
                0 if item["classification"] == "currently_down" else (1 if item["classification"] == "currently_warning" else 2),
                -(item["_latest_ts"].timestamp() if item["_latest_ts"] is not None else 0.0),
                item["device"].lower(),
                item["sensor"].lower(),
            )
        )

        currently_down_count = sum(1 for item in filtered if item["classification"] == "currently_down")
        currently_warning_count = sum(1 for item in filtered if item["classification"] == "currently_warning")
        recovered_count = sum(1 for item in filtered if item["classification"] == "down_and_recovered")

        cleaned_incidents = []
        for item in filtered[:limit]:
            cleaned = {k: v for k, v in item.items() if not k.startswith("_")}
            cleaned_incidents.append(cleaned)

        return {
            "period": period,
            "window_description": window_desc,
            "cutoff_time": cutoff_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "current_time": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_affected_sensors": len(filtered),
                "currently_down_count": currently_down_count,
                "currently_warning_count": currently_warning_count,
                "recovered_in_window_count": recovered_count,
            },
            "returned_count": len(cleaned_incidents),
            "incidents": cleaned_incidents,
        }

    def _tool_search_sensors(self, args):
        query = _optional_str(args.get("query"), "query")
        status = (_optional_str(args.get("status"), "status") or "all").lower()
        device_filter = _optional_str(args.get("device"), "device")
        group_filter = _optional_str(args.get("group"), "group")
        limit = _clamp_int(args.get("limit"), 50, 1, 500, "limit")

        sensors, err = self.client.fetch_all_sensors()
        if err is not None:
            raise RuntimeError(self._safe_error(err))

        matched = []
        for s in sensors:
            if not _sensor_matches_status(s, status):
                continue
            if not _sensor_matches_query(s, query):
                continue
            if device_filter and device_filter.lower() not in str(s.get("device", "")).lower():
                continue
            if group_filter and group_filter.lower() not in str(s.get("group", "")).lower():
                continue
            matched.append(s)

        matched.sort(
            key=lambda s: (
                _score_sensor_match(s, query) if query else STATUS_PRIORITY.get(safe_int(s.get("status_raw"), 0), 99),
                STATUS_PRIORITY.get(safe_int(s.get("status_raw"), 0), 99),
                str(s.get("device", "")).lower(),
                str(s.get("sensor", "")).lower(),
            )
        )
        prtg_url = self.config.get("prtg_url", "")
        return {
            "total_matching": len(matched),
            "returned_count": min(len(matched), limit),
            "sensors": [
                {
                    "objid": s["objid"],
                    "sensor": s["sensor"],
                    "device": s["device"],
                    "group": s["group"],
                    "probe": s["probe"],
                    "status": s["status"],
                    "status_raw": s["status_raw"],
                    "lastvalue": s["lastvalue"],
                    "message": s["message"],
                    "uptime": s["uptime"],
                    "downtime": s["downtime"],
                    "lastcheck": s["lastcheck"],
                    "lastup": s["lastup"],
                    "lastdown": s["lastdown"],
                    "tags": s["tags"],
                    "sensor_url": _sensor_url(prtg_url, s["objid"]),
                }
                for s in matched[:limit]
            ],
        }

    def _tool_get_sensor_details(self, args):
        objid = args.get("objid")
        query = _optional_str(args.get("query"), "query")
        include_channels = _require_bool(args.get("include_channels"), True, "include_channels")
        include_events = _require_bool(args.get("include_events"), True, "include_events")

        target_id, matched_summary, candidates, resolve_err = _resolve_sensor_target(
            self.client, objid=objid, query=query
        )
        if resolve_err is not None:
            raise ValueError(resolve_err) if "Provide either" in resolve_err or "Invalid 'objid'" in resolve_err else RuntimeError(self._safe_error(resolve_err))

        details, det_err = self.client.fetch_sensor_details(target_id)
        if det_err is not None:
            raise RuntimeError(self._safe_error(det_err))

        channels = None
        channels_error = None
        if include_channels:
            ch_data, ch_err = self.client.fetch_sensor_channels(target_id)
            if ch_err is not None:
                channels_error = self._safe_error(ch_err)
            else:
                channels = ch_data

        events = None
        events_error = None
        if include_events:
            ev_data, ev_err = self.client.fetch_messages(objid=target_id, drel="30days", count=50)
            if ev_err is not None:
                events_error = self._safe_error(ev_err)
            else:
                events = ev_data

        result = {
            "objid": target_id,
            "details": details,
            "channels": channels,
            "channels_error": channels_error,
            "recent_events": events,
            "events_error": events_error,
            "sensor_url": _sensor_url(self.config.get("prtg_url", ""), target_id),
        }
        if matched_summary is not None:
            result["matched_from_query"] = matched_summary
        if len(candidates) > 1:
            result["other_candidates"] = candidates[1:]
        return result

    def _tool_sensor_history(self, args):
        objid = args.get("objid")
        query = _optional_str(args.get("query"), "query")
        window = (_optional_str(args.get("window"), "window") or "24h").lower()
        valid_windows = tuple(w[0] for w in HISTORY_WINDOWS)
        if window not in valid_windows:
            raise ValueError(f"Parameter 'window' must be one of: {', '.join(valid_windows)}.")

        hours_arg = args.get("hours")
        if hours_arg is not None:
            hours_arg = _clamp_int(hours_arg, 24, 1, 720, "hours")
        max_points = _clamp_int(args.get("max_points"), 48, 1, 500, "max_points")

        target_id, matched_summary, candidates, resolve_err = _resolve_sensor_target(
            self.client, objid=objid, query=query
        )
        if resolve_err is not None:
            raise ValueError(resolve_err) if "Provide either" in resolve_err or "Invalid 'objid'" in resolve_err else RuntimeError(self._safe_error(resolve_err))

        now_dt = self.now_fn()
        historic, hist_err = self.client.fetch_historic_data(
            target_id, window_key=window, now=now_dt, hours=hours_arg
        )
        if hist_err is not None:
            raise RuntimeError(self._safe_error(hist_err))

        details, det_err = self.client.fetch_sensor_details(target_id)
        if det_err is not None:
            details = None
            details_error = self._safe_error(det_err)
        else:
            details_error = None

        channels, ch_err = self.client.fetch_sensor_channels(target_id)
        if ch_err is not None:
            channels = None
            channels_error = self._safe_error(ch_err)
        else:
            channels_error = None

        if hours_arg is not None:
            drel = "today" if hours_arg <= 24 else ("7days" if hours_arg <= 168 else "30days")
        elif window in ("1h", "2h", "24h"):
            drel = "7days"
        elif window == "7d":
            drel = "7days"
        else:
            drel = "30days"

        events, ev_err = self.client.fetch_messages(objid=target_id, drel=drel, count=100)
        if ev_err is not None:
            events = None
            events_error = self._safe_error(ev_err)
        else:
            events_error = None

        channel_summaries = {}
        for ch_name, stat in historic.get("stats", {}).items():
            channel_summaries[ch_name] = {
                "min": stat["min"],
                "max": stat["max"],
                "avg": stat["avg"],
                "last": stat["last"],
                "count": stat["count"],
                "min_human": format_human_number(stat["min"]),
                "max_human": format_human_number(stat["max"]),
                "avg_human": format_human_number(stat["avg"]),
                "last_human": format_human_number(stat["last"]),
            }

        timestamps = historic.get("timestamps", [])
        coverage = historic.get("coverage", [])
        series = historic.get("series", {})
        ch_names = historic.get("channels", [])
        total_points = len(timestamps)
        indices = _downsample_indices(total_points, max_points)

        samples = []
        for idx in indices:
            point = {
                "datetime": timestamps[idx] if idx < len(timestamps) else "",
                "coverage": coverage[idx] if idx < len(coverage) else "",
                "values": {},
            }
            for ch in ch_names:
                vals = series.get(ch, [])
                val = vals[idx] if idx < len(vals) else None
                point["values"][ch] = val
            samples.append(point)

        result = {
            "objid": target_id,
            "window": f"{hours_arg}h" if hours_arg is not None else window,
            "sensor_url": _sensor_url(self.config.get("prtg_url", ""), target_id),
            "details": details,
            "details_error": details_error,
            "live_channels": channels,
            "channels_error": channels_error,
            "total_raw_points": total_points,
            "sampled_points_count": len(samples),
            "channels": ch_names,
            "channel_summaries": channel_summaries,
            "samples": samples,
            "recent_events": events,
            "events_error": events_error,
        }
        if matched_summary is not None:
            result["matched_from_query"] = matched_summary
        if len(candidates) > 1:
            result["other_candidates"] = candidates[1:]
        return result

    def _tool_list_devices(self, args):
        query = _optional_str(args.get("query"), "query")
        only_issues = _require_bool(args.get("only_with_issues"), False, "only_with_issues")
        limit = _clamp_int(args.get("limit"), 100, 1, 500, "limit")

        devices, err = self.client.fetch_devices()
        if err is not None:
            raise RuntimeError(self._safe_error(err))

        tokens = [tok for tok in (query or "").lower().split() if tok]
        matched = []
        for d in devices:
            if only_issues and (d["down"] + d["partial"] + d["warn"] + d["unusual"]) <= 0:
                continue
            if tokens:
                haystack = " ".join(
                    str(d.get(f, "") or "").lower()
                    for f in ("objid", "device", "host", "group", "probe", "tags")
                )
                if not all(tok in haystack for tok in tokens):
                    continue
            matched.append(d)

        matched.sort(key=lambda d: (-(d["down"] + d["partial"]), -d["warn"], str(d.get("device", "")).lower()))
        return {
            "total_matching": len(matched),
            "returned_count": min(len(matched), limit),
            "devices": matched[:limit],
        }

    def _tool_list_groups(self, args):
        query = _optional_str(args.get("query"), "query")
        only_issues = _require_bool(args.get("only_with_issues"), False, "only_with_issues")
        limit = _clamp_int(args.get("limit"), 100, 1, 500, "limit")

        groups, err = self.client.fetch_groups()
        if err is not None:
            raise RuntimeError(self._safe_error(err))

        tokens = [tok for tok in (query or "").lower().split() if tok]
        matched = []
        for g in groups:
            if only_issues and (g["down"] + g["warn"]) <= 0:
                continue
            if tokens:
                haystack = " ".join(str(g.get(f, "") or "").lower() for f in ("objid", "group", "probe"))
                if not all(tok in haystack for tok in tokens):
                    continue
            matched.append(g)

        matched.sort(key=lambda g: (-g["down"], -g["warn"], str(g.get("group", "")).lower()))
        return {
            "total_matching": len(matched),
            "returned_count": min(len(matched), limit),
            "groups": matched[:limit],
        }


class PRTGMCPServer:
    """JSON-RPC 2.0 MCP protocol dispatcher and stdio loop."""

    def __init__(self, service):
        self.service = service

    @staticmethod
    def _error_response(req_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": str(message),
            },
        }

    @staticmethod
    def _result_response(req_id, result):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result,
        }

    def handle_message(self, msg):
        """Processes a single parsed JSON-RPC object and returns a response dict or None for notifications."""
        if not isinstance(msg, dict):
            return self._error_response(None, -32600, "Invalid Request: JSON-RPC message must be an object.")

        req_id = msg.get("id")
        has_id = "id" in msg and req_id is not None
        method = msg.get("method")
        if not isinstance(method, str) or not method.strip():
            return self._error_response(req_id if has_id else None, -32600, "Invalid Request: missing method.")

        params = msg.get("params")
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            if not has_id:
                return None
            return self._error_response(req_id, -32602, "Invalid params: expected JSON object.")

        # Notifications produce no response.
        if not has_id or method.startswith("notifications/"):
            return None

        if method == "initialize":
            client_version = params.get("protocolVersion")
            negotiated = (
                client_version
                if isinstance(client_version, str) and client_version in SUPPORTED_PROTOCOL_VERSIONS
                else DEFAULT_PROTOCOL_VERSION
            )
            return self._result_response(req_id, {
                "protocolVersion": negotiated,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "Servidor MCP somente leitura para PRTG Network Monitor. "
                    "Use prtg_outages_report para perguntas como 'quais sensores caíram essa noite?', "
                    "prtg_sensor_history para 'qual o histórico desse link?', "
                    "prtg_list_alerts para alertas ativos, e prtg_search_sensors / prtg_get_sensor_details "
                    "para localizar e inspecionar sensores, dispositivos e grupos."
                ),
            })

        if method == "ping":
            return self._result_response(req_id, {})

        if method == "tools/list":
            return self._result_response(req_id, {"tools": self.service.get_tool_definitions()})

        if method == "tools/call":
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or not tool_name.strip():
                return self._error_response(req_id, -32602, "Invalid params: missing tool 'name'.")
            tool_args = params.get("arguments", {})
            try:
                payload = self.service.call_tool(tool_name, tool_args)
                return self._result_response(req_id, {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }
                    ],
                    "isError": False,
                })
            except (KeyError, ValueError, RuntimeError) as e:
                safe_msg = self.service._safe_error(e)
                return self._result_response(req_id, {
                    "content": [
                        {
                            "type": "text",
                            "text": safe_msg,
                        }
                    ],
                    "isError": True,
                })
            except Exception as e:
                safe_msg = self.service._safe_error(e)
                return self._result_response(req_id, {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Unexpected server error: {safe_msg}",
                        }
                    ],
                    "isError": True,
                })

        if method == "resources/list":
            return self._result_response(req_id, {"resources": self.service.get_resource_definitions()})

        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri.strip():
                return self._error_response(req_id, -32602, "Invalid params: missing resource 'uri'.")
            try:
                contents = self.service.read_resource(uri)
                return self._result_response(req_id, {"contents": contents})
            except KeyError as e:
                return self._error_response(req_id, -32602, str(e))
            except Exception as e:
                return self._error_response(req_id, -32603, self.service._safe_error(e))

        if method == "prompts/list":
            return self._result_response(req_id, {"prompts": self.service.get_prompt_definitions()})

        if method == "prompts/get":
            prompt_name = params.get("name")
            if not isinstance(prompt_name, str) or not prompt_name.strip():
                return self._error_response(req_id, -32602, "Invalid params: missing prompt 'name'.")
            try:
                prompt_data = self.service.get_prompt(prompt_name, params.get("arguments"))
                return self._result_response(req_id, prompt_data)
            except (KeyError, ValueError) as e:
                return self._error_response(req_id, -32602, str(e))

        return self._error_response(req_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _read_exact(stream, num_bytes):
        chunks = []
        remaining = num_bytes
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write_frame(out_stream, payload, framing_mode):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if framing_mode == "content-length":
            header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
            out_stream.write(header + encoded)
        else:
            out_stream.write(encoded + b"\n")
        out_stream.flush()

    def serve_stdio(self, in_stream=None, out_stream=None):
        """Serves MCP JSON-RPC requests over binary stdio, supporting NDJSON and Content-Length framing."""
        reader = sys.stdin.buffer if in_stream is None else in_stream
        writer = sys.stdout.buffer if out_stream is None else out_stream
        framing_mode = "ndjson"

        while True:
            line = reader.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue

            lower = stripped.lower()
            if lower.startswith(b"content-length:"):
                framing_mode = "content-length"
                try:
                    content_length = int(stripped.split(b":", 1)[1].strip())
                except ValueError:
                    self._write_frame(writer, self._error_response(None, -32700, "Invalid Content-Length header."), framing_mode)
                    continue
                if content_length <= 0 or content_length > MAX_MCP_MESSAGE_BYTES:
                    self._write_frame(writer, self._error_response(None, -32600, "Message size out of bounds."), framing_mode)
                    continue
                while True:
                    hdr = reader.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break
                raw_body = self._read_exact(reader, content_length)
                if len(raw_body) != content_length:
                    break
            else:
                framing_mode = "ndjson"
                raw_body = stripped

            try:
                parsed = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self._write_frame(writer, self._error_response(None, -32700, f"Parse error: {e}"), framing_mode)
                continue

            if isinstance(parsed, list):
                responses = []
                for item in parsed:
                    resp = self.handle_message(item)
                    if resp is not None:
                        responses.append(resp)
                if responses:
                    self._write_frame(writer, responses, framing_mode)
            else:
                resp = self.handle_message(parsed)
                if resp is not None:
                    self._write_frame(writer, resp, framing_mode)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only MCP (Model Context Protocol) stdio server for PRTG."
    )
    parser.add_argument(
        "config_pos",
        nargs="?",
        default=None,
        help="Optional path to PRTG config.json",
    )
    parser.add_argument(
        "--config",
        dest="config_opt",
        default=None,
        help="Path to PRTG config.json (overrides positional argument and PRTG_CONFIG)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit without starting the MCP server",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print the JSON catalog of read-only MCP tools and exit",
    )
    return parser.parse_args(argv)


def main(argv=None):
    prev_handler = install_stderr_logging()
    try:
        args = parse_args(argv)

        if args.list_tools:
            dummy_service = PRTGReadOnlyService({"prtg_url": "https://example.invalid", "apitoken": "unused"})
            sys.stdout.write(json.dumps({"tools": dummy_service.get_tool_definitions()}, ensure_ascii=False, indent=2) + "\n")
            sys.stdout.flush()
            return 0

        explicit_config = args.config_opt or args.config_pos
        try:
            validated_config, state_path, resolved_path = load_mcp_config(explicit_config)
        except ConfigError as e:
            sys.stderr.write(f"[ERROR] {e}\n")
            sys.stderr.flush()
            return 1

        if args.check_config:
            sys.stdout.write(
                json.dumps(
                    {
                        "ok": True,
                        "prtg_url": validated_config["prtg_url"],
                        "verify_ssl": validated_config["verify_ssl"],
                        "config_path": resolved_path,
                        "state_path": state_path,
                        "read_only": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            sys.stdout.flush()
            return 0

        service = PRTGReadOnlyService(validated_config, state_path=state_path)
        server = PRTGMCPServer(service)
        server.serve_stdio()
        return 0
    finally:
        set_log_handler(prev_handler)


if __name__ == "__main__":
    raise SystemExit(main())
