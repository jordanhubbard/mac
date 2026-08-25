"""Deterministic policy boundary for outbound coding-agent prompts.

The policy is derived from the pinned Prompt Master material vendored under
``mac/third_party/prompt-master``.  That material is inert data: this module is
the only executable interpretation, and it never calls a model or the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

POLICY_VERSION = "mac-prompt-master-v1"
UPSTREAM_COMMIT = "d15eabbe5d2122eedc060bae8a771381e9873d1b"
MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = MAX_INPUT_BYTES + 4096
_MARKER = "<!-- mac.prompt_master.v1 -->\n"
_KNOWN_TARGETS = {"claude", "codex", "cursor", "opencode", "pi", "acp", "api"}
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)\b((?:[A-Z][A-Z0-9_]*_)?(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY))"
    r"(\s*[=:]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


class PromptPolicyError(ValueError):
    """The prompt cannot safely cross the coding-agent boundary."""


@dataclass(frozen=True)
class CompiledPrompt:
    text: str
    evidence: Dict[str, Any]


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact_secrets(text: str) -> tuple[str, bool]:
    changed = False

    def assignment(match: re.Match[str]) -> str:
        nonlocal changed
        value = match.group(3)
        if value in {"<redacted>", "${%s}" % match.group(1), "$" + match.group(1)}:
            return match.group(0)
        changed = True
        return match.group(1) + match.group(2) + "<redacted>"

    text = _SECRET_ASSIGNMENT.sub(assignment, text)
    rewritten, count = _BEARER.subn(r"\1<redacted>", text)
    return rewritten, changed or bool(count)


def compile_prompt(
    prompt: str,
    *,
    target: str,
    model: str = "",
    prompt_kind: str = "task",
    task_id: str = "",
    attempt: Optional[Any] = None,
    agent_id: str = "",
    route_fingerprint: str = "",
    command_id: str = "",
) -> CompiledPrompt:
    """Validate and deterministically rewrite one final outbound prompt.

    Callers must invoke this after route selection and before staging prompt
    bytes in a file, argv, ACP request, or HTTP body.
    """

    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptPolicyError("coding-agent prompt must be non-empty")
    source_bytes = len(prompt.encode("utf-8"))
    if source_bytes > MAX_INPUT_BYTES:
        raise PromptPolicyError("coding-agent prompt exceeds %d-byte input limit" % MAX_INPUT_BYTES)
    normalized_target = str(target or "").strip().lower()
    profile = normalized_target if normalized_target in _KNOWN_TARGETS else "universal"

    if prompt.startswith(_MARKER):
        rewritten = prompt
        changed_dimensions = []
        source_digest = _digest(prompt)
    else:
        redacted, secrets_changed = _redact_secrets(prompt)
        guidance = {
            "constraints": "Higher-authority MAC policy and explicit safe user constraints win.",
            "evidence": "Produce the requested deterministic evidence; do not expose chain-of-thought.",
            "profile": profile,
            "stop": "Stop on a policy, scope, safety, or missing-critical-context failure; never wait for chat.",
            "target_model": str(model or ""),
        }
        header = (
            _MARKER
            + json.dumps(guidance, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\n\n"
        )
        rewritten = header + redacted
        changed_dimensions = ["policy_envelope"]
        if secrets_changed:
            changed_dimensions.append("secret_redaction")
        source_digest = _digest(prompt)

    if len(rewritten.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise PromptPolicyError(
            "rewritten coding-agent prompt exceeds %d-byte output limit" % MAX_OUTPUT_BYTES
        )
    evidence: Dict[str, Any] = {
        "schema": "mac.prompt_rewrite.v1",
        "policy_version": POLICY_VERSION,
        "policy_commit": UPSTREAM_COMMIT,
        "source_sha256": source_digest,
        "rewritten_sha256": _digest(rewritten),
        "target_profile": profile,
        "model": str(model or "") or None,
        "prompt_kind": str(prompt_kind or "task"),
        "task_id": str(task_id or "") or None,
        "attempt": attempt,
        "agent_id": str(agent_id or "") or None,
        "route_fingerprint": str(route_fingerprint or "") or None,
        "command_id": str(command_id or "") or None,
        "diagnostics": [],
        "changed_dimensions": changed_dimensions,
    }
    return CompiledPrompt(rewritten, evidence)


def compile_for_environment(
    prompt: str, *, target: str, model: str = "", prompt_kind: str = "task"
) -> CompiledPrompt:
    return compile_prompt(
        prompt,
        target=target,
        model=model,
        prompt_kind=prompt_kind,
        task_id=os.environ.get("MAC_TASK_ID", ""),
        attempt=os.environ.get("MAC_TASK_ATTEMPT") or None,
        agent_id=os.environ.get("MAC_AGENT_ID", ""),
        route_fingerprint=os.environ.get("MAC_ROUTE_FINGERPRINT", ""),
        command_id=os.environ.get("MAC_COMMAND_ID", ""),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a coding-agent prompt from stdin")
    parser.add_argument("--target", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--kind", default="task")
    parser.add_argument("--evidence-file", default="")
    args = parser.parse_args(argv)
    try:
        result = compile_for_environment(
            sys.stdin.read(), target=args.target, model=args.model, prompt_kind=args.kind
        )
    except PromptPolicyError as exc:
        sys.stderr.write("prompt policy rejected dispatch: %s\n" % exc)
        return 2
    if args.evidence_file:
        with open(args.evidence_file, "w", encoding="utf-8") as handle:
            json.dump(result.evidence, handle, indent=2, sort_keys=True)
            handle.write("\n")
    sys.stdout.write(result.text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
