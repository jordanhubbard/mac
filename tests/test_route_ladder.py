"""Vertical-slice tests for the ordered coding-route ladder (ADR 0029).

The scenarios are the ones the release gate names, driven through fake CLI/API
outcomes rather than live providers: quota exhaustion, fallback without repeated
waste, fleet convergence over AgentBus, bounded half-open recovery, return to a
cheaper route, and in-flight stability. Two independent ladders stand in for two
agents, exchanging only ``mac.route_availability.v1`` records — the same bytes
the bus would carry — so "the fleet converged" is proven by the contract, not by
shared memory.
"""

from __future__ import annotations

import json

import pytest

from mac.route_ladder import (
    ANY_CAPABILITY,
    AVAILABILITY_SCHEMA,
    ENDPOINT_DIRECT_API,
    ENDPOINT_SUBSCRIPTION,
    FAILURE_AUTH,
    FAILURE_MODEL_UNAVAILABLE,
    FAILURE_PROVIDER_OUTAGE,
    FAILURE_QUOTA_EXHAUSTED,
    FAILURE_RATE_LIMITED,
    FAILURE_SEMANTIC,
    FAILURE_TRANSPORT,
    LADDER_ENV,
    LADDER_FILE_ENV,
    LADDER_SCHEMA,
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    LadderConfigError,
    LadderPolicy,
    LadderRoute,
    RouteCapability,
    RouteCost,
    RouteKey,
    RouteLadder,
    SecretLeakError,
    assert_secret_free,
    classify_route_failure,
    ladder_harness_order,
    load_ladder,
    parse_ladder_document,
    parse_route_availability,
    route_availability_record,
)


class FakeClock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


LADDER_DOC = {
    "schema": LADDER_SCHEMA,
    "policy": {
        "quota_cooldown_seconds": 3600,
        "rate_limit_cooldown_seconds": 60,
        "auth_cooldown_seconds": 900,
        "transport_cooldown_seconds": 120,
        "failure_threshold": 3,
        "half_open_max_probes": 1,
    },
    "routes": [
        {
            "rank": 0,
            "harness": "codex",
            "credential_source": "codex-oauth-file",
            "provider": "openai",
            "model": "gpt-5.5",
            "endpoint_class": ENDPOINT_SUBSCRIPTION,
        },
        {
            "rank": 1,
            "harness": "claude",
            "credential_source": "claude-oauth-file",
            "provider": "anthropic",
            "model": "claude-opus-5",
            "endpoint_class": ENDPOINT_SUBSCRIPTION,
        },
        {
            "rank": 2,
            "harness": "opencode",
            "credential_source": "OPENCODE_API_KEY",
            "provider": "openrouter",
            "model": "qwen3-coder",
            "endpoint_class": ENDPOINT_DIRECT_API,
        },
    ],
}


def build_ladder(agent_id: str = "agent_a", clock=None, capability=ANY_CAPABILITY):
    routes, policy = parse_ladder_document(LADDER_DOC)
    clock = clock or FakeClock()
    return (
        RouteLadder(
            routes,
            policy=policy,
            capability=capability,
            agent_id=agent_id,
            clock=clock,
            wall_clock=clock,
        ),
        clock,
    )


def route_named(ladder: RouteLadder, harness: str) -> LadderRoute:
    for route in ladder.effective_order():
        if route.key.harness == harness:
            return route
    raise AssertionError("no %s route on the ladder" % harness)


# --------------------------------------------------------------------------- #
# Document + identity
# --------------------------------------------------------------------------- #
def test_owner_order_is_authoritative_and_harness_is_the_major_key():
    routes, _ = parse_ladder_document(LADDER_DOC)
    assert [r.key.harness for r in sorted(routes, key=lambda r: r.rank)] == [
        "codex",
        "claude",
        "opencode",
    ]
    assert ladder_harness_order(routes) == ("codex", "claude", "opencode")


