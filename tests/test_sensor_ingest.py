"""
Tests for mac.sensor_ingest -- MVP sensor ingestion for Know the Mountain.

Covers validation and storage using a mock/in-memory path so no real
Postgres connection is needed in CI.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import under test (pure-Python parts -- no psycopg needed for validation)
# ---------------------------------------------------------------------------
from mac.sensor_ingest import validate_reading, SensorHandler


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

VALID_PAYLOAD: dict = {
    "sensor_id": "creek-01",
    "location_tag": "boulder_creek_upper",
    "timestamp": "2026-11-11T15:00:00Z",
    "creek_level": 412.0,
    "soil_moisture": 58.3,
    "air_temp": 14.2,
    "units": {"creek_level": "mm", "soil_moisture": "pct", "air_temp": "C"},
}


def _payload(**overrides) -> dict:
    p = dict(VALID_PAYLOAD)
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# Validation tests (Phase RED-GREEN-REFACTOR: validate_reading)
# ---------------------------------------------------------------------------


class TestValidateReading(unittest.TestCase):
    def test_valid_payload_returns_no_errors(self):
        errors = validate_reading(VALID_PAYLOAD)
        self.assertEqual(errors, [])

    def test_missing_sensor_id_returns_error(self):
        p = dict(VALID_PAYLOAD)
        del p["sensor_id"]
        errors = validate_reading(p)
        self.assertTrue(any("sensor_id" in e for e in errors), errors)

    def test_missing_creek_level_returns_error(self):
        p = dict(VALID_PAYLOAD)
        del p["creek_level"]
        errors = validate_reading(p)
        self.assertTrue(any("creek_level" in e for e in errors), errors)

    def test_missing_timestamp_returns_error(self):
        p = dict(VALID_PAYLOAD)
        del p["timestamp"]
        errors = validate_reading(p)
        self.assertTrue(any("timestamp" in e for e in errors), errors)

    def test_missing_units_returns_error(self):
        p = dict(VALID_PAYLOAD)
        del p["units"]
        errors = validate_reading(p)
        self.assertTrue(any("units" in e for e in errors), errors)

    def test_non_numeric_creek_level_returns_error(self):
        errors = validate_reading(_payload(creek_level="high"))
        self.assertTrue(any("creek_level" in e for e in errors), errors)

    def test_non_numeric_soil_moisture_returns_error(self):
        errors = validate_reading(_payload(soil_moisture=None))
        self.assertTrue(any("soil_moisture" in e for e in errors), errors)

    def test_non_numeric_air_temp_returns_error(self):
        errors = validate_reading(_payload(air_temp="warm"))
        self.assertTrue(any("air_temp" in e for e in errors), errors)

    def test_bad_timestamp_returns_error(self):
        errors = validate_reading(_payload(timestamp="not-a-date"))
        self.assertTrue(any("timestamp" in e for e in errors), errors)

    def test_units_as_list_returns_error(self):
        errors = validate_reading(_payload(units=["mm", "pct", "C"]))
        self.assertTrue(any("units" in e for e in errors), errors)

    def test_empty_sensor_id_returns_error(self):
        errors = validate_reading(_payload(sensor_id=""))
        self.assertTrue(any("sensor_id" in e for e in errors), errors)

    def test_empty_location_tag_returns_error(self):
        errors = validate_reading(_payload(location_tag="   "))
        self.assertTrue(any("location_tag" in e for e in errors), errors)

    def test_integer_numerics_accepted(self):
        """Integer creek_level/soil_moisture/air_temp should be valid."""
        errors = validate_reading(_payload(creek_level=412, soil_moisture=58, air_temp=14))
        self.assertEqual(errors, [])

    def test_iso8601_with_plus_offset_accepted(self):
        errors = validate_reading(_payload(timestamp="2026-11-11T07:00:00+00:00"))
        self.assertEqual(errors, [])

    def test_multiple_missing_fields_all_reported(self):
        p = {}
        errors = validate_reading(p)
        # Should report all REQUIRED_FIELDS as missing
        self.assertGreater(len(errors), 1)


# ---------------------------------------------------------------------------
# HTTP handler tests (mock write_reading so no real DB)
# ---------------------------------------------------------------------------


def _make_request(method: str, path: str, body: bytes = b"") -> MagicMock:
    rfile = io.BytesIO(body)
    mock_req = MagicMock()
    mock_req.makefile = MagicMock(return_value=rfile)
    return mock_req


class _FakeSocket:
    def makefile(self, *a, **kw):
        return io.BytesIO(b"")

    def getsockname(self):
        return ("127.0.0.1", 8800)

    def getpeername(self):
        return ("127.0.0.1", 12345)


class TestSensorHandler(unittest.TestCase):
    def _handler(
        self, method: str, path: str, body: bytes = b""
    ) -> tuple[SensorHandler, io.BytesIO]:
        """Create a handler with mocked socket, return (handler, response_buf)."""
        response_buf = io.BytesIO()
        handler = SensorHandler.__new__(SensorHandler)
        handler.db_url = None
        handler.log_requests = False
        handler.rfile = io.BytesIO(body)
        handler.wfile = response_buf
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = method
        handler.path = path
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        handler.client_address = ("127.0.0.1", 12345)
        # Patch send methods to write to response_buf
        sent_code = []
        sent_headers = {}

        def fake_send_response(code, message=None):
            sent_code.append(code)

        def fake_send_header(key, val):
            sent_headers[key] = val

        def fake_end_headers():
            pass

        handler.send_response = fake_send_response
        handler.send_header = fake_send_header
        handler.end_headers = fake_end_headers
        handler._sent_code = sent_code
        handler._sent_headers = sent_headers
        return handler, response_buf

    def test_health_returns_200(self):
        handler, buf = self._handler("GET", "/health")
        handler.do_GET()
        self.assertEqual(handler._sent_code, [200])
        response = json.loads(buf.getvalue())
        self.assertEqual(response["status"], "ok")

    def test_unknown_path_returns_404(self):
        handler, buf = self._handler("GET", "/unknown")
        handler.do_GET()
        self.assertEqual(handler._sent_code, [404])

    def test_post_valid_payload_calls_write_and_returns_201(self):
        body = json.dumps(VALID_PAYLOAD).encode()
        handler, buf = self._handler("POST", "/ingest", body)
        with patch(
            "mac.sensor_ingest.write_reading",
            return_value={"id": 1, "ingested_at": "2026-11-11T15:00:00+00:00"},
        ):
            handler.do_POST()
        self.assertEqual(handler._sent_code, [201])
        response = json.loads(buf.getvalue())
        self.assertEqual(response["ack"], "ok")
        self.assertEqual(response["id"], 1)

    def test_post_invalid_payload_returns_422(self):
        body = json.dumps({"sensor_id": "x"}).encode()  # many fields missing
        handler, buf = self._handler("POST", "/ingest", body)
        handler.do_POST()
        self.assertEqual(handler._sent_code, [422])
        response = json.loads(buf.getvalue())
        self.assertEqual(response["error"], "validation failed")
        self.assertIn("details", response)

    def test_post_malformed_json_returns_400(self):
        handler, buf = self._handler("POST", "/ingest", b"{not json}")
        handler.do_POST()
        self.assertEqual(handler._sent_code, [400])

    def test_post_storage_error_returns_500(self):
        body = json.dumps(VALID_PAYLOAD).encode()
        handler, buf = self._handler("POST", "/ingest", body)
        with patch("mac.sensor_ingest.write_reading", side_effect=RuntimeError("db down")):
            handler.do_POST()
        self.assertEqual(handler._sent_code, [500])
        response = json.loads(buf.getvalue())
        self.assertIn("storage error", response["error"])

    def test_post_to_wrong_path_returns_404(self):
        body = json.dumps(VALID_PAYLOAD).encode()
        handler, buf = self._handler("POST", "/other", body)
        handler.do_POST()
        self.assertEqual(handler._sent_code, [404])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# GET /readings tests (Phase RED -- query endpoint not yet implemented)
# ---------------------------------------------------------------------------


class TestGetReadings(unittest.TestCase):
    """Tests for GET /readings?sensor_id=&limit=&since= query endpoint."""

    def _make_server(self):
        """Return a SensorHandler instance with a mock DB."""
        handler = SensorHandler.__new__(SensorHandler)
        handler.db_url = None
        handler.log_requests = False
        return handler

    def _do_get(self, path: str, mock_rows: list[dict] | None = None):
        """Drive do_GET on a mock server, return (status_code, body_dict)."""
        sent_status = []
        sent_headers = []
        sent_body = io.BytesIO()

        handler = SensorHandler.__new__(SensorHandler)
        handler.db_url = None
        handler.log_requests = False
        handler.path = path
        handler.headers = {}

        def fake_send_response(code):
            sent_status.append(code)

        def fake_send_header(k, v):
            sent_headers.append((k, v))

        def fake_end_headers():
            pass

        handler.send_response = fake_send_response
        handler.send_header = fake_send_header
        handler.end_headers = fake_end_headers
        handler.wfile = sent_body

        if mock_rows is not None:
            with patch("mac.sensor_ingest.query_readings", return_value=mock_rows):
                handler.do_GET()
        else:
            handler.do_GET()

        sent_body.seek(0)
        raw = sent_body.read()
        body = json.loads(raw) if raw else {}
        return sent_status[0] if sent_status else None, body

    def test_get_readings_returns_200_with_empty_list(self):
        code, body = self._do_get("/readings", mock_rows=[])
        self.assertEqual(code, 200)
        self.assertIn("readings", body)
        self.assertEqual(body["readings"], [])

    def test_get_readings_returns_rows(self):
        rows = [
            {
                "id": 1,
                "sensor_id": "creek-01",
                "location_tag": "boulder_creek_upper",
                "ts": "2026-11-11T15:00:00+00:00",
                "creek_level": 412.0,
                "soil_moisture": 58.3,
                "air_temp": 14.2,
                "units": {"creek_level": "mm"},
                "ingested_at": "2026-11-11T15:01:00+00:00",
            }
        ]
        code, body = self._do_get("/readings?sensor_id=creek-01", mock_rows=rows)
        self.assertEqual(code, 200)
        self.assertEqual(len(body["readings"]), 1)
        self.assertEqual(body["readings"][0]["sensor_id"], "creek-01")

    def test_get_readings_bad_limit_returns_400(self):
        code, body = self._do_get("/readings?limit=notanumber")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_get_readings_limit_too_large_returns_400(self):
        code, body = self._do_get("/readings?limit=10000")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_query_readings_function_exists(self):
        """query_readings must be importable -- fails RED until implemented."""
        from mac.sensor_ingest import query_readings

        self.assertTrue(callable(query_readings))


class TestQueryReadingsFunction(unittest.TestCase):
    """Unit tests for the query_readings() DB function."""

    def test_query_readings_returns_list(self):
        from mac.sensor_ingest import query_readings

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []
        import sys, types

        fake_psycopg = types.ModuleType("psycopg")
        fake_psycopg.connect = MagicMock(return_value=mock_conn)
        fake_row = types.ModuleType("psycopg.rows")
        fake_row.dict_row = dict
        sys.modules["psycopg"] = fake_psycopg
        sys.modules["psycopg.rows"] = fake_row
        try:
            result = query_readings(sensor_id=None, limit=10, db_url="postgresql://fake/db")
        finally:
            del sys.modules["psycopg"]
            del sys.modules["psycopg.rows"]
        self.assertIsInstance(result, list)
