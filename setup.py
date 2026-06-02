#!/usr/bin/env python3
"""Compatibility setup entrypoint.

This is intentionally not a Python packaging ``setup()`` file. The project uses
``pyproject.toml``/hatchling for packaging; this script replaces ``setup.sh``'s
Bash orchestration with Python so first-run fleet setup works on macOS systems
that still ship old Bash.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


ROOT = Path(__file__).resolve().parent
SETUP_FLEET = ROOT / "scripts" / "setup-fleet.py"
DEPLOY_FLEET = ROOT / "deploy" / "deploy-mac-fleet.sh"
DEFAULT_ENV_FILE = Path.home() / ".mac" / ".env"


def parse_setup_args(argv: Sequence[str]) -> Tuple[List[str], bool, bool, bool]:
    """Return (forwarded_args, config_only, dry_run, deploy_direct).

    Mirrors the old setup.sh contract:
    - --configure-only / --no-deploy are consumed by setup.py.
    - --deploy is a no-op because deploy-after-config is the default.
    - --hub / --new-hub short-circuit straight to the deploy wrapper unless
      configure-only was requested.
    """
    forwarded: List[str] = []
    config_only = False
    dry_run = False
    deploy_direct = False
    for arg in argv:
        if arg in {"--configure-only", "--no-deploy"}:
            config_only = True
            continue
        if arg == "--dry-run":
            dry_run = True
            forwarded.append(arg)
            continue
        if arg == "--deploy":
            continue
        if arg in {"--hub", "--new-hub"} or arg.startswith("--hub=") or arg.startswith("--new-hub="):
            deploy_direct = True
        forwarded.append(arg)
    return forwarded, config_only, dry_run, deploy_direct


def parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.replace("export ", "").strip()
        try:
            parts = shlex.split(value, posix=True)
            values[key] = parts[0] if parts else ""
        except ValueError:
            values[key] = value.strip().strip("'\"")
    return values


def deploy_args_from_plan(plan: Dict[str, object]) -> List[str]:
    hub = str(plan.get("hub") or "").strip()
    if not hub:
        raise RuntimeError("setup plan missing hub")
    args = [str(DEPLOY_FLEET), "--hub", hub]
    for item in plan.get("agents") or []:
        agent = str(item).strip()
        if agent:
            args.append(agent)
    return args


def run(cmd: Sequence[str], *, env: Dict[str, str] | None = None) -> int:
    return subprocess.call(list(cmd), cwd=str(ROOT), env=env)


def run_setup_fleet(args: Sequence[str]) -> int:
    return run([sys.executable, str(SETUP_FLEET), *args])


def deploy_env(env_file: Path | None = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.update(parse_env_file((env_file or DEFAULT_ENV_FILE).expanduser()))
    env["PYTHON"] = sys.executable
    return env


def run_deploy(args: Sequence[str], *, env: Dict[str, str] | None = None) -> int:
    deploy_process_env = deploy_env() if env is None else dict(env)
    deploy_process_env["PYTHON"] = sys.executable
    return run([str(DEPLOY_FLEET), *args], env=deploy_process_env)


def configure_then_deploy(args: Sequence[str]) -> int:
    fd, raw_path = tempfile.mkstemp(prefix="mac-setup-plan.", suffix=".json")
    os.close(fd)
    plan_path = Path(raw_path)
    try:
        rc = run_setup_fleet(["--deploy-plan-file", str(plan_path), *args])
        if rc != 0:
            return rc
        if not plan_path.exists() or plan_path.stat().st_size == 0:
            return 0
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise RuntimeError("setup plan must be a JSON object")
        env_file = str(plan.get("env_file") or "").strip()
        env = deploy_env(Path(env_file)) if env_file else deploy_env()
        return run(deploy_args_from_plan(plan), env=env)
    finally:
        try:
            plan_path.unlink()
        except OSError:
            pass


def main(argv: Iterable[str] | None = None) -> int:
    forwarded, config_only, dry_run, deploy_direct = parse_setup_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    if not config_only and deploy_direct:
        return run_deploy(forwarded)
    if config_only or dry_run:
        return run_setup_fleet(forwarded)
    return configure_then_deploy(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
