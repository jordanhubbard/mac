"""
sensor_ingest.py - MVP sensor ingestion for the Know the Mountain project.

Accepts JSON POST payloads of creek/soil/air sensor readings, validates
required fields, and writes them to fleet_brain Postgres.

Schema: creek_level (mm), soil_moisture (%), air_temp (C), timestamp (ISO8601),
        sensor_id (str), location_tag (str), units (dict).

Usage:
    python -m mac.sensor_ingest [--host 0.0.0.0] [--port 8800] [--db-url DSN]

Standalone (no deps beyond stdlib + psycopg):
    curl -X POST http://localhost:8800/ingest \
         -H 'Content-Type: application/json' \
         -d '{"sensor_id":"creek-01","location_tag":"boulder_creek_upper",
              "timestamp":"2026-11-11T15:00:00Z",
              "creek_level":412,"soil_moisture":58.3,"air_temp":14.2,
              "units":{"creek_level":"mm","soil_moisture":"pct","air_temp":"C"}}'
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

REQUIRED_FIELDS: list[str] = [
    "sensor_id",
    "location_tag",
    "timestamp",
    "creek_level",
    "soil_moisture",
    "air_temp",
    "units",
]

NUMERIC_FIELDS: list[str] = ["creek_level", "soil_moisture", "air_temp"]


def validate_reading(payload: dict[str, Any]) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in payload:
            errors.append(f"missing required field: {field}")

    if not errors:
        for field in NUMERIC_FIELDS:
            if not isinstance(payload[field], (int, float)):
                errors.append(f"{field} must be numeric, got {type(payload[field]).__name__}")

        # Validate ISO8601 timestamp
        try:
            datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            errors.append(f"timestamp is not valid ISO8601: {payload.get('timestamp')!r}")

        if not isinstance(payload.get("units"), dict):
            errors.append("units must be a JSON object")

        if not isinstance(payload.get("sensor_id"), str) or not payload["sensor_id"].strip():
            errors.append("sensor_id must be a non-empty string")

        if not isinstance(payload.get("location_tag"), str) or not payload["location_tag"].strip():
            errors.append("location_tag must be a non-empty string")

    return errors


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    id          BIGSERIAL PRIMARY KEY,
    sensor_id   TEXT        NOT NULL,
    location_tag TEXT       NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    creek_level DOUBLE PRECISION,
    soil_moisture DOUBLE PRECISION,
    air_temp    DOUBLE PRECISION,
    units       JSONB,
    raw         JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sensor_readings_ts_idx
    ON sensor_readings (ts DESC);

CREATE INDEX IF NOT EXISTS sensor_readings_sensor_id_idx
    ON sensor_readings (sensor_id);
"""


def _get_db_url() -> str:
    return os.environ.get(
        "SENSOR_DB_URL",
        os.environ.get("MAC_DB_URL", "postgresql://fleet_agent@100.72.16.110:5432/fleet_brain"),
    )


