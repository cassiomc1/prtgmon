import unittest
import urllib.parse

from prtg_monitor import PRTGMonitor


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


if __name__ == "__main__":
    unittest.main()
