#!/usr/bin/env python3
import html
import json
import math
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from prtg_monitor import redact_secrets

HISTORY_WINDOWS = (
    ("1h", 1, 60),
    ("2h", 2, 300),
    ("24h", 24, 900),
    ("7d", 168, 3600),
    ("30d", 720, 14400),
)
LOG_RANGES = (
    ("today", "Hoje"),
    ("7days", "7 dias"),
    ("30days", "30 dias"),
)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_OLE_PREFIX_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(\[.*\])?\s*$")
_OLE_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)
_HISTORIC_META_KEYS = frozenset({"datetime", "datetime_raw", "coverage", "coverage_raw"})


def strip_html(text):
    """Strips HTML tags, unescapes entities, and normalizes whitespace."""
    if text is None:
        return ""
    plain = _HTML_TAG_RE.sub(" ", str(text))
    plain = html.unescape(plain)
    return " ".join(plain.split())


def format_prtg_ole_datetime(text):
    """Converts PRTG OLE Automation date strings (e.g. '46345.57 [51 s ago]') to readable local time."""
    cleaned = strip_html(text)
    if not cleaned:
        return ""
    match = _OLE_PREFIX_RE.match(cleaned)
    if not match:
        return cleaned
    try:
        ole_days = float(match.group(1))
        if not math.isfinite(ole_days) or ole_days < 1 or ole_days > 100000:
            return cleaned
        dt = (_OLE_EPOCH + timedelta(days=ole_days)).astimezone()
        formatted = dt.strftime("%Y-%m-%d %H:%M:%S")
        suffix = f" {match.group(2)}" if match.group(2) else ""
        return f"{formatted}{suffix}"
    except (OverflowError, OSError, ValueError):
        return cleaned


def safe_int(val, default=0):
    if isinstance(val, bool) or val is None:
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float) and math.isfinite(val):
        return int(val)
    try:
        return int(str(val).strip())
    except ValueError:
        return default


def safe_float(val, default=None):
    if isinstance(val, bool) or val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(val) else default
    text = str(val).strip()
    if not text:
        return default
    try:
        num = float(text)
        return num if math.isfinite(num) else default
    except ValueError:
        return default


def format_human_number(value):
    """Formats numeric metric values compactly for terminal axes and summaries."""
    num = safe_float(value)
    if num is None:
        return "-"
    abs_num = abs(num)
    if abs_num == 0:
        return "0"
    if abs_num >= 1e12:
        return f"{num / 1e12:.2f}T"
    if abs_num >= 1e9:
        return f"{num / 1e9:.2f}G"
    if abs_num >= 1e6:
        return f"{num / 1e6:.2f}M"
    if abs_num >= 1e4:
        return f"{num / 1e3:.1f}K"
    if abs_num >= 100:
        return f"{num:.1f}"
    if abs_num >= 1:
        return f"{num:.2f}"
    return f"{num:.3g}"


def extract_historic_series(histdata):
    """Normalizes PRTG historicdata rows into per-channel numeric series and stats."""
    points = histdata if isinstance(histdata, list) else []
    timestamps = []
    coverage = []
    channels = []
    seen_channels = set()

    for pt in points:
        if not isinstance(pt, dict):
            continue
        timestamps.append(strip_html(pt.get("datetime", "")))
        coverage.append(strip_html(pt.get("coverage", "")))
        for key, val in pt.items():
            if not key or key in _HISTORIC_META_KEYS or key.endswith("_raw"):
                continue
            if safe_float(val) is not None and key not in seen_channels:
                seen_channels.add(key)
                channels.append(key)

    series = {ch: [] for ch in channels}
    for pt in points:
        if not isinstance(pt, dict):
            continue
        for ch in channels:
            series[ch].append(safe_float(pt.get(ch)))

    stats = {}
    for ch, vals in series.items():
        valid = [v for v in vals if v is not None]
        if valid:
            stats[ch] = {
                "min": min(valid),
                "max": max(valid),
                "avg": sum(valid) / len(valid),
                "last": valid[-1],
                "count": len(valid),
            }
        else:
            stats[ch] = {"min": None, "max": None, "avg": None, "last": None, "count": 0}

    return {
        "timestamps": timestamps,
        "coverage": coverage,
        "channels": channels,
        "series": series,
        "stats": stats,
        "raw_points": points,
    }


