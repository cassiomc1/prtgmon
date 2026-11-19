#!/usr/bin/env python3
import argparse
import json
import math
import os
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

OSASCRIPT_TIMEOUT_SECONDS = 10
VALID_NOTIFICATION_TYPES = frozenset({"banner", "alert", "both"})
VALID_ALERT_ICONS = frozenset({"caution", "note", "stop"})
PLACEHOLDER_URL = "https://your-prtg-server.example.com"
PLACEHOLDER_TOKEN = "YOUR_API_TOKEN_HERE"
_APITOKEN_QUERY_RE = re.compile(r"(apitoken=)[^&\s'\"]+", re.IGNORECASE)
_active_alert_processes = []
_log_handler = None


def set_log_handler(handler):
    """Installs an optional log sink callback and returns the previous handler."""
    global _log_handler
    previous = _log_handler
    _log_handler = handler
    return previous


class ConfigError(ValueError):
    """Raised when the monitor configuration is missing or invalid."""


# ANSI Color Codes for terminal readability
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def _use_color():
    return bool(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))


def log(message, level="info"):
    handler = _log_handler
    if handler is not None:
        handler(message, level)
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    use_color = _use_color()
    if level == "info":
        prefix = f"{Colors.OKBLUE}[INFO]{Colors.ENDC}" if use_color else "[INFO]"
        body = message
    elif level == "success":
        prefix = f"{Colors.OKGREEN}[SUCCESS]{Colors.ENDC}" if use_color else "[SUCCESS]"
        body = message
    elif level == "warning":
        prefix = f"{Colors.WARNING}[WARNING]{Colors.ENDC}" if use_color else "[WARNING]"
        body = message
    elif level == "error":
        prefix = f"{Colors.FAIL}[ERROR]{Colors.ENDC}" if use_color else "[ERROR]"
        body = f"{Colors.BOLD}{message}{Colors.ENDC}" if use_color else message
    elif level == "alert":
        prefix = f"{Colors.FAIL}{Colors.BOLD}[ALERT]{Colors.ENDC}" if use_color else "[ALERT]"
        body = message
    else:
        prefix = f"[{level.upper()}]"
        body = message
    print(f"[{timestamp}] {prefix} {body}", flush=True)


def redact_secrets(value, secret=None):
    """Redacts PRTG API tokens from strings or exception messages before logging."""
    text = _APITOKEN_QUERY_RE.sub(r"\1REDACTED", str(value))
    if secret and isinstance(secret, str):
        text = text.replace(secret, "REDACTED")
    return text


def escape_applescript_string(s):
    """Escapes backslashes, double quotes, and control characters for AppleScript."""
    if not s:
        return ""
    return (
        str(s)
        .replace('\x00', '')
        .replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\r', '\\r')
        .replace('\n', '\\n')
        .replace('\t', '\\t')
    )


def _sanitize_osascript_arg(value):
    if value is None:
        return ""
    return str(value).replace("\x00", "")


def reap_alert_processes():
    """Removes completed osascript dialog subprocesses to prevent zombies."""
    _active_alert_processes[:] = [
        proc for proc in _active_alert_processes if proc.poll() is None
    ]


