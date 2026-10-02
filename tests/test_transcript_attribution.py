"""A transcript must say WHICH coding agent and model produced it.

`task_agent_transcripts` has `coding_agent` and `model` columns. On the live hub
they were empty on **all 275 rows**, across 188 distinct tasks:

    SELECT count(*) FILTER (WHERE coalesce(coding_agent,'')<>'') -> 0

So every transcript recorded what was said, by nobody in particular. That makes
the one question a transcript exists to answer -- which CLI and which model
produced this -- unanswerable from the ledger, and it silently defeats any
comparison between agents or between models.

The obvious diagnosis was wrong, and the numbers say so. It was reported as
"`run_audited_command` reads `opts` instead of `opts['task']['metadata']`", but
neither location holds the value: on the live hub **0 of 8,154 tasks** carry
`coding_agent` in their metadata, and 11 carry `model`. Nothing ever put it
there. Reading a different key would have changed nothing.

The only truthful source is the route that actually ran. `_agent_argv` populates
its `chosen` dict with the selected agent (`route["agent"]`), and
`CodingAgentChoice` also knows the model -- but neither ever left that function.
`_opts_with_route` carries both into the metadata the runner writes.
"""

from __future__ import annotations

import pytest

from mac import executor_sandbox


def test_the_resolved_route_reaches_the_runner_metadata():
    opts = {"task": {"id": "task_1", "metadata": {}}}
    route = {"agent": "claude", "model": "claude-opus-4-8"}

    merged = executor_sandbox._opts_with_route(opts, route)

    assert merged["coding_agent"] == "claude", (
        "the transcript would be written with no coding_agent, which is the "
        "state all 275 live rows are in"
    )
    assert merged["model"] == "claude-opus-4-8"
    assert merged["task"] == opts["task"], "unrelated opts must survive"


def test_an_empty_route_changes_nothing():
    """No route resolved yet is not the same as 'attributed to nothing'."""
    opts = {"task": {"id": "task_1"}}

    assert executor_sandbox._opts_with_route(opts, {}) is opts
    assert executor_sandbox._opts_with_route(opts, {"agent": "", "model": ""}) is opts


def test_an_explicit_value_already_present_wins():
    """A caller that already knows better is not overwritten."""
    opts = {"coding_agent": "codex", "model": "gpt-5"}
    route = {"agent": "claude", "model": "claude-opus-4-8"}

    merged = executor_sandbox._opts_with_route(opts, route)

    assert merged["coding_agent"] == "codex"
    assert merged["model"] == "gpt-5"


def test_a_partial_route_fills_only_what_it_knows():
    """Some routes resolve an agent without a model. Half an attribution is
    still better than none, and must not invent the other half."""
    merged = executor_sandbox._opts_with_route({}, {"agent": "cursor"})

    assert merged["coding_agent"] == "cursor"
    assert "model" not in merged, "a model must never be fabricated"


def test_the_original_opts_are_not_mutated():
    """Mutating the caller's opts would leak this run's attribution into it."""
    opts = {"task": {"id": "task_1"}}

    executor_sandbox._opts_with_route(opts, {"agent": "claude"})

    assert "coding_agent" not in opts, (
        "opts was mutated in place; the caller's dict must not carry one "
        "run's attribution into another"
    )


def test_every_invocation_path_attributes(monkeypatch):
    """Source-level: both runner call sites must go through the helper.

    The coding agent is invoked sandboxed or unsandboxed/break-glass. A path
    that skips attribution produces exactly the silent hole this fixes.
    """
    import inspect

    source = inspect.getsource(executor_sandbox._invoke_agent)

    assert source.count("_opts_with_route") == 2, (
        "expected both runner call sites (sandboxed, unsandboxed) to "
        "attribute; found %d" % source.count("_opts_with_route")
    )


def test_the_route_carries_the_model_not_just_the_agent():
    """`chosen` used to record only agent + fingerprint, so `model` had no
    source at all."""
    import inspect

    source = inspect.getsource(executor_sandbox._agent_argv)

    assert 'chosen["model"]' in source, (
        "the resolved route does not carry the model, so the transcript's "
        "`model` column can never be populated"
    )
