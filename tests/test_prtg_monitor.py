import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import prtg_monitor
from prtg_monitor import (
    ConfigError,
    PRTGMonitor,
    main,
    reap_alert_processes,
    redact_secrets,
    trigger_macos_alert,
    trigger_macos_notification,
)


def sensor_xml(items, total_count=None, list_end=True):
    total_count = len(items) if total_count is None else total_count
    body = "".join(
        (
            "<item>"
            f"<objid>{item['objid']}</objid>"
            f"<sensor>{item.get('sensor', 'Sensor')}</sensor>"
            f"<device>{item.get('device', 'Device')}</device>"
            f"<status>{item.get('status', 'Down')}</status>"
            f"<status_RAW>{item.get('status_raw', '5')}</status_RAW>"
            "<message>Message</message>"
            "<lastvalue>Value</lastvalue>"
            "</item>"
        )
        for item in items
    )
    return (
        f'<sensors totalcount="{total_count}" listend="{int(list_end)}">'
        f'{body}</sensors>'
    ).encode()


def monitor_with_config():
    monitor = PRTGMonitor.__new__(PRTGMonitor)
    monitor.config = {
        "prtg_url": "https://prtg.example",
        "apitoken": "token",
        "filter_statuses": [5, 14],
        "notify_on_resolve": True,
    }
    monitor.active_alerts = {}
    monitor.first_run = False
    return monitor


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


