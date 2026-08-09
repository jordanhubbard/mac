"""Roll a new sandbox image onto each worker, one drained worker at a time.

Today the image goes out through deploy-mac-fleet.sh, which pushes to nodes
from outside the control plane. That has no idea what any worker is doing. A
worker mid-task gets its sandbox replaced underneath the task -- the tools the
running work resolved at start are gone, and the failure surfaces as the task
misbehaving rather than as a deployment that ran at the wrong moment.

A rollout is work that mutates the worker, so it is exactly what the sync
execution mode exists for. This files ONE sync task per agent:

* it does not start until that agent has drained,
* nothing else runs on that agent while it does,
* the agent stops accepting new async work the moment it is queued,
* and each agent is independent, so the fleet rolls rather than stops.

Two limits, both deliberate:

* This does not BUILD or publish an image. It takes a digest that has already
  been through the reviewed publication path. Deriving a BOM is mechanical;
  putting a new image into the security boundary is not, and an automated path
  from "a contract changed" to "every worker is running a new image" is the
  supply-chain hole the frozen-input hash exists to close.
* It never mutates a running worker directly. It files work the worker itself
  executes, so a rollout is visible, auditable, and refusable like anything
  else in the ledger.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

ROLLOUT_SCHEMA = "mac.sandbox_rollout.v1"

#: Marker on a filed rollout task, so "is this worker already scheduled for
#: this digest" is answered from structure rather than by matching a title.
ROLLOUT_METADATA_KEY = "sandbox_rollout"

#: The digest form the deploy path already requires. Accepting a tag here would
#: make the rollout non-reproducible: a tag can be repointed after review, so
#: what shipped and what was reviewed could differ with nothing recording it.
IMAGE_DIGEST_PATTERN = re.compile(
    r"^ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}$"
)


class RolloutError(ValueError):
    """The rollout was refused before anything was filed."""


def validate_image_ref(image_ref: str) -> str:
    text = str(image_ref or "").strip()
    if not IMAGE_DIGEST_PATTERN.match(text):
        raise RolloutError(
            "a sandbox rollout requires the immutable GHCR digest "
            "(ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<64 hex>), not "
            "%r. A tag can be repointed after review, so what ships and what "
            "was reviewed could differ with nothing recording it." % text
        )
    return text


def rollout_title(agent_name: str, image_ref: str) -> str:
    return "Sandbox rollout: %s -> %s" % (agent_name, image_ref.split("@", 1)[-1][:19])


def rollout_description(agent_name: str, image_ref: str, bom: Mapping[str, Any]) -> str:
    packages = ", ".join(bom.get("packages") or []) or "(none recorded)"
    return "\n".join(
        [
            "Install the reviewed OpenShell sandbox image on %s." % agent_name,
            "",
            "  image: %s" % image_ref,
            "",
            "This is a SYNCHRONOUS task. It will not start until this worker "
            "has drained, and no other work runs on it until this finishes. "
            "That is not a precaution: replacing the sandbox under a running "
            "task removes the tools that task resolved when it started, and it "
            "fails in a way that looks like the task's fault.",
            "",
            "The image covers this bill of materials, derived from every "
            "registered repository contract:",
            "",
            "  %s" % packages,
            "",
            "If this worker cannot pull or verify the digest, FAIL the task "
            "rather than falling back to the previous image. A fleet that is "
            "half-rolled and says nothing is worse than one that is not rolled "
            "at all, because nothing downstream can tell which sandbox a task "
            "actually ran in.",
        ]
    )


def rollout_metadata(agent_id: str, image_ref: str, bom: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "execution_mode": "sync",
        # The barrier is per agent, and a rollout with no agent is meaningless,
        # so this is set here rather than left to the caller.
        "target_agent_id": agent_id,
        ROLLOUT_METADATA_KEY: {
            "schema": ROLLOUT_SCHEMA,
            "image": image_ref,
            "agent_id": agent_id,
            "bom_schema": bom.get("schema"),
            "packages": list(bom.get("packages") or []),
        },
    }


def scheduled_rollouts(tasks: Sequence[Any]) -> set:
    """(agent, image) pairs already filed and not finished.

    Read from the marker, not the title: a rollout re-filed on every hub tick
    would queue a barrier per tick, and since barriers quiesce their agent, the
    worker would stop taking work permanently.
    """
    seen = set()
    for task in tasks:
        record = task.to_dict() if hasattr(task, "to_dict") else task
        if not isinstance(record, Mapping):
            continue
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        marker = metadata.get(ROLLOUT_METADATA_KEY)
        if not isinstance(marker, Mapping):
            continue
        agent_id = str(marker.get("agent_id") or "").strip()
        image = str(marker.get("image") or "").strip()
        if agent_id and image:
            seen.add((agent_id, image))
    return seen


def plan_rollout(
    agents: Sequence[Any],
    image_ref: str,
    *,
    bom: Optional[Mapping[str, Any]] = None,
    already_scheduled: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """One barrier task per agent that is not already scheduled for this image.

    Every agent, including the ones that are busy: busy is the normal state,
    and skipping them would roll the idle half of the fleet and quietly leave
    the working half behind -- the workers doing the most work would be the
    ones running the oldest sandbox.
    """
    image_ref = validate_image_ref(image_ref)
    manifest = dict(bom or {})
    scheduled = set(already_scheduled or ())
    plan: List[Dict[str, Any]] = []
    for agent in agents:
        record = agent.to_dict() if hasattr(agent, "to_dict") else agent
        if not isinstance(record, Mapping):
            continue
        agent_id = str(record.get("id") or "").strip()
        if not agent_id or (agent_id, image_ref) in scheduled:
            continue
        name = str(record.get("name") or agent_id)
        plan.append(
            {
                "agent_id": agent_id,
                "agent_name": name,
                "title": rollout_title(name, image_ref),
                "description": rollout_description(name, image_ref, manifest),
                "metadata": rollout_metadata(agent_id, image_ref, manifest),
            }
        )
    return plan
