import contextlib
import curses
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import prtg_monitor
import prtg_tui
from prtg_tui import (
    TUIState,
    TUIRunner,
    build_sensor_url,
    filter_and_sort_alerts,
    handle_key,
    load_state_snapshot,
    open_sensor_url,
    render_analog_clock_gmt3,
    render_braille_chart,
    render_tui,
    resolve_config_path,
    sao_paulo_datetime,
    summarize_alerts,
    summarize_all_sensors,
)


class FakeWindow:
    """In-memory curses window stub verifying bounds and capturing rendered lines."""

    def __init__(self, height=34, width=110):
        self.height = height
        self.width = width
        self.calls = []
        self.grid = {}
        self.erased = False
        self.refreshed = False

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        self.erased = True
        self.calls.clear()
        self.grid.clear()

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise AssertionError(f"out of bounds addstr({y}, {x}) on {self.height}x{self.width}")
        if y == self.height - 1 and x + len(text) >= self.width:
            raise AssertionError("attempted to write into bottom-right corner")
        self.calls.append((y, x, text, attr))
        row = self.grid.setdefault(y, [" "] * self.width)
        for offset, ch in enumerate(text):
            if x + offset < self.width:
                row[x + offset] = ch

    def noutrefresh(self):
        self.refreshed = True

    def rendered_text(self):
        return "\n".join("".join(self.grid[y]).rstrip() for y in sorted(self.grid))


def sample_alerts():
    return [
        {
            "objid": "2113",
            "probe": "X-OCI",
            "group": "X-OCI",
            "sensor": "Memory: Physical memory",
            "device": "X-POSTGRESQL",
            "status": "Down",
            "status_raw": 5,
            "message": "3 % is below the error limit of 5 %",
            "lastvalue": "3 %",
            "uptime": "66.6%",
        },
        {
            "objid": "2069",
            "probe": "X-OCI",
            "group": "SITES",
            "sensor": "SG -X",
            "device": "SITES",
            "status": "Down Partial",
            "status_raw": 14,
            "message": "The remote server returned an error: (404) Not Found.",
            "lastvalue": "",
            "uptime": "98.0%",
        },
        {
            "objid": "105",
            "probe": "Local",
            "group": "Servers",
            "sensor": "Disk Free",
            "device": "APP-01",
            "status": "Down (Acknowledged)",
            "status_raw": 13,
            "message": "Ack by ops",
            "lastvalue": "4 %",
            "uptime": "99.1%",
        },
    ]


def sample_all_sensors():
    return sample_alerts() + [
        {
            "objid": "1001",
            "probe": "Local",
            "group": "Local",
            "device": "Probe Device",
            "sensor": "System Health",
            "status": "Up",
            "status_raw": 3,
            "message": "OK",
            "lastvalue": "100 %",
            "uptime": "99.99%",
            "lastcheck": "10:42:00 [20 s ago]",
        },
        {
            "objid": "1002",
            "probe": "Local",
            "group": "Servers",
            "device": "APP-01",
            "sensor": "CPU Load",
            "status": "Paused",
            "status_raw": 7,
            "message": "Paused by user",
            "lastvalue": "",
            "uptime": "95.0%",
        },
    ]


