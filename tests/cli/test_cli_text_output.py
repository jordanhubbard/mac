"""Interactive `mac` output is compact text; non-interactive output is JSON."""

from mac import cli

# A realistic 37-char task id (task_ + 32 hex chars).
_FULL_ID = "task_d95bcaee1234567890abcdef12345678"
_SHORT_ID = "task_d95bcaee"  # task_ + first 8 hex chars


def test_short_task_id_truncates_long_id():
    assert cli._short_task_id(_FULL_ID) == _SHORT_ID


def test_short_task_id_preserves_short_fixtures():
    # Test fixtures with < 8 hex chars are returned unchanged.
    assert cli._short_task_id("task_abc123") == "task_abc123"


def test_short_task_id_passthrough_non_task():
    assert cli._short_task_id("agent_x") == "agent_x"


def test_task_list_renders_short_id_by_default():
    """task list shows short ids (task_ + 8 hex) in text mode by default."""
    tasks = [
        {"id": _FULL_ID, "state": "open", "project": "mac", "title": "Do a thing"},
    ]
    cli._set_full_ids(False)
    out = cli._render_text(tasks)
    assert out.startswith(_SHORT_ID)
    assert _FULL_ID not in out
    assert "open" in out and "mac" in out and "Do a thing" in out


def test_task_list_renders_full_id_with_flag():
    """--full-ids restores the 37-char canonical id in text mode."""
    tasks = [
        {"id": _FULL_ID, "state": "open", "project": "mac", "title": "Do a thing"},
    ]
    cli._set_full_ids(True)
    out = cli._render_text(tasks)
    cli._set_full_ids(False)
    assert out.startswith(_FULL_ID)


def test_task_list_renders_one_liner_per_task():
    # Short fixture ids (< 8 hex) pass through unchanged; one line per task.
    tasks = [
        {"id": "task_abc123", "state": "open", "project": "mac", "title": "Do a thing"},
        {"id": "task_def456", "state": "completed", "project": "Aviation", "title": "Other"},
    ]
    cli._set_full_ids(False)
    out = cli._render_text(tasks)
    lines = out.splitlines()
    assert len(lines) == 2  # exactly one line per task
    assert lines[0].startswith("task_abc123")
    assert "open" in lines[0] and "mac" in lines[0] and "Do a thing" in lines[0]
    assert lines[1].startswith("task_def456")


def test_task_table_shows_dependency_arrays_for_roots_and_children():
    tasks = [
        {
            "id": "task_root",
            "state": "open",
            "title": "Root",
            "dependencies": [],
        },
        {
            "id": "task_child",
            "state": "waiting",
            "title": "Child",
            "dependencies": ["task_root", "task_peer"],
        },
    ]

    out = cli._render_task_table(
        tasks,
        show_project=False,
        color=False,
        width=120,
    )

    assert "DEPENDENCIES" in out.splitlines()[0]
    root_line = next(line for line in out.splitlines() if line.startswith("task_root"))
    child_line = next(line for line in out.splitlines() if line.startswith("task_child"))
    assert "[]" in root_line
    # The DEPENDENCIES column is capped so one task's blocker list cannot
    # starve every title (see tests/cli/test_task_table_column_widths.py). A
    # real short id is 13 characters, so the cap holds one plus an overflow
    # marker; these synthetic 9-character ids are two-thirds of a real pair.
    # What must survive is the first blocker AND the count of the rest.
    assert "task_root" in child_line
    assert "+1" in child_line


def test_task_table_prioritizes_active_and_attention_states():
    tasks = [
        {"id": "task_done", "state": "completed", "project": "mac", "title": "Done"},
        {"id": "task_open", "state": "open", "project": "mac", "title": "Queued"},
        {"id": "task_blocked", "state": "blocked", "project": "mac", "title": "Needs help"},
        {"id": "task_running", "state": "running", "project": "mac", "title": "In progress"},
    ]

    out = cli._render_task_table(tasks, show_project=False, color=False, width=100)

    assert out.index("task_running") < out.index("task_blocked")
    assert out.index("task_blocked") < out.index("task_open")
    assert out.index("task_open") < out.index("task_done")
    assert "TASK" in out.splitlines()[0]
    assert "STATE" in out.splitlines()[0]
    assert "PROJECT" not in out.splitlines()[0]
    assert "4 tasks" in out
    assert "● 1 running" in out
    assert "! 1 blocked" in out
    assert "○ 1 open" in out
    assert "✓ 1 completed" in out


