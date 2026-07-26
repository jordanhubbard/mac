"""Hermes startup orchestration.

Resolves environment flags, expands filesystem paths, and assembles the file and
capability references required to launch the Hermes runtime for a MAC agent
session.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mac.agent_provider import resolve_agent_provider
from mac.env_config import resolve_hub_agent
from mac.hermes_runtime import RUNTIME_CONTEXT_SCHEMA


STATE_REF_CANDIDATES = (
    ("hermes_config", "config.yaml", True),
    ("soul", "SOUL.md", True),
    ("user_profile", "USER.md", True),
    ("long_term_memory", "MEMORY.md", True),
    ("memory_user_profile", "memories/USER.md", True),
    ("memory_long_term", "memories/MEMORY.md", True),
    ("conversation_state", "state.db", True),
    ("memory_store", "memory_store.db", True),
    ("kanban_state", "kanban.db", True),
    ("gateway_auth", "auth.json", True),
    ("gateway_env", ".env", True),
    ("slack_accounts", "slack_accounts.json", True),
    ("slack_channel_teams", "slack_channel_teams.json", True),
    ("slack_home_channels", "slack_home_channels.json", True),
)

ACC_STATE_REF_CANDIDATES = (
    ("acc_coding_sessions", "data/coding-sessions.json", True),
    ("acc_fleet_db", "data/fleet.db", True),
)

TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}

RUNTIME_MARKDOWN_REQUIRED_SNIPPETS = (
    "MAC Task and Project Runtime",
    "First-Class Objects",
    "MAC Vocabulary",
    "`fleets`: authority `mac`",
    "`tasks`: authority `mac`",
    "`projects`: authority `mac`",
    "`agents`: authority `mac`",
    "Identity boundary",
    "mac-hermes tasks",
    "mac-hermes add-child-task",
    "mac-hermes projects",
    "mac-hermes agents",
    "Project Bridge",
    "Agent View",
    "Dashboard Views",
    "/ui?view=work",
    "project={project}",
    "selected={task_id}",
    "Direct Session Parity",
    "`hermes_oneshot_executor`",
    "mac-task-executor",
    "`shell_execution`",
    "`workspace_file_access`",
    "mac task ready",
    "git push",
)


def _env_enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    return default


def _any_env_enabled(names: Iterable[str], default: bool) -> bool:
    for name in names:
        if os.environ.get(name) is not None:
            return _env_enabled(name, default)
    return default


def _expand_path(value: Optional[str], default: str) -> Path:
    return Path(value or default).expanduser()


def _file_ref(path: Path, role: str, sensitive: bool) -> Dict[str, Any]:
    try:
        exists = path.exists()
    except OSError:
        exists = False
    ref: Dict[str, Any] = {
        "role": role,
        "path": str(path),
        "exists": exists,
        "sensitive": sensitive,
    }
    if exists:
        try:
            stat = path.stat()
            ref["kind"] = "dir" if path.is_dir() else "file"
            ref["size_bytes"] = stat.st_size
            ref["mtime_ns"] = stat.st_mtime_ns
        except OSError:
            ref["exists"] = False
    return ref


def _refs(root: Path, candidates: Iterable[tuple[str, str, bool]]) -> List[Dict[str, Any]]:
    return [
        _file_ref(root / relative_path, role, sensitive)
        for role, relative_path, sensitive in candidates
    ]


def _ref_exists(refs: Iterable[Dict[str, Any]], role: str) -> bool:
    return any(ref["role"] == role and ref["exists"] for ref in refs)


def _read_small_text(path: Path, limit: int = 262_144) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ""
        data = path.read_bytes()[:limit]
    except OSError:
        return ""
    return data.decode("utf-8", errors="ignore")


def _qdrant_endpoint_from_env() -> Tuple[Optional[str], Optional[str]]:
    for name in ("QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip().rstrip("/"), name
    return None, None


def _firecrawl_endpoint_from_env() -> Tuple[Optional[str], Optional[str]]:
    for name in ("FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL", "MAC_WEB_SEARCH_URL"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip().rstrip("/"), name
    return None, None


def _redact_url(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return raw
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return "<invalid-url>"
    if not parsed.scheme or not parsed.netloc:
        return "<invalid-url>"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    netloc = host
    if parsed.port is not None:
        netloc = "%s:%s" % (netloc, parsed.port)
    if parsed.username or parsed.password:
        netloc = "redacted@%s" % netloc
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _memory_topology_path(hermes_home: Path) -> Path:
    configured = os.environ.get("MAC_MEMORY_TOPOLOGY_FILE")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return hermes_home / "mac-memory-topology.json"


def _runtime_context_path(hermes_home: Path) -> Path:
    configured = os.environ.get("MAC_HERMES_RUNTIME_CONTEXT_FILE")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return hermes_home / "mac-runtime-context.json"


def _runtime_context_markdown_path(hermes_home: Path) -> Path:
    configured = os.environ.get("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return hermes_home / "mac-runtime-context.md"


def _runtime_markdown_contract(markdown_path: Path) -> Dict[str, Any]:
    file_ref = _file_ref(markdown_path, "task_project_runtime_markdown", False)
    text = _read_small_text(markdown_path, limit=1_000_000)
    missing = [
        snippet
        for snippet in RUNTIME_MARKDOWN_REQUIRED_SNIPPETS
        if snippet not in text
    ]
    return {
        "schema": "mac.hermes.runtime_markdown_contract.v1",
        "file": file_ref,
        "ready": bool(file_ref["exists"]) and not missing,
        "required_snippets": list(RUNTIME_MARKDOWN_REQUIRED_SNIPPETS),
        "missing_snippets": missing,
    }


def _runtime_prompt_bridge_report(
    agent_dir: Optional[Path],
    *,
    required: bool,
) -> Dict[str, Any]:
    path = agent_dir / "agent" / "prompt_builder.py" if agent_dir is not None else Path("")
    report: Dict[str, Any] = {
        "required": required,
        "file": _file_ref(path, "hermes_prompt_builder", False) if agent_dir is not None else None,
        "present": False,
        "warning": "",
    }
    if agent_dir is None:
        if required:
            report["warning"] = "Hermes MAC runtime prompt bridge cannot be verified without MAC_HERMES_AGENT_DIR"
        return report
    text = _read_small_text(path, limit=1_000_000)
    report["present"] = (
        "_load_mac_runtime_context" in text
        and "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN" in text
        and "mac-runtime-context.md" in text
    )
    if required and not report["present"]:
        report["warning"] = "Hermes MAC task/project runtime prompt bridge is missing from %s" % path
    return report


def _command_name(command: Optional[str]) -> str:
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


def _command_resolution(
    command: Optional[str],
    *,
    expected_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    executable = _command_name(command)
    expected = Path(expected_path).expanduser() if expected_path else None
    cwd_path = Path(cwd).expanduser() if cwd else None
    expected_ref = (
        {
            **_file_ref(expected, "session_capability_executable", False),
            "executable": expected.exists() and os.access(expected, os.X_OK),
        }
        if expected is not None
        else None
    )
    resolved = None
    local_candidate = None
    if executable:
        executable_path = Path(executable)
        if executable_path.is_absolute():
            if executable_path.exists() and os.access(executable_path, os.X_OK):
                resolved = str(executable_path)
        elif "/" in executable or "\\" in executable:
            if cwd_path is not None:
                candidate = cwd_path / executable
                if candidate.exists() and os.access(candidate, os.X_OK):
                    local_candidate = str(candidate)
                    resolved = local_candidate
        else:
            resolved = shutil.which(executable)
    if expected_ref and expected_ref["executable"] and not resolved:
        resolved = expected_ref["path"]
    return {
        "command": command,
        "executable": executable,
        "resolved": resolved,
        "expected_path": expected_ref,
        "cwd": str(cwd_path) if cwd_path is not None else None,
        "cwd_exists": cwd_path.exists() if cwd_path is not None else None,
        "local_candidate": local_candidate,
        "available": bool(resolved),
    }


def _session_capability_availability(
    capabilities: List[Dict[str, Any]],
    *,
    workspace: Dict[str, Any],
) -> Dict[str, Any]:
    workspace_path = str(workspace.get("path") or "").strip()
    workspace_ref = _file_ref(Path(workspace_path).expanduser(), "mac_hermes_workspace", False) if workspace_path else None
    project_contract = workspace.get("project_contract") if isinstance(workspace.get("project_contract"), dict) else {}
    contract_path = str(project_contract.get("path") or "").strip()
    contract_ref = _file_ref(Path(contract_path).expanduser(), "mac_project_contract", False) if contract_path else None
    required_commands = [
        str(command).strip()
        for command in (project_contract.get("required_commands") or [])
        if str(command).strip()
    ]
    toolchain = [
        {
            "command": command,
            "resolved": shutil.which(command),
            "available": bool(shutil.which(command)),
        }
        for command in required_commands
    ]
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    for item in capabilities:
        name = str(item.get("name") or "").strip()
        checks: Dict[str, bool] = {}
        command_info = _command_resolution(
            item.get("command") if isinstance(item.get("command"), str) else None,
            expected_path=item.get("expected_path") if isinstance(item.get("expected_path"), str) else None,
            cwd=item.get("cwd") if isinstance(item.get("cwd"), str) else workspace_path or None,
        )
        if item.get("command"):
            checks["command_available"] = bool(command_info["available"])
        if command_info["expected_path"] is not None:
            checks["expected_path_executable"] = bool(command_info["expected_path"]["executable"])
        if item.get("cwd"):
            checks["cwd_exists"] = bool(command_info["cwd_exists"])
        if name == "shell_execution":
            shell_probe_ready = False
            try:
                result = subprocess.run(
                    ["sh", "-c", "true"],
                    cwd=workspace_path or None,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
                shell_probe_ready = result.returncode == 0
            except (OSError, subprocess.SubprocessError, ValueError):
                shell_probe_ready = False
            checks["shell_probe_succeeded"] = shell_probe_ready
        if name == "workspace_file_access":
            workspace_path_obj = Path(workspace_path).expanduser() if workspace_path else None
            checks["workspace_readable"] = bool(
                workspace_path_obj is not None and os.access(workspace_path_obj, os.R_OK)
            )
            checks["workspace_writable"] = bool(
                workspace_path_obj is not None and os.access(workspace_path_obj, os.W_OK)
            )
            checks["workspace_searchable"] = bool(
                workspace_path_obj is not None and os.access(workspace_path_obj, os.X_OK)
            )
            write_probe_ready = False
            probe_path: Optional[Path] = None
            if workspace_path_obj is not None:
                try:
                    with tempfile.NamedTemporaryFile(
                        "w",
                        encoding="utf-8",
                        dir=workspace_path_obj,
                        prefix=".mac-runtime-probe-",
                        delete=False,
                    ) as handle:
                        probe_path = Path(handle.name)
                        handle.write("ok")
                    write_probe_ready = probe_path.read_text(encoding="utf-8") == "ok"
                except OSError:
                    write_probe_ready = False
                finally:
                    if probe_path is not None:
                        try:
                            probe_path.unlink()
                        except OSError:
                            pass
            checks["workspace_write_probe"] = write_probe_ready
        if name == "mac_api":
            endpoint = item.get("endpoint")
            checks["endpoint_configured"] = bool(endpoint and endpoint != "<invalid-url>")
        if name == "web_search":
            env_names = [
                str(env_name)
                for env_name in (item.get("environment") or [])
                if str(env_name).strip()
            ]
            checks["web_search_environment_configured"] = any(
                bool(os.environ.get(env_name)) for env_name in env_names
            )
        ready = all(checks.values()) if checks else True
        row = {
            "name": name,
            "kind": item.get("kind"),
            "required": bool(item.get("required")),
            "ready": ready,
            "checks": checks,
            "command": command_info,
            "environment": [
                str(env_name)
                for env_name in (item.get("environment") or [])
                if str(env_name).strip()
            ],
        }
        rows.append(row)
        if item.get("required") and not ready:
            missing.append(name or "unnamed_capability")
    workspace_ready = bool(workspace_ref and workspace_ref["exists"] and workspace_ref.get("kind") == "dir")
    contract_ready = bool(
        contract_ref
        and contract_ref["exists"]
        and project_contract.get("schema") == "mac.repository_contract.v1"
    )
    missing.extend(
        "project_toolchain:%s" % item["command"]
        for item in toolchain
        if not item["available"]
    )
    if not workspace_ready:
        missing.append("workspace")
    if not contract_ready:
        missing.append("project_contract")
    return {
        "ready": not missing,
        "missing": sorted(set(missing)),
        "workspace": workspace_ref,
        "project_contract": contract_ref,
        "toolchain": toolchain,
        "capabilities": rows,
    }


def _topology_summary(path: Path) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "file": _file_ref(path, "memory_topology", False),
        "schema": None,
        "agent": None,
        "hub_agent": None,
        "hub_url": None,
        "qdrant_url": None,
        "error": "",
    }
    if not path.exists() or not path.is_file():
        return summary
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summary["error"] = str(exc)
        return summary
    if not isinstance(data, dict):
        summary["error"] = "memory topology root is not an object"
        return summary
    hub = data.get("hub") if isinstance(data.get("hub"), dict) else {}
    services = data.get("shared_services") if isinstance(data.get("shared_services"), dict) else {}
    qdrant = services.get("qdrant") if isinstance(services.get("qdrant"), dict) else {}
    summary.update(
        {
            "schema": data.get("schema"),
            "agent": data.get("agent"),
            "hub_agent": hub.get("agent"),
            "hub_url": _redact_url(hub.get("url")) if isinstance(hub.get("url"), str) else None,
            "qdrant_url": _redact_url(qdrant.get("url")) if isinstance(qdrant.get("url"), str) else None,
        }
    )
    return summary


def _runtime_context_summary(hermes_home: Path) -> Dict[str, Any]:
    context_path = _runtime_context_path(hermes_home)
    markdown_path = _runtime_context_markdown_path(hermes_home)
    required = _env_enabled("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", False)
    summary: Dict[str, Any] = {
        "status": "disabled",
        "ready": True,
        "required": required,
        "context_file": _file_ref(context_path, "task_project_runtime_context", False),
        "markdown_file": _file_ref(markdown_path, "task_project_runtime_markdown", False),
        "schema": None,
        "fleet": None,
        "agent_id": None,
        "agent_name": None,
        "hermes_instance_id": os.environ.get("MAC_HERMES_INSTANCE_ID")
        or os.environ.get("MAC_WORKER_HERMES_INSTANCE_ID")
        or None,
        "mac_url": _redact_url(os.environ.get("MAC_URL") or os.environ.get("MAC_HUB_URL")),
        "authority": {},
        "first_class_object_names": [],
        "first_class_objects": {},
        "operation_groups": [],
        "workspace": {},
        "session_capability_names": [],
        "session_capabilities": [],
        "session_capability_availability": {"ready": True, "missing": []},
        "markdown_contract": _runtime_markdown_contract(markdown_path),
        "warning": "",
        "error": "",
    }
    if not context_path.exists() or not context_path.is_file():
        if required:
            summary["ready"] = False
            summary["status"] = "missing_context"
            summary["warning"] = "Hermes MAC task/project runtime context file is missing: %s" % context_path
        return summary

    try:
        data = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summary["ready"] = not required
        summary["status"] = "invalid_context"
        summary["error"] = str(exc)
        if required:
            summary["warning"] = "Hermes MAC task/project runtime context file is invalid: %s" % exc
        return summary
    if not isinstance(data, dict):
        summary["ready"] = not required
        summary["status"] = "invalid_context"
        summary["error"] = "runtime context root is not an object"
        if required:
            summary["warning"] = "Hermes MAC task/project runtime context file is invalid"
        return summary

    agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
    identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
    authority = data.get("authority") if isinstance(data.get("authority"), dict) else {}
    endpoints = data.get("endpoints") if isinstance(data.get("endpoints"), dict) else {}
    operations = data.get("operations") if isinstance(data.get("operations"), dict) else {}
    first_class = data.get("first_class_objects") if isinstance(data.get("first_class_objects"), dict) else {}
    first_class_objects = (
        first_class.get("objects")
        if isinstance(first_class.get("objects"), dict)
        else {}
    )
    session = data.get("session_capabilities") if isinstance(data.get("session_capabilities"), dict) else {}
    workspace = session.get("workspace") if isinstance(session.get("workspace"), dict) else {}
    if not workspace and isinstance(data.get("workspace"), dict):
        workspace = data.get("workspace")
    raw_capabilities = session.get("capabilities") if isinstance(session.get("capabilities"), list) else []
    session_capabilities = [
        {
            "name": item.get("name"),
            "kind": item.get("kind"),
            "required": bool(item.get("required")),
            "command": item.get("command"),
            "endpoint": _redact_url(item.get("endpoint")) if isinstance(item.get("endpoint"), str) else None,
            "expected_path": item.get("expected_path"),
            "cwd": item.get("cwd"),
            "environment": item.get("environment") if isinstance(item.get("environment"), list) else [],
        }
        for item in raw_capabilities
        if isinstance(item, dict) and item.get("name")
    ]
    session_capability_names = sorted(
        str(item["name"])
        for item in session_capabilities
        if str(item.get("name") or "").strip()
    )
    summary.update(
        {
            "schema": data.get("schema"),
            "fleet": data.get("fleet"),
            "agent_id": agent.get("agent_id"),
            "agent_name": agent.get("name"),
            "hermes_instance_id": identity.get("hermes_instance_id")
            or agent.get("hermes_instance_id")
            or summary["hermes_instance_id"],
            "mac_url": _redact_url(endpoints.get("mac_api"))
            if isinstance(endpoints.get("mac_api"), str)
            else summary["mac_url"],
            "authority": {
                "fleets": authority.get("fleets"),
                "tasks": authority.get("tasks"),
                "projects": authority.get("projects"),
                "agents": authority.get("agents"),
                "personality": authority.get("personality"),
                "user_memory": authority.get("user_memory"),
            },
            "first_class_object_names": sorted(
                str(name)
                for name, value in first_class_objects.items()
                if isinstance(value, dict) and str(name).strip()
            ),
            "first_class_objects": {
                str(name): {
                    "authority": value.get("authority"),
                    "source_of_truth": value.get("source_of_truth"),
                    "identity_fields": value.get("identity_fields")
                    if isinstance(value.get("identity_fields"), list)
                    else [],
                    "api_paths": value.get("api_paths")
                    if isinstance(value.get("api_paths"), list)
                    else [],
                    "mac_cli": value.get("mac_cli") if isinstance(value.get("mac_cli"), list) else [],
                    "mac_hermes_cli": value.get("mac_hermes_cli")
                    if isinstance(value.get("mac_hermes_cli"), list)
                    else [],
                    "dashboard_state_keys": value.get("dashboard_state_keys")
                    if isinstance(value.get("dashboard_state_keys"), list)
                    else [],
                    "dashboard_urls": value.get("dashboard_urls")
                    if isinstance(value.get("dashboard_urls"), list)
                    else [],
                    "runtime_rule": value.get("runtime_rule"),
                }
                for name, value in first_class_objects.items()
                if isinstance(value, dict)
            },
            "operation_groups": sorted(str(key) for key in operations.keys()),
            "workspace": {
                "path": workspace.get("path"),
                "project_contract": (
                    {
                        "path": workspace.get("project_contract", {}).get("path"),
                        "exists": workspace.get("project_contract", {}).get("exists"),
                        "schema": workspace.get("project_contract", {}).get("schema"),
                        "project": workspace.get("project_contract", {}).get("project"),
                        "required_commands": workspace.get("project_contract", {}).get("required_commands") or [],
                        "bootstrap_command": workspace.get("project_contract", {}).get("bootstrap_command"),
                        "test_command": workspace.get("project_contract", {}).get("test_command"),
                    }
                    if isinstance(workspace.get("project_contract"), dict)
                    else {}
                ),
            },
            "session_capability_names": session_capability_names,
            "session_capabilities": session_capabilities,
        }
    )
    if data.get("schema") != RUNTIME_CONTEXT_SCHEMA:
        summary["ready"] = not required
        summary["status"] = "invalid_schema"
        summary["error"] = "unexpected runtime context schema: %s" % data.get("schema")
        if required:
            summary["warning"] = "Hermes MAC task/project runtime context schema is invalid"
        return summary
    if required and not markdown_path.exists():
        summary["ready"] = False
        summary["status"] = "missing_markdown"
        summary["warning"] = "Hermes MAC task/project runtime markdown is missing: %s" % markdown_path
        return summary
    for key in ("fleets", "tasks", "projects", "agents"):
        if authority.get(key) != "mac":
            summary["ready"] = not required
            summary["status"] = "authority_mismatch"
            summary["error"] = "runtime context does not declare MAC authority for %s" % key
            if required:
                summary["warning"] = summary["error"]
            return summary
    object_model = summary["first_class_objects"]
    object_model_errors = []
    for key in ("fleets", "tasks", "projects", "agents"):
        item = object_model.get(key) if isinstance(object_model.get(key), dict) else {}
        if item.get("authority") != "mac":
            object_model_errors.append("%s.authority" % key)
        for field in (
            "source_of_truth",
            "identity_fields",
            "api_paths",
            "dashboard_state_keys",
            "dashboard_urls",
        ):
            value = item.get(field)
            if not value:
                object_model_errors.append("%s.%s" % (key, field))
        if key != "fleets" and not item.get("mac_hermes_cli"):
            object_model_errors.append("%s.mac_hermes_cli" % key)
    if object_model_errors:
        summary["ready"] = not required
        summary["status"] = "first_class_object_contract_missing"
        summary["error"] = "runtime context is missing first-class object contract fields: %s" % ", ".join(
            sorted(object_model_errors)
        )
        if required:
            summary["warning"] = summary["error"]
        return summary
    expected_capabilities = {
        "mac_api",
        "mac_cli",
        "mac_hermes_cli",
        "shell_execution",
        "workspace_file_access",
        "ticket_mirror",
        "mac_task_cli",
        "git_source_control",
        "quality_gate",
        "hermes_oneshot_executor",
        "command_audit",
        "web_search",
    }
    if not expected_capabilities <= set(session_capability_names):
        missing = sorted(expected_capabilities - set(session_capability_names))
        summary["ready"] = not required
        summary["status"] = "session_capability_contract_missing"
        summary["error"] = "runtime context is missing session capabilities: %s" % ", ".join(missing)
        if required:
            summary["warning"] = summary["error"]
        return summary
    markdown_contract = summary["markdown_contract"]
    if not markdown_contract.get("ready"):
        summary["ready"] = not required
        summary["status"] = "markdown_contract_missing"
        summary["error"] = (
            "runtime markdown is missing first-class prompt contract snippets: %s"
            % ", ".join(markdown_contract.get("missing_snippets") or [])
        )
        if required:
            summary["warning"] = summary["error"]
        return summary
    availability = _session_capability_availability(
        session_capabilities,
        workspace=summary["workspace"],
    )
    summary["session_capability_availability"] = availability
    if not availability["ready"]:
        summary["ready"] = not required
        summary["status"] = "session_capability_unavailable"
        summary["error"] = "runtime session capabilities are unavailable: %s" % ", ".join(
            availability["missing"]
        )
        if required:
            summary["warning"] = summary["error"]
        return summary
    summary["status"] = "ready"
    return summary


def _fetch_qdrant_collections(endpoint: str, api_key: Optional[str], timeout_seconds: float) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["api-key"] = api_key
    request = urllib.request.Request(endpoint.rstrip("/") + "/collections", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read(262_144)
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def _fetch_firecrawl_health(endpoint: str, api_key: Optional[str], timeout_seconds: float) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key and api_key.lower() != "none":
        headers["Authorization"] = "Bearer %s" % api_key
    request = urllib.request.Request(endpoint.rstrip("/") + "/health", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read(1_048_576)
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def _qdrant_memory_report(hermes_home: Path) -> Dict[str, Any]:
    endpoint, endpoint_source = _qdrant_endpoint_from_env()
    memory_disabled = not _any_env_enabled(("MAC_QDRANT_MEMORY", "ACC_QDRANT_MEMORY"), True)
    required = True
    degraded_allowed = False
    topology = _topology_summary(_memory_topology_path(hermes_home))
    api_key = os.environ.get("QDRANT_API_KEY") or os.environ.get("QDRANT_FLEET_KEY")
    report: Dict[str, Any] = {
        "status": "disabled",
        "ready": False,
        "required": required,
        "degraded_allowed": degraded_allowed,
        "mandatory": True,
        "endpoint": _redact_url(endpoint),
        "endpoint_source": endpoint_source,
        "api_key_present": bool(api_key),
        "role": os.environ.get("MAC_QDRANT_MEMORY_ROLE", "shared_level2"),
        "manager_agent": resolve_hub_agent("MAC_SHARED_SERVICES_MANAGER_AGENT"),
        "topology": topology,
        "collection_count": None,
        "warning": "",
        "degradation_reason": "",
    }
    if memory_disabled:
        report["status"] = "disabled_by_env"
        report["degradation_reason"] = "Qdrant shared memory is mandatory and cannot be disabled"
        report["warning"] = report["degradation_reason"]
        return report
    if not endpoint:
        report["ready"] = False
        report["status"] = "missing_endpoint"
        report["degradation_reason"] = "required Qdrant shared memory endpoint is not configured"
        report["warning"] = report["degradation_reason"]
        return report

    parsed = urllib.parse.urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        report["ready"] = False
        report["status"] = "invalid_endpoint"
        report["degradation_reason"] = "Qdrant shared memory endpoint is invalid"
        report["warning"] = report["degradation_reason"]
        return report

    if required and not topology["file"]["exists"]:
        report["ready"] = False
        report["status"] = "missing_topology"
        report["warning"] = "Hermes memory topology file is missing: %s" % topology["file"]["path"]
        return report
    if required and topology["error"]:
        report["ready"] = False
        report["status"] = "invalid_topology"
        report["warning"] = "Hermes memory topology file is invalid: %s" % topology["error"]
        return report

    timeout_seconds = float(os.environ.get("MAC_QDRANT_CHECK_TIMEOUT_SECONDS", "2"))
    try:
        collections = _fetch_qdrant_collections(endpoint, api_key, timeout_seconds)
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        report["ready"] = False
        report["status"] = "unreachable"
        report["degradation_reason"] = "Qdrant collections endpoint is unreachable: %s" % exc
        report["warning"] = report["degradation_reason"]
        return report
    result = collections.get("result") if isinstance(collections, dict) else {}
    collection_rows = result.get("collections") if isinstance(result, dict) else None
    report["collection_count"] = len(collection_rows) if isinstance(collection_rows, list) else None
    report["status"] = "ready"
    report["ready"] = True
    return report


def _firecrawl_web_search_report() -> Dict[str, Any]:
    endpoint, endpoint_source = _firecrawl_endpoint_from_env()
    required = True
    degraded_allowed = False
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    report: Dict[str, Any] = {
        "status": "disabled",
        "ready": False,
        "required": required,
        "degraded_allowed": degraded_allowed,
        "mandatory": True,
        "endpoint": _redact_url(endpoint),
        "endpoint_source": endpoint_source,
        "api_key_present": bool(api_key),
        "warning": "",
        "degradation_reason": "",
    }
    if not endpoint:
        report["ready"] = False
        report["status"] = "missing_endpoint"
        report["degradation_reason"] = "required Firecrawl web search endpoint is not configured"
        report["warning"] = report["degradation_reason"]
        return report

    parsed = urllib.parse.urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        report["ready"] = False
        report["status"] = "invalid_endpoint"
        report["degradation_reason"] = "Firecrawl web search endpoint is invalid"
        report["warning"] = report["degradation_reason"]
        return report

    timeout_seconds = float(os.environ.get("MAC_FIRECRAWL_CHECK_TIMEOUT_SECONDS", "2"))
    try:
        _fetch_firecrawl_health(endpoint, api_key, timeout_seconds)
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        report["ready"] = False
        report["status"] = "unreachable"
        report["degradation_reason"] = "Firecrawl web search health endpoint is unreachable: %s" % exc
        report["warning"] = report["degradation_reason"]
        return report
    report["status"] = "ready"
    report["ready"] = True
    return report


def _tokenhub_endpoint_from_env() -> Tuple[Optional[str], Optional[str]]:
    # th-merge-07: TokenHub is retired. Only an EXPLICIT TokenHub URL counts — do
    # NOT fall back to CUSTOM_BASE_URL/OPENAI_BASE_URL: on a spoke that is the hub's
    # in-mac router /v1, and health-checking it as a "TokenHub" yields a spurious
    # 403 + degraded status. With no explicit endpoint the report is "retired".
    for name in ("TOKENHUB_URL", "MAC_TOKENHUB_URL"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip().rstrip("/"), name
    return None, None


def _fetch_tokenhub_json(endpoint: str, path: str, api_key: Optional[str], timeout_seconds: float) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    request = urllib.request.Request(endpoint.rstrip("/") + path, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read(1_048_576)
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def _tokenhub_report() -> Dict[str, Any]:
    endpoint, endpoint_source = _tokenhub_endpoint_from_env()
    explicitly_required = _env_enabled("MAC_REQUIRE_TOKENHUB", False)
    required = explicitly_required or bool(endpoint)
    degraded_allowed = _env_enabled("MAC_TOKENHUB_ALLOW_DEGRADED", False)
    api_key = (
        os.environ.get("TOKENHUB_API_KEY")
        or os.environ.get("TOKENHUB_AGENT_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    report: Dict[str, Any] = {
        "status": "disabled",
        "ready": True,
        "required": required,
        "degraded_allowed": degraded_allowed,
        "endpoint": _redact_url(endpoint),
        "endpoint_source": endpoint_source,
        "api_key_present": bool(api_key),
        "model_count": None,
        "adapter_count": None,
        "warning": "",
        "degradation_reason": "",
    }
    if (os.environ.get("MAC_ROUTER_BACKEND") or "").strip().lower() in {"inproc", "standalone"}:
        # th-merge-07: in router mode TokenHub is retired — its health endpoint
        # being unreachable is expected, not a degradation. Report cleanly.
        # "standalone" is the same router in its own process (mac-router).
        report["status"] = "retired"
        report["ready"] = True
        return report
    if not endpoint:
        if not required:
            # No explicit TokenHub endpoint + not required = the retired state
            # (e.g. a spoke routing chat through the hub). Report it as such so the
            # operator sees "retired", not a vague "disabled".
            report["status"] = "retired"
            return report
        report["ready"] = bool(degraded_allowed)
        report["status"] = "degraded_allowed" if degraded_allowed else "missing_endpoint"
        report["degradation_reason"] = "required TokenHub endpoint is not configured"
        if not degraded_allowed:
            report["warning"] = report["degradation_reason"]
        return report

    parsed = urllib.parse.urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        report["ready"] = bool(degraded_allowed)
        report["status"] = "degraded_allowed" if degraded_allowed else "invalid_endpoint"
        report["degradation_reason"] = "TokenHub endpoint is invalid"
        if not degraded_allowed:
            report["warning"] = report["degradation_reason"]
        return report

    timeout_seconds = float(os.environ.get("MAC_TOKENHUB_CHECK_TIMEOUT_SECONDS", "2"))
    try:
        health = _fetch_tokenhub_json(endpoint, "/healthz", None, timeout_seconds)
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        report["ready"] = bool(degraded_allowed)
        report["status"] = "degraded_allowed" if degraded_allowed else "unreachable"
        report["degradation_reason"] = "TokenHub health endpoint is unreachable: %s" % exc
        if not degraded_allowed:
            report["warning"] = report["degradation_reason"]
        return report
    if isinstance(health, dict):
        adapters = health.get("adapters")
        if isinstance(adapters, int):
            report["adapter_count"] = adapters

    if required and not api_key:
        report["ready"] = bool(degraded_allowed)
        report["status"] = "degraded_allowed" if degraded_allowed else "missing_client_key"
        report["degradation_reason"] = "TokenHub client key is missing"
        if not degraded_allowed:
            report["warning"] = report["degradation_reason"]
        return report

    try:
        models = _fetch_tokenhub_json(endpoint, "/v1/models", api_key, timeout_seconds)
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        report["ready"] = bool(degraded_allowed)
        report["status"] = "degraded_allowed" if degraded_allowed else "models_unreachable"
        report["degradation_reason"] = "TokenHub model endpoint is unreachable: %s" % exc
        if not degraded_allowed:
            report["warning"] = report["degradation_reason"]
        return report
    rows = models.get("data") if isinstance(models, dict) else None
    report["model_count"] = len(rows) if isinstance(rows, list) else None
    report["status"] = "ready"
    report["ready"] = True
    return report


def _config_explicitly_enables_slack(config_path: Path) -> bool:
    text = _read_small_text(config_path)
    if not text:
        return False
    if re.search(r"(?mi)^\s*SLACK_BOT_TOKEN\s*[:=]", text):
        return True
    slack_block = re.search(r"(?ms)^\s*(slack|platforms)\s*:.*?^\S", text + "\nend:", re.M)
    if slack_block and re.search(r"(?mi)^\s*enabled\s*:\s*true\s*$", slack_block.group(0)):
        return True
    return bool(re.search(r"(?mi)^\s*slack\s*:\s*\{\s*enabled\s*:\s*true", text))


def _bool_value(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    value = raw.strip().strip("\"'").lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    return None


def _env_file_redaction_ref(path: Path, role: str) -> Dict[str, Any]:
    ref = _file_ref(path, role, True)
    ref["redact_secrets"] = "unset"
    ref["redact_secrets_disabled"] = False
    text = _read_small_text(path)
    if not text:
        return ref
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "HERMES_REDACT_SECRETS":
            continue
        parsed = _bool_value(value)
        if parsed is None:
            ref["redact_secrets"] = "invalid"
        else:
            ref["redact_secrets"] = "true" if parsed else "false"
            ref["redact_secrets_disabled"] = not parsed
        break
    return ref


def _config_redaction_value(config_path: Path) -> str:
    text = _read_small_text(config_path)
    if not text:
        return "unset"
    match = re.search(r"(?mi)^\s*redact_secrets\s*:\s*(true|false|yes|no|on|off|1|0)\s*$", text)
    if not match:
        return "unset"
    parsed = _bool_value(match.group(1))
    if parsed is None:
        return "invalid"
    return "true" if parsed else "false"


def _secret_redaction_report(hermes_home: Path, acc_dir: Path) -> Dict[str, Any]:
    env_value = _bool_value(os.environ.get("HERMES_REDACT_SECRETS"))
    config_value = _config_redaction_value(hermes_home / "config.yaml")
    env_files = [
        _env_file_redaction_ref(hermes_home / ".env", "hermes_env"),
        _env_file_redaction_ref(acc_dir / ".env", "acc_env"),
    ]
    disabled_files = [
        ref["role"] for ref in env_files if ref.get("redact_secrets_disabled")
    ]

    if env_value is not None:
        effective = env_value
        source = "environment"
    elif config_value in {"true", "false"}:
        effective = config_value == "true"
        source = "config"
    else:
        effective = True
        source = "default"

    warnings = []
    if env_value is False:
        warnings.append("Hermes secret redaction is disabled by HERMES_REDACT_SECRETS")
    if config_value == "false":
        warnings.append("Hermes config disables secret redaction")
    if disabled_files:
        warnings.append(
            "Inherited Hermes environment files disable secret redaction: %s"
            % ", ".join(disabled_files)
        )

    return {
        "effective": effective,
        "source": source,
        "environment": (
            "unset"
            if os.environ.get("HERMES_REDACT_SECRETS") is None
            else ("true" if env_value is True else "false" if env_value is False else "invalid")
        ),
        "config": config_value,
        "env_files": env_files,
        "drift_detected": bool(warnings),
        "warnings": warnings,
    }


def _log_classification_report() -> Dict[str, Any]:
    default_path = Path("~/.mac/logs/hermes-log-summary.json").expanduser()
    path = _expand_path(os.environ.get("MAC_HERMES_LOG_SUMMARY"), str(default_path))
    report: Dict[str, Any] = {
        "summary": _file_ref(path, "hermes_log_summary", False),
        "known_benign_classes": [
            "controlled_restart",
            "slack_file_public_unhandled",
            "discord_missing_token_unconfigured",
        ],
        "actionable_count": 0,
        "benign_count": 0,
        "classes": [],
        "warnings": [],
    }
    try:
        import json

        if path.exists() and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            classes = data.get("classes") if isinstance(data, dict) else []
            if isinstance(classes, list):
                report["classes"] = classes
            report["actionable_count"] = int(data.get("actionable_count", 0))
            report["benign_count"] = int(data.get("benign_count", 0))
    except Exception as exc:
        report["warnings"].append("could not read Hermes log summary: %s" % exc)
    if report["actionable_count"]:
        report["warnings"].append(
            "Hermes gateway logs contain actionable classified warnings"
        )
    return report


def _slack_home_channel_name() -> str:
    return (
        os.environ.get("MAC_HERMES_SLACK_HOME_CHANNEL_NAME")
        or os.environ.get("ACC_SLACK_HOME_CHANNEL_NAME")
        or os.environ.get("SLACK_HOME_CHANNEL_NAME")
        or ""
    ).strip().lstrip("#")


def _configured_gateway_model() -> str:
    return (
        os.environ.get("MAC_HERMES_GATEWAY_MODEL")
        or os.environ.get("ACC_HERMES_GATEWAY_MODEL")
        or os.environ.get("HERMES_INFERENCE_MODEL")
        or os.environ.get("ACC_LLM_MODEL")
        or ""
    ).strip()


def _configured_gateway_provider() -> str:
    return (
        os.environ.get("MAC_HERMES_GATEWAY_PROVIDER")
        or os.environ.get("ACC_HERMES_GATEWAY_PROVIDER")
        or os.environ.get("HERMES_INFERENCE_PROVIDER")
        or ""
    ).strip()


def _configured_gateway_base_url_present() -> bool:
    return bool(
        (
            os.environ.get("MAC_HERMES_GATEWAY_BASE_URL")
            or os.environ.get("ACC_HERMES_GATEWAY_BASE_URL")
            or os.environ.get("TOKENHUB_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or ""
        ).strip()
    )


def apply_hermes_gateway_runtime_shim_report(
    agent_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Report the per-agent gateway provider/model override.

    ADR 0001 hu-03/hu-04: the override is now **owned, in-process code**
    (``mac.agent_provider``, called by the vendored gateway in ``src/mac/_hermes``).
    The old runtime string-surgery of a cloned upstream tree is gone, so there is
    no shim to "miss" — when an override is configured the in-process gateway
    always honors it. The ``gateway_runtime_shim_*`` field names are retained for
    dashboard/report-schema compatibility but now reflect the owned decision.
    ``agent_dir`` is accepted and ignored (no upstream checkout is consulted).
    """
    configured_model = _configured_gateway_model()
    configured_provider = _configured_gateway_provider()
    base_url_configured = _configured_gateway_base_url_present()
    provider_decision = resolve_agent_provider().observable()
    override_active = bool(provider_decision["override_active"])

    return {
        "hermes_agent_dir": None,
        "hermes_agent_dir_explicit": False,
        "configured_model": configured_model or None,
        "provider_override_configured": bool(configured_provider),
        "base_url_override_configured": base_url_configured,
        # "active" now means: an override is configured and the in-process
        # gateway honors it via mac.agent_provider (no string surgery to fail).
        "gateway_runtime_shim_present": override_active,
        "gateway_runtime_shim_patch": {
            "obsolete": True,
            "applied": False,
            "note": "in-process via mac.agent_provider (vendored gateway); no string surgery",
            "error": "",
        },
        "provider_decision": provider_decision,
        "warnings": [],
    }


