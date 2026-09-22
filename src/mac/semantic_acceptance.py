"""Deterministic, content-addressed task acceptance checks.

Structural evidence validation answers whether an executor produced a valid
MAC evidence envelope.  It deliberately cannot answer a task-specific
question such as "is this the expected canary result?".  This module supplies
that second, semantic gate without delegating it to an LLM.

The first verifier is intentionally small: select one value from the signed
executor manifest with a JSON Pointer, validate its declared type, and compare
the SHA-256 of its canonical JSON representation with the expected digest.
The verifier implementation is identified by a digest of its public contract,
so an unknown or drifted implementation fails closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple


ACCEPTANCE_SCHEMA = "mac.acceptance_check.v1"
ACCEPTANCE_RESULT_SCHEMA = "mac.acceptance_result.v1"
VERIFIER_ID = "mac.canonical_json_sha256.v1"
_CANARY_MARKER = "MAC_" + "CANARY_RESULT="
_VERIFIER_CONTRACT = (
    "mac.canonical_json_sha256.v1\n"
    "input=executor_manifest JSON Pointer value\n"
    "encoding=RFC8785-compatible sorted compact JSON UTF-8\n"
    "output={digest:sha256:<lowercase hex>}\n"
)
VERIFIER_DIGEST = "sha256:" + hashlib.sha256(_VERIFIER_CONTRACT.encode("utf-8")).hexdigest()
CANARY_VERIFIER_ID = "mac.canary_result_sha256.v1"
_CANARY_VERIFIER_CONTRACT = (
    "mac.canary_result_sha256.v1\n"
    "input=executor_manifest /operator_result/result string\n"
    "marker=exactly one complete line " + _CANARY_MARKER + "<canonical JSON>\n"
    "output={digest:sha256:<payload bytes lowercase hex>}\n"
)
CANARY_VERIFIER_DIGEST = (
    "sha256:" + hashlib.sha256(_CANARY_VERIFIER_CONTRACT.encode("utf-8")).hexdigest()
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _legacy_expected_digest(metadata: Mapping[str, Any]) -> str:
    candidates: List[Any] = [metadata.get("expected_digest"), metadata.get("expected_sha256")]
    for name in ("canary", "oracle", "acceptance"):
        block = metadata.get(name)
        if isinstance(block, Mapping):
            candidates.extend((block.get("expected_digest"), block.get("expected_sha256")))
    for value in candidates:
        text = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", text):
            return "sha256:" + text
        if _DIGEST_RE.fullmatch(text):
            return text
    return ""


def acceptance_contract(metadata: Any) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Return ``(required, contract)`` and adapt legacy canary digests.

    Existing canary tasks used an expected digest as advisory metadata.  They
    become mandatory semantic checks at read time, without a data rewrite.
    New tasks should write ``metadata.acceptance_check`` directly.
    """
    if not isinstance(metadata, Mapping):
        return False, None
    raw = metadata.get("acceptance_check")
    required = bool(metadata.get("acceptance_required")) or raw is not None
    if isinstance(raw, Mapping):
        return True, dict(raw)
    workload = metadata.get("workload")
    if metadata.get("schema") == "mac.canary_workload.v1" and isinstance(workload, Mapping):
        expected = str(workload.get("expected_result_sha256") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", expected):
            return True, {
                "schema": ACCEPTANCE_SCHEMA,
                "verifier": {
                    "id": CANARY_VERIFIER_ID,
                    "digest": CANARY_VERIFIER_DIGEST,
                },
                "input": {
                    "source": "executor_manifest",
                    "pointer": "/operator_result/result",
                },
                "input_schema": {"type": "string"},
                "output_schema": {
                    "type": "object",
                    "required": ["digest"],
                    "properties": {
                        "digest": {
                            "type": "string",
                            "pattern": "^sha256:[0-9a-f]{64}$",
                        }
                    },
                    "additionalProperties": False,
                },
                "expected_output": {"digest": "sha256:" + expected},
                "migrated_from": "mac.canary_workload.v1",
            }
        return True, None
    if metadata.get("schema") == "mac.canary_workload.v1":
        return True, None
    digest = _legacy_expected_digest(metadata)
    if digest:
        pointer = str(metadata.get("acceptance_result_pointer") or "/operator_result/result")
        return True, {
            "schema": ACCEPTANCE_SCHEMA,
            "verifier": {"id": VERIFIER_ID, "digest": VERIFIER_DIGEST},
            "input": {"source": "executor_manifest", "pointer": pointer},
            "input_schema": {},
            "output_schema": {
                "type": "object",
                "required": ["digest"],
                "properties": {"digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}},
                "additionalProperties": False,
            },
            "expected_output": {"digest": digest},
            "migrated_from": "legacy_expected_digest",
        }
    return required, None


def _pointer(document: Any, pointer: str) -> Tuple[bool, Any]:
    if pointer == "":
        return True, document
    if not pointer.startswith("/"):
        return False, None
    current = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return False, None
    return True, current


def _schema_problems(value: Any, schema: Any, path: str) -> List[str]:
    if not isinstance(schema, Mapping):
        return ["%s schema must be an object" % path]
    problems: List[str] = []
    expected_type = schema.get("type")
    type_checks = {
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected_type is not None:
        check = type_checks.get(str(expected_type))
        if check is None:
            return ["%s schema has unsupported type %s" % (path, expected_type)]
        if not check(value):
            return ["%s must be %s" % (path, expected_type)]
    if "enum" in schema and value not in schema.get("enum", []):
        problems.append("%s is not an allowed value" % path)
    if isinstance(value, str) and schema.get("pattern") is not None:
        try:
            if re.fullmatch(str(schema["pattern"]), value) is None:
                problems.append("%s does not match its pattern" % path)
        except re.error:
            problems.append("%s schema pattern is invalid" % path)
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            problems.append("%s schema.required must be a string list" % path)
            required = []
        for key in required:
            if key not in value:
                problems.append("%s.%s is required" % (path, key))
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            problems.append("%s schema.properties must be an object" % path)
            properties = {}
        for key, child in properties.items():
            if key in value:
                problems.extend(_schema_problems(value[key], child, "%s.%s" % (path, key)))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    problems.append("%s.%s is not allowed" % (path, key))
    return problems


def evaluate_acceptance(metadata: Any, executor_manifest: Any) -> Dict[str, Any]:
    """Evaluate semantic acceptance and return a deterministic signed payload."""
    required, contract = acceptance_contract(metadata)
    base: Dict[str, Any] = {
        "schema": ACCEPTANCE_RESULT_SCHEMA,
        "required": required,
        "status": "not_required" if not required else "fail",
        "problems": [],
    }
    if not required:
        return base
    if contract is None:
        base["problems"] = ["mandatory acceptance_check is absent"]
        return base
    base["contract_digest"] = canonical_digest(contract)
    if contract.get("schema") != ACCEPTANCE_SCHEMA:
        base["problems"] = ["acceptance_check schema mismatch"]
        return base
    verifier = contract.get("verifier")
    if not isinstance(verifier, Mapping):
        base["problems"] = ["acceptance verifier is absent"]
        return base
    base["verifier"] = {"id": verifier.get("id"), "digest": verifier.get("digest")}
    known_verifiers = {
        VERIFIER_ID: VERIFIER_DIGEST,
        CANARY_VERIFIER_ID: CANARY_VERIFIER_DIGEST,
    }
    verifier_id = str(verifier.get("id") or "")
    if known_verifiers.get(verifier_id) != verifier.get("digest"):
        base["problems"] = ["acceptance verifier is unavailable or its version drifted"]
        return base
    input_spec = contract.get("input")
    if not isinstance(input_spec, Mapping) or input_spec.get("source") != "executor_manifest":
        base["problems"] = ["acceptance input source must be executor_manifest"]
        return base
    pointer = str(input_spec.get("pointer") or "")
    present, value = _pointer(executor_manifest, pointer)
    if not present:
        base["problems"] = ["acceptance input is absent at %s" % pointer]
        return base
    problems = _schema_problems(value, contract.get("input_schema"), "input")
    if verifier_id == CANARY_VERIFIER_ID:
        marker_lines = (
            [line for line in value.splitlines() if line.startswith(_CANARY_MARKER)]
            if isinstance(value, str)
            else []
        )
        if len(marker_lines) != 1:
            problems.append("canary evidence must contain exactly one result marker line")
            actual_output = {"digest": ""}
        else:
            payload_text = marker_lines[0][len(_CANARY_MARKER) :]
            try:
                payload = json.loads(payload_text)
                canonical_payload = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            except (TypeError, ValueError):
                canonical_payload = ""
                problems.append("canary result payload is not valid JSON")
            if canonical_payload and payload_text != canonical_payload:
                problems.append("canary result payload is not canonical JSON")
            actual_output = {
                "digest": "sha256:" + hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
                if canonical_payload
                else ""
            }
    else:
        actual_output = {"digest": canonical_digest(value)}
    problems.extend(_schema_problems(actual_output, contract.get("output_schema"), "output"))
    expected = contract.get("expected_output")
    problems.extend(_schema_problems(expected, contract.get("output_schema"), "expected_output"))
    base["input_digest"] = canonical_digest(value)
    base["actual_output"] = actual_output
    base["expected_output"] = expected
    if actual_output != expected:
        problems.append("acceptance output does not equal expected_output")
    base["problems"] = problems
    base["status"] = "pass" if not problems else "fail"
    return base


def acceptance_result_problems(expected: Mapping[str, Any], recorded: Any) -> List[str]:
    """Validate that signed verdict acceptance is the deterministic replay."""
    if not isinstance(recorded, Mapping):
        return ["signed verdict is missing semantic acceptance result"]
    if dict(recorded) != dict(expected):
        return ["signed verdict semantic acceptance result does not match deterministic replay"]
    if expected.get("required") and expected.get("status") != "pass":
        return ["mandatory semantic acceptance did not pass"]
    return []
