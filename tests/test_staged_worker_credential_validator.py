from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "validate-staged-worker-credential.py"
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"


def _release_archive(path: Path, module: str) -> str:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in {
            "src/mac/__init__.py": "",
            "src/mac/worker_credentials.py": module,
        }.items():
            raw = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(raw))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _old_installed_package(root: Path) -> Path:
    package = root / "mac"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "worker_credentials.py").write_text(
        "raise SystemExit('old installed MAC has no validate-current verb')\n"
    )
    return root


def test_candidate_validator_works_when_installed_package_lacks_verb(tmp_path: Path) -> None:
    archive = tmp_path / "release.tar.gz"
    digest = _release_archive(
        archive,
        """\
import argparse, json
parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
current = sub.add_parser("validate-current")
current.add_argument("--agent-id", required=True)
args = parser.parse_args()
print(json.dumps({
    "schema": "mac.worker_credential_current.v1",
    "status": "valid",
    "agent_id": args.agent_id,
    "principal_id": "worker-candidate-v0001",
}))
""",
    )
    environment = dict(os.environ, PYTHONPATH=str(_old_installed_package(tmp_path / "old")))
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--archive",
            str(archive),
            "--archive-sha256",
            digest,
            "--python",
            sys.executable,
            "--agent-id",
            "agent_alpha",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "mac.worker_credential_current.v1",
        "status": "valid",
        "agent_id": "agent_alpha",
        "principal_id": "worker-candidate-v0001",
    }


def test_candidate_validation_failure_aborts_before_following_mutation(tmp_path: Path) -> None:
    archive = tmp_path / "release.tar.gz"
    digest = _release_archive(
        archive,
        """\
import argparse
parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
current = sub.add_parser("validate-current")
current.add_argument("--agent-id", required=True)
parser.parse_args()
raise SystemExit(23)
""",
    )
    mutation = tmp_path / "mutation-started"
    argv = [
        sys.executable,
        str(HELPER),
        "--archive",
        str(archive),
        "--archive-sha256",
        digest,
        "--python",
        sys.executable,
        "--agent-id",
        "agent_alpha",
    ]
    command = " ".join(shlex.quote(item) for item in argv)
    command += " && " + " ".join(("touch", shlex.quote(str(mutation))))
    result = subprocess.run(["bash", "-c", command], check=False)

    assert result.returncode == 23
    assert not mutation.exists()


def test_typed_cohort_stages_candidate_before_credential_validation() -> None:
    script = DEPLOY.read_text(encoding="utf-8")
    syntax = subprocess.run(
        ["bash", "-n", str(DEPLOY)], capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr

    validator = script.split("validate_current_worker_credential() (", 1)[1].split(
        "\n)\n\ncreate_attestation_candidate", 1
    )[0]
    cohort = script.split("run_typed_cohort() {", 1)[1].split("\n}\n\nmain()", 1)[0]
    assert "validate-staged-worker-credential.py" in script
    assert (
        '"$HOME/.mac/venv/bin/python" -m mac.worker_credentials validate-current' not in validator
    )
    assert "HUB_CREDENTIAL_VALIDATOR_REMOTE_HELPER" in validator
    assert cohort.index("stage_hub_candidate_credential_validator") < cohort.index(
        "build_and_open_hub_epoch"
    )