def test_equal_rank_ties_break_on_harness_then_model_not_on_price_guessing():
    doc = {
        "schema": LADDER_SCHEMA,
        "routes": [
            {"rank": 0, "harness": "zed", "credential_source": "s", "provider": "p", "model": "a"},
            {"rank": 0, "harness": "abe", "credential_source": "s", "provider": "p", "model": "z"},
            {"rank": 0, "harness": "abe", "credential_source": "s", "provider": "p", "model": "b"},
        ],
    }
    routes, _ = parse_ladder_document(doc)
    ladder = RouteLadder(routes, clock=FakeClock())
    ordered = [(r.key.harness, r.key.model) for r in ladder.effective_order()]
    # harness first, model only as the final tie-break -- "z" sorting before "b"
    # would mean something read the model name as a price signal.
    assert ordered == [("abe", "b"), ("abe", "z"), ("zed", "a")]


def test_missing_rank_means_written_position():
    doc = {
        "schema": LADDER_SCHEMA,
        "routes": [
            {"harness": "codex", "credential_source": "s", "provider": "openai"},
            {"harness": "claude", "credential_source": "s2", "provider": "anthropic"},
        ],
    }
    routes, _ = parse_ladder_document(doc)
    assert [(r.rank, r.key.harness) for r in routes] == [(0, "codex"), (1, "claude")]


@pytest.mark.parametrize(
    "doc, needle",
    [
        ({"schema": "other.v1", "routes": []}, "schema"),
        ({"schema": LADDER_SCHEMA}, "routes"),
        ({"schema": LADDER_SCHEMA, "routes": []}, "no routes"),
        (
            {"schema": LADDER_SCHEMA, "routes": [{"harness": "codex", "provider": "openai"}]},
            "credential_source",
        ),
        (
            {
                "schema": LADDER_SCHEMA,
                "routes": [
                    {"harness": "c", "credential_source": "s", "provider": "p", "rank": 0},
                    {"harness": "c", "credential_source": "s", "provider": "p", "rank": 1},
                ],
            },
            "duplicates",
        ),
    ],
)
def test_a_ladder_that_half_parses_is_rejected(doc, needle):
    with pytest.raises(LadderConfigError) as excinfo:
        parse_ladder_document(doc)
    assert needle in str(excinfo.value)


def test_route_identity_refuses_to_carry_a_credential():
    with pytest.raises(SecretLeakError):
        RouteKey(
            harness="codex",
            credential_source="sk-abcdefghijklmnopqrstuvwxyz012345",
            provider="openai",
        )
    with pytest.raises(SecretLeakError):
        RouteKey(
            harness="codex",
            credential_source="https://user:pass@api.example.com/v1",
            provider="openai",
        )
    with pytest.raises(SecretLeakError):
        assert_secret_free("https://api.example.com/v1?api_key=x", field_name="endpoint")


def test_fingerprint_is_stable_and_contains_no_field_values_by_accident():
    key = RouteKey("codex", "codex-oauth-file", "openai", "gpt-5.5", ENDPOINT_SUBSCRIPTION)
    assert key.fingerprint() == RouteKey.from_dict(key.observable()).fingerprint()
    assert key.fingerprint().startswith("sha256:")
    assert key.credential_identity == "codex|codex-oauth-file"


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, status, expected",
    [
        ("You have reached your monthly usage limit", None, FAILURE_QUOTA_EXHAUSTED),
        ("resource_exhausted", None, FAILURE_QUOTA_EXHAUSTED),
        # A cap delivered as 429: the body is true, the status is not.
        ("429 Too Many Requests: credit balance is too low", 429, FAILURE_QUOTA_EXHAUSTED),
        ("Rate limit reached, retry-after 20", 429, FAILURE_RATE_LIMITED),
        ("401 Unauthorized", 401, FAILURE_AUTH),
        ("model not found: gpt-4", None, FAILURE_MODEL_UNAVAILABLE),
        ("502 Bad Gateway", 502, FAILURE_PROVIDER_OUTAGE),
        ("connection refused", None, FAILURE_TRANSPORT),
        (None, 503, FAILURE_PROVIDER_OUTAGE),
        ("2 tests failed in the executor", None, FAILURE_SEMANTIC),
        ("", None, FAILURE_SEMANTIC),
    ],
)
def test_classification_defaults_to_semantic_rather_than_guessing_unavailable(
    text, status, expected
):
    assert classify_route_failure(text or "", status_code=status) == expected


