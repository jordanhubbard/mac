#!/usr/bin/env python3
"""Prove an OpenShell runtime digest is anonymously readable and revision-bound."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile


IMAGE_RE = re.compile(
    r"ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}"
)
SHA_RE = re.compile(r"[0-9a-f]{40}")


def run(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        argv,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "%s failed (%d): %s"
            % (argv[0], completed.returncode, (completed.stderr or completed.stdout)[-2000:])
        )
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()
    if not IMAGE_RE.fullmatch(args.image_ref):
        parser.error("--image-ref must be the repository-owned immutable GHCR digest")
    if not SHA_RE.fullmatch(args.revision):
        parser.error("--revision must be an exact lowercase Git SHA")

    with tempfile.TemporaryDirectory(prefix="mac-runtime-anonymous-pull.") as config:
        with open(os.path.join(config, "config.json"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        anonymous_env = dict(os.environ)
        anonymous_env["DOCKER_CONFIG"] = config
        run([args.docker, "pull", args.image_ref], env=anonymous_env)

    observed_revision = run(
        [
            args.docker,
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
            args.image_ref,
        ]
    )
    if observed_revision != args.revision:
        raise RuntimeError(
            "runtime OCI revision mismatch: expected %s, observed %s"
            % (args.revision, observed_revision or "<missing>")
        )
    platform = run(
        [
            args.docker,
            "image",
            "inspect",
            "--format",
            "{{.Os}}/{{.Architecture}}",
            args.image_ref,
        ]
    )
    smoke = run(
        [
            args.docker,
            "run",
            "--rm",
            args.image_ref,
            "/bin/bash",
            "-c",
            (
                "set -euo pipefail; /usr/local/bin/mac-verify-bash-contract; "
                "gh --version | head -1 | grep -Eq '^gh version 2\\.95\\.0 '; "
                "test \"$(codex --version)\" = \"codex-cli 0.140.0\"; "
                "test \"$(pnpm --version)\" = \"11.13.1\"; "
                "codegraph --version; /opt/mac-venv/bin/python -c \"import mac\""
            ),
        ]
    )
    print(
        json.dumps(
            {
                "schema": "mac.openshell_runtime.publication_verification.v1",
                "status": "pass",
                "image_ref": args.image_ref,
                "revision": observed_revision,
                "platform": platform,
                "anonymous_pull": True,
                "runtime_smoke": bool(smoke or smoke == ""),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
