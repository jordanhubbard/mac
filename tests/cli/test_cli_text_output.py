"""The `mac` CLI defaults to compact human-readable text; `--json` switches to
JSON. These lock the text renderer (a pure function, independent of the global
output-mode flag the rest of the suite forces to JSON)."""

from mac import cli


def test_task_list_renders_one_liner_per_task():
    tasks = [
        {"id": "task_abc123", "state": "open", "project": "mac", "title": "Do a thing"},
        {"id": "task_def456", "state": "completed", "project": "Aviation", "title": "Other"},
    ]
    out = cli._render_text(tasks)
    lines = out.splitlines()
    assert len(lines) == 2  # exactly one line per task
    assert lines[0].startswith("task_abc123")
    assert "open" in lines[0] and "mac" in lines[0] and "Do a thing" in lines[0]
    assert lines[1].startswith("task_def456")


def test_empty_list_is_none():
    assert cli._render_text([]) == "(none)"


def test_agent_one_liner():
    out = cli._render_text([{"name": "rocky", "status": "idle", "capabilities": ["review"]}])
    assert out.startswith("rocky")
    assert "idle" in out


def test_task_show_wrapper_is_compact():
    detail = {
        "task": {"id": "task_x", "state": "completed", "project": "mac", "title": "T", "attempt_count": 1},
        "evidence": [1, 2],
        "reviews": [{"verdict": "approved"}],
        "publications": [{"status": "published"}],
    }
    out = cli._render_text(detail)
    assert out.splitlines()[0].startswith("task_x")
    assert "evidence: 2" in out and "reviews: 1" in out  # counts, not the full blob


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
    cli._set_output_json(False)
    cli.main(["noop", "--json"])
    assert cli._OUTPUT_JSON is True
    cli._set_output_json(False)
