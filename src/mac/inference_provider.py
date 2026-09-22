"""Home-scoped, fail-safe convergence for self-hosted inference providers.

The resource is intentionally one-shot: the fleet scheduler decides when hosts
reconcile, so this module cannot create a synchronized polling herd.  Health
loss is observational and never authorizes replacement or removal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from mac import mac_paths
from mac.atomic_file import atomic_write_text
from mac.deploy_env import parse_env_text, render_env
from mac.models import ValidationError

SCHEMA = "mac.self_hosted_inference_providers.v1"
_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_REVISION = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    image: str
    model: str
    model_revision: str
    router_base_url: str
    port: int
    bind_host: str = "127.0.0.1"
    priority: int = 0
    gpu_count: int = 1
    min_free_memory_mib: int = 1

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> "ProviderSpec":
        allowed = {
            "provider_id",
            "image",
            "model",
            "model_revision",
            "router_base_url",
            "port",
            "bind_host",
            "priority",
            "gpu_count",
            "min_free_memory_mib",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValidationError("unknown provider fields: %s" % ", ".join(sorted(unknown)))
        normalized = dict(value)
        for field in ("port", "priority", "gpu_count", "min_free_memory_mib"):
            raw = normalized.get(field, getattr(cls, field, None))
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValidationError("%s must be an integer" % field)
        try:
            item = cls(**normalized)
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid provider resource: %s" % exc) from exc
        if not _ID.fullmatch(item.provider_id):
            raise ValidationError("provider_id must be a lowercase DNS-style label")
        if not _DIGEST.fullmatch(item.image):
            raise ValidationError("image must be pinned by sha256 digest")
        if not item.model or any(ch.isspace() or ch in ",;|=" for ch in item.model):
            raise ValidationError("model must be an exact non-empty model id")
        if not _REVISION.fullmatch(item.model_revision):
            raise ValidationError("model_revision must be an immutable hexadecimal commit revision")
        parsed_url = urlsplit(item.router_base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValidationError("router_base_url must be an explicit http(s) URL")
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValidationError(
                "router_base_url must not contain credentials, query, or fragment"
            )
        if any(ch in item.router_base_url for ch in ",;"):
            raise ValidationError("router_base_url contains a router-spec delimiter")
        if not (1 <= int(item.port) <= 65535):
            raise ValidationError("port must be between 1 and 65535")
        if int(item.gpu_count) < 1 or int(item.min_free_memory_mib) < 1:
            raise ValidationError("GPU count and free-memory requirement must be positive")
        return item

    @property
    def fingerprint(self) -> str:
        wire = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(wire.encode()).hexdigest()

    @property
    def container_name(self) -> str:
        return "mac-inference-%s" % self.provider_id

    @property
    def cache_dir(self) -> Path:
        return mac_paths.inference_providers_dir() / self.provider_id / "model-cache"

    @property
    def router_spec(self) -> str:
        base = self.router_base_url.rstrip("/")
        return "%s=%s,%d,models=%s" % (self.provider_id, base, self.priority, self.model)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]
Health = Callable[[ProviderSpec], tuple[bool, str]]


def subprocess_runner(command: Sequence[str]) -> CommandResult:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def load_specs(path: Path | None = None) -> list[ProviderSpec]:
    source = path or mac_paths.inference_providers_config()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError("provider resource file not found: %s" % source) from exc
    except json.JSONDecodeError as exc:
        raise ValidationError("invalid provider JSON: %s" % exc) from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ValidationError("provider resource must use schema %s" % SCHEMA)
    values = raw.get("providers")
    if not isinstance(values, list):
        raise ValidationError("providers must be a list")
    specs = [ProviderSpec.parse(value) for value in values]
    ids = [item.provider_id for item in specs]
    if len(ids) != len(set(ids)):
        raise ValidationError("provider_id values must be unique")
    return specs


def gpu_capacity(runner: Runner) -> list[int]:
    result = runner(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
    if result.returncode:
        raise RuntimeError(
            "GPU discovery failed: %s" % (result.stderr.strip() or "nvidia-smi failed")
        )
    try:
        return [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    except ValueError as exc:
        raise RuntimeError("GPU discovery returned non-numeric capacity") from exc


def check_capacity(spec: ProviderSpec, runner: Runner) -> None:
    available = [mib for mib in gpu_capacity(runner) if mib >= spec.min_free_memory_mib]
    if len(available) < spec.gpu_count:
        raise RuntimeError(
            "%s requires %d GPU(s) with %d MiB free; found %d"
            % (spec.provider_id, spec.gpu_count, spec.min_free_memory_mib, len(available))
        )


def http_health(spec: ProviderSpec) -> tuple[bool, str]:
    url = spec.router_base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # health is evidence, not a destructive signal
        return False, str(exc)
    ids = {str(item.get("id")) for item in body.get("data", []) if isinstance(item, dict)}
    if spec.model not in ids:
        return False, "exact model %s absent from /models" % spec.model
    return True, "exact model available"


def wait_for_health(
    spec: ProviderSpec,
    health: Health,
    *,
    attempts: int = 6,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Bounded startup wait with provider-specific jitter to avoid a probe herd."""
    detail = "health was not checked"
    for attempt in range(max(1, attempts)):
        ok, detail = health(spec)
        if ok:
            return True, detail
        if attempt + 1 < attempts:
            fingerprint_jitter = int(spec.fingerprint[:4], 16) / 65535
            sleep(min(8.0, 0.5 * (2**attempt)) + fingerprint_jitter)
    return False, detail


