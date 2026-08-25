from mac.agent_inner_loop import PersistentAgentLoop


def test_productive_iteration_continues_immediately_and_resets_backoff():
    loop = PersistentAgentLoop(base_delay_seconds=1, max_delay_seconds=5)

    assert [loop.observe("no_task").delay_seconds for _ in range(4)] == [1, 2, 4, 5]

    decision = loop.observe("submitted_for_review")
    assert decision.mode == "continuing"
    assert decision.delay_seconds == 0
    assert decision.to_dict()["schema"] == "mac.agent_inner_loop.v1"

    assert loop.observe("no_task").delay_seconds == 1


def test_explicit_hub_hold_suspends_at_cap_without_accumulating_a_streak():
    loop = PersistentAgentLoop(base_delay_seconds=0.5, max_delay_seconds=4)

    decision = loop.observe("held")

    assert decision.mode == "waiting"
    assert decision.delay_seconds == 4
    assert decision.streak == 0
    assert loop.observe("no_task").delay_seconds == 0.5


def test_errors_back_off_but_a_successful_wake_recovers_immediately():
    loop = PersistentAgentLoop(base_delay_seconds=2, max_delay_seconds=5)

    assert [loop.observe("error").delay_seconds for _ in range(3)] == [2, 4, 5]
    assert loop.observe("completed").delay_seconds == 0
    assert loop.observe("error").delay_seconds == 2


def test_zero_delay_keeps_bounded_test_workers_deterministic():
    loop = PersistentAgentLoop(base_delay_seconds=0, max_delay_seconds=0)

    assert loop.observe("no_task").delay_seconds == 0
    assert loop.observe("error").delay_seconds == 0


def test_restart_is_terminal_for_the_local_loop():
    loop = PersistentAgentLoop(base_delay_seconds=1, max_delay_seconds=5)

    decision = loop.observe("self_update_restart")

    assert decision.mode == "stopped"
    assert decision.delay_seconds == 0
