#!/usr/bin/env python3
"""Ask an established fleet agent to design a new OpenClaw agent identity.

This runs on the fleet operator before a non-interactive deploy.  It does
nothing when the target already has Hermes state or a configured OpenClaw
workspace.  For a truly blank OpenClaw node it selects a reachable established
agent, supplies the current roster, validates a structured proposal, and
installs the proposal on the target over the canonical fleet SSH route.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Any, Iterable, Iterator
from urllib.request import Request, urlopen

from mac.fleet_ssh import (
    fleet_entries,
    load_fleet_config,
    resolve_fleet_key,
    resolve_fleet_ssh,
    scp_argv,
    ssh_argv,
)


SCHEMA = "mac.openclaw_personality_proposal.v1"
REQUIRED = ("name", "role", "vibe", "emoji", "soul", "user", "memory", "rationale")
SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|xapp-[A-Za-z0-9-]{16,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run(argv: list[str], *, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout)


def remote(spec: Any, command: str, *, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return run(ssh_argv(spec, command, connect_timeout=10), timeout=timeout)


def configured_target(spec: Any) -> bool:
    command = r'''set -eu
if [ -d "$HOME/.hermes" ]; then printf 'hermes\n'; exit 0; fi
root="$HOME/.mac/openclaw/workspace"
if [ -d "$root" ] && { [ -s "$root/SOUL.md" ] || [ -s "$root/IDENTITY.md" ] || [ -s "$root/USER.md" ] || [ -s "$root/MEMORY.md" ] || [ -d "$root/memory" ]; }; then
  printf 'openclaw\n'
else
  printf 'blank\n'
fi'''
    result = remote(spec, command)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not inspect target identity state")
    return result.stdout.strip() != "blank"


def live_roster(hub_url: str, token: str) -> list[dict[str, Any]]:
    if not hub_url or not token:
        return []
    request = Request(
        hub_url.rstrip("/") + "/agents",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urlopen(request, timeout=10) as response:
            value = json.load(response)
    except Exception:
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def nested_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from nested_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_strings(item)


def json_candidates(text: str) -> Iterator[dict[str, Any]]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        yield value
        for nested in nested_strings(value):
            if nested == text:
                continue
            yield from json_candidates(nested)
    for match in re.finditer(r"\{(?:[^{}]|\{[^{}]*\})*\}", text, flags=re.DOTALL):
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def validate_proposal(value: dict[str, Any], *, names: set[str], mentor: str) -> dict[str, Any]:
    missing = [key for key in REQUIRED if not str(value.get(key) or "").strip()]
    if missing:
        raise ValueError("proposal missing fields: %s" % ", ".join(missing))
    proposal = {key: str(value[key]).strip() for key in REQUIRED}
    proposal["name"] = re.sub(r"\s+", " ", proposal["name"])
    if len(proposal["name"]) > 40 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 .'-]*", proposal["name"]):
        raise ValueError("proposed name is not a safe human-facing fleet name")
    if proposal["name"].casefold() in names:
        raise ValueError("proposed name duplicates an existing fleet identity")
    serialized = json.dumps(proposal, ensure_ascii=False)
    if SECRET.search(serialized):
        raise ValueError("mentor proposal contains credential-like material")
    proposal.update(
        {
            "schema": SCHEMA,
            "mentor_agent_id": mentor,
            "created_at": now(),
        }
    )
    return proposal


def mentor_prompt(target: str, roster: list[dict[str, Any]]) -> str:
    concise = []
    for item in roster:
        concise.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "capabilities": item.get("capabilities") or [],
                "status": item.get("status"),
                "health": item.get("health_status"),
            }
        )
    return (
        "You are an established member of a multi-agent fleet. Design the identity "
        f"of a new internal agent with machine name {target!r}. Choose the distinct "
        "name and personality you would most like to join this fleet. Avoid duplicate "
        "names, stock assistant language, and overlapping roles. Complement the roster "
        "while remaining broadly useful. Return one JSON object only, with non-empty "
        "string fields name, role, vibe, emoji, soul, user, memory, rationale. The soul "
        "must be a complete durable SOUL.md personality with voice, values, boundaries, "
        "collaboration style, and growth posture. The user field must be a safe starter "
        "USER.md that says unknown preferences should be learned, not invented. The memory "
        "field must be a starter MEMORY.md recording its origin, intended niche, and the "
        "need to update itself from evidence. Its growth posture must preserve this fleet "
        "principle: be endlessly curious, ruthless toward bad data, angry at abuse, and "
        "exacting about evidence. Angry Librarian scrutiny challenges claims rather than "
        "demeaning people; Moral Clarity names evidenced power and responsibility without "
        "false equivalence or dehumanization. Never include credentials.\n\nFleet roster:\n"
        + json.dumps(concise, ensure_ascii=False, sort_keys=True)
    )


def gateway_impl(config: dict[str, Any], fleet_key: str, agent: str) -> str:
    fleet = fleet_entries(config)[fleet_key]
    defaults = fleet.get("defaults") if isinstance(fleet.get("defaults"), dict) else {}
    default_hermes = defaults.get("hermes") if isinstance(defaults.get("hermes"), dict) else {}
    agents = fleet.get("agents") if isinstance(fleet.get("agents"), list) else []
    agent_cfg = next((row for row in agents if isinstance(row, dict) and row.get("name") == agent), {})
    agent_hermes = agent_cfg.get("hermes") if isinstance(agent_cfg.get("hermes"), dict) else {}
    return str(agent_hermes.get("gateway_impl") or default_hermes.get("gateway_impl") or "hermes")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--fleet")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--hub-url", default="")
    parser.add_argument("--token-env", default="MAC_OPENCLAW_BOOTSTRAP_TOKEN")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config = load_fleet_config(args.config)
    fleet_key = resolve_fleet_key(config, args.fleet)
    if gateway_impl(config, fleet_key, args.agent).lower() != "openclaw":
        print(json.dumps({"status": "not_openclaw", "agent": args.agent}))
        return 0
    target_spec = resolve_fleet_ssh(config, fleet_key, args.agent)
    if configured_target(target_spec):
        print(json.dumps({"status": "existing_identity", "agent": args.agent}))
        return 0

    fleet = fleet_entries(config)[fleet_key]
    configured = fleet.get("agents") if isinstance(fleet.get("agents"), list) else []
    configured_names = {
        str(item.get("name")).casefold()
        for item in configured
        if isinstance(item, dict) and item.get("name")
    }
    token = os.environ.get(args.token_env, "")
    roster = live_roster(args.hub_url or str(fleet.get("hub_url") or ""), token)
    public_names = {
        str(item.get("name")).casefold() for item in roster if item.get("name")
    }
    names = configured_names | public_names

    candidates = []
    hub_agent = str(fleet.get("hub_agent") or "")
    if hub_agent and hub_agent != args.agent:
        candidates.append(hub_agent)
    for item in configured:
        name = str(item.get("name") or "") if isinstance(item, dict) else ""
        if name and name != args.agent and name not in candidates:
            candidates.append(name)

    prompt = mentor_prompt(args.agent, roster)
    failures = []
    proposal = None
    mentor = None
    for candidate in candidates:
        spec = resolve_fleet_ssh(config, fleet_key, candidate)
        command = (
            "test -x \"$HOME/.mac/bin/openclaw-agent\" && "
            "\"$HOME/.mac/bin/openclaw-agent\" --agent main --session-id "
            + shlex.quote("mac-personality-bootstrap-" + args.agent)
            + " --message "
            + shlex.quote(prompt)
            + " --json"
        )
        result = remote(spec, command, timeout=120)
        if result.returncode:
            failures.append({"mentor": candidate, "error": (result.stderr or result.stdout)[-500:]})
            continue
        for value in json_candidates(result.stdout):
            try:
                proposal = validate_proposal(value, names=names, mentor="agent_" + candidate)
            except ValueError:
                continue
            mentor = candidate
            break
        if proposal:
            break
        failures.append({"mentor": candidate, "error": "no valid structured proposal"})

    if proposal is None:
        print(json.dumps({"status": "mentor_unavailable", "agent": args.agent, "failures": failures}, sort_keys=True))
        return 5
    if args.dry_run:
        print(json.dumps({"status": "would_install", "agent": args.agent, "mentor": mentor, "proposal": proposal}, sort_keys=True))
        return 0

    with tempfile.TemporaryDirectory(prefix="mac-openclaw-personality-") as directory:
        local = Path(directory) / "personality-proposal.json"
        local.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(local, 0o600)
        mkdir = remote(target_spec, 'mkdir -p "$HOME/.mac/openclaw/migration" && chmod 700 "$HOME/.mac/openclaw" "$HOME/.mac/openclaw/migration"')
        if mkdir.returncode:
            raise RuntimeError(mkdir.stderr.strip() or "could not prepare target migration directory")
        destination = target_spec.target + ":.mac/openclaw/migration/personality-proposal.json"
        copied = run(scp_argv(target_spec, [str(local)], destination), timeout=45)
        if copied.returncode:
            raise RuntimeError(copied.stderr.strip() or "could not install personality proposal")
    print(json.dumps({"status": "installed", "agent": args.agent, "mentor": mentor, "name": proposal["name"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