def test_task_table_shows_project_for_all_project_view():
    tasks = [
        {"id": "task_a", "state": "open", "project": "mac", "title": "One"},
        {"id": "task_b", "state": "open", "project": "nanolang", "title": "Two"},
    ]

    out = cli._render_task_table(tasks, show_project=True, color=False, width=100)

    assert "PROJECT" in out.splitlines()[0]
    assert "mac" in out
    assert "nanolang" in out


def test_task_table_uses_color_only_when_enabled():
    tasks = [
        {"id": "task_run", "state": "running", "project": "mac", "title": "Run"},
        {"id": "task_fail", "state": "failed", "project": "mac", "title": "Fail"},
        {"id": "task_done", "state": "completed", "project": "mac", "title": "Done"},
    ]

    plain = cli._render_task_table(tasks, show_project=False, color=False, width=100)
    colored = cli._render_task_table(tasks, show_project=False, color=True, width=100)

    assert "\033[" not in plain
    assert "\033[1;36m" in colored  # running
    assert "\033[1;31m" in colored  # failed
    assert "\033[32m" in colored  # completed


def test_task_table_truncates_title_to_terminal_width():
    tasks = [
        {
            "id": _FULL_ID,
            "state": "needs_review",
            "project": "mac",
            "title": "A deliberately long title that cannot fit in a narrow terminal",
        }
    ]

    out = cli._render_task_table(tasks, show_project=False, color=False, width=60)
    task_line = next(line for line in out.splitlines() if line.startswith(_SHORT_ID))

    assert len(task_line) <= 60
    assert task_line.endswith("…")


def test_task_table_full_ids_align_header_rule_and_row():
    tasks = [{"id": _FULL_ID, "state": "open", "project": "mac", "title": "One"}]
    cli._set_full_ids(True)
    try:
        out = cli._render_task_table(tasks, show_project=False, color=False, width=100)
    finally:
        cli._set_full_ids(False)

    header, rule, row = out.splitlines()[:3]
    assert header.index("STATE") == rule.index("  ") + 2 == row.index("○ open")


class _ListedTask:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return dict(self.payload)


class _TaskListPlane:
    def __init__(self, tasks, routes=None, *, route_error=None):
        self.tasks = [_ListedTask(task) for task in tasks]
        self.routes = routes or {}
        self.route_error = route_error
        self.list_call = None
        self.route_call = None

    def list_tasks(self, state, *, project, limit, view):
        self.list_call = {
            "state": state,
            "project": project,
            "limit": limit,
            "view": view,
        }
        return self.tasks


def test_cmd_task_list_json_preserves_dependency_arrays(monkeypatch):
    dependencies = ["task_parent"]
    plane = _TaskListPlane(
        [
            {
                "id": "task_child",
                "state": "waiting",
                "title": "Child",
                "dependencies": dependencies,
            }
        ]
    )
    printed = []
    args = cli.argparse.Namespace(state="waiting", limit=25, full_ids=False)
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_effective_read_project", lambda _args: "mac")
    monkeypatch.setattr(cli, "_print", printed.append)
    monkeypatch.setattr(cli, "_OUTPUT_JSON", True)

    cli.cmd_task_list(args)

    assert plane.list_call == {
        "state": "waiting",
        "project": "mac",
        "limit": 25,
        "view": "summary",
    }
    assert printed[0][0]["dependencies"] == dependencies
    assert plane.route_call is None


class _TTY:
    def __init__(self, value=True):
        self.value = value

    def isatty(self):
        return self.value


