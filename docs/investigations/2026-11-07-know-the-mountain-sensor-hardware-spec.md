# Know the Mountain — Sensor Hardware Spec

**Author:** Rocky (agent_rocky)  
**Date:** 2026-11-07  
**Task:** task_0b8057afce1742ac961d74e25af10cb8  
**Quest:** ★ GRAND QUEST — Sensor Network for Boulder Creek Watershed  
**Status:** READY TO ORDER

---

## Mission

Build a persistent, real-time sensor network for the Boulder Creek watershed (Boulder Creek Rd / Ben Lomond, Santa Cruz Mountains). Rocky will ingest this data stream continuously and build a world-model of the watershed — continuous physical grounding (C2 in the Compounding Theorem). jkh and Tom's land is the anchor.

Data target: readings every 1–5 min, stored locally on each node, forwarded to a central hub (existing Mac Mini or dedicated Pi). Rocky polls the hub via AgentBus.

---

## Node Architecture

```
Creek Bank Node          Hub (existing Mac)         Rocky
   [MB7389 ultrasonic]  ←── WiFi/LoRa ──→  [ingestion service] → AgentBus → Rocky
   [RPi Zero 2W + HAT]

Soil Node ×2             Hub                        Rocky
   [Capacitive sensor]  ←── WiFi ──→       [ingestor]           → AgentBus → Rocky
   [RPi Pico W]

Camera Node              Hub                        Rocky  
   [RPi Zero 2W + cam3] ←── WiFi ──→       [still/timelapse]    → AgentBus → Rocky

Audio Node               Hub                        Rocky
   [RPi Zero 2W + USB mic] ←── WiFi ──→    [audio events]       → AgentBus → Rocky

Weather Node             Hub                        Rocky
   [BME280/BME680]      ←── WiFi ──→       [env readings]       → AgentBus → Rocky
```

---

## Bill of Materials

### Node 1: Creek Level Gauge

**Purpose:** Measure Boulder Creek water depth continuously. Detect flash flood events, seasonal baseflow, storm response.

| Part | SKU / Source | Qty | Unit Cost | Notes |
|------|-------------|-----|-----------|-------|
| **MaxBotix MB7389-100** HRXL-MaxSonar-WRL | DigiKey MB7389-100 | 1 | ~$108 | IP67, 300–5000mm range, 1mm resolution, TTL serial, operates 3.0–5.5V. The waterproof creek gauge sensor. |
| **Raspberry Pi Zero 2W** | RPi Foundation / Adafruit | 1 | $15 | WiFi built-in, quad-core 1GHz, 512MB RAM. Runs ingestion daemon. |
| **MicroSD 32GB** (Sandisk Endurance) | Amazon | 1 | $12 | High-write-cycle rated for continuous data logging. |
| **5V PoE splitter** or solar + LiPo | Various | 1 | $20–30 | Power via long USB cable from bank, or solar panel + LiPo for remote install. |
| **Hammond 1554B2GY watertight enclosure** | Mouser / Amazon | 1 | $18 | IP65 ABS enclosure, 4.7"×3.0"×2.1". Fits Pi Zero + HAT. |
| **Weatherproof cable gland** (PG7) | Amazon | 2 | $1 | Sensor cable ingress, power ingress. |
| **Logic level converter** (3.3V ↔ 5V) | Adafruit #757 | 1 | $4 | MB7389 TTL is 5V; Pi GPIO is 3.3V. |
| **RJ45 bulkhead + Cat5e** | Amazon | 1 | $8 | Optional: run Ethernet from house if site allows. |

**Node 1 subtotal:** ~$186–196  
**Deployment:** Mount sensor over creek from bridge/overhang on Creek Rd, 0.5–3m above water. Pi in sealed enclosure on bank or underside of bridge. WiFi to house.

---

### Node 2a & 2b: Soil Moisture Nodes (×2)

**Purpose:** Measure volumetric water content at 2 depths (10cm, 30cm) near creek bank. Track saturation events, drought conditions, infiltration after rain.

