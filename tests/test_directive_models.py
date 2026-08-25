from __future__ import annotations

import pytest

from mac.directive_models import (
    condition_overlap,
    evaluate_condition,
    evaluate_directive,
    parse_directive_document,
    render_marked_value,
    resolve_variables,
)
from mac.models import ValidationError


def _document(**patch):
    raw = {
        "schema": "mac.directive.v1",
        "name": "build.bazel-first",
        "description": "Use the registered Bazel migration workflow for Make repositories.",
        "scope": "fleet",
        "when": {
            "eq": [
                {"fact": "repository.metadata.build_system"},
                {"literal": "make"},
            ]
        },
        "variables": {
            "primary_target": {
                "type": "string",
                "binding": "build.primary_target",
                "required": True,
            }
        },
        "set": {"build.bazel.required": True},
        "macro": {
            "workflow": "build-system.make-to-bazel",
            "version": 1,
            "inputs": {
                "repository_id": {"fact": "repository.id"},
                "primary_target": {"template": "${primary_target}"},
            },
            "effects": {"exclusive": [{"template": "repository:${repository.id}:build-system"}]},
        },
    }
    raw.update(patch)
    return raw


def test_typed_condition_binding_precedence_and_marked_substitution() -> None:
    document = parse_directive_document(_document())
    evaluation = evaluate_directive(
        document,
        facts={
            "fleet": {"name": "mac"},
            "project": {"name": "demo"},
            "repository": {
                "id": "repo_demo",
                "metadata": {"build_system": "make"},
            },
            "agent": {},
        },
        bindings=[
            {"build": {"primary_target": "//app:repo-specific"}},
            {"build": {"primary_target": "//app:fleet-default"}},
        ],
    )

    assert evaluation.matched is True
    assert evaluation.blocked is False
    assert evaluation.variables == {"primary_target": "//app:repo-specific"}
    assert evaluation.macro == {
        "workflow": "build-system.make-to-bazel",
        "version": 1,
        "inputs": {
            "repository_id": "repo_demo",
            "primary_target": "//app:repo-specific",
        },
        "effects": {"exclusive": ["repository:repo_demo:build-system"]},
    }


def test_missing_required_binding_blocks_only_after_condition_matches() -> None:
    document = parse_directive_document(_document())
    facts = {
        "fleet": {},
        "project": {},
        "repository": {"id": "repo", "metadata": {"build_system": "make"}},
        "agent": {},
    }
    matched = evaluate_directive(document, facts=facts)
    assert matched.blocked is True
    assert "primary_target" in str(matched.reason)

    facts["repository"]["metadata"]["build_system"] = "bazel"
    unmatched = evaluate_directive(document, facts=facts)
    assert unmatched.matched is False
    assert unmatched.blocked is False


def test_overlap_is_sound_and_unknown_fails_closed() -> None:
    make = {"eq": [{"fact": "repository.metadata.build_system"}, {"literal": "make"}]}
    bazel = {"eq": [{"fact": "repository.metadata.build_system"}, {"literal": "bazel"}]}
    prefix = {
        "starts_with": [
            {"fact": "repository.name"},
            {"literal": "lib"},
        ]
    }
    assert condition_overlap(make, bazel) == "disjoint"
    assert condition_overlap(make, make) == "overlap"
    assert condition_overlap(make, prefix) == "unknown"


@pytest.mark.parametrize(
    "patch",
    [
        {"set": {"credential.token": "redacted"}},
        {
            "macro": {
                "workflow": "build-system.make-to-bazel",
                "version": 1,
                "inputs": {"value": {"template": "Bearer abcdefghijklmnop"}},
                "effects": {"exclusive": ["repository:demo"]},
            }
        },
    ],
)
def test_directive_documents_reject_secret_material(patch) -> None:
    with pytest.raises(ValidationError, match="credential material"):
        parse_directive_document(_document(**patch))


def test_unmarked_strings_are_never_interpolated() -> None:
    raw = _document()
    raw["macro"]["inputs"]["literal_text"] = "${primary_target}"
    document = parse_directive_document(raw)
    evaluation = evaluate_directive(
        document,
        facts={
            "fleet": {},
            "project": {},
            "repository": {"id": "repo", "metadata": {"build_system": "make"}},
            "agent": {},
        },
        bindings=[{"build": {"primary_target": "//app:all"}}],
    )
    assert evaluation.macro["inputs"]["literal_text"] == "${primary_target}"