class PRTGMonitorTests(unittest.TestCase):
    def test_malformed_xml_keeps_previous_alert_state(self):
        monitor = monitor_with_config()
        monitor.active_alerts["42"] = {"objid": "42", "sensor": "S", "device": "D"}
        monitor.fetch_xml = lambda url: b"<sensors><item>"
        monitor.notify_resolution = lambda sensor: self.fail("must not notify resolution")

        monitor.run_cycle()

        self.assertIn("42", monitor.active_alerts)

    def test_error_xml_keeps_previous_alert_state(self):
        monitor = monitor_with_config()
        monitor.active_alerts["42"] = {"objid": "42", "sensor": "S", "device": "D"}
        monitor.fetch_xml = lambda url: b"<prtg><error>Unauthorized</error></prtg>"
        monitor.notify_resolution = lambda sensor: self.fail("must not notify resolution")

        monitor.run_cycle()

        self.assertIn("42", monitor.active_alerts)

    def test_empty_response_keeps_previous_alert_state(self):
        monitor = monitor_with_config()
        monitor.active_alerts["42"] = {"objid": "42", "sensor": "S", "device": "D"}
        monitor.fetch_xml = lambda url: b""
        monitor.notify_resolution = lambda sensor: self.fail("must not notify resolution")

        monitor.run_cycle()

        self.assertIn("42", monitor.active_alerts)

    def test_valid_empty_snapshot_is_not_a_parse_failure(self):
        monitor = monitor_with_config()

        page = monitor.parse_xml_sensor_page(sensor_xml([]))

        self.assertEqual(([], 0, True), page)

    def test_acknowledged_sensor_is_not_resolved(self):
        monitor = monitor_with_config()
        monitor.active_alerts["42"] = {"objid": "42", "sensor": "S", "device": "D"}
        monitor.fetch_active_errors = lambda: []
        monitor.fetch_sensor_status = lambda objid: 13
        monitor.notify_resolution = lambda sensor: self.fail("must not notify resolution")

        monitor.run_cycle()

        self.assertIn("42", monitor.active_alerts)

    def test_sensor_is_resolved_only_when_status_is_up(self):
        monitor = monitor_with_config()
        monitor.active_alerts["42"] = {"objid": "42", "sensor": "S", "device": "D"}
        monitor.fetch_active_errors = lambda: []
        monitor.fetch_sensor_status = lambda objid: 3
        resolutions = []
        monitor.notify_resolution = lambda sensor: resolutions.append(sensor["objid"])

        monitor.run_cycle()

        self.assertNotIn("42", monitor.active_alerts)
        self.assertEqual(["42"], resolutions)

    def test_sensor_status_lookup_uses_supported_single_object_endpoint(self):
        monitor = monitor_with_config()
        requested_query = {}
        requested_path = []

        def fetch_xml(url, timeout=None):
            requested_path.append(urllib.parse.urlsplit(url).path)
            requested_query.update(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
            return b'<prtg><version>26.1</version><result>3</result></prtg>'

        monitor.fetch_xml = fetch_xml

        status = monitor.fetch_sensor_status("42")

        self.assertEqual(3, status)
        self.assertEqual(["/api/getobjectstatus.htm"], requested_path)
        self.assertEqual(["42"], requested_query["id"])
        self.assertEqual(["status"], requested_query["name"])
        self.assertNotIn("filter_status", requested_query)

    def test_status_lookups_are_bounded_and_rotate_without_starvation(self):
        monitor = monitor_with_config()
        monitor.STATUS_LOOKUP_LIMIT = 2
        monitor.active_alerts = {
            str(objid): {"objid": str(objid), "sensor": "S", "device": "D"}
            for objid in range(1, 5)
        }
        monitor.fetch_active_errors = lambda: []
        checked = []
        monitor.fetch_sensor_status = lambda objid: checked.append(objid)

        monitor.run_cycle()
        monitor.run_cycle()

        self.assertEqual(["1", "2", "3", "4"], checked)

    def test_fetch_active_errors_collects_every_page(self):
        monitor = monitor_with_config()
        monitor.API_PAGE_SIZE = 2
        requested_starts = []
        pages = {
            0: sensor_xml(
                [{"objid": "1"}, {"objid": "2"}],
                total_count=3,
                list_end=False,
            ),
            2: sensor_xml([{"objid": "3"}], total_count=3),
        }

        def fetch_xml(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            start = int(query["start"][0])
            requested_starts.append(start)
            self.assertEqual(["2"], query["count"])
            self.assertEqual(["objid"], query["sortby"])
            self.assertEqual(["5", "14"], query["filter_status"])
            return pages[start]

        monitor.fetch_xml = fetch_xml

        sensors = monitor.fetch_active_errors()

        self.assertEqual(["1", "2", "3"], [sensor["objid"] for sensor in sensors])
        self.assertEqual([0, 2], requested_starts)

    def test_inconsistent_pagination_is_not_reconciled(self):
        monitor = monitor_with_config()
        monitor.API_PAGE_SIZE = 2
        pages = iter([
            sensor_xml(
                [{"objid": "1"}, {"objid": "2"}],
                total_count=3,
                list_end=False,
            ),
            sensor_xml([{"objid": "3"}], total_count=4),
        ])
        monitor.fetch_xml = lambda url: next(pages)

        self.assertIsNone(monitor.fetch_active_errors())

    def test_duplicate_sensor_across_pages_is_not_reconciled(self):
        monitor = monitor_with_config()
        monitor.API_PAGE_SIZE = 1
        pages = iter([
            sensor_xml([{"objid": "1"}], total_count=2, list_end=False),
            sensor_xml([{"objid": "1"}], total_count=2),
        ])
        monitor.fetch_xml = lambda url: next(pages)

        self.assertIsNone(monitor.fetch_active_errors())

    def test_later_page_failure_keeps_state_and_defers_notifications(self):
        monitor = monitor_with_config()
        monitor.API_PAGE_SIZE = 1
        monitor.first_run = True
        monitor.active_alerts["42"] = {"objid": "42", "sensor": "Old", "device": "D"}
        pages = iter([
            sensor_xml([{"objid": "99"}], total_count=2, list_end=False),
            b"<sensors><item>",
        ])
        monitor.fetch_xml = lambda url: next(pages)
        monitor.notify_alert = lambda sensor: self.fail("must not notify a partial page")
        monitor.notify_resolution = lambda sensor: self.fail("must not notify resolution")

        monitor.run_cycle()

        self.assertEqual(["42"], list(monitor.active_alerts))
        self.assertTrue(monitor.first_run)

    # --- New tests covering state persistence, atomic reconciliation, security, validation, and CLI ---

    def test_state_persists_across_restarts_without_duplicate_alerts_and_notifies_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            write_json(config_path, {
                "prtg_url": "https://prtg.example",
                "apitoken": "super-secret-token",
                "state_file": "state.json",
            })

            first = PRTGMonitor(config_path)
            self.assertTrue(first.first_run)
            alerts_1 = []
            first.notify_alert = lambda s: alerts_1.append(s["objid"])
            first.fetch_xml = lambda url, timeout=None: sensor_xml([
                {"objid": "100", "sensor": "Ping", "device": "Core"}
            ])

            self.assertTrue(first.run_cycle())
            self.assertEqual(["100"], alerts_1)
            self.assertFalse(first.first_run)

            state_path = os.path.join(tmpdir, "state.json")
            self.assertTrue(os.path.exists(state_path))
            mode = stat.S_IMODE(os.stat(state_path).st_mode)
            self.assertEqual(0o600, mode)

            # Restart monitor from disk
            restarted = PRTGMonitor(config_path)
            self.assertFalse(restarted.first_run)
            self.assertIn("100", restarted.active_alerts)

            alerts_2 = []
            resolutions_2 = []
            restarted.notify_alert = lambda s: alerts_2.append(s["objid"])
            restarted.notify_resolution = lambda s: resolutions_2.append(s["objid"])

            # Same sensor still failing -> no duplicate alert
            restarted.fetch_xml = lambda url, timeout=None: sensor_xml([
                {"objid": "100", "sensor": "Ping", "device": "Core"}
            ])
            self.assertTrue(restarted.run_cycle())
            self.assertEqual([], alerts_2)
            self.assertEqual([], resolutions_2)

            # Sensor disappears from error filter and status lookup reports UP (3)
            restarted.fetch_active_errors = lambda: []
            restarted.fetch_sensor_status = lambda objid: 3
            self.assertTrue(restarted.run_cycle())
            self.assertEqual(["100"], resolutions_2)
            self.assertEqual({}, restarted.active_alerts)

    def test_failed_save_state_aborts_cycle_atomically_without_mutating_or_notifying(self):
        monitor = monitor_with_config()
        monitor.first_run = True
        monitor.status_lookup_offset = 1
        monitor.active_alerts = {"42": {"objid": "42", "sensor": "Old", "device": "D"}}
        monitor.fetch_active_errors = lambda: [{"objid": "99", "sensor": "New", "device": "D"}]
        monitor.fetch_sensor_status = lambda objid: 3
        monitor.save_state = lambda alerts, offset: False
        monitor.notify_alert = lambda s: self.fail("must not notify alert when save_state fails")
        monitor.notify_resolution = lambda s: self.fail("must not notify resolution when save_state fails")

        ok = monitor.run_cycle()

        self.assertFalse(ok)
        self.assertEqual(["42"], list(monitor.active_alerts))
        self.assertEqual(1, monitor.status_lookup_offset)
        self.assertTrue(monitor.first_run)

    def test_exception_in_status_lookup_leaves_state_unchanged_and_defers_alerts(self):
        monitor = monitor_with_config()
        monitor.active_alerts = {"42": {"objid": "42", "sensor": "Old", "device": "D"}}
        monitor.fetch_active_errors = lambda: [{"objid": "99", "sensor": "New", "device": "D"}]

        def boom(objid):
            raise RuntimeError("status lookup blew up")

        monitor.fetch_sensor_status = boom
        monitor.notify_alert = lambda s: self.fail("must not notify before full cycle succeeds")

        with self.assertRaises(RuntimeError):
            monitor.run_cycle()

        self.assertEqual(["42"], list(monitor.active_alerts))

    def test_corrupted_state_file_resets_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            state_path = os.path.join(tmpdir, "state.json")
            write_json(config_path, {
                "prtg_url": "https://prtg.example",
                "apitoken": "super-secret-token",
                "state_file": "state.json",
            })
            with open(state_path, "w", encoding="utf-8") as f:
                f.write("{broken json")

            monitor = PRTGMonitor(config_path)

            self.assertEqual({}, monitor.active_alerts)
            self.assertEqual(0, monitor.status_lookup_offset)
            self.assertTrue(monitor.first_run)

    def test_status_lookup_rotation_does_not_skip_when_sensor_resolves_mid_window(self):
        monitor = monitor_with_config()
        monitor.STATUS_LOOKUP_LIMIT = 2
        monitor.active_alerts = {
            str(objid): {"objid": str(objid), "sensor": "S", "device": "D"}
            for objid in range(1, 5)
        }
        monitor.fetch_active_errors = lambda: []
        monitor.notify_resolution = lambda s: None

        checked = []

        def fetch_status(objid):
            checked.append(objid)
            return 3 if objid == "1" else 13

        monitor.fetch_sensor_status = fetch_status

        monitor.run_cycle()
        monitor.run_cycle()

        self.assertEqual(["1", "2", "3", "4"], checked)

    def test_missing_config_creates_private_template_and_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "nested", "config.json")
            with self.assertRaises(ConfigError):
                PRTGMonitor(config_path)

            self.assertTrue(os.path.exists(config_path))
            mode = stat.S_IMODE(os.stat(config_path).st_mode)
            self.assertEqual(0o600, mode)
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertTrue(data["verify_ssl"])

    def test_config_validation_rejects_invalid_configurations(self):
        valid = {
            "prtg_url": "https://prtg.example",
            "apitoken": "real-token-123",
            "poll_interval_seconds": 60,
            "filter_statuses": [5, 14],
            "verify_ssl": True,
            "notification_type": "banner",
            "notify_on_resolve": True,
        }
        self.assertEqual("https://prtg.example", PRTGMonitor.validate_config(valid)["prtg_url"])

        bad_cases = [
            ["not", "a", "dict"],
            {**valid, "prtg_url": "https://your-prtg-server.example.com"},
            {**valid, "prtg_url": "ftp://prtg.example"},
            {**valid, "apitoken": "YOUR_API_TOKEN_HERE"},
            {**valid, "apitoken": "   "},
            {**valid, "filter_statuses": []},
            {**valid, "filter_statuses": [True]},
            {**valid, "filter_statuses": ["5"]},
            {**valid, "poll_interval_seconds": 0},
            {**valid, "poll_interval_seconds": -5},
            {**valid, "poll_interval_seconds": True},
            {**valid, "notification_type": "popup"},
            {**valid, "verify_ssl": "false"},
            {**valid, "notify_on_resolve": "yes"},
        ]
        for case in bad_cases:
            with self.subTest(case=case):
                with self.assertRaises(ConfigError):
                    PRTGMonitor.validate_config(case)

    def test_secret_redaction_and_load_config_never_leak_tokens(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            short_secret = "abc12"
            write_json(config_path, {
                "prtg_url": "https://prtg.example",
                "apitoken": short_secret,
            })

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                monitor = PRTGMonitor(config_path)

            output = buf.getvalue()
            self.assertNotIn(short_secret, output)
            self.assertIn("API Token: *** (configured)", output)

            # Redaction on URLError, HTTPError, and arbitrary exceptions
            url_err = urllib.error.URLError(f"https://prtg.example/api/table.xml?apitoken={short_secret}&x=1")
            http_err = urllib.error.HTTPError(
                f"https://prtg.example/api/table.xml?apitoken={short_secret}",
                401,
                f"Unauthorized token {short_secret}",
                {},
                None,
            )

            log_buf = io.StringIO()
            with contextlib.redirect_stdout(log_buf):
                with mock.patch("urllib.request.urlopen", side_effect=http_err):
                    self.assertIsNone(monitor.fetch_xml("https://prtg.example"))
                with mock.patch("urllib.request.urlopen", side_effect=url_err):
                    self.assertIsNone(monitor.fetch_xml("https://prtg.example"))
                with mock.patch("urllib.request.urlopen", side_effect=RuntimeError(f"boom {short_secret}")):
                    self.assertIsNone(monitor.fetch_xml("https://prtg.example"))

            logs = log_buf.getvalue()
            self.assertIn("HTTP 401", logs)
            self.assertNotIn(short_secret, logs)
            self.assertIn("REDACTED", redact_secrets("GET /api?apitoken=xyz123&a=1"))

    def test_notification_types_remain_behaviorally_distinct(self):
        sensor = {
            "objid": "77",
            "sensor": "CPU",
            "device": "HostA",
            "status": "Down",
            "message": "Timeout",
        }
        for mode, expect_banner, expect_alert in [
            ("banner", True, False),
            ("alert", False, True),
            ("both", True, True),
        ]:
            with self.subTest(mode=mode):
                monitor = monitor_with_config()
                monitor.config["notification_type"] = mode
                monitor.config["sound_name"] = None
                monitor.config["poll_interval_seconds"] = 60
                monitor._stop_event = mock.MagicMock()
                monitor._stop_event.is_set.return_value = True

                with mock.patch("prtg_monitor.trigger_macos_notification") as mock_banner, \
                     mock.patch("prtg_monitor.trigger_macos_alert") as mock_alert:
                    monitor.start()
                    self.assertEqual(expect_banner, mock_banner.called)

                    mock_banner.reset_mock()
                    mock_alert.reset_mock()
                    monitor.notify_alert(sensor)
                    self.assertEqual(expect_banner, mock_banner.called)
                    self.assertEqual(expect_alert, mock_alert.called)
                    if expect_banner:
                        self.assertIsNone(mock_banner.call_args[0][3])

                    mock_banner.reset_mock()
                    mock_alert.reset_mock()
                    monitor.notify_resolution(sensor)
                    self.assertEqual(expect_banner, mock_banner.called)
                    self.assertEqual(expect_alert, mock_alert.called)
                    if expect_banner:
                        # Silent sound_name=None suppresses resolution sound too
                        self.assertIsNone(mock_banner.call_args[0][3])

    def test_applescript_uses_argv_after_double_dash_and_reaps_processes(self):
        prtg_monitor._active_alert_processes.clear()
        tricky_msg = 'Line1 "quoted" \\ backslash\nLine2\x00'
        tricky_sound = 'Glass"; do shell script "whoami'

        with mock.patch("subprocess.run") as mock_run:
            ok = trigger_macos_notification("Title", "Sub", tricky_msg, tricky_sound)

        self.assertTrue(ok)
        cmd = mock_run.call_args[0][0]
        self.assertEqual("osascript", cmd[0])
        self.assertEqual("-e", cmd[1])
        self.assertEqual("--", cmd[3])
        self.assertNotIn("whoami", cmd[2])
        self.assertNotIn("quoted", cmd[2])
        self.assertEqual('Line1 "quoted" \\ backslash\nLine2', cmd[4])
        self.assertEqual(tricky_sound, cmd[7])
        self.assertGreater(mock_run.call_args[1]["timeout"], 0)

        with mock.patch("subprocess.run", side_effect=OSError("osascript missing")):
            self.assertFalse(trigger_macos_notification("T", "S", "M"))

        finished_proc = mock.MagicMock()
        finished_proc.poll.return_value = 0
        running_proc = mock.MagicMock()
        running_proc.poll.return_value = None
        prtg_monitor._active_alert_processes.append(finished_proc)

        with mock.patch("subprocess.Popen", return_value=running_proc) as mock_popen:
            ok_alert = trigger_macos_alert('Alert "Title"', tricky_msg, icon="invalid_icon")

        self.assertTrue(ok_alert)
        popen_cmd = mock_popen.call_args[0][0]
        self.assertEqual("--", popen_cmd[3])
        self.assertIn("with icon caution", popen_cmd[2])
        self.assertEqual([running_proc], prtg_monitor._active_alert_processes)

        running_proc.poll.return_value = 0
        reap_alert_processes()
        self.assertEqual([], prtg_monitor._active_alert_processes)

    def test_xml_and_http_edge_cases(self):
        monitor = monitor_with_config()
        xml_bytes = (
            b'<sensors totalcount="1" listend="1"><item>'
            b'<objid>55</objid><sensor>Disk</sensor><device>NAS</device>'
            b'<status>Down</status><status_RAW>5</status_RAW>'
            b'<message>&lt;div&gt;HTML msg&lt;/div&gt;</message>'
            b'<message_raw>Plain raw error</message_raw>'
            b'<lastvalue>99%</lastvalue>'
            b'</item></sensors>'
        )
        sensors, total, list_end = monitor.parse_xml_sensor_page(xml_bytes)
        self.assertEqual(1, total)
        self.assertTrue(list_end)
        self.assertEqual("Plain raw error", sensors[0]["message"])

        # Negative totalcount rejected
        self.assertIsNone(monitor.parse_xml_sensor_page(b'<sensors totalcount="-1" listend="1"></sensors>'))

        # Duplicate objid within a page fails fast before fetching next page
        monitor.API_PAGE_SIZE = 2
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return sensor_xml([{"objid": "1"}, {"objid": "1"}], total_count=4, list_end=False)

        monitor.fetch_xml = fake_fetch
        self.assertIsNone(monitor.fetch_active_errors())
        self.assertEqual(1, len(calls))

        # Oversized response rejected
        oversized = mock.MagicMock()
        oversized.status = 200
        oversized.read.return_value = b"x" * (PRTGMonitor.MAX_RESPONSE_BYTES + 1)
        cm = mock.MagicMock()
        cm.__enter__.return_value = oversized
        cm.__exit__.return_value = False
        monitor_normal = monitor_with_config()
        with mock.patch("urllib.request.urlopen", return_value=cm):
            self.assertIsNone(monitor_normal.fetch_xml("https://prtg.example/api/table.xml"))

    def test_cli_check_config_once_and_non_tty_log_formatting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            write_json(config_path, {
                "prtg_url": "https://prtg.example",
                "apitoken": "real-token",
            })

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--check-config", config_path])
            self.assertEqual(0, rc)
            # StringIO.isatty() is False -> no ANSI escapes
            self.assertNotIn("\033[", buf.getvalue())

            with mock.patch.object(PRTGMonitor, "run_cycle", return_value=True) as mock_cycle:
                self.assertEqual(0, main(["--once", config_path]))
                self.assertEqual(1, mock_cycle.call_count)

            with mock.patch.object(PRTGMonitor, "run_cycle", return_value=False):
                self.assertEqual(1, main(["--once", config_path]))

            bad_path = os.path.join(tmpdir, "missing.json")
            self.assertEqual(1, main(["--check-config", bad_path]))


if __name__ == "__main__":
    unittest.main()
