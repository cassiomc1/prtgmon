#!/usr/bin/env python3
import argparse
import collections
import curses
import json
import locale
import os
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from prtg_api import (
    HISTORY_WINDOWS,
    LOG_RANGES,
    PRTGClient,
    format_human_number,
    safe_float,
    safe_int,
    strip_html,
)
from prtg_monitor import ConfigError, PRTGMonitor, redact_secrets, set_log_handler

DEFAULT_INSTALLED_CONFIG = os.path.expanduser("~/.local/share/prtgmon/config.json")
STATE_POLL_INTERVAL_SECONDS = 2.0
UI_TICK_MS = 200

TABS = (
    ("alerts", "1:Alertas"),
    ("sensors", "2:Sensores"),
    ("devices", "3:Dispositivos"),
    ("groups", "4:Grupos"),
    ("logs", "5:Logs"),
)
INSPECTOR_VIEWS = ("chart", "details", "history")
INSPECTOR_VIEW_LABELS = {
    "chart": "1:Gráfico+Canais",
    "details": "2:Detalhes",
    "history": "3:Histórico+Logs",
}
STATUS_FILTERS = ("all", "alerts", "down", "warn", "up", "paused", "other")
STATUS_FILTER_LABELS = {
    "all": "Todos",
    "alerts": "Alertas",
    "down": "Down",
    "warn": "Warning",
    "up": "Up",
    "paused": "Pausados",
    "other": "Outros",
}
SORT_MODES = ("status", "device", "sensor", "group", "objid", "uptime")
SORT_LABELS = {
    "status": "Status",
    "device": "Dispositivo",
    "sensor": "Sensor",
    "group": "Grupo",
    "objid": "ID",
    "uptime": "Uptime",
}
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
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
PAUSED_STATUS_CODES = frozenset({7, 8, 9, 11, 12})
UP_STATUS_CODES = frozenset({2, 3})
DOWN_STATUS_CODES = frozenset({5, 13, 14})
ALERT_STATUS_CODES = frozenset({4, 5, 10, 13, 14})

_BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


def resolve_config_path(explicit_path=None):
    """Resolves config path from explicit CLI arg, ./config.json, or ~/.local/share/prtgmon/config.json."""
    if explicit_path:
        return os.path.expanduser(explicit_path)
    if os.path.exists("config.json"):
        return "config.json"
    if os.path.exists(DEFAULT_INSTALLED_CONFIG):
        return DEFAULT_INSTALLED_CONFIG
    return "config.json"


