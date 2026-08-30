#!/usr/bin/env python3
"""Generate the durable MAC_* environment registry and operator reference.

The scanner only records literal MAC_* names in repository-owned runtime,
deployment, and automation sources.  It never reads environment values.  The
JSON output is the runtime source of truth consumed by ``mac.env_config``; the
Markdown output is an operator view generated from exactly the same records.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# The fleet-scoped credential contract is authored in mac.fleet_env; import
# its data so this doc can never drift from the code.  Add ``src`` to the
# import path so the generator stays runnable from a bare checkout, and keep
# the import side-effect-free (fleet_env never reads the environment on
# import).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from mac.fleet_env import FLEET_SCOPED_VARS, scoped_var  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/mac/data/env_config_registry.json"
REFERENCE = ROOT / "docs/env-config-reference.md"
NAME_RE = re.compile(r"\bMAC_[A-Z][A-Z0-9_]*\b")
SOURCE_ROOTS = (ROOT / "src/mac", ROOT / "deploy", ROOT / "scripts")
SOURCE_SUFFIXES = {".py", ".sh", ".yaml", ".yml", ".toml", ".conf", ".service"}

FAMILIES = (
    ("MAC_SCIENTIFIC_OPTIMIZER_", "scientific-optimizer"),
    ("MAC_REPOSITORY_REF_RECONCILER_", "repository-lifecycle"),
    ("MAC_CODING_ROUTE_", "coding-route-ladder"),
    ("MAC_CODING_AGENT_", "coding-agent-auth"),
    ("MAC_CLIENT_PRINCIPALS_", "client-auth"),
    ("MAC_LOCAL_CONSOLE_", "client-auth"),
    ("MAC_DEPLOY_ROUTER_", "deploy-router"),
    ("MAC_DEPLOY_AGENT_GEN_", "deploy-agent-generation"),
    ("MAC_DEPLOY_", "deployment"),
    ("MAC_REVIEW_", "review"),
    ("MAC_NOTIFIER_", "notifier"),
    ("MAC_EVIDENCE_", "evidence"),
    ("MAC_OPENSHELL_", "openshell-sandbox"),
    ("MAC_SANDBOX_", "openshell-sandbox"),
    ("MAC_WORKER_", "worker"),
    ("MAC_TASK_REPO_", "task-repository"),
    ("MAC_TASK_", "task-execution"),
    ("MAC_ROUTER_", "router"),
    ("MAC_HERMES_", "hermes-runtime"),
    ("MAC_OPENCLAW_", "openclaw-runtime"),
    ("MAC_FIRECRAWL_", "firecrawl-gateway"),
    ("MAC_QDRANT_", "qdrant-memory"),
    ("MAC_TOKENHUB_", "tokenhub-legacy"),
    ("MAC_WEBDAV_", "webdav-publish"),
    ("MAC_MERGE_QUEUE_", "merge-queue"),
    ("MAC_PUBLISH_", "publication"),
    ("MAC_MEMORY_", "memory"),
    ("MAC_RUNNER_", "kubernetes-runner"),
    ("MAC_ACP_", "acp"),
    ("MAC_AGENT_", "agent"),
    ("MAC_HUB_", "hub"),
    ("MAC_API_", "api-auth"),
    ("MAC_GITHUB_", "github-ingest"),
    ("MAC_BACKLOG_", "backlog-grooming"),
    ("MAC_JUDGEMENT_", "judgement"),
)

BOOL_MARKERS = (
    "_ENABLED",
    "_REQUIRED",
    "_ALLOW_",
    "_AUTO_",
    "_DRY_RUN",
    "_KEEP",
    "_REBUILD_",
    "_VALIDATE_",
    "_VERBOSE_",
    "_ROTATE_",
    "_REQUIRE_",
    "_UPLOAD_",
    "_RECONCILE_",
    "_REJECT_",
    "_PREFER_",
)
BOOL_SUFFIXES = ("_OK", "_GC", "_INSTALL", "_MANAGE", "_TRUSTED", "_FATAL")
INT_SUFFIXES = (
    "_SECONDS",
    "_PORT",
    "_LIMIT",
    "_BYTES",
    "_DIM",
    "_ATTEMPTS",
    "_THRESHOLD",
    "_SIZE",
    "_MAX",
    "_TIMEOUT",
    "_INTERVAL",
    "_CONCURRENCY",
    "_TTL",
    "_FLOOR",
    "_AGE",
    "_COUNT",
    "_RETRIES",
)
RETIRED = {"MAC_BEADS_BRIDGE_HUB_AGENT"}
CONSUMER_DEFAULTS = {
    # The contract runner deliberately bounds its default. Operators may still
    # request ``auto`` or another explicit worker count for a qualified host.
    "MAC_TEST_JOBS": "2",
    # deploy/fleet-node-install.sh reads ``${MAC_DEPLOY_GATEWAY_PROBE_FATAL:-0}``,
    # so the non-fatal default is the installer's, not an invented one.
    "MAC_DEPLOY_GATEWAY_PROBE_FATAL": "0",
    "MAC_OPENCLAW_READY_LOG_TIMEOUT": "20",
}
# Descriptions an operator cannot derive from the variable name. The generated
# sentence is fine for a setting whose name says what it does; an escape hatch
# needs its default, its blast radius, and the one case for turning it on.
CURATED_DESCRIPTIONS = {
    "MAC_HUB_VERIFY_PG_URL": (
        "Dedicated test Postgres DSN injected into the hub-verify OpenShell "
        "sandbox as `MAC_TEST_PG_URL`. Never the live hub Postgres (same host "
        "and port, not merely the same database name). Loopback hosts are "
        "rewritten to `host.openshell.internal` (or `MAC_HUB_VERIFY_PG_HOST` / "
        "`MAC_OPENSHELL_HOST_ALIAS`) so the sandbox can reach Postgres on the "
        "hub. If unset, hub-verify runs `scripts/start-test-postgres.sh` on a "
        "dedicated port (default 55432) and rewrites that DSN the same way."
    ),
    "MAC_HUB_VERIFY_PG_HOST": (
        "Hostname substituted for `127.0.0.1`/`localhost`/`::1` in the "
        "hub-verify test DSN. Default `host.openshell.internal` (OpenShell's "
        "host-bridge alias). Does not select the live hub Postgres."
    ),
    "MAC_HUB_VERIFY_PG_PORT": (
        "Port passed to `scripts/start-test-postgres.sh` when hub-verify "
        "provisions a dedicated test DSN. Default 55432 so the helper does not "
        "attach to the live hub listener on 5432."
    ),
    "MAC_HUB_VERIFY_PG_DATADIR": (
        "Data directory for the dedicated hub-verify Postgres started by "
        "`scripts/start-test-postgres.sh`. Defaults to a temp "
        "`mac-hubverify-pgdata` directory, never the live hub cluster."
    ),
    "MAC_DEPLOY_GATEWAY_PROBE_FATAL": (
        "Set `1` to make a failed OpenClaw gateway/channel probe fail the node, "
        "and therefore the whole deploy cohort; unset or `0` records the failure, "
        "retains the failed successor for diagnosis, and continues. Non-fatal by "
        "default because task execution is OpenShell plus the coding CLI plus "
        "mac-agent and none of them consult chat, so a node that cannot post is "
        "degraded for conversation and fully capable of work. Set it for a deploy "
        "whose purpose is to prove the chat surface."
    ),
    "MAC_NETWORK_PROVIDER": (
        "Fleet overlay: `tailscale`, `headscale`, or `none`. When `tailscale` "
        "or `headscale`, the hub process refuses to listen on `0.0.0.0` / LAN / "
        "public addresses and binds loopback plus the Tailscale IPv4 instead. "
        "Not a host firewall by itself; it is the listen-address policy that "
        "makes the overlay the only worker path. Unset means no mesh bind "
        "policy (container/dev)."
    ),
    "MAC_OPENCLAW_READY_LOG_TIMEOUT": (
        "Seconds to wait for `[gateway] ready` in the host log after `verify` "
        "already proved the gateway reachable. Default 20. This is not the "
        "Slack `--probe` budget; reusing `MAC_OPENCLAW_VERIFY_STARTUP_TIMEOUT` "
        "here added 180s of no-op wait on Linux spokes whose journals never "
        "contain that line."
    ),
}


def family_for(name: str) -> str:
    for prefix, family in FAMILIES:
        if name.startswith(prefix):
            return family
    return "core"


def type_for(name: str) -> str:
    if name.endswith(INT_SUFFIXES):
        return "int"
    if any(marker in name for marker in BOOL_MARKERS) or name.endswith(BOOL_SUFFIXES):
        return "bool"
    return "str"


def description_for(name: str, family: str, retired: bool) -> str:
    if retired:
        return "Retired beads bridge selector; ignored by current hub-agent resolution."
    curated = CURATED_DESCRIPTIONS.get(name)
    if curated:
        return curated
    words = name.removeprefix("MAC_").lower().replace("_", " ")
    return f"{family.replace('-', ' ').title()} setting: {words}."


def source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if path == Path(__file__) or "__pycache__" in path.parts:
                continue
            files.append(path)
    return sorted(files)


def build_records() -> list[dict[str, object]]:
    locations: dict[str, set[str]] = {}
    for path in source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=str(path))
                fragments = [
                    node.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                ]
            except SyntaxError:
                fragments = []
        else:
            fragments = [text]
        for name in {match for fragment in fragments for match in NAME_RE.findall(fragment)}:
            # Prefix fragments are template mechanics, not environment keys.
            if name.endswith("_"):
                continue
            locations.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    records = []
    for name in sorted(locations):
        family = family_for(name)
        retired = name in RETIRED
        records.append(
            {
                "name": name,
                "type": type_for(name),
                "default": CONSUMER_DEFAULTS.get(name),
                "family": family,
                "description": description_for(name, family, retired),
                "retired": retired,
                "sources": sorted(locations[name]),
            }
        )
    return records


def fleet_scoped_precedence_lines() -> list[str]:
    """Render the fleet-scoped credential precedence section.

    Every fact here is sourced from :mod:`mac.fleet_env` (the base variable
    set and the scoped-name construction) so the operator doc can never drift
    from the resolver in ``src/mac/fleet_env.py``.
    """
    example_fleet = "example-fleet"
    lines = [
        "## Fleet-scoped credential precedence",
        "",
        "Credential-bearing variables that would otherwise collide when one "
        "workstation joins more than one fleet are resolved by "
        "`mac.fleet_env.resolve`, which understands a *scoped* form in addition "
        "to the legacy flat name.",
        "",
        "### Scoped naming rule",
        "",
        "Each fleet-scoped variable has the form `BASE_NAME__<FLEET>`, where "
        "`<FLEET>` is the active fleet name normalized to an env-var suffix: "
        "uppercased, with every run of non-alphanumeric characters replaced by "
        "a single `_` and leading/trailing `_` stripped. For example, fleet "
        "`%s` yields suffix `%s`, so `MAC_API_TOKEN` scopes to "
        "`%s`."
        % (
            example_fleet,
            scoped_var("X", example_fleet).split("__", 1)[1],
            scoped_var("MAC_API_TOKEN", example_fleet),
        ),
        "",
        "### Resolution order",
        "",
        "For a fleet-scoped base variable, `resolve` looks up values in this "
        "order and returns the first that is set:",
        "",
        "1. **Scoped form wins.** `BASE_NAME__<FLEET>`, where the fleet comes "
        "from the explicit `fleet` argument (e.g. CLI `--fleet`) or, when that "
        "is absent, the `MAC_FLEET` environment variable.",
        "2. **Legacy flat form.** `BASE_NAME`, used only when no scoped value is "
        "present (or no active fleet is known).",
        "",
        "When a fleet-scoped variable is read via its legacy flat name, "
        "`resolve` emits a one-time deprecation warning per `(variable, fleet)` "
        "and points operators at the scoped form. Run "
        "`mac admin config migrate-env-namespace` to append scoped variants of the "
        "flat credentials in your env file and retire the collision.",
        "",
        "### Fleet-scoped base variables",
        "",
        "| Base variable | Example scoped form |",
        "| --- | --- |",
    ]
    for name in sorted(FLEET_SCOPED_VARS):
        lines.append("| `%s` | `%s` |" % (name, scoped_var(name, example_fleet)))
    lines.append("")
    return lines


def render_reference(records: list[dict[str, object]]) -> str:
    lines = [
        "# MAC environment configuration reference",
        "",
        "Generated by `scripts/generate-env-config-registry.py`. Do not edit by hand.",
        "Defaults shown as `consumer-defined` are intentionally owned by the calling subsystem; the registry does not invent a second default.",
        "",
        "| Variable | Type | Default | Family | Description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in records:
        default = item["default"] if item["default"] is not None else "consumer-defined"
        lines.append(
            "| `{name}` | {type} | {default} | {family} | {description} |".format(
                name=item["name"],
                type=item["type"],
                default=default,
                family=item["family"],
                description=item["description"],
            )
        )
    lines.extend(
        [
            "",
            "## Environment variable precedence",
            "",
            "MAC resolves each `MAC_*` variable according to a three-level contract:",
            "",
            "1. **Process environment wins.** A variable already present in the invoking process environment (e.g. set by the supervisor unit, injected by a secret manager, or exported by the operator shell) is used as-is and is never overridden by the env file.",
            "2. **Env file supplies defaults.** A variable absent from the process environment receives its value from the operator env file (typically `~/.mac/.env` or the path given by `MAC_ENV_FILE`).",
            "3. **Env file is the operator default store.** Operators should record stable deployment values — tokens, URLs, feature flags — in the env file. Runtime overrides belong in the process environment and are not written back to the file.",
            "",
        ]
    )
    lines.extend(fleet_scoped_precedence_lines())
    lines.extend(
        [
            "## Precedence and retirement",
            "",
            "Fallback precedence is left-to-right in `resolve_env_chain`. Fleet-scoped credential keys are resolved before legacy flat keys by their owning subsystem. `MAC_BEADS_BRIDGE_HUB_AGENT` is retained only as a documented retired name and is never consulted by `resolve_hub_agent`.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = parser.parse_args()
    records = build_records()
    registry_text = json.dumps(records, indent=2, sort_keys=True) + "\n"
    reference_text = render_reference(records)
    if args.check:
        stale = []
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != registry_text:
            stale.append(str(OUTPUT.relative_to(ROOT)))
        if not REFERENCE.exists() or REFERENCE.read_text(encoding="utf-8") != reference_text:
            stale.append(str(REFERENCE.relative_to(ROOT)))
        if stale:
            raise SystemExit(
                "stale generated environment registry: %s; run %s"
                % (", ".join(stale), Path(__file__).relative_to(ROOT))
            )
        print(f"environment registry is current ({len(records)} variables)")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(registry_text, encoding="utf-8")
    REFERENCE.write_text(reference_text, encoding="utf-8")
    print(
        f"wrote {len(records)} variables to {OUTPUT.relative_to(ROOT)} and {REFERENCE.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
