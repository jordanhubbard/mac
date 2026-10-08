"""Shared OpenShell runtime identity helpers."""

from __future__ import annotations

import base64
import json
import os
import shlex
from typing import Any, Iterable, Mapping, MutableMapping, Optional


# Empty by default: sandbox required-ness is DATA-DRIVEN (the agent's runtime
# ``resources["openshell_required"]``, an explicit override, or the
# ``MAC_OPENSHELL_REQUIRED`` env), never a hardcoded fleet roster baked into
# source that goes stale as the fleet changes. Callers may still pass an
# explicit ``required_agent_names`` set for a name-match fallback, but the
# default matches nothing.
DEFAULT_REQUIRED_AGENT_NAMES: frozenset[str] = frozenset()
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
# OpenShell's supervisor does not preserve a container image's ENV PATH for
# every create/exec surface. Production launchers therefore pass and reassert
# this image-owned baseline explicitly; repository-contract tool directories
# are prepended to it by the task executor.
SANDBOX_BASE_PATH = "/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin"


VERIFIER_PROFILE_READY = "[hub-verifier-profile] bounded-tmpfs ready"
# The bounded test storage mount. It must sit outside the image's WorkingDir
# (/sandbox): OpenShell 0.1 reserves the workspace root and everything under
# it, and refuses a driver mount there ("mount target ... is reserved for the
# OpenShell workspace"). /tmp is read-write in every MAC sandbox policy, so
# Landlock admits the mount without a policy change.
VERIFIER_TEST_STORAGE = "/tmp/mac-test-storage"


def verifier_resource_profile() -> tuple[list[str], list[str], str]:
    """Controller-owned resources shared by hub and worker verification.

    The shell fragment must run in every fresh create/exec shell before any
    toolchain or repository bootstrap. Agent shell exports do not survive exec.
    """
    profile = (os.environ.get("MAC_HUB_VERIFY_PROFILE") or "").strip()
    if profile in {"", "default"}:
        return [], [], ""
    if profile != "bounded-tmpfs":
        raise ValueError(
            "hub verifier resource profile unavailable: MAC_HUB_VERIFY_PROFILE "
            "must be default or bounded-tmpfs"
        )
    args = [
        "--cpu",
        "12",
        "--memory",
        "32Gi",
        "--driver-config-json",
        json.dumps(
            {
                "docker": {
                    "mounts": [
                        {
                            "type": "tmpfs",
                            "target": VERIFIER_TEST_STORAGE,
                            "size_bytes": 8 * 1024**3,
                            "mode": 0o1777,
                            "options": ["exec"],
                        }
                    ]
                }
            }
        ),
    ]
    # Large repository fixtures must not consume the database's bounded mount.
    # Both paths stay sandbox-local; PostgreSQL keeps its normal durability.
    environment = [
        "TMPDIR=/sandbox/test-scratch",
        f"MAC_TEST_PG_DATADIR={VERIFIER_TEST_STORAGE}/mac-test-pgdata",
        "MAC_TEST_JOBS=8",
    ]
    preflight = (
        'if [ "$(uname -s)" != Linux ] || '
        f'[ "$(stat -f -c %T {VERIFIER_TEST_STORAGE} 2>/dev/null)" != tmpfs ] || '
        f"[ ! -w {VERIFIER_TEST_STORAGE} ]; then "
        "echo 'hub verifier resource profile unavailable: bounded-tmpfs "
        f"requires a writable Linux tmpfs at {VERIFIER_TEST_STORAGE}' >&2; exit 96; fi; "
        "export " + " ".join(shlex.quote(value) for value in environment) + "; "
        'if ! mkdir -p "$TMPDIR" || [ ! -w "$TMPDIR" ]; then '
        "echo 'hub verifier resource profile unavailable: fixture scratch is not writable' "
        ">&2; exit 96; fi; "
        f"echo '{VERIFIER_PROFILE_READY}'; "
    )
    return args, environment, preflight


def verifier_profile_create_args(extra: list[str]) -> list[str]:
    """Add the fixed profile after validating caller-owned create arguments.

    Conflicting resource/driver flags are errors, never duplicate flags whose
    precedence would depend on the OpenShell CLI version. Report callers still
    apply their full allowlist before reaching this function.
    """
    args, _, _ = verifier_resource_profile()
    if args and any(
        token.split("=", 1)[0] in {"--cpu", "--memory", "--driver-config-json"} for token in extra
    ):
        raise ValueError(
            "bounded-tmpfs profile conflicts with MAC_OPENSHELL_CREATE_ARGS resource overrides"
        )
    return [*extra, *args]


def truthy(value: Any) -> bool:
    """Return whether the value represents a truthy string."""
    return str(value or "").strip().lower() in TRUTHY_VALUES


