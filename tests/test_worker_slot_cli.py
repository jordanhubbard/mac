from mac.cli import build_parser


TICKET = "a" * 64


def test_worker_slot_is_registered_under_admin():
    args = build_parser().parse_args(
        [
            "admin",
            "worker-slot",
            "wait",
            "--webmux",
            "/trusted/webmux",
            "--sha256",
            "0" * 64,
            "--node-id",
            "sparky",
            "--owner",
            "codex",
            "--session-id",
            "session-1",
            "--incarnation",
            "inc-1",
            "--resource",
            "node:sparky:heavy",
            "--record-id",
            TICKET,
            "--request-id",
            "wait-0001",
            "--timeout-seconds",
            "20",
        ]
    )
    assert args.verb == "wait"
    assert args.timeout_seconds == 20


def test_worker_slot_uses_bounded_integer_errors():
    parser = build_parser()
    with __import__("pytest").raises(SystemExit):
        parser.parse_args(
            [
                "admin",
                "worker-slot",
                "enqueue",
                "--webmux",
                "/trusted/webmux",
                "--sha256",
                "0" * 64,
                "--node-id",
                "sparky",
                "--owner",
                "codex",
                "--session-id",
                "session-1",
                "--incarnation",
                "inc-1",
                "--resource",
                "node:sparky:heavy",
                "--request-id",
                "enqueue-0001",
                "--wait-seconds",
                "86401",
            ]
        )
