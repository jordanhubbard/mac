"""Agent liveness: the TTL must be able to fire, and a hub-side agent must not
age out of a fleet it never left.

Observed live on 2026-08-20 against the running hub:

    jordanh-worker1  offline  fungible  last seen  35 min ago
    jordanh-worker5  offline  fungible  last seen  52 min ago
    operator         offline  virtual   last seen  51 min ago
    hub-reviewer     idle     virtual   last seen  14 min ago

The fungible workers' HOSTS had been deleted hours earlier, yet the ledger said
they had been seen minutes ago -- and they were still registered, which is not
cosmetic: `deploy/deploy-mac-fleet.sh` enumerates every registered agent, so
four dead rows made the whole fleet undeployable. Three separate attempts
failed on `could not establish bounded direct SSH route` before the hub was
touched at all.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from mac.models import AgentStatus, parse_time, utcnow
from mac.services import ControlPlane


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _age_last_seen(cp: ControlPlane, agent_id: str, seconds: int) -> None:
    stale = (parse_time(utcnow()) - timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )
    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?", (stale, agent_id)
    )


def _agent(cp: ControlPlane, name: str, **kwargs):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(machine.id, name, agent_id="agent_%s" % name, **kwargs)


# --- the sweep must not refresh the clock it is reading ---------------------

def test_marking_an_agent_offline_does_not_refresh_its_last_seen(cp) -> None:
    """The defect that made the TTL unfireable.

    `mark_stale_agents_offline` marked agents offline by calling
    `heartbeat_agent`, which unconditionally restamps `last_seen_at`. So the
    sweep announced "this agent has not been seen since T" and, in the same
    call, recorded that it had just been seen. Every downstream age check --
    ephemeral expiry, reviewer staleness -- then measured from the sweep
    instead of from the agent.
    """
    agent = _agent(cp, "ghost")
    _age_last_seen(cp, agent.id, 3600)
    before = cp.get_agent(agent.id).last_seen_at

    marked = cp.mark_stale_agents_offline(60)

    assert [a.id for a in marked] == [agent.id]
    after = cp.get_agent(agent.id)
    assert after.status == AgentStatus.OFFLINE.value
    assert after.last_seen_at == before, (
        "the sweep restamped last_seen_at, so the agent looks freshly seen at "
        "the moment it was declared absent"
    )


def test_a_real_heartbeat_still_refreshes_last_seen(cp) -> None:
    """The fix must not break the thing heartbeats are for."""
    agent = _agent(cp, "live")
    _age_last_seen(cp, agent.id, 3600)
    before = cp.get_agent(agent.id).last_seen_at

    cp.heartbeat_agent(agent.id, status=AgentStatus.IDLE.value)

    assert cp.get_agent(agent.id).last_seen_at > before


# --- a disposable worker must actually be disposable -----------------------

def test_a_fungible_agent_expires_without_anyone_setting_a_resources_flag(cp) -> None:
    """Expiry keyed on `resources.ephemeral`, which no registration path
    writes -- while `instance_kind` is a real column carrying exactly this
    meaning. So the workers that most needed a TTL were the ones that could
    never get one, and they accumulated until they broke deployment.
    """
    agent = _agent(cp, "fungible1", instance_kind="fungible")
    _age_last_seen(cp, agent.id, 24 * 3600)

    expired = cp.expire_ephemeral_agents()

    assert agent.id in [a.id for a in expired]


def test_a_static_agent_is_never_expired_by_the_ttl(cp) -> None:
    """A bare-metal worker that is merely quiet must not be tombstoned."""
    agent = _agent(cp, "static1", instance_kind="static")
    _age_last_seen(cp, agent.id, 30 * 24 * 3600)

    expired = cp.expire_ephemeral_agents()

    assert agent.id not in [a.id for a in expired]


# --- a hub-side agent is alive exactly as long as the hub is ---------------

def test_a_virtual_agent_is_kept_alive_by_the_hub(cp) -> None:
    """`operator` and `hub-reviewer` are hub-side constructs: no process, so
    nothing to send a heartbeat, so they aged into `offline` while the hub that
    IS their liveness was running fine. Observed: operator offline, last seen
    51 minutes earlier.
    """
    agent = _agent(cp, "virtual1", resources={"virtual": True})
    _age_last_seen(cp, agent.id, 3600)

    cp.heartbeat_virtual_agents()

    refreshed = cp.get_agent(agent.id)
    assert refreshed.status != AgentStatus.OFFLINE.value
    assert cp.mark_stale_agents_offline(60) == []


def test_the_hub_does_not_vouch_for_agents_that_are_not_its_own(cp) -> None:
    """An artificial heartbeat is a claim about liveness. The hub may only make
    it for agents whose liveness it actually constitutes -- never for a real
    worker on a real host, which would report a dead machine as healthy.
    """
    real = _agent(cp, "real1")
    _age_last_seen(cp, real.id, 3600)
    before = cp.get_agent(real.id).last_seen_at

    cp.heartbeat_virtual_agents()

    assert cp.get_agent(real.id).last_seen_at == before
