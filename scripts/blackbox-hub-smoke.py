#!/usr/bin/env python3
"""Exercise a running deployable MAC hub using only its public HTTP API."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any


def request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw.decode("utf-8", errors="replace")
        return exc.code, parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8789")
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)

    health_status, health = request(args.url, "GET", "/health")
    if health_status != 200 or health != {"status": "ok"}:
        print(f"blackbox hub: health failed: {health_status} {health!r}", file=sys.stderr)
        return 1

    unauthorized_status, _ = request(args.url, "GET", "/tasks")
    if unauthorized_status not in {401, 403}:
        print(
            f"blackbox hub: unauthenticated /tasks returned {unauthorized_status}, expected 401/403",
            file=sys.stderr,
        )
        return 1

    title = "blackbox-smoke-" + uuid.uuid4().hex
    create_status, task = request(
        args.url,
        "POST",
        "/tasks",
        token=args.token,
        payload={
            "title": title,
            "project": "blackbox-smoke",
            "required_capabilities": ["python"],
            "metadata": {"no_dispatch": True, "blackbox_smoke": True},
        },
    )
    if create_status not in {200, 201} or not isinstance(task, dict) or not task.get("id"):
        print(f"blackbox hub: task creation failed: {create_status} {task!r}", file=sys.stderr)
        return 1

    show_status, shown = request(args.url, "GET", f"/tasks/{task['id']}", token=args.token)
    shown_task = shown.get("task", shown) if isinstance(shown, dict) else None
    if (
        show_status != 200
        or not isinstance(shown_task, dict)
        or shown_task.get("title") != title
        or shown_task.get("state") != "open"
    ):
        print(f"blackbox hub: task readback failed: {show_status} {shown!r}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "schema": "mac.blackbox_hub_smoke.v1",
                "status": "pass",
                "health": health,
                "authentication": "enforced",
                "task_round_trip": task["id"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