def test_missing_fact_does_not_match_comparison_and_exists_can_test_absence() -> None:
    document = parse_directive_document(_document())
    facts = {"fleet": {}, "project": {}, "repository": {"id": "repo"}, "agent": {}}
    comparison = evaluate_directive(document, facts=facts)
    assert comparison.matched is False
    assert comparison.blocked is False

    exists_document = parse_directive_document(
        _document(
            when={"exists": {"fact": "repository.metadata.build_system"}},
            variables={},
            macro=None,
        )
    )
    evaluation = evaluate_directive(exists_document, facts=facts)
    assert evaluation.matched is False
    assert evaluation.blocked is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda _raw: [], "must be an object"),
        (lambda raw: {**raw, "unknown": True}, "unsupported directive fields"),
        (lambda raw: {**raw, "schema": "other"}, "directive schema"),
        (lambda raw: {**raw, "name": "Bad Name"}, "directive name"),
        (lambda raw: {**raw, "description": ""}, "description is required"),
        (lambda raw: {**raw, "description": "x" * 4001}, "description is too long"),
        (lambda raw: {**raw, "scope": "repository"}, "scope must be fleet"),
        (lambda raw: {**raw, "variables": ["bad"]}, "variables must be an object"),
        (
            lambda raw: {**raw, "variables": {"target": "bad"}},
            "variable target must be an object",
        ),
        (
            lambda raw: {
                **raw,
                "variables": {
                    "target": {"type": "string", "binding": "build.target", "extra": True}
                },
            },
            "unsupported fields for directive variable",
        ),
        (
            lambda raw: {
                **raw,
                "variables": {"target": {"type": "path", "binding": "build.target"}},
            },
            "unsupported type",
        ),
        (
            lambda raw: {
                **raw,
                "variables": {"target": {"type": "string", "binding": "bad path"}},
            },
            "binding is invalid",
        ),
        (
            lambda raw: {
                **raw,
                "variables": {
                    "target": {"type": "integer", "binding": "build.target", "default": True}
                },
            },
            "is not a integer",
        ),
        (lambda raw: {**raw, "set": ["bad"], "macro": None}, "set must be an object"),
        (lambda raw: {**raw, "set": {"bad path": True}, "macro": None}, "policy key is invalid"),
        (lambda raw: {**raw, "macro": ["bad"]}, "macro must be an object"),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "unknown": True}},
            "unsupported directive macro fields",
        ),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "workflow": "Bad Name"}},
            "macro workflow is invalid",
        ),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "version": "bad"}},
            "version must be a positive integer",
        ),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "version": 0}},
            "version must be a positive integer",
        ),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "inputs": []}},
            "inputs and effects must be objects",
        ),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "effects": {}}},
            "must declare effects",
        ),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "effects": {"exec": ["x"]}}},
            "unsupported directive macro effects",
        ),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "effects": {"writes": []}}},
            "effect writes must be a non-empty list",
        ),
        (
            lambda raw: {**raw, "macro": {**raw["macro"], "effects": {"writes": [None]}}},
            "effects must be non-empty strings",
        ),
        (
            lambda raw: {**raw, "set": {}, "macro": None},
            "must set policy values or name a workflow",
        ),
    ],
)
def test_document_schema_rejects_every_unbounded_or_malformed_surface(mutate, message) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_directive_document(mutate(_document()))


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ({"all": [{"eq": [{"literal": 1}, {"literal": 1}]}]}, True),
        (
            {"any": [{"eq": [{"literal": 1}, {"literal": 2}]}, {"exists": {"fact": "fleet.name"}}]},
            True,
        ),
        ({"not": {"eq": [{"literal": "a"}, {"literal": "b"}]}}, True),
        ({"ne": [{"literal": 1}, {"literal": 2}]}, True),
        ({"in": [{"literal": "a"}, {"literal": ["a", "b"]}]}, True),
        ({"in": [{"literal": "a"}, {"literal": 1}]}, False),
        ({"contains": [{"literal": {"a": 1}}, {"literal": "a"}]}, True),
        ({"contains": [{"literal": 1}, {"literal": 1}]}, False),
        ({"starts_with": [{"literal": "abc"}, {"literal": "a"}]}, True),
        ({"starts_with": [{"literal": 1}, {"literal": "a"}]}, False),
        ({"ends_with": [{"literal": "abc"}, {"literal": "bc"}]}, True),
        ({"ends_with": [{"literal": "abc"}, {"literal": 1}]}, False),
    ],
)
def test_condition_operator_truth_table(expression, expected) -> None:
    facts = {"fleet": {"name": "mac"}, "project": {}, "repository": {}, "agent": {}}
    assert evaluate_condition(expression, facts) is expected


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ([], "exactly one operator"),
        ({"shell": []}, "unsupported directive condition operator"),
        ({"all": []}, "requires a non-empty list"),
        ({"eq": [{"literal": 1}]}, "requires exactly two operands"),
        ({"eq": [1, {"literal": 1}]}, "operands must be"),
        ({"eq": [{"unknown": 1}, {"literal": 1}]}, "operands must be"),
    ],
)
def test_condition_runtime_rejects_malformed_nodes(expression, message) -> None:
    with pytest.raises(ValidationError, match=message):
        evaluate_condition(expression, {"fleet": {}, "project": {}, "repository": {}, "agent": {}})


