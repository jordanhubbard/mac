"""``mac admin worker-token``: operator-issued worker bearer tokens.

A small, human-run surface over the ``worker_credentials`` lifecycle.
``issue`` and ``rotate`` mint a new credential for one
agent, make it the agent's only active credential, and hand the raw token to
the operator exactly once: on stdout, in a mode-0600 ``--out`` file, or
straight into the worker's ``~/.mac/mac.env`` with ``--install HOST``.

The token is never logged, never put in a process argument list, and never
written anywhere but the destinations above.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

DEFAULT_DAYS = 365

# Runs on the worker under its own venv Python; the token arrives on stdin.
# Self-contained apart from mac.deploy_env so it works against whatever mac
# version the worker currently runs.  Every MAC_WORKER_TOKEN / _<FLEET> key
# the host already uses is rewritten (MAC_WORKER_TOKEN when it has none), and
# hub-facing aliases that held the previous token move with it, so no copy of
# the old token survives on the host.
REMOTE_ENV_UPDATE = r"""
import sys
from pathlib import Path
from mac.deploy_env import env_file_lock, read_env_file, write_env_file

token = sys.stdin.read().strip()
if not token.startswith("mac_worker_"):
    sys.exit("refusing: stdin is not a worker token")
path = Path(sys.argv[1]).expanduser()
with env_file_lock(path):
    values = read_env_file(path)
    keys = sorted(k for k in values if k == "MAC_WORKER_TOKEN" or k.startswith("MAC_WORKER_TOKEN__"))
    keys = keys or ["MAC_WORKER_TOKEN"]
    previous = {values[k] for k in keys if values.get(k)}
    for key in keys:
        values[key] = token
    aliases = ("OPENAI_API_KEY", "MAC_HERMES_GATEWAY_API_KEY", "ACC_HERMES_GATEWAY_API_KEY", "NVIDIA_API_KEY")
    moved = [a for a in aliases if values.get(a) and values[a] in previous]
    for alias in moved:
        values[alias] = token
    write_env_file(path, values)
print("mac.env: updated " + ", ".join(keys + moved))
"""

# Bash run on the worker (read from stdin, so nothing here reaches argv).
REMOTE_INSTALL = """set -euo pipefail
umask 077
env_file="$HOME/.mac/mac.env"
py="$HOME/.mac/venv/bin/python"
printf '%s' {token} | "$py" -c {script} "$env_file"
# From here on the host holds the new token: report failures as exit 3.
trap 'exit {installed_rc}' ERR
if [ -d "$HOME/.hermes" ]; then
  "$py" -m mac.hermes_chat_config --hermes-home "$HOME/.hermes" --mac-env "$env_file"
fi
if [ "$(uname)" = Linux ]; then
  sudo -n systemctl restart mac-agent
  systemctl --user restart hermes-gateway || echo "warning: hermes-gateway restart failed" >&2
else
  launchctl kickstart -k "gui/$(id -u)/com.mac.agent"
  launchctl kickstart -k "gui/$(id -u)/ai.hermes.gateway" || echo "warning: hermes restart failed" >&2
fi
"""
# Remote exit status meaning "mac.env has the new token, a later step failed".
INSTALLED_BUT_RESTART_FAILED = 3


class InstallError(RuntimeError):
    def __init__(self, message: str, *, token_installed: bool) -> None:
        super().__init__(message)
        self.token_installed = token_installed


def remote_install_script(token: str) -> str:
    """The stdin script ``--install`` sends over ssh.  Contains the token."""

    return REMOTE_INSTALL.format(
        token=shlex.quote(token),
        script=shlex.quote(REMOTE_ENV_UPDATE),
        installed_rc=INSTALLED_BUT_RESTART_FAILED,
    )


def install_on_host(host: str, token: str, *, run: Callable[..., Any] = subprocess.run) -> None:
    """Write ``token`` into HOST's mac.env, resync Hermes, restart services."""

    result = run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "bash", "-s"],
        input=remote_install_script(token),
        text=True,
        check=False,
    )
    if result.returncode == INSTALLED_BUT_RESTART_FAILED:
        raise InstallError(
            "token written to %s's mac.env, but the Hermes resync or service restart failed" % host,
            token_installed=True,
        )
    if result.returncode != 0:
        raise InstallError(
            "install on %s failed (ssh exit %s)" % (host, result.returncode),
            token_installed=False,
        )


def _write_token_file(path: Path, token: str) -> None:
    path = path.expanduser()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(token + "\n")