def test_a_caller_that_knows_it_is_a_transport_failure_can_say_so():
    assert (
        classify_route_failure("weird harness crash", default=FAILURE_TRANSPORT)
        == FAILURE_TRANSPORT
    )


# --------------------------------------------------------------------------- #
# Quota exhaustion, fallback, and not paying twice
# --------------------------------------------------------------------------- #
def test_quota_exhaustion_suppresses_one_route_and_work_continues_on_the_next():
    ladder, clock = build_ladder()
    codex = ladder.begin_turn()
    assert codex.key.harness == "codex"

    record = ladder.record_failure(codex, FAILURE_QUOTA_EXHAUSTED, evidence="monthly cap")
    assert record["schema"] == AVAILABILITY_SCHEMA
    assert record["outcome"] == OUTCOME_FAILURE
    assert record["failure_class"] == FAILURE_QUOTA_EXHAUSTED
    assert record["affects_availability"] is True

    # One observation is enough; the next turn is already on the next rung.
    assert ladder.begin_turn().key.harness == "claude"
    # ...and stays there for the whole cooldown rather than re-proving the cap.
    clock.advance(3599)
    assert ladder.begin_turn().key.harness == "claude"


def test_a_semantic_failure_never_costs_a_route_its_rank():
    ladder, _ = build_ladder()
    codex = ladder.begin_turn()
    record = ladder.record_failure(codex, FAILURE_SEMANTIC, evidence="contract tests failed")
    assert record["affects_availability"] is False
    assert ladder.begin_turn().key.harness == "codex"


def test_transport_noise_needs_corroboration_before_it_demotes_a_route():
    ladder, _ = build_ladder()
    codex = route_named(ladder, "codex")
    for _ in range(2):
        ladder.record_failure(codex, FAILURE_TRANSPORT)
        assert ladder.begin_turn().key.harness == "codex"
    ladder.record_failure(codex, FAILURE_TRANSPORT)
    assert ladder.begin_turn().key.harness == "claude"


def test_every_route_suppressed_fails_closed_rather_than_hanging():
    ladder, _ = build_ladder()
    for route in ladder.effective_order():
        ladder.record_failure(route, FAILURE_QUOTA_EXHAUSTED)
    assert ladder.select() is None
    assert ladder.begin_turn() is None


# --------------------------------------------------------------------------- #
# Bounded half-open recovery
# --------------------------------------------------------------------------- #
def test_recovery_is_a_single_bounded_probe_not_a_stampede():
    ladder, clock = build_ladder()
    codex = route_named(ladder, "codex")
    ladder.record_failure(codex, FAILURE_QUOTA_EXHAUSTED)
    clock.advance(3600)

    # The cooldown has elapsed: exactly one probe is handed out...
    assert ladder.select().key.harness == "codex"
    # ...and every concurrent selection until it reports keeps falling back.
    assert ladder.select().key.harness == "claude"
    assert ladder.select().key.harness == "claude"


def test_a_failed_probe_reopens_and_restarts_the_cooldown():
    ladder, clock = build_ladder()
    codex = route_named(ladder, "codex")
    ladder.record_failure(codex, FAILURE_QUOTA_EXHAUSTED)
    clock.advance(3600)
    probe = ladder.select()
    assert probe.key.harness == "codex"

    ladder.record_failure(codex, FAILURE_QUOTA_EXHAUSTED)
    clock.advance(3599)
    assert ladder.select().key.harness == "claude"
    clock.advance(1)
    assert ladder.select().key.harness == "codex"


