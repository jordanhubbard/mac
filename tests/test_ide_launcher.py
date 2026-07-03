from __future__ import annotations

import subprocess

import pytest

from mac import ide_launcher


def _profile(*, token: str = "profile-token", api_url: str = "http://127.0.0.1:48789"):
    return {
        "profile": "default",
        "connection": {"api_url": api_url, "mode": "ssh-tunnel"},
        "credential": {"token": token},
    }


def test_active_login_profile_precedes_legacy_tokens(monkeypatch) -> None:
    ensured: list[str] = []
    monkeypatch.setattr(ide_launcher, "active_profile_name", lambda: "default")
    monkeypatch.setattr(
        ide_launcher, "ensure_session", lambda name: ensured.append(name) or {"status": "running"}
    )
    monkeypatch.setattr(
        ide_launcher,
        "load_profile",
        lambda name, include_token: _profile(),
    )

    connection = ide_launcher.resolve_ide_connection(
        {
            "MAC_API_TOKEN": "stale-admin-token",
            "MAC_DEPLOY_HUB_TOKEN": "stale-deploy-token",
        }
    )

    assert connection == ide_launcher.IdeConnection(
        api_url="http://127.0.0.1:48789",
        token="profile-token",
        source="client-profile:default",
        profile="default",
    )
    assert ensured == ["default"]


def test_explicit_token_and_manual_mode_do_not_inspect_profiles(monkeypatch) -> None:
    monkeypatch.setattr(
        ide_launcher,
        "active_profile_name",
        lambda: (_ for _ in ()).throw(AssertionError("profile lookup was not expected")),
    )

    explicit = ide_launcher.resolve_ide_connection(
        {"IDE_TOKEN": "operator-token", "IDE_API_URL": "https://hub.example"}
    )
    manual = ide_launcher.resolve_ide_connection(
        {"IDE_AUTH": "manual", "IDE_API_URL": "https://manual.example"}
    )

    assert explicit.token == "operator-token"
    assert explicit.source == "IDE_TOKEN"
    assert explicit.api_url == "https://hub.example"
    assert manual == ide_launcher.IdeConnection(api_url="https://manual.example")


def test_no_active_profile_uses_fleet_scoped_legacy_fallback(monkeypatch) -> None:
    monkeypatch.setattr(ide_launcher, "active_profile_name", lambda: None)

    connection = ide_launcher.resolve_ide_connection(
        {
            "MAC_FLEET": "team-blue",
            "MAC_API_TOKEN__TEAM_BLUE": "fleet-token",
            "MAC_API_TOKEN": "admin-token",
        }
    )

    assert connection.token == "fleet-token"
    assert connection.source == "MAC_API_TOKEN__TEAM_BLUE"
    assert connection.api_url == ide_launcher.DEFAULT_API_URL


def test_required_or_broken_profile_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(ide_launcher, "active_profile_name", lambda: None)
    with pytest.raises(ide_launcher.IdeLauncherError, match="no active MAC login"):
        ide_launcher.resolve_ide_connection({"IDE_AUTH": "profile"})

    monkeypatch.setattr(ide_launcher, "active_profile_name", lambda: "default")
    monkeypatch.setattr(
        ide_launcher,
        "ensure_session",
        lambda _name: (_ for _ in ()).throw(ide_launcher.ClientLoginError("SSH failed")),
    )
    with pytest.raises(ide_launcher.IdeLauncherError, match="SSH failed"):
        ide_launcher.resolve_ide_connection({"MAC_API_TOKEN": "must-not-fallback"})