def _inspect(spec: ProviderSpec, runner: Runner) -> tuple[str, str] | None:
    result = runner(["docker", "inspect", spec.container_name])
    if result.returncode:
        return None
    try:
        item = json.loads(result.stdout)[0]
        labels = item["Config"].get("Labels") or {}
        state = "running" if item["State"].get("Running") else "stopped"
        return state, str(labels.get("mac.provider.fingerprint", ""))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("malformed docker inspection for %s" % spec.provider_id) from exc


def _run_args(spec: ProviderSpec) -> list[str]:
    return [
        "docker",
        "run",
        "-d",
        "--name",
        spec.container_name,
        "--restart",
        "unless-stopped",
        "--gpus",
        str(spec.gpu_count),
        "-p",
        "%s:%d:8000" % (spec.bind_host, spec.port),
        "-v",
        "%s:/root/.cache/huggingface" % spec.cache_dir,
        "--label",
        "mac.provider.fingerprint=%s" % spec.fingerprint,
        "--label",
        "mac.provider.id=%s" % spec.provider_id,
        spec.image,
        "--model",
        spec.model,
        "--revision",
        spec.model_revision,
    ]


def _must_run(runner: Runner, command: Sequence[str]) -> None:
    result = runner(command)
    if result.returncode:
        raise RuntimeError("command failed: %s: %s" % (" ".join(command), result.stderr.strip()))


def reconcile_one(
    spec: ProviderSpec,
    *,
    runner: Runner = subprocess_runner,
    health: Health = http_health,
    dry_run: bool = True,
    allow_upgrade: bool = False,
    health_attempts: int = 6,
) -> dict[str, Any]:
    current = _inspect(spec, runner)
    actions: list[str] = []
    if current and current[1] != spec.fingerprint and not allow_upgrade:
        return {"provider_id": spec.provider_id, "status": "upgrade_required", "actions": []}
    if current is None or current[1] != spec.fingerprint:
        check_capacity(spec, runner)
        actions.extend(["pull pinned image", "create provider container"])
        if not dry_run:
            spec.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            _must_run(runner, ["docker", "pull", spec.image])
            if current:
                rollback = spec.container_name + "-rollback"
                _must_run(runner, ["docker", "stop", spec.container_name])
                _must_run(runner, ["docker", "rename", spec.container_name, rollback])
                try:
                    _must_run(runner, _run_args(spec))
                    ok, detail = wait_for_health(spec, health, attempts=health_attempts)
                    if not ok:
                        raise RuntimeError("replacement failed health: %s" % detail)
                    _must_run(runner, ["docker", "rm", rollback])
                except Exception:
                    runner(["docker", "rm", "-f", spec.container_name])
                    _must_run(runner, ["docker", "rename", rollback, spec.container_name])
                    _must_run(runner, ["docker", "start", spec.container_name])
                    raise
            else:
                _must_run(runner, _run_args(spec))
                ok, detail = wait_for_health(spec, health, attempts=health_attempts)
                if not ok:
                    return {
                        "provider_id": spec.provider_id,
                        "status": "degraded",
                        "health": detail,
                        "actions": actions,
                    }
        return {
            "provider_id": spec.provider_id,
            "status": "planned" if dry_run else "converged",
            "actions": actions,
        }
    if current[0] == "stopped":
        check_capacity(spec, runner)
        actions.append("start exact provider container")
        if not dry_run:
            _must_run(runner, ["docker", "start", spec.container_name])
    ok, detail = health(spec) if not dry_run else (True, "health probe deferred in dry-run")
    return {
        "provider_id": spec.provider_id,
        "status": ("healthy" if ok else "degraded"),
        "health": detail,
        "actions": actions,
    }


