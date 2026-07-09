#!/usr/bin/env python3
"""Durable, evidence-first curiosity workflow for managed OpenClaw agents.

Candidates remain quarantined until an explicit approval command promotes one
to the OpenClaw workspace.  Every state change is recorded in a hash-chained
provenance ledger.  Agent-facing OpenClaw tools deliberately expose submission,
inspection, and abuse framing, but not approval.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable


SCHEMA = "mac.openclaw_curiosity.v1"
LEDGER_SCHEMA = "mac.openclaw_curiosity_ledger.v1"
SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
POLICY = """Be endlessly curious, ruthless toward bad data, angry at abuse, and exacting about evidence.

Curiosity creates quarantined candidates, never automatic durable memory.
Angry Librarian mode challenges weak sourcing, missing provenance, and inflated certainty without attacking people.
Moral Clarity mode names documented harm, power, responsibility, and moral injury without flattening materially unequal conduct into false equivalence.
Protective anger must remain evidence-bound, proportionate, non-dehumanizing, and directed toward preventing harm.
Unknowns, inferences, counterevidence, and confidence must remain explicit.
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def redact(value: str) -> tuple[str, int]:
    count = 0
    for pattern in SECRET_PATTERNS:
        value, replacements = pattern.subn("[REDACTED_SECRET]", value)
        count += replacements
    return value, count


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


