"""Small backend-neutral guards for authoritative work-package task links."""

from __future__ import annotations

from typing import Optional

from mac.models import (
    TransitionError,
    ValidationError,
    WorkPackageEpoch,
    WorkPackageTaskLink,
    json_dumps,
    new_id,
    utcnow,
)
from mac.store import Store


def get_work_package_task_link(
    store: Store, task_id: str
) -> Optional[WorkPackageTaskLink]:
    """Return the authoritative package link for a task, if one exists."""

    row = store.query_one(
        "SELECT * FROM work_package_task_links WHERE task_id = ?", (task_id,)
    )
    if row is None:
        return None
    return WorkPackageTaskLink(
        task_id=row["task_id"],
        package_id=row["package_id"],
        plan_version=int(row["plan_version"]),
        epoch=int(row["epoch"]),
        node_key=row["node_key"],
        node_generation=int(row["node_generation"]),
        declared_effects_digest=row["declared_effects_digest"],
        contract_digest=row["contract_digest"],
        input_digest=row["input_digest"],
        node_state=row["node_state"],
        created_at=row["created_at"],
    )


def guard_generic_task_mutation(store: Store, task_id: str, operation: str) -> None:
    """Fail closed when a generic task mutation targets package-owned work.

    Package coordinators update the task, node state, history, WIP token, and
    integration state in one transaction. Generic task paths cannot preserve
    those invariants and must call this guard before writing.
    """

    link = get_work_package_task_link(store, task_id)
    if link is None:
        return
    operation_name = str(operation or "mutation").strip() or "mutation"
    raise ValidationError(
        "%s is not allowed for work-package task %s (%s/%s); "
        "use a package-aware transaction"
        % (operation_name, task_id, link.package_id, link.node_key)
    )


def swap_work_package_epoch(
    store: Store,
    *,
    package_id: str,
    expected_plan_version: int,
    expected_epoch: int,
    new_plan_version: int,
    new_epoch: int,
    planning_base_ref: str,
    planning_base_sha: str,
    actor: str,
    reason: str,
) -> WorkPackageEpoch:
    """Atomically stage and activate a new package epoch with CAS fencing."""

    if new_epoch <= expected_epoch:
        raise ValidationError("new work-package epoch must increase monotonically")
    now = utcnow()
    with store.transaction() as conn:
        package = conn.execute(
            "SELECT state, current_plan_version, current_epoch "
            "FROM work_packages WHERE id = ?",
            (package_id,),
        ).fetchone()
        if package is None:
            raise ValidationError("work package not found: %s" % package_id)
        if (
            package["state"] != "active"
            or int(package["current_plan_version"]) != expected_plan_version
            or int(package["current_epoch"]) != expected_epoch
        ):
            raise TransitionError("work package epoch CAS did not match current state")
        conn.execute(
            "INSERT INTO work_package_epochs ("
            "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
            "status, reason, created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                package_id,
                new_epoch,
                new_plan_version,
                planning_base_ref,
                planning_base_sha,
                "staged",
                reason,
                actor,
                now,
            ),
        )
        cursor = conn.execute(
            "UPDATE work_packages SET state = ?, updated_at = ? "
            "WHERE id = ? AND state = ? AND current_plan_version = ? AND current_epoch = ?",
            (
                "replanning",
                now,
                package_id,
                "active",
                expected_plan_version,
                expected_epoch,
            ),
        )
        if cursor.rowcount != 1:
            raise TransitionError("work package epoch CAS lost during staged swap")
        cursor = conn.execute(
            "UPDATE work_package_epochs SET status = ?, superseded_at = ? "
            "WHERE package_id = ? AND epoch = ? AND plan_version = ? AND status = ?",
            (
                "superseded",
                now,
                package_id,
                expected_epoch,
                expected_plan_version,
                "active",
            ),
        )
        if cursor.rowcount != 1:
            raise TransitionError("current work-package epoch was not active")
        cursor = conn.execute(
            "UPDATE work_package_epochs SET status = ? "
            "WHERE package_id = ? AND epoch = ? AND plan_version = ? AND status = ?",
            ("active", package_id, new_epoch, new_plan_version, "staged"),
        )
        if cursor.rowcount != 1:
            raise TransitionError("staged work-package epoch was not activated")
        cursor = conn.execute(
            "UPDATE work_packages SET state = ?, current_plan_version = ?, "
            "current_epoch = ?, updated_at = ? "
            "WHERE id = ? AND state = ? AND current_plan_version = ? AND current_epoch = ?",
            (
                "active",
                new_plan_version,
                new_epoch,
                now,
                package_id,
                "replanning",
                expected_plan_version,
                expected_epoch,
            ),
        )
        if cursor.rowcount != 1:
            raise TransitionError("work package epoch CAS lost during activation")
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM work_package_history WHERE package_id = ?",
            (package_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO work_package_history ("
            "id, package_id, seq, event_type, actor, plan_version, epoch, detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("wph"),
                package_id,
                int(seq_row["next_seq"]),
                "work_package.epoch_swapped",
                actor,
                new_plan_version,
                new_epoch,
                json_dumps(
                    {
                        "previous_plan_version": expected_plan_version,
                        "previous_epoch": expected_epoch,
                        "reason": reason,
                    }
                ),
                now,
            ),
        )
    return WorkPackageEpoch(
        package_id=package_id,
        epoch=new_epoch,
        plan_version=new_plan_version,
        planning_base_ref=planning_base_ref,
        planning_base_sha=planning_base_sha,
        status="active",
        reason=reason,
        created_by=actor,
        created_at=now,
        superseded_at=None,
    )


__all__ = [
    "get_work_package_task_link",
    "guard_generic_task_mutation",
    "swap_work_package_epoch",
]
