#!/usr/bin/env python3
"""Validate and freeze explicit dispatch-hold adoption authority.

The fleet deployer must never infer permission to clear an operator hold.  This
helper turns a narrowly-scoped, owner-only JSON authorization into an immutable
per-invocation snapshot and provides deterministic cohort/lookup operations for
the shell controller.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Dict, Iterable, List


SCHEMA = "mac.dispatch_hold_adoptions.v1"
MAX_AUTHORITY_BYTES = 1024 * 1024
TOP_LEVEL_KEYS = {"schema", "fleet", "hub_agent", "source_commit", "adoptions"}
ADOPTION_KEYS = {"agent", "reason"}


class AuthorityError(ValueError):
    """The hold-adoption authority is unsafe or outside its exact scope."""


def _object_without_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorityError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _bounded_text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AuthorityError("%s must be a non-empty, trimmed string" % field)
    if len(value) > maximum:
        raise AuthorityError("%s exceeds %d characters" % (field, maximum))
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise AuthorityError("%s contains control characters" % field)
    return value


def parse_authority(raw: bytes, *, expected_commit: str | None = None) -> Dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthorityError("authority must be UTF-8 JSON") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, AuthorityError) as exc:
        raise AuthorityError("invalid hold-adoption authority: %s" % exc) from exc
    if not isinstance(payload, dict):
        raise AuthorityError("authority must be a JSON object")
    keys = set(payload)
    if keys != TOP_LEVEL_KEYS:
        raise AuthorityError(
            "authority keys must be exactly: %s" % ", ".join(sorted(TOP_LEVEL_KEYS))
        )
    if payload.get("schema") != SCHEMA:
        raise AuthorityError("authority schema must be %s" % SCHEMA)
    fleet = _bounded_text(payload.get("fleet"), "fleet", maximum=200)
    hub_agent = _bounded_text(payload.get("hub_agent"), "hub_agent", maximum=200)
    source_commit = _bounded_text(payload.get("source_commit"), "source_commit", maximum=128)
    if expected_commit is not None and source_commit != expected_commit:
        raise AuthorityError(
            "authority source_commit %s does not match deploy commit %s"
            % (source_commit, expected_commit)
        )
    raw_adoptions = payload.get("adoptions")
    if not isinstance(raw_adoptions, list):
        raise AuthorityError("adoptions must be a JSON list")
    adoptions: List[Dict[str, str]] = []
    seen: set[str] = set()
    seen_casefolded: set[str] = set()
    for index, item in enumerate(raw_adoptions):
        if not isinstance(item, dict) or set(item) != ADOPTION_KEYS:
            raise AuthorityError("adoptions[%d] keys must be exactly: agent, reason" % index)
        agent = _bounded_text(item.get("agent"), "adoptions[%d].agent" % index, maximum=200)
        if not agent.startswith("agent_"):
            raise AuthorityError("adoptions[%d].agent must be a stable agent id" % index)
        reason = _bounded_text(item.get("reason"), "adoptions[%d].reason" % index, maximum=1024)
        if agent in seen:
            raise AuthorityError("duplicate adoption agent: %s" % agent)
        if agent.casefold() in seen_casefolded:
            raise AuthorityError("case-colliding adoption agent: %s" % agent)
        seen.add(agent)
        seen_casefolded.add(agent.casefold())
        adoptions.append({"agent": agent, "reason": reason})
    adoptions.sort(key=lambda item: item["agent"])
    return {
        "schema": SCHEMA,
        "fleet": fleet,
        "hub_agent": hub_agent,
        "source_commit": source_commit,
        "adoptions": adoptions,
    }


def _read_owner_only_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthorityError("cannot safely open authority %s: %s" % (path, exc)) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AuthorityError("authority must be a regular file, not a symlink or device")
        if metadata.st_uid != os.getuid():
            raise AuthorityError("authority must be owned by the invoking uid")
        if not stat.S_IMODE(metadata.st_mode) & stat.S_IRUSR:
            raise AuthorityError("authority must be readable by its owner")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise AuthorityError("authority must not have group or other permission bits")
        if metadata.st_size < 1 or metadata.st_size > MAX_AUTHORITY_BYTES:
            raise AuthorityError(
                "authority size must be between 1 and %d bytes" % MAX_AUTHORITY_BYTES
            )
        chunks: List[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_AUTHORITY_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_AUTHORITY_BYTES:
                raise AuthorityError("authority grew beyond the maximum size while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def snapshot_authority(source: Path, output: Path, *, expected_commit: str) -> None:
    payload = parse_authority(
        _read_owner_only_regular_file(source), expected_commit=expected_commit
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load_snapshot(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AuthorityError("cannot read frozen authority %s: %s" % (path, exc)) from exc
    if len(raw) > MAX_AUTHORITY_BYTES:
        raise AuthorityError("frozen authority exceeds the maximum size")
    return parse_authority(raw)


def validate_selected(
    payload: Dict[str, Any],
    *,
    expected_fleet: str,
    expected_hub_agent: str,
    selected_agents: Iterable[str],
) -> None:
    if payload["fleet"] != expected_fleet:
        raise AuthorityError(
            "authority fleet %s does not match selected fleet %s"
            % (payload["fleet"], expected_fleet)
        )
    if payload["hub_agent"] != expected_hub_agent:
        raise AuthorityError(
            "authority hub_agent %s does not match selected hub %s"
            % (payload["hub_agent"], expected_hub_agent)
        )
    selected = set(selected_agents)
    if not selected:
        raise AuthorityError("selected cohort is empty")
    extras = sorted(item["agent"] for item in payload["adoptions"] if item["agent"] not in selected)
    if extras:
        raise AuthorityError("authority contains unselected agents: %s" % ", ".join(extras))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("source", type=Path)
    snapshot.add_argument("output", type=Path)
    snapshot.add_argument("--source-commit", required=True)

    validate = subparsers.add_parser("validate-selected")
    validate.add_argument("snapshot", type=Path)
    validate.add_argument("--fleet", required=True)
    validate.add_argument("--hub-agent", required=True)
    validate.add_argument("--agent", action="append", default=[], dest="agents")

    reason = subparsers.add_parser("reason")
    reason.add_argument("snapshot", type=Path)
    reason.add_argument("agent")
    return parser


def main(argv: List[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "snapshot":
            snapshot_authority(
                arguments.source,
                arguments.output,
                expected_commit=arguments.source_commit,
            )
        elif arguments.command == "validate-selected":
            payload = load_snapshot(arguments.snapshot)
            validate_selected(
                payload,
                expected_fleet=arguments.fleet,
                expected_hub_agent=arguments.hub_agent,
                selected_agents=arguments.agents,
            )
        elif arguments.command == "reason":
            payload = load_snapshot(arguments.snapshot)
            reason = next(
                (
                    item["reason"]
                    for item in payload["adoptions"]
                    if item["agent"] == arguments.agent
                ),
                "",
            )
            print(reason)
        else:  # pragma: no cover - argparse enforces the command set.
            raise AssertionError(arguments.command)
    except AuthorityError as exc:
        print("ERROR: %s" % exc, file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
