"""Tiny CLI for signing mac worker_evidence manifests.

This module exists so the bash stubs under ``deploy/codex-runner``
do not have to re-implement ``mac.services.sign_verification_manifest``
in a Python heredoc. The Job pod has the mac wheel installed already,
so a real entry point keeps the canonicalisation + HMAC logic in
exactly one place.

Usage::

    mac-evidence sign --manifest /path/to/manifest.json --key-env MAC_AGENT_ATTESTATION_KEY
    cat manifest.json | mac-evidence sign --manifest-stdin --key-env MAC_AGENT_ATTESTATION_KEY

Behaviour:

* Reads the manifest as JSON (existing ``signature`` / ``signed_by``
  fields are overwritten with the freshly computed values).
* ``--signed-by`` overrides the value embedded in the manifest. Falls
  back to whatever the manifest already carries or to
  ``MAC_AGENT_ID`` from the environment.
* ``--key-env`` (default ``MAC_AGENT_ATTESTATION_KEY``) names the env
  variable holding the HMAC key. The key is never echoed.
* On success writes the signed manifest back to ``--manifest`` (or
  to ``--output``, or to stdout in ``--manifest-stdin`` mode) using
  ``mac.models.json_dumps`` for byte-stable canonicalisation.
* Exit codes: ``0`` ok, ``2`` missing key, ``3`` bad input.

This module deliberately re-exports
``mac.services.sign_verification_manifest`` — no new crypto code
lives here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from mac.models import json_dumps
from mac.services import sign_verification_manifest


def _read_manifest(args: argparse.Namespace) -> dict:
    if args.manifest_stdin:
        raw = sys.stdin.read()
    else:
        if not args.manifest:
            raise SystemExit("--manifest or --manifest-stdin is required")
        raw = Path(args.manifest).read_text(encoding="utf-8")
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("manifest is not valid JSON: %s" % exc)
    if not isinstance(loaded, dict):
        raise SystemExit("manifest JSON root must be an object")
    return loaded


def _resolve_signed_by(args: argparse.Namespace, manifest: dict) -> str:
    candidate = (
        (args.signed_by or "").strip()
        or str(manifest.get("signed_by") or "").strip()
        or os.environ.get("MAC_AGENT_ID", "").strip()
    )
    if not candidate:
        raise SystemExit(
            "signed_by is required: pass --signed-by, set MAC_AGENT_ID, "
            "or pre-populate the manifest with signed_by"
        )
    return candidate


def _write_manifest(args: argparse.Namespace, manifest: dict) -> None:
    serialized = json_dumps(manifest)
    if args.output:
        target = Path(args.output)
    elif args.manifest_stdin:
        sys.stdout.write(serialized)
        sys.stdout.flush()
        return
    else:
        target = Path(args.manifest)
    # Atomic-ish rename so partial writes don't leave a half-formed
    # manifest if signing is interrupted (matches the bash stub's
    # tmp_path + os.rename pattern).
    tmp = target.with_name(target.name + ".tmp.%d" % os.getpid())
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, target)


def cmd_sign(args: argparse.Namespace) -> int:
    key_env = args.key_env or "MAC_AGENT_ATTESTATION_KEY"
    key = os.environ.get(key_env, "")
    if not key:
        sys.stderr.write(
            "[mac-evidence] %s is unset; refusing to write an unsigned manifest. "
            "Set the HMAC key on the environment (e.g. via MAC_RUNNER_ROLE_ATTESTATION_KEY_SECRETS).\n"
            % key_env
        )
        return 2
    manifest = _read_manifest(args)
    manifest["signed_by"] = _resolve_signed_by(args, manifest)
    manifest["signature"] = sign_verification_manifest(key, manifest)
    _write_manifest(args, manifest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mac-evidence",
        description="Sign mac worker_evidence manifests using the agent's attestation key.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sign = subparsers.add_parser(
        "sign", help="Sign a verification manifest in-place (or via stdin/stdout)."
    )
    source = sign.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--manifest",
        help="Path to a JSON manifest. Will be rewritten in place with signed_by + signature.",
    )
    source.add_argument(
        "--manifest-stdin",
        action="store_true",
        help="Read the manifest JSON from stdin. The signed manifest is written to stdout.",
    )
    sign.add_argument(
        "--output",
        help="Optional output path. Defaults to --manifest when reading from a file.",
    )
    sign.add_argument(
        "--key-env",
        default="MAC_AGENT_ATTESTATION_KEY",
        help="Env variable holding the HMAC key (default: MAC_AGENT_ATTESTATION_KEY).",
    )
    sign.add_argument(
        "--signed-by",
        help="Explicit signer id. Falls back to manifest.signed_by or MAC_AGENT_ID.",
    )
    sign.set_defaults(func=cmd_sign)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args) or 0)
    except SystemExit as exc:
        # argparse / our helpers raise SystemExit("msg") for input errors;
        # surface those as exit-3 (bad input) with the message on stderr.
        code = exc.code
        if isinstance(code, str):
            sys.stderr.write("[mac-evidence] %s\n" % code)
            return 3
        return int(code or 0)


if __name__ == "__main__":  # pragma: no cover - module entry shim
    sys.exit(main())