def test_a_refreshed_quota_is_rediscovered_and_the_cheapest_route_returns():
    ladder, clock = build_ladder()
    codex = route_named(ladder, "codex")
    ladder.record_failure(codex, FAILURE_QUOTA_EXHAUSTED)
    assert ladder.begin_turn().key.harness == "claude"

    clock.advance(3600)
    probe = ladder.select()
    ladder.record_success(probe)
    assert ladder.begin_turn().key.harness == "codex"
    telemetry = ladder.telemetry()
    codex_row = next(r for r in telemetry["routes"] if r["route"]["harness"] == "codex")
    assert codex_row["suppressed"] is False
    assert codex_row["last_success_at"] is not None


# --------------------------------------------------------------------------- #
# In-flight stability and turn-boundary switching
# --------------------------------------------------------------------------- #
def test_a_cheaper_route_coming_back_does_not_move_an_in_flight_turn():
    ladder, clock = build_ladder()
    codex = route_named(ladder, "codex")
    ladder.record_failure(codex, FAILURE_QUOTA_EXHAUSTED)
    pinned = ladder.begin_turn()
    assert pinned.key.harness == "claude"

    # Mid-turn: the cheaper route becomes eligible again.
    ladder.record_success(codex, agent_id="agent_b")
    assert ladder.current().key.harness == "claude"  # unchanged in flight
    assert ladder.pending_switch().key.harness == "codex"

    ladder.end_turn()
    assert ladder.begin_turn().key.harness == "codex"
    assert ladder.pending_switch() is None


# --------------------------------------------------------------------------- #
# Fleet convergence over the published record
# --------------------------------------------------------------------------- #
def test_one_agents_cap_suppresses_the_route_for_a_peer_without_a_second_probe():
    a, _ = build_ladder("agent_a")
    b, _ = build_ladder("agent_b")

    codex = route_named(a, "codex")
    record = a.record_failure(codex, FAILURE_QUOTA_EXHAUSTED, evidence="monthly cap")
    # Cross the wire exactly as the bus would.
    wire = json.loads(json.dumps(record))

    assert b.begin_turn().key.harness == "codex"
    assert b.apply_outcome(wire) is True
    assert b.begin_turn().key.harness == "claude"
    # Re-delivery of the same record is idempotent for the ladder's purposes.
    assert b.apply_outcome(wire) is False


def test_a_peers_success_supersedes_an_older_failure():
    a, _ = build_ladder("agent_a")
    b, _ = build_ladder("agent_b")
    codex = route_named(b, "codex")
    b.record_failure(codex, FAILURE_QUOTA_EXHAUSTED)
    assert b.begin_turn().key.harness == "claude"

    success = a.record_success(route_named(a, "codex"))
    assert b.apply_outcome(json.loads(json.dumps(success))) is True
    b.end_turn()
    assert b.begin_turn().key.harness == "codex"


def test_a_cap_on_one_account_does_not_suppress_the_same_cli_on_another():
    doc = {
        "schema": LADDER_SCHEMA,
        "routes": [
            {
                "rank": 0,
                "harness": "codex",
                "credential_source": "codex-account-one",
                "provider": "openai",
            },
            {
                "rank": 1,
                "harness": "codex",
                "credential_source": "codex-account-two",
                "provider": "openai",
            },
        ],
    }
    routes, policy = parse_ladder_document(doc)
    clock = FakeClock()
    ladder = RouteLadder(routes, policy=policy, agent_id="b", clock=clock, wall_clock=clock)
    peer_routes, _ = parse_ladder_document(doc)
    capped = peer_routes[0]
    record = route_availability_record(
        capped,
        outcome=OUTCOME_FAILURE,
        failure_class=FAILURE_QUOTA_EXHAUSTED,
        agent_id="a",
        observed_at=1.0,
    )
    assert ladder.apply_outcome(record) is True
    chosen = ladder.begin_turn()
    assert chosen.key.credential_source == "codex-account-two"