def _parse_iso_utc_to_epoch(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def load_state_snapshot(state_path):
    """Read-only load of persisted state.json returning (alerts, updated_at, error)."""
    if not state_path:
        return [], None, "Persistência de estado desabilitada (state_file=null)."
    if not os.path.exists(state_path):
        return [], None, f"Arquivo de estado não encontrado: {state_path}"

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("raiz do estado deve ser um objeto JSON")

        raw_alerts = data.get("active_alerts")
        if not isinstance(raw_alerts, dict):
            raise ValueError("active_alerts ausente ou inválido")

        alerts = []
        for key, entry in raw_alerts.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(entry, dict):
                raise ValueError("entrada de alerta inválida")
            objid = str(entry.get("objid") or key).strip()
            status_raw = entry.get("status_raw", 0)
            if isinstance(status_raw, bool) or not isinstance(status_raw, int):
                status_raw = 0
            alerts.append({
                "objid": objid,
                "sensor": str(entry.get("sensor", "")),
                "device": str(entry.get("device", "")),
                "status": str(entry.get("status", "")),
                "status_raw": status_raw,
                "message": str(entry.get("message", "")),
                "lastvalue": str(entry.get("lastvalue", "")),
            })

        updated_at = _parse_iso_utc_to_epoch(data.get("updated_at"))
        if updated_at is None:
            updated_at = os.path.getmtime(state_path)
        return alerts, updated_at, None
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return None, None, f"Falha ao ler estado: {e}"


def summarize_alerts(alerts):
    """Counts total, Down (5), Down Partial (14), and other alert statuses."""
    total = len(alerts)
    down = sum(1 for a in alerts if a.get("status_raw") == 5)
    partial = sum(1 for a in alerts if a.get("status_raw") == 14)
    other = total - down - partial
    return {
        "total": total,
        "down": down,
        "partial": partial,
        "other": other,
    }


def summarize_all_sensors(sensors):
    """Counts total and per-status breakdown across all PRTG sensors."""
    total = len(sensors)
    down = 0
    partial = 0
    ack = 0
    warn = 0
    up = 0
    paused = 0
    unusual = 0
    for s in sensors:
        raw = s.get("status_raw", 0)
        if raw == 5:
            down += 1
        elif raw == 14:
            partial += 1
        elif raw == 13:
            ack += 1
        elif raw == 4:
            warn += 1
        elif raw in UP_STATUS_CODES:
            up += 1
        elif raw in PAUSED_STATUS_CODES:
            paused += 1
        elif raw == 10:
            unusual += 1
    known = down + partial + ack + warn + up + paused + unusual
    return {
        "total": total,
        "down": down,
        "partial": partial,
        "ack": ack,
        "warn": warn,
        "up": up,
        "paused": paused,
        "unusual": unusual,
        "other": max(0, total - known),
    }


def sensor_matches_status_filter(sensor, status_filter):
    raw = sensor.get("status_raw", 0)
    if status_filter == "all":
        return True
    if status_filter == "alerts":
        return raw in ALERT_STATUS_CODES
    if status_filter == "down":
        return raw in DOWN_STATUS_CODES
    if status_filter == "warn":
        return raw == 4
    if status_filter == "up":
        return raw in UP_STATUS_CODES
    if status_filter == "paused":
        return raw in PAUSED_STATUS_CODES
    if status_filter == "other":
        return raw not in (UP_STATUS_CODES | DOWN_STATUS_CODES | PAUSED_STATUS_CODES | {4})
    return True


def _objid_sort_key(objid):
    text = str(objid or "").strip()
    if text.isdigit():
        return (0, int(text), text)
    return (1, 0, text)


def _uptime_sort_value(sensor):
    raw = str(sensor.get("uptime", "")).replace("%", "").strip()
    val = safe_float(raw)
    return (0, val) if val is not None else (1, 0.0)


def filter_and_sort_alerts(alerts, query="", sort_by="status"):
    """Filters sensors/alerts case-insensitively across fields and sorts deterministically."""
    q = (query or "").strip().lower()
    if q:
        filtered = [
            a for a in alerts
            if q in str(a.get("objid", "")).lower()
            or q in str(a.get("probe", "")).lower()
            or q in str(a.get("group", "")).lower()
            or q in str(a.get("device", "")).lower()
            or q in str(a.get("sensor", "")).lower()
            or q in str(a.get("status", "")).lower()
            or q in str(a.get("message", "")).lower()
            or q in str(a.get("lastvalue", "")).lower()
            or q in str(a.get("tags", "")).lower()
        ]
    else:
        filtered = list(alerts)

    def key_fn(a):
        device = str(a.get("device", "")).lower()
        sensor = str(a.get("sensor", "")).lower()
        group = str(a.get("group", "")).lower()
        objid_key = _objid_sort_key(a.get("objid", ""))
        if sort_by == "device":
            return (device, sensor, objid_key)
        if sort_by == "sensor":
            return (sensor, device, objid_key)
        if sort_by == "group":
            return (group, device, sensor, objid_key)
        if sort_by == "objid":
            return (objid_key,)
        if sort_by == "uptime":
            return (_uptime_sort_value(a), device, sensor, objid_key)
        prio = STATUS_PRIORITY.get(a.get("status_raw", 0), 50)
        return (prio, device, sensor, objid_key)

    return sorted(filtered, key=key_fn)


def filter_devices(devices, query=""):
    q = (query or "").strip().lower()
    if not q:
        items = list(devices)
    else:
        items = [
            d for d in devices
            if q in str(d.get("objid", "")).lower()
            or q in str(d.get("device", "")).lower()
            or q in str(d.get("host", "")).lower()
            or q in str(d.get("group", "")).lower()
            or q in str(d.get("probe", "")).lower()
            or q in str(d.get("tags", "")).lower()
        ]
    return sorted(
        items,
        key=lambda d: (
            -(d.get("down", 0) + d.get("partial", 0)),
            -d.get("warn", 0),
            str(d.get("device", "")).lower(),
            _objid_sort_key(d.get("objid", "")),
        ),
    )


def filter_groups(groups, query=""):
    q = (query or "").strip().lower()
    if not q:
        items = list(groups)
    else:
        items = [
            g for g in groups
            if q in str(g.get("objid", "")).lower()
            or q in str(g.get("group", "")).lower()
            or q in str(g.get("probe", "")).lower()
        ]
    return sorted(
        items,
        key=lambda g: (
            -g.get("down", 0),
            -g.get("warn", 0),
            str(g.get("group", "")).lower(),
            _objid_sort_key(g.get("objid", "")),
        ),
    )


def filter_messages(messages, query=""):
    q = (query or "").strip().lower()
    if not q:
        return list(messages)
    return [
        m for m in messages
        if q in str(m.get("datetime", "")).lower()
        or q in str(m.get("status", "")).lower()
        or q in str(m.get("name", "")).lower()
        or q in str(m.get("parent", "")).lower()
        or q in str(m.get("type", "")).lower()
        or q in str(m.get("message", "")).lower()
        or q in str(m.get("objid", "")).lower()
    ]


def render_bar(fraction, width):
    """Renders a proportional filled bar using Unicode block characters."""
    width = max(1, int(width))
    clamped = max(0.0, min(1.0, float(fraction)))
    filled = int(round(clamped * width))
    return "█" * filled + "░" * (width - filled)


def _draw_line_on_bitmap(grid, x0, y0, x1, y1, pixel_w, pixel_h):
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        if 0 <= x0 < pixel_w and 0 <= y0 < pixel_h:
            grid[y0 // 4][x0 // 2] |= _BRAILLE_BITS[y0 % 4][x0 % 2]
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy


def render_braille_chart(values, width, height):
    """Renders a high-resolution Unicode Braille line chart returning (lines, y_min, y_max)."""
    width = max(4, int(width))
    height = max(2, int(height))
    pixel_w = width * 2
    pixel_h = height * 4
    grid = [[0] * width for _ in range(height)]

    numeric = [safe_float(v) for v in (values or [])]
    valid = [v for v in numeric if v is not None]
    if not valid:
        return [" " * width for _ in range(height)], None, None

    y_min = min(valid)
    y_max = max(valid)
    span = y_max - y_min

    def to_py(val):
        if span == 0:
            return pixel_h // 2
        norm = (val - y_min) / span
        return max(0, min(pixel_h - 1, int(round((1.0 - norm) * (pixel_h - 1)))))

    n = len(numeric)
    prev_pt = None
    for idx, val in enumerate(numeric):
        if val is None:
            prev_pt = None
            continue
        if n == 1:
            px = pixel_w // 2
        else:
            px = int(round(idx * (pixel_w - 1) / (n - 1)))
        py = to_py(val)
        if prev_pt is not None:
            _draw_line_on_bitmap(grid, prev_pt[0], prev_pt[1], px, py, pixel_w, pixel_h)
        else:
            if 0 <= px < pixel_w and 0 <= py < pixel_h:
                grid[py // 4][px // 2] |= _BRAILLE_BITS[py % 4][px % 2]
        prev_pt = (px, py)

    lines = [
        "".join(chr(0x2800 + cell) for cell in row)
        for row in grid
    ]
    return lines, y_min, y_max


def build_sensor_url(prtg_url, objid):
    base = str(prtg_url or "").rstrip("/")
    return f"{base}/sensor.htm?id={urllib.parse.quote(str(objid), safe='')}"


def build_object_url(prtg_url, tab, item):
    base = str(prtg_url or "").rstrip("/")
    objid = urllib.parse.quote(str(item.get("objid", "")), safe="")
    if tab == "devices":
        return f"{base}/device.htm?id={objid}"
    if tab == "groups":
        return f"{base}/group.htm?id={objid}"
    return f"{base}/sensor.htm?id={objid}"


def open_sensor_url(url):
    """Safely opens an HTTP/HTTPS PRTG URL using macOS open."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    try:
        subprocess.Popen(
            ["open", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def sanitize_line(text):
    if text is None:
        return ""
    cleaned = str(text).replace("\x00", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return " ".join(cleaned.split())


def truncate_text(text, max_len):
    if max_len <= 0:
        return ""
    text = str(text or "")
    if len(text) <= max_len:
        return text
    if max_len == 1:
        return "…"
    return text[: max_len - 1] + "…"


def format_timestamp_local(epoch_ts):
    if epoch_ts is None:
        return "--:--:--"
    try:
        return datetime.fromtimestamp(epoch_ts).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "--:--:--"


def format_age(seconds):
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class TUIState:
    """Thread-safe presentation and data state for the full PRTG Explorer TUI."""

    def __init__(
        self,
        mode="live",
        prtg_url="",
        config_path="",
        state_path=None,
        refresh_interval=60.0,
        state_interval=STATE_POLL_INTERVAL_SECONDS,
        alert_statuses=None,
    ):
        self.mode = mode if mode in ("live", "state") else "live"
        self.prtg_url = str(prtg_url or "")
        self.config_path = str(config_path or "")
        self.state_path = state_path
        self.refresh_interval = float(refresh_interval)
        self.state_interval = float(state_interval)
        self.alert_statuses = frozenset(alert_statuses or (5, 14))

        self.tab_index = 0
        self.alerts = []
        self.all_sensors = []
        self.devices = []
        self.groups = []
        self.messages = []
        self.server_status = {}
        self.live_loaded = False

        self.selected_objid = None
        self.selected_ids_by_tab = {}
        self.last_selected_idx_by_tab = {}
        self.scroll_by_tab = {}
        self.sort_index = 0
        self.status_filter_index = 0
        self.device_filter = None
        self.group_filter = None
        self.log_range_index = 1  # default 7days

        self.filter_query = ""
        self.filter_draft = ""
        self.filter_prev = ""
        self.filter_input_active = False
        self.show_help = False
        self.is_refreshing = False
        self.last_updated = None
        self.next_refresh_at = 0.0
        self.last_error = None
        self.status_notice = None
        self.status_notice_until = 0.0
        self.recent_logs = collections.deque(maxlen=50)

        # Sensor Inspector state
        self.inspector_open = False
        self.inspector_sensor = None
        self.inspector_view_index = 0
        self.inspector_window_index = 2  # default 24h
        self.inspector_channel_index = 0
        self.inspector_loading = False
        self.inspector_error = None
        self.inspector_details = None
        self.inspector_channels = []
        self.inspector_historic = None
        self.inspector_messages = []
        self.inspector_scroll = 0

        self.lock = threading.Lock()

    @property
    def scroll_offset(self):
        return self.scroll_by_tab.get(self.current_tab_locked(), 0)

    @scroll_offset.setter
    def scroll_offset(self, value):
        self.scroll_by_tab[self.current_tab_locked()] = max(0, int(value))

    def active_interval(self):
        return self.state_interval if self.mode == "state" else self.refresh_interval

    def current_tab_locked(self):
        return TABS[self.tab_index % len(TABS)][0]

    def current_tab(self):
        with self.lock:
            return self.current_tab_locked()

    def current_sort_locked(self):
        return SORT_MODES[self.sort_index % len(SORT_MODES)]

    def current_sort(self):
        with self.lock:
            return self.current_sort_locked()

    def current_status_filter_locked(self):
        return STATUS_FILTERS[self.status_filter_index % len(STATUS_FILTERS)]

    def current_log_range_locked(self):
        return LOG_RANGES[self.log_range_index % len(LOG_RANGES)][0]

    def current_inspector_view_locked(self):
        return INSPECTOR_VIEWS[self.inspector_view_index % len(INSPECTOR_VIEWS)]

    def current_inspector_window_locked(self):
        return HISTORY_WINDOWS[self.inspector_window_index % len(HISTORY_WINDOWS)][0]

    def _item_key(self, item, idx):
        objid = str(item.get("objid", "")).strip()
        if objid and self.current_tab_locked() != "logs":
            return objid
        return f"{objid}|{item.get('datetime', '')}|{idx}"

    def visible_items_locked(self, tab=None):
        tab = tab or self.current_tab_locked()
        if tab == "alerts":
            return filter_and_sort_alerts(self.alerts, self.filter_query, self.current_sort_locked())
        if tab == "sensors":
            source = self.all_sensors if (self.mode == "live" and self.live_loaded) else self.alerts
            if self.device_filter:
                source = [s for s in source if s.get("device") == self.device_filter]
            if self.group_filter:
                source = [s for s in source if s.get("group") == self.group_filter]
            status_f = self.current_status_filter_locked()
            if status_f != "all":
                source = [s for s in source if sensor_matches_status_filter(s, status_f)]
            return filter_and_sort_alerts(source, self.filter_query, self.current_sort_locked())
        if tab == "devices":
            return filter_devices(self.devices, self.filter_query)
        if tab == "groups":
            return filter_groups(self.groups, self.filter_query)
        if tab == "logs":
            return filter_messages(self.messages, self.filter_query)
        return []

    def visible_alerts_locked(self):
        return self.visible_items_locked()

    def visible_alerts(self):
        with self.lock:
            return self.visible_items_locked()

    def _selected_index_locked(self, visible, tab=None):
        tab = tab or self.current_tab_locked()
        if not visible:
            self.selected_ids_by_tab[tab] = None
            self.last_selected_idx_by_tab[tab] = 0
            if tab == self.current_tab_locked():
                self.selected_objid = None
            return 0

        wanted = self.selected_ids_by_tab.get(tab)
        if wanted is None and tab == "alerts":
            wanted = self.selected_objid

        if wanted is not None:
            for idx, item in enumerate(visible):
                key = str(item.get("objid", "")) if tab != "logs" else self._item_key(item, idx)
                if key == wanted:
                    self.last_selected_idx_by_tab[tab] = idx
                    if tab == self.current_tab_locked():
                        self.selected_objid = key
                    return idx

        last_idx = self.last_selected_idx_by_tab.get(tab, 0)
        clamped = max(0, min(last_idx, len(visible) - 1))
        self.last_selected_idx_by_tab[tab] = clamped
        key = str(visible[clamped].get("objid", "")) if tab != "logs" else self._item_key(visible[clamped], clamped)
        self.selected_ids_by_tab[tab] = key
        if tab == self.current_tab_locked():
            self.selected_objid = key
        return clamped

    def selected_alert(self):
        with self.lock:
            visible = self.visible_items_locked()
            if not visible:
                return None
            idx = self._selected_index_locked(visible)
            return dict(visible[idx])

    def move_selection(self, delta):
        with self.lock:
            tab = self.current_tab_locked()
            visible = self.visible_items_locked(tab)
            if not visible:
                self.selected_ids_by_tab[tab] = None
                self.last_selected_idx_by_tab[tab] = 0
                self.selected_objid = None
                return
            current_idx = self._selected_index_locked(visible, tab)
            next_idx = max(0, min(len(visible) - 1, current_idx + delta))
            self.last_selected_idx_by_tab[tab] = next_idx
            key = str(visible[next_idx].get("objid", "")) if tab != "logs" else self._item_key(visible[next_idx], next_idx)
            self.selected_ids_by_tab[tab] = key
            self.selected_objid = key

    def select_first(self):
        with self.lock:
            tab = self.current_tab_locked()
            visible = self.visible_items_locked(tab)
            if visible:
                self.last_selected_idx_by_tab[tab] = 0
                key = str(visible[0].get("objid", "")) if tab != "logs" else self._item_key(visible[0], 0)
                self.selected_ids_by_tab[tab] = key
                self.selected_objid = key

    def select_last(self):
        with self.lock:
            tab = self.current_tab_locked()
            visible = self.visible_items_locked(tab)
            if visible:
                last = len(visible) - 1
                self.last_selected_idx_by_tab[tab] = last
                key = str(visible[last].get("objid", "")) if tab != "logs" else self._item_key(visible[last], last)
                self.selected_ids_by_tab[tab] = key
                self.selected_objid = key

    def set_tab(self, tab_index):
        with self.lock:
            self.tab_index = tab_index % len(TABS)
            tab = self.current_tab_locked()
            visible = self.visible_items_locked(tab)
            self._selected_index_locked(visible, tab)
            return tab

    def cycle_sort(self):
        with self.lock:
            self.sort_index = (self.sort_index + 1) % len(SORT_MODES)
            return self.current_sort_locked()

    def cycle_status_filter(self):
        with self.lock:
            self.status_filter_index = (self.status_filter_index + 1) % len(STATUS_FILTERS)
            self.scroll_by_tab["sensors"] = 0
            return self.current_status_filter_locked()

    def cycle_log_range(self):
        with self.lock:
            self.log_range_index = (self.log_range_index + 1) % len(LOG_RANGES)
            return LOG_RANGES[self.log_range_index]

    def toggle_mode(self):
        with self.lock:
            self.mode = "state" if self.mode == "live" else "live"
            self.last_error = None
            self.next_refresh_at = 0.0
            return self.mode

    def set_notice(self, text, duration=4.0):
        with self.lock:
            self.status_notice = str(text)
            self.status_notice_until = time.monotonic() + duration

    def open_inspector(self, sensor):
        with self.lock:
            self.inspector_open = True
            self.inspector_sensor = dict(sensor)
            self.inspector_view_index = 0
            self.inspector_channel_index = 0
            self.inspector_scroll = 0
            self.inspector_error = None
            self.inspector_details = None
            self.inspector_channels = []
            self.inspector_historic = None
            self.inspector_messages = []
            self.inspector_loading = self.mode == "live"

    def close_inspector(self):
        with self.lock:
            self.inspector_open = False

    def apply_inspector_data(
        self,
        objid,
        details=None,
        channels=None,
        historic=None,
        messages=None,
        error=None,
    ):
        with self.lock:
            if not self.inspector_sensor or str(self.inspector_sensor.get("objid")) != str(objid):
                return
            if details is not None:
                self.inspector_details = details
            if channels is not None:
                self.inspector_channels = list(channels)
            if historic is not None:
                self.inspector_historic = historic
                ch_count = len(historic.get("channels", []))
                if ch_count > 0:
                    self.inspector_channel_index %= ch_count
            if messages is not None:
                self.inspector_messages = list(messages)
            self.inspector_error = error
            self.inspector_loading = False

    def apply_snapshot(self, alerts, updated_at=None, error=None, expected_mode=None):
        with self.lock:
            if expected_mode is not None and self.mode != expected_mode:
                self.is_refreshing = False
                return
            if alerts is not None:
                self.alerts = list(alerts)
                if updated_at is not None:
                    self.last_updated = updated_at
                visible = self.visible_items_locked()
                self._selected_index_locked(visible)
            self.last_error = error
            self.is_refreshing = False
            self.next_refresh_at = time.monotonic() + self.active_interval()

    def apply_explorer_snapshot(
        self,
        all_sensors=None,
        devices=None,
        groups=None,
        messages=None,
        server_status=None,
        updated_at=None,
        error=None,
        expected_mode=None,
    ):
        with self.lock:
            if expected_mode is not None and self.mode != expected_mode:
                self.is_refreshing = False
                return
            if all_sensors is not None:
                self.all_sensors = list(all_sensors)
                self.live_loaded = True
                target_statuses = set(self.alert_statuses) | ALERT_STATUS_CODES
                self.alerts = [
                    s for s in self.all_sensors
                    if s.get("status_raw", 0) in target_statuses
                ]
            if devices is not None:
                self.devices = list(devices)
            if groups is not None:
                self.groups = list(groups)
            if messages is not None:
                self.messages = list(messages)
            if server_status is not None:
                self.server_status = dict(server_status)
            if updated_at is not None and (all_sensors is not None or devices is not None or messages is not None):
                self.last_updated = updated_at
            tab = self.current_tab_locked()
            visible = self.visible_items_locked(tab)
            self._selected_index_locked(visible, tab)
            self.last_error = error
            self.is_refreshing = False
            self.next_refresh_at = time.monotonic() + self.active_interval()

    def update_scroll(self, scroll_offset, tab=None):
        with self.lock:
            key = tab or self.current_tab_locked()
            self.scroll_by_tab[key] = max(0, int(scroll_offset))

    def snapshot_for_render(self, now_mono=None):
        now_mono = time.monotonic() if now_mono is None else now_mono
        with self.lock:
            tab = self.current_tab_locked()
            visible = self.visible_items_locked(tab)
            selected_idx = self._selected_index_locked(visible, tab)
            selected = dict(visible[selected_idx]) if visible else None
            notice = self.status_notice if now_mono < self.status_notice_until else None
            return {
                "mode": self.mode,
                "tab": tab,
                "tab_index": self.tab_index,
                "prtg_url": self.prtg_url,
                "config_path": self.config_path,
                "state_path": self.state_path,
                "all_alerts": list(self.alerts),
                "all_sensors": list(self.all_sensors),
                "live_loaded": self.live_loaded,
                "server_status": dict(self.server_status),
                "visible": visible,
                "selected_idx": selected_idx,
                "selected": selected,
                "scroll_offset": self.scroll_by_tab.get(tab, 0),
                "sort_key": self.current_sort_locked(),
                "status_filter": self.current_status_filter_locked(),
                "device_filter": self.device_filter,
                "group_filter": self.group_filter,
                "log_range": self.current_log_range_locked(),
                "filter_query": self.filter_query,
                "filter_draft": self.filter_draft,
                "filter_input_active": self.filter_input_active,
                "show_help": self.show_help,
                "is_refreshing": self.is_refreshing,
                "last_updated": self.last_updated,
                "next_refresh_at": self.next_refresh_at,
                "last_error": self.last_error,
                "notice": notice,
                "inspector_open": self.inspector_open,
                "inspector_sensor": dict(self.inspector_sensor) if self.inspector_sensor else None,
                "inspector_view": self.current_inspector_view_locked(),
                "inspector_window": self.current_inspector_window_locked(),
                "inspector_channel_index": self.inspector_channel_index,
                "inspector_loading": self.inspector_loading,
                "inspector_error": self.inspector_error,
                "inspector_details": dict(self.inspector_details) if self.inspector_details else None,
                "inspector_channels": list(self.inspector_channels),
                "inspector_historic": self.inspector_historic,
                "inspector_messages": list(self.inspector_messages),
                "inspector_scroll": self.inspector_scroll,
            }


class TUIRunner:
    """Coordinates read-only PRTGClient exploration, monitor fallback, and state.json polling."""

    def __init__(self, monitor, state, client=None):
        self.monitor = monitor
        self.state = state
        self.client = client if client is not None else PRTGClient.from_config(monitor.config)
        self._capture_lock = threading.Lock()
        self._active_fetch_errors = None

    def log_sink(self, message, level="info"):
        token = self.monitor.config.get("apitoken", "")
        clean = redact_secrets(message, token)
        with self.state.lock:
            self.state.recent_logs.append((datetime.now().strftime("%H:%M:%S"), level, clean))
        if level in ("error", "warning"):
            with self._capture_lock:
                if self._active_fetch_errors is not None:
                    self._active_fetch_errors.append(clean)

    def prime_from_state(self):
        if not self.monitor.state_path or not os.path.exists(self.monitor.state_path):
            return
        alerts, updated_at, error = load_state_snapshot(self.monitor.state_path)
        if alerts is not None:
            self.state.apply_snapshot(alerts, updated_at=updated_at, error=None)
        elif error:
            self.state.apply_snapshot(None, error=error)

    def trigger_refresh(self, force=False):
        with self.state.lock:
            mode = self.state.mode
            if mode == "live" and self.state.is_refreshing:
                return False
            if mode == "live":
                self.state.is_refreshing = True
            log_range = self.state.current_log_range_locked()

        if mode == "state":
            alerts, updated_at, error = load_state_snapshot(self.state.state_path)
            self.state.apply_snapshot(alerts, updated_at=updated_at, error=error, expected_mode="state")
            if force:
                self.state.set_notice("Estado recarregado de state.json.", duration=2.5)
            return True

        thread = threading.Thread(target=self._run_live_fetch, args=(log_range,), daemon=True)
        thread.start()
        return True

    def trigger_inspector_refresh(self, objid=None, window_key=None, history_only=False):
        with self.state.lock:
            if self.state.mode != "live":
                self.state.inspector_loading = False
                return False
            if objid is None and self.state.inspector_sensor:
                objid = self.state.inspector_sensor.get("objid")
            if not objid:
                return False
            if window_key is None:
                window_key = self.state.current_inspector_window_locked()
            self.state.inspector_loading = True
            self.state.inspector_error = None

        thread = threading.Thread(
            target=self._run_inspector_fetch,
            args=(str(objid), window_key, history_only),
            daemon=True,
        )
        thread.start()
        return True

    @staticmethod
    def _run_parallel(calls):
        """Runs (fn, args) callables in daemon threads so quitting never blocks atexit."""
        results = [None] * len(calls)
        errors = [None] * len(calls)

        def worker(idx, fn, args):
            try:
                results[idx] = fn(*args)
            except Exception as exc:
                errors[idx] = exc

        threads = []
        for idx, (fn, args) in enumerate(calls):
            t = threading.Thread(target=worker, args=(idx, fn, args), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        for exc in errors:
            if exc is not None:
                raise exc
        return results

    def _run_live_fetch(self, log_range="7days"):
        with self._capture_lock:
            captured = []
            self._active_fetch_errors = captured
        try:
            if not hasattr(self.client, "fetch_all_sensors"):
                sensors = self.monitor.fetch_active_errors()
                if sensors is None:
                    err_msg = captured[-1] if captured else "Falha ao consultar API do PRTG."
                    self.state.apply_snapshot(None, error=err_msg, expected_mode="live")
                else:
                    self.state.apply_snapshot(sensors, updated_at=time.time(), error=None, expected_mode="live")
                return

            results = self._run_parallel([
                (self.client.fetch_all_sensors, ()),
                (self.client.fetch_devices, ()),
                (self.client.fetch_groups, ()),
                (self.client.fetch_messages, (None, log_range, 150)),
                (self.client.fetch_server_status, ()),
            ])
            all_sensors, err_s = results[0]
            devices, err_d = results[1]
            groups, err_g = results[2]
            messages, err_m = results[3]
            server_status, _ = results[4]

            errors = [e for e in (err_s, err_d, err_g, err_m) if e]
            error_msg = errors[0] if errors else None
            self.state.apply_explorer_snapshot(
                all_sensors=all_sensors,
                devices=devices,
                groups=groups,
                messages=messages,
                server_status=server_status,
                updated_at=time.time(),
                error=error_msg,
                expected_mode="live",
            )
        except Exception as e:
            token = self.monitor.config.get("apitoken", "")
            self.state.apply_explorer_snapshot(
                error=redact_secrets(e, token),
                expected_mode="live",
            )
        finally:
            with self._capture_lock:
                self._active_fetch_errors = None

    def _run_inspector_fetch(self, objid, window_key, history_only=False):
        try:
            if history_only:
                historic, err_h = self.client.fetch_historic_data(objid, window_key=window_key)
                self.state.apply_inspector_data(objid, historic=historic, error=err_h)
                return

            results = self._run_parallel([
                (self.client.fetch_sensor_details, (objid,)),
                (self.client.fetch_sensor_channels, (objid,)),
                (self.client.fetch_historic_data, (objid, window_key)),
                (self.client.fetch_messages, (objid, "30days", 80)),
            ])
            details, err_d = results[0]
            channels, err_c = results[1]
            historic, err_h = results[2]
            messages, err_m = results[3]

            errors = [e for e in (err_d, err_c, err_h, err_m) if e]
            self.state.apply_inspector_data(
                objid,
                details=details,
                channels=channels,
                historic=historic,
                messages=messages,
                error=errors[0] if errors else None,
            )
        except Exception as e:
            token = self.monitor.config.get("apitoken", "")
            self.state.apply_inspector_data(objid, error=redact_secrets(e, token))


def init_colors(use_color=True):
    palette = {
        "normal": 0,
        "red": 0,
        "yellow": 0,
        "green": 0,
        "cyan": 0,
        "magenta": 0,
        "dim": 0,
        "bold": curses.A_BOLD,
        "reverse": curses.A_REVERSE,
    }
    if not use_color:
        return palette
    try:
        if not curses.has_colors():
            return palette
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_RED, bg)
        curses.init_pair(2, curses.COLOR_YELLOW, bg)
        curses.init_pair(3, curses.COLOR_GREEN, bg)
        curses.init_pair(4, curses.COLOR_CYAN, bg)
        curses.init_pair(5, curses.COLOR_MAGENTA, bg)
        palette["red"] = curses.color_pair(1) | curses.A_BOLD
        palette["yellow"] = curses.color_pair(2) | curses.A_BOLD
        palette["green"] = curses.color_pair(3) | curses.A_BOLD
        palette["cyan"] = curses.color_pair(4)
        palette["magenta"] = curses.color_pair(5) | curses.A_BOLD
        palette["dim"] = getattr(curses, "A_DIM", 0)
    except curses.error:
        pass
    return palette


def safe_addstr(win, y, x, text, attr=0):
    height, width = win.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    max_chars = width - x - (1 if y == height - 1 else 0)
    if max_chars <= 0:
        return
    clean = str(text or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    clipped = truncate_text(clean, max_chars) if len(clean) > max_chars else clean
    try:
        win.addstr(y, x, clipped, attr)
    except curses.error:
        pass


def draw_box(win, top, left, height, width, title="", border_attr=0, title_attr=0):
    if height < 2 or width < 2:
        return
    inner_w = width - 2
    if title:
        label = f" {title} "
        if len(label) > inner_w:
            label = truncate_text(label, inner_w)
        top_line = "╭" + label + "─" * (inner_w - len(label)) + "╮"
    else:
        top_line = "╭" + "─" * inner_w + "╮"
    safe_addstr(win, top, left, top_line, border_attr)
    if title and title_attr:
        safe_addstr(win, top, left + 1, truncate_text(f" {title} ", inner_w), title_attr)

    for row in range(top + 1, top + height - 1):
        safe_addstr(win, row, left, "│", border_attr)
        safe_addstr(win, row, left + width - 1, "│", border_attr)

    bottom_line = "╰" + "─" * inner_w + "╯"
    safe_addstr(win, top + height - 1, left, bottom_line, border_attr)


def _status_badge(status_raw, colors):
    if status_raw == 5:
        return "●", colors["red"]
    if status_raw == 14:
        return "◐", colors["yellow"]
    if status_raw == 13:
        return "◆", colors["magenta"]
    if status_raw == 4:
        return "▲", colors["yellow"]
    if status_raw in UP_STATUS_CODES:
        return "●", colors["green"]
    if status_raw in PAUSED_STATUS_CODES:
        return "⏸", colors["dim"]
    if status_raw == 10:
        return "◈", colors["magenta"]
    return "•", colors["cyan"]


def _render_help_modal(win, snap, height, width, colors):
    if snap["inspector_open"]:
        lines = [
            "Atalhos no Inspetor do Sensor:",
            "  v  ou  1 / 2 / 3      Alternar visão (Gráfico / Detalhes / Histórico)",
            "  n / p  ou  ← / →      Próximo / anterior canal no gráfico",
            "  t                     Alternar janela do gráfico (1h/2h/24h/7d/30d)",
            "  ↑ / ↓  ou  j / k      Rolar detalhes/histórico",
            "  r                     Atualizar sensor agora",
            "  o                     Abrir sensor no navegador",
            "  Esc  ou  b            Voltar para a lista",
            "  q                     Sair do programa",
        ]
    else:
        lines = [
            "Atalhos do PRTG Explorer TUI:",
            "  1..5  ou  Tab         Trocar aba (Alertas/Sensores/Disp./Grupos/Logs)",
            "  ↑ / ↓  ou  j / k      Mover seleção",
            "  PgUp / PgDn / g / G   Página / Início / Fim",
            "  Enter                 Inspecionar sensor (ou filtrar disp./grupo)",
            "  /                     Buscar por nome, dispositivo, tag, mensagem",
            "  c                     Limpar busca e filtros",
            "  f                     Filtrar por status (na aba Sensores)",
            "  s                     Alternar ordenação (Status/Disp./Sensor/ID/Uptime)",
            "  t                     Alternar período dos Logs (Hoje / 7d / 30d)",
            "  r                     Atualizar agora",
            "  m                     Alternar modo (LIVE API / STATE json)",
            "  o                     Abrir item selecionado no navegador",
            "  ?                     Mostrar/fechar ajuda   |   q: Sair",
        ]
    lines.extend([
        "",
        f"Modo atual : {snap['mode'].upper()}   |   Aba: {snap['tab'].upper()}",
        f"Config     : {snap['config_path']}",
        f"Estado     : {snap['state_path'] or '(desativado)'}",
        "Pressione Esc, ? ou Enter para fechar.",
    ])

    box_w = min(width - 4, max(len(line) for line in lines) + 4)
    box_h = min(height - 2, len(lines) + 2)
    if box_w < 24 or box_h < 6:
        return
    top = max(0, (height - box_h) // 2)
    left = max(0, (width - box_w) // 2)

    blank = " " * (box_w - 2)
    for r in range(top + 1, top + box_h - 1):
        safe_addstr(win, r, left + 1, blank, colors["normal"])

    draw_box(
        win,
        top,
        left,
        box_h,
        box_w,
        title="Ajuda - PRTG Monitor TUI",
        border_attr=colors["cyan"],
        title_attr=colors["cyan"] | colors["bold"],
    )
    for idx, line in enumerate(lines[: box_h - 2]):
        attr = colors["bold"] if idx == 0 else colors["normal"]
        safe_addstr(win, top + 1 + idx, left + 2, truncate_text(line, box_w - 4), attr)


def _render_inspector(win, snap, height, width, now_mono, spinner_idx, colors):
    sensor = snap["inspector_sensor"] or {}
    details = snap["inspector_details"] or {}
    objid = sensor.get("objid", details.get("objid", ""))
    name = details.get("name") or sensor.get("sensor", "")
    device = details.get("parentdevicename") or sensor.get("device", "")
    group = details.get("parentgroupname") or sensor.get("group", "")
    probe = details.get("probename") or sensor.get("probe", "")
    status_txt = details.get("statustext") or sensor.get("status", "")
    status_raw = details.get("statusid") or sensor.get("status_raw", 0)
    dot, dot_attr = _status_badge(status_raw, colors)

    # 1. Top Sensor Summary Box (5 rows)
    top_h = 5
    title = f"Sensor #{objid}: {truncate_text(sanitize_line(name), max(12, width // 2))}"
    draw_box(win, 0, 0, top_h, width, title=title, border_attr=colors["cyan"], title_attr=colors["bold"])

    if snap["inspector_loading"]:
        spin = SPINNER_FRAMES[spinner_idx % len(SPINNER_FRAMES)]
        safe_addstr(win, 0, max(2, width - 20), f" {spin} CARREGANDO... ", colors["yellow"])

    path_str = " ➔ ".join(p for p in (probe, group, device) if p) or "-"
    safe_addstr(win, 1, 2, f"{dot} {status_txt}", dot_attr)
    info_x = 2 + len(f"{dot} {status_txt}") + 3
    if info_x < width - 10:
        stype = details.get("sensortype") or ""
        interval = f" | Intervalo: {details['interval']}s" if details.get("interval") else ""
        meta = f"Caminho: {path_str}" + (f" | Tipo: {stype}" if stype else "") + interval
        safe_addstr(win, 1, info_x, truncate_text(meta, width - info_x - 2), colors["normal"])

    uptime = details.get("uptime") or sensor.get("uptime") or "-"
    downtime = details.get("downtime") or sensor.get("downtime") or "-"
    lastcheck = details.get("lastcheck") or sensor.get("lastcheck") or "-"
    lastval = details.get("lastvalue") or sensor.get("lastvalue") or "-"
    row2 = f"Último valor: {lastval} | Uptime: {uptime} | Downtime: {downtime} | Últ. Check: {lastcheck}"
    safe_addstr(win, 2, 2, truncate_text(row2, width - 4), colors["normal"])

    # View tabs on row 3
    vx = 2
    for view_key in INSPECTOR_VIEWS:
        lbl = f"[{INSPECTOR_VIEW_LABELS[view_key]}]" if view_key == snap["inspector_view"] else f" {INSPECTOR_VIEW_LABELS[view_key]} "
        attr = (colors["cyan"] | colors["bold"] | colors["reverse"]) if view_key == snap["inspector_view"] else colors["dim"]
        safe_addstr(win, 3, vx, lbl, attr)
        vx += len(lbl) + 2
    win_lbl = f"Janela: {snap['inspector_window']}"
    if vx + len(win_lbl) + 4 < width:
        safe_addstr(win, 3, width - len(win_lbl) - 3, win_lbl, colors["cyan"])

    cursor_y = top_h
    if snap["inspector_error"]:
        safe_addstr(win, cursor_y, 1, truncate_text(f"⚠ {snap['inspector_error']}", width - 2), colors["red"])
        cursor_y += 1
    elif snap["mode"] == "state" and not snap["inspector_historic"]:
        safe_addstr(win, cursor_y, 1, truncate_text("ℹ Modo STATE: pressione [m] para alternar para LIVE e carregar gráficos/histórico.", width - 2), colors["yellow"])
        cursor_y += 1
    elif snap["notice"]:
        safe_addstr(win, cursor_y, 1, truncate_text(f"ℹ {snap['notice']}", width - 2), colors["cyan"])
        cursor_y += 1

    footer_y = height - 1
    body_h = footer_y - cursor_y
    view = snap["inspector_view"]

    if body_h >= 6:
        if view == "chart":
            _render_inspector_chart_view(win, snap, cursor_y, body_h, width, colors)
        elif view == "details":
            _render_inspector_details_view(win, snap, cursor_y, body_h, width, colors)
        else:
            _render_inspector_history_view(win, snap, cursor_y, body_h, width, colors)

    footer = "[v/1-3] Visão  [n/p] Canal  [t] Janela  [↑↓/jk] Rolar  [r] Atualizar  [o] Navegador  [Esc/b] Voltar  [?] Ajuda  [q] Sair"
    safe_addstr(win, footer_y, 1, truncate_text(footer, width - 2), colors["dim"])


def _render_inspector_chart_view(win, snap, top, body_h, width, colors):
    channels = snap["inspector_channels"]
    historic = snap["inspector_historic"] or {}
    hist_channels = historic.get("channels", [])
    channels_panel_h = min(max(4, len(channels) + 2), max(4, body_h // 3)) if channels else 4
    chart_h = body_h - channels_panel_h

    if chart_h >= 6:
        draw_box(
            win,
            top,
            0,
            chart_h,
            width,
            title=f"Gráfico Histórico ({snap['inspector_window']})",
            border_attr=colors["cyan"],
            title_attr=colors["bold"],
        )
        if not hist_channels:
            msg = "Carregando série histórica..." if snap["inspector_loading"] else "Sem dados históricos numéricos disponíveis."
            safe_addstr(win, top + chart_h // 2, max(2, (width - len(msg)) // 2), msg, colors["dim"])
        else:
            ch_idx = snap["inspector_channel_index"] % len(hist_channels)
            ch_name = hist_channels[ch_idx]
            series = historic.get("series", {}).get(ch_name, [])
            stats = historic.get("stats", {}).get(ch_name, {})
            timestamps = historic.get("timestamps", [])

            stat_line = (
                f"Canal [{ch_idx + 1}/{len(hist_channels)}]: {ch_name}  |  "
                f"Últ: {format_human_number(stats.get('last'))}  "
                f"Mín: {format_human_number(stats.get('min'))}  "
                f"Méd: {format_human_number(stats.get('avg'))}  "
                f"Máx: {format_human_number(stats.get('max'))}  "
                f"({stats.get('count', 0)} pts)"
            )
            safe_addstr(win, top + 1, 2, truncate_text(stat_line, width - 4), colors["bold"])

            plot_h = chart_h - 4  # border top + stat line + x-axis + border bottom
            y_axis_w = 9
            plot_w = max(8, width - y_axis_w - 4)
            if plot_h >= 2:
                chart_lines, y_min, y_max = render_braille_chart(series, plot_w, plot_h)
                y_mid = ((y_min + y_max) / 2.0) if (y_min is not None and y_max is not None) else None
                for i, cline in enumerate(chart_lines):
                    if i == 0:
                        ylabel = format_human_number(y_max)
                    elif i == len(chart_lines) - 1:
                        ylabel = format_human_number(y_min)
                    elif i == len(chart_lines) // 2:
                        ylabel = format_human_number(y_mid)
                    else:
                        ylabel = ""
                    safe_addstr(win, top + 2 + i, 2, f"{ylabel:>{y_axis_w - 1}}│", colors["dim"])
                    safe_addstr(win, top + 2 + i, 2 + y_axis_w, cline, colors["green"])

                if timestamps:
                    t_start = truncate_text(timestamps[0], 19)
                    t_end = truncate_text(timestamps[-1], 19)
                    axis_y = top + 2 + plot_h
                    safe_addstr(win, axis_y, 2 + y_axis_w, t_start, colors["dim"])
                    if width - len(t_end) - 3 > 2 + y_axis_w + len(t_start):
                        safe_addstr(win, axis_y, width - len(t_end) - 2, t_end, colors["dim"])

    # Live channels summary box below chart
    ch_top = top + chart_h
    draw_box(
        win,
        ch_top,
        0,
        channels_panel_h,
        width,
        title=f"Canais Atuais ({len(channels)})",
        border_attr=colors["cyan"],
        title_attr=colors["bold"],
    )
    if not channels:
        msg = "Carregando canais..." if snap["inspector_loading"] else "Nenhum canal reportado."
        safe_addstr(win, ch_top + 1, 2, msg, colors["dim"])
    else:
        cap = channels_panel_h - 2
        for i in range(min(cap, len(channels))):
            ch = channels[i]
            line = f"• {ch['name']:<34} {ch['lastvalue']}"
            safe_addstr(win, ch_top + 1 + i, 2, truncate_text(line, width - 4), colors["normal"])


def _render_inspector_details_view(win, snap, top, body_h, width, colors):
    sensor = snap["inspector_sensor"] or {}
    details = snap["inspector_details"] or {}
    channels = snap["inspector_channels"]
    objid = sensor.get("objid", details.get("objid", ""))

    lines = [
        ("Nome", details.get("name") or sensor.get("sensor") or "-"),
        ("Tipo", details.get("sensortype") or "-"),
        ("Probe", details.get("probename") or sensor.get("probe") or "-"),
        ("Grupo", details.get("parentgroupname") or sensor.get("group") or "-"),
        ("Dispositivo", details.get("parentdevicename") or sensor.get("device") or "-"),
        ("Status", f"{details.get('statustext') or sensor.get('status') or '-'} ({details.get('statusid') or sensor.get('status_raw') or '-'})"),
        ("Intervalo", f"{details['interval']}s" if details.get("interval") else "-"),
        ("Último valor", details.get("lastvalue") or sensor.get("lastvalue") or "-"),
        ("Uptime", f"{details.get('uptime') or sensor.get('uptime') or '-'} ({details.get('uptimetime') or '-'})"),
        ("Downtime", f"{details.get('downtime') or sensor.get('downtime') or '-'} ({details.get('downtimetime') or '-'})"),
        ("Desde", details.get("updownsince") or "-"),
        ("Último Check", details.get("lastcheck") or sensor.get("lastcheck") or "-"),
        ("Último Up", details.get("lastup") or sensor.get("lastup") or "-"),
        ("Último Down", details.get("lastdown") or sensor.get("lastdown") or "-"),
        ("Tags", sensor.get("tags") or "-"),
        ("Mensagem", details.get("lastmessage") or sensor.get("message") or "-"),
        ("URL", build_sensor_url(snap["prtg_url"], objid)),
        ("", ""),
        ("Canais", f"{len(channels)} canal(is)"),
    ]
    for ch in channels:
        lines.append((f"  {ch['name']}", f"{ch['lastvalue']} (raw: {ch['lastvalue_raw']})"))

    draw_box(win, top, 0, body_h, width, title="Detalhes Completos & Canais", border_attr=colors["cyan"], title_attr=colors["bold"])
    cap = body_h - 2
    scroll = max(0, min(snap["inspector_scroll"], max(0, len(lines) - cap)))
    for i in range(cap):
        idx = scroll + i
        if idx >= len(lines):
            break
        k, v = lines[idx]
        if not k and not v:
            continue
        text = f"{k:<16}: {v}"
        safe_addstr(win, top + 1 + i, 2, truncate_text(text, width - 4), colors["normal"])


def _render_inspector_history_view(win, snap, top, body_h, width, colors):
    messages = snap["inspector_messages"]
    historic = snap["inspector_historic"] or {}
    hist_channels = historic.get("channels", [])
    timestamps = historic.get("timestamps", [])
    series = historic.get("series", {})

    rows = []
    rows.append(("hdr", f"--- Eventos recentes do sensor (últimos 30 dias: {len(messages)}) ---"))
    if not messages:
        rows.append(("dim", "Nenhum evento registrado nos últimos 30 dias."))
    else:
        for m in messages:
            rows.append(("msg", f"[{m['datetime']}] {m['status']}: {m['message']}"))

    rows.append(("hdr", ""))
    ch_name = None
    if hist_channels:
        ch_name = hist_channels[snap["inspector_channel_index"] % len(hist_channels)]
    rows.append(("hdr", f"--- Tabela histórica ({snap['inspector_window']}) | Canal: {ch_name or '-'} ({len(timestamps)} pts) ---"))
    if not timestamps or not ch_name:
        rows.append(("dim", "Sem pontos históricos para exibir."))
    else:
        vals = series.get(ch_name, [])
        coverage = historic.get("coverage", [])
        for idx in range(len(timestamps) - 1, -1, -1):
            ts = timestamps[idx]
            val = format_human_number(vals[idx] if idx < len(vals) else None)
            cov = coverage[idx] if idx < len(coverage) else ""
            rows.append(("pt", f"{ts:<31} | {val:>12} | {cov}"))

    draw_box(win, top, 0, body_h, width, title="Histórico & Eventos do Sensor", border_attr=colors["cyan"], title_attr=colors["bold"])
    cap = body_h - 2
    scroll = max(0, min(snap["inspector_scroll"], max(0, len(rows) - cap)))
    for i in range(cap):
        idx = scroll + i
        if idx >= len(rows):
            break
        kind, text = rows[idx]
        attr = colors["cyan"] | colors["bold"] if kind == "hdr" else (colors["dim"] if kind == "dim" else colors["normal"])
        safe_addstr(win, top + 1 + i, 2, truncate_text(text, width - 4), attr)


def render_tui(win, state, now_mono=None, now_epoch=None, spinner_idx=0, colors=None):
    """Renders the complete TUI frame cleanly onto win."""
    colors = colors or {
        "normal": 0,
        "red": 0,
        "yellow": 0,
        "green": 0,
        "cyan": 0,
        "magenta": 0,
        "dim": 0,
        "bold": curses.A_BOLD,
        "reverse": curses.A_REVERSE,
    }
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_epoch = time.time() if now_epoch is None else now_epoch

    snap = state.snapshot_for_render(now_mono=now_mono)
    win.erase()
    height, width = win.getmaxyx()

    if height < 12 or width < 50:
        msg = "Terminal muito pequeno (mínimo 50x12)."
        safe_addstr(win, max(0, height // 2), max(0, (width - len(msg)) // 2), msg, colors["yellow"])
        win.noutrefresh()
        return

    if snap["inspector_open"] and snap["inspector_sensor"] is not None:
        _render_inspector(win, snap, height, width, now_mono, spinner_idx, colors)
        if snap["show_help"]:
            _render_help_modal(win, snap, height, width, colors)
        win.noutrefresh()
        return

    # 1. Top Header Box (5 rows: border + status/counts + tabs + meta + bottom border)
    header_h = 5
    prtg_ver = snap["server_status"].get("Version", "")
    box_title = f"PRTG Monitor TUI (v{prtg_ver})" if prtg_ver else "PRTG Monitor TUI"
    draw_box(
        win,
        0,
        0,
        header_h,
        width,
        title=box_title,
        border_attr=colors["cyan"],
        title_attr=colors["cyan"] | colors["bold"],
    )
    url_label = truncate_text(snap["prtg_url"], max(10, width // 3))
    if width > len(url_label) + 30:
        safe_addstr(win, 0, width - len(url_label) - 3, f" {url_label} ", colors["cyan"])

    if snap["is_refreshing"]:
        spin = SPINNER_FRAMES[spinner_idx % len(SPINNER_FRAMES)]
        mode_tag = f"{spin} ATUALIZANDO..."
        mode_attr = colors["yellow"]
    elif snap["last_error"]:
        mode_tag = f"▲ {snap['mode'].upper()} (ERRO)"
        mode_attr = colors["red"]
    elif snap["mode"] == "live":
        mode_tag = "● LIVE"
        mode_attr = colors["green"]
    else:
        mode_tag = "● STATE"
        mode_attr = colors["cyan"] | colors["bold"]

    safe_addstr(win, 1, 2, mode_tag, mode_attr)
    col_x = 2 + len(mode_tag) + 3
    if snap["live_loaded"] and snap["mode"] == "live":
        s_sum = summarize_all_sensors(snap["all_sensors"])
        counts = [
            ("Sensores: ", f"{s_sum['total']}", colors["bold"]),
            ("Down: ", f"{s_sum['down'] + s_sum['partial']}", colors["red"] if (s_sum["down"] + s_sum["partial"]) else colors["green"]),
            ("Warn: ", f"{s_sum['warn']}", colors["yellow"] if s_sum["warn"] else colors["normal"]),
            ("Up: ", f"{s_sum['up']}", colors["green"]),
            ("Pausados: ", f"{s_sum['paused']}", colors["dim"]),
        ]
    else:
        a_sum = summarize_alerts(snap["all_alerts"])
        counts = [
            ("Total: ", f"{a_sum['total']}", colors["bold"]),
            ("Down: ", f"{a_sum['down']}", colors["red"] if a_sum["down"] else colors["green"]),
            ("Partial: ", f"{a_sum['partial']}", colors["yellow"] if a_sum["partial"] else colors["normal"]),
            ("Outros: ", f"{a_sum['other']}", colors["magenta"] if a_sum["other"] else colors["normal"]),
        ]
    for lbl, val, val_attr in counts:
        if col_x + len(lbl) + len(val) + 2 >= width - 1:
            break
        safe_addstr(win, 1, col_x, lbl, colors["dim"])
        col_x += len(lbl)
        safe_addstr(win, 1, col_x, val, val_attr)
        col_x += len(val) + 2

    # Row 2: Tab Bar
    tx = 2
    for idx, (tab_key, tab_label) in enumerate(TABS):
        is_active = tab_key == snap["tab"]
        chunk = f"[{tab_label}]" if is_active else f" {tab_label} "
        if tx + len(chunk) >= width - 2:
            break
        attr = (colors["cyan"] | colors["bold"] | colors["reverse"]) if is_active else colors["dim"]
        safe_addstr(win, 2, tx, chunk, attr)
        tx += len(chunk) + 1

    # Row 3: Timing / Filters / Sort metadata
    updated_str = format_timestamp_local(snap["last_updated"])
    if snap["last_updated"] is not None:
        age_str = format_age(max(0.0, now_epoch - snap["last_updated"]))
        updated_str = f"{updated_str} (há {age_str})"
    remaining = max(0, int(snap["next_refresh_at"] - now_mono))
    sort_label = SORT_LABELS.get(snap["sort_key"], snap["sort_key"])
    filter_display = f'"{snap["filter_query"]}"' if snap["filter_query"] else "todos"
    extra = ""
    if snap["tab"] == "sensors":
        extra += f" | Status: {STATUS_FILTER_LABELS.get(snap['status_filter'], snap['status_filter'])}"
        if snap["device_filter"]:
            extra += f" | Disp: {snap['device_filter']}"
        if snap["group_filter"]:
            extra += f" | Grupo: {snap['group_filter']}"
    elif snap["tab"] == "logs":
        range_lbl = dict(LOG_RANGES).get(snap["log_range"], snap["log_range"])
        extra += f" | Período: {range_lbl}"
    info_line = f"Atualizado: {updated_str} | Próx: {remaining}s | Ordem: {sort_label} | Busca: {filter_display}{extra}"
    safe_addstr(win, 3, 2, truncate_text(info_line, width - 4), colors["normal"])

    # 2. Optional error or notice banner
    cursor_y = header_h
    banner_text = None
    banner_attr = colors["normal"]
    if snap["last_error"]:
        banner_text = f"⚠ {snap['last_error']}"
        banner_attr = colors["red"]
    elif snap["notice"]:
        banner_text = f"ℹ {snap['notice']}"
        banner_attr = colors["cyan"]
    if banner_text and height >= 14:
        safe_addstr(win, cursor_y, 1, truncate_text(banner_text, width - 2), banner_attr)
        cursor_y += 1

    footer_y = height - 1
    detail_h = 6 if height >= 16 else 4
    table_top = cursor_y
    table_bottom = footer_y - detail_h
    table_h = table_bottom - table_top
    visible = snap["visible"]
    tab = snap["tab"]

    # 3. Main Table Box
    if table_h >= 4:
        tab_title = dict(TABS).get(tab, tab).split(":", 1)[-1]
        draw_box(
            win,
            table_top,
            0,
            table_h,
            width,
            title=f"{tab_title} ({len(visible)})",
            border_attr=colors["cyan"],
            title_attr=colors["bold"],
        )

        inner_w = width - 4
        hdr_y = table_top + 1
        _render_table_header(win, tab, hdr_y, inner_w, colors)
        safe_addstr(win, hdr_y + 1, 1, "─" * (width - 2), colors["dim"])

        body_top = hdr_y + 2
        body_cap = max(0, (table_top + table_h - 1) - body_top)

        if not visible:
            if body_cap > 0:
                if snap["is_refreshing"] and not snap["live_loaded"] and tab != "alerts":
                    empty_msg = "Carregando dados do servidor PRTG..."
                    empty_attr = colors["yellow"]
                elif tab == "alerts" and not snap["all_alerts"] and not snap["filter_query"]:
                    empty_msg = "✓ Nenhum alerta ativo no momento"
                    empty_attr = colors["green"]
                elif snap["mode"] == "state" and tab != "alerts":
                    empty_msg = "Modo STATE mostra apenas alertas ativos (pressione [m] para modo LIVE)"
                    empty_attr = colors["yellow"]
                else:
                    empty_msg = "Nenhum item encontrado para o filtro atual"
                    empty_attr = colors["yellow"]
                msg_y = body_top + max(0, (body_cap - 1) // 2)
                msg_x = max(2, (width - len(empty_msg)) // 2)
                safe_addstr(win, msg_y, msg_x, empty_msg, empty_attr)
        elif body_cap > 0:
            sel_idx = snap["selected_idx"]
            scroll = snap["scroll_offset"]
            if sel_idx < scroll:
                scroll = sel_idx
            elif sel_idx >= scroll + body_cap:
                scroll = sel_idx - body_cap + 1
            scroll = max(0, min(scroll, max(0, len(visible) - body_cap)))
            state.update_scroll(scroll, tab=tab)

            if len(visible) > body_cap:
                range_label = f" {scroll + 1}-{min(len(visible), scroll + body_cap)}/{len(visible)} "
                if width > len(range_label) + 25:
                    safe_addstr(win, table_top, width - len(range_label) - 3, range_label, colors["dim"])

            for row_i in range(body_cap):
                item_idx = scroll + row_i
                if item_idx >= len(visible):
                    break
                _render_table_row(
                    win,
                    tab,
                    visible[item_idx],
                    body_top + row_i,
                    inner_w,
                    item_idx == sel_idx,
                    colors,
                )

    # 4. Bottom Details Box
    _render_bottom_details(win, snap, table_bottom, detail_h, width, colors)

    # 5. Footer Bar
    if snap["filter_input_active"]:
        footer_text = f"Filtro: {snap['filter_draft']}█   [Enter] Aplicar  [Esc] Cancelar"
        footer_attr = colors["yellow"]
    else:
        footer_text = "[1-5/Tab] Abas  [Enter] Abrir/Detalhes  [/] Buscar  [f] Status  [s] Ordem  [r] Atualizar  [m] Modo  [o] Web  [?] Ajuda  [q] Sair"
        footer_attr = colors["dim"]
    safe_addstr(win, footer_y, 1, truncate_text(footer_text, width - 2), footer_attr)

    if snap["show_help"]:
        _render_help_modal(win, snap, height, width, colors)

    win.noutrefresh()


def _render_table_header(win, tab, y, inner_w, colors):
    if tab in ("alerts", "sensors"):
        id_w = 6
        status_w = 13
        val_w = max(8, min(16, inner_w // 7))
        up_w = 8 if (tab == "sensors" and inner_w >= 86) else 0
        reserved = 2 + id_w + 1 + status_w + 1 + val_w + (up_w + 1 if up_w else 0) + 2
        rem = max(12, inner_w - reserved)
        dev_w = max(8, int(rem * 0.36))
        sen_w = max(8, rem - dev_w)
        hdr = f"  {'ID':<{id_w}} {'STATUS':<{status_w}} {'DISPOSITIVO':<{dev_w}} {'SENSOR':<{sen_w}} {'VALOR':<{val_w}}"
        if up_w:
            hdr += f" {'UPTIME':<{up_w}}"
    elif tab == "devices":
        id_w = 6
        host_w = max(12, min(18, inner_w // 5))
        cnt_w = 24
        rem = max(12, inner_w - (2 + id_w + 1 + host_w + 1 + cnt_w + 3))
        dev_w = max(10, int(rem * 0.55))
        grp_w = max(8, rem - dev_w)
        hdr = f"  {'ID':<{id_w}} {'DISPOSITIVO':<{dev_w}} {'HOST':<{host_w}} {'GRUPO':<{grp_w}} {'D/W/U/P':<{cnt_w}}"
    elif tab == "groups":
        id_w = 6
        cnt_w = 24
        rem = max(12, inner_w - (2 + id_w + 1 + cnt_w + 3))
        grp_w = max(10, int(rem * 0.58))
        prb_w = max(8, rem - grp_w)
        hdr = f"  {'ID':<{id_w}} {'GRUPO':<{grp_w}} {'PROBE':<{prb_w}} {'D/W/U/P':<{cnt_w}}"
    else:  # logs
        dt_w = 19
        st_w = 14
        src_w = max(12, min(22, inner_w // 4))
        msg_w = max(12, inner_w - (2 + dt_w + 1 + st_w + 1 + src_w + 2))
        hdr = f"  {'DATA/HORA':<{dt_w}} {'STATUS':<{st_w}} {'ORIGEM':<{src_w}} {'MENSAGEM':<{msg_w}}"
    safe_addstr(win, y, 2, truncate_text(hdr, inner_w), colors["bold"] | colors["cyan"])


def _render_table_row(win, tab, item, row_y, inner_w, is_selected, colors):
    pointer = "▶ " if is_selected else "  "
    row_attr = colors["reverse"] if is_selected else colors["normal"]

    if tab in ("alerts", "sensors"):
        id_w = 6
        status_w = 13
        val_w = max(8, min(16, inner_w // 7))
        up_w = 8 if (tab == "sensors" and inner_w >= 86) else 0
        reserved = 2 + id_w + 1 + status_w + 1 + val_w + (up_w + 1 if up_w else 0) + 2
        rem = max(12, inner_w - reserved)
        dev_w = max(8, int(rem * 0.36))
        sen_w = max(8, rem - dev_w)

        dot, dot_attr = _status_badge(item.get("status_raw", 0), colors)
        objid_col = truncate_text(sanitize_line(item.get("objid", "")), id_w)
        status_txt = truncate_text(sanitize_line(item.get("status", "")), max(1, status_w - 2))
        dev_col = truncate_text(sanitize_line(item.get("device", "")), dev_w)
        sen_col = truncate_text(sanitize_line(item.get("sensor", "")), sen_w)
        val_col = truncate_text(sanitize_line(item.get("lastvalue", "")), val_w)

        row_text = (
            f"{pointer}{objid_col:<{id_w}} {dot} {status_txt:<{max(1, status_w - 2)}} "
            f"{dev_col:<{dev_w}} {sen_col:<{sen_w}} {val_col:<{val_w}}"
        )
        if up_w:
            up_col = truncate_text(sanitize_line(item.get("uptime", "")), up_w)
            row_text += f" {up_col:<{up_w}}"
        safe_addstr(win, row_y, 2, truncate_text(row_text, inner_w), row_attr)
        if not is_selected:
            safe_addstr(win, row_y, 2 + 2 + id_w + 1, dot, dot_attr)
    elif tab == "devices":
        id_w = 6
        host_w = max(12, min(18, inner_w // 5))
        cnt_w = 24
        rem = max(12, inner_w - (2 + id_w + 1 + host_w + 1 + cnt_w + 3))
        dev_w = max(10, int(rem * 0.55))
        grp_w = max(8, rem - dev_w)

        down = item.get("down", 0) + item.get("partial", 0)
        warn = item.get("warn", 0)
        up = item.get("up", 0)
        paused = item.get("paused", 0)
        total = max(1, down + warn + up + paused)
        bar = render_bar(up / float(total), 8)
        summary_col = f"{down}/{warn}/{up}/{paused} {bar}"

        row_text = (
            f"{pointer}{truncate_text(item.get('objid', ''), id_w):<{id_w}} "
            f"{truncate_text(sanitize_line(item.get('device', '')), dev_w):<{dev_w}} "
            f"{truncate_text(sanitize_line(item.get('host', '')), host_w):<{host_w}} "
            f"{truncate_text(sanitize_line(item.get('group', '')), grp_w):<{grp_w}} "
            f"{truncate_text(summary_col, cnt_w):<{cnt_w}}"
        )
        if not is_selected and down > 0:
            row_attr = colors["red"]
        elif not is_selected and warn > 0:
            row_attr = colors["yellow"]
        safe_addstr(win, row_y, 2, truncate_text(row_text, inner_w), row_attr)
    elif tab == "groups":
        id_w = 6
        cnt_w = 24
        rem = max(12, inner_w - (2 + id_w + 1 + cnt_w + 3))
        grp_w = max(10, int(rem * 0.58))
        prb_w = max(8, rem - grp_w)

        down = item.get("down", 0)
        warn = item.get("warn", 0)
        up = item.get("up", 0)
        paused = item.get("paused", 0)
        total = max(1, down + warn + up + paused)
        bar = render_bar(up / float(total), 8)
        summary_col = f"{down}/{warn}/{up}/{paused} {bar}"

        row_text = (
            f"{pointer}{truncate_text(item.get('objid', ''), id_w):<{id_w}} "
            f"{truncate_text(sanitize_line(item.get('group', '')), grp_w):<{grp_w}} "
            f"{truncate_text(sanitize_line(item.get('probe', '')), prb_w):<{prb_w}} "
            f"{truncate_text(summary_col, cnt_w):<{cnt_w}}"
        )
        if not is_selected and down > 0:
            row_attr = colors["red"]
        elif not is_selected and warn > 0:
            row_attr = colors["yellow"]
        safe_addstr(win, row_y, 2, truncate_text(row_text, inner_w), row_attr)
    else:  # logs
        dt_w = 19
        st_w = 14
        src_w = max(12, min(22, inner_w // 4))
        msg_w = max(12, inner_w - (2 + dt_w + 1 + st_w + 1 + src_w + 2))
        row_text = (
            f"{pointer}{truncate_text(sanitize_line(item.get('datetime', '')), dt_w):<{dt_w}} "
            f"{truncate_text(sanitize_line(item.get('status', '')), st_w):<{st_w}} "
            f"{truncate_text(sanitize_line(item.get('name', '')), src_w):<{src_w}} "
            f"{truncate_text(sanitize_line(item.get('message', '')), msg_w):<{msg_w}}"
        )
        safe_addstr(win, row_y, 2, truncate_text(row_text, inner_w), row_attr)


def _render_bottom_details(win, snap, detail_top, detail_h, width, colors):
    tab = snap["tab"]
    title = (
        "Detalhes do Sensor Selecionado"
        if tab in ("alerts", "sensors")
        else "Detalhes do Item Selecionado"
    )
    draw_box(
        win,
        detail_top,
        0,
        detail_h,
        width,
        title=title,
        border_attr=colors["cyan"],
        title_attr=colors["bold"],
    )
    selected = snap["selected"]
    if selected is None:
        safe_addstr(win, detail_top + 1, 2, "Nenhum item selecionado.", colors["dim"])
        return

    inner_d_w = width - 4
    if tab in ("alerts", "sensors"):
        sensor_line = (
            f"Sensor: {sanitize_line(selected.get('sensor', ''))}  |  "
            f"Dispositivo: {sanitize_line(selected.get('device', ''))}  |  "
            f"Status: {sanitize_line(selected.get('status', ''))} ({selected.get('status_raw', '')})"
        )
        safe_addstr(win, detail_top + 1, 2, truncate_text(sensor_line, inner_d_w), colors["bold"])
        msg_clean = sanitize_line(selected.get("message", "")) or "(sem mensagem)"
        url_str = build_sensor_url(snap["prtg_url"], selected.get("objid", ""))
        if detail_h >= 6:
            wrapped = textwrap.wrap(f"Mensagem: {msg_clean}", width=max(20, inner_d_w)) or ["Mensagem: "]
            safe_addstr(win, detail_top + 2, 2, truncate_text(wrapped[0], inner_d_w), colors["normal"])
            if len(wrapped) > 1:
                safe_addstr(win, detail_top + 3, 2, truncate_text(wrapped[1], inner_d_w), colors["normal"])
            else:
                val_str = sanitize_line(selected.get("lastvalue", "")) or "-"
                safe_addstr(
                    win,
                    detail_top + 3,
                    2,
                    truncate_text(f"Último valor: {val_str}  |  [Enter] Gráficos, canais e histórico", inner_d_w),
                    colors["dim"],
                )
            safe_addstr(win, detail_top + 4, 2, truncate_text(f"URL: {url_str}", inner_d_w), colors["cyan"])
        else:
            safe_addstr(win, detail_top + 2, 2, truncate_text(f"Msg: {msg_clean}", inner_d_w), colors["normal"])
    elif tab == "devices":
        line1 = f"Dispositivo: {selected.get('device', '')} | Host: {selected.get('host', '')} | Grupo: {selected.get('group', '')}"
        line2 = (
            f"Sensores -> Down: {selected.get('down', 0)} | Partial: {selected.get('partial', 0)} | "
            f"Warn: {selected.get('warn', 0)} | Up: {selected.get('up', 0)} | Pausados: {selected.get('paused', 0)}"
        )
        safe_addstr(win, detail_top + 1, 2, truncate_text(line1, inner_d_w), colors["bold"])
        safe_addstr(win, detail_top + 2, 2, truncate_text(line2, inner_d_w), colors["normal"])
        if detail_h >= 6:
            safe_addstr(win, detail_top + 3, 2, "Pressione [Enter] para ver os sensores deste dispositivo.", colors["dim"])
            safe_addstr(win, detail_top + 4, 2, truncate_text(f"URL: {build_object_url(snap['prtg_url'], 'devices', selected)}", inner_d_w), colors["cyan"])
    elif tab == "groups":
        line1 = f"Grupo: {selected.get('group', '')} | Probe: {selected.get('probe', '') or '-'}"
        line2 = (
            f"Sensores -> Down: {selected.get('down', 0)} | Warn: {selected.get('warn', 0)} | "
            f"Up: {selected.get('up', 0)} | Pausados: {selected.get('paused', 0)}"
        )
        safe_addstr(win, detail_top + 1, 2, truncate_text(line1, inner_d_w), colors["bold"])
        safe_addstr(win, detail_top + 2, 2, truncate_text(line2, inner_d_w), colors["normal"])
        if detail_h >= 6:
            safe_addstr(win, detail_top + 3, 2, "Pressione [Enter] para ver os sensores deste grupo.", colors["dim"])
            safe_addstr(win, detail_top + 4, 2, truncate_text(f"URL: {build_object_url(snap['prtg_url'], 'groups', selected)}", inner_d_w), colors["cyan"])
    else:  # logs
        line1 = f"[{selected.get('datetime', '')}] {selected.get('status', '')} | {selected.get('name', '')} ({selected.get('parent', '')})"
        safe_addstr(win, detail_top + 1, 2, truncate_text(line1, inner_d_w), colors["bold"])
        if detail_h >= 6:
            wrapped = textwrap.wrap(sanitize_line(selected.get("message", "")), width=max(20, inner_d_w)) or ["-"]
            safe_addstr(win, detail_top + 2, 2, truncate_text(wrapped[0], inner_d_w), colors["normal"])
            if len(wrapped) > 1:
                safe_addstr(win, detail_top + 3, 2, truncate_text(wrapped[1], inner_d_w), colors["normal"])


def handle_key(
    key,
    state,
    refresh_cb,
    open_cb,
    page_size=10,
    inspector_refresh_cb=None,
):
    """Processes one keypress (int or str). Returns False when user quits."""
    if key is None:
        return True

    key_code = ord(key) if isinstance(key, str) and len(key) == 1 else key
    key_char = key if isinstance(key, str) else (chr(key) if isinstance(key, int) and 32 <= key <= 126 else None)

    with state.lock:
        show_help = state.show_help
        in_filter = state.filter_input_active
        in_inspector = state.inspector_open

    if show_help:
        if key_code in (27, 10, 13, curses.KEY_ENTER) or key_char in ("?", "q", "Q", " "):
            with state.lock:
                state.show_help = False
        return True

    if in_filter:
        if key_code == 27:  # Esc cancels filter edit and restores prior query
            with state.lock:
                state.filter_input_active = False
                state.filter_query = state.filter_prev
                state.filter_draft = state.filter_prev
            return True
        if key_code in (10, 13, curses.KEY_ENTER):
            with state.lock:
                state.filter_input_active = False
                state.filter_query = state.filter_draft
                state.scroll_offset = 0
                visible = state.visible_items_locked()
                state._selected_index_locked(visible)
            return True
        if key_code in (curses.KEY_BACKSPACE, 127, 8):
            with state.lock:
                state.filter_draft = state.filter_draft[:-1]
                state.filter_query = state.filter_draft
                state.scroll_offset = 0
            return True
        if isinstance(key, str) and key.isprintable() and key not in ("\n", "\r", "\t"):
            with state.lock:
                state.filter_draft += key
                state.filter_query = state.filter_draft
                state.scroll_offset = 0
            return True
        if key_char is not None:
            with state.lock:
                state.filter_draft += key_char
                state.filter_query = state.filter_draft
                state.scroll_offset = 0
        return True

    if in_inspector:
        return _handle_inspector_key(
            key_code,
            key_char,
            state,
            open_cb,
            page_size,
            inspector_refresh_cb,
        )

    if key_char in ("q", "Q"):
        return False
    if key_char in ("1", "2", "3", "4", "5"):
        tab = state.set_tab(int(key_char) - 1)
        state.set_notice(f"Aba: {dict(TABS)[tab].split(':', 1)[-1]}", duration=1.5)
    elif key_code == 9:  # Tab
        with state.lock:
            next_idx = (state.tab_index + 1) % len(TABS)
        state.set_tab(next_idx)
    elif key_code == curses.KEY_UP or key_char in ("k", "K"):
        state.move_selection(-1)
    elif key_code == curses.KEY_DOWN or key_char in ("j", "J"):
        state.move_selection(1)
    elif key_code == curses.KEY_PPAGE:
        state.move_selection(-max(1, page_size))
    elif key_code == curses.KEY_NPAGE:
        state.move_selection(max(1, page_size))
    elif key_code == curses.KEY_HOME or key_char == "g":
        state.select_first()
    elif key_code == curses.KEY_END or key_char == "G":
        state.select_last()
    elif key_char in ("r", "R"):
        refresh_cb(force=True)
    elif key_char == "/":
        with state.lock:
            state.filter_prev = state.filter_query
            state.filter_draft = state.filter_query
            state.filter_input_active = True
    elif key_char in ("c", "C"):
        with state.lock:
            state.filter_query = ""
            state.filter_draft = ""
            state.device_filter = None
            state.group_filter = None
            state.status_filter_index = 0
            state.scroll_offset = 0
        state.set_notice("Filtros limpos.", duration=2.0)
    elif key_char in ("f", "F"):
        with state.lock:
            tab = state.current_tab_locked()
            if tab != "sensors":
                state.tab_index = 1
        new_sf = state.cycle_status_filter()
        state.set_notice(f"Status: {STATUS_FILTER_LABELS.get(new_sf, new_sf)}", duration=2.5)
    elif key_char in ("s", "S"):
        new_sort = state.cycle_sort()
        state.set_notice(f"Ordenação: {SORT_LABELS.get(new_sort, new_sort)}", duration=2.5)
    elif key_char in ("t", "T"):
        if state.current_tab() == "logs":
            _, label = state.cycle_log_range()
            state.set_notice(f"Período dos logs: {label}", duration=2.5)
            refresh_cb(force=True)
    elif key_char in ("m", "M"):
        new_mode = state.toggle_mode()
        state.set_notice(f"Modo alterado para {new_mode.upper()}", duration=2.5)
        refresh_cb(force=True)
    elif key_char in ("o", "O"):
        selected = state.selected_alert()
        if selected is not None:
            open_cb(selected)
    elif key_code in (10, 13, curses.KEY_ENTER):
        selected = state.selected_alert()
        if selected is not None:
            tab = state.current_tab()
            if tab in ("alerts", "sensors"):
                state.open_inspector(selected)
                if inspector_refresh_cb is not None:
                    inspector_refresh_cb(objid=selected.get("objid"), history_only=False)
            elif tab == "devices":
                with state.lock:
                    state.device_filter = selected.get("device")
                    state.group_filter = None
                    state.status_filter_index = 0
                    state.tab_index = 1
                    state.scroll_by_tab["sensors"] = 0
                state.set_notice(f"Sensores de {selected.get('device', '')}", duration=2.5)
            elif tab == "groups":
                with state.lock:
                    state.group_filter = selected.get("group")
                    state.device_filter = None
                    state.status_filter_index = 0
                    state.tab_index = 1
                    state.scroll_by_tab["sensors"] = 0
                state.set_notice(f"Sensores do grupo {selected.get('group', '')}", duration=2.5)
            else:
                open_cb(selected)
    elif key_char == "?":
        with state.lock:
            state.show_help = True
    elif key_code == 27:
        with state.lock:
            if state.filter_query or state.device_filter or state.group_filter:
                state.filter_query = ""
                state.filter_draft = ""
                state.device_filter = None
                state.group_filter = None
                state.scroll_offset = 0
            elif state.last_error:
                state.last_error = None
    return True


def _handle_inspector_key(
    key_code,
    key_char,
    state,
    open_cb,
    page_size,
    inspector_refresh_cb,
):
    if key_char in ("q", "Q"):
        return False
    if key_code == 27 or key_char in ("b", "B"):
        state.close_inspector()
        return True
    if key_char == "?":
        with state.lock:
            state.show_help = True
        return True
    if key_char in ("v", "V"):
        with state.lock:
            state.inspector_view_index = (state.inspector_view_index + 1) % len(INSPECTOR_VIEWS)
            state.inspector_scroll = 0
    elif key_char in ("1", "2", "3"):
        with state.lock:
            state.inspector_view_index = int(key_char) - 1
            state.inspector_scroll = 0
    elif key_char in ("n", "N") or key_code in (curses.KEY_RIGHT, 9):
        with state.lock:
            hist = state.inspector_historic or {}
            ch_count = len(hist.get("channels", []))
            if ch_count > 0:
                state.inspector_channel_index = (state.inspector_channel_index + 1) % ch_count
    elif key_char in ("p", "P") or key_code == curses.KEY_LEFT:
        with state.lock:
            hist = state.inspector_historic or {}
            ch_count = len(hist.get("channels", []))
            if ch_count > 0:
                state.inspector_channel_index = (state.inspector_channel_index - 1) % ch_count
    elif key_char in ("t", "T"):
        with state.lock:
            state.inspector_window_index = (state.inspector_window_index + 1) % len(HISTORY_WINDOWS)
            new_win = state.current_inspector_window_locked()
            objid = state.inspector_sensor.get("objid") if state.inspector_sensor else None
        state.set_notice(f"Janela histórica: {new_win}", duration=2.0)
        if inspector_refresh_cb is not None and objid:
            inspector_refresh_cb(objid=objid, window_key=new_win, history_only=True)
    elif key_char in ("r", "R"):
        with state.lock:
            objid = state.inspector_sensor.get("objid") if state.inspector_sensor else None
            win_key = state.current_inspector_window_locked()
        if inspector_refresh_cb is not None and objid:
            inspector_refresh_cb(objid=objid, window_key=win_key, history_only=False)
    elif key_char in ("o", "O"):
        with state.lock:
            sensor = dict(state.inspector_sensor) if state.inspector_sensor else None
        if sensor is not None:
            open_cb(sensor)
    elif key_code == curses.KEY_UP or key_char in ("k", "K"):
        with state.lock:
            state.inspector_scroll = max(0, state.inspector_scroll - 1)
    elif key_code == curses.KEY_DOWN or key_char in ("j", "J"):
        with state.lock:
            state.inspector_scroll += 1
    elif key_code == curses.KEY_PPAGE:
        with state.lock:
            state.inspector_scroll = max(0, state.inspector_scroll - max(1, page_size))
    elif key_code == curses.KEY_NPAGE:
        with state.lock:
            state.inspector_scroll += max(1, page_size)
    elif key_code == curses.KEY_HOME or key_char == "g":
        with state.lock:
            state.inspector_scroll = 0
    elif key_code == curses.KEY_END or key_char == "G":
        with state.lock:
            state.inspector_scroll += 500
    return True


def _run_curses_loop(stdscr, runner, use_color=True):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    stdscr.timeout(UI_TICK_MS)

    colors = init_colors(use_color=use_color)
    spinner_idx = 0

    def open_selected(item):
        tab = runner.state.current_tab()
        if runner.state.inspector_open:
            url = build_sensor_url(runner.state.prtg_url, item.get("objid", ""))
        else:
            url = build_object_url(runner.state.prtg_url, tab, item)
        if open_sensor_url(url):
            runner.state.set_notice(f"Abrindo objeto {item.get('objid', '')} no navegador...", duration=3.0)
        else:
            runner.state.set_notice("Não foi possível abrir URL no navegador.", duration=3.0)

    runner.trigger_refresh(force=False)

    while True:
        now_mono = time.monotonic()
        with runner.state.lock:
            due = (
                not runner.state.inspector_open
                and (now_mono >= runner.state.next_refresh_at)
                and not runner.state.is_refreshing
            )
        if due:
            runner.trigger_refresh(force=False)

        render_tui(stdscr, runner.state, now_mono=now_mono, spinner_idx=spinner_idx, colors=colors)
        curses.doupdate()
        spinner_idx = (spinner_idx + 1) % len(SPINNER_FRAMES)

        try:
            key = stdscr.get_wch()
        except curses.error:
            continue

        if key == curses.KEY_RESIZE:
            continue

        height, _ = stdscr.getmaxyx()
        page_size = max(1, height - 14)
        if not handle_key(
            key,
            runner.state,
            runner.trigger_refresh,
            open_selected,
            page_size=page_size,
            inspector_refresh_cb=runner.trigger_inspector_refresh,
        ):
            break


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Interactive terminal TUI for PRTG Monitor")
    parser.add_argument(
        "config_file",
        nargs="?",
        default=None,
        help="Path to JSON configuration file (defaults to ./config.json or ~/.local/share/prtgmon/config.json)",
    )
    parser.add_argument(
        "--from-state",
        action="store_true",
        help="Watch LaunchAgent state.json instead of polling PRTG directly.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Override refresh interval in seconds for live mode.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable terminal colors.",
    )
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.interval is not None and args.interval <= 0:
        print("Erro: --interval deve ser maior que zero.", file=sys.stderr)
        return 1

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Erro: prtg_tui.py requer um terminal interativo (TTY).", file=sys.stderr)
        return 1

    config_path = resolve_config_path(args.config_file)
    try:
        monitor = PRTGMonitor(config_path, load_state=False)
    except ConfigError as e:
        print(f"Erro de configuração: {e}", file=sys.stderr)
        return 1

    refresh_interval = args.interval if args.interval is not None else monitor.config.get("poll_interval_seconds", 60)
    state = TUIState(
        mode="state" if args.from_state else "live",
        prtg_url=monitor.config.get("prtg_url", ""),
        config_path=config_path,
        state_path=monitor.state_path,
        refresh_interval=refresh_interval,
        alert_statuses=monitor.config.get("filter_statuses", [5, 14]),
    )
    runner = TUIRunner(monitor, state)
    runner.prime_from_state()

    os.environ.setdefault("ESCDELAY", "25")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    use_color = not args.no_color and not os.environ.get("NO_COLOR")
    previous_handler = set_log_handler(runner.log_sink)
    try:
        curses.wrapper(_run_curses_loop, runner, use_color)
    except KeyboardInterrupt:
        pass
    finally:
        set_log_handler(previous_handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
