from __future__ import annotations

import argparse

from mac import cli


def _run(_tmp_path, *args):
    """Coverage-gate-visible parser execution for the admin event domain."""
    return cli.build_parser().parse_args(list(args))


class Plane:
    def __init__(self):
        self.calls = []

    def list_news(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {
            "schema": "mac.news.v1",
            "server_time": "2026-09-01T12:00:00+00:00",
            "cursor": 8,
            "items": [
                {
                    "sequence": 8,
                    "created_at": "2026-09-01T12:00:00+00:00",
                    "kind": "task",
                    "summary": "agent_worker moved Build from claimed to running",
                }
            ],
        }

    def stream_news(self, **kwargs):
        self.calls.append(("stream", kwargs))
        raise KeyboardInterrupt


def test_news_parser_is_under_the_existing_events_domain():
    args = _run(None, "admin", "events", "news", "--follow", "--project", "mac")
    assert args.func is cli.cmd_news
    assert args.follow is True
    assert args.project == "mac"


def test_news_recent_text_is_compact(monkeypatch, capsys):
    plane = Plane()
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_OUTPUT_JSON", False)

    cli.cmd_news(argparse.Namespace(project=None, limit=50, follow=False, poll_interval=2.0))

    output = capsys.readouterr().out
    assert "2026-09-01 12:00:00  task" in output
    assert "agent_worker moved Build" in output


def test_news_follow_resumes_after_the_initial_cursor(monkeypatch, capsys):
    plane = Plane()
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_OUTPUT_JSON", True)

    cli.cmd_news(argparse.Namespace(project="mac", limit=1, follow=True, poll_interval=2.0))

    assert plane.calls[1][0] == "stream"
    assert plane.calls[1][1]["after_sequence"] == 8
    assert plane.calls[1][1]["project"] == "mac"
    assert '"sequence": 8' in capsys.readouterr().out
