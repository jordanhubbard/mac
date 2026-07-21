"""Pure contracts for versioned fleet directives.

Directives are intentionally data, not programs.  This module is free of
store, network, clock, and worker dependencies so the hub, CLI, and tests all
evaluate the same bounded condition language and produce the same digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from mac.models import JsonDict, ValidationError


DIRECTIVE_SCHEMA = "mac.directive.v1"
DIRECTIVE_SNAPSHOT_SCHEMA = "mac.directive.snapshot.v1"
DIRECTIVE_ACTIVATION_SCHEMA = "mac.directive.activation.v1"
DIRECTIVE_MAX_CONDITION_DEPTH = 16
DIRECTIVE_MAX_CONDITION_NODES = 256
DIRECTIVE_MAX_TEMPLATE_LENGTH = 16_384

_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,15}$")
_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){0,15}$")
_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_.-]{0,127})\}")
_OPERATORS = {
    "all",
    "any",
    "not",
    "eq",
    "ne",
    "in",
    "contains",
    "starts_with",
    "ends_with",
    "exists",
}
_FACT_ROOTS = {"fleet", "project", "repository", "agent"}
_VARIABLE_TYPES = {"string", "boolean", "integer", "number", "list", "object"}
_SECRET_KEY_RE = re.compile(
    r"(^|[._-])(token|secret|password|passwd|private[_-]?key|credential|api[_-]?key)($|[._-])",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:https?|ssh)://[^\s/:]+:[^\s/@]+@|Bearer\s+[A-Za-z0-9._~+/-]{12,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DirectiveDocument:
    schema: str
    name: str
    description: str
    scope: str
    when: Optional[JsonDict]
    variables: JsonDict
    policy: JsonDict
    macro: Optional[JsonDict]
    raw: JsonDict
    digest: str

    def to_dict(self) -> JsonDict:
        return _json_copy(self.raw)


@dataclass(frozen=True)
class DirectiveEvaluation:
    matched: bool
    blocked: bool
    reason: Optional[str]
    variables: JsonDict
    policy: JsonDict
    macro: Optional[JsonDict]

    def to_dict(self) -> JsonDict:
        return {
            "matched": self.matched,
            "blocked": self.blocked,
            "reason": self.reason,
            "variables": _json_copy(self.variables),
            "set": _json_copy(self.policy),
            "macro": _json_copy(self.macro) if self.macro is not None else None,
        }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_directive_document(raw: Mapping[str, Any]) -> DirectiveDocument:
    if not isinstance(raw, Mapping):
        raise ValidationError("directive document must be an object")
    candidate = _json_copy(dict(raw))
    allowed = {
        "schema",
        "name",
        "description",
        "scope",
        "when",
        "variables",
        "set",
        "macro",
    }
    unknown = sorted(set(candidate) - allowed)
    if unknown:
        raise ValidationError("unsupported directive fields: %s" % ", ".join(unknown))
    if candidate.get("schema") != DIRECTIVE_SCHEMA:
        raise ValidationError("directive schema must be %s" % DIRECTIVE_SCHEMA)
    name = _required_name(candidate.get("name"), "directive name")
    description = _required_text(candidate.get("description"), "directive description", 4000)
    if candidate.get("scope") != "fleet":
        raise ValidationError("directive scope must be fleet")

    when = candidate.get("when")
    if when is not None:
        _validate_condition(when)
    variables_value = candidate.get("variables")
    variables = {} if variables_value is None else variables_value
    if not isinstance(variables, Mapping):
        raise ValidationError("directive variables must be an object")
    normalized_variables: JsonDict = {}
    for variable_name, definition in variables.items():
        variable = _required_name(variable_name, "directive variable")
        if not isinstance(definition, Mapping):
            raise ValidationError("directive variable %s must be an object" % variable)
        unknown_definition = set(definition) - {"type", "binding", "required", "default"}
        if unknown_definition:
            raise ValidationError(
                "unsupported fields for directive variable %s: %s"
                % (variable, ", ".join(sorted(unknown_definition)))
            )
        value_type = str(definition.get("type") or "").strip()
        if value_type not in _VARIABLE_TYPES:
            raise ValidationError("unsupported type for directive variable %s" % variable)
        binding = _required_path(definition.get("binding"), "directive variable binding")
        normalized: JsonDict = {
            "type": value_type,
            "binding": binding,
            "required": bool(definition.get("required", False)),
        }
        if "default" in definition:
            normalized["default"] = _json_copy(definition["default"])
            _validate_variable_type(variable, normalized["default"], value_type)
        normalized_variables[variable] = normalized

    policy_value = candidate.get("set")
    policy = {} if policy_value is None else policy_value
    if not isinstance(policy, Mapping):
        raise ValidationError("directive set must be an object")
    normalized_policy: JsonDict = {}
    for key, value in policy.items():
        policy_key = _required_path(key, "directive policy key")
        normalized_policy[policy_key] = _json_copy(value)

    macro = candidate.get("macro")
    normalized_macro: Optional[JsonDict] = None
    if macro is not None:
        if not isinstance(macro, Mapping):
            raise ValidationError("directive macro must be an object")
        unknown_macro = set(macro) - {"workflow", "version", "inputs", "effects"}
        if unknown_macro:
            raise ValidationError(
                "unsupported directive macro fields: %s" % ", ".join(sorted(unknown_macro))
            )
        workflow = _required_name(macro.get("workflow"), "directive macro workflow")
        try:
            workflow_version = int(macro.get("version"))
        except (TypeError, ValueError):
            raise ValidationError("directive macro version must be a positive integer")
        if workflow_version < 1:
            raise ValidationError("directive macro version must be a positive integer")
        inputs_value = macro.get("inputs")
        effects_value = macro.get("effects")
        inputs = {} if inputs_value is None else inputs_value
        effects = {} if effects_value is None else effects_value
        if not isinstance(inputs, Mapping) or not isinstance(effects, Mapping):
            raise ValidationError("directive macro inputs and effects must be objects")
        if not effects:
            raise ValidationError("directive macro must declare effects")
        unknown_effects = set(effects) - {"reads", "writes", "exclusive", "external"}
        if unknown_effects:
            raise ValidationError(
                "unsupported directive macro effects: %s" % ", ".join(sorted(unknown_effects))
            )
        for kind, values in effects.items():
            if not isinstance(values, list) or not values:
                raise ValidationError("directive macro effect %s must be a non-empty list" % kind)
            for item in values:
                marked = isinstance(item, Mapping) and set(item) in (
                    {"template"},
                    {"fact"},
                    {"var"},
                )
                if not marked and (not isinstance(item, str) or not item.strip()):
                    raise ValidationError(
                        "directive macro effects must be non-empty strings or marked substitutions"
                    )
        normalized_macro = {
            "workflow": workflow,
            "version": workflow_version,
            "inputs": _json_copy(dict(inputs)),
            "effects": _json_copy(dict(effects)),
        }

    if not normalized_policy and normalized_macro is None:
        raise ValidationError("directive must set policy values or name a workflow macro")

    normalized: JsonDict = {
        "schema": DIRECTIVE_SCHEMA,
        "name": name,
        "description": description,
        "scope": "fleet",
    }
    if when is not None:
        normalized["when"] = _json_copy(when)
    if normalized_variables:
        normalized["variables"] = normalized_variables
    if normalized_policy:
        normalized["set"] = normalized_policy
    if normalized_macro is not None:
        normalized["macro"] = normalized_macro
    _reject_secret_material(normalized)
    _validate_templates(normalized_macro, normalized_variables)
    return DirectiveDocument(
        schema=DIRECTIVE_SCHEMA,
        name=name,
        description=description,
        scope="fleet",
        when=_json_copy(when) if when is not None else None,
        variables=normalized_variables,
        policy=normalized_policy,
        macro=normalized_macro,
        raw=normalized,
        digest=canonical_digest(normalized),
    )


def evaluate_directive(
    document: DirectiveDocument,
    *,
    facts: Mapping[str, Any],
    bindings: Sequence[Mapping[str, Any]] = (),
) -> DirectiveEvaluation:
    matched = True if document.when is None else evaluate_condition(document.when, facts)
    if not matched:
        return DirectiveEvaluation(False, False, None, {}, {}, None)
    resolved, missing = resolve_variables(document.variables, bindings)
    if missing:
        return DirectiveEvaluation(
            True,
            True,
            "missing required directive bindings: %s" % ", ".join(sorted(missing)),
            resolved,
            {},
            None,
        )
    macro = render_marked_value(document.macro, facts=facts, variables=resolved)
    return DirectiveEvaluation(
        True,
        False,
        None,
        resolved,
        _json_copy(document.policy),
        macro,
    )


def evaluate_condition(expression: Any, facts: Mapping[str, Any]) -> bool:
    counter = [0]

    def visit(node: Any, depth: int) -> bool:
        counter[0] += 1
        if counter[0] > DIRECTIVE_MAX_CONDITION_NODES:
            raise ValidationError("directive condition is too large")
        if depth > DIRECTIVE_MAX_CONDITION_DEPTH:
            raise ValidationError("directive condition is too deeply nested")
        if not isinstance(node, Mapping) or len(node) != 1:
            raise ValidationError("directive condition nodes require exactly one operator")
        operator, arguments = next(iter(node.items()))
        if operator not in _OPERATORS:
            raise ValidationError("unsupported directive condition operator: %s" % operator)
        if operator in {"all", "any"}:
            if not isinstance(arguments, list) or not arguments:
                raise ValidationError("directive %s requires a non-empty list" % operator)
            values = [visit(item, depth + 1) for item in arguments]
            return all(values) if operator == "all" else any(values)
        if operator == "not":
            return not visit(arguments, depth + 1)
        if operator == "exists":
            return _operand(arguments, facts, allow_missing=True) is not _MISSING
        if not isinstance(arguments, list) or len(arguments) != 2:
            raise ValidationError("directive %s requires exactly two operands" % operator)
        left = _operand(arguments[0], facts)
        right = _operand(arguments[1], facts)
        if left is _MISSING or right is _MISSING:
            return False
        if operator == "eq":
            return left == right
        if operator == "ne":
            return left != right
        if operator == "in":
            return left in right if isinstance(right, (list, tuple, set, str, dict)) else False
        if operator == "contains":
            return right in left if isinstance(left, (list, tuple, set, str, dict)) else False
        if operator == "starts_with":
            return isinstance(left, str) and isinstance(right, str) and left.startswith(right)
        if operator == "ends_with":
            return isinstance(left, str) and isinstance(right, str) and left.endswith(right)
        raise AssertionError(operator)

    return visit(expression, 1)


def resolve_variables(
    definitions: Mapping[str, Any],
    bindings: Sequence[Mapping[str, Any]],
) -> Tuple[JsonDict, Tuple[str, ...]]:
    """Resolve ordered high-to-low precedence binding layers."""

    resolved: JsonDict = {}
    missing = []
    for name, definition in definitions.items():
        path = str(definition["binding"])
        value: Any = _MISSING
        for layer in bindings:
            candidate = _path_value(layer, path)
            if candidate is not _MISSING:
                value = candidate
                break
        if value is _MISSING and "default" in definition:
            value = definition["default"]
        if value is _MISSING:
            if definition.get("required"):
                missing.append(name)
            continue
        _validate_variable_type(name, value, str(definition["type"]))
        resolved[name] = _json_copy(value)
    return resolved, tuple(missing)


def render_marked_value(
    value: Any,
    *,
    facts: Mapping[str, Any],
    variables: Mapping[str, Any],
) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        if set(value) == {"fact"}:
            return _json_copy(_fact_value(value["fact"], facts))
        if set(value) == {"var"}:
            name = str(value["var"])
            if name not in variables:
                raise ValidationError("unresolved directive variable: %s" % name)
            return _json_copy(variables[name])
        if set(value) == {"template"}:
            template = value["template"]
            if not isinstance(template, str):
                raise ValidationError("directive template must be a string")
            return _render_template(template, facts=facts, variables=variables)
        return {
            str(key): render_marked_value(item, facts=facts, variables=variables)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [render_marked_value(item, facts=facts, variables=variables) for item in value]
    return _json_copy(value)


def condition_overlap(left: Any, right: Any) -> str:
    """Return overlap, disjoint, or unknown using a deliberately sound proof."""

    if left is None or right is None:
        return "overlap"
    if canonical_digest(left) == canonical_digest(right):
        return "overlap"
    left_eq = _conjunctive_equalities(left)
    right_eq = _conjunctive_equalities(right)
    if left_eq is None or right_eq is None:
        return "unknown"
    for path in set(left_eq) & set(right_eq):
        if left_eq[path] != right_eq[path]:
            return "disjoint"
    return "overlap"


def _conjunctive_equalities(expression: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(expression, Mapping) or len(expression) != 1:
        return None
    operator, arguments = next(iter(expression.items()))
    nodes = arguments if operator == "all" else [expression]
    if not isinstance(nodes, list):
        return None
    result: Dict[str, Any] = {}
    for node in nodes:
        if not isinstance(node, Mapping) or set(node) != {"eq"}:
            return None
        operands = node["eq"]
        if not isinstance(operands, list) or len(operands) != 2:
            return None
        fact_operand, literal_operand = operands
        if not isinstance(fact_operand, Mapping) or set(fact_operand) != {"fact"}:
            fact_operand, literal_operand = literal_operand, fact_operand
        if not isinstance(fact_operand, Mapping) or set(fact_operand) != {"fact"}:
            return None
        if not isinstance(literal_operand, Mapping) or set(literal_operand) != {"literal"}:
            return None
        result[str(fact_operand["fact"])] = _json_copy(literal_operand["literal"])
    return result


class _Missing:
    pass


_MISSING = _Missing()


def _operand(
    value: Any,
    facts: Mapping[str, Any],
    *,
    allow_missing: bool = False,
) -> Any:
    if not isinstance(value, Mapping) or len(value) != 1:
        raise ValidationError("directive operands must be {fact: path} or {literal: value}")
    if "literal" in value:
        return value["literal"]
    if "fact" in value:
        try:
            return _fact_value(value["fact"], facts)
        except ValidationError:
            return _MISSING
    raise ValidationError("directive operands must be {fact: path} or {literal: value}")


def _fact_value(path: Any, facts: Mapping[str, Any]) -> Any:
    normalized = _required_path(path, "directive fact path")
    if normalized.split(".", 1)[0] not in _FACT_ROOTS:
        raise ValidationError("unsupported directive fact root: %s" % normalized)
    value = _path_value(facts, normalized)
    if value is _MISSING:
        raise ValidationError("directive fact is unavailable: %s" % normalized)
    return value


def _path_value(root: Mapping[str, Any], path: str) -> Any:
    current: Any = root
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _render_template(template: str, *, facts: Mapping[str, Any], variables: Mapping[str, Any]) -> str:
    if len(template) > DIRECTIVE_MAX_TEMPLATE_LENGTH:
        raise ValidationError("directive template is too long")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            value = variables[name]
        elif name.split(".", 1)[0] in _FACT_ROOTS:
            value = _fact_value(name, facts)
        else:
            raise ValidationError("unresolved directive template value: %s" % name)
        if isinstance(value, (dict, list)):
            return canonical_json(value)
        if value is None:
            return ""
        return str(value)

    rendered = _TEMPLATE_RE.sub(replace, template)
    if "${" in rendered:
        raise ValidationError("unresolved directive template expression")
    return rendered


def _validate_condition(expression: Any) -> None:
    # Evaluation validates every node and operand even though no facts exist.
    counter = [0]

    def visit(node: Any, depth: int) -> None:
        counter[0] += 1
        if counter[0] > DIRECTIVE_MAX_CONDITION_NODES or depth > DIRECTIVE_MAX_CONDITION_DEPTH:
            raise ValidationError("directive condition exceeds complexity limits")
        if not isinstance(node, Mapping) or len(node) != 1:
            raise ValidationError("directive condition nodes require exactly one operator")
        operator, arguments = next(iter(node.items()))
        if operator not in _OPERATORS:
            raise ValidationError("unsupported directive condition operator: %s" % operator)
        if operator in {"all", "any"}:
            if not isinstance(arguments, list) or not arguments:
                raise ValidationError("directive %s requires a non-empty list" % operator)
            for item in arguments:
                visit(item, depth + 1)
            return
        if operator == "not":
            visit(arguments, depth + 1)
            return
        operands = [arguments] if operator == "exists" else arguments
        if not isinstance(operands, list) or len(operands) != (1 if operator == "exists" else 2):
            raise ValidationError("directive %s has invalid operands" % operator)
        for operand in operands:
            if not isinstance(operand, Mapping) or len(operand) != 1:
                raise ValidationError("directive operands must be marked fact or literal values")
            marker = next(iter(operand))
            if marker not in {"fact", "literal"}:
                raise ValidationError("directive operands must be marked fact or literal values")
            if marker == "fact":
                path = _required_path(operand[marker], "directive fact path")
                if path.split(".", 1)[0] not in _FACT_ROOTS:
                    raise ValidationError("unsupported directive fact root: %s" % path)

    visit(expression, 1)


def _validate_templates(macro: Optional[Mapping[str, Any]], variables: Mapping[str, Any]) -> None:
    if macro is None:
        return

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if set(value) == {"template"}:
                if not isinstance(value["template"], str):
                    raise ValidationError("directive template must be a string")
                for token in _TEMPLATE_RE.findall(value["template"]):
                    if token not in variables and token.split(".", 1)[0] not in _FACT_ROOTS:
                        raise ValidationError("unknown directive template value: %s" % token)
                return
            if set(value) == {"fact"}:
                path = _required_path(value["fact"], "directive fact path")
                if path.split(".", 1)[0] not in _FACT_ROOTS:
                    raise ValidationError("unsupported directive fact root: %s" % path)
                return
            if set(value) == {"var"}:
                if value["var"] not in variables:
                    raise ValidationError("unknown directive variable: %s" % value["var"])
                return
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(macro.get("inputs"))
    visit(macro.get("effects"))


def _validate_variable_type(name: str, value: Any, value_type: str) -> None:
    valid = {
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "list": isinstance(value, list),
        "object": isinstance(value, Mapping),
    }[value_type]
    if not valid:
        raise ValidationError("directive variable %s is not a %s" % (name, value_type))


def _reject_secret_material(value: Any, path: str = "directive") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                raise ValidationError("directive documents cannot contain credential material (%s.%s)" % (path, key_text))
            _reject_secret_material(item, "%s.%s" % (path, key_text))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_material(item, "%s[%d]" % (path, index))
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise ValidationError("directive documents cannot contain credential material (%s)" % path)


def _required_name(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _NAME_RE.match(text):
        raise ValidationError("%s is invalid" % label)
    return text


def _required_path(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _PATH_RE.match(text):
        raise ValidationError("%s is invalid" % label)
    return text


def _required_text(value: Any, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError("%s is required" % label)
    if len(text) > maximum:
        raise ValidationError("%s is too long" % label)
    return text


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise ValidationError("directive values must be finite JSON data")