def test_vite_environment_keeps_token_server_side() -> None:
    connection = ide_launcher.IdeConnection(
        api_url="http://127.0.0.1:48789",
        token="profile-token",
        source="client-profile:default",
        profile="default",
    )

    child = ide_launcher.build_vite_environment(
        connection,
        {
            "IDE_TOKEN": "explicit-token",
            "VITE_MAC_TOKEN": "browser-token",
            "MAC_API_TOKEN": "admin-token",
            "MAC_API_TOKEN__DEFAULT": "fleet-token",
        },
    )

    assert child["MAC_API_URL"] == "http://127.0.0.1:48789"
    assert child["MAC_IDE_PROXY_TOKEN"] == "profile-token"
    assert child["VITE_MAC_AUTH_MODE"] == "managed"
    assert child["VITE_MAC_AUTH_LABEL"] == "CLI profile default"
    assert "VITE_MAC_TOKEN" not in child
    assert "IDE_TOKEN" not in child
    assert "MAC_API_TOKEN" not in child
    assert "MAC_API_TOKEN__DEFAULT" not in child


def test_interactive_prompt_selects_hub_without_changing_managed_auth() -> None:
    connection = ide_launcher.IdeConnection(
        api_url="http://127.0.0.1:48789",
        token="profile-token",
        source="client-profile:default",
        profile="default",
    )
    prompts: list[str] = []

    selected = ide_launcher.prompt_for_ide_connection(
        connection,
        {},
        interactive=True,
        input_fn=lambda prompt: prompts.append(prompt) or "https://192.0.2.10:8789/",
    )

    assert prompts == ["Target hub URL [http://127.0.0.1:48789]: "]
    assert selected == ide_launcher.IdeConnection(
        api_url="https://192.0.2.10:8789",
        token="profile-token",
        source="client-profile:default",
        profile="default",
    )


def test_hub_prompt_retries_invalid_url_and_skips_explicit_or_noninteractive(
    capsys,
) -> None:
    connection = ide_launcher.IdeConnection(api_url=ide_launcher.DEFAULT_API_URL)
    answers = iter(["hub.example:8789", "http://hub.example:8789"])

    selected = ide_launcher.prompt_for_ide_connection(
        connection,
        {},
        interactive=True,
        input_fn=lambda _prompt: next(answers),
    )

    assert selected.api_url == "http://hub.example:8789"
    assert "must include http:// or https://" in capsys.readouterr().err
    assert (
        ide_launcher.prompt_for_ide_connection(
            connection,
            {"IDE_API_URL": "http://explicit.example:8789"},
            interactive=True,
            input_fn=lambda _prompt: (_ for _ in ()).throw(
                AssertionError("unexpected prompt")
            ),
        )
        is connection
    )
    assert (
        ide_launcher.prompt_for_ide_connection(
            connection,
            {},
            interactive=False,
            input_fn=lambda _prompt: (_ for _ in ()).throw(
                AssertionError("unexpected prompt")
            ),
        )
        is connection
    )


def test_run_starts_vite_without_token_in_command(tmp_path, monkeypatch) -> None:
    ide_dir = tmp_path / "ide"
    ide_dir.mkdir()
    (ide_dir / "package.json").write_text("{}\n", encoding="utf-8")
    connection = ide_launcher.IdeConnection(
        api_url="http://127.0.0.1:48789",
        token="secret-token",
        source="client-profile:default",
        profile="default",
    )
    monkeypatch.setattr(ide_launcher, "resolve_ide_connection", lambda _env: connection)
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ide_launcher.subprocess, "run", fake_run)

    result = ide_launcher.run(
        {
            "IDE_DIR": str(ide_dir),
            "IDE_HOST": "127.0.0.1",
            "IDE_PORT": "5273",
            "NPM": "npm",
        }
    )

    assert result == 0
    assert seen["command"] == [
        "npm",
        "run",
        "dev",
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        "5273",
    ]
    assert "secret-token" not in " ".join(seen["command"])
    assert seen["env"]["MAC_IDE_PROXY_TOKEN"] == "secret-token"


def test_main_handles_ctrl_c_without_traceback(monkeypatch) -> None:
    monkeypatch.setattr(
        ide_launcher,
        "run",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(SystemExit) as caught:
        ide_launcher.main()

    assert caught.value.code == 130