def test_condition_runtime_enforces_size_and_depth_bounds() -> None:
    too_deep = {"eq": [{"literal": 1}, {"literal": 1}]}
    for _index in range(17):
        too_deep = {"not": too_deep}
    with pytest.raises(ValidationError, match="deeply nested"):
        evaluate_condition(too_deep, {})

    too_large = {"all": [{"eq": [{"literal": index}, {"literal": index}]} for index in range(257)]}
    with pytest.raises(ValidationError, match="too large"):
        evaluate_condition(too_large, {})


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ([], "exactly one operator"),
        ({"shell": []}, "unsupported directive condition operator"),
        ({"all": []}, "requires a non-empty list"),
        ({"not": {"eq": [{"literal": 1}, {"literal": 1}]}}, None),
        ({"eq": [{"literal": 1}]}, "invalid operands"),
        ({"eq": [1, {"literal": 1}]}, "marked fact or literal"),
        ({"eq": [{"unknown": 1}, {"literal": 1}]}, "marked fact or literal"),
        (
            {"eq": [{"fact": "unknown.value"}, {"literal": 1}]},
            "unsupported directive fact root",
        ),
        ({"exists": [{"fact": "fleet.name"}]}, "marked fact or literal"),
    ],
)
def test_document_condition_validator_covers_every_grammar_boundary(condition, message) -> None:
    if message is None:
        assert parse_directive_document(_document(when=condition)).when == condition
        return
    with pytest.raises(ValidationError, match=message):
        parse_directive_document(_document(when=condition))


def test_document_condition_validator_enforces_complexity_and_allows_macro_only() -> None:
    too_deep = {"eq": [{"literal": 1}, {"literal": 1}]}
    for _index in range(17):
        too_deep = {"not": too_deep}
    with pytest.raises(ValidationError, match="complexity limits"):
        parse_directive_document(_document(when=too_deep))

    raw = _document()
    raw["set"] = {}
    parsed = parse_directive_document(raw)
    assert "set" not in parsed.to_dict()
    assert parsed.macro is not None


def test_document_condition_validator_accepts_nested_boolean_groups() -> None:
    condition = {
        "all": [
            {
                "eq": [
                    {"fact": "repository.metadata.build_system"},
                    {"literal": "make"},
                ]
            },
            {
                "not": {
                    "eq": [
                        {"fact": "project.name"},
                        {"literal": "archived"},
                    ]
                }
            },
        ]
    }

    assert parse_directive_document(_document(when=condition)).when == condition


def test_document_template_validator_accepts_known_variable_marker() -> None:
    raw = _document()
    raw["macro"]["inputs"]["primary_target"] = {"var": "primary_target"}

    parsed = parse_directive_document(raw)

    assert parsed.macro["inputs"]["primary_target"] == {"var": "primary_target"}