def _slack_activation_report(
    hermes_home: Path,
    hermes_refs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    account_ref = next(
        ref for ref in hermes_refs if ref["role"] == "slack_accounts"
    )
    home_channel_ref = next(
        ref for ref in hermes_refs if ref["role"] == "slack_home_channels"
    )
    account_file_present = bool(account_ref["exists"])
    home_channel_file_present = bool(home_channel_ref["exists"])
    env_token_present = bool(os.environ.get("SLACK_BOT_TOKEN"))
    explicit_config = _config_explicitly_enables_slack(hermes_home / "config.yaml")
    # ADR 0001 hu-04: multi-workspace Slack support (activation from
    # slack_accounts.json) is now baked into the vendored gateway — the former
    # out-of-tree multi-slack-mvp patch. So an account file is sufficient; there
    # is no upstream-checkout "shim" to apply. Shim field names are retained for
    # report-schema compatibility and report as effectively present.
    activation_source = "not_configured"
    can_activate = False
    if env_token_present:
        activation_source = "slack_bot_token"
        can_activate = True
    elif explicit_config:
        activation_source = "explicit_config"
        can_activate = True
    elif account_file_present:
        activation_source = "slack_accounts_file"
        can_activate = True

    _obsolete_patch = {
        "obsolete": True,
        "applied": False,
        "note": "vendored gateway (no upstream-checkout shim)",
        "error": "",
    }
    return {
        "account_file": account_ref,
        "account_file_present": account_file_present,
        "home_channel_file": home_channel_ref,
        "home_channel_file_present": home_channel_file_present,
        "configured_home_channel_name": _slack_home_channel_name(),
        "slack_bot_token_present": env_token_present,
        "explicit_config_present": explicit_config,
        "hermes_agent_dir": None,
        "hermes_agent_dir_explicit": False,
        "account_file_activation_shim_present": True,
        "account_file_activation_shim_patch": dict(_obsolete_patch),
        "home_channel_shim_present": True,
        "home_channel_shim_patch": dict(_obsolete_patch),
        "needs_account_file_activation_shim": False,
        "activation_source": activation_source,
        "can_activate": can_activate,
        "warning": "",
    }


def build_hermes_startup_report() -> Dict[str, Any]:
    """Return a redacted startup report for Hermes-owned durable state.

    The report deliberately contains only file metadata and boolean activation
    facts. It never includes file contents from Hermes state, auth, memory, or
    Slack account files.
    """

    enabled = _env_enabled("MAC_HERMES_STARTUP_CHECK", True)
    if not enabled:
        return {
            "enabled": False,
            "ready": True,
            "warnings": [],
            "state_refs": [],
            "slack": {"activation_source": "startup_check_disabled"},
            "runtime": {
                "configured_model": None,
                "gateway_runtime_shim_present": False,
                "warnings": [],
            },
            "security": {
                "secret_redaction": {"effective": True, "source": "startup_check_disabled"}
            },
            "logs": {"classes": [], "actionable_count": 0, "benign_count": 0},
            "qdrant_level2": {
                "status": "startup_check_disabled",
                "ready": True,
                "required": False,
            },
            "firecrawl_web_search": {
                "status": "startup_check_disabled",
                "ready": True,
                "required": False,
            },
            "tokenhub": {
                "status": "startup_check_disabled",
                "ready": True,
                "required": False,
            },
            "task_project_runtime": {
                "status": "startup_check_disabled",
                "ready": True,
                "required": False,
                "prompt_bridge": {"required": False, "present": False},
            },
            "operator_health": {"status": "healthy"},
        }

    hermes_home = _expand_path(os.environ.get("HERMES_HOME"), "~/.hermes")
    acc_dir = _expand_path(os.environ.get("ACC_DIR"), "~/.acc")
    hermes_refs = _refs(hermes_home, STATE_REF_CANDIDATES)
    acc_refs = _refs(acc_dir, ACC_STATE_REF_CANDIDATES)
    state_refs = hermes_refs + acc_refs
    slack = _slack_activation_report(hermes_home, hermes_refs)
    runtime = apply_hermes_gateway_runtime_shim_report()
    secret_redaction = _secret_redaction_report(hermes_home, acc_dir)
    logs = _log_classification_report()
    qdrant = _qdrant_memory_report(hermes_home)
    firecrawl = _firecrawl_web_search_report()
    tokenhub = _tokenhub_report()
    task_project_runtime = _runtime_context_summary(hermes_home)
    # ADR 0001 hu-04: the Hermes runtime is vendored in-tree, so verify the
    # mac-runtime-context prompt bridge against the vendored tree (which has the
    # patch folded in) rather than a separate upstream checkout.
    from mac.hermes_vendor import VENDOR_DIR, is_vendored

    agent_dir = Path(VENDOR_DIR) if is_vendored() else None
    task_project_runtime["prompt_bridge"] = _runtime_prompt_bridge_report(
        agent_dir,
        required=bool(task_project_runtime["required"]),
    )
    if task_project_runtime["prompt_bridge"]["warning"]:
        task_project_runtime["ready"] = False

    warnings: List[str] = []
    if not hermes_home.exists():
        warnings.append("Hermes home does not exist: %s" % hermes_home)
    if not _ref_exists(hermes_refs, "hermes_config"):
        warnings.append("Hermes config.yaml is missing")
    if not _ref_exists(hermes_refs, "soul"):
        warnings.append("Hermes SOUL.md is missing")
    if not _ref_exists(hermes_refs, "conversation_state"):
        warnings.append("Hermes state.db is missing")
    if slack["warning"]:
        warnings.append(slack["warning"])
    warnings.extend(runtime["warnings"])
    warnings.extend(secret_redaction["warnings"])
    warnings.extend(logs["warnings"])
    if qdrant["warning"]:
        warnings.append(qdrant["warning"])
    if firecrawl["warning"]:
        warnings.append(firecrawl["warning"])
    if tokenhub["warning"]:
        warnings.append(tokenhub["warning"])
    if task_project_runtime["warning"]:
        warnings.append(task_project_runtime["warning"])
    if task_project_runtime["prompt_bridge"]["warning"]:
        warnings.append(task_project_runtime["prompt_bridge"]["warning"])

    checks = {
        "hermes_home_exists": hermes_home.exists(),
        "config_present": _ref_exists(hermes_refs, "hermes_config"),
        "soul_present": _ref_exists(hermes_refs, "soul"),
        "long_term_memory_present": _ref_exists(hermes_refs, "long_term_memory")
        or _ref_exists(hermes_refs, "memory_long_term"),
        "conversation_state_present": _ref_exists(hermes_refs, "conversation_state"),
        "slack_activates_if_configured": (
            not slack["account_file_present"] or bool(slack["can_activate"])
        ),
        "secret_redaction_enabled": bool(secret_redaction["effective"])
        and not secret_redaction["drift_detected"],
        "logs_have_no_actionable_classes": not bool(logs["actionable_count"]),
        "shared_qdrant_memory_ready": bool(qdrant["ready"]),
        "firecrawl_web_search_ready": bool(firecrawl["ready"]),
        "tokenhub_ready": bool(tokenhub["ready"]),
        "tokenhub_client_key_present": (
            bool(tokenhub["api_key_present"]) or not tokenhub["required"]
        ),
        "memory_topology_available": (
            not qdrant["required"] or bool(qdrant["topology"]["file"]["exists"])
        ),
        "task_project_runtime_context_available": bool(task_project_runtime["ready"]),
        "task_project_runtime_prompt_bridge_active": bool(
            task_project_runtime["prompt_bridge"]["present"]
        )
        or not task_project_runtime["prompt_bridge"]["required"],
        "task_project_runtime_markdown_contract_present": bool(
            task_project_runtime.get("markdown_contract", {}).get("ready")
        )
        or not task_project_runtime["required"],
        "mac_task_project_authority_declared": (
            not task_project_runtime["required"]
            or (
                task_project_runtime["authority"].get("tasks") == "mac"
                and task_project_runtime["authority"].get("projects") == "mac"
                and task_project_runtime["authority"].get("agents") == "mac"
                and task_project_runtime["authority"].get("fleets") == "mac"
            )
        ),
        "mac_session_capability_contract_declared": (
            not task_project_runtime["required"]
            or {
                "mac_api",
                "mac_cli",
                "mac_hermes_cli",
                "shell_execution",
                "workspace_file_access",
                "ticket_mirror",
        "mac_task_cli",
                "git_source_control",
                "quality_gate",
                "hermes_oneshot_executor",
                "command_audit",
                "web_search",
            }
            <= set(task_project_runtime["session_capability_names"])
        ),
        "mac_first_class_object_model_declared": (
            not task_project_runtime["required"]
            or {"fleets", "tasks", "projects", "agents"}
            <= set(task_project_runtime["first_class_object_names"])
        ),
        "mac_session_capabilities_available": (
            not task_project_runtime["required"]
            or bool(task_project_runtime["session_capability_availability"]["ready"])
        ),
        "gateway_runtime_override_active": (
            not (
                runtime["configured_model"]
                or runtime["provider_override_configured"]
                or runtime["base_url_override_configured"]
            )
            or bool(runtime["gateway_runtime_shim_present"])
        ),
    }
    state_refs_existing = sum(1 for ref in state_refs if ref["exists"])
    operator_status = "healthy" if not warnings else "degraded"

    return {
        "enabled": True,
        "ready": not warnings,
        "hermes_home": str(hermes_home),
        "acc_dir": str(acc_dir),
        "checks": checks,
        "warnings": warnings,
        "state_refs": state_refs,
        "slack": slack,
        "runtime": runtime,
        "security": {"secret_redaction": secret_redaction},
        "logs": logs,
        "qdrant_level2": qdrant,
        "firecrawl_web_search": firecrawl,
        "tokenhub": tokenhub,
        "task_project_runtime": task_project_runtime,
        "operator_health": {
            "status": operator_status,
            "state_refs_existing": state_refs_existing,
            "slack_activation_source": slack["activation_source"],
            "gateway_model": runtime["configured_model"],
            "gateway_runtime_shim_present": runtime["gateway_runtime_shim_present"],
            "secret_redaction_effective": secret_redaction["effective"],
            "log_actionable_count": logs["actionable_count"],
            "qdrant_level2_status": qdrant["status"],
            "qdrant_level2_ready": qdrant["ready"],
            "firecrawl_web_search_status": firecrawl["status"],
            "firecrawl_web_search_ready": firecrawl["ready"],
            "tokenhub_status": tokenhub["status"],
            "tokenhub_ready": tokenhub["ready"],
            "tokenhub_model_count": tokenhub["model_count"],
            "memory_topology_present": qdrant["topology"]["file"]["exists"],
            "task_project_runtime_status": task_project_runtime["status"],
            "task_project_runtime_ready": task_project_runtime["ready"]
            and (
                task_project_runtime["prompt_bridge"]["present"]
                or not task_project_runtime["prompt_bridge"]["required"]
            ),
            "task_project_runtime_present": task_project_runtime["context_file"]["exists"],
            "task_project_runtime_prompt_bridge_present": task_project_runtime["prompt_bridge"]["present"],
            "task_project_runtime_hermes_instance_id": task_project_runtime["hermes_instance_id"],
            "task_project_runtime_session_capabilities_ready": task_project_runtime[
                "session_capability_availability"
            ]["ready"],
        },
    }
