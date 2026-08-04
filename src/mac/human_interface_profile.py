"""Port an agent's profile between human interfaces (Hermes <-> OpenClaw).

A *human interface* is the component that faces humans and the plugin
ecosystem: Hermes or OpenClaw. MAC supports both and activates one per agent
(``gateway_impl``). Switching the active interface without moving the agent's
accumulated state silently reverts its identity, memory and messaging
credentials to whatever the other interface last held.

That is not hypothetical. Measured on the hub 2026-08-04, four weeks after the
fleet moved to OpenClaw:

* ``SOUL.md`` was identical on both sides -- no loss.
* ``MEMORY.md`` had **diverged and become disjoint**. OpenClaw's copy carried
  April-July operational knowledge (thread etiquette, a mandatory
  context-search directive, the AgentFS canonical-storage rule, safety-filter
  workarounds); Hermes' April copy carried the record of the *previous*
  migration -- including its hard-won fix, that Slack tokens do not port
  automatically. Neither was a superset. A file copy in either direction
  destroys real operational knowledge.
* The Slack signing secret was absent from ``~/.hermes/.env`` entirely, because
  the projection that writes it is gated behind a non-OpenClaw gateway and had
  not run in four weeks.

So the rule this module implements is: **the interface the agent last used is
authoritative, and its profile must be ported before the switch** -- in either
direction.

Design, inherited deliberately from
``deploy/openclaw/migrate-hermes-continuity.py`` rather than reinvented:

* **The source is never modified.** Porting is a read of one tree and a write
  into the other.
* **Merge by preservation, never by overwrite.** When a destination file
  differs from both the proposed content and the hash recorded by the previous
  port, the destination is kept and the candidate is written beside it for an
  operator to reconcile. Divergent content is therefore never lost -- it is
  made visible.
* **Idempotent.** Re-porting unchanged state is a no-op.
* **Dry-run by default.** Porting mutates an agent's identity; the caller must
  ask for it explicitly.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROFILE_PORT_SCHEMA = "mac.human_interface_profile_port.v1"

#: Identity documents carried between interfaces. SOUL.md is the personality,
#: USER.md the operator model, MEMORY.md the durable operational knowledge.
IDENTITY_FILES: Tuple[str, ...] = ("SOUL.md", "USER.md", "MEMORY.md")

#: Hermes' messaging credentials: single-account and flat.
MESSAGING_KEYS: Tuple[str, ...] = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
)

#: The two interfaces do not merely NAME credentials differently -- they use
#: different credential MODELS, so porting is a translation, not a copy.
#:
#:   OpenClaw: multi-account and namespaced. MAC_OPENCLAW_SLACK_<ACCOUNT>_BOT_TOKEN
#:             and _APP_TOKEN, with MAC_OPENCLAW_SLACK_ACCOUNT_ID naming the
#:             active account. Verified on the hub: OMGJKH and OFFTERA both
#:             present.
#:   Hermes:   single-account and flat. SLACK_BOT_TOKEN / SLACK_APP_TOKEN /
#:             SLACK_SIGNING_SECRET.
#:
#: SLACK_SIGNING_SECRET HAS NO OPENCLAW SOURCE. OpenClaw connects over Socket
#: Mode using app+bot tokens; Hermes' slack_bolt additionally verifies request
#: signatures. Porting therefore CANNOT produce it -- it must come from the hub
#: vault. Reporting it as merely "missing" would imply the port could supply it,
#: so it is reported separately as unavailable-from-source.
OPENCLAW_ACCOUNT_KEY = "MAC_OPENCLAW_SLACK_ACCOUNT_ID"
OPENCLAW_TOKEN_TEMPLATE = "MAC_OPENCLAW_SLACK_%s_%s"
UNAVAILABLE_FROM_OPENCLAW: Tuple[str, ...] = ("SLACK_SIGNING_SECRET",)

HERMES = "hermes"
OPENCLAW = "openclaw"
INTERFACES: Tuple[str, ...] = (HERMES, OPENCLAW)


class ProfilePortError(RuntimeError):
    """A profile port cannot be performed safely."""


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> Optional[str]:
    try:
        return _digest_bytes(path.read_bytes())
    except OSError:
        return None


def _atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class InterfaceLayout:
    """Where one human interface keeps the state a port must move.

    The two interfaces do NOT share locations. Hermes keeps identity and its
    messaging env under ``~/.hermes``; OpenClaw keeps identity in its workspace
    under ``~/.mac/openclaw`` and messaging config in its managed tree.
    """

    name: str
    identity_dir: Path
    env_file: Path
    #: Extra directories searched (in order) for an identity document, because
    #: Hermes historically stored them under ``memories/`` as well.
    identity_fallbacks: Tuple[Path, ...] = ()
    #: Files searched for messaging credentials, in order (later wins).
    credential_files: Tuple[Path, ...] = ()

    def identity_path(self, name: str) -> Optional[Path]:
        for directory in (self.identity_dir, *self.identity_fallbacks):
            candidate = directory / name
            if candidate.is_file():
                return candidate
        return None


def hermes_layout(home: Path) -> InterfaceLayout:
    hermes_home = home / ".hermes"
    return InterfaceLayout(
        name=HERMES,
        identity_dir=hermes_home,
        env_file=hermes_home / ".env",
        identity_fallbacks=(hermes_home / "memories",),
        credential_files=(hermes_home / ".env",),
    )


def openclaw_layout(home: Path) -> InterfaceLayout:
    openclaw_home = home / ".mac" / "openclaw"
    return InterfaceLayout(
        name=OPENCLAW,
        identity_dir=openclaw_home / "workspace",
        env_file=openclaw_home / "managed" / "runtime.env",
        identity_fallbacks=(openclaw_home / "state",),
        credential_files=(
            openclaw_home / "managed" / "runtime.env",
            openclaw_home / "credentials.env",
        ),
    )


def layout_for(interface: str, home: Path) -> InterfaceLayout:
    interface = str(interface or "").strip().lower()
    if interface == HERMES:
        return hermes_layout(home)
    if interface == OPENCLAW:
        return openclaw_layout(home)
    raise ProfilePortError(
        "unknown human interface %r; expected one of %s"
        % (interface, ", ".join(INTERFACES))
    )


def parse_env(path: Path) -> Dict[str, str]:
    """Read a shell-style env file into a mapping, tolerating junk."""
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :]
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def upsert_env(path: Path, updates: Dict[str, str]) -> None:
    """Write ``updates`` into an env file, PRESERVING every other line.

    Deliberately line-preserving: the target env holds keys this port knows
    nothing about, and rewriting the file from a parsed mapping would drop
    comments and unrecognised entries.
    """
    existing_lines: List[str] = []
    try:
        existing_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        existing_lines = []

    remaining = dict(updates)
    output: List[str] = []
    for line in existing_lines:
        stripped = line.strip()
        body = stripped[len("export ") :] if stripped.startswith("export ") else stripped
        key, separator, _ = body.partition("=")
        key = key.strip()
        if separator and key in remaining:
            prefix = "export " if stripped.startswith("export ") else ""
            output.append("%s%s=%s" % (prefix, key, remaining.pop(key)))
            continue
        output.append(line)
    for key in sorted(remaining):
        output.append("%s=%s" % (key, remaining[key]))
    _atomic_write(path, "\n".join(output) + "\n")


@dataclass
class PortReport:
    """What a port did, or would do."""

    source: str
    target: str
    dry_run: bool
    ported: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, str]] = field(default_factory=list)
    credentials_ported: List[str] = field(default_factory=list)
    credentials_missing: List[str] = field(default_factory=list)
    #: Keys the target needs that the SOURCE model cannot supply at all.
    credentials_unavailable: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": PROFILE_PORT_SCHEMA,
            "source": self.source,
            "target": self.target,
            "dry_run": self.dry_run,
            "ported": list(self.ported),
            "unchanged": list(self.unchanged),
            "conflicts": list(self.conflicts),
            "credentials_ported": list(self.credentials_ported),
            "credentials_missing": list(self.credentials_missing),
            "credentials_unavailable": list(self.credentials_unavailable),
            "errors": list(self.errors),
            "clean": not self.conflicts and not self.errors,
        }


class ProfilePort:
    """Port an agent profile from one human interface to the other."""

    def __init__(
        self,
        source: str,
        target: str,
        *,
        home: Optional[Path] = None,
        state_file: Optional[Path] = None,
    ) -> None:
        source = str(source or "").strip().lower()
        target = str(target or "").strip().lower()
        if source == target:
            raise ProfilePortError(
                "source and target are the same interface (%r); nothing to port"
                % source
            )
        self.home = Path(home) if home is not None else Path.home()
        self.source = layout_for(source, self.home)
        self.target = layout_for(target, self.home)
        self.state_file = (
            Path(state_file)
            if state_file is not None
            else self.home / ".mac" / "human-interface-port-state.json"
        )
        self._previous: Dict[str, str] = self._load_state()

    # -- previously-ported hashes ----------------------------------------

    def _state_key(self, relative: str) -> str:
        return "%s->%s:%s" % (self.source.name, self.target.name, relative)

    def _load_state(self) -> Dict[str, str]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, updates: Dict[str, str]) -> None:
        merged = dict(self._previous)
        merged.update(updates)
        _atomic_write(self.state_file, json.dumps(merged, indent=1, sort_keys=True) + "\n")

    # -- identity ---------------------------------------------------------

    def _port_identity_file(
        self, name: str, report: PortReport, updates: Dict[str, str]
    ) -> None:
        source_path = self.source.identity_path(name)
        if source_path is None:
            return
        try:
            content = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report.errors.append("%s: unreadable source (%s)" % (name, exc))
            return
        if not content.endswith("\n"):
            content += "\n"
        proposed = _digest_bytes(content.encode("utf-8"))

        destination = self.target.identity_dir / name
        current = _digest_file(destination) if destination.is_file() else None
        previous = self._previous.get(self._state_key(name))

        if current == proposed:
            report.unchanged.append(name)
            updates[self._state_key(name)] = proposed
            return

        if current is not None and (previous is None or current != previous):
            # The target has content this port did not write. It may hold
            # knowledge the source lacks -- on the hub, the two MEMORY.md files
            # were disjoint -- so the destination is PRESERVED and the incoming
            # version is written beside it for a human to reconcile.
            candidate = destination.with_name(destination.name + ".incoming")
            if not report.dry_run:
                _atomic_write(candidate, content)
            report.conflicts.append(
                {
                    "file": name,
                    "reason": "target has unmanaged local content; not overwritten",
                    "preserved": str(destination),
                    "candidate": str(candidate),
                    "source": str(source_path),
                }
            )
            return

        if not report.dry_run:
            _atomic_write(destination, content)
        report.ported.append(name)
        updates[self._state_key(name)] = proposed

    # -- credentials ------------------------------------------------------

    def _openclaw_messaging(self, env: Dict[str, str]) -> Dict[str, str]:
        """Translate OpenClaw's namespaced Slack keys into Hermes' flat ones.

        Uses MAC_OPENCLAW_SLACK_ACCOUNT_ID to pick the active account, so a
        multi-account host ports the account it is actually serving rather than
        an arbitrary one.
        """
        account = str(env.get(OPENCLAW_ACCOUNT_KEY) or "").strip()
        if not account:
            return {}
        translated: Dict[str, str] = {}
        for flat, suffix in (("SLACK_BOT_TOKEN", "BOT_TOKEN"),
                             ("SLACK_APP_TOKEN", "APP_TOKEN")):
            namespaced = OPENCLAW_TOKEN_TEMPLATE % (account.upper(), suffix)
            value = str(env.get(namespaced) or "").strip()
            if value:
                translated[flat] = value
        return translated

    def _source_messaging(self) -> Tuple[Dict[str, str], List[str]]:
        """Read messaging credentials from the source in ITS OWN model."""
        if self.source.name == OPENCLAW:
            # OpenClaw keeps them in the managed runtime env and a sibling
            # credentials file; read both, later wins.
            env: Dict[str, str] = {}
            for candidate in self.source.credential_files:
                env.update(parse_env(candidate))
            return self._openclaw_messaging(env), list(UNAVAILABLE_FROM_OPENCLAW)
        env = parse_env(self.source.env_file)
        present = {
            key: env[key] for key in MESSAGING_KEYS if str(env.get(key) or "").strip()
        }
        return present, []

    def _port_credentials(self, report: PortReport) -> None:
        present, unavailable = self._source_messaging()
        report.credentials_unavailable.extend(unavailable)
        report.credentials_missing.extend(
            key
            for key in MESSAGING_KEYS
            if key not in present and key not in unavailable
        )
        if not present:
            return
        if not report.dry_run:
            upsert_env(self.target.env_file, present)
        report.credentials_ported.extend(sorted(present))

    # -- entry point ------------------------------------------------------

    def run(self, *, dry_run: bool = True) -> Dict[str, Any]:
        report = PortReport(
            source=self.source.name, target=self.target.name, dry_run=bool(dry_run)
        )
        updates: Dict[str, str] = {}
        for name in IDENTITY_FILES:
            self._port_identity_file(name, report, updates)
        self._port_credentials(report)
        if not dry_run and updates:
            self._save_state(updates)
        return report.to_dict()


def port_profile(
    source: str,
    target: str,
    *,
    home: Optional[Path] = None,
    dry_run: bool = True,
    state_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Port an agent profile between human interfaces. Dry-run by default."""
    return ProfilePort(source, target, home=home, state_file=state_file).run(
        dry_run=dry_run
    )