def test_host_local_failures_do_not_travel_between_workers():
    a, _ = build_ladder("agent_a")
    b, _ = build_ladder("agent_b")
    codex = route_named(a, "codex")
    for failure_class in (FAILURE_TRANSPORT, FAILURE_RATE_LIMITED, FAILURE_PROVIDER_OUTAGE):
        record = a.record_failure(codex, failure_class)
        assert b.apply_outcome(record) is False
    assert b.begin_turn().key.harness == "codex"


def test_an_auth_failure_is_an_account_fact_and_does_travel():
    a, _ = build_ladder("agent_a")
    b, _ = build_ladder("agent_b")
    record = a.record_failure(route_named(a, "codex"), FAILURE_AUTH)
    assert b.apply_outcome(record) is True
    assert b.begin_turn().key.harness == "claude"


def test_fleet_learning_never_grants_a_route_the_worker_cannot_run():
    capability = RouteCapability.from_iterables(harnesses=["claude", "opencode"])
    ladder, _ = build_ladder("agent_b", capability=capability)
    assert ladder.begin_turn().key.harness == "claude"

    a, _ = build_ladder("agent_a")
    success = a.record_success(route_named(a, "codex"))
    ladder.apply_outcome(success)
    ladder.end_turn()
    # A peer proving codex works does not install codex here.
    assert ladder.begin_turn().key.harness == "claude"
    row = next(r for r in ladder.telemetry()["routes"] if r["route"]["harness"] == "codex")
    assert row["locally_capable"] is False
    # ...and an incapable route is never reported as unhealthy.
    assert row["suppressed"] is False


def test_an_agent_ignores_the_echo_of_its_own_report():
    a, _ = build_ladder("agent_a")
    record = a.record_failure(route_named(a, "codex"), FAILURE_QUOTA_EXHAUSTED)
    assert a.apply_outcome(record) is False


# --------------------------------------------------------------------------- #
# Published records are secret-free and bounded
# --------------------------------------------------------------------------- #
def test_published_evidence_is_scrubbed_and_bounded():
    ladder, _ = build_ladder()
    record = ladder.record_failure(
        route_named(ladder, "codex"),
        FAILURE_AUTH,
        evidence=(
            "request to https://user:secret@api.example.com failed with "
            "Authorization: Bearer sk-live-abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 500
        ),
    )
    evidence = record["evidence"]
    assert len(evidence) <= 240
    assert "secret" not in evidence
    assert "sk-live" not in evidence
    assert "Bearer sk" not in evidence
    assert json.dumps(record)  # the whole record is JSON-serialisable


def test_a_peer_record_is_validated_to_the_same_standard_as_a_local_one():
    ladder, _ = build_ladder()
    good = ladder.record_failure(route_named(ladder, "codex"), FAILURE_QUOTA_EXHAUSTED)
    assert parse_route_availability(good)["failure_class"] == FAILURE_QUOTA_EXHAUSTED

    with pytest.raises(ValueError):
        parse_route_availability({"schema": "something.else.v1"})
    bad_class = dict(good, failure_class="cheaper_elsewhere")
    with pytest.raises(ValueError):
        parse_route_availability(bad_class)
    leaky = json.loads(json.dumps(good))
    leaky["route"]["credential_source"] = "sk-abcdefghijklmnopqrstuvwxyz012345"
    with pytest.raises(SecretLeakError):
        parse_route_availability(leaky)


def test_a_success_record_must_not_claim_a_failure_class():
    ladder, _ = build_ladder()
    route = route_named(ladder, "codex")
    with pytest.raises(ValueError):
        route_availability_record(
            route,
            outcome=OUTCOME_SUCCESS,
            failure_class=FAILURE_QUOTA_EXHAUSTED,
            agent_id="a",
            observed_at=1.0,
        )