class PRTGClient:
    """Read-only JSON API client for exploring PRTG sensors, devices, groups, channels, and history."""

    def __init__(self, prtg_url, apitoken, verify_ssl=True, timeout=15):
        self.prtg_url = str(prtg_url or "").rstrip("/")
        self.apitoken = str(apitoken or "")
        self.verify_ssl = bool(verify_ssl)
        self.timeout = timeout

    @classmethod
    def from_config(cls, config):
        return cls(
            prtg_url=config.get("prtg_url", ""),
            apitoken=config.get("apitoken", ""),
            verify_ssl=config.get("verify_ssl", True),
            timeout=config.get("request_timeout_seconds", 15),
        )

    def _build_ssl_context(self):
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _fetch_json(self, path, params=None, timeout=None):
        query = dict(params or {})
        query["apitoken"] = self.apitoken
        url = f"{self.prtg_url}{path}?{urllib.parse.urlencode(query, doseq=True)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PRTG-Mac-Monitor/1.0"},
        )
        req_timeout = self.timeout if timeout is None else timeout
        ctx = self._build_ssl_context()

        try:
            with urllib.request.urlopen(req, context=ctx, timeout=req_timeout) as response:
                if response.status != 200:
                    return None, f"PRTG HTTP {response.status}"
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    return None, "Resposta da API excedeu o limite máximo."
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and payload.get("Error"):
                return None, f"PRTG: {redact_secrets(payload.get('Error'), self.apitoken)}"
            return payload, None
        except urllib.error.HTTPError as e:
            return None, f"PRTG HTTP {e.code}: {redact_secrets(e.reason, self.apitoken)}"
        except urllib.error.URLError as e:
            return None, f"Erro de rede: {redact_secrets(e.reason, self.apitoken)}"
        except json.JSONDecodeError as e:
            return None, f"JSON inválido do PRTG: {e}"
        except Exception as e:
            return None, f"Erro inesperado: {redact_secrets(e, self.apitoken)}"

    def _fetch_paginated_table(self, content, columns, extra_params=None, page_size=500, max_pages=20):
        collected = []
        start = 0
        for _ in range(max_pages):
            params = {
                "content": content,
                "columns": columns,
                "start": str(start),
                "count": str(page_size),
            }
            if extra_params:
                params.update(extra_params)

            data, err = self._fetch_json("/api/table.json", params=params)
            if err is not None:
                return None, err
            if not isinstance(data, dict):
                return None, f"Resposta inválida para {content}."

            rows = data.get(content)
            if not isinstance(rows, list):
                return None, f"Campo '{content}' ausente na resposta da API."

            collected.extend(rows)
            treesize = safe_int(data.get("treesize"), len(collected))
            start += len(rows)
            if len(rows) < page_size or not rows or start >= treesize:
                return collected, None

        return None, f"Paginação de {content} excedeu o limite de páginas."

    def fetch_server_status(self):
        data, err = self._fetch_json("/api/getstatus.htm", timeout=10)
        if err is not None:
            return None, err
        if not isinstance(data, dict):
            return None, "Resposta inválida em getstatus.htm."
        return data, None

    def fetch_all_sensors(self):
        columns = (
            "objid,probe,group,device,sensor,status,message,lastvalue,"
            "priority,uptime,downtime,lastcheck,lastup,lastdown,tags"
        )
        rows, err = self._fetch_paginated_table(
            "sensors",
            columns=columns,
            extra_params={"sortby": "objid"},
        )
        if err is not None:
            return None, err

        sensors = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            objid = str(item.get("objid", "")).strip()
            if not objid:
                continue
            message = strip_html(item.get("message_raw") or item.get("message") or "")
            sensors.append({
                "objid": objid,
                "probe": strip_html(item.get("probe_raw") or item.get("probe") or ""),
                "group": strip_html(item.get("group_raw") or item.get("group") or ""),
                "device": strip_html(item.get("device_raw") or item.get("device") or ""),
                "sensor": strip_html(item.get("sensor_raw") or item.get("sensor") or ""),
                "status": strip_html(item.get("status") or ""),
                "status_raw": safe_int(item.get("status_raw"), 0),
                "message": message,
                "lastvalue": strip_html(item.get("lastvalue") or ""),
                "priority": safe_int(item.get("priority_raw", item.get("priority")), 3),
                "uptime": strip_html(item.get("uptime") or ""),
                "downtime": strip_html(item.get("downtime") or ""),
                "lastcheck": strip_html(item.get("lastcheck") or ""),
                "lastup": strip_html(item.get("lastup") or ""),
                "lastdown": strip_html(item.get("lastdown") or ""),
                "tags": strip_html(item.get("tags") or ""),
            })
        return sensors, None

    def fetch_devices(self):
        columns = (
            "objid,probe,group,device,host,status,"
            "downsens,partialdownsens,warnsens,upsens,pausedsens,unusualsens,tags"
        )
        rows, err = self._fetch_paginated_table(
            "devices",
            columns=columns,
            extra_params={"sortby": "device"},
        )
        if err is not None:
            return None, err

        devices = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            objid = str(item.get("objid", "")).strip()
            if not objid:
                continue
            devices.append({
                "objid": objid,
                "probe": strip_html(item.get("probe_raw") or item.get("probe") or ""),
                "group": strip_html(item.get("group_raw") or item.get("group") or ""),
                "device": strip_html(item.get("device_raw") or item.get("device") or ""),
                "host": strip_html(item.get("host_raw") or item.get("host") or ""),
                "status": strip_html(item.get("status") or ""),
                "status_raw": safe_int(item.get("status_raw"), 0),
                "down": safe_int(item.get("downsens_raw"), 0),
                "partial": safe_int(item.get("partialdownsens_raw"), 0),
                "warn": safe_int(item.get("warnsens_raw"), 0),
                "up": safe_int(item.get("upsens_raw"), 0),
                "paused": safe_int(item.get("pausedsens_raw"), 0),
                "unusual": safe_int(item.get("unusualsens_raw"), 0),
                "tags": strip_html(item.get("tags") or ""),
            })
        return devices, None

    def fetch_groups(self):
        columns = "objid,probe,group,status,downsens,warnsens,upsens,pausedsens"
        rows, err = self._fetch_paginated_table(
            "groups",
            columns=columns,
            extra_params={"sortby": "group"},
        )
        if err is not None:
            return None, err

        groups = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            objid = str(item.get("objid", "")).strip()
            if not objid:
                continue
            groups.append({
                "objid": objid,
                "probe": strip_html(item.get("probe_raw") or item.get("probe") or ""),
                "group": strip_html(item.get("group_raw") or item.get("group") or ""),
                "status": strip_html(item.get("status") or ""),
                "status_raw": safe_int(item.get("status_raw"), 0),
                "down": safe_int(item.get("downsens_raw"), 0),
                "warn": safe_int(item.get("warnsens_raw"), 0),
                "up": safe_int(item.get("upsens_raw"), 0),
                "paused": safe_int(item.get("pausedsens_raw"), 0),
            })
        return groups, None

    def fetch_sensor_details(self, objid):
        data, err = self._fetch_json("/api/getsensordetails.json", params={"id": str(objid)})
        if err is not None:
            return None, err
        raw = data.get("sensordata") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return None, f"Detalhes indisponíveis para o sensor {objid}."

        return {
            "objid": str(objid),
            "name": strip_html(raw.get("name") or ""),
            "sensortype": strip_html(raw.get("sensortype") or ""),
            "probename": strip_html(raw.get("probename") or ""),
            "parentgroupname": strip_html(raw.get("parentgroupname") or ""),
            "parentdevicename": strip_html(raw.get("parentdevicename") or ""),
            "parentdeviceid": str(raw.get("parentdeviceid") or ""),
            "interval": strip_html(raw.get("interval") or ""),
            "statusid": safe_int(raw.get("statusid"), 0),
            "statustext": strip_html(raw.get("statustext") or ""),
            "uptime": strip_html(raw.get("uptime") or ""),
            "uptimetime": strip_html(raw.get("uptimetime") or ""),
            "downtime": strip_html(raw.get("downtime") or ""),
            "downtimetime": strip_html(raw.get("downtimetime") or ""),
            "updownsince": format_prtg_ole_datetime(raw.get("updownsince") or ""),
            "lastcheck": format_prtg_ole_datetime(raw.get("lastcheck") or ""),
            "lastup": format_prtg_ole_datetime(raw.get("lastup") or ""),
            "lastdown": format_prtg_ole_datetime(raw.get("lastdown") or ""),
            "lastvalue": strip_html(raw.get("lastvalue") or ""),
            "lastmessage": strip_html(raw.get("lastmessage") or ""),
            "info": strip_html(raw.get("info") or ""),
        }, None

    def fetch_sensor_channels(self, objid):
        data, err = self._fetch_json(
            "/api/table.json",
            params={
                "content": "channels",
                "id": str(objid),
                "columns": "name,lastvalue,lastvalue_raw",
                "count": "200",
            },
        )
        if err is not None:
            return None, err
        rows = data.get("channels") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None, f"Canais indisponíveis para o sensor {objid}."

        channels = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            name = strip_html(item.get("name_raw") or item.get("name") or "")
            if not name:
                continue
            channels.append({
                "name": name,
                "lastvalue": strip_html(item.get("lastvalue") or "") or "-",
                "lastvalue_raw": safe_float(item.get("lastvalue_raw")),
            })
        return channels, None

    def fetch_historic_data(self, objid, window_key="24h", now=None):
        window_map = {key: (hours, avg) for key, hours, avg in HISTORY_WINDOWS}
        hours, avg = window_map.get(window_key, (24, 900))
        end_dt = datetime.now() if now is None else now
        start_dt = end_dt - timedelta(hours=hours)

        data, err = self._fetch_json(
            "/api/historicdata.json",
            params={
                "id": str(objid),
                "avg": str(avg),
                "sdate": start_dt.strftime("%Y-%m-%d-%H-%M-%S"),
                "edate": end_dt.strftime("%Y-%m-%d-%H-%M-%S"),
                "usecaption": "1",
            },
            timeout=20,
        )
        if err is not None:
            return None, err
        if not isinstance(data, dict) or not isinstance(data.get("histdata"), list):
            return None, f"Histórico indisponível para o sensor {objid}."
        return extract_historic_series(data.get("histdata", [])), None

    def fetch_messages(self, objid=None, drel="7days", count=150):
        params = {
            "content": "messages",
            "columns": "objid,datetime,parent,type,name,status,message",
            "filter_drel": drel,
            "count": str(count),
            "sortby": "-datetime",
        }
        if objid is not None:
            params["id"] = str(objid)

        data, err = self._fetch_json("/api/table.json", params=params)
        if err is not None:
            return None, err
        rows = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None, "Logs indisponíveis na resposta da API."

        messages = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            messages.append({
                "objid": str(item.get("objid", "")).strip(),
                "datetime": strip_html(item.get("datetime") or ""),
                "parent": strip_html(item.get("parent_raw") or item.get("parent") or ""),
                "type": strip_html(item.get("type_raw") or item.get("type") or ""),
                "name": strip_html(item.get("name_raw") or item.get("name") or ""),
                "status": strip_html(item.get("status") or ""),
                "status_raw": safe_int(item.get("status_raw"), 0),
                "message": strip_html(item.get("message_raw") or item.get("message") or ""),
            })
        return messages, None