def trigger_macos_notification(title, subtitle, message, sound_name=None):
    """Triggers a macOS system notification banner safely via osascript argv."""
    message_arg = _sanitize_osascript_arg(message)
    title_arg = _sanitize_osascript_arg(title)
    subtitle_arg = _sanitize_osascript_arg(subtitle)
    args = [message_arg, title_arg, subtitle_arg]

    if sound_name:
        sound_arg = _sanitize_osascript_arg(sound_name)
        script = (
            "on run argv\n"
            "display notification (item 1 of argv) with title (item 2 of argv) "
            "subtitle (item 3 of argv) sound name (item 4 of argv)\n"
            "end run"
        )
        args.append(sound_arg)
    else:
        script = (
            "on run argv\n"
            "display notification (item 1 of argv) with title (item 2 of argv) "
            "subtitle (item 3 of argv)\n"
            "end run"
        )

    try:
        subprocess.run(
            ["osascript", "-e", script, "--", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT_SECONDS,
        )
        return True
    except (subprocess.SubprocessError, OSError) as e:
        stderr = getattr(e, "stderr", "")
        detail = f" ({stderr.strip()})" if isinstance(stderr, str) and stderr.strip() else ""
        log(f"Failed to trigger macOS notification: {e}{detail}", "warning")
        return False


def trigger_macos_alert(title, message, icon="caution"):
    """Triggers a non-blocking macOS dialog alert safely via osascript argv."""
    reap_alert_processes()
    if icon not in VALID_ALERT_ICONS:
        icon = "caution"

    message_arg = _sanitize_osascript_arg(message)
    title_arg = _sanitize_osascript_arg(title)
    script = (
        "on run argv\n"
        "display dialog (item 1 of argv) with title (item 2 of argv) "
        f'buttons {{"OK"}} default button "OK" with icon {icon}\n'
        "end run"
    )

    try:
        # Run in the background so modal user confirmation does not stall polling.
        proc = subprocess.Popen(
            ["osascript", "-e", script, "--", message_arg, title_arg],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _active_alert_processes.append(proc)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        log(f"Failed to trigger macOS alert dialog: {e}", "warning")
        return False


class PRTGMonitor:
    API_PAGE_SIZE = 500
    MAX_PAGES = 1000
    MAX_RESPONSE_BYTES = 10 * 1024 * 1024
    DEFAULT_REQUEST_TIMEOUT = 15
    STATUS_LOOKUP_LIMIT = 10
    STATUS_LOOKUP_TIMEOUT = 5
    STATUS_UP = 3
    STATE_VERSION = 1
    DEFAULT_CONFIG = {
        "prtg_url": PLACEHOLDER_URL,
        "apitoken": PLACEHOLDER_TOKEN,
        "poll_interval_seconds": 60,
        "filter_statuses": [5, 14],  # 5 = Down, 14 = Down Partial
        "verify_ssl": True,
        "notification_type": "banner",  # 'banner', 'alert', or 'both'
        "sound_name": "Glass",
        "notify_on_resolve": True,
        "notify_on_startup": True,
        "state_file": "state.json",
    }

    def __init__(self, config_path="config.json", load_state=True):
        self.config_path = config_path
        self.config = {}
        self.active_alerts = {}  # Tracks currently down sensors: {objid: {sensor_info}}
        self.first_run = True
        self.status_lookup_offset = 0
        self.state_path = None
        self._stop_event = threading.Event()
        self.load_config()
        if load_state:
            self.load_state()

    def load_config(self):
        if not os.path.exists(self.config_path):
            log(f"Configuration file '{self.config_path}' not found! Creating default...", "warning")
            self.create_default_config()
            raise ConfigError(
                f"Configuration file '{self.config_path}' was created with defaults; "
                "configure PRTG URL and API token before starting."
            )

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw_config = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log(f"Failed to read configuration: {e}", "error")
            raise ConfigError(f"Failed to read configuration '{self.config_path}': {e}") from e

        self.config = self.validate_config(raw_config)

        state_file = self.config.get("state_file")
        if state_file:
            expanded = os.path.expanduser(state_file)
            if os.path.isabs(expanded):
                self.state_path = expanded
            else:
                config_dir = os.path.dirname(os.path.abspath(self.config_path))
                self.state_path = os.path.join(config_dir, expanded)
        else:
            self.state_path = None

        log("Configuration loaded successfully.", "info")
        log(f"PRTG Server: {self.config['prtg_url']}", "info")
        log("API Token: *** (configured)", "info")

    @staticmethod
    def validate_config(raw_config):
        """Validates and normalizes configuration values."""
        if not isinstance(raw_config, dict):
            raise ConfigError("Configuration must be a JSON object.")

        prtg_url = raw_config.get("prtg_url")
        if not isinstance(prtg_url, str) or not prtg_url.strip():
            raise ConfigError("Configuration 'prtg_url' must be a non-empty string.")
        prtg_url = prtg_url.strip().rstrip("/")
        parsed_url = urllib.parse.urlsplit(prtg_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise ConfigError("Configuration 'prtg_url' must be a valid http:// or https:// URL.")
        if prtg_url.lower() == PLACEHOLDER_URL.lower():
            raise ConfigError("Configuration 'prtg_url' is still set to the example placeholder.")
        if parsed_url.scheme == "http":
            log("Configuration 'prtg_url' uses unencrypted HTTP; prefer HTTPS in production.", "warning")

        apitoken = raw_config.get("apitoken")
        if not isinstance(apitoken, str) or not apitoken.strip():
            raise ConfigError("Configuration 'apitoken' must be a non-empty string.")
        apitoken = apitoken.strip()
        if apitoken == PLACEHOLDER_TOKEN:
            raise ConfigError("Configuration 'apitoken' is still set to the example placeholder.")

        poll_interval = raw_config.get("poll_interval_seconds", 60)
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or not math.isfinite(poll_interval)
            or poll_interval <= 0
        ):
            raise ConfigError("Configuration 'poll_interval_seconds' must be a positive finite number.")
        if poll_interval < 10:
            log("Configuration 'poll_interval_seconds' is under 10s; high polling frequency may load PRTG.", "warning")

        filter_statuses = raw_config.get("filter_statuses", [5, 14])
        if not isinstance(filter_statuses, list) or not filter_statuses:
            raise ConfigError("Configuration 'filter_statuses' must be a non-empty list of integer status codes.")
        normalized_statuses = []
        seen_statuses = set()
        for status in filter_statuses:
            if isinstance(status, bool) or not isinstance(status, int):
                raise ConfigError("Configuration 'filter_statuses' entries must be integers.")
            if status not in seen_statuses:
                seen_statuses.add(status)
                normalized_statuses.append(status)

        verify_ssl = raw_config.get("verify_ssl", True)
        if not isinstance(verify_ssl, bool):
            raise ConfigError("Configuration 'verify_ssl' must be a boolean.")
        if not verify_ssl:
            log("TLS certificate verification is disabled (verify_ssl=false); use only in trusted labs.", "warning")

        notification_type = raw_config.get("notification_type", "banner")
        if not isinstance(notification_type, str):
            raise ConfigError("Configuration 'notification_type' must be a string.")
        notification_type = notification_type.strip().lower()
        if notification_type not in VALID_NOTIFICATION_TYPES:
            raise ConfigError(
                f"Configuration 'notification_type' must be one of: {', '.join(sorted(VALID_NOTIFICATION_TYPES))}."
            )

        sound_name = raw_config.get("sound_name", "Glass")
        if sound_name is not None:
            if not isinstance(sound_name, str):
                raise ConfigError("Configuration 'sound_name' must be a string or null.")
            sound_name = sound_name.strip() or None

        notify_on_resolve = raw_config.get("notify_on_resolve", True)
        if not isinstance(notify_on_resolve, bool):
            raise ConfigError("Configuration 'notify_on_resolve' must be a boolean.")

        notify_on_startup = raw_config.get("notify_on_startup", True)
        if not isinstance(notify_on_startup, bool):
            raise ConfigError("Configuration 'notify_on_startup' must be a boolean.")

        if "state_file" in raw_config:
            state_file = raw_config["state_file"]
            if state_file is not None:
                if not isinstance(state_file, str):
                    raise ConfigError("Configuration 'state_file' must be a string or null.")
                state_file = state_file.strip() or None
        else:
            state_file = "state.json"

        normalized = {
            "prtg_url": prtg_url,
            "apitoken": apitoken,
            "poll_interval_seconds": poll_interval,
            "filter_statuses": normalized_statuses,
            "verify_ssl": verify_ssl,
            "notification_type": notification_type,
            "sound_name": sound_name,
            "notify_on_resolve": notify_on_resolve,
            "notify_on_startup": notify_on_startup,
            "state_file": state_file,
        }

        if "resolve_sound_name" in raw_config:
            resolve_sound = raw_config["resolve_sound_name"]
            if resolve_sound is not None:
                if not isinstance(resolve_sound, str):
                    raise ConfigError("Configuration 'resolve_sound_name' must be a string or null.")
                resolve_sound = resolve_sound.strip() or None
            normalized["resolve_sound_name"] = resolve_sound

        if "request_timeout_seconds" in raw_config:
            req_timeout = raw_config["request_timeout_seconds"]
            if (
                isinstance(req_timeout, bool)
                or not isinstance(req_timeout, (int, float))
                or not math.isfinite(req_timeout)
                or req_timeout <= 0
            ):
                raise ConfigError("Configuration 'request_timeout_seconds' must be a positive finite number.")
            normalized["request_timeout_seconds"] = req_timeout

        if "status_lookup_limit" in raw_config:
            lookup_limit = raw_config["status_lookup_limit"]
            if isinstance(lookup_limit, bool) or not isinstance(lookup_limit, int) or lookup_limit < 1:
                raise ConfigError("Configuration 'status_lookup_limit' must be an integer >= 1.")
            normalized["status_lookup_limit"] = lookup_limit

        if "status_lookup_timeout_seconds" in raw_config:
            lookup_timeout = raw_config["status_lookup_timeout_seconds"]
            if (
                isinstance(lookup_timeout, bool)
                or not isinstance(lookup_timeout, (int, float))
                or not math.isfinite(lookup_timeout)
                or lookup_timeout <= 0
            ):
                raise ConfigError("Configuration 'status_lookup_timeout_seconds' must be a positive finite number.")
            normalized["status_lookup_timeout_seconds"] = lookup_timeout

        return normalized

    def create_default_config(self):
        parent_dir = os.path.dirname(self.config_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        try:
            fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.DEFAULT_CONFIG, f, indent=2)
                f.write("\n")
            os.chmod(self.config_path, 0o600)
            log(f"Default configuration written to '{self.config_path}'. Please configure it and restart.", "warning")
        except OSError as e:
            log(f"Failed to write default config: {e}", "error")

    def load_state(self):
        """Loads persisted alert state so restarts do not re-alert or miss recoveries."""
        state_path = getattr(self, "state_path", None)
        if not state_path or not os.path.exists(state_path):
            self.active_alerts = {}
            self.status_lookup_offset = 0
            self.first_run = True
            return False

        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state root must be a JSON object")

            raw_alerts = data.get("active_alerts")
            if not isinstance(raw_alerts, dict):
                raise ValueError("active_alerts must be an object")

            normalized_alerts = {}
            for key, entry in raw_alerts.items():
                if not isinstance(key, str) or not key.strip() or not isinstance(entry, dict):
                    raise ValueError("invalid alert entry")
                objid = entry.get("objid")
                if not isinstance(objid, str) or objid.strip() != key:
                    raise ValueError("alert entry objid mismatch")
                status_raw = entry.get("status_raw", 0)
                if isinstance(status_raw, bool) or not isinstance(status_raw, int):
                    status_raw = 0
                normalized_alerts[key] = {
                    "objid": key,
                    "sensor": str(entry.get("sensor", "")),
                    "device": str(entry.get("device", "")),
                    "status": str(entry.get("status", "")),
                    "status_raw": status_raw,
                    "message": str(entry.get("message", "")),
                    "lastvalue": str(entry.get("lastvalue", "")),
                }

            offset = data.get("status_lookup_offset", 0)
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("status_lookup_offset must be a non-negative integer")

            self.active_alerts = normalized_alerts
            self.status_lookup_offset = offset
            self.first_run = False
            log(f"Loaded persisted alert state ({len(self.active_alerts)} active alert(s)).", "info")
            return True
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log(f"Failed to load persisted state '{state_path}': {e}. Starting with clean state.", "warning")
            self.active_alerts = {}
            self.status_lookup_offset = 0
            self.first_run = True
            return False

    def save_state(self, active_alerts, status_lookup_offset):
        """Atomically persists the complete next alert state to disk."""
        state_path = getattr(self, "state_path", None)
        if not state_path:
            return True

        state_dir = os.path.dirname(os.path.abspath(state_path)) or "."
        os.makedirs(state_dir, exist_ok=True)
        payload = {
            "version": self.STATE_VERSION,
            "active_alerts": active_alerts,
            "status_lookup_offset": status_lookup_offset,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".prtg_state_", suffix=".tmp", dir=state_dir)
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, state_path)
            tmp_path = None
            return True
        except OSError as e:
            log(f"Failed to persist alert state to '{state_path}': {e}", "error")
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def build_api_url(self, start=0, count=None):
        prtg_url = self.config.get("prtg_url", "").rstrip("/")
        apitoken = self.config.get("apitoken", "")
        count = count or self.API_PAGE_SIZE

        params = [
            ('content', 'sensors'),
            ('columns', 'objid,sensor,device,status,message,message_raw,lastvalue'),
            ('sortby', 'objid'),
            ('start', str(start)),
            ('count', str(count)),
            ('apitoken', apitoken),
        ]
        for status in self.config.get("filter_statuses", [5]):
            params.append(('filter_status', str(status)))

        query_string = urllib.parse.urlencode(params)
        return f"{prtg_url}/api/table.xml?{query_string}"

    def build_object_status_url(self, objid):
        prtg_url = self.config.get("prtg_url", "").rstrip("/")
        params = urllib.parse.urlencode({
            'id': str(objid),
            'name': 'status',
            'apitoken': self.config.get("apitoken", ""),
        })
        return f"{prtg_url}/api/getobjectstatus.htm?{params}"

    def fetch_xml(self, url, timeout=None):
        if timeout is None:
            timeout = self.config.get("request_timeout_seconds", self.DEFAULT_REQUEST_TIMEOUT)
        verify_ssl = self.config.get("verify_ssl", True)
        token = self.config.get("apitoken", "")

        ctx = ssl.create_default_context()
        if not verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'PRTG-Mac-Monitor/1.0'},
        )

        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
                if response.status != 200:
                    log(f"PRTG Server responded with status code: {response.status}", "error")
                    return None
                data = response.read(self.MAX_RESPONSE_BYTES + 1)
                if len(data) > self.MAX_RESPONSE_BYTES:
                    log("PRTG response exceeded maximum allowed size.", "error")
                    return None
                return data
        except urllib.error.HTTPError as e:
            log(f"PRTG server returned HTTP {e.code}: {redact_secrets(e.reason, token)}", "error")
            return None
        except urllib.error.URLError as e:
            log(f"Network error querying PRTG server: {redact_secrets(e.reason, token)}", "error")
            return None
        except Exception as e:
            log(f"Unexpected error fetching PRTG data: {redact_secrets(e, token)}", "error")
            return None

    def parse_xml_sensor_page(self, xml_data):
        if not xml_data:
            log("PRTG returned an empty response.", "error")
            return None

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log(f"XML parsing failed. Keeping the previous alert state. Details: {e}", "error")
            return None

        err_msg = (root.findtext('error') or '').strip()
        if err_msg:
            log(f"PRTG error: {redact_secrets(err_msg, self.config.get('apitoken', ''))}", "error")
            return None
        if root.tag != 'sensors':
            log(f"Unexpected PRTG XML root '{root.tag}'. Keeping the previous alert state.", "error")
            return None

        try:
            total_count = int(root.attrib['totalcount'])
        except (KeyError, TypeError, ValueError):
            log("PRTG response does not contain a valid totalcount.", "error")
            return None
        if total_count < 0:
            log("PRTG response contains a negative totalcount.", "error")
            return None
        if root.attrib.get('listend') not in ('0', '1'):
            log("PRTG response does not contain a valid listend.", "error")
            return None
        list_end = root.attrib['listend'] == '1'

        sensors = []
        for item in root.findall('./item'):
            objid = (item.findtext('objid') or '').strip()
            if not objid:
                log("PRTG response contains a sensor without objid.", "error")
                return None
            status_raw_text = (item.findtext('status_RAW') or item.findtext('status_raw') or '').strip()
            try:
                status_raw = int(status_raw_text)
            except ValueError:
                log(f"PRTG response contains an invalid status_RAW for sensor {objid}.", "error")
                return None
            message = (
                item.findtext('message_raw')
                or item.findtext('message_RAW')
                or item.findtext('message')
                or ''
            ).strip()
            sensors.append({
                'objid': objid,
                'sensor': (item.findtext('sensor') or '').strip(),
                'device': (item.findtext('device') or '').strip(),
                'status': (item.findtext('status') or '').strip(),
                'status_raw': status_raw,
                'message': message,
                'lastvalue': (item.findtext('lastvalue') or '').strip(),
            })
        return sensors, total_count, list_end

    def fetch_active_errors(self):
        sensors_by_id = {}
        expected_total = None
        start = 0
        pages_fetched = 0

        while True:
            if pages_fetched >= self.MAX_PAGES:
                log("PRTG pagination exceeded maximum page limit. Keeping the previous alert state.", "error")
                return None
            pages_fetched += 1

            url = self.build_api_url(start=start, count=self.API_PAGE_SIZE)
            xml_data = self.fetch_xml(url)
            if xml_data is None:
                return None

            page = self.parse_xml_sensor_page(xml_data)
            if page is None:
                return None
            page_sensors, total_count, list_end = page

            if expected_total is None:
                expected_total = total_count
            elif total_count != expected_total:
                log("PRTG result changed during pagination. Keeping the previous alert state.", "warning")
                return None

            for sensor in page_sensors:
                if sensor['objid'] in sensors_by_id:
                    log("PRTG pagination returned duplicate or missing sensors. Keeping the previous alert state.", "error")
                    return None
                sensors_by_id[sensor['objid']] = sensor
            start += len(page_sensors)

            if list_end:
                if start != expected_total:
                    log("PRTG pagination ended with an inconsistent item count.", "error")
                    return None
                if len(sensors_by_id) != expected_total:
                    log("PRTG pagination returned duplicate or missing sensors. Keeping the previous alert state.", "error")
                    return None
                return list(sensors_by_id.values())
            if start >= expected_total:
                log("PRTG pagination continued past totalcount.", "error")
                return None
            if not page_sensors:
                log("PRTG pagination ended before all sensors were returned.", "error")
                return None

    def fetch_sensor_status(self, objid):
        url = self.build_object_status_url(objid)
        timeout = self.config.get("status_lookup_timeout_seconds", self.STATUS_LOOKUP_TIMEOUT)
        xml_data = self.fetch_xml(url, timeout=timeout)
        if not xml_data:
            return None

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log(f"Sensor status XML parsing failed for {objid}: {e}", "error")
            return None

        err_msg = (root.findtext('error') or '').strip()
        if err_msg:
            log(
                f"PRTG error checking sensor {objid}: {redact_secrets(err_msg, self.config.get('apitoken', ''))}",
                "error",
            )
            return None
        if root.tag != 'prtg':
            log(f"Unexpected status XML root '{root.tag}' for sensor {objid}.", "error")
            return None

        try:
            return int((root.findtext('result') or '').strip())
        except ValueError:
            log(f"PRTG returned an invalid status for sensor {objid}.", "error")
            return None

    def notify_alert(self, sensor):
        objid = sensor['objid']
        name = sensor.get('sensor', '')
        device = sensor.get('device', '')
        msg = sensor.get('message', '')
        status = sensor.get('status', '')

        title = f"🔴 Alerta PRTG: {status}"
        subtitle = f"{device} ➔ {name}"
        alert_body = f"Sensor: {name}\nDispositivo: {device}\nMensagem: {msg}"

        log(f"Alerta Ativo - Sensor: {name} | Dispositivo: {device} | Status: {status} | Msg: {msg}", "alert")
        prtg_url = self.config.get("prtg_url", "").rstrip("/")
        sensor_link = f"{prtg_url}/sensor.htm?id={urllib.parse.quote(str(objid), safe='')}"
        if _use_color():
            print(f"   ➔ Link para o sensor: {Colors.UNDERLINE}{sensor_link}{Colors.ENDC}", flush=True)
        else:
            print(f"   ➔ Link para o sensor: {sensor_link}", flush=True)

        notification_type = self.config.get("notification_type", "banner").lower()
        sound = self.config.get("sound_name", "Glass")
        banner_msg = msg or f"Status: {status}"

        if notification_type in ("banner", "both"):
            trigger_macos_notification(title, subtitle, banner_msg, sound)
        if notification_type in ("alert", "both"):
            trigger_macos_alert(title, alert_body, icon="caution")

    def notify_resolution(self, sensor):
        name = sensor.get('sensor', '')
        device = sensor.get('device', '')

        title = "🟢 Alerta PRTG Resolvido"
        subtitle = f"{device} ➔ {name}"
        msg = "O sensor retornou ao estado normal."
        resolution_body = f"Sensor: {name}\nDispositivo: {device}\nStatus: {msg}"

        log(f"Alerta Resolvido - Sensor: {name} | Dispositivo: {device}", "success")

        notification_type = self.config.get("notification_type", "banner").lower()
        if "resolve_sound_name" in self.config:
            resolve_sound = self.config["resolve_sound_name"]
        else:
            resolve_sound = "Blow" if self.config.get("sound_name", "Glass") else None

        if notification_type in ("banner", "both"):
            trigger_macos_notification(title, subtitle, msg, resolve_sound)
        if notification_type in ("alert", "both"):
            trigger_macos_alert(title, resolution_body, icon="note")

    def run_cycle(self):
        reap_alert_processes()
        log("Checking PRTG for active error alerts...", "info")
        current_sensors = self.fetch_active_errors()

        if current_sensors is None:
            # Fetch or parsing failed, skip logic to avoid false recovery notifications
            return False

        # Phase 1: Compute the complete next state without mutating active state or sending alerts
        next_alerts = dict(self.active_alerts)
        new_alerts = []
        current_sensor_ids = set()

        for sensor in current_sensors:
            objid = sensor['objid']
            current_sensor_ids.add(objid)
            if objid not in next_alerts:
                new_alerts.append(sensor)
            next_alerts[objid] = sensor

        missing_ids = [
            objid for objid in self.active_alerts
            if objid not in current_sensor_ids
        ]

        resolved_alerts = []
        resolved_ids = set()
        next_offset = 0
        if missing_ids:
            lookup_limit = self.config.get("status_lookup_limit", self.STATUS_LOOKUP_LIMIT)
            offset = getattr(self, "status_lookup_offset", 0) % len(missing_ids)
            ordered_ids = missing_ids[offset:] + missing_ids[:offset]
            status_check_ids = ordered_ids[:lookup_limit]
            next_target_id = ordered_ids[len(status_check_ids)] if len(status_check_ids) < len(missing_ids) else None

            for objid in status_check_ids:
                current_status = self.fetch_sensor_status(objid)
                if current_status != self.STATUS_UP:
                    # Missing from the configured filter is not proof of recovery.
                    continue
                resolved_ids.add(objid)
                resolved_alerts.append(next_alerts.pop(objid))

            if next_target_id is not None:
                remaining_missing = [objid for objid in missing_ids if objid not in resolved_ids]
                if remaining_missing and next_target_id in remaining_missing:
                    next_offset = remaining_missing.index(next_target_id)

        # Phase 2: Persist atomically before committing in-memory state or notifying
        if not self.save_state(next_alerts, next_offset):
            log("State persistence failed. Keeping the previous alert state.", "error")
            return False

        # Phase 3: Commit in-memory state
        was_first_run = getattr(self, "first_run", False)
        self.active_alerts = next_alerts
        self.status_lookup_offset = next_offset
        self.first_run = False

        # Phase 4: Deliver notifications after state transition is committed
        for sensor in new_alerts:
            self.notify_alert(sensor)

        if self.config.get("notify_on_resolve", True) and not was_first_run:
            for sensor in resolved_alerts:
                self.notify_resolution(sensor)

        log(f"Cycle completed. Active error count: {len(self.active_alerts)}", "info")
        return True

    def stop(self):
        self._stop_event.set()

    def start(self, notify_on_startup=None):
        interval = self.config.get("poll_interval_seconds", 60)
        notification_type = self.config.get("notification_type", "banner").lower()
        should_notify_startup = (
            self.config.get("notify_on_startup", True)
            if notify_on_startup is None
            else notify_on_startup
        )

        log(f"Starting PRTG monitor loop. Interval: {interval} seconds.", "info")

        if should_notify_startup and notification_type in ("banner", "both"):
            trigger_macos_notification(
                "PRTG Monitor Iniciado",
                "Monitoramento Ativo",
                f"Buscando erros a cada {interval} segundos.",
                self.config.get("sound_name"),
            )

        previous_handlers = {}
        is_main_thread = threading.current_thread() is threading.main_thread()
        if is_main_thread:
            def _handle_signal(signum, frame):
                self._stop_event.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    previous_handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, _handle_signal)
                except (ValueError, OSError):
                    pass

        try:
            while not self._stop_event.is_set():
                try:
                    self.run_cycle()
                except Exception as e:
                    token = self.config.get("apitoken", "")
                    log(f"Unexpected error during monitoring cycle: {redact_secrets(e, token)}", "error")
                self._stop_event.wait(interval)
        finally:
            if is_main_thread:
                for sig, handler in previous_handlers.items():
                    try:
                        signal.signal(sig, handler)
                    except (ValueError, OSError):
                        pass
            reap_alert_processes()
            log("Monitor stopped.", "info")


def build_arg_parser():
    parser = argparse.ArgumentParser(description="PRTG macOS Alert Monitor")
    parser.add_argument(
        "config_file",
        nargs="?",
        default="config.json",
        help="Path to the JSON configuration file (default: config.json)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration file and exit without polling.",
    )
    parser.add_argument(
        "--no-startup-notification",
        action="store_true",
        help="Do not send the macOS notification banner on startup.",
    )
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        monitor = PRTGMonitor(args.config_file, load_state=not args.check_config)
    except ConfigError as e:
        log(str(e), "error")
        return 1

    if args.check_config:
        log("Configuration is valid.", "success")
        return 0

    if args.once:
        return 0 if monitor.run_cycle() else 1

    monitor.start(notify_on_startup=False if args.no_startup_notification else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
