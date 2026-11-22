import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import prtg_mcp
from prtg_monitor import set_log_handler


class _FakePRTGClient:
    """In-memory deterministic PRTGClient fake for MCP unit tests."""

    def __init__(self, apitoken="super-secret-prtg-token"):
        self.apitoken = apitoken
        self.fail_with = None
        self.sensors = [
            {
                "objid": "101",
                "probe": "Core",
                "group": "Links WAN",
                "device": "Router-Borda-01",
                "sensor": "Link Embratel Principal",
                "status": "Up",
                "status_raw": 3,
                "message": "OK",
                "lastvalue": "45.2 Mbit/s",
                "priority": 5,
                "uptime": "99.8%",
                "downtime": "0.2%",
                "lastcheck": "2026-11-22 06:45:00",
                "lastup": "2026-11-22 06:45:00",
                "lastdown": "2026-11-20 14:10:00",
                "tags": "wan link embratel",
            },
            {
                "objid": "102",
                "probe": "Core",
                "group": "Firewalls",
                "device": "FW-Matriz",
                "sensor": "Tunnel VPN Filial",
                "status": "Down",
                "status_raw": 5,
                "message": "Timeout after 5s",
                "lastvalue": "-",
                "priority": 4,
                "uptime": "98.0%",
                "downtime": "2.0%",
                "lastcheck": "2026-11-22 06:46:00",
                "lastup": "2026-11-22 02:10:00",
                "lastdown": "2026-11-22 02:15:00",
                "tags": "vpn firewall",
            },
            {
                "objid": "103",
                "probe": "Core",
                "group": "Links WAN",
                "device": "Switch-Core",
                "sensor": "Uplink Vivo Backup",
                "status": "Up",
                "status_raw": 3,
                "message": "OK",
                "lastvalue": "12.0 Mbit/s",
                "priority": 3,
                "uptime": "99.5%",
                "downtime": "0.5%",
                "lastcheck": "2026-11-22 06:46:00",
                "lastup": "2026-11-22 03:20:00",
                "lastdown": "2026-11-22 03:05:00",
                "tags": "wan backup",
            },
            {
                "objid": "104",
                "probe": "Core",
                "group": "Servers",
                "device": "DB-01",
                "sensor": "CPU Load",
                "status": "Warning",
                "status_raw": 4,
                "message": "High CPU 88%",
                "lastvalue": "88 %",
                "priority": 3,
                "uptime": "100%",
                "downtime": "0%",
                "lastcheck": "2026-11-22 06:46:00",
                "lastup": "2026-11-22 06:46:00",
                "lastdown": "2026-11-10 10:00:00",
                "tags": "db cpu",
            },
        ]
        self.devices = [
            {"objid": "40", "probe": "Core", "group": "Firewalls", "device": "FW-Matriz", "host": "10.0.0.1", "status": "Down", "status_raw": 5, "down": 1, "partial": 0, "warn": 0, "up": 4, "paused": 0, "unusual": 0, "tags": "fw"},
            {"objid": "41", "probe": "Core", "group": "Links WAN", "device": "Router-Borda-01", "host": "10.0.0.2", "status": "Up", "status_raw": 3, "down": 0, "partial": 0, "warn": 0, "up": 8, "paused": 0, "unusual": 0, "tags": "wan"},
        ]
        self.groups = [
            {"objid": "10", "probe": "Core", "group": "Firewalls", "status": "Down", "status_raw": 5, "down": 1, "warn": 0, "up": 4, "paused": 0},
            {"objid": "11", "probe": "Core", "group": "Links WAN", "status": "Up", "status_raw": 3, "down": 0, "warn": 0, "up": 10, "paused": 0},
        ]
        self.messages = [
            {
                "objid": "102",
                "datetime": "22.11.2026 02:15:00",
                "parent": "FW-Matriz",
                "type": "Sensor",
                "name": "Tunnel VPN Filial",
                "status": "Down",
                "status_raw": 5,
                "message": "Timeout after 5s",
            },
            {
                "objid": "103",
                "datetime": "22.11.2026 03:20:00",
                "parent": "Switch-Core",
                "type": "Sensor",
                "name": "Uplink Vivo Backup",
                "status": "Up",
                "status_raw": 3,
                "message": "OK",
            },
            {
                "objid": "103",
                "datetime": "22.11.2026 03:05:00",
                "parent": "Switch-Core",
                "type": "Sensor",
                "name": "Uplink Vivo Backup",
                "status": "Down",
                "status_raw": 5,
                "message": "Link loss on interface ge-0/0/1",
            },
            {
                "objid": "101",
                "datetime": "20.11.2026 14:10:00",
                "parent": "Router-Borda-01",
                "type": "Sensor",
                "name": "Link Embratel Principal",
                "status": "Down",
                "status_raw": 5,
                "message": "Old outage outside tonight window",
            },
        ]

    def fetch_server_status(self):
        if self.fail_with:
            return None, self.fail_with
        return {"Version": "26.1.116", "ReadOnlyUser": "true"}, None

    def fetch_all_sensors(self):
        if self.fail_with:
            return None, self.fail_with
        return list(self.sensors), None

    def fetch_devices(self):
        if self.fail_with:
            return None, self.fail_with
        return list(self.devices), None

    def fetch_groups(self):
        if self.fail_with:
            return None, self.fail_with
        return list(self.groups), None

    def fetch_sensor_details(self, objid):
        if self.fail_with:
            return None, self.fail_with
        for s in self.sensors:
            if s["objid"] == str(objid):
                return {
                    "objid": s["objid"],
                    "name": s["sensor"],
                    "sensortype": "snmptraffic",
                    "probename": s["probe"],
                    "parentgroupname": s["group"],
                    "parentdevicename": s["device"],
                    "parentdeviceid": "41",
                    "interval": "60",
                    "statusid": s["status_raw"],
                    "statustext": s["status"],
                    "uptime": s["uptime"],
                    "uptimetime": "10 d",
                    "downtime": s["downtime"],
                    "downtimetime": "20 m",
                    "updownsince": "2026-11-20 14:15:00",
                    "lastcheck": s["lastcheck"],
                    "lastup": s["lastup"],
                    "lastdown": s["lastdown"],
                    "lastvalue": s["lastvalue"],
                    "lastmessage": s["message"],
                    "info": "Primary WAN circuit",
                }, None
        return None, f"Details unavailable for sensor {objid}."

    def fetch_sensor_channels(self, objid):
        if self.fail_with:
            return None, self.fail_with
        return [
            {"name": "Traffic In", "lastvalue": "30.1 Mbit/s", "lastvalue_raw": 30100000.0},
            {"name": "Traffic Out", "lastvalue": "15.1 Mbit/s", "lastvalue_raw": 15100000.0},
        ], None

    def fetch_historic_data(self, objid, window_key="24h", now=None, hours=None, avg=None):
        if self.fail_with:
            return None, self.fail_with
        return {
            "timestamps": [f"22.11.2026 0{i}:00:00" for i in range(6)],
            "coverage": ["100 %"] * 6,
            "channels": ["Traffic In", "Traffic Out"],
            "series": {
                "Traffic In": [10e6, 20e6, 50e6, 40e6, 30e6, 35e6],
                "Traffic Out": [5e6, 10e6, 25e6, 20e6, 15e6, 18e6],
            },
            "stats": {
                "Traffic In": {"min": 10e6, "max": 50e6, "avg": 30833333.33, "last": 35e6, "count": 6},
                "Traffic Out": {"min": 5e6, "max": 25e6, "avg": 15500000.0, "last": 18e6, "count": 6},
            },
            "raw_points": [],
        }, None

    def fetch_messages(self, objid=None, drel="7days", count=150):
        if self.fail_with:
            return None, self.fail_with
        if objid is None:
            return list(self.messages), None
        return [m for m in self.messages if m["objid"] == str(objid)], None