def test_terminal_color_detection_honors_tty_force_and_no_color():
    assert cli._terminal_color_enabled(_TTY(True), {"TERM": "xterm-256color"}) is True
    assert cli._terminal_color_enabled(_TTY(False), {"TERM": "xterm-256color"}) is False
    assert cli._terminal_color_enabled(_TTY(True), {"TERM": "dumb"}) is False
    assert cli._terminal_color_enabled(_TTY(False), {"FORCE_COLOR": "1"}) is True
    assert cli._terminal_color_enabled(_TTY(True), {"FORCE_COLOR": "0"}) is False
    assert (
        cli._terminal_color_enabled(_TTY(True), {"TERM": "xterm-256color", "NO_COLOR": ""}) is False
    )


def test_empty_list_is_none():
    assert cli._render_text([]) == "(none)"


def test_agent_one_liner():
    out = cli._render_text(
        [
            {
                "name": "rocky",
                "status": "idle",
                "instance_kind": "static",
                "capabilities": ["review"],
            }
        ]
    )
    assert out.startswith("rocky")
    assert "idle" in out
    assert "static" in out


def test_agent_one_liner_shows_fungible_instance_kind():
    out = cli._render_text(
        [
            {
                "name": "worker-one",
                "status": "idle",
                "instance_kind": "fungible",
                "capabilities": ["python"],
            }
        ]
    )
    assert "fungible" in out


def test_agent_one_liner_shows_measured_hardware():
    # Agents report resources.hardware (mac.hardware.v1) at registration; the
    # human list line surfaces it so operators see real HW without --json.
    agents = [
        {
            "name": "bullwinkle",
            "status": "idle",
            "current_task_id": None,
            "resources": {
                "hardware": {
                    "os": "linux",
                    "arch": "x86_64",
                    "cpu_count": 32,
                    "memory_mb": 188647,
                    "accelerator": "cuda",
                    "gpu": {"name": "NVIDIA GeForce RTX 5090", "vram_mb": 32607},
                }
            },
        },
        {
            "name": "rocky",
            "status": "busy",
            "current_task_id": "task_1",
            "resources": {
                "hardware": {
                    "os": "darwin",
                    "arch": "arm64",
                    "cpu_count": 12,
                    "memory_mb": 65536,
                    "accelerator": "metal",
                    "gpu": {"name": "Apple M4 Pro"},
                }
            },
        },
    ]
    lines = cli._render_text(agents).splitlines()
    assert "linux/x86_64" in lines[0] and "32c" in lines[0]
    assert "184G" in lines[0] and "RTX 5090 32G" in lines[0]
    assert "cuda" not in lines[0]  # implied by the NVIDIA gpu name
    assert "darwin/arm64" in lines[1] and "M4 Pro" in lines[1] and "metal" in lines[1]
    assert "▶ task_1" in lines[1]


def test_agent_one_liner_without_hardware_shows_dash():
    out = cli._render_text([{"name": "x", "status": "idle", "current_task_id": None}])
    assert out.startswith("x")
    assert "-" in out.split()


def test_task_show_wrapper_is_compact():
    detail = {
        "task": {
            "id": "task_x",
            "state": "completed",
            "project": "mac",
            "title": "T",
            "attempt_count": 1,
        },
        "evidence": [1, 2],
        "reviews": [{"verdict": "approved"}],
        "publications": [{"status": "published"}],
        "llm_usage": {
            "observed_route_count": 2,
            "resolved_models": ["azure/anthropic/claude-sonnet-4-6"],
            "providers": ["nvidia"],
            "total_tokens": 321,
        },
        "profile": {
            "kpis": {"model_latency_ms": 120000},
            "commands": {"duration_ms": 60000, "failure_count": 3},
            "provider_attempt_count": 4,
            "signals": [
                {
                    "code": "command_failure_churn",
                    "detail": "3 failed terminal command records",
                }
            ],
        },
    }
    out = cli._render_text(detail)
    assert out.splitlines()[0].startswith("task_x")
    assert "evidence: 2" in out and "reviews: 1" in out  # counts, not the full blob
    assert "claude-sonnet-4-6 via nvidia" in out
    assert "2 routes, 321 tokens" in out
    assert "profile: 2.0m model, 1.0m commands, 4 provider attempts" in out
    assert "signal: command_failure_churn" in out