class PRTGTUITests(unittest.TestCase):
    def test_resolve_config_path_priority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            local_cfg = os.path.join(tmpdir, "config.json")
            installed_cfg = os.path.join(tmpdir, "installed.json")
            with open(installed_cfg, "w", encoding="utf-8") as f:
                f.write("{}")

            orig_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                with mock.patch.object(prtg_tui, "DEFAULT_INSTALLED_CONFIG", installed_cfg):
                    self.assertEqual(os.path.expanduser("~/custom.json"), resolve_config_path("~/custom.json"))
                    self.assertEqual(installed_cfg, resolve_config_path(None))

                    with open(local_cfg, "w", encoding="utf-8") as f:
                        f.write("{}")
                    self.assertEqual("config.json", resolve_config_path(None))
            finally:
                os.chdir(orig_cwd)

    def test_load_state_snapshot_valid_missing_and_corrupted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = os.path.join(tmpdir, "missing.json")
            alerts, updated, err = load_state_snapshot(missing)
            self.assertEqual([], alerts)
            self.assertIsNone(updated)
            self.assertIn("not found", err)

            state_file = os.path.join(tmpdir, "state.json")
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": 1,
                        "updated_at": "2026-11-19T13:31:50Z",
                        "active_alerts": {
                            "2069": {
                                "objid": "2069",
                                "sensor": "SG -X",
                                "device": "SITES",
                                "status": "Down",
                                "status_raw": 5,
                                "message": "404",
                                "lastvalue": "",
                            }
                        },
                    },
                    f,
                )
            alerts, updated, err = load_state_snapshot(state_file)
            self.assertIsNone(err)
            self.assertEqual(1, len(alerts))
            self.assertEqual("2069", alerts[0]["objid"])
            self.assertIsInstance(updated, float)

            with open(state_file, "w", encoding="utf-8") as f:
                f.write("{corrupt")
            alerts, updated, err = load_state_snapshot(state_file)
            self.assertIsNone(alerts)
            self.assertIsNone(updated)
            self.assertIn("Failed to read state", err)

    def test_summarize_filter_and_sort_modes(self):
        alerts = sample_alerts()
        summary = summarize_alerts(alerts)
        self.assertEqual({"total": 3, "down": 1, "partial": 1, "other": 1}, summary)

        all_summary = summarize_all_sensors(sample_all_sensors())
        self.assertEqual(5, all_summary["total"])
        self.assertEqual(1, all_summary["up"])
        self.assertEqual(1, all_summary["paused"])

        by_status = [a["objid"] for a in filter_and_sort_alerts(alerts, sort_by="status")]
        self.assertEqual(["2113", "2069", "105"], by_status)

        by_device = [a["objid"] for a in filter_and_sort_alerts(alerts, sort_by="device")]
        self.assertEqual(["105", "2069", "2113"], by_device)

        by_sensor = [a["objid"] for a in filter_and_sort_alerts(alerts, sort_by="sensor")]
        self.assertEqual(["105", "2113", "2069"], by_sensor)

        by_objid = [a["objid"] for a in filter_and_sort_alerts(alerts, sort_by="objid")]
        self.assertEqual(["105", "2069", "2113"], by_objid)

        filtered = filter_and_sort_alerts(alerts, query="postgres")
        self.assertEqual(["2113"], [a["objid"] for a in filtered])

    def test_render_braille_chart_variants(self):
        empty_lines, y_min, y_max = render_braille_chart([], 12, 4)
        self.assertEqual(4, len(empty_lines))
        self.assertEqual(12, len(empty_lines[0]))
        self.assertIsNone(y_min)
        self.assertIsNone(y_max)

        flat_lines, fmin, fmax = render_braille_chart([50.0, 50.0, 50.0], 10, 3)
        self.assertEqual(50.0, fmin)
        self.assertEqual(50.0, fmax)
        self.assertTrue(any(ord(ch) > 0x2800 for line in flat_lines for ch in line))

        curve_lines, cmin, cmax = render_braille_chart([10.0, 20.0, None, 40.0, 30.0, 80.0], 15, 5)
        self.assertEqual(5, len(curve_lines))
        self.assertEqual(15, len(curve_lines[0]))
        self.assertEqual(10.0, cmin)
        self.assertEqual(80.0, cmax)
        self.assertTrue(any(0x2800 <= ord(ch) <= 0x28FF for line in curve_lines for ch in line))

    def test_analog_clock_gmt3_computation_and_rendering(self):
        # 2026-11-21T13:15:30Z -> 10:15:30 in GMT-3 (São Paulo)
        epoch_utc = 1795266930.0
        dt_sp = sao_paulo_datetime(epoch_utc)
        self.assertEqual(10, dt_sp.hour)
        self.assertEqual(15, dt_sp.minute)
        self.assertEqual(30, dt_sp.second)
        self.assertEqual(-10800, int(dt_sp.utcoffset().total_seconds()))

        dial_lines, time_str, tz_str = render_analog_clock_gmt3(epoch_utc, width=7, height=3)
        self.assertEqual(3, len(dial_lines))
        self.assertTrue(all(len(row) == 7 for row in dial_lines))
        self.assertEqual("10:15:30", time_str)
        self.assertIn("GMT-3", tz_str)
        self.assertTrue(any(ord(ch) > 0x2800 for row in dial_lines for ch in row))

        # Different time should produce different hand positions on the analog Braille dial
        dial_lines_later, time_str_later, _ = render_analog_clock_gmt3(epoch_utc + 3600 + 900, width=7, height=3)
        self.assertEqual("11:30:30", time_str_later)
        self.assertNotEqual(dial_lines, dial_lines_later)

        # Verify top-right corner placement in main TUI and inspector
        state = TUIState(mode="live", prtg_url="https://x.bytesec.app")
        state.apply_snapshot(sample_alerts(), updated_at=epoch_utc)
        win = FakeWindow(30, 110)
        render_tui(win, state, now_mono=100.0, now_epoch=epoch_utc)
        # Check that rows 1..3 in the top-right header box contain the GMT-3 clock and Braille dial
        top_right_rows = ["".join(win.grid.get(y, [])[75:]).rstrip() for y in (1, 2, 3)]
        top_right_block = "\n".join(top_right_rows)
        self.assertIn("10:15:30", top_right_block)
        self.assertIn("GMT-3", top_right_block)
        self.assertTrue(any(0x2801 <= ord(ch) <= 0x28FF for ch in top_right_block))

        # Also verify in Sensor Inspector top-right header
        state.open_inspector(sample_alerts()[0])
        render_tui(win, state, now_mono=100.0, now_epoch=epoch_utc)
        insp_top_right = "\n".join("".join(win.grid.get(y, [])[75:]).rstrip() for y in (1, 2, 3))
        self.assertIn("10:15:30", insp_top_right)
        self.assertIn("GMT-3", insp_top_right)
        self.assertTrue(any(0x2801 <= ord(ch) <= 0x28FF for ch in insp_top_right))

    def test_tui_state_preserves_selection_and_alerts_on_failed_refresh(self):
        state = TUIState(mode="live", prtg_url="https://prtg.example", refresh_interval=60)
        alerts = sample_alerts()
        state.apply_snapshot(alerts, updated_at=1000.0)
        state.move_selection(1)  # selects 2069
        self.assertEqual("2069", state.selected_alert()["objid"])

        state.apply_snapshot(None, error="HTTP 500 Internal Server Error")
        self.assertEqual(3, len(state.alerts))
        self.assertEqual("2069", state.selected_alert()["objid"])
        self.assertEqual("HTTP 500 Internal Server Error", state.last_error)

        state.apply_snapshot([alerts[0], alerts[1]], updated_at=1060.0, error=None)
        self.assertEqual("2069", state.selected_alert()["objid"])
        self.assertIsNone(state.last_error)

    def test_handle_key_navigation_filter_sort_mode_open_help_and_quit(self):
        state = TUIState(mode="live", prtg_url="https://prtg.example")
        state.apply_snapshot(sample_alerts(), updated_at=1000.0)
        refreshed = []
        opened = []

        self.assertTrue(handle_key("j", state, lambda force=False: refreshed.append(force), opened.append))
        self.assertEqual("2069", state.selected_alert()["objid"])

        self.assertTrue(handle_key(curses.KEY_UP, state, lambda force=False: None, opened.append))
        self.assertEqual("2113", state.selected_alert()["objid"])

        self.assertTrue(handle_key("G", state, lambda force=False: None, opened.append))
        self.assertEqual("105", state.selected_alert()["objid"])

        self.assertTrue(handle_key("g", state, lambda force=False: None, opened.append))
        self.assertEqual("2113", state.selected_alert()["objid"])

        handle_key("/", state, lambda force=False: None, opened.append)
        for ch in "sitesx":
            handle_key(ch, state, lambda force=False: None, opened.append)
        handle_key(127, state, lambda force=False: None, opened.append)
        self.assertEqual("sites", state.filter_query)
        handle_key("\n", state, lambda force=False: None, opened.append)
        self.assertFalse(state.filter_input_active)
        self.assertEqual(["2069"], [a["objid"] for a in state.visible_alerts()])

        handle_key("/", state, lambda force=False: None, opened.append)
        handle_key("z", state, lambda force=False: None, opened.append)
        handle_key(27, state, lambda force=False: None, opened.append)
        self.assertEqual("sites", state.filter_query)

        handle_key("c", state, lambda force=False: None, opened.append)
        self.assertEqual("", state.filter_query)

        handle_key("s", state, lambda force=False: None, opened.append)
        self.assertEqual("device", state.current_sort())

        handle_key("m", state, lambda force=False: refreshed.append(force), opened.append)
        self.assertEqual("state", state.mode)
        self.assertTrue(refreshed[-1])

        handle_key("r", state, lambda force=False: refreshed.append(force), opened.append)
        self.assertEqual(2, len(refreshed))

        handle_key("o", state, lambda force=False: None, opened.append)
        self.assertEqual(1, len(opened))

        handle_key("?", state, lambda force=False: None, opened.append)
        self.assertTrue(state.show_help)
        handle_key("q", state, lambda force=False: None, opened.append)
        self.assertFalse(state.show_help)

        self.assertFalse(handle_key("q", state, lambda force=False: None, opened.append))

    def test_tabs_drilldown_and_sensor_inspector_workflow(self):
        state = TUIState(mode="live", prtg_url="https://x.bytesec.app")
        state.apply_explorer_snapshot(
            all_sensors=sample_all_sensors(),
            devices=[
                {"objid": "40", "device": "APP-01", "host": "10.0.0.2", "group": "Servers", "down": 1, "partial": 0, "warn": 0, "up": 1, "paused": 1},
            ],
            groups=[
                {"objid": "1", "group": "Servers", "probe": "Local", "down": 1, "warn": 0, "up": 1, "paused": 1},
            ],
            messages=[
                {"objid": "105", "datetime": "19.11.2026 10:00:00", "status": "Down", "name": "Disk Free", "parent": "APP-01", "message": "Low space"},
            ],
            server_status={"Version": "26.1.116"},
            updated_at=time.time(),
        )

        inspector_calls = []
        opened = []
        refreshed = []

        # Switch to Sensors tab ('2') and cycle status filter ('f')
        handle_key("2", state, lambda force=False: refreshed.append(force), opened.append)
        self.assertEqual("sensors", state.current_tab())
        self.assertEqual(5, len(state.visible_alerts()))

        handle_key("f", state, lambda force=False: refreshed.append(force), opened.append)  # alerts
        self.assertEqual("alerts", state.current_status_filter_locked())
        handle_key("c", state, lambda force=False: refreshed.append(force), opened.append)
        self.assertEqual("all", state.current_status_filter_locked())

        # Drilldown from Devices tab ('3') -> Enter filters Sensors to APP-01
        handle_key("3", state, lambda force=False: refreshed.append(force), opened.append)
        self.assertEqual("devices", state.current_tab())
        handle_key("\n", state, lambda force=False: refreshed.append(force), opened.append)
        self.assertEqual("sensors", state.current_tab())
        self.assertEqual("APP-01", state.device_filter)
        self.assertEqual({"105", "1002"}, {s["objid"] for s in state.visible_alerts()})

        # Drilldown from Groups tab ('4') -> Enter filters Sensors to Servers
        handle_key("4", state, lambda force=False: refreshed.append(force), opened.append)
        handle_key("\n", state, lambda force=False: refreshed.append(force), opened.append)
        self.assertEqual("Servers", state.group_filter)

        # Open Sensor Inspector on selected sensor
        handle_key(
            "\n",
            state,
            lambda force=False: refreshed.append(force),
            opened.append,
            inspector_refresh_cb=lambda **kw: inspector_calls.append(kw),
        )
        self.assertTrue(state.inspector_open)
        self.assertEqual(1, len(inspector_calls))

        # Populate historic multi-channel data & test channel/window/subview navigation
        objid = state.inspector_sensor["objid"]
        state.apply_inspector_data(
            objid,
            details={
                "objid": objid,
                "name": "Disk Free",
                "sensortype": "snmpdisk",
                "probename": "Local",
                "parentgroupname": "Servers",
                "parentdevicename": "APP-01",
                "interval": "60",
                "uptime": "99.1%",
                "downtime": "0.9%",
                "lastcheck": "10:42:00 [20 s ago]",
                "lastvalue": "4 %",
                "lastmessage": "Ack by ops",
                "statustext": "Down (Acknowledged)",
                "statusid": 13,
            },
            channels=[
                {"name": "Free Space", "lastvalue": "4 %", "lastvalue_raw": 4.0},
                {"name": "Total Bytes", "lastvalue": "100 GB", "lastvalue_raw": 1e11},
            ],
            historic={
                "timestamps": ["10:00", "10:05", "10:10"],
                "coverage": ["100 %", "100 %", "100 %"],
                "channels": ["Free Space", "Total Bytes"],
                "series": {"Free Space": [6.0, 5.0, 4.0], "Total Bytes": [1e11, 1e11, 1e11]},
                "stats": {
                    "Free Space": {"min": 4.0, "max": 6.0, "avg": 5.0, "last": 4.0, "count": 3},
                    "Total Bytes": {"min": 1e11, "max": 1e11, "avg": 1e11, "last": 1e11, "count": 3},
                },
            },
            messages=[{"objid": objid, "datetime": "10:00", "status": "Down", "message": "Ack"}],
        )

        # Render all 3 inspector views via FakeWindow
        win = FakeWindow(34, 110)
        for key_view, expected_text in [
            ("1", "Historical Chart"),
            ("2", "Full Details"),
            ("3", "Sensor History"),
        ]:
            handle_key(key_view, state, lambda force=False: None, opened.append)
            render_tui(win, state)
            self.assertIn(expected_text, win.rendered_text())

        # Cycle channel ('n') and history window ('t')
        handle_key("1", state, lambda force=False: None, opened.append)
        handle_key("n", state, lambda force=False: None, opened.append)
        self.assertEqual(1, state.inspector_channel_index)

        handle_key(
            "t",
            state,
            lambda force=False: None,
            opened.append,
            inspector_refresh_cb=lambda **kw: inspector_calls.append(kw),
        )
        self.assertEqual("7d", state.current_inspector_window_locked())
        self.assertTrue(inspector_calls[-1]["history_only"])

        # Close inspector ('b')
        handle_key("b", state, lambda force=False: None, opened.append)
        self.assertFalse(state.inspector_open)

        # Render Logs tab ('5') and cycle log range ('t')
        handle_key("5", state, lambda force=False: refreshed.append(force), opened.append)
        render_tui(win, state)
        self.assertIn("Low space", win.rendered_text())
        handle_key("t", state, lambda force=False: refreshed.append(force), opened.append)
        self.assertEqual("30days", state.current_log_range_locked())

    def test_render_tui_scenarios_with_fake_window(self):
        state = TUIState(
            mode="live",
            prtg_url="https://x.bytesec.app",
            config_path="/tmp/config.json",
            state_path="/tmp/state.json",
        )
        state.apply_snapshot(sample_alerts(), updated_at=time.time() - 5)

        win = FakeWindow(30, 100)
        render_tui(win, state, now_mono=time.monotonic(), now_epoch=time.time())
        text = win.rendered_text()
        self.assertIn("PRTG Monitor TUI", text)
        self.assertIn("https://x.bytesec.app", text)
        self.assertIn("X-POSTGRESQL", text)
        self.assertIn("Memory: Physical memory", text)
        self.assertIn("https://x.bytesec.app/sensor.htm?id=2113", text)
        self.assertTrue(win.refreshed)

        state.filter_input_active = True
        state.filter_draft = "pg"
        render_tui(win, state)
        self.assertIn("Filter: pg█", win.rendered_text())
        state.filter_input_active = False

        state.last_error = "PRTG server returned HTTP 503"
        render_tui(win, state)
        self.assertIn("PRTG server returned HTTP 503", win.rendered_text())
        state.last_error = None

        state.show_help = True
        render_tui(win, state)
        self.assertIn("Help - PRTG Monitor TUI", win.rendered_text())
        state.show_help = False

        state.apply_snapshot([], updated_at=time.time())
        render_tui(win, state)
        self.assertIn("No active alerts right now", win.rendered_text())

        tiny = FakeWindow(10, 40)
        render_tui(tiny, state)
        self.assertIn("Terminal too small", tiny.rendered_text())

    def test_open_sensor_url_validates_scheme(self):
        url = build_sensor_url("https://x.bytesec.app/", "2069")
        self.assertEqual("https://x.bytesec.app/sensor.htm?id=2069", url)

        with mock.patch("subprocess.Popen") as mock_popen:
            self.assertTrue(open_sensor_url(url))
            self.assertEqual(["open", url], mock_popen.call_args[0][0])

            mock_popen.reset_mock()
            self.assertFalse(open_sensor_url("file:///etc/passwd"))
            self.assertFalse(open_sensor_url("javascript:alert(1)"))
            self.assertFalse(mock_popen.called)

    def test_set_log_handler_intercepts_and_restores_stdout_logging(self):
        captured = []
        previous = prtg_monitor.set_log_handler(lambda msg, lvl: captured.append((lvl, msg)))
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                prtg_monitor.log("hello tui", "warning")
            self.assertEqual("", buf.getvalue())
            self.assertEqual([("warning", "hello tui")], captured)
        finally:
            prtg_monitor.set_log_handler(previous)

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            prtg_monitor.log("restored stdout", "info")
        self.assertIn("restored stdout", buf2.getvalue())

    def test_tui_runner_captures_errors_without_touching_state_file(self):
        monitor = mock.MagicMock()
        monitor.config = {"apitoken": "secret-tok", "prtg_url": "https://prtg.example"}
        monitor.state_path = None

        def failing_fetch():
            prtg_monitor.log("Network error querying PRTG server: https://prtg.example?apitoken=secret-tok", "error")
            return None

        monitor.fetch_active_errors.side_effect = failing_fetch
        state = TUIState(mode="live", prtg_url="https://prtg.example")
        state.apply_snapshot(sample_alerts(), updated_at=1000.0)

        runner = TUIRunner(monitor, state, client=mock.MagicMock(spec=[]))
        prev = prtg_monitor.set_log_handler(runner.log_sink)
        try:
            runner._run_live_fetch()
        finally:
            prtg_monitor.set_log_handler(prev)

        self.assertEqual(3, len(state.alerts))
        self.assertIn("REDACTED", state.last_error)
        self.assertNotIn("secret-tok", state.last_error)

    def test_tui_runner_explorer_and_inspector_fetch_with_prtg_client(self):
        monitor = mock.MagicMock()
        monitor.config = {"apitoken": "secret-tok", "prtg_url": "https://prtg.example"}
        monitor.state_path = None

        client = mock.MagicMock()
        client.fetch_all_sensors.return_value = (sample_all_sensors(), None)
        client.fetch_devices.return_value = ([{"objid": "40", "device": "APP-01", "down": 1, "partial": 0, "warn": 0, "up": 1, "paused": 1}], None)
        client.fetch_groups.return_value = ([{"objid": "1", "group": "Servers", "down": 1, "warn": 0, "up": 1, "paused": 1}], None)
        client.fetch_messages.return_value = ([{"objid": "105", "datetime": "10:00", "status": "Down", "name": "Disk", "message": "Low"}], None)
        client.fetch_server_status.return_value = ({"Version": "26.1.116"}, None)
        client.fetch_sensor_details.return_value = ({"objid": "2113", "name": "Memory"}, None)
        client.fetch_sensor_channels.return_value = ([{"name": "Avail", "lastvalue": "3 %", "lastvalue_raw": 3.0}], None)
        client.fetch_historic_data.return_value = ({"channels": ["Avail"], "series": {"Avail": [3.0]}, "stats": {}, "timestamps": ["10:00"]}, None)

        state = TUIState(mode="live", prtg_url="https://prtg.example")
        runner = TUIRunner(monitor, state, client=client)
        runner._run_live_fetch(log_range="7days")

        self.assertTrue(state.live_loaded)
        self.assertEqual(5, len(state.all_sensors))
        self.assertEqual(3, len(state.alerts))
        self.assertEqual("26.1.116", state.server_status["Version"])

        state.open_inspector(sample_all_sensors()[0])
        runner._run_inspector_fetch("2113", "24h", history_only=False)
        self.assertFalse(state.inspector_loading)
        self.assertEqual("Memory", state.inspector_details["name"])
        self.assertEqual(["Avail"], state.inspector_historic["channels"])

    def test_main_requires_tty_and_returns_one(self):
        err_buf = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch("sys.stdout.isatty", return_value=False), \
             contextlib.redirect_stderr(err_buf):
            rc = prtg_tui.main([])
        self.assertEqual(1, rc)
        self.assertIn("TTY", err_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
