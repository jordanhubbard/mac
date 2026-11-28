"""Administrative CLI for authoritative WebMux worker admission."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mac.worker_slot import WorkerSlotClient, WorkerSlotConfig, WorkerSlotError


def _bounded_integer(minimum: int, maximum: int):
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return value

    return parse


def _client(args: argparse.Namespace) -> WorkerSlotClient:
    return WorkerSlotClient(
        WorkerSlotConfig(
            Path(args.webmux),
            args.sha256,
            args.node_id,
            args.owner,
            args.session_id,
            args.incarnation,
        )
    )


def _run(args: argparse.Namespace) -> None:
    client = _client(args)
    if args.verb == "wait":
        result = client.wait(args.resource, args.record_id, args.request_id, args.timeout_seconds)
    else:
        fields = {
            key: value
            for key, value in {
                "record_id": getattr(args, "record_id", None),
                "lease_seconds": getattr(args, "lease_seconds", None),
                "wait_seconds": getattr(args, "wait_seconds", None),
                "fence": getattr(args, "fence", None),
            }.items()
            if value is not None
        }
        result = client.action(
            {
                "enqueue": "enqueue-reservation",
                "status": "inspect-reservation",
                "acquire": "acquire-reservation",
                "cancel": "cancel-reservation",
                "renew": "renew-reservation",
                "release": "release-reservation",
            }[args.verb],
            args.resource,
            args.request_id,
            **fields,
        )
    print(json.dumps(result, sort_keys=True))


def _safe(args: argparse.Namespace) -> None:
    try:
        _run(args)
    except WorkerSlotError as exc:
        raise SystemExit(str(exc)) from None


def register(sub: Any) -> None:
    group = sub.add_parser(
        "worker-slot", help="use WebMux FIFO admission for host heavy work"
    ).add_subparsers(dest="verb", required=True)
    for verb in ("enqueue", "status", "wait", "acquire", "cancel", "renew", "release"):
        parser = group.add_parser(verb)
        parser.add_argument("--webmux", required=True, help="trusted absolute WebMux executable")
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--node-id", required=True)
        parser.add_argument("--owner", required=True)
        parser.add_argument("--session-id", required=True)
        parser.add_argument("--incarnation", required=True)
        parser.add_argument("--resource", required=True)
        parser.add_argument("--request-id", required=True)
        if verb != "enqueue":
            parser.add_argument("--record-id", required=True)
        if verb == "enqueue":
            parser.add_argument("--lease-seconds", type=_bounded_integer(15, 900), default=120)
            parser.add_argument("--wait-seconds", type=_bounded_integer(15, 86400), default=900)
        if verb == "wait":
            parser.add_argument("--timeout-seconds", type=_bounded_integer(1, 30), default=20)
        if verb in {"renew", "release"}:
            parser.add_argument("--fence", type=_bounded_integer(1, 2**63 - 1), required=True)
        if verb == "renew":
            parser.add_argument("--lease-seconds", type=_bounded_integer(15, 900), default=120)
        parser.set_defaults(func=_safe)