def register_router(
    specs: Sequence[ProviderSpec], *, dry_run: bool = True, path: Path | None = None
) -> str:
    target = path or mac_paths.mac_env_file()
    try:
        values = parse_env_text(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        values = {}
    managed = {spec.provider_id for spec in specs}
    existing = [
        item
        for item in values.get("MAC_ROUTER_PROVIDERS", "").split(";")
        if item.strip() and item.split("=", 1)[0].strip() not in managed
    ]
    exact = existing + [spec.router_spec for spec in specs]
    values["MAC_ROUTER_PROVIDERS"] = ";".join(exact)
    rendered = render_env(values)
    if not dry_run:
        atomic_write_text(target, rendered, mode=0o600)
    return values["MAC_ROUTER_PROVIDERS"]


def unregister_router(
    specs: Sequence[ProviderSpec], *, dry_run: bool = True, path: Path | None = None
) -> str:
    target = path or mac_paths.mac_env_file()
    try:
        values = parse_env_text(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        values = {}
    managed = {spec.provider_id for spec in specs}
    values["MAC_ROUTER_PROVIDERS"] = ";".join(
        item
        for item in values.get("MAC_ROUTER_PROVIDERS", "").split(";")
        if item.strip() and item.split("=", 1)[0].strip() not in managed
    )
    if not dry_run:
        atomic_write_text(target, render_env(values), mode=0o600)
    return values["MAC_ROUTER_PROVIDERS"]


def remove_provider(
    spec: ProviderSpec, *, runner: Runner = subprocess_runner, dry_run: bool = True
) -> dict[str, Any]:
    current = _inspect(spec, runner)
    actions = ["remove provider container"] if current else []
    if current and not dry_run:
        _must_run(runner, ["docker", "rm", "-f", spec.container_name])
    return {
        "provider_id": spec.provider_id,
        "status": "planned" if dry_run else "removed",
        "actions": actions,
        "cache_preserved": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("reconcile", "health", "remove"):
        item = sub.add_parser(name)
        item.add_argument(
            "--apply", action="store_true", help="perform mutations; default is dry-run"
        )
        if name == "reconcile":
            item.add_argument("--allow-upgrade", action="store_true")
    args = parser.parse_args(argv)
    try:
        specs = load_specs(args.config)
        if args.command == "health":
            output = []
            for item in specs:
                healthy, detail = http_health(item)
                output.append(dict(provider_id=item.provider_id, healthy=healthy, detail=detail))
        elif args.command == "remove":
            output = [remove_provider(s, dry_run=not args.apply) for s in specs]
            unregister_router(specs, dry_run=not args.apply)
        else:
            output = [
                reconcile_one(s, dry_run=not args.apply, allow_upgrade=args.allow_upgrade)
                for s in specs
            ]
            if all(item["status"] not in {"degraded", "upgrade_required"} for item in output):
                register_router(specs, dry_run=not args.apply)
        print(json.dumps({"schema": SCHEMA, "providers": output}, indent=2, sort_keys=True))
        if args.command == "health":
            return 0 if all(item["healthy"] for item in output) else 1
        return (
            0
            if all(item.get("status") not in {"degraded", "upgrade_required"} for item in output)
            else 1
        )
    except (ValidationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
