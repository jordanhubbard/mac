"""Proactive environment contract derivation for repository onboarding.

Statically analyses a repository checkout and emits an environment contract
that captures the *deeper* environment requirements implied by the project —
version floors, native-build needs, and egress hosts — so the sandbox
provisioner can fail fast before the first build run, rather than discovering
each requirement through a sequence of reactive failures.

Schema: ``mac.environment_contract.v1``

Key fields emitted
------------------
runtime_versions : dict
    Per-language minimum version floors.  Currently populated:

    ``node_min`` (str|None)
        Minimum Node.js version string derived from ``engines.node`` in
        ``package.json``, ``packageManager`` field, ``.nvmrc``,
        ``.node-version``, or pnpm/yarn/npm lockfile header comments.

    ``python_min`` (str|None)
        Minimum Python version string derived from ``requires-python`` in
        ``pyproject.toml`` or ``setup.cfg``, or the ``python_requires``
        argument in ``setup.py``.

    ``pnpm_min`` (str|None)
        Minimum pnpm version from ``packageManager`` field or engines block.

native_build : dict
    ``required`` (bool)
        True when any of the following signals are found: a ``binding.gyp``
        file, a ``node-gyp rebuild`` or ``node-pre-gyp install`` script in
        ``package.json``, ``pnpm.onlyBuiltDependencies`` containing a
        known-native package name, a known-native npm package directly
        listed in ``dependencies``/``devDependencies``, a ``Cargo.toml``,
        a top-level ``CMakeLists.txt``, or a top-level ``go.mod``.

    ``signals`` (list[str])
        Human-readable descriptions of each detected signal.

egress : dict
    ``hosts`` (list[str])
        Deduplicated, sorted list of external hostnames the install/build
        steps are expected to contact.  Derived from ``.npmrc`` registry
        lines, ``lockfile`` resolution URL prefixes, and ``nodejs.org``
        when ``native_build.required`` is True.

preflight : dict
    ``status`` (str)  ``"pass"`` | ``"warn"`` | ``"fail"``
    ``checks`` (list[dict])
        List of ``{name, status, message}`` entries describing each
        individual check (version floor vs detected runtime, native-build
        toolchain availability, etc.).  Populated by
        :func:`validate_environment_contract` — left empty by
        :func:`derive_environment_contract`.

Usage example::

    from mac.environment_contract import derive_environment_contract, validate_environment_contract
    contract = derive_environment_contract("/path/to/repo")
    contract = validate_environment_contract(contract)
    if contract["preflight"]["status"] == "fail":
        raise RuntimeError(contract["preflight"]["checks"])
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

JsonDict = Dict[str, Any]

ENVIRONMENT_CONTRACT_SCHEMA = "mac.environment_contract.v1"

# ---------------------------------------------------------------------------
# Known-native npm packages: any direct dep match triggers native_build.
# ---------------------------------------------------------------------------
_KNOWN_NATIVE_NPM_PACKAGES = frozenset(
    {
        "@vscode/sqlite3",
        "better-sqlite3",
        "bcrypt",
        "bcryptjs",
        "canvas",
        "cpu-features",
        "ffi-napi",
        "fsevents",
        "grpc",
        "@grpc/grpc-js",
        "kerberos",
        "leveldown",
        "levelup",
        "lzma-native",
        "node-canvas",
        "node-gyp",
        "node-hid",
        "node-iconv",
        "node-pre-gyp",
        "node-sass",
        "nodegit",
        "ref",
        "ref-napi",
        "robotjs",
        "sass",
        "sharp",
        "sodium-native",
        "sqlite3",
        "usb",
        "uWebSockets.js",
        "zeromq",
    }
)

# ---------------------------------------------------------------------------
# Well-known registry hostname patterns
# ---------------------------------------------------------------------------
_REGISTRY_RE = re.compile(r"https?://([a-zA-Z0-9][a-zA-Z0-9._-]*\.[a-zA-Z]{2,})")


# ===========================================================================
# Public API
# ===========================================================================


def derive_environment_contract(
    repo_path: str | Path,
) -> JsonDict:
    """Derive an environment contract by *static analysis* of a repository.

    Does not execute any code, does not require network access, and has no
    side effects on the checkout.  Returns the contract dict; the
    ``preflight`` block is initialised but empty (run
    :func:`validate_environment_contract` to populate it).

    Parameters
    ----------
    repo_path:
        Root directory of the repository checkout.  Must exist; files that
        don't exist are silently skipped.

    Returns
    -------
    dict
        Contract with schema ``mac.environment_contract.v1``.
    """
    root = Path(repo_path)

    node_min, pnpm_min = _derive_node_version(root)
    python_min = _derive_python_version(root)
    native_required, native_signals = _derive_native_build(root)
    egress_hosts = _derive_egress_hosts(root, native_build=native_required)

    return {
        "schema": ENVIRONMENT_CONTRACT_SCHEMA,
        "repository_path": str(root),
        "runtime_versions": {
            "node_min": node_min,
            "python_min": python_min,
            "pnpm_min": pnpm_min,
        },
        "native_build": {
            "required": native_required,
            "signals": native_signals,
        },
        "egress": {
            "hosts": egress_hosts,
        },
        "preflight": {
            "status": "pending",
            "checks": [],
        },
    }


def validate_environment_contract(
    contract: JsonDict,
    *,
    node_version: Optional[str] = None,
    python_version: Optional[str] = None,
    pnpm_version: Optional[str] = None,
    has_c_compiler: Optional[bool] = None,
) -> JsonDict:
    """Run preflight checks against the current sandbox environment.

    Populates ``contract["preflight"]`` in-place and also returns the contract
    for chaining.  Each check is a dict with ``name``, ``status``
    (``"pass"``/``"warn"``/``"fail"``), and ``message``.

    Auto-detection: if a parameter is ``None`` the function probes the live
    environment (``shutil.which``, subprocess).  Pass explicit values in tests
    or when you already know the environment capabilities.

    Parameters
    ----------
    contract:
        A contract previously returned by :func:`derive_environment_contract`.
    node_version:
        Detected Node.js version (e.g. ``"20.11.0"``).  ``None`` = auto-detect.
    python_version:
        Detected Python version (e.g. ``"3.11.4"``).  ``None`` = auto-detect.
    pnpm_version:
        Detected pnpm version.  ``None`` = auto-detect.
    has_c_compiler:
        Whether a C compiler is available.  ``None`` = auto-detect via
        ``shutil.which("gcc")`` / ``shutil.which("clang")``.

    Returns
    -------
    dict
        The same contract dict with ``preflight`` populated.
    """
    checks: List[JsonDict] = []

    rv = contract.get("runtime_versions", {})

    # --- Node.js version floor ---
    node_min = rv.get("node_min")
    if node_min:
        if node_version is None:
            node_version = _detect_command_version("node", "--version")
        checks.append(_check_version_floor("node", required=node_min, detected=node_version))

    # --- Python version floor ---
    python_min = rv.get("python_min")
    if python_min:
        if python_version is None:
            python_version = _detect_command_version("python3", "--version")
        checks.append(_check_version_floor("python3", required=python_min, detected=python_version))

    # --- pnpm version floor ---
    pnpm_min = rv.get("pnpm_min")
    if pnpm_min:
        if pnpm_version is None:
            pnpm_version = _detect_command_version("pnpm", "--version")
        checks.append(_check_version_floor("pnpm", required=pnpm_min, detected=pnpm_version))

    # --- Native build toolchain ---
    nb = contract.get("native_build", {})
    if nb.get("required"):
        if has_c_compiler is None:
            has_c_compiler = bool(
                shutil.which("gcc") or shutil.which("clang") or shutil.which("cc")
            )
        if has_c_compiler:
            checks.append(
                {
                    "name": "c_compiler",
                    "status": "pass",
                    "message": "C compiler detected (native build supported)",
                }
            )
        else:
            checks.append(
                {
                    "name": "c_compiler",
                    "status": "fail",
                    "message": (
                        "native_build.required=true but no C compiler found "
                        "(gcc/clang/cc); repo needs a compiler toolchain and "
                        "Node headers — rebuild the sandbox image or install build-essential"
                    ),
                }
            )

    statuses = {c["status"] for c in checks}
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    elif checks:
        overall = "pass"
    else:
        overall = "pass"

    contract["preflight"] = {"status": overall, "checks": checks}
    return contract


def environment_contract_summary(contract: JsonDict) -> str:
    """Return a human-readable one-paragraph summary of the contract.

    Suitable for embedding in an onboarding task description or a log line.
    """
    rv = contract.get("runtime_versions", {})
    nb = contract.get("native_build", {})
    egress = contract.get("egress", {})

    parts: List[str] = []

    version_parts: List[str] = []
    if rv.get("node_min"):
        version_parts.append("Node>=%s" % rv["node_min"])
    if rv.get("pnpm_min"):
        version_parts.append("pnpm>=%s" % rv["pnpm_min"])
    if rv.get("python_min"):
        version_parts.append("Python>=%s" % rv["python_min"])
    if version_parts:
        parts.append("Runtime version floors: %s." % ", ".join(version_parts))

    if nb.get("required"):
        signals = nb.get("signals", [])
        sig_str = ("; ".join(signals[:3])) if signals else "native bindings detected"
        parts.append(
            "Native build required (%s): sandbox needs a C compiler and Node headers." % sig_str
        )
    else:
        parts.append("No native build signals detected.")

    hosts = egress.get("hosts", [])
    if hosts:
        parts.append("Expected egress hosts: %s." % ", ".join(hosts))

    pf = contract.get("preflight", {})
    status = pf.get("status", "pending")
    if status not in ("pending", "pass"):
        failed = [c for c in pf.get("checks", []) if c.get("status") == "fail"]
        if failed:
            parts.append(
                "PREFLIGHT %s: %s"
                % (
                    status.upper(),
                    "; ".join(c["message"] for c in failed),
                )
            )

    return " ".join(parts) if parts else "No environment constraints detected."


# ===========================================================================
# Derivation helpers — one per concern
# ===========================================================================


def _derive_node_version(root: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return (node_min, pnpm_min) from static repo files."""
    node_min: Optional[str] = None
    pnpm_min: Optional[str] = None

    # 1. .nvmrc
    nvmrc = root / ".nvmrc"
    if nvmrc.exists():
        raw = nvmrc.read_text(encoding="utf-8", errors="ignore").strip().lstrip("v")
        if raw and re.match(r"^\d", raw):
            node_min = _coerce_version(raw)

    # 2. .node-version
    node_version_file = root / ".node-version"
    if node_version_file.exists() and not node_min:
        raw = node_version_file.read_text(encoding="utf-8", errors="ignore").strip().lstrip("v")
        if raw and re.match(r"^\d", raw):
            node_min = _coerce_version(raw)

    # 3. package.json — engines.node + packageManager
    pkg = _read_json(root / "package.json")
    if pkg:
        engines = pkg.get("engines") or {}
        if isinstance(engines, dict):
            node_eng = engines.get("node") or ""
            extracted = _extract_semver_floor(str(node_eng))
            if extracted:
                node_min = _coerce_version(extracted)

        pm = str(pkg.get("packageManager") or "")
        if pm.startswith("pnpm@"):
            pnpm_min = _coerce_version(pm.split("@", 1)[1].split("+")[0])
        elif pm.startswith("npm@"):
            pass  # npm version — not pnpm

    # 4. pnpm lockfile header (pnpm-lock.yaml) can have lockfileVersion
    pnpm_lock = root / "pnpm-lock.yaml"
    if pnpm_lock.exists():
        first_lines = _read_text_head(pnpm_lock, 5)
        for line in first_lines.splitlines():
            # lockfileVersion: '9.0' -> pnpm 9+
            m = re.match(r"lockfileVersion:\s*['\"]?(\d+)(?:\.\d+)?['\"]?", line)
            if m:
                lock_major = int(m.group(1))
                # lockfileVersion 6+ = pnpm 7+, 7+ = pnpm 8+, 9 = pnpm 9+
                # Rough mapping used in the field:
                _lockver_to_pnpm = {6: "7", 7: "8", 8: "8", 9: "9"}
                inferred = _lockver_to_pnpm.get(lock_major)
                if inferred and not pnpm_min:
                    pnpm_min = inferred

    return node_min, pnpm_min


