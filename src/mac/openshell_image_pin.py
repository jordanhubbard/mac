"""Keep a worker's OpenShell runtime image pinned in exactly one place.

A worker used to pin its sandbox image three times: ``MAC_OPENSHELL_CREATE_ARGS
--from`` (coding sandboxes), ``~/.mac/openshell/runtime-image-ref``
(attestation), and ``MAC_HUB_VERIFY_IMAGE`` (the repository test gate). On
2026-10-02 a repin updated the first two and missed the third, so for a day
every gate ran image 27f53777 while coding ran f939c63e (task_b1828d67). On
2026-10-10 both workers still had a stale ``MAC_HUB_VERIFY_IMAGE``.

``runtime-image-ref`` is now the one pin. The test gate reads it
(``executor_sandbox.verifier_runtime_image``), and a ``--from`` that disagrees
with it is refused rather than guessed at. This module keeps ``mac.env``
derived from it:

* ``sync`` (the default; ``scripts/fleet-update`` runs it on every worker
  before restarting ``mac-agent``) rewrites ``MAC_OPENSHELL_CREATE_ARGS --from``
  to the pinned image and removes ``MAC_HUB_VERIFY_IMAGE``. A host with no
  managed pin (the hub) is left alone.
* ``--image REF --input-sha256 SHA`` repins first: it pulls the published
  digest, checks its build-revision and frozen-input labels as
  ``deploy/openshell/bootstrap-openshell.sh`` does, writes the pin files, and
  then syncs. This changes only the image, never the OpenShell version.

It prints one JSON line and backs up ``mac.env`` before changing it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from mac import mac_paths

IMAGE_RE = re.compile(r"ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}")
INPUT_SHA_RE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
LOCAL_TAG = "localhost/mac-hermes:net"

_CREATE_ARGS_RE = re.compile(r"^(export\s+)?MAC_OPENSHELL_CREATE_ARGS=(.*)$")
_VERIFY_IMAGE_RE = re.compile(r"^(export\s+)?MAC_HUB_VERIFY_IMAGE=")

#: Runs a command and returns (exit code, combined output). Injected by tests.
Runner = Callable[..., "tuple[int, str]"]


def run(cmd: Sequence[str], env: Optional[Dict[str, str]] = None) -> "tuple[int, str]":
    proc = subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=1800, env=env, check=False
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _with_from(value: str, image: str) -> Optional[str]:
    """``value`` (a shell-quoted mac.env RHS) with its ``--from`` set to ``image``."""
    try:
        words = shlex.split(value)
        args = shlex.split(words[0]) if len(words) == 1 else words
    except ValueError:
        return None
    out: List[str] = []
    index = 0
    replaced = False
    while index < len(args):
        token = args[index]
        if token == "--from":
            index += 2
        elif token.startswith("--from="):
            index += 1
        else:
            out.append(token)
            index += 1
            continue
        if not replaced:
            out += ["--from", image]
            replaced = True
    if not replaced:
        out = ["--from", image] + out
    return '"%s"' % shlex.join(out)


def sync(mac_home: Path) -> Dict[str, object]:
    """Derive ``mac.env``'s image settings from the runtime-image-ref pin."""
    ref_path = mac_home / "openshell" / "runtime-image-ref"
    try:
        image = ref_path.read_text(encoding="utf-8").strip()
    except OSError:
        return {"status": "ok", "pin": "none", "changed": False}
    if not IMAGE_RE.fullmatch(image):
        return {"status": "ok", "pin": "unmanaged", "changed": False}
    env_path = mac_home / "mac.env"
    try:
        original = env_path.read_text(encoding="utf-8")
    except OSError:
        return {"status": "ok", "pin": image, "changed": False, "env": "missing"}
    lines: List[str] = []
    removed = 0
    for line in original.splitlines():
        if _VERIFY_IMAGE_RE.match(line):
            removed += 1
            continue
        match = _CREATE_ARGS_RE.match(line)
        if match:
            rewritten = _with_from(match.group(2), image)
            if rewritten is None:
                return {
                    "status": "error",
                    "pin": image,
                    "error": "MAC_OPENSHELL_CREATE_ARGS could not be parsed",
                }
            line = "%sMAC_OPENSHELL_CREATE_ARGS=%s" % (match.group(1) or "", rewritten)
        lines.append(line)
    updated = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
    if updated == original:
        return {"status": "ok", "pin": image, "changed": False}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = env_path.with_name("%s.bak-%s-image-pin" % (env_path.name, stamp))
    mode = env_path.stat().st_mode & 0o777
    _atomic_write(backup, original, mode)
    _atomic_write(env_path, updated, mode)
    return {
        "status": "ok",
        "pin": image,
        "changed": True,
        "removed_hub_verify_image": removed,
        "backup": str(backup),
    }


