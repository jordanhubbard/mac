"""One exact release manifest for the complete MAC stack.

A matching Git HEAD does not describe a deployed system: the Python lock, uv,
Hermes, OpenShell, the coding and verification images, the service templates
and the database schema can each differ while the checkout agrees. This module
derives a single manifest from an exact source tree, plus the CI image
publication receipt, that names every one of them:

- ``mac``: commit, tree and version;
- ``python``: the reviewed interpreter, ``requires-python`` and the lock digests;
- ``tools``: reviewed native tools (uv, python) and the asset table digest;
- ``runtime_tools``: the frozen build arguments of the worker runtime image
  (OpenCode, Claude Code, Node, pnpm, gh, Rust, buildx);
- ``hermes``: the upstream revision and patch recipe (``mac.hermes_release``);
- ``openshell``: the reviewed CLI/gateway/supervisor version and asset digests;
- ``images``: the coding and verification image, one pin, from the receipt;
- ``services``: digests of every service template and wrapper;
- ``config_schema``: the generated environment registry digest;
- ``database``: the migration level;
- ``roles``: which components each role installs, per platform.

Only explicit role/platform differences are allowed: a role lists the
platforms it supports, and nothing claims byte-identical arm64/x86 artifacts.
The manifest carries its own ``manifest_sha256`` over its canonical JSON, so
it is immutable by content. It is built from files alone: no host, network or
installed package is consulted, so two builds of one commit agree.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "mac.release_manifest.v1"
RUNTIME_REPOSITORY = "ghcr.io/jordanhubbard/mac-openshell-runtime"

#: Explicit role/platform matrix. A host outside it is not a supported target.
ROLES: Dict[str, Dict[str, Any]] = {
    "hub": {
        "platforms": ["darwin/arm64"],
        "components": ["mac", "python", "tools", "hermes", "services", "config_schema", "database"],
    },
    "worker": {
        "platforms": ["linux/arm64", "linux/amd64"],
        "components": [
            "mac",
            "python",
            "tools",
            "hermes",
            "openshell",
            "images",
            "runtime_tools",
            "services",
            "config_schema",
            "database",
        ],
    },
}

SERVICE_PATHS = ("deploy/systemd", "deploy/bin")
SERVICE_FILES = (
    "deploy/install-macos-services.sh",
    "deploy/install-fleet-context-service.sh",
    "deploy/hermes/install-hermes-gateway.sh",
    "scripts/fleet-update",
)


class ManifestError(ValueError):
    """The source tree or receipt cannot produce an exact manifest."""


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    try:
        return _sha(path.read_bytes())
    except OSError as exc:
        raise ManifestError("missing release input %s" % path) from exc


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ManifestError("git %s failed in %s: %s" % (" ".join(args), source, result.stderr.strip()))
    return result.stdout.strip()


def _shell_assignments(path: Path, pattern: str) -> Dict[str, str]:
    """``NAME="value"`` lines of a sourced, side-effect-free shell file."""
    text = path.read_text(encoding="utf-8")
    return dict(re.findall(r'^(%s)="([^"$]*)"' % pattern, text, flags=re.MULTILINE))


def _requires_python(source: Path) -> str:
    match = re.search(
        r'^requires-python\s*=\s*"([^"]+)"', (source / "pyproject.toml").read_text(), re.MULTILINE
    )
    if not match:
        raise ManifestError("pyproject.toml declares no requires-python")
    return match.group(1)


def _version(source: Path) -> str:
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"', (source / "src/mac/__init__.py").read_text(), re.MULTILINE
    )
    if not match:
        raise ManifestError("src/mac/__init__.py declares no __version__")
    return match.group(1)


def _identity_module(source: Path) -> Any:
    """``scripts/image-publication-identity.py``: the image pins and receipt
    schema live there, once, and the manifest reuses them."""
    path = source / "scripts" / "image-publication-identity.py"
    spec = importlib.util.spec_from_file_location("_mac_image_publication_identity", path)
    if spec is None or spec.loader is None:
        raise ManifestError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_build_args(identity: Any) -> Dict[str, str]:
    """The frozen worker-image build arguments, from the one place they live."""
    return dict(identity.IMAGE_SPECS["openshell-runtime"]["build_args"])


def _hermes(source: Path) -> Dict[str, Any]:
    from mac.hermes_release import recipe

    manifests = sorted((source / "deploy" / "hermes").glob("*-source.json"))
    if not manifests:
        raise ManifestError("no reviewed Hermes source manifests under deploy/hermes")
    value = recipe(manifests)
    value["patches"] = {
        path.name: file_digest(path) for path in sorted((source / "deploy" / "hermes").glob("*.patch"))
    }
    return value


def _openshell(source: Path) -> Dict[str, Any]:
    assets = source / "deploy" / "openshell" / "reviewed-cli-assets.sh"
    values = _shell_assignments(assets, r"OPENSHELL_[A-Z0-9_]+")
    version = values.get("OPENSHELL_REVIEWED_CLI_VERSION", "")
    if not version:
        raise ManifestError("%s pins no OPENSHELL_REVIEWED_CLI_VERSION" % assets)
    return {
        "version": version,
        "components": ["cli", "gateway", "supervisor"],
        "assets_sha256": file_digest(assets),
        "bootstrap_sha256": file_digest(source / "deploy" / "openshell" / "bootstrap-openshell.sh"),
        "policy_sha256": file_digest(source / "deploy" / "openshell" / "mac-hermes-policy.yaml"),
    }


def _services(source: Path) -> Dict[str, str]:
    files: List[Path] = []
    for rel in SERVICE_PATHS:
        files.extend(path for path in sorted((source / rel).iterdir()) if path.is_file())
    files.extend(source / rel for rel in SERVICE_FILES)
    return {str(path.relative_to(source)): file_digest(path) for path in files}


def _database(source: Path) -> Dict[str, Any]:
    migrations = sorted(
        path.name for path in (source / "src/mac/data/postgres/migrations").glob("*.sql")
    )
    if not migrations:
        raise ManifestError("no database migrations found")
    return {"migrations": len(migrations), "latest": migrations[-1]}


def _images(receipt: Optional[Dict[str, Any]], commit: str, identity: Any) -> Dict[str, Any]:
    if receipt is None:
        return {"status": "unresolved", "reason": "no runtime image publication receipt"}
    if receipt.get("schema") != identity.RECEIPT_SCHEMA or receipt.get("status") != "passed":
        raise ManifestError("runtime image receipt is not a passed %s" % identity.RECEIPT_SCHEMA)
    if receipt.get("kind") != "openshell-runtime" or receipt.get("repository") != RUNTIME_REPOSITORY:
        raise ManifestError("receipt is not for the worker runtime image")
    if receipt.get("requested_revision") != commit:
        raise ManifestError(
            "receipt was produced for %s, not %s" % (receipt.get("requested_revision"), commit)
        )
    digest = str(receipt.get("image_digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ManifestError("receipt has no exact image digest")
    ref = "%s@%s" % (RUNTIME_REPOSITORY, digest)
    return {
        "status": "resolved",
        # One pin: coding and verification run the same reviewed image
        # (task_b1828d67: three pins drifted apart for a day).
        "coding": ref,
        "verification": ref,
        "platforms": list(receipt.get("platforms") or []),
        "build_revision": str(receipt.get("build_revision") or ""),
        "frozen_inputs_sha256": str(receipt.get("frozen_inputs_sha256") or ""),
        "receipt_sha256": _sha(canonical(receipt)),
    }


def build(
    source: Path, *, receipt: Optional[Dict[str, Any]] = None, commit: Optional[str] = None
) -> Dict[str, Any]:
    """The manifest for the source tree at ``commit`` (default: its HEAD)."""
    source = Path(source).resolve()
    commit = commit or _git(source, "rev-parse", "HEAD")
    if _git(source, "rev-parse", "HEAD") != commit:
        raise ManifestError("the source tree is not checked out at %s" % commit)
    if _git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ManifestError("the source tree has uncommitted changes; a manifest needs exact source")
    identity = _identity_module(source)
    tools = _shell_assignments(source / "deploy" / "reviewed-tool-assets.sh", r"MAC_REVIEWED_[A-Z]+_VERSION")
    python_version = (source / ".python-version").read_text().strip()
    if tools.get("MAC_REVIEWED_PYTHON_VERSION") != python_version:
        raise ManifestError(
            "reviewed python %s disagrees with .python-version %s"
            % (tools.get("MAC_REVIEWED_PYTHON_VERSION"), python_version)
        )
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "mac": {
            "commit": commit,
            "tree": _git(source, "rev-parse", "%s^{tree}" % commit),
            "version": _version(source),
        },
        "python": {
            "version": python_version,
            "requires": _requires_python(source),
            "pyproject_sha256": file_digest(source / "pyproject.toml"),
            "lock_sha256": file_digest(source / "uv.lock"),
        },
        "tools": {
            "uv": tools.get("MAC_REVIEWED_UV_VERSION", ""),
            "python": tools.get("MAC_REVIEWED_PYTHON_VERSION", ""),
            "assets_sha256": file_digest(source / "deploy" / "reviewed-tool-assets.sh"),
        },
        "runtime_tools": _runtime_build_args(identity),
        "hermes": _hermes(source),
        "openshell": _openshell(source),
        "images": _images(receipt, commit, identity),
        "services": _services(source),
        "config_schema": {
            "env_registry_sha256": file_digest(source / "src/mac/data/env_config_registry.json"),
        },
        "database": _database(source),
        "roles": ROLES,
    }
    if not manifest["tools"]["uv"]:
        raise ManifestError("deploy/reviewed-tool-assets.sh pins no uv version")
    manifest["complete"] = manifest["images"]["status"] == "resolved"
    manifest["manifest_sha256"] = _sha(canonical(manifest))
    return manifest


def verify(manifest: Dict[str, Any], *, require_complete: bool = True) -> None:
    """Refuse a manifest that was edited, is foreign, or leaves a pin open."""
    if manifest.get("schema") != SCHEMA:
        raise ManifestError("not a %s manifest" % SCHEMA)
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if _sha(canonical(body)) != manifest.get("manifest_sha256"):
        raise ManifestError("manifest_sha256 does not match its content")
    if require_complete and not manifest.get("complete"):
        raise ManifestError(
            "manifest is incomplete: images %s" % manifest.get("images", {}).get("status")
        )


def diff(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """Dotted paths whose values differ between two manifests."""
    changes: List[str] = []

    def walk(a: Any, b: Any, prefix: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                walk(a.get(key), b.get(key), "%s.%s" % (prefix, key) if prefix else key)
        elif a != b:
            changes.append(prefix)

    walk(
        {k: v for k, v in old.items() if k != "manifest_sha256"},
        {k: v for k, v in new.items() if k != "manifest_sha256"},
        "",
    )
    return changes


def load(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError("cannot read manifest %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict):
        raise ManifestError("manifest %s is not an object" % path)
    return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.release_manifest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build_cmd = sub.add_parser("build", help="derive the manifest from an exact source tree")
    build_cmd.add_argument("--source", type=Path, default=Path("."))
    build_cmd.add_argument("--commit", help="the commit the tree must be at (default: HEAD)")
    build_cmd.add_argument("--runtime-receipt", type=Path, help="CI publication-receipt.json")
    build_cmd.add_argument("--require-complete", action="store_true")
    build_cmd.add_argument("--output", type=Path)
    verify_cmd = sub.add_parser("verify", help="check a manifest's digest and completeness")
    verify_cmd.add_argument("manifest", type=Path)
    verify_cmd.add_argument("--allow-incomplete", action="store_true")
    diff_cmd = sub.add_parser("diff", help="what changed between two manifests")
    diff_cmd.add_argument("old", type=Path)
    diff_cmd.add_argument("new", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "build":
            receipt = load(args.runtime_receipt) if args.runtime_receipt else None
            manifest = build(args.source, receipt=receipt, commit=args.commit)
            verify(manifest, require_complete=args.require_complete)
            text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            if args.output:
                args.output.write_text(text, encoding="utf-8")
                print("%s %s" % (manifest["manifest_sha256"], args.output))
            else:
                sys.stdout.write(text)
        elif args.cmd == "verify":
            manifest = load(args.manifest)
            verify(manifest, require_complete=not args.allow_incomplete)
            print("%s ok (%s)" % (manifest["manifest_sha256"], manifest["mac"]["commit"]))
        else:
            for path in diff(load(args.old), load(args.new)):
                print(path)
    except ManifestError as exc:
        print("release manifest: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