class PRTGMCPTests(unittest.TestCase):
    def _make_server(self, client=None, now=None):
        client = client or _FakePRTGClient()
        config = {
            "prtg_url": "https://prtg.example",
            "apitoken": client.apitoken,
            "verify_ssl": True,
        }
        now_fn = (lambda: now) if now else (lambda: datetime(2026, 11, 22, 6, 48, 0))
        service = prtg_mcp.PRTGReadOnlyService(config, client=client, now_fn=now_fn)
        return prtg_mcp.PRTGMCPServer(service)

    def test_protocol_handshake_tools_resources_prompts_and_error_codes(self):
        server = self._make_server()

        init_resp = server.handle_message({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        })
        self.assertEqual("2025-03-26", init_resp["result"]["protocolVersion"])
        self.assertEqual("prtg-mcp", init_resp["result"]["serverInfo"]["name"])

        # Notifications must produce no response
        self.assertIsNone(server.handle_message({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }))

        self.assertEqual({}, server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"])

        tools_resp = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        tools = tools_resp["result"]["tools"]
        self.assertEqual(8, len(tools))
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"], f"{tool['name']} must be read-only")
            self.assertFalse(tool["annotations"]["destructiveHint"], f"{tool['name']} must be non-destructive")

        res_list = server.handle_message({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        self.assertEqual(2, len(res_list["result"]["resources"]))
        res_read = server.handle_message({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": "prtg://alerts/active"},
        })
        alerts_json = json.loads(res_read["result"]["contents"][0]["text"])
        self.assertGreaterEqual(alerts_json["total_matching"], 2)

        prompts_list = server.handle_message({"jsonrpc": "2.0", "id": 6, "method": "prompts/list"})
        self.assertEqual(["relatorio-noturno", "historico-link"], [p["name"] for p in prompts_list["result"]["prompts"]])
        prompt_get = server.handle_message({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "prompts/get",
            "params": {"name": "historico-link", "arguments": {"query": "Embratel", "window": "7d"}},
        })
        self.assertIn("Embratel", prompt_get["result"]["messages"][0]["content"]["text"])

        # Error handling
        unknown_method = server.handle_message({"jsonrpc": "2.0", "id": 8, "method": "unknown/op"})
        self.assertEqual(-32601, unknown_method["error"]["code"])
        invalid_req = server.handle_message(["not-an-object"])
        self.assertEqual(-32600, invalid_req["error"]["code"])
        unknown_tool = server.handle_message({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "prtg_delete_sensor", "arguments": {}},
        })
        self.assertTrue(unknown_tool["result"]["isError"])
        self.assertIn("Unknown tool", unknown_tool["result"]["content"][0]["text"])

    def test_stdio_framing_supports_ndjson_and_content_length(self):
        server = self._make_server()

        # 1. NDJSON framing + malformed JSON handling
        ndjson_in = io.BytesIO(
            b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
            b'not-valid-json\n'
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        ndjson_out = io.BytesIO()
        server.serve_stdio(in_stream=ndjson_in, out_stream=ndjson_out)
        lines = [line for line in ndjson_out.getvalue().decode("utf-8").splitlines() if line.strip()]
        self.assertEqual(2, len(lines))
        self.assertEqual({}, json.loads(lines[0])["result"])
        self.assertEqual(-32700, json.loads(lines[1])["error"]["code"])

        # 2. Content-Length framing
        req_body = json.dumps({"jsonrpc": "2.0", "id": 11, "method": "ping"}).encode("utf-8")
        cl_in = io.BytesIO(f"Content-Length: {len(req_body)}\r\n\r\n".encode("ascii") + req_body)
        cl_out = io.BytesIO()
        server.serve_stdio(in_stream=cl_in, out_stream=cl_out)
        raw_out = cl_out.getvalue().decode("utf-8")
        self.assertTrue(raw_out.startswith("Content-Length: "))
        _, body = raw_out.split("\r\n\r\n", 1)
        self.assertEqual({}, json.loads(body)["result"])

    def test_outages_report_answers_quais_sensores_cairam_essa_noite(self):
        server = self._make_server(now=datetime(2026, 11, 22, 6, 48, 0))
        resp = server.handle_message({
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "prtg_outages_report", "arguments": {"period": "overnight"}},
        })
        self.assertFalse(resp["result"]["isError"])
        report = json.loads(resp["result"]["content"][0]["text"])

        self.assertEqual(2, report["summary"]["total_affected_sensors"])
        self.assertEqual(1, report["summary"]["currently_down_count"])
        self.assertEqual(1, report["summary"]["recovered_in_window_count"])

        by_id = {inc["objid"]: inc for inc in report["incidents"]}
        self.assertIn("102", by_id)  # Currently down since 02:15
        self.assertEqual("currently_down", by_id["102"]["classification"])
        self.assertIn("103", by_id)  # Went down at 03:05 and recovered at 03:20
        self.assertEqual("down_and_recovered", by_id["103"]["classification"])
        self.assertNotIn("101", by_id)  # Old outage on 2026-11-20 excluded

    def test_sensor_history_answers_qual_historico_desse_link(self):
        server = self._make_server()
        resp = server.handle_message({
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {
                "name": "prtg_sensor_history",
                "arguments": {"query": "Embratel", "window": "24h", "max_points": 3},
            },
        })
        self.assertFalse(resp["result"]["isError"])
        data = json.loads(resp["result"]["content"][0]["text"])

        self.assertEqual("101", data["objid"])
        self.assertEqual("Link Embratel Principal", data["matched_from_query"]["sensor"])
        self.assertEqual(6, data["total_raw_points"])
        self.assertEqual(3, data["sampled_points_count"])
        self.assertEqual(["Traffic In", "Traffic Out"], data["channels"])
        self.assertEqual("50.00M", data["channel_summaries"]["Traffic In"]["max_human"])
        self.assertEqual("10.00M", data["channel_summaries"]["Traffic In"]["min_human"])
        self.assertEqual(2, len(data["live_channels"]))
        self.assertEqual(1, len(data["recent_events"]))

    def test_overview_search_details_devices_and_groups_tools(self):
        server = self._make_server()

        overview = json.loads(server.handle_message({
            "jsonrpc": "2.0", "id": 40, "method": "tools/call",
            "params": {"name": "prtg_get_overview", "arguments": {}},
        })["result"]["content"][0]["text"])
        self.assertEqual(4, overview["sensor_counts"]["total"])
        self.assertEqual(1, overview["sensor_counts"]["down"])
        self.assertEqual(1, overview["sensor_counts"]["warning"])
        self.assertEqual(2, overview["active_alerts_total"])

        searched = json.loads(server.handle_message({
            "jsonrpc": "2.0", "id": 41, "method": "tools/call",
            "params": {"name": "prtg_search_sensors", "arguments": {"query": "wan vivo"}},
        })["result"]["content"][0]["text"])
        self.assertEqual(1, searched["total_matching"])
        self.assertEqual("103", searched["sensors"][0]["objid"])

        details = json.loads(server.handle_message({
            "jsonrpc": "2.0", "id": 42, "method": "tools/call",
            "params": {"name": "prtg_get_sensor_details", "arguments": {"objid": "101"}},
        })["result"]["content"][0]["text"])
        self.assertEqual("Primary WAN circuit", details["details"]["info"])

        devices = json.loads(server.handle_message({
            "jsonrpc": "2.0", "id": 43, "method": "tools/call",
            "params": {"name": "prtg_list_devices", "arguments": {"only_with_issues": True}},
        })["result"]["content"][0]["text"])
        self.assertEqual(1, devices["total_matching"])
        self.assertEqual("FW-Matriz", devices["devices"][0]["device"])

        groups = json.loads(server.handle_message({
            "jsonrpc": "2.0", "id": 44, "method": "tools/call",
            "params": {"name": "prtg_list_groups", "arguments": {"only_with_issues": True}},
        })["result"]["content"][0]["text"])
        self.assertEqual(1, groups["total_matching"])
        self.assertEqual("Firewalls", groups["groups"][0]["group"])

    def test_redacts_secrets_and_never_pollutes_stdout_with_logs(self):
        secret = "top-secret-api-token-999"
        client = _FakePRTGClient(apitoken=secret)
        client.fail_with = f"HTTP 401 for https://prtg.example/api/table.json?apitoken={secret}"
        server = self._make_server(client=client)

        resp = server.handle_message({
            "jsonrpc": "2.0",
            "id": 50,
            "method": "tools/call",
            "params": {"name": "prtg_list_alerts", "arguments": {}},
        })
        self.assertTrue(resp["result"]["isError"])
        err_text = resp["result"]["content"][0]["text"]
        self.assertIn("REDACTED", err_text)
        self.assertNotIn(secret, err_text)

        # Ensure stdout remains clean during config validation with insecure/lab warnings
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        prev_handler = set_log_handler(None)
        try:
            with mock.patch.dict(os.environ, {
                "PRTG_URL": "http://prtg.lab.local",
                "PRTG_APITOKEN": secret,
                "PRTG_VERIFY_SSL": "false",
            }, clear=False):
                with mock.patch("sys.stdout", stdout_buf), mock.patch("sys.stderr", stderr_buf):
                    rc = prtg_mcp.main(["--check-config"])
            self.assertEqual(0, rc)
            out_json = json.loads(stdout_buf.getvalue())
            self.assertTrue(out_json["ok"])
            self.assertTrue(out_json["read_only"])
            self.assertNotIn(secret, stdout_buf.getvalue())
            self.assertNotIn(secret, stderr_buf.getvalue())
            self.assertIn("WARNING", stderr_buf.getvalue())
        finally:
            set_log_handler(prev_handler)

    def test_cli_list_tools_and_missing_config(self):
        stdout_buf = io.StringIO()
        with mock.patch("sys.stdout", stdout_buf):
            rc = prtg_mcp.main(["--list-tools"])
        self.assertEqual(0, rc)
        payload = json.loads(stdout_buf.getvalue())
        self.assertEqual(8, len(payload["tools"]))

        with tempfile.TemporaryDirectory() as tmpdir:
            missing = os.path.join(tmpdir, "absent.json")
            stderr_buf = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("sys.stderr", stderr_buf):
                    rc = prtg_mcp.main(["--check-config", "--config", missing])
            self.assertEqual(1, rc)
            self.assertIn("not found", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
