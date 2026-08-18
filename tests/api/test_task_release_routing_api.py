"""HTTP regression tests for `POST /tasks/{id}/release` on staged tasks that
carry controller-owned publication routing metadata (release-fix /
regression_tests node).

Before the fix, releasing a staged task whose metadata already contained
``publication_route``/``publication_lane``/``managed_fast_lane``
returned HTTP 400 because the release routed the metadata through the
user-input guard.  These prove the endpoint now:

1. releases a plain staged task (200) and clears ``no_dispatch``.
2. releases a staged task with routing metadata (200, no HTTP 400) and
   un-stages it.
3. leaves the persisted metadata differing only by removal of ``no_dispatch``
   (controller-owned fields byte-for-byte unchanged).
4. treats release of an un-held task as a 200 no-op.
5. still rejects operator-supplied routing metadata on task create (HTTP 400) —
   the guard is not globally weakened.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


ROUTING_KEYS = (
    "publication_route",
    "publication_lane",
    "managed_fast_lane",
)


def _app_and_cp():
    cp = ControlPlane.in_memory()
    return TestClient(create_app(control_plane=cp)), cp


def _persisted_metadata(cp, task_id):
    row = cp.store.query_one(
        "SELECT metadata FROM tasks WHERE id = ?", (task_id,)
    )
    return json.loads(row["metadata"])


def _attach_controller_routing(cp, task_id):
    md = _persisted_metadata(cp, task_id)
    md["publication_route"] = {
        "schema": "mac.task_publication_route.v1",
        "lane": "legacy",
        "route_state": "legacy_compatibility",
    }
    md["publication_lane"] = "legacy"
    md["managed_fast_lane"] = {
        "schema": "mac.managed_single_task.route.v1",
        "activation": "legacy_compatibility",
    }
    cp.store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps(md), task_id),
    )


def _create_staged(client):
    resp = client.post(
        "/tasks", json={"title": "staged via api", "metadata": {"no_dispatch": True}}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# Case 1 -------------------------------------------------------------------


def test_release_plain_staged_task_over_http():
    client, cp = _app_and_cp()
    task = _create_staged(client)

    resp = client.post(f"/tasks/{task['id']}/release", json={"actor": "operator"})
    assert resp.status_code == 200, resp.text

    after = _persisted_metadata(cp, task["id"])
    assert "no_dispatch" not in after


# Cases 2 & 3 --------------------------------------------------------------


def test_release_after_routing_attached_returns_200_and_preserves_fields():
    client, cp = _app_and_cp()
    task = _create_staged(client)
    _attach_controller_routing(cp, task["id"])

    before = _persisted_metadata(cp, task["id"])
    assert before["no_dispatch"] is True

    resp = client.post(f"/tasks/{task['id']}/release", json={"actor": "operator"})
    # Case 2: no HTTP 400 — the release succeeds.
    assert resp.status_code == 200, resp.text
    assert resp.json()["metadata"].get("no_dispatch") is None

    after = _persisted_metadata(cp, task["id"])
    assert "no_dispatch" not in after

    # Case 3: differs only by no_dispatch removal; routing fields byte-identical.
    expected = dict(before)
    expected.pop("no_dispatch", None)
    assert after == expected
    for key in ROUTING_KEYS:
        assert after[key] == before[key]


# Case 4 -------------------------------------------------------------------


def test_release_unheld_task_is_http_noop():
    client, cp = _app_and_cp()
    resp = client.post("/tasks", json={"title": "not staged"})
    assert resp.status_code == 200, resp.text
    task = resp.json()
    before = _persisted_metadata(cp, task["id"])

    release = client.post(f"/tasks/{task['id']}/release", json={"actor": "operator"})
    assert release.status_code == 200, release.text

    after = _persisted_metadata(cp, task["id"])
    assert after == before


# Case 5 -------------------------------------------------------------------


def test_create_rejects_operator_routing_metadata_over_http():
    client, _cp = _app_and_cp()
    for key in ROUTING_KEYS:
        resp = client.post(
            "/tasks",
            json={"title": "operator routing", "metadata": {key: {"lane": "legacy"}}},
        )
        assert resp.status_code == 400, (key, resp.text)
        assert "control-plane-owned" in resp.json()["detail"]