def base_agent_name(value: Any) -> str:
    """Return the normalized base agent name without prefix or domain."""
    text = str(value or "").strip().lower()
    if text.startswith("agent_"):
        text = text[len("agent_") :]
    return text.split(".", 1)[0]


def _boolish(value: Any) -> bool:
    if isinstance(value, str):
        return truthy(value)
    return bool(value)


def openshell_required_for_identity(
    *,
    agent_id: Any = None,
    agent_name: Any = None,
    host: Any = None,
    resources: Optional[Mapping[str, Any]] = None,
    explicit: Any = None,
    required_agent_names: Iterable[str] = DEFAULT_REQUIRED_AGENT_NAMES,
) -> bool:
    """Determine whether OpenShell is required for the given agent identity."""
    if explicit is not None:
        return truthy(explicit)
    data = resources or {}
    raw = data.get("openshell_required")
    if raw is not None:
        return _boolish(raw)
    names = {
        agent_id,
        agent_name,
        host,
        data.get("hostname"),
        data.get("host"),
    }
    required = {base_agent_name(name) for name in required_agent_names}
    return any(base_agent_name(name) in required for name in names if name)


def openshell_required_for_local_agent(
    environ: Optional[Mapping[str, str]] = None,
    *,
    fallback_name: Optional[str] = None,
) -> bool:
    """Determine whether OpenShell is required for the local agent from the environment."""
    env = os.environ if environ is None else environ
    if "MAC_OPENSHELL_REQUIRED" in env:
        return truthy(env.get("MAC_OPENSHELL_REQUIRED"))
    name = (
        env.get("MAC_AGENT_ID")
        or env.get("MAC_WORKER_AGENT_ID")
        or env.get("MAC_WORKER_AGENT_NAME")
        or fallback_name
        or os.uname().nodename
    )
    return openshell_required_for_identity(agent_id=name)


def apply_openshell_requirement(
    resources: Optional[Mapping[str, Any]],
    environ: MutableMapping[str, str],
) -> Optional[bool]:
    """Stamp ``MAC_OPENSHELL_REQUIRED`` into ``environ`` from an agent's runtime
    ``resources`` so a DB-driven sandbox requirement reaches the local executor
    (which inherits the worker process environment) WITHOUT a hardcoded agent
    list. This is the data-driven channel that replaces the old name allowlist:
    the hub owns ``resources["openshell_required"]`` per agent, the worker reads
    its own record back at registration, and this function projects that fact
    into the env the executor reads via :func:`openshell_required_for_local_agent`.

    Precedence: an existing ``MAC_OPENSHELL_REQUIRED`` (operator/deploy override)
    always wins and is left untouched. Otherwise, if ``resources`` carries an
    explicit ``openshell_required`` flag it is written as ``"1"``/``"0"``. A
    missing flag is a no-op — the executor keeps its own default — so this never
    silently flips an unconfigured agent. Returns the bool applied, or ``None``
    when nothing was written.
    """
    if "MAC_OPENSHELL_REQUIRED" in environ:
        return None
    raw = (resources or {}).get("openshell_required")
    if raw is None:
        return None
    value = _boolish(raw)
    environ["MAC_OPENSHELL_REQUIRED"] = "1" if value else "0"
    return value


# ---------------------------------------------------------------------------
# Sandbox lifecycle shape: create (upload) -> exec -> delete
# ---------------------------------------------------------------------------
#
# OpenShell 0.1 rejects ``sandbox create --upload X -- <command>`` ("the
# argument '--upload' cannot be used with '[COMMAND]...'"), and a trailing
# command becomes the sandbox's canonical main process: once it exits the
# sandbox leaves ``Ready`` and every later ``exec`` fails with "sandbox is not
# ready". The canary on 2026-10-03 failed task_b3e16b5f on exactly that.
#
# Every MAC flow that uploads files and/or execs afterwards therefore creates
# the sandbox with no command, kept alive, and runs its work with
# ``sandbox exec``. How to keep it alive differs by CLI generation:
#
# * 0.1.x: ``--detach`` and no command. The main process is an idle login
#   shell whose stdin the supervisor holds open, so the sandbox stays Ready.
# * 0.0.x: no ``--detach``, and with no command the CLI attaches an
#   interactive shell and never returns. A bounded ``/bin/true`` initial
#   command runs over exec and the kept sandbox stays available.
#
# Both shapes coexist so the fleet can roll host by host.

_OPENSHELL_VERSION_CACHE: dict[tuple[str, int, int], Optional[tuple[int, ...]]] = {}


