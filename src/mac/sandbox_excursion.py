"""A sandbox excursion becomes a task against the project that caused it.

A repository contract declares the commands a repo needs
(``toolchain.required_commands``). The sandbox image is supposed to cover them.
When it does not, the executor provisions the missing tool into a task-local
``.mac-toolchain`` so the work still runs -- and records exactly what happened:

    {"schema": "mac.sandbox_environment_delta.v1",
     "commands": [...required...],
     "missing_after": [...still absent after provisioning...],
     "reason": "repository_contract.toolchain.required_commands"}

That record has always been written and never read. It travels back inside
``mac.sandbox_verification.v1``, lands on a check item, and stops. The sandbox
is disposable, so every excursion was measured and then discarded.

The visible consequence is in the Containerfile, whose package list is a manual
ledger of exactly these incidents, transcribed by a human months later:

    "libssl-dev: nanolang's src/sign.c #includes <openssl/evp.h> ... without it
     a coding agent will destructively stub sign.c just to compile"

That comment is an excursion someone eventually noticed. This module is the
consumer that makes noticing automatic: the excursion is filed against the
project that caused it, carrying the delta, so a human or an LLM can audit that
specific case and decide whether the CONTRACT should gain the tool or the
SPECIFICATION should be tightened so the tool is not needed.

Two properties matter more than the filing itself:

* **The excursion never blocks the work.** Provisioning stays exactly as it is.
  A contract may be wrong or stale, and a sandbox that has to reach outside its
  contract must still succeed -- the task is a report, not a gate.
* **One missing tool is one task.** Deduplicated per (project, command), so a
  tool absent across a hundred tasks does not file a hundred tickets. Noise is
  what stops people reading, and this only pays off if it is read.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

EXCURSION_SCHEMA = "mac.sandbox_excursion.v1"
DELTA_SCHEMA = "mac.sandbox_environment_delta.v1"

#: Marker on a filed task, so the dedupe lookup does not depend on title text.
EXCURSION_METADATA_KEY = "sandbox_excursion"


def excursion_from_delta(
    delta: Any, *, project: Optional[str], task_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The auditable facts of one excursion, or None when nothing escaped.

    An excursion is a command the repository contract REQUIRED that the sandbox
    image did not supply. ``missing_after`` (still absent even after
    provisioning) is the strongest signal, but a command that had to be
    provisioned at all is equally a gap between the contract and the image --
    the work succeeded, and the image still did not cover the contract.
    """
    if not isinstance(delta, Mapping):
        return None
    if str(delta.get("schema") or "").strip() != DELTA_SCHEMA:
        return None
    required = [str(item).strip() for item in (delta.get("commands") or []) if str(item).strip()]
    missing = [
        str(item).strip() for item in (delta.get("missing_after") or []) if str(item).strip()
    ]
    provisioned = [
        str(item).strip() for item in (delta.get("provisioned") or []) if str(item).strip()
    ]
    escaped = sorted(set(missing) | set(provisioned))
    if not escaped:
        return None
    return {
        "schema": EXCURSION_SCHEMA,
        "project": project or None,
        "task_id": task_id,
        "required_commands": sorted(set(required)),
        "escaped_commands": escaped,
        # Kept apart because they mean different things to a reviewer: one is a
        # gap the sandbox papered over, the other is a gap it could not.
        "missing_after": sorted(set(missing)),
        "provisioned": sorted(set(provisioned)),
        "reason": str(delta.get("reason") or ""),
        "toolchain_root": str(delta.get("toolchain_root") or ""),
    }


def excursion_title(project: Optional[str], command: str) -> str:
    return "Sandbox excursion: %s requires %r, which the image does not provide" % (
        project or "unknown project",
        command,
    )


def excursion_description(excursion: Mapping[str, Any], command: str) -> str:
    """What a reviewer needs to decide contract-vs-specification."""
    lines = [
        "The sandbox image does not supply %r, which %s's repository contract "
        "declares in toolchain.required_commands."
        % (command, excursion.get("project") or "this project"),
        "",
        "The work was NOT blocked: the executor provisioned it into a "
        "task-local .mac-toolchain and the task continued. This is a report so "
        "the gap can be audited, not a failure.",
        "",
        "Observed on task %s" % (excursion.get("task_id") or "(unrecorded)"),
        "  required by contract : %s" % ", ".join(excursion.get("required_commands") or []),
        "  escaped the image    : %s" % ", ".join(excursion.get("escaped_commands") or []),
    ]
    if excursion.get("missing_after"):
        lines.append(
            "  still absent AFTER provisioning: %s" % ", ".join(excursion["missing_after"])
        )
        lines.append("    (this one the sandbox could not paper over; the task ran without it)")
    lines.extend(
        [
            "",
            "THE DECISION THIS TASK EXISTS FOR, either answer closes it:",
            "",
            "  1. The contract is RIGHT and the image is behind. Add %r to the "
            "sandbox bill of materials so every task gets it, instead of each "
            "task provisioning it again." % command,
            "",
            "  2. The contract is WRONG or too loose. Tighten the specification "
            "so the repo does not need %r, and remove it from "
            "toolchain.required_commands." % command,
            "",
            "Deciding neither leaves every future task paying the provisioning "
            "cost and the image drifting further from the contract that is "
            "supposed to be authoritative.",
        ]
    )
    return "\n".join(lines)


def excursion_metadata(excursion: Mapping[str, Any], command: str) -> Dict[str, Any]:
    return {
        EXCURSION_METADATA_KEY: {
            "schema": EXCURSION_SCHEMA,
            "command": command,
            "project": excursion.get("project"),
            "observed_on_task_id": excursion.get("task_id"),
            "required_commands": list(excursion.get("required_commands") or []),
            "missing_after": list(excursion.get("missing_after") or []),
            "provisioned": list(excursion.get("provisioned") or []),
            "reason": excursion.get("reason"),
        }
    }


def dedupe_key(project: Optional[str], command: str) -> str:
    """One missing tool is one task, however many tasks trip over it."""
    return "%s::%s" % (project or "unknown", command)


def excursion_commands(excursion: Mapping[str, Any]) -> List[str]:
    return list(excursion.get("escaped_commands") or [])


def existing_excursion_commands(tasks: Sequence[Any]) -> set:
    """Commands already filed, read from the marker rather than the title.

    Titles get edited; a dedupe that keys off them starts filing duplicates the
    first time someone rewords one.
    """
    seen = set()
    for task in tasks:
        record = task.to_dict() if hasattr(task, "to_dict") else task
        if not isinstance(record, Mapping):
            continue
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        marker = metadata.get(EXCURSION_METADATA_KEY)
        if not isinstance(marker, Mapping):
            continue
        command = str(marker.get("command") or "").strip()
        if command:
            seen.add(dedupe_key(marker.get("project"), command))
    return seen
