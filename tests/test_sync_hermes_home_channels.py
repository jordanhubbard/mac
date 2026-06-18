import json
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "sync-hermes-home-channels.py"


def run_sync(tmp_path, monkeypatch, accounts=None):
    accounts_path = tmp_path / "slack_accounts.json"
    home_path = tmp_path / "slack_home_channels.json"
    routes_path = tmp_path / "slack_channel_teams.json"
    report_path = tmp_path / "report.json"
    if accounts is not None:
        accounts_path.write_text(json.dumps(accounts), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(accounts_path),
            str(home_path),
            str(routes_path),
            str(report_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return result, home_path, routes_path, report_path


def test_home_channel_static_json_writes_home_and_route_files(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_HERMES_SLACK_HOME_CHANNELS_JSON", json.dumps([
        {
            "name": "omgjkh",
            "team_id": "T123",
            "channel_id": "C456",
            "channel_name": "#ops",
        }
    ]))

    result, home_path, routes_path, report_path = run_sync(tmp_path, monkeypatch)

    assert result.returncode == 0
    assert json.loads(home_path.read_text(encoding="utf-8")) == [
        {
            "name": "omgjkh",
            "team_id": "T123",
            "channel_id": "C456",
            "channel_name": "#ops",
        }
    ]
    assert json.loads(routes_path.read_text(encoding="utf-8")) == {"C456": "T123"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "written"
    assert report["home_channel_count"] == 1


def test_home_channel_sync_removes_legacy_direct_home_env(monkeypatch, tmp_path):
    hermes_env = tmp_path / ".env"
    hermes_env.write_text(
        "\n".join(
            [
                "KEEP_ME=1",
                "SLACK_HOME_CHANNEL=C123",
                "SLACK_HOME_CHANNEL_NAME=team-home",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_HERMES_SLACK_HOME_CHANNELS_JSON", json.dumps([
        {
            "name": "omgjkh",
            "team_id": "T123",
            "channel_id": "C456",
            "channel_name": "#ops",
        }
    ]))

    result, _home_path, _routes_path, report_path = run_sync(tmp_path, monkeypatch)

    assert result.returncode == 0
    assert hermes_env.read_text(encoding="utf-8") == "KEEP_ME=1\n"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["legacy_env_normalization"] == "removed"
    assert report["legacy_env_removed_keys"] == [
        "SLACK_HOME_CHANNEL",
        "SLACK_HOME_CHANNEL_NAME",
    ]


class SlackHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        body = {"ok": False, "error": "unknown"}
        if self.path == "/auth.test":
            body = {"ok": True, "team_id": "T999"}
        elif self.path == "/conversations.list":
            assert params["types"] == ["public_channel,private_channel"]
            body = {
                "ok": True,
                "channels": [
                    {"id": "C111", "name": "general"},
                    {"id": "C999", "name": "team-home"},
                ],
            }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def test_home_channel_name_discovers_channel_from_slack_accounts(monkeypatch, tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "MAC_HERMES_SLACK_API_BASE",
            "http://127.0.0.1:%d" % server.server_address[1],
        )
        monkeypatch.setenv("MAC_HERMES_SLACK_HOME_CHANNEL_NAME", "#team-home")

        result, home_path, routes_path, report_path = run_sync(
            tmp_path,
            monkeypatch,
            accounts=[{"name": "Offtera", "bot_token": "xoxb-secret"}],
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 0
    homes = json.loads(home_path.read_text(encoding="utf-8"))
    assert homes == [
        {
            "name": "offtera",
            "team_id": "T999",
            "channel_id": "C999",
            "channel_name": "#team-home",
        }
    ]
    assert json.loads(routes_path.read_text(encoding="utf-8")) == {"C999": "T999"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["configured_home_channel_name"] == "team-home"
    assert "xoxb-secret" not in report_path.read_text(encoding="utf-8")


def test_home_channel_sync_skips_without_accounts(monkeypatch, tmp_path):
    result, home_path, routes_path, report_path = run_sync(tmp_path, monkeypatch)

    assert result.returncode == 0
    assert not home_path.exists()
    assert not routes_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "skipped_no_accounts"