def openshell_cli_version(openshell_bin: str) -> Optional[tuple[int, ...]]:
    """Return the OpenShell CLI version tuple, or None when it is unknown.

    Cached per resolved binary identity (path, mtime, size), so an in-place
    upgrade of the binary is observed without restarting the worker.
    """
    import re
    import shutil
    import subprocess

    resolved = shutil.which(openshell_bin) if os.sep not in openshell_bin else openshell_bin
    if not resolved:
        return None
    try:
        info = os.stat(resolved)
    except OSError:
        return None
    key = (os.path.realpath(resolved), info.st_mtime_ns, info.st_size)
    if key in _OPENSHELL_VERSION_CACHE:
        return _OPENSHELL_VERSION_CACHE[key]
    version: Optional[tuple[int, ...]] = None
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        match = re.search(r"\bopenshell\s+v?(\d+)\.(\d+)\.(\d+)", proc.stdout or "")
        if proc.returncode == 0 and match:
            version = tuple(int(part) for part in match.groups())
    except (OSError, subprocess.SubprocessError):
        version = None
    _OPENSHELL_VERSION_CACHE[key] = version
    return version


class OpenShellExecArgvError(ValueError):
    """An ``openshell sandbox exec`` argv OpenShell's exec RPC would reject."""


def assert_exec_argv_single_line(argv: Iterable[str]) -> list[str]:
    """Fail at build time if any ``sandbox exec`` argument spans lines.

    OpenShell's exec RPC rejects every command argument containing a newline
    or carriage return ("command argument N contains newline or carriage
    return characters"). Live on 0.0.72 that failed every task at the
    coding-agent preflight after work moved from ``create -- <cmd>`` to
    create-then-exec, while unit tests that mock OpenShell passed. Checking
    here turns the same mistake into an immediate internal error instead.
    """
    argv = [str(token) for token in argv]
    for index, token in enumerate(argv):
        if "\n" in token or "\r" in token:
            raise OpenShellExecArgvError(
                "internal error: openshell sandbox exec argument %d contains a "
                "newline or carriage return, which OpenShell rejects; encode "
                "multi-line scripts with single_line_shell_script()" % index
            )
    return argv


def single_line_shell_script(script: str) -> str:
    """Return a one-line ``bash -c`` body that runs the multi-line ``script``.

    The script travels base64-encoded and is decoded by the image's own
    coreutils, then evaluated in the same shell, so exit status, ``exec``,
    and output are unchanged. A decode failure exits 126 instead of
    evaluating an empty script as success. Single-line input is unchanged.
    """
    if "\n" not in script and "\r" not in script:
        return script
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        '__mac_script="$(printf %%s %s | /usr/bin/base64 -d)" || exit 126; '
        'eval "$__mac_script"' % encoded
    )


def openshell_create_keepalive_args(openshell_bin: str) -> list[str]:
    """Trailing ``sandbox create`` args that leave a kept sandbox Ready for exec.

    An unknown version gets the 0.1 shape: that is the reviewed fleet pin, and
    a 0.0.x CLI rejects ``--detach`` loudly rather than misbehaving.
    """
    version = openshell_cli_version(openshell_bin)
    if version is not None and version < (0, 1, 0):
        return ["--no-tty", "--", "/bin/true"]
    return ["--detach"]


def split_sandbox_create_command(
    argv: list[str],
    *,
    exec_args: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Split ``<bin> sandbox create ... -- <command>`` into create + exec argvs.

    The create keeps every flag (policy, labels, image, uploads, resources)
    except terminal flags, and ends with the generation's keep-alive tail. The
    exec runs the original command in that sandbox with ``--no-tty`` plus the
    caller's ``exec_args`` (for example ``--workdir`` or ``--timeout``).
    """
    if len(argv) < 3 or list(argv[1:3]) != ["sandbox", "create"]:
        raise ValueError("expected an `openshell sandbox create` argv")
    if "--" not in argv:
        raise ValueError("sandbox create argv has no command to exec")
    separator = argv.index("--")
    flags = [token for token in argv[3:separator] if token not in {"--tty", "--no-tty"}]
    command = list(argv[separator + 1 :])
    if not command:
        raise ValueError("sandbox create argv has an empty command")
    if "--name" not in flags or flags.index("--name") + 1 >= len(flags):
        raise ValueError("sandbox create argv must name the sandbox to exec into")
    name = flags[flags.index("--name") + 1]
    openshell_bin = argv[0]
    create = [openshell_bin, "sandbox", "create", *flags]
    create += openshell_create_keepalive_args(openshell_bin)
    exec_argv = [
        openshell_bin,
        "sandbox",
        "exec",
        "--name",
        name,
        "--no-tty",
        *exec_args,
        "--",
        *command,
    ]
    return create, assert_exec_argv_single_line(exec_argv)