def _lifecycle(args: argparse.Namespace) -> Any:
    from mac.store import make_store_from_env, open_postgres_store
    from mac.worker_credentials import WorkerCredentialLifecycle

    # Row-level credential changes only; never replay schema DDL against the
    # live hub authority.
    dsn = getattr(args, "db", None)
    store = (
        open_postgres_store(dsn, initialize_schema=False)
        if dsn
        else make_store_from_env(initialize_schema=False)
    )
    return WorkerCredentialLifecycle(store)


def _summary(record: dict, **extra: Any) -> dict:
    keys = ("agent_id", "id", "credential_version", "token_fingerprint", "state", "expires_at")
    return {**{k: record.get(k) for k in keys}, **extra}


def _refuse_on_error(func: Callable[[argparse.Namespace], None]) -> Callable[..., None]:
    def wrapped(args: argparse.Namespace) -> None:
        from mac.store import StoreError
        from mac.worker_credentials import WorkerCredentialError

        try:
            func(args)
        except (WorkerCredentialError, StoreError) as exc:
            # Messages are built from identifiers only; never from token values.
            print("worker-token: %s" % exc, file=sys.stderr)
            raise SystemExit(1) from None

    wrapped.__doc__ = func.__doc__
    return wrapped


def cmd_worker_token_issue(args: argparse.Namespace) -> None:
    """Issue or rotate one agent's worker token and deliver it exactly once."""
    if args.days < 1:
        raise SystemExit("--days must be at least 1")
    lifecycle = _lifecycle(args)
    actor = args.actor or "%s@%s" % (getpass.getuser(), socket.gethostname())
    issued = lifecycle.issue(args.agent_id, expires_in=args.days * 86400, actor=actor)
    principal_id = issued.record["id"]
    token = issued.token
    if args.out:
        _write_token_file(Path(args.out), token)
    if args.install:
        # Install while the new credential is still pending: the old one keeps
        # authenticating until the worker has the new one, then is superseded.
        try:
            install_on_host(args.install, token)
        except InstallError as exc:
            if exc.token_installed:
                # The host already has the new token: activating it is the only
                # state in which the worker can authenticate once restarted.
                lifecycle.activate(args.agent_id, principal_id)
                print(
                    "worker-token: %s; the new credential is active -- restart mac-agent "
                    "and hermes-gateway on %s by hand" % (exc, args.install),
                    file=sys.stderr,
                )
                raise SystemExit(1) from None
            lifecycle.revoke(args.agent_id, principal_id)
            print(
                "worker-token: %s; the new credential was revoked and the previous one "
                "is still active" % exc,
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    record = lifecycle.activate(args.agent_id, principal_id)
    meta = _summary(
        record,
        token_written_to=str(Path(args.out).expanduser()) if args.out else None,
        installed_on=args.install,
    )
    if args.out or args.install:
        print(json.dumps(meta, sort_keys=True))
    else:
        print(json.dumps(meta, sort_keys=True), file=sys.stderr)
        print(token)


def cmd_worker_token_list(args: argparse.Namespace) -> None:
    """List worker credentials without token hashes."""
    rows = _lifecycle(args).list(agent_id=args.agent_id or "")
    print(json.dumps(rows, indent=2, sort_keys=True))


def register(sub: Any) -> None:
    """Add the ``worker-token`` group (re-parented under ``mac admin``)."""

    group = sub.add_parser(
        "worker-token", help="issue, rotate and list long-lived worker bearer tokens"
    ).add_subparsers(dest="worker_token_command", required=True)
    for verb, text in (
        ("issue", "mint a worker token; it becomes the agent's only active credential"),
        ("rotate", "replace an agent's worker token (same as issue)"),
    ):
        parser = group.add_parser(verb, help=text)
        parser.add_argument("agent_id")
        parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
        parser.add_argument("--out", help="write the token to this file (0600) instead of stdout")
        parser.add_argument(
            "--install",
            metavar="HOST",
            help="ssh to HOST, put the token in ~/.mac/mac.env, resync Hermes, restart services",
        )
        parser.add_argument("--actor", help="recorded as the credential's creator")
        parser.set_defaults(func=_refuse_on_error(cmd_worker_token_issue))
    listing = group.add_parser("list", help="list worker credentials (never the token)")
    listing.add_argument("agent_id", nargs="?")
    listing.set_defaults(func=_refuse_on_error(cmd_worker_token_list))