def write_reading(payload: dict[str, Any], db_url: str | None = None) -> dict[str, Any]:
    """
    Insert a validated reading into Postgres fleet_brain.sensor_readings.
    Returns a dict with the new row id and the ingested_at timestamp.
    Raises on DB error.
    """
    try:
        import psycopg  # type: ignore[import]
        from psycopg.rows import dict_row  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("psycopg is required: pip install 'psycopg[binary]'") from exc

    url = db_url or _get_db_url()
    ts = payload["timestamp"].replace("Z", "+00:00")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        # Ensure table exists (idempotent)
        conn.execute(_CREATE_TABLE_SQL)
        row = conn.execute(
            """
            INSERT INTO sensor_readings
                (sensor_id, location_tag, ts,
                 creek_level, soil_moisture, air_temp,
                 units, raw)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, ingested_at
            """,
            (
                payload["sensor_id"],
                payload["location_tag"],
                ts,
                float(payload["creek_level"]),
                float(payload["soil_moisture"]),
                float(payload["air_temp"]),
                json.dumps(payload["units"]),
                json.dumps(payload),
            ),
        ).fetchone()
        conn.commit()

    return {
        "id": row["id"],
        "ingested_at": row["ingested_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class SensorHandler(BaseHTTPRequestHandler):
    db_url: str | None = None
    log_requests: bool = True

    def log_message(self, fmt: str, *args: Any) -> None:  # type: ignore[override]
        if self.log_requests:
            super().log_message(fmt, *args)

    def _send_json(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "sensor_ingest"})
            return

        if path == "/readings":
            from urllib.parse import parse_qs

            qs = parse_qs(parsed.query)

            # Validate limit
            raw_limit = qs.get("limit", ["50"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                self._send_json(400, {"error": f"limit must be an integer, got {raw_limit!r}"})
                return
            if not (1 <= limit <= MAX_QUERY_LIMIT):
                self._send_json(400, {"error": f"limit must be 1-{MAX_QUERY_LIMIT}, got {limit}"})
                return

            sensor_id = qs.get("sensor_id", [None])[0]
            since = qs.get("since", [None])[0]

            try:
                rows = query_readings(
                    sensor_id=sensor_id, limit=limit, since=since, db_url=self.db_url
                )
            except Exception as exc:
                self._send_json(500, {"error": f"query error: {exc}"})
                return

            self._send_json(200, {"readings": rows, "count": len(rows)})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ingest":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return

        errors = validate_reading(payload)
        if errors:
            self._send_json(422, {"error": "validation failed", "details": errors})
            return

        try:
            result = write_reading(payload, db_url=self.db_url)
        except Exception as exc:
            self._send_json(500, {"error": f"storage error: {exc}"})
            return

        self._send_json(
            201, {"ack": "ok", "id": result["id"], "ingested_at": result["ingested_at"]}
        )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _make_handler(db_url: str | None, quiet: bool) -> type[SensorHandler]:
    class Handler(SensorHandler):
        pass

    Handler.db_url = db_url
    Handler.log_requests = not quiet
    return Handler


def serve(
    host: str = "0.0.0.0", port: int = 8800, db_url: str | None = None, quiet: bool = False
) -> None:
    handler = _make_handler(db_url, quiet)
    server = HTTPServer((host, port), handler)
    print(f"sensor_ingest listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Know the Mountain — sensor ingestion MVP")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--db-url", default=None, help="Postgres DSN (default: SENSOR_DB_URL env)")
    parser.add_argument("--quiet", action="store_true", help="suppress request logging")
    args = parser.parse_args()

    serve(host=args.host, port=args.port, db_url=args.db_url, quiet=args.quiet)


# ---------------------------------------------------------------------------
# Query / read endpoint
# ---------------------------------------------------------------------------

MAX_QUERY_LIMIT = 1000


def query_readings(
    sensor_id: str | None = None,
    limit: int = 50,
    since: str | None = None,
    db_url: str | None = None,
) -> list[dict[str, Any]]:
    """
    Query recent sensor readings from fleet_brain Postgres.

    Args:
        sensor_id: Optional filter by sensor ID.
        limit: Max rows to return (1–1000).
        since: Optional ISO8601 lower bound on ts (inclusive).
        db_url: Postgres DSN (default: SENSOR_DB_URL env).

    Returns:
        List of dicts with keys: id, sensor_id, location_tag, ts,
        creek_level, soil_moisture, air_temp, units, ingested_at.
    """
    try:
        import psycopg  # type: ignore[import]
        from psycopg.rows import dict_row  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("psycopg is required: pip install 'psycopg[binary]'") from exc

    url = db_url or _get_db_url()
    clauses: list[str] = []
    params: list[Any] = []

    if sensor_id:
        clauses.append("sensor_id = %s")
        params.append(sensor_id)

    if since:
        since_ts = since.replace("Z", "+00:00")
        clauses.append("ts >= %s")
        params.append(since_ts)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    sql = f"""
        SELECT id, sensor_id, location_tag,
               ts AT TIME ZONE 'UTC' AS ts,
               creek_level, soil_moisture, air_temp,
               units, ingested_at AT TIME ZONE 'UTC' AS ingested_at
        FROM sensor_readings
        {where}
        ORDER BY ts DESC
        LIMIT %s
    """

    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(sql, params).fetchall()

    # Serialise datetime objects to ISO strings for JSON compatibility
    result = []
    for row in rows:
        r = dict(row)
        for key in ("ts", "ingested_at"):
            if hasattr(r.get(key), "isoformat"):
                r[key] = r[key].isoformat()
        if isinstance(r.get("units"), str):
            try:
                r["units"] = json.loads(r["units"])
            except (ValueError, TypeError):
                pass
        result.append(r)
    return result
