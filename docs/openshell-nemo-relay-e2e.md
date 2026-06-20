# OpenShell + NeMo Relay: Docker E2E Verification

Status: **Container e2e harness** (skipped unless `TEST_DOCKER_E2E=1`)

This document describes the containerized end-to-end verification harness for the
OpenShell sandbox + NeMo Relay observability integration. It complements the
architecture guide in `docs/openshell-nemo-relay-integration.md`.

Unit/integration coverage of the moving parts lives in the main suite —
`tests/test_openshell_sandbox.py` (the sandbox wrapper) and
`tests/test_relay_observability.py` (the OCSF translator). **This** harness is the
container-level seam: it builds the executor image and exercises the full chain
against the real binary.

---

## Deliverables

| File | Purpose |
|---|---|
| `tests/e2e/test_openshell_nemo_relay.py` | Container-level (`docker_e2e`) pytest suite |
| `tests/e2e/docker-compose.e2e.yaml` | Container topology for executor + relay collector |
| `tests/e2e/otelcol-config.yaml` | OpenTelemetry Collector config for relay export verification |
| `Dockerfile.e2e` | executor image with openshell + nemo-relay installed |
| `docs/openshell-nemo-relay-e2e.md` | This document |

---

## What is Verified (in-container)

### 1. OpenShell sandbox wrap
`task_executor._maybe_wrap_openshell()`:
- `MAC_OPENSHELL_SANDBOX=1` → the openshell binary is prepended to the agent argv (confined execution).
- `MAC_OPENSHELL_SANDBOX=0` / unset → argv unchanged (unconfined fallback).
- Binary not in PATH → warning, argv unchanged (safe fallback).
- Policy resolved via `MAC_OPENSHELL_POLICY` or the bundled canonical policy.

### 2. OCSF event → mac observation
`relay_observability.ocsf_to_observation(record)` returns a dict shaped for
`ObservabilityService.record_log` (`kind`, `layer`, `level`, `name`, `source`, `detail`):
- `severity_id` → `level`: `0-2`=info, `3`=warning, `4`=error, `5-6`=critical.
- A denied/blocked enforcement decision (`action`/`disposition` in
  `{denied, deny, blocked}`) is **escalated to at least `warning`**, regardless of
  the record's own severity.
- `class_uid` → `name` (`sandbox.network`/`http`/`ssh`/`process`/`finding`/`config`/`lifecycle`).
- `layer="sandbox"`, `source="openshell"`; the raw OCSF record is preserved in `detail`.

So a denied-egress event (e.g. `severity_id=2, action="denied"`) yields
`level="warning"`, `layer="sandbox"`, `name="sandbox.network"`.

### 3. NeMo Relay scope lifecycle
```python
with relay_observability.create_agent_scope(session_id):
    ...  # executor runs the agent here
relay_observability.flush()
```
With `MAC_RELAY_OBSERVABILITY=1` and `nemo_relay` installed, this opens an Agent
scope and flushes async export buffers afterward; when relay is absent/disabled,
the body is a transparent no-op.

---

## Test Execution

The container tests are marked `docker_e2e` and **skipped automatically** unless
`TEST_DOCKER_E2E=1` is set and `docker compose` is available, so the normal
contract suite stays hermetic.

```bash
# Build the executor image:
docker build -f Dockerfile.e2e -t mac-executor-e2e:latest .

# Run the container e2e suite:
TEST_DOCKER_E2E=1 pytest tests/e2e/test_openshell_nemo_relay.py -v -m docker_e2e

# Or bring up the full compose stack interactively:
docker compose -f tests/e2e/docker-compose.e2e.yaml up
```

For the non-Docker unit coverage (sandbox wrapper, OCSF translator), run the main
suite: `pytest tests/test_openshell_sandbox.py tests/test_relay_observability.py`.

---

## CI Integration

```yaml
# GitHub Actions example
- name: Build e2e image
  run: docker build -f Dockerfile.e2e -t mac-executor-e2e:latest .
- name: Run Docker e2e tests
  env:
    TEST_DOCKER_E2E: "1"
  run: pytest tests/e2e/test_openshell_nemo_relay.py -v -m docker_e2e
```

When Docker is unavailable, the `docker_e2e` tests skip automatically; the
behaviour they exercise is also covered (unit-level) by the main suite.

---

## Environment Variable Reference

| Variable | Default | Effect |
|---|---|---|
| `MAC_OPENSHELL_SANDBOX` | unset/0 | `1` = confined execution via the openshell binary |
| `MAC_OPENSHELL_POLICY` | auto-discover | Path to the YAML policy file for openshell |
| `MAC_RELAY_OBSERVABILITY` | unset/0 | `1` = NeMo Relay export active |
| `NEMO_RELAY_ENDPOINT` | (none) | OTLP endpoint for the relay exporter |
| `TEST_DOCKER_E2E` | unset | `1` = run the `docker_e2e` container tests |

---

## Known Constraints

1. **openshell / Python 3.14 import issue**: the openshell Python SDK has a
   protobuf circular-import bug on Python 3.14; the CLI binary works. `Dockerfile.e2e`
   uses `python:3.11-slim` to avoid it.
2. **Landlock kernel requirement**: full filesystem confinement needs Linux 5.13+
   with `CONFIG_SECURITY_LANDLOCK=y`. Validate the production path with Docker
   Engine/Moby on Linux; desktop runtimes are not production-equivalent. The
   compose file sets `security_opt: seccomp=unconfined` so Landlock syscalls are
   permitted where supported.
3. **No live hub in e2e containers**: `MAC_HUB_URL`/`MAC_HUB_TOKEN` are empty in
   the compose spec; OCSF events flow to a local observability service only. Real
   fleet deployments inject these via fleet operator secrets.