def test_defaults_optional_bindings_and_all_variable_types() -> None:
    definitions = {
        "text": {"type": "string", "binding": "values.text", "default": "fallback"},
        "flag": {"type": "boolean", "binding": "values.flag", "default": True},
        "count": {"type": "integer", "binding": "values.count", "default": 3},
        "ratio": {"type": "number", "binding": "values.ratio", "default": 1.5},
        "items": {"type": "list", "binding": "values.items", "default": [1]},
        "mapping": {"type": "object", "binding": "values.mapping", "default": {"x": 1}},
        "optional": {"type": "string", "binding": "values.optional", "required": False},
    }
    resolved, missing = resolve_variables(definitions, [])
    assert missing == ()
    assert resolved == {
        "text": "fallback",
        "flag": True,
        "count": 3,
        "ratio": 1.5,
        "items": [1],
        "mapping": {"x": 1},
    }


def test_marked_rendering_covers_fact_var_template_collections_and_failures() -> None:
    facts = {
        "fleet": {"name": "mac"},
        "project": {},
        "repository": {"id": "repo", "metadata": {"labels": ["a", "b"], "empty": None}},
        "agent": {},
    }
    variables = {"target": "//:all", "options": {"fast": True}}
    assert render_marked_value(None, facts=facts, variables=variables) is None
    assert (
        render_marked_value({"fact": "repository.id"}, facts=facts, variables=variables) == "repo"
    )
    assert render_marked_value({"var": "options"}, facts=facts, variables=variables) == {
        "fast": True
    }
    assert render_marked_value(
        [
            {"template": "${target}:${repository.metadata.labels}"},
            {"template": "empty=${repository.metadata.empty}"},
        ],
        facts=facts,
        variables=variables,
    ) == ['//:all:["a","b"]', "empty="]
    with pytest.raises(ValidationError, match="unresolved directive variable"):
        render_marked_value({"var": "missing"}, facts=facts, variables=variables)
    with pytest.raises(ValidationError, match="template must be a string"):
        render_marked_value({"template": 1}, facts=facts, variables=variables)
    with pytest.raises(ValidationError, match="unresolved directive template value"):
        render_marked_value({"template": "${missing}"}, facts=facts, variables=variables)
    with pytest.raises(ValidationError, match="unresolved directive template expression"):
        render_marked_value({"template": "${unterminated"}, facts=facts, variables=variables)
    with pytest.raises(ValidationError, match="template is too long"):
        render_marked_value({"template": "x" * 16_385}, facts=facts, variables=variables)


def test_template_validation_and_json_finiteness_fail_closed() -> None:
    cases = []
    raw = _document()
    raw["macro"]["inputs"] = {"bad": {"template": 1}}
    cases.append((raw, "template must be a string"))
    raw = _document()
    raw["macro"]["inputs"] = {"bad": {"template": "${unknown}"}}
    cases.append((raw, "unknown directive template value"))
    raw = _document()
    raw["macro"]["inputs"] = {"bad": {"fact": "unknown.value"}}
    cases.append((raw, "unsupported directive fact root"))
    raw = _document()
    raw["macro"]["inputs"] = {"bad": {"var": "unknown"}}
    cases.append((raw, "unknown directive variable"))
    raw = _document(set={"build.value": float("nan")}, macro=None, variables={})
    cases.append((raw, "finite JSON data"))
    for candidate, message in cases:
        with pytest.raises(ValidationError, match=message):
            parse_directive_document(candidate)


def test_overlap_analysis_handles_unconditional_reversed_and_malformed_forms() -> None:
    assert condition_overlap(None, {"eq": [{"literal": 1}, {"literal": 1}]}) == "overlap"
    reversed_eq = {"eq": [{"literal": "make"}, {"fact": "repository.metadata.build_system"}]}
    direct_eq = {"eq": [{"fact": "repository.metadata.build_system"}, {"literal": "make"}]}
    assert condition_overlap(reversed_eq, direct_eq) == "overlap"
    assert condition_overlap({"all": direct_eq}, direct_eq) == "unknown"
    assert condition_overlap({"eq": "bad"}, direct_eq) == "unknown"
    assert condition_overlap({"eq": [{"literal": 1}, {"literal": 2}]}, direct_eq) == "unknown"
    assert (
        condition_overlap(
            {"eq": [{"fact": "repository.id"}, {"fact": "repository.name"}]}, direct_eq
        )
        == "unknown"
    )
    assert condition_overlap([], direct_eq) == "unknown"
    with pytest.raises(ValidationError, match="unsupported directive fact root"):
        render_marked_value({"fact": "unknown.value"}, facts={}, variables={})
