#!/usr/bin/env python3
import os
import sys
import json
import time
import ssl
import subprocess
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

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

def log(message, level="info"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if level == "info":
        print(f"[{timestamp}] {Colors.OKBLUE}[INFO]{Colors.ENDC} {message}")
    elif level == "success":
        print(f"[{timestamp}] {Colors.OKGREEN}[SUCCESS]{Colors.ENDC} {message}")
    elif level == "warning":
        print(f"[{timestamp}] {Colors.WARNING}[WARNING]{Colors.ENDC} {message}")
    elif level == "error":
        print(f"[{timestamp}] {Colors.FAIL}[ERROR]{Colors.ENDC} {Colors.BOLD}{message}{Colors.ENDC}")
    elif level == "alert":
        print(f"[{timestamp}] {Colors.FAIL}{Colors.BOLD}[ALERT]{Colors.ENDC} {message}")

def escape_applescript_string(s):
    """Escapes backslashes and double quotes for AppleScript."""
    if not s:
        return ""
    return s.replace('\\', '\\\\').replace('"', '\\"')

def trigger_macos_notification(title, subtitle, message, sound_name=None):
    """Triggers a macOS system notification banner."""
    title_esc = escape_applescript_string(title)
    subtitle_esc = escape_applescript_string(subtitle)
    message_esc = escape_applescript_string(message)
    
    script = f'display notification "{message_esc}" with title "{title_esc}" subtitle "{subtitle_esc}"'
    if sound_name:
        script += f' sound name "{sound_name}"'
        
    try:
        subprocess.run(["osascript", "-e", script], check=True)
    except subprocess.SubprocessError as e:
        log(f"Failed to trigger macOS notification: {e}", "warning")

def trigger_macos_alert(title, message):
    """Triggers a blocking macOS dialog alert."""
    title_esc = escape_applescript_string(title)
    message_esc = escape_applescript_string(message)
    
    script = f'display dialog "{message_esc}" with title "{title_esc}" buttons {{"OK"}} default button "OK" with icon caution'
    try:
        # We run this in the background to avoid blocking the monitor loop
        subprocess.Popen(["osascript", "-e", script])
    except subprocess.SubprocessError as e:
        log(f"Failed to trigger macOS alert dialog: {e}", "warning")

class PRTGMonitor:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.config = {}
        self.active_alerts = {}  # Tracks currently down sensors: {objid: {sensor_info}}
        self.first_run = True
        self.load_config()

    def load_config(self):
        if not os.path.exists(self.config_path):
            log(f"Configuration file '{self.config_path}' not found! Creating default...", "warning")
            self.create_default_config()
            
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            log("Configuration loaded successfully.", "info")
            # Mask API token for security in logs
            token = self.config.get("apitoken", "")
            masked_token = token[:6] + "..." if len(token) > 6 else token
            log(f"PRTG Server: {self.config.get('prtg_url')}", "info")
            log(f"API Token: {masked_token}", "info")
        except Exception as e:
            log(f"Failed to read configuration: {e}. Exiting.", "error")
            sys.exit(1)

    def create_default_config(self):
        default_conf = {
            "prtg_url": "https://your-prtg-server.example.com",
            "apitoken": "YOUR_API_TOKEN_HERE",
            "poll_interval_seconds": 60,
            "filter_statuses": [5, 14],  # 5 = Down, 14 = Down Partial
            "verify_ssl": False,
            "notification_type": "banner", # 'banner', 'alert', or 'both'
            "sound_name": "Glass",
            "notify_on_resolve": True
        }
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(default_conf, f, indent=2)
            log(f"Default configuration written to '{self.config_path}'. Please configure it and restart.", "warning")
        except Exception as e:
            log(f"Failed to write default config: {e}", "error")

    def build_api_url(self):
        prtg_url = self.config.get("prtg_url", "").rstrip("/")
        apitoken = self.config.get("apitoken", "")
        filter_statuses = self.config.get("filter_statuses", [5])
        
        # Build query parameters
        params = [
            ('content', 'sensors'),
            ('columns', 'objid,sensor,device,status,message,lastvalue'),
            ('apitoken', apitoken)
        ]
        # Append status filters
        for status in filter_statuses:
            params.append(('filter_status', str(status)))
            
        query_string = urllib.parse.urlencode(params)
        return f"{prtg_url}/api/table.xml?{query_string}"

    def fetch_active_errors(self):
        url = self.build_api_url()
        verify_ssl = self.config.get("verify_ssl", False)
        
        # Create SSL context based on verification preference
        if not verify_ssl:
            ctx = ssl._create_unverified_context()
        else:
            ctx = ssl.create_default_context()
            
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'PRTG-Mac-Monitor/1.0'}
        )
        
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as response:
                if response.status != 200:
                    log(f"PRTG Server responded with status code: {response.status}", "error")
                    return None
                return response.read()
        except urllib.error.URLError as e:
            log(f"Network error querying PRTG server: {e.reason}", "error")
            return None
        except Exception as e:
            log(f"Unexpected error fetching PRTG data: {e}", "error")
            return None

    def parse_xml_sensors(self, xml_data):
        if not xml_data:
            return []
            
        try:
            root = ET.fromstring(xml_data)
            sensors = []
            
            # Look for item tags containing sensor info
            for item in root.findall('.//item'):
                sensor_info = {
                    'objid': item.findtext('objid', '').strip(),
                    'sensor': item.findtext('sensor', '').strip(),
                    'device': item.findtext('device', '').strip(),
                    'status': item.findtext('status', '').strip(),
                    'message': item.findtext('message', '').strip(),
                    'lastvalue': item.findtext('lastvalue', '').strip()
                }
                if sensor_info['objid']:
                    sensors.append(sensor_info)
            return sensors
        except ET.ParseError as e:
            log(f"XML Parsing failed. PRTG response might contain an error. Details: {e}", "error")
            # Attempt to extract error message if PRTG returned an error XML format
            try:
                err_root = ET.fromstring(xml_data)
                err_msg = err_root.findtext('error')
                if err_msg:
                    log(f"PRTG Error: {err_msg}", "error")
            except Exception:
                pass
            return []

    def notify_alert(self, sensor):
        objid = sensor['objid']
        name = sensor['sensor']
        device = sensor['device']
        msg = sensor['message']
        status = sensor['status']
        
        title = f"🔴 Alerta PRTG: {status}"
        subtitle = f"{device} ➔ {name}"
        alert_body = f"Sensor: {name}\nDispositivo: {device}\nMensagem: {msg}"
        
        log(f"Alerta Ativo - Sensor: {name} | Dispositivo: {device} | Status: {status} | Msg: {msg}", "alert")
        prtg_url = self.config.get("prtg_url", "").rstrip("/")
        sensor_link = f"{prtg_url}/sensor.htm?id={objid}"
        print(f"   ➔ Link para o sensor: {Colors.UNDERLINE}{sensor_link}{Colors.ENDC}")
        
        notification_type = self.config.get("notification_type", "banner").lower()
        sound = self.config.get("sound_name", "Glass")
        
        if notification_type in ("banner", "both"):
            trigger_macos_notification(title, subtitle, msg, sound)
        if notification_type in ("alert", "both"):
            trigger_macos_alert(title, alert_body)

    def notify_resolution(self, sensor):
        name = sensor['sensor']
        device = sensor['device']
        
        title = "🟢 Alerta PRTG Resolvido"
        subtitle = f"{device} ➔ {name}"
        msg = "O sensor retornou ao estado normal."
        
        log(f"Alerta Resolvido - Sensor: {name} | Dispositivo: {device}", "success")
        
        notification_type = self.config.get("notification_type", "banner").lower()
        if notification_type in ("banner", "both"):
            trigger_macos_notification(title, subtitle, msg, "Blow")

    def run_cycle(self):
        log("Checking PRTG for active error alerts...", "info")
        xml_data = self.fetch_active_errors()
        
        if xml_data is None:
            # Fetch failed, skip logic to avoid false recovery notifications
            return
            
        current_sensors = self.parse_xml_sensors(xml_data)
        current_sensor_ids = set()
        
        # Process active alerts
        for sensor in current_sensors:
            objid = sensor['objid']
            current_sensor_ids.add(objid)
            
            if objid not in self.active_alerts:
                # New alert detected!
                self.active_alerts[objid] = sensor
                # On first run, we list the existing alerts but might not want to flood notifications.
                # Let's notify anyway, or just log them. We'll send notifications by default.
                self.notify_alert(sensor)
            else:
                # Update stored sensor info in case message changes
                self.active_alerts[objid] = sensor

        # Process resolved alerts
        resolved_ids = list(self.active_alerts.keys())
        for objid in resolved_ids:
            if objid not in current_sensor_ids:
                # Alert has been resolved
                resolved_sensor = self.active_alerts.pop(objid)
                if self.config.get("notify_on_resolve", True) and not self.first_run:
                    self.notify_resolution(resolved_sensor)
                    
        self.first_run = False
        log(f"Cycle completed. Active error count: {len(self.active_alerts)}", "info")

    def start(self):
        interval = self.config.get("poll_interval_seconds", 60)
        log(f"Starting PRTG monitor loop. Interval: {interval} seconds.", "info")
        
        # Test macOS notification on startup
        trigger_macos_notification(
            "PRTG Monitor Iniciado", 
            "Monitoramento Ativo", 
            f"Buscando erros a cada {interval} segundos.",
            self.config.get("sound_name")
        )
        
        try:
            while True:
                self.run_cycle()
                time.sleep(interval)
        except KeyboardInterrupt:
            log("Monitor stopped by user.", "info")
            sys.exit(0)

if __name__ == "__main__":
    # Allow passing custom config path via argument
    config_file = "config.json"
    if len(sys.argv) > 1:
        config_file = sys.argv[1]
        
    monitor = PRTGMonitor(config_file)
    monitor.start()