def _derive_python_version(root: Path) -> Optional[str]:
    """Return python_min from static repo files."""
    # 1. pyproject.toml [project] requires-python
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            extracted = _extract_semver_floor(m.group(1))
            if extracted:
                return _coerce_version(extracted)

    # 2. setup.cfg [options] python_requires
    setup_cfg = root / "setup.cfg"
    if setup_cfg.exists():
        text = setup_cfg.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"python_requires\s*=\s*([^\n]+)", text)
        if m:
            extracted = _extract_semver_floor(m.group(1).strip())
            if extracted:
                return _coerce_version(extracted)

    # 3. setup.py (best-effort regex; don't import/exec it)
    setup_py = root / "setup.py"
    if setup_py.exists():
        text = setup_py.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            extracted = _extract_semver_floor(m.group(1))
            if extracted:
                return _coerce_version(extracted)

    return None


def _derive_native_build(root: Path) -> Tuple[bool, List[str]]:
    """Return (required: bool, signals: list[str])."""
    signals: List[str] = []

    # binding.gyp
    if (root / "binding.gyp").exists():
        signals.append("binding.gyp found (node-gyp native addon)")

    # Cargo.toml
    if (root / "Cargo.toml").exists():
        signals.append("Cargo.toml found (Rust/native build required)")

    # go.mod
    if (root / "go.mod").exists():
        signals.append("go.mod found (Go build required)")

    # CMakeLists.txt
    if (root / "CMakeLists.txt").exists():
        signals.append("CMakeLists.txt found (CMake/native build required)")

    # package.json scripts / deps
    pkg = _read_json(root / "package.json")
    if pkg:
        scripts = pkg.get("scripts") or {}
        if isinstance(scripts, dict):
            for _name, cmd in scripts.items():
                cmd_str = str(cmd or "")
                if "node-gyp rebuild" in cmd_str or "node-pre-gyp install" in cmd_str:
                    signals.append("package.json script contains node-gyp/node-pre-gyp rebuild")
                    break

        for dep_key in ("dependencies", "devDependencies", "optionalDependencies"):
            deps = pkg.get(dep_key) or {}
            if not isinstance(deps, dict):
                continue
            for pkg_name in deps:
                if pkg_name in _KNOWN_NATIVE_NPM_PACKAGES:
                    signals.append("known-native npm package in %s: %s" % (dep_key, pkg_name))

    # pnpm onlyBuiltDependencies (pnpm >= 8 native allow-list)
    pnpm_ws = root / "pnpm-workspace.yaml"
    if pnpm_ws.exists():
        text = pnpm_ws.read_text(encoding="utf-8", errors="ignore")
        if "onlyBuiltDependencies" in text:
            signals.append("pnpm-workspace.yaml onlyBuiltDependencies block found")

    # Deduplicate while preserving insertion order
    seen: set[str] = set()
    unique: List[str] = []
    for s in signals:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    return bool(unique), unique