| Part | SKU / Source | Qty | Unit Cost | Notes |
|------|-------------|-----|-----------|-------|
| **Capacitive Soil Moisture Sensor v2.0** | Amazon / AliExpress | 4 | $4 | Corrosion-resistant vs resistive. Outputs 0–3.3V analog. 2 per node × 2 nodes. |
| **MCP3008 ADC** (8-ch, SPI) | Adafruit #856 | 2 | $4 | Converts analog sensor → Pi digital GPIO. |
| **Raspberry Pi Pico W** | RPi Foundation | 2 | $6 | WiFi + MicroPython. Perfect for low-power sensor node. Alternative: RPi Zero 2W if you need Linux. |
| **MicroUSB power** (from solar or long cable) | — | 2 | $10 | LiPo + small solar panel per node. |
| **IP67 junction box** (100×68×50mm) | Amazon | 2 | $8 | Small enclosure for Pico + ADC. Sensor probes protrude into soil. |
| **Silicone sealant** | Hardware store | 1 | $5 | Seal cable entry points. |

**Node 2 subtotal (both nodes):** ~$74  
**Deployment:** Node 2a — 50m from creek, upper bank. Node 2b — edge of riparian zone. Probes buried at 10cm and 30cm depth. Pico sits in sealed box above soil surface.

---

### Node 3: Camera Node

**Purpose:** Visual time-lapse of creek and surrounding forest. Flood detection, wildlife, seasonal change. Feeds image stream to Rocky.

| Part | SKU / Source | Qty | Unit Cost | Notes |
|------|-------------|-----|-----------|-------|
| **Raspberry Pi Zero 2W** | RPi Foundation | 1 | $15 | Runs rpicam-still / rpicam-apps. |
| **Camera Module 3** (standard) | RPi Foundation | 1 | $25 | 12MP, autofocus IMX708. Wide angle variant ($35) for creek view. |
| **MicroSD 64GB** (Sandisk Endurance) | Amazon | 1 | $18 | Timelapse images accumulate fast. |
| **Weatherproof enclosure with clear window** | Amazon (Zulkit IP65 box) | 1 | $22 | 200×120×75mm, with clear acrylic lid for camera window. |
| **5V/3A USB-C power supply + outdoor extension** | Amazon | 1 | $15 | Run from nearest outlet. |
| **PIR motion sensor** (HC-SR501) | Amazon | 1 | $3 | Optional: event-triggered capture in addition to time-lapse. |
| **IR cut filter + IR LEDs** | Amazon | 1 | $12 | Optional: night vision capability. |

**Node 3 subtotal:** ~$110 (without IR) / ~$122 (with IR night vision)  
**Deployment:** Mount 2–4m high on tree or post with view of creek pool. Aim for 180° creek + bank coverage. Time-lapse every 5 min during daylight, event-capture via PIR.

---

### Node 4: Audio / Rain Intensity Node

**Purpose:** Classify rain intensity (drizzle / moderate / heavy / torrential) from acoustic signature. Detect thunder, creek roar, wildlife. Feeds audio events to Rocky.

| Part | SKU / Source | Qty | Unit Cost | Notes |
|------|-------------|-----|-----------|-------|
| **Raspberry Pi Zero 2W** | RPi Foundation | 1 | $15 | Runs audio capture + classification. |
| **USB sound card** (Plugable USB Audio Adapter) | Amazon B00NMXY2MO | 1 | $10 | Pi Zero has no audio input. |
| **Electret microphone** (outdoor, omnidirectional) | Parts Express 390-025 | 1 | $12 | 20Hz–20kHz. Mount in rain shield. |
| **DIY rain shield** (upside-down PVC cap) | Hardware store | 1 | $3 | Keeps direct rain off mic while passing sound. |
| **MicroSD 32GB** | Amazon | 1 | $12 | Rotating buffer of audio segments. |
| **IP65 enclosure** | Amazon | 1 | $14 | For Pi Zero + USB sound card. Mic protrudes through rain shield. |
| **5V power** (USB from outdoor outlet) | — | 1 | $8 | Short cable run. |

**Node 4 subtotal:** ~$74  
**Software:** `sox` + `arecord` for capture; Python classifier using short-time energy + spectral features for rain intensity. BirdNET-Pi integration for wildlife detection bonus.  
**Deployment:** Mount 2m high, sheltered from direct rain. Clear line to open sky for rain acoustic pickup.

---

### Node 5: Micro-Weather Array

**Purpose:** Hyper-local temp, humidity, pressure beyond KCABOULD15 (the existing PWS). Multiple nodes to map microclimate gradient from creek to ridge.

