import json
import unittest
import urllib.error
import urllib.parse
from datetime import datetime
from unittest import mock

from prtg_api import (
    PRTGClient,
    extract_historic_series,
    format_human_number,
    format_prtg_ole_datetime,
    strip_html,
)


def _mock_json_response(payload):
    body = json.dumps(payload).encode("utf-8")
    resp = mock.MagicMock()
    resp.status = 200
    resp.read.return_value = body
    cm = mock.MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class PRTGAPITests(unittest.TestCase):
    def test_strip_html_and_ole_datetime_and_human_number(self):
        self.assertEqual(
            "19.11.2026 10:41:59 [51 s ago]",
            strip_html('19.11.2026 10:41:59 <span class="percent">[51 s ago]</span>'),
        )
        formatted = format_prtg_ole_datetime("46345.5616209028 [51 s ago]")
        self.assertIn("2026-11-19", formatted)
        self.assertIn("[51 s ago]", formatted)
        self.assertEqual("Plain text", format_prtg_ole_datetime("Plain text"))

        self.assertEqual("0", format_human_number(0))
        self.assertEqual("1.50G", format_human_number(1.5e9))
        self.assertEqual("12.50M", format_human_number(12.5e6))
        self.assertEqual("3.14", format_human_number(3.14159))
        self.assertEqual("-", format_human_number(None))

    def test_extract_historic_series_computes_stats_and_ignores_meta_keys(self):
        histdata = [
            {
                "datetime": "19.11.2026 09:00:00 - 09:05:00",
                "datetime_raw": 46345.5,
                "Percent Available Memory": 3.0,
                "Available Memory": 4000000000.0,
                "coverage": "100 %",
                "coverage_raw": 10000,
            },
            {
                "datetime": "19.11.2026 09:05:00 - 09:10:00",
                "datetime_raw": 46345.51,
                "Percent Available Memory": 5.0,
                "Available Memory": None,
                "coverage": "100 %",
                "coverage_raw": 10000,
            },
        ]
        extracted = extract_historic_series(histdata)
        self.assertEqual(["Percent Available Memory", "Available Memory"], extracted["channels"])
        self.assertEqual(2, len(extracted["timestamps"]))
        self.assertEqual([3.0, 5.0], extracted["series"]["Percent Available Memory"])
        self.assertEqual([4000000000.0, None], extracted["series"]["Available Memory"])
        stats = extracted["stats"]["Percent Available Memory"]
        self.assertEqual(3.0, stats["min"])
        self.assertEqual(5.0, stats["max"])
        self.assertEqual(4.0, stats["avg"])
        self.assertEqual(5.0, stats["last"])
        self.assertEqual(2, stats["count"])

    def test_prtg_client_paginates_sensors_and_fetches_all_endpoints(self):
        client = PRTGClient("https://prtg.example", "secret-token-xyz", verify_ssl=True)

        def fake_urlopen(req, context=None, timeout=None):
            parsed = urllib.parse.urlsplit(req.full_url)
            qs = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(["secret-token-xyz"], qs["apitoken"])

            if parsed.path == "/api/getstatus.htm":
                return _mock_json_response({"Version": "26.1.116", "ReadOnlyUser": "true"})

            if parsed.path == "/api/getsensordetails.json":
                return _mock_json_response({
                    "sensordata": {
                        "name": "CPU Load",
                        "sensortype": "snmpcpu",
                        "probename": "Local",
                        "parentgroupname": "Servers",
                        "parentdevicename": "DB-01",
                        "interval": "60",
                        "uptime": "99.9%",
                        "downtime": "0.1%",
                        "lastmessage": "<b>OK</b>",
                    }
                })

            if parsed.path == "/api/historicdata.json":
                self.assertEqual(["2113"], qs["id"])
                self.assertEqual(["300"], qs["avg"])
                return _mock_json_response({
                    "histdata": [
                        {"datetime": "t1", "CPU Total": 12.5, "coverage": "100 %"},
                        {"datetime": "t2", "CPU Total": 17.5, "coverage": "100 %"},
                    ]
                })

            content = qs.get("content", [""])[0]
            if content == "sensors":
                start = int(qs["start"][0])
                if start == 0:
                    return _mock_json_response({
                        "treesize": 2,
                        "sensors": [
                            {"objid": 10, "device": "DB-01", "sensor": "Ping", "status": "Up", "status_raw": 3, "message_raw": "OK"},
                        ],
                    })
                return _mock_json_response({
                    "treesize": 2,
                    "sensors": [
                        {"objid": 20, "device": "WEB-01", "sensor": "HTTP", "status": "Down", "status_raw": 5, "message_raw": "500"},
                    ],
                })
            if content == "devices":
                return _mock_json_response({
                    "treesize": 1,
                    "devices": [{"objid": 40, "device": "DB-01", "host": "10.0.0.1", "group": "Prod", "downsens_raw": 0, "upsens_raw": 5}],
                })
            if content == "groups":
                return _mock_json_response({
                    "treesize": 1,
                    "groups": [{"objid": 1, "group": "Prod", "probe": "Local", "downsens_raw": 0, "upsens_raw": 5}],
                })
            if content == "channels":
                return _mock_json_response({
                    "channels": [
                        {"name": "", "lastvalue": ""},
                        {"name": "Primary", "lastvalue": "42 %", "lastvalue_raw": 42.0},
                    ]
                })
            if content == "messages":
                return _mock_json_response({
                    "messages": [{"objid": 20, "datetime": "19.11.2026 10:00:00", "status": "Down", "name": "HTTP", "message_raw": "Timeout"}]
                })
            raise AssertionError(f"Unexpected endpoint: {req.full_url}")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            status, err = client.fetch_server_status()
            self.assertIsNone(err)
            self.assertEqual("26.1.116", status["Version"])

            # Force two pages by lowering page_size
            orig = client._fetch_paginated_table
            client._fetch_paginated_table = (
                lambda content, columns, extra_params=None: orig(
                    content, columns=columns, extra_params=extra_params, page_size=1
                )
            )
            sensors, err = client.fetch_all_sensors()
            self.assertIsNone(err)
            self.assertEqual(["10", "20"], [s["objid"] for s in sensors])
            client._fetch_paginated_table = orig

            devices, err = client.fetch_devices()
            self.assertIsNone(err)
            self.assertEqual("10.0.0.1", devices[0]["host"])

            groups, err = client.fetch_groups()
            self.assertIsNone(err)
            self.assertEqual("Prod", groups[0]["group"])

            details, err = client.fetch_sensor_details("2113")
            self.assertIsNone(err)
            self.assertEqual("OK", details["lastmessage"])

            channels, err = client.fetch_sensor_channels("2113")
            self.assertIsNone(err)
            self.assertEqual(1, len(channels))
            self.assertEqual("Primary", channels[0]["name"])

            historic, err = client.fetch_historic_data("2113", window_key="2h", now=datetime(2026, 11, 19, 10, 0, 0))
            self.assertIsNone(err)
            self.assertEqual(["CPU Total"], historic["channels"])
            self.assertEqual(15.0, historic["stats"]["CPU Total"]["avg"])

            msgs, err = client.fetch_messages(objid="20", drel="today")
            self.assertIsNone(err)
            self.assertEqual("Timeout", msgs[0]["message"])

    def test_prtg_client_redacts_token_on_http_and_url_errors(self):
        client = PRTGClient("https://prtg.example", "my-super-secret-token")
        http_err = urllib.error.HTTPError(
            "https://prtg.example/api/table.json?apitoken=my-super-secret-token",
            401,
            "Unauthorized token my-super-secret-token",
            {},
            None,
        )
        with mock.patch("urllib.request.urlopen", side_effect=http_err):
            data, err = client.fetch_all_sensors()
        self.assertIsNone(data)
        self.assertIn("401", err)
        self.assertNotIn("my-super-secret-token", err)

        url_err = urllib.error.URLError("Failed for ?apitoken=my-super-secret-token")
        with mock.patch("urllib.request.urlopen", side_effect=url_err):
            data, err = client.fetch_server_status()
        self.assertIsNone(data)
        self.assertIn("REDACTED", err)
        self.assertNotIn("my-super-secret-token", err)


if __name__ == "__main__":
    unittest.main()