class CuriosityStore:
    def __init__(self, state_dir: Path, workspace: Path, agent_id: str) -> None:
        self.root = state_dir / "mac-curiosity"
        self.quarantine = self.root / "quarantine"
        self.ledger = self.root / "provenance.ndjson"
        self.lock = self.root / ".lock"
        self.workspace = workspace
        self.agent_id = agent_id
        self.quarantine.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.quarantine, 0o700)

    def _events(self) -> list[dict[str, Any]]:
        if not self.ledger.is_file():
            return []
        events = []
        for line in self.ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def append(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock.open("a+", encoding="utf-8") as guard:
            os.chmod(self.lock, 0o600)
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
            events = self._events()
            previous = str(events[-1].get("event_sha256") or "") if events else ""
            event = {
                "schema": LEDGER_SCHEMA,
                "sequence": len(events) + 1,
                "recorded_at": now(),
                "agent_id": self.agent_id,
                "action": action,
                "previous_sha256": previous,
                "payload": payload,
            }
            event["event_sha256"] = hashlib.sha256(canonical(event)).hexdigest()
            with self.ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.ledger, 0o600)
            return event

    def verify(self) -> dict[str, Any]:
        previous = ""
        for sequence, event in enumerate(self._events(), 1):
            claimed = str(event.get("event_sha256") or "")
            unsigned = dict(event)
            unsigned.pop("event_sha256", None)
            actual = hashlib.sha256(canonical(unsigned)).hexdigest()
            if event.get("sequence") != sequence or event.get("previous_sha256") != previous or claimed != actual:
                return {"valid": False, "failed_sequence": sequence}
            previous = claimed
        return {"valid": True, "events": len(self._events()), "head_sha256": previous}

    def submit(self, values: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        redactions = 0
        for key, value in values.items():
            if isinstance(value, list):
                cleaned[key] = []
                for item in value:
                    safe, count = redact(str(item))
                    cleaned[key].append(safe)
                    redactions += count
            else:
                safe, count = redact(str(value or ""))
                cleaned[key] = safe
                redactions += count
        created = now()
        candidate_id = "cur_%s" % hashlib.sha256(
            canonical({"agent_id": self.agent_id, "created_at": created, **cleaned})
        ).hexdigest()[:20]
        candidate = {
            "schema": SCHEMA,
            "id": candidate_id,
            "agent_id": self.agent_id,
            "status": "quarantined",
            "created_at": created,
            "redactions": redactions,
            **cleaned,
        }
        atomic_json(self.quarantine / f"{candidate_id}.json", candidate)
        self.append("candidate.quarantined", {"candidate_id": candidate_id, "candidate_sha256": hashlib.sha256(canonical(candidate)).hexdigest(), "redactions": redactions})
        return candidate

    def candidate(self, candidate_id: str) -> tuple[Path, dict[str, Any]]:
        if not re.fullmatch(r"cur_[0-9a-f]{20}", candidate_id):
            raise ValueError("invalid curiosity candidate id")
        path = self.quarantine / f"{candidate_id}.json"
        if not path.is_file():
            raise ValueError("curiosity candidate not found")
        return path, json.loads(path.read_text(encoding="utf-8"))

    def decide(self, candidate_id: str, *, decision: str, actor: str, reason: str, approval_id: str) -> dict[str, Any]:
        if not actor.strip() or not reason.strip() or not approval_id.strip():
            raise ValueError("actor, reason, and external approval id are required")
        path, candidate = self.candidate(candidate_id)
        if candidate.get("status") != "quarantined":
            raise ValueError("candidate has already been decided")
        candidate.update({"status": decision, "decided_at": now(), "decided_by": actor, "decision_reason": reason, "approval_id": approval_id})
        atomic_json(path, candidate)
        if decision == "approved":
            items = "\n".join(f"- {item}" for item in candidate.get("evidence", [])) or "- No evidence supplied."
            provenance = "\n".join(f"- {item}" for item in candidate.get("provenance", [])) or "- No provenance supplied."
            memory = (
                f"# Approved curiosity memory: {candidate_id}\n\n"
                f"- Approved by: {actor}\n- External approval: {approval_id}\n- Reason: {reason}\n"
                f"- Hypothesis: {candidate.get('hypothesis', '')}\n- Confidence: {candidate.get('confidence', '')}\n\n"
                f"## Evidence\n\n{items}\n\n## Provenance\n\n{provenance}\n\n"
                f"## Test and remaining unknowns\n\n{candidate.get('test', '')}\n"
            )
            atomic_text(self.workspace / "memory" / "curiosity-approved" / f"{candidate_id}.md", memory)
        self.append(f"candidate.{decision}", {"candidate_id": candidate_id, "actor": actor, "reason": reason, "approval_id": approval_id})
        return candidate


def abuse_frame(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "event": args.event,
        "comparison": args.comparison,
        "harmed_parties": args.harmed_party,
        "evidence": args.evidence,
        "unknowns": args.unknown,
    }
    redactions = 0
    for key, value in list(values.items()):
        if isinstance(value, list):
            rows = []
            for item in value:
                safe, count = redact(item)
                rows.append(safe)
                redactions += count
            values[key] = rows
        else:
            values[key], count = redact(value or "")
            redactions += count
    asymmetry = bool(args.power_asymmetry or args.responsibility_asymmetry)
    return {
        "schema": "mac.openclaw_abuse_frame.v1",
        "mode": "moral_clarity",
        **values,
        "power_asymmetry": bool(args.power_asymmetry),
        "responsibility_asymmetry": bool(args.responsibility_asymmetry),
        "moral_injury": bool(args.moral_injury),
        "possible_false_equivalence": bool(values["comparison"] and asymmetry),
        "protective_anger": "Name evidenced harm and responsibility plainly; direct anger toward stopping harm and protecting people, never toward dehumanization or unsupported retaliation.",
        "evidence_posture": "Separate observed facts, sourced claims, inferences, counterevidence, and unknowns. Challenge absent provenance and inflated confidence.",
        "redactions": redactions,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("OPENCLAW_STATE_DIR", "~/.openclaw")).expanduser())
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("MAC_OPENCLAW_WORKSPACE", "/sandbox/workspace")))
    parser.add_argument("--agent-id", default=os.environ.get("MAC_OPENCLAW_AGENT_ID", "unknown"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("policy")
    commands.add_parser("verify")
    listed = commands.add_parser("list")
    listed.add_argument("--status", choices=["quarantined", "approved", "rejected"])
    submit = commands.add_parser("submit")
    submit.add_argument("--hypothesis", required=True)
    submit.add_argument("--question", required=True)
    submit.add_argument("--test", required=True)
    submit.add_argument("--evidence", action="append", default=[])
    submit.add_argument("--provenance", action="append", default=[])
    submit.add_argument("--counterevidence", action="append", default=[])
    submit.add_argument("--unknown", action="append", default=[])
    submit.add_argument("--confidence", choices=["low", "medium", "high"], default="low")
    submit.add_argument("--mode", choices=["curiosity", "angry-librarian", "moral-clarity"], default="curiosity")
    for name in ("approve", "reject"):
        decision = commands.add_parser(name)
        decision.add_argument("candidate_id")
        decision.add_argument("--actor", required=True)
        decision.add_argument("--reason", required=True)
        decision.add_argument("--approval-id", required=True)
    abuse = commands.add_parser("abuse-frame")
    abuse.add_argument("--event", required=True)
    abuse.add_argument("--comparison", default="")
    abuse.add_argument("--harmed-party", action="append", default=[])
    abuse.add_argument("--evidence", action="append", default=[])
    abuse.add_argument("--unknown", action="append", default=[])
    abuse.add_argument("--power-asymmetry", action="store_true")
    abuse.add_argument("--responsibility-asymmetry", action="store_true")
    abuse.add_argument("--moral-injury", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "policy":
        print(POLICY.rstrip())
        return 0
    if args.command == "abuse-frame":
        print(json.dumps(abuse_frame(args), indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    store = CuriosityStore(args.state_dir.expanduser(), args.workspace.expanduser(), args.agent_id)
    if args.command == "verify":
        result = store.verify()
    elif args.command == "list":
        result = []
        for path in sorted(store.quarantine.glob("cur_*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            if not args.status or item.get("status") == args.status:
                result.append(item)
    elif args.command == "submit":
        result = store.submit({key: getattr(args, key) for key in ("hypothesis", "question", "test", "evidence", "provenance", "counterevidence", "unknown", "confidence", "mode")})
    elif args.command in {"approve", "reject"}:
        result = store.decide(args.candidate_id, decision="approved" if args.command == "approve" else "rejected", actor=args.actor, reason=args.reason, approval_id=args.approval_id)
    else:  # pragma: no cover
        raise AssertionError(args.command)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"curiosity: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
