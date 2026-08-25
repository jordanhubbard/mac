"""Deterministic conflict analysis for two declared effect sets.

These rules used to live in the work-package plan compiler
(``work_package_effect_conflicts`` over ``WorkPackageEffects``).  The compiler
is gone; the rules are not -- ``mac.directive_service`` uses them to decide
whether two directive macros may run together, so they are pinned here.
"""

from __future__ import annotations

from mac.effect_conflicts import DeclaredEffects, effect_conflicts


def test_effect_conflicts_encode_parallel_safety_rules() -> None:
    reader = DeclaredEffects(reads=("db:tasks",))
    other_reader = DeclaredEffects(reads=("db:tasks",))
    writer = DeclaredEffects(writes=("db:tasks",))
    exclusive = DeclaredEffects(exclusive=("repo:mac",))
    repo_reader = DeclaredEffects(reads=("repo:mac",))
    publisher = DeclaredEffects(external=("github:mac",))

    # Read/read overlap is safe; everything else against a write is not.
    assert effect_conflicts(reader, other_reader) == []
    assert effect_conflicts(reader, writer) == ["write:db:tasks"]
    assert effect_conflicts(exclusive, repo_reader) == ["exclusive:repo:mac"]
    assert effect_conflicts(publisher, publisher) == ["external:github:mac"]

    # An exclusive repository lock conflicts with any path inside it, and the
    # exclusive reason subsumes the write reason for the same resource.
    repository_lock = DeclaredEffects(exclusive=("repo:mac",))
    path_writer = DeclaredEffects(writes=("src/api",))
    assert effect_conflicts(repository_lock, path_writer) == ["exclusive:repo:mac~src/api"]


def test_conflicts_are_symmetric_and_deterministically_ordered() -> None:
    left = DeclaredEffects(writes=("db:a", "db:b"), external=("github:mac",))
    right = DeclaredEffects(reads=("db:b",), writes=("db:a",), external=("github:mac",))

    forward = effect_conflicts(left, right)
    backward = effect_conflicts(right, left)

    assert forward == backward
    assert forward == sorted(forward)
    assert forward == ["external:github:mac", "write:db:a", "write:db:b"]


def test_path_prefixes_and_the_wildcard_overlap() -> None:
    assert effect_conflicts(
        DeclaredEffects(writes=("src/",)), DeclaredEffects(reads=("src/api/app.py",))
    ) == ["write:src/~src/api/app.py"]
    assert effect_conflicts(
        DeclaredEffects(writes=("*",)), DeclaredEffects(reads=("anything",))
    ) == ["write:*~anything"]
    # Sibling paths do not overlap.
    assert (
        effect_conflicts(
            DeclaredEffects(writes=("src/api",)), DeclaredEffects(writes=("src/apiary",))
        )
        == []
    )


def test_empty_effects_never_conflict() -> None:
    assert effect_conflicts(DeclaredEffects(), DeclaredEffects()) == []
    assert effect_conflicts(DeclaredEffects(writes=("db:tasks",)), DeclaredEffects()) == []


def test_to_dict_is_a_plain_json_object() -> None:
    effects = DeclaredEffects(
        reads=("a",),
        writes=("b",),
        exclusive=("c",),
        external=("d",),
        external_contract={"idempotency": "key"},
    )

    assert effects.to_dict() == {
        "reads": ["a"],
        "writes": ["b"],
        "exclusive": ["c"],
        "external": ["d"],
        "external_contract": {"idempotency": "key"},
    }