# --------------------------------------------------------------------------- #
# Cost: advisory, and unknown is not zero
# --------------------------------------------------------------------------- #
def test_unmeasured_cost_is_unknown_and_never_zero():
    ladder, _ = build_ladder()
    rows = {r["route"]["harness"]: r for r in ladder.telemetry()["routes"]}
    for row in rows.values():
        assert row["cost"]["known"] is False
        assert row["cost"]["usd_per_million_tokens"] is None

    ladder.record_cost(
        route_named(ladder, "opencode"),
        RouteCost(usd_per_million_tokens=0.9, observed_at=12.0, source="nemo-relay"),
    )
    row = next(r for r in ladder.telemetry()["routes"] if r["route"]["harness"] == "opencode")
    assert row["cost"] == {
        "known": True,
        "usd_per_million_tokens": 0.9,
        "observed_at": 12.0,
        "source": "nemo-relay",
    }


def test_relay_cost_does_not_silently_reorder_the_owners_ladder():
    ladder, _ = build_ladder()
    # Relay measures the owner's rank-0 route as the most expensive one.
    ladder.record_cost(route_named(ladder, "codex"), RouteCost(99.0, 1.0, "nemo-relay"))
    ladder.record_cost(route_named(ladder, "opencode"), RouteCost(0.1, 1.0, "nemo-relay"))
    assert ladder.begin_turn().key.harness == "codex"
    assert [r.key.harness for r in ladder.effective_order()][0] == "codex"


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
def test_telemetry_surfaces_rank_effective_route_reason_cost_and_last_success():
    ladder, clock = build_ladder()
    ladder.record_success(route_named(ladder, "codex"))
    ladder.record_failure(
        route_named(ladder, "codex"), FAILURE_QUOTA_EXHAUSTED, evidence="monthly cap"
    )
    ladder.begin_turn()
    telemetry = ladder.telemetry()

    assert telemetry["schema"] == "mac.route_ladder.telemetry.v1"
    assert telemetry["effective_route"]["harness"] == "claude"
    assert telemetry["effective_rank"] == 1
    codex_row = next(r for r in telemetry["routes"] if r["route"]["harness"] == "codex")
    assert codex_row["rank"] == 0
    assert codex_row["suppressed"] is True
    assert codex_row["suppression_reason"].startswith(FAILURE_QUOTA_EXHAUSTED)
    assert codex_row["seconds_until_probe"] == pytest.approx(3600.0)
    assert codex_row["last_success_at"] is not None
    assert json.dumps(telemetry)
    assert "Bearer" not in json.dumps(telemetry)


# --------------------------------------------------------------------------- #
# Loading the owner's document
# --------------------------------------------------------------------------- #
def test_no_configured_ladder_is_reported_as_none_not_invented():
    assert load_ladder({}) is None


def test_inline_and_file_documents_load_identically(tmp_path):
    path = tmp_path / "ladder.json"
    path.write_text(json.dumps(LADDER_DOC), encoding="utf-8")
    from_inline = load_ladder({LADDER_ENV: json.dumps(LADDER_DOC)})
    from_file = load_ladder({LADDER_FILE_ENV: str(path)})
    from_env_path = load_ladder({LADDER_ENV: str(path)})
    assert from_inline is not None and from_file is not None
    assert [r.identity for r in from_inline[0]] == [r.identity for r in from_file[0]]
    assert [r.identity for r in from_env_path[0]] == [r.identity for r in from_file[0]]
    assert from_file[1].quota_cooldown_seconds == 3600.0


def test_an_unreadable_or_invalid_document_fails_loudly():
    with pytest.raises(LadderConfigError):
        load_ladder({LADDER_FILE_ENV: "/nonexistent/ladder.json"})
    with pytest.raises(LadderConfigError):
        load_ladder({LADDER_ENV: "{not json"})


def test_policy_rejects_nonsense_rather_than_defaulting_around_it():
    with pytest.raises(LadderConfigError):
        LadderPolicy.from_dict({"quota_cooldown_seconds": "soon"})
    with pytest.raises(LadderConfigError):
        LadderPolicy.from_dict({"quota_cooldown_seconds": -1})