| Part | SKU / Source | Qty | Unit Cost | Notes |
|------|-------------|-----|-----------|-------|
| **BME280 breakout** | Adafruit #2652 or Amazon | 3 | $7 | Temp ±0.5°C, humidity ±3%RH, pressure ±1hPa. I2C, 3.3V. One per location. |
| **Raspberry Pi Pico W** | RPi Foundation | 2 | $6 | Runs 2 sensors each (I2C addresses differ with addr pin). Share hub Pi. |
| **Radiation shield** (Stevenson screen style) | Amazon / print | 3 | $15 | Louvered plastic housing keeps sensor shielded from direct sun/rain. |
| **Weatherproof box** (small) | Amazon | 2 | $8 | For Pico W. |
| **5V power** | — | 2 | $8 | Long USB or solar LiPo. |
| **Jumper wire / 3m cable** | Amazon | — | $5 | I2C extension to sensors outside enclosure. |

**Node 5 subtotal:** ~$107  
**Deployment:** 3 locations: (a) creek bank (N-facing, cool/humid), (b) mid-slope, (c) upper property near ridge. Maps the temperature inversion and cold air drainage common in this canyon.

---

## Central Hub

**Hardware:** Existing Mac Mini (or dedicate a RPi 4 2GB at $35 if isolation is preferred).  
**Software:**
- `rocky-sensor-ingestor` Python service (to be built): polls each node via HTTP/MQTT, writes to TimescaleDB or InfluxDB local instance.
- AgentBus topic: `watershed/creek-level`, `watershed/soil`, `watershed/camera`, `watershed/audio`, `watershed/weather`
- Rocky polls AgentBus; raw readings and anomaly events surface in contemplation loop.

---

## Budget Summary

| Node | Description | Estimated Cost |
|------|-------------|---------------|
| Node 1 | Creek level gauge (MB7389 + RPi Zero 2W) | $190 |
| Node 2a + 2b | Soil moisture × 2 (Pico W) | $74 |
| Node 3 | Camera (RPi Zero 2W + Cam3) | $120 |
| Node 4 | Audio / rain intensity (RPi Zero 2W + mic) | $74 |
| Node 5 | Micro-weather array × 3 (Pico W + BME280) | $107 |
| **TOTAL** | | **~$565** |
| Shipping + misc | Cables, connectors, zip ties, silicone, misc | +$40 |
| **GRAND TOTAL** | | **~$605** |

> Budget does not include hub hardware (existing Mac Mini suffices). Does not include outdoor power extension runs — budget $20–50 for those site-specifically.

---

## Ordering Priority

**Phase 1 — Creek Level + Weather** (most grounding value, simplest to deploy):
1. MB7389-100 from DigiKey — $108
2. RPi Zero 2W × 2 (creek + weather) — $30
3. BME280 × 3 from Amazon/Adafruit — $21
4. Enclosures, cable glands, MicroSD — $60
5. Logic level converter — $4

**Phase 1 cost: ~$223** — order this first.

**Phase 2 — Camera + Audio** after Phase 1 is running and reporting to Rocky.

**Phase 3 — Soil Nodes** requires digging; plan for dry season.

---

## Rocky's Ingestion Plan

Each node runs a minimal Python/MicroPython daemon that:
1. Reads sensor at configured interval (1 min default, 5s during storm events)
2. POSTs JSON to hub ingestor (`http://hub.local:8765/ingest`)
3. Hub stores in local TimescaleDB + publishes to AgentBus

Rocky reads AgentBus events via:
```python
# In Rocky's cron loop (sketch)
events = agentbus.poll('watershed/#', since=last_poll)
for ev in events:
    world_model.update(ev)
    if ev.anomaly:
        mg.add_node(f"WATERSHED ANOMALY: {ev}", tags=['creek', 'anomaly'])
```

The world model accumulates: creek level baselines, storm response curves, seasonal soil moisture patterns, wildlife acoustic events, microclimate gradients.

---

## Deployment Notes for jkh

- **Creek gauge mounting:** A simple aluminum angle bracket from Home Depot, bolted to bridge railing or clamped to overhanging branch. Sensor faces straight down toward water. Needs to be above max flood level (~2m clearance from normal water).
- **WiFi range:** Boulder Creek Rd is tree-dense. Plan for 50–100m range from house. If range is marginal, add a WiFi repeater ($30 Eero or similar) or switch to LoRa (longer range, lower data rate — fine for sensor readings).
- **Power:** Priority: run long outdoor-rated USB cables from house circuits. Backup: 10W solar panel + 5000mAh LiPo for each node (~$35/node extra).
- **Weatherproofing:** Silicon all cable glands. Desiccant pack inside each enclosure. Check seals before winter rainy season.

---

*Spec written by Rocky | 2026-11-07 | task_0b8057afce1742ac961d74e25af10cb8*  
*This is the C2 foundation of Know the Mountain — not modeled, but measurably real.*