def test_task_show_wrapper_includes_activity_narrative():
    detail = {
        "task": {
            "id": "task_x",
            "state": "failed",
            "project": "mac",
            "title": "T",
            "metadata": {
                "activity": [
                    {
                        "phase": "worker",
                        "actor": "agent_rocky",
                        "at": "2026-07-26T10:12:00.104330+00:00",
                        "summary": "No repository contract was attached.",
                    },
                    {
                        "phase": "diagnosis",
                        "actor": "dispatcher.tick",
                        "at": "2026-07-26T10:56:57.923710+00:00",
                        "summary": (
                            "Problem: retry budget exhausted.\n"
                            "Remediation: repair the contract, then reopen."
                        ),
                    },
                ]
            },
        },
        "evidence": [{"id": "ev_1"}],
        "reviews": [{"status": "rejected"}],
    }

    out = cli._render_text(detail)

    assert "Activity:" in out
    assert "worker / agent_rocky @ 2026-07-26T10:12:00" in out
    assert "No repository contract was attached." in out
    assert "diagnosis / dispatcher.tick @ 2026-07-26T10:56:57" in out
    assert "Problem: retry budget exhausted." in out
    assert "Remediation: repair the contract, then reopen." in out


def test_task_activity_renderer_handles_empty_and_partial_entries():
    empty = cli._task_activity_lines(
        {"metadata": "invalid"},
        include_empty=True,
    )
    assert "no activity recorded" in "\n".join(empty)

    partial = cli._task_activity_lines(
        {"metadata": {"activity": [None, {}]}},
    )
    assert partial == ["", "Activity:", "  • note"]


def test_task_show_wrapper_discloses_missing_model_attribution():
    detail = {
        "task": {"id": "task_x", "state": "running", "title": "T"},
        "llm_usage": {"observed_route_count": 0},
    }

    assert "llm: no attributed model calls recorded" in cli._render_text(detail)


def test_json_flag_strips_position_independently(monkeypatch):
    # `mac task list --json` and `mac --json task list` both enable JSON mode.
    seen = {}

    def fake_build_parser():
        import argparse

        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command", required=True)
        sp = sub.add_parser("noop")
        sp.set_defaults(func=lambda a: seen.setdefault("ran", True))
        return p

    monkeypatch.setattr(cli, "build_parser", fake_build_parser)
    monkeypatch.setattr(cli, "_stdout_is_interactive", lambda: True)

    cli.main(["noop", "--json"])

    assert cli._OUTPUT_JSON is True
    assert seen["ran"] is True


def test_main_defaults_to_json_when_stdout_is_not_a_terminal(monkeypatch):
    seen = {}

    def fake_build_parser():
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        command = sub.add_parser("noop")
        command.set_defaults(func=lambda _args: seen.setdefault("json", cli._OUTPUT_JSON))
        return parser

    monkeypatch.setattr(cli, "build_parser", fake_build_parser)
    monkeypatch.setattr(cli, "_stdout_is_interactive", lambda: False)
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setenv("GH_PAGER", "delta")

    cli.main(["noop"])

    assert seen["json"] is True
    assert cli.os.environ["PAGER"] == "cat"
    assert cli.os.environ["GH_PAGER"] == "cat"


def test_main_keeps_text_for_an_interactive_terminal(monkeypatch):
    seen = {}

    def fake_build_parser():
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        command = sub.add_parser("noop")
        command.set_defaults(func=lambda _args: seen.setdefault("json", cli._OUTPUT_JSON))
        return parser

    monkeypatch.setattr(cli, "build_parser", fake_build_parser)
    monkeypatch.setattr(cli, "_stdout_is_interactive", lambda: True)

    cli.main(["noop"])

    assert seen["json"] is False