# --------------------------------------------------------------------------- #
# The one place every consumer reads the order from
# --------------------------------------------------------------------------- #
class TestCodingAgentSelectionConsumesTheLadder:
    """``resolve_coding_agent`` is the single selection point every worker,
    executor and reviewer already goes through. Making IT read the ladder is
    what turns the owner's document into the fleet's actual search path -- a
    second ordering living next to it would be the per-worker accident again,
    only with a schema on top."""

    @staticmethod
    def _which(*present):
        names = {n for n in present}

        def _lookup(command):
            base = command.replace("cursor-agent", "cursor")
            return "/usr/bin/%s" % command if base in names else None

        return _lookup

    def _env(self, doc, **extra):
        env = {LADDER_ENV: json.dumps(doc), "ANTHROPIC_API_KEY": "k"}
        env.update(extra)
        return env

    def test_owner_order_beats_the_built_in_priority(self, tmp_path):
        from mac.coding_agent import AGENT_PRIORITY, resolve_coding_agent

        # The built-in order prefers opencode; the owner ranks claude first.
        assert AGENT_PRIORITY[0] == "opencode"
        (tmp_path / ".local" / "share" / "opencode").mkdir(parents=True)
        (tmp_path / ".local" / "share" / "opencode" / "auth.json").write_text(
            json.dumps({"anthropic": {"key": "x"}}), encoding="utf-8"
        )
        doc = {
            "schema": LADDER_SCHEMA,
            "routes": [
                {
                    "rank": 0,
                    "harness": "claude",
                    "credential_source": "ANTHROPIC_API_KEY",
                    "provider": "anthropic",
                },
                {
                    "rank": 1,
                    "harness": "opencode",
                    "credential_source": "opencode-auth-file",
                    "provider": "openrouter",
                },
            ],
        }
        choice = resolve_coding_agent(
            env=self._env(doc), home=tmp_path, which=self._which("claude", "opencode")
        )
        assert choice.agent == "claude"
        assert any("route ladder order applies" in line for line in choice.rationale)

    def test_a_cli_absent_from_the_ladder_is_not_selected(self, tmp_path):
        from mac.coding_agent import resolve_coding_agent

        doc = {
            "schema": LADDER_SCHEMA,
            "routes": [
                {
                    "rank": 0,
                    "harness": "codex",
                    "credential_source": "codex-oauth-file",
                    "provider": "openai",
                }
            ],
        }
        # claude is installed and authenticated, but the owner did not rank it.
        choice = resolve_coding_agent(
            env=self._env(doc), home=tmp_path, which=self._which("claude")
        )
        assert choice.agent == ""
        assert choice.available is False

    def test_an_unusable_ladder_is_reported_not_silently_ignored(self, tmp_path):
        from mac.coding_agent import resolve_coding_agent

        choice = resolve_coding_agent(
            env={LADDER_ENV: "{not json", "ANTHROPIC_API_KEY": "k"},
            home=tmp_path,
            which=self._which("claude"),
        )
        assert choice.agent == "claude"  # the fleet keeps working...
        assert any("unusable" in line for line in choice.rationale)  # ...and says why

    def test_a_pin_still_wins_over_the_ladder(self, tmp_path):
        from mac.coding_agent import FORCE_ENV, resolve_coding_agent

        doc = {
            "schema": LADDER_SCHEMA,
            "routes": [
                {
                    "rank": 0,
                    "harness": "codex",
                    "credential_source": "codex-oauth-file",
                    "provider": "openai",
                }
            ],
        }
        choice = resolve_coding_agent(
            env=self._env(doc, **{FORCE_ENV: "claude"}),
            home=tmp_path,
            which=self._which("claude"),
        )
        assert choice.agent == "claude"

    def test_an_unconfigured_fleet_keeps_the_built_in_order_and_its_rationale(self, tmp_path):
        from mac.coding_agent import resolve_coding_agent

        choice = resolve_coding_agent(
            env={"ANTHROPIC_API_KEY": "k"}, home=tmp_path, which=self._which("claude")
        )
        assert choice.agent == "claude"
        assert not any("ladder" in line for line in choice.rationale)
