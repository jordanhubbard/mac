"""OpenShell supervisor entrypoint for MAC agent hosts.

The production shape is one long-lived supervisor per agent. It materializes
the assigned MAC-managed policy, starts one named OpenShell sandbox, and runs
MAC/Hermes runtime children inside that boundary. This module keeps the command
construction testable and fail-closed; the Linux hosts provide the actual
OpenShell binary/runtime.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from mac import mac_paths
from typing import Iterable, List, Optional, Sequence

from mac.openshell_runtime import base_agent_name, openshell_required_for_identity


def build_supervisor_argv(
    *,
    agent_id: str,
    policy_path: str,
    child_argv: Sequence[str],
    openshell_bin: str = "openshell",
    sandbox_name: Optional[str] = None,
    keep: bool = True,
    env_passthrough: Optional[Iterable[str]] = None,
    extra_create_args: Optional[Sequence[str]] = None,
) -> List[str]:
    if not agent_id:
        raise ValueError("agent_id is required")
    if not policy_path:
        raise ValueError("policy_path is required")
    if not child_argv:
        raise ValueError("child_argv is required")
    argv: List[str] = [
        openshell_bin,
        "sandbox",
        "create",
        "--no-auto-providers",
        "--policy",
        policy_path,
        "--name",
        sandbox_name or "mac-%s" % base_agent_name(agent_id),
    ]
    if keep:
        argv.append("--keep")
    for name in env_passthrough or ():
        value = os.environ.get(name)
        if value is not None:
            argv += ["--env", "%s=%s" % (name, value)]
    if extra_create_args:
        argv += list(extra_create_args)
    argv += ["--", *child_argv]
    return argv


def default_policy_path(agent_id: str) -> Path:
    explicit = os.environ.get("MAC_OPENSHELL_POLICY")
    if explicit:
        return Path(explicit).expanduser()
    return mac_paths.mac_home() / "openshell" / ("%s-policy.yaml" % base_agent_name(agent_id))


def default_child_argv() -> List[str]:
    configured = os.environ.get("MAC_OPENSHELL_CHILD")
    if configured:
        import shlex

        return shlex.split(configured)
    return ["mac-hermes-gateway"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mac-openshell-supervisor")
    parser.add_argument("--agent-id", default=os.environ.get("MAC_AGENT_ID") or os.uname().nodename)
    parser.add_argument("--policy")
    parser.add_argument("--openshell-bin", default=os.environ.get("MAC_OPENSHELL_BIN") or "openshell")
    parser.add_argument("--sandbox-name")
    parser.add_argument("--no-keep", action="store_true")
    parser.add_argument("--allow-unsandboxed", action="store_true")
    parser.add_argument("child", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv) if argv is not None else None)

    child = list(args.child)
    if child and child[0] == "--":
        child = child[1:]
    if not child:
        child = default_child_argv()

    required = openshell_required_for_identity(
        agent_id=args.agent_id,
        explicit=os.environ.get("MAC_OPENSHELL_REQUIRED")
        if "MAC_OPENSHELL_REQUIRED" in os.environ
        else None,
    )
    policy = Path(args.policy).expanduser() if args.policy else default_policy_path(args.agent_id)
    openshell = shutil.which(args.openshell_bin) or (
        args.openshell_bin if Path(args.openshell_bin).exists() else None
    )
    if required and not openshell:
        sys.stderr.write("OpenShell is required for %s but %s was not found\n" % (args.agent_id, args.openshell_bin))
        return 78
    if required and not policy.is_file():
        sys.stderr.write("OpenShell is required for %s but policy %s is missing\n" % (args.agent_id, policy))
        return 78
    if not openshell or not policy.is_file():
        if args.allow_unsandboxed:
            return subprocess.call(child)
        sys.stderr.write("OpenShell supervisor cannot start without OpenShell and a policy\n")
        return 78

    os.environ["MAC_OPENSHELL_SANDBOX"] = "1"
    os.environ["MAC_ALLOW_UNSANDBOXED_YOLO"] = "0"
    os.environ["MAC_OPENSHELL_POLICY"] = str(policy)
    command = build_supervisor_argv(
        agent_id=args.agent_id,
        policy_path=str(policy),
        child_argv=child,
        openshell_bin=openshell,
        sandbox_name=args.sandbox_name,
        keep=not args.no_keep,
        env_passthrough=(
            "MAC_AGENT_ID",
            "MAC_HUB_URL",
            "MAC_API_TOKEN",
            "MAC_WORKER_TOKEN",
            "HERMES_GATEWAY_BASE_URL",
            "HERMES_GATEWAY_MODEL",
        ),
    )
    return subprocess.call(command)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
