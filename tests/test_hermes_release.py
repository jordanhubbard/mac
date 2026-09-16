"""Release preparation must precede activation and never resync serving code."""

import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys

import pytest

from mac import hermes_release as release


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def fixture(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init")
    git(upstream, "config", "user.name", "Fixture")
    git(upstream, "config", "user.email", "fixture@example.invalid")
    (upstream / "agent").mkdir()
    (upstream / "agent/__init__.py").write_text("")
    builder = upstream / "agent/prompt_builder.py"
    original = (
        "def build_context_files_prompt(cwd=None, home_override=None):\n    return 'missing'\n"
    )
    patched = """import os
from pathlib import Path
def build_context_files_prompt(cwd=None, home_override=None):
    home=Path(home_override)
    return (Path(os.environ['MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN']).read_text()
            + (home/'SOUL.md').read_text())
"""
    builder.write_text(original)
    (upstream / "hermes").write_text("print('selected qualified runtime')\n")
    (upstream / "hermes_cli").mkdir()
    (upstream / "hermes_cli/__init__.py").write_text("")
    (upstream / "hermes_cli/main.py").write_text("print('Hermes CLI help')\n")
    (upstream / "hermes_cli/stderr_timestamp.py").write_text(
        "import subprocess,sys\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(subprocess.call(sys.argv[sys.argv.index('--')+1:]))\n"
    )
    (upstream / ".gitignore").write_text(".venv/\nvenv\n__pycache__/\n")
    for module in ("slack_bolt", "slack_sdk", "aiohttp", "mcp"):
        (upstream / f"{module}.py").write_text("")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "upstream fixture")
    revision = git(upstream, "rev-parse", "HEAD")
    builder.write_text(patched)
    patch = tmp_path / "reviewed.patch"
    patch.write_text(git(upstream, "diff") + "\n")
    builder.write_text(original)
    manifest = tmp_path / "source.json"
    manifest.write_text(
        json.dumps(
            {
                "upstream_repository": str(upstream),
                "upstream_commit": revision,
                "python_version": platform.python_version(),
                "uv_version": "0.12.12",
                "patch": patch.name,
                "patch_sha256": release.digest(patch),
                "files": {
                    "agent/prompt_builder.py": {
                        "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
                        "patched_sha256": hashlib.sha256(patched.encode()).hexdigest(),
                    }
                },
            }
        )
    )
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,sys,venv
from pathlib import Path
if sys.argv[1:] == ['--version']:
    print('uv 0.12.12'); sys.exit()
Path('sync-args.json').write_text(json.dumps(sys.argv[1:]))
if os.environ.get('FAIL_SYNC'): sys.exit(1)
venv.EnvBuilder(system_site_packages=True).create('.venv')
# Model uv's editable install, but use a real isolated interpreter and its
# site initialization. A dependency-only sync must not make source importable.
if '--no-install-project' not in sys.argv:
    site=next(Path('.venv/lib').glob('python*/site-packages'))
    (site/'hermes-fixture.pth').write_text(str(Path.cwd())+'\\n')