def _derive_egress_hosts(root: Path, *, native_build: bool) -> List[str]:
    """Return sorted, deduplicated egress hostnames."""
    hosts: set[str] = set()

    # 1. .npmrc registry entries
    npmrc = root / ".npmrc"
    if npmrc.exists():
        text = npmrc.read_text(encoding="utf-8", errors="ignore")
        for m in _REGISTRY_RE.finditer(text):
            hosts.add(m.group(1))

    # 2. pnpm-lock.yaml / yarn.lock / package-lock.json — extract resolution URLs
    for lockfile_name in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json"):
        lf = root / lockfile_name
        if lf.exists():
            _extract_lockfile_hosts(lf, hosts)

    # 3. nodejs.org when native build is needed (downloads headers)
    if native_build:
        hosts.add("nodejs.org")

    return sorted(hosts)


# ===========================================================================
# Private utilities
# ===========================================================================


def _read_json(path: Path) -> Optional[JsonDict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_text_head(path: Path, lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return "\n".join(text.splitlines()[:lines])
    except OSError:
        return ""


def _extract_semver_floor(spec: str) -> Optional[str]:
    """Extract the minimum version string from a version specifier.

    Handles ``>=3.11``, ``^18``, ``~18.12``, ``18.x``, bare ``18``, etc.
    Returns None when the spec is empty, a wildcard-only expression, or
    unparseable.
    """
    spec = spec.strip()
    if not spec or spec in ("*", "latest", "x"):
        return None
    # >=X.Y.Z  /  >X.Y.Z  /  ^X.Y.Z  /  ~X.Y.Z  /  ==X.Y.Z
    m = re.search(r"[>~^=]{0,2}\s*(\d+(?:\.\d+)*)", spec)
    if m:
        return m.group(1)
    return None


def _coerce_version(v: str) -> str:
    """Strip leading 'v', trailing metadata, whitespace."""
    return v.strip().lstrip("v").split("+")[0].strip()


def _detect_command_version(command: str, flag: str = "--version") -> Optional[str]:
    """Run ``command flag`` and return the version string, or None."""
    exe = shutil.which(command)
    if not exe:
        return None
    try:
        import subprocess as _sp

        result = _sp.run(
            [exe, flag],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (result.stdout + result.stderr).strip()
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _check_version_floor(
    name: str,
    *,
    required: str,
    detected: Optional[str],
) -> JsonDict:
    """Return a preflight check dict for a version floor comparison."""
    if detected is None:
        return {
            "name": name,
            "status": "fail",
            "message": ("%s not found in PATH; repo requires >=%s" % (name, required)),
        }
    try:
        req_parts = _version_tuple(required)
        det_parts = _version_tuple(detected)
        if det_parts >= req_parts:
            return {
                "name": name,
                "status": "pass",
                "message": "%s %s satisfies >=%s" % (name, detected, required),
            }
        else:
            return {
                "name": name,
                "status": "fail",
                "message": (
                    "%s %s is too old; repo requires >=%s — "
                    "update the runtime or rebuild the sandbox image" % (name, detected, required)
                ),
            }
    except (ValueError, TypeError):
        return {
            "name": name,
            "status": "warn",
            "message": (
                "could not compare %s %s against required >=%s" % (name, detected, required)
            ),
        }


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v)
    if not parts:
        raise ValueError("no digits in version %r" % v)
    return tuple(int(p) for p in parts)


def _extract_lockfile_hosts(lockfile: Path, hosts: set[str]) -> None:
    """Best-effort extraction of registry/CDN hosts from lockfiles."""
    try:
        # Read up to 512 KB to keep it cheap for large lockfiles
        text = lockfile.read_bytes()[:524288].decode("utf-8", errors="ignore")
        for m in _REGISTRY_RE.finditer(text):
            host = m.group(1)
            # Skip clearly local or very generic internal names
            if not _is_internal_host(host):
                hosts.add(host)
    except OSError:
        pass


def _is_internal_host(host: str) -> bool:
    """Return True for obviously private/local hostnames to skip."""
    lower = host.lower()
    # localhost variants, .local, .internal, bare IPs
    if lower in ("localhost", "127.0.0.1", "::1"):
        return True
    if lower.endswith((".local", ".internal", ".corp", ".lan")):
        return True
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", lower):
        return True
    return False