def repin(
    mac_home: Path,
    image: str,
    input_sha256: str,
    *,
    docker: str = "docker",
    runner: Runner = run,
) -> Dict[str, object]:
    """Pull and verify a published runtime image, pin it, then sync mac.env."""
    if not IMAGE_RE.fullmatch(image):
        return {"status": "error", "error": "image must be an immutable %s" % IMAGE_RE.pattern}
    if not INPUT_SHA_RE.fullmatch(input_sha256):
        return {"status": "error", "error": "--input-sha256 must be sha256:<64 hex>"}
    with tempfile.TemporaryDirectory() as config:
        # An empty client config: the image is public, and a stale credential
        # helper must not decide whether the pull works.
        Path(config, "config.json").write_text("{}", encoding="utf-8")
        code, output = runner([docker, "pull", image], env=dict(os.environ, DOCKER_CONFIG=config))
    if code != 0:
        return {"status": "error", "error": "pull failed: %s" % output[-400:]}
    labels: Dict[str, str] = {}
    for key, label in (
        ("revision", "org.opencontainers.image.revision"),
        ("input", "io.mac.frozen-inputs.sha256"),
    ):
        code, output = runner(
            [
                docker,
                "image",
                "inspect",
                "--format",
                '{{ index .Config.Labels "%s" }}' % label,
                image,
            ]
        )
        labels[key] = output.strip() if code == 0 else ""
    if not REVISION_RE.fullmatch(labels["revision"]):
        return {"status": "error", "error": "image build revision label is absent or malformed"}
    if labels["input"] != input_sha256:
        return {
            "status": "error",
            "error": "image frozen-input identity %s does not match %s"
            % (labels["input"] or "(none)", input_sha256),
        }
    code, output = runner([docker, "tag", image, LOCAL_TAG])
    if code != 0:
        return {"status": "error", "error": "tag failed: %s" % output[-400:]}
    osh = mac_home / "openshell"
    ref_path = osh / "runtime-image-ref"
    previous = ref_path.read_text(encoding="utf-8").strip() if ref_path.is_file() else ""
    if previous and previous != image:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        _atomic_write(osh / ("runtime-image-ref.bak-%s" % stamp), previous + "\n", 0o600)
    _atomic_write(osh / "runtime-input-sha256", input_sha256 + "\n", 0o600)
    _atomic_write(osh / "runtime-image-build-revision", labels["revision"] + "\n", 0o600)
    (osh / "image-source-sha").unlink(missing_ok=True)
    _atomic_write(ref_path, image + "\n", 0o600)
    result = sync(mac_home)
    result["previous_pin"] = previous
    result["build_revision"] = labels["revision"]
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.openshell_image_pin", description=__doc__)
    parser.add_argument("--mac-home", type=Path, default=None)
    parser.add_argument("--image", default="", help="repin to this published image digest first")
    parser.add_argument("--input-sha256", default="", help="the image's CI frozen-input identity")
    parser.add_argument("--docker", default=os.environ.get("OSH_DOCKER_BIN") or "docker")
    args = parser.parse_args(argv)
    mac_home = (args.mac_home or mac_paths.mac_home()).expanduser()
    if args.image:
        result = repin(mac_home, args.image, args.input_sha256, docker=args.docker)
    else:
        result = sync(mac_home)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