"""
    )
    uv.chmod(0o755)
    home = tmp_path / "profile"
    home.mkdir()
    (home / "SOUL.md").write_text("existing persona")
    markdown = home / "context.md"
    markdown.write_text("required MAC instructions")
    return dict(
        root=tmp_path / "releases",
        manifests=[manifest],
        home=home,
        markdown=markdown,
        uv=str(uv),
        launcher=tmp_path / "bin/hermes",
    )


def test_fresh_release_qualifies_then_activates_and_reuses_without_sync(fixture):
    runtime = release.prepare(**fixture)
    assert not fixture["launcher"].exists()
    args = json.loads((runtime / "sync-args.json").read_text())
    assert "--locked" in args and "--no-default-groups" in args
    assert "--no-install-project" not in args
    assert args.count("--extra") == 2 and "slack" in args and "mcp" in args
    release.activate(
        runtime, fixture["launcher"], fixture["home"], fixture["markdown"], fixture["manifests"]
    )
    assert release.resolve_runtime(fixture["launcher"]) == runtime
    assert (
        subprocess.check_output([str(fixture["launcher"])], text=True).strip()
        == "selected qualified runtime"
    )
    before = (runtime / "sync-args.json").stat().st_mtime_ns
    assert release.prepare(**fixture) == runtime
    assert (runtime / "sync-args.json").stat().st_mtime_ns == before


def test_qualification_rejects_missing_service_installation(fixture, monkeypatch):
    runtime = release.prepare(**fixture)
    next((runtime / ".venv/lib").glob("python*/site-packages/hermes-fixture.pth")).unlink()
    # Neither the caller's cwd nor PYTHONPATH may rescue the missing install.
    monkeypatch.chdir(runtime)
    monkeypatch.setenv("PYTHONPATH", str(runtime))
    with pytest.raises(RuntimeError, match="preparation failed"):
        release.qualify(
            runtime, fixture["home"], fixture["markdown"], release.recipe(fixture["manifests"])
        )
    assert not fixture["launcher"].exists()


def test_qualification_runs_service_child_not_only_imports(fixture):
    runtime = release.prepare(**fixture)
    (runtime / "hermes_cli/main.py").write_text(
        "if __name__ == '__main__':\n    raise SystemExit(37)\n"
    )
    with pytest.raises(RuntimeError, match="preparation failed"):
        release.qualify(
            runtime, fixture["home"], fixture["markdown"], release.recipe(fixture["manifests"])
        )


def test_qualification_rejects_other_runtime_on_environment_path(fixture):
    runtime = release.prepare(**fixture)
    other = fixture["root"] / "other"
    shutil.copytree(runtime / "hermes_cli", other / "hermes_cli")
    site = next((runtime / ".venv/lib").glob("python*/site-packages"))
    (site / "hermes-fixture.pth").write_text(str(other) + "\n" + str(runtime) + "\n")
    with pytest.raises(RuntimeError, match="preparation failed"):
        release.qualify(
            runtime, fixture["home"], fixture["markdown"], release.recipe(fixture["manifests"])
        )


@pytest.mark.parametrize("failure", ["patch", "dependencies", "prompt"])
def test_failed_candidate_leaves_previous_launcher_unchanged(fixture, monkeypatch, failure):
    runtime = release.prepare(**fixture)
    release.activate(
        runtime, fixture["launcher"], fixture["home"], fixture["markdown"], fixture["manifests"]
    )
    previous = fixture["launcher"].read_bytes()
    # Change the recipe so this is an upgrade, not reuse of a valid release.
    manifest = fixture["manifests"][0]
    manifest.write_text(manifest.read_text() + "\n")
    if failure == "patch":
        manifest.with_name("reviewed.patch").write_text("invalid patch")
    elif failure == "dependencies":
        monkeypatch.setenv("FAIL_SYNC", "1")
    else:
        fixture["markdown"].write_text("")
    with pytest.raises((ValueError, RuntimeError)):
        release.prepare(**fixture)
    assert fixture["launcher"].read_bytes() == previous
    assert release.resolve_runtime(fixture["launcher"]) == runtime


def test_modified_candidate_cannot_be_activated(fixture):
    runtime = release.prepare(**fixture)
    (runtime / "hermes").write_text("raise SystemExit('unreviewed')")
    with pytest.raises(ValueError, match="source changed"):
        release.activate(
            runtime, fixture["launcher"], fixture["home"], fixture["markdown"], fixture["manifests"]
        )
    assert not fixture["launcher"].exists()


def test_launcher_is_the_only_selection_and_supports_spaces(fixture):
    fixture["root"] = fixture["root"].with_name("runtime releases")
    runtime = release.prepare(**fixture)
    release.activate(
        runtime, fixture["launcher"], fixture["home"], fixture["markdown"], fixture["manifests"]
    )
    assert release.resolve_runtime(fixture["launcher"]) == runtime


def test_failed_launcher_replace_preserves_previous_selection(fixture, monkeypatch):
    runtime = release.prepare(**fixture)
    release.activate(
        runtime, fixture["launcher"], fixture["home"], fixture["markdown"], fixture["manifests"]
    )
    before = fixture["launcher"].read_bytes()

    def fail_replace(*args):
        raise OSError("injected activation interruption")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        release.activate(
            runtime, fixture["launcher"], fixture["home"], fixture["markdown"], fixture["manifests"]
        )
    assert fixture["launcher"].read_bytes() == before


@pytest.mark.parametrize("supervisor", ["systemd", "launchd"])
def test_service_must_select_same_environment_and_profile(fixture, supervisor):
    runtime = release.prepare(**fixture)
    home = fixture["home"].parent
    profile = fixture["home"]

    def service_for(selected_runtime, selected_profile):
        argv = [
            str(selected_runtime / "venv/bin/python"),
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
        ]
        if supervisor == "launchd":
            path = home / "Library/LaunchAgents/ai.hermes.gateway.plist"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                plistlib.dumps(
                    {
                        "ProgramArguments": argv,
                        "EnvironmentVariables": {"HERMES_HOME": str(selected_profile)},
                    }
                )
            )
        else:
            path = home / ".config/systemd/user/hermes-gateway.service"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "[Service]\nExecStart="
                + shlex.join(argv)
                + "\nEnvironment="
                + shlex.quote("HERMES_HOME=" + str(selected_profile))
                + "\n"
            )

    service_for(runtime, profile)
    release.verify_service(runtime, home, profile)
    service_for(runtime, home / "wrong-profile")
    with pytest.raises(ValueError, match="profile differs"):
        release.verify_service(runtime, home, profile)
    service_for(home / "old-runtime", profile)
    with pytest.raises(ValueError, match="interpreter differs"):
        release.verify_service(runtime, home, profile)


def test_startup_uses_launcher_instead_of_stale_runtime_environment(fixture, monkeypatch):
    from mac.hermes_startup import _runtime_prompt_bridge_report

    home = fixture["home"].parent
    fixture["launcher"] = home / ".local/bin/hermes"
    runtime = release.prepare(**fixture)
    release.activate(
        runtime, fixture["launcher"], fixture["home"], fixture["markdown"], fixture["manifests"]
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(fixture["home"]))
    monkeypatch.setenv("MAC_HERMES_PYTHON", "/missing/stale/python")
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(fixture["markdown"]))
    monkeypatch.setenv("MAC_HERMES_WORKSPACE", str(fixture["home"]))
    # Production health's fleet baseline is fixed, so qualify its selected
    # interpreter version without depending on the interpreter running pytest.
    original = subprocess.run

    def probe(argv, **kwargs):
        result = original(argv, **kwargs)
        if len(argv) > 2 and argv[1] == "-c" and "module_under_runtime" in argv[2]:
            observed = json.loads(result.stdout)
            observed["python"] = "3.14.7"
            result.stdout = json.dumps(observed)
        return result

    monkeypatch.setattr(subprocess, "run", probe)
    report = _runtime_prompt_bridge_report(home / "missing-old-runtime", required=True)
    assert report["constructed_prompt_verified"] is True


def test_node_prepare_refreshes_parent_runtime_after_child_activation(fixture):
    root = Path(__file__).resolve().parents[1]
    home = fixture["home"].parent
    fixture["launcher"] = home / ".local/bin/hermes"
    runtime = release.prepare(**fixture)
    release.activate(
        runtime, fixture["launcher"], fixture["home"], fixture["markdown"], fixture["manifests"]
    )
    source = home / "mac-source"
    installer = source / "deploy/hermes/install-hermes-gateway.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text('#!/bin/sh\n[ "$2" = --uv ] && [ "$3" = "$EXPECTED_UV" ]\n')
    installer.chmod(0o755)
    venv = home / "mac-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    node = (root / "deploy/fleet-node-install.sh").read_text()
    function = node.split("prepare_hermes_gateway() {", 1)[1].split("\n}", 1)[0]
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        SRC_DIR=str(source),
        VENV=str(venv),
        FLEET_NAME="fixture",
        NATIVE_UV="/reviewed toolchain/uv",
        EXPECTED_UV="/reviewed toolchain/uv",
        MAC_HERMES_AGENT_DIR="/stale/runtime",
        MAC_HERMES_PYTHON="/stale/python",
        PYTHONPATH=str(root / "src"),
    )
    program = (
        'set -eu\ndie() { exit 1; }\nverify_hermes_prompt_bridge() { test -x "$MAC_HERMES_PYTHON"; }\nprepare_hermes_gateway() {'
        + function
        + "\n}\nprepare_hermes_gateway\n"
        + 'printf "%s\\n%s\\n" "$MAC_HERMES_AGENT_DIR" "$MAC_HERMES_PYTHON"\n'
    )
    result = subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(runtime), str(runtime / ".venv/bin/python")]
