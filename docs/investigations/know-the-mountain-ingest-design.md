# Know the Mountain — Sensor Ingestion MVP Design

> Design note for `src/mac/sensor_ingest.py`.  
> Author: Bullwinkle / 2026-11-11.

## Schema

| Field | Type | Unit | Required |
|---|---|---|---|
| `sensor_id` | string | — | yes |
| `location_tag` | string | — | yes |
| `timestamp` | string (ISO 8601) | UTC | yes |
| `creek_level` | number | mm | yes |
| `soil_moisture` | number | % | yes |
| `air_temp` | number | °C | yes |
| `units` | object | — | yes |

The `units` object documents what each numeric column represents.
Example: `{"creek_level":"mm","soil_moisture":"pct","air_temp":"C"}`.

## Storage decision: Postgres in `fleet_brain`

**Chosen:** Postgres table `sensor_readings` in `fleet_brain` on the hub.

**Rationale:**
- Fleet agents already query `fleet_brain`; no new infrastructure.
- `TIMESTAMPTZ` + B-tree index on `ts` enables time-window queries.
- `raw JSONB` preserves the original payload for schema evolution.
- Fleet memory queries can join sensor data with soul graph results.

**Not chosen:** CSV file log — no indexing, no concurrent access, not queryable
by fleet peers.

## Table (auto-created by `write_reading` on first call)

```sql
CREATE TABLE IF NOT EXISTS sensor_readings (
    id            BIGSERIAL PRIMARY KEY,
    sensor_id     TEXT NOT NULL,
    location_tag  TEXT NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    creek_level   DOUBLE PRECISION,
    soil_moisture DOUBLE PRECISION,
    air_temp      DOUBLE PRECISION,
    units         JSONB,
    raw           JSONB,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Running the ingestion server

```bash
# Default port 8800, connects to fleet_brain
python -m mac.sensor_ingest

# Or with explicit DSN and quiet mode
python -m mac.sensor_ingest --port 8800 --db-url postgresql://fleet_agent@100.72.16.110:5432/fleet_brain --quiet
```

## Raspberry Pi integration (curl example)

A Pi running a creek-level gauge (e.g. ultrasonic HC-SR04 or pressure sensor
feeding a GPIO + Python script) would POST every N seconds:

```bash
#!/usr/bin/env bash
# /usr/local/bin/creek-report.sh  — run via cron every 5 min

SENSOR_ID="creek-01"
LOCATION="boulder_creek_upper"
INGEST_URL="http://100.72.16.110:8800/ingest"  # or your fleet IP

# Read your sensor here (replace with real GPIO/ADC call)
CREEK_MM=412
SOIL_PCT=58.3
TEMP_C=14.2

PAYLOAD=$(cat <<JSON
{
  "sensor_id": "$SENSOR_ID",
  "location_tag": "$LOCATION",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "creek_level": $CREEK_MM,
  "soil_moisture": $SOIL_PCT,
  "air_temp": $TEMP_C,
  "units": {"creek_level": "mm", "soil_moisture": "pct", "air_temp": "C"}
}
JSON
)

curl -sf -X POST "$INGEST_URL" \
     -H "Content-Type: application/json" \
     -d "$PAYLOAD" \
  && echo "reported" \
  || echo "ingest failed" >&2
```

Crontab entry on the Pi:
```
*/5 * * * * /usr/local/bin/creek-report.sh >> /var/log/creek-report.log 2>&1
```

## Next steps

- Add authentication (bearer token or IP allowlist) before exposing to internet.
- Add `GET /readings?sensor_id=&from=&to=` for quick time-window queries.
- Rocky can add a Qdrant embedding pipeline for semantic queries over readings.
