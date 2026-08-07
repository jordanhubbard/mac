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

#: BOTH interfaces are multi-account. They differ in ENCODING, not in model.
#:
#:   OpenClaw: namespaced env keys --
#:             MAC_OPENCLAW_SLACK_<ACCOUNT>_BOT_TOKEN / _APP_TOKEN, with
#:             MAC_OPENCLAW_SLACK_ACCOUNT_ID naming the default account.
#:   Hermes:   a JSON array at ~/.hermes/slack_accounts.json --
#:             [{"name": ..., "bot_token": ..., "app_token": ...}, ...].
#:             Added by deploy/hermes/multi-slack-mvp.patch, which gives each
#:             account its own AsyncApp and its own Socket Mode websocket.
#:             Flat SLACK_BOT_TOKEN / SLACK_APP_TOKEN remain a single-account
#:             FALLBACK, used only when the JSON file is absent.
#:
#: Getting this wrong loses a workspace silently. Verified on the hub
#: 2026-08-04: BOTH sides already carry the same two accounts, `omgjkh` and
#: `offtera`. A port that wrote only the active account into the flat keys
#: would drop `offtera` while reporting success -- so the port is a UNION over
#: accounts, and an account present only at the TARGET is always preserved.
HERMES_ACCOUNTS_FILE = "slack_accounts.json"
OPENCLAW_ACCOUNT_KEY = "MAC_OPENCLAW_SLACK_ACCOUNT_ID"
OPENCLAW_TOKEN_TEMPLATE = "MAC_OPENCLAW_SLACK_%s_%s"

#: Hermes-only extras that are NOT part of an account. Socket Mode carries no
#: inbound HTTP request, so there are no signatures to verify and no signing
#: secret is required; it is reported as not-required rather than missing, so
#: an operator does not read a complete port as a failed one.
FLAT_FALLBACK_KEYS: Tuple[str, ...] = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
SOCKET_MODE_NOT_REQUIRED: Tuple[str, ...] = ("SLACK_SIGNING_SECRET",)

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
    #: Hermes only: the multi-account JSON array. When present it is the
    #: authoritative account list and the flat env keys are ignored.
    accounts_file: Optional[Path] = None

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
        accounts_file=hermes_home / HERMES_ACCOUNTS_FILE,
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


@dataclass(frozen=True)
class SlackAccount:
    """One Slack workspace, in the encoding-independent form both sides share."""

    name: str
    bot_token: str
    app_token: str


@dataclass
class PortReport:
    """What a port did, or would do."""

    source: str
    target: str
    dry_run: bool
    ported: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, str]] = field(default_factory=list)
    #: Slack workspaces written to the target from the source.
    accounts_ported: List[str] = field(default_factory=list)
    #: Already identical on both sides.
    accounts_unchanged: List[str] = field(default_factory=list)
    #: Known only to the TARGET and carried through untouched. A non-empty
    #: list here is the port declining to lose a workspace.
    accounts_preserved: List[str] = field(default_factory=list)
    #: Missing a bot or app token, so the gateway would skip them.
    accounts_incomplete: List[str] = field(default_factory=list)
    credentials_ported: List[str] = field(default_factory=list)
    credentials_missing: List[str] = field(default_factory=list)
    #: Keys the TARGET does not need, distinguished from keys it needs and
    #: lacks, so a complete port is not read as a failed one.
    credentials_not_required: List[str] = field(default_factory=list)
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
            "accounts_ported": list(self.accounts_ported),
            "accounts_unchanged": list(self.accounts_unchanged),
            "accounts_preserved": list(self.accounts_preserved),
            "accounts_incomplete": list(self.accounts_incomplete),
            "credentials_ported": list(self.credentials_ported),
            "credentials_missing": list(self.credentials_missing),
            "credentials_not_required": list(self.credentials_not_required),
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

    def _merged_env(self, layout: InterfaceLayout) -> Dict[str, str]:
        env: Dict[str, str] = {}
        for candidate in layout.credential_files or (layout.env_file,):
            env.update(parse_env(candidate))
        return env

    def _read_openclaw_accounts(
        self, layout: InterfaceLayout, report: PortReport
    ) -> Dict[str, SlackAccount]:
        """Collect EVERY namespaced account, not just the active one."""
        env = self._merged_env(layout)
        tokens: Dict[str, Dict[str, str]] = {}
        for key, value in env.items():
            for suffix, field_name in (("_BOT_TOKEN", "bot_token"),
                                       ("_APP_TOKEN", "app_token")):
                prefix = "MAC_OPENCLAW_SLACK_"
                if key.startswith(prefix) and key.endswith(suffix):
                    name = key[len(prefix):-len(suffix)]
                    if not name or key == OPENCLAW_ACCOUNT_KEY:
                        continue
                    value = str(value or "").strip()
                    if value:
                        tokens.setdefault(name.lower(), {})[field_name] = value
        return self._finalise_accounts(tokens, report)

    def _read_hermes_accounts(
        self, layout: InterfaceLayout, report: PortReport
    ) -> Dict[str, SlackAccount]:
        """slack_accounts.json is authoritative; flat env is the fallback."""
        path = layout.accounts_file
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                report.errors.append("%s: unreadable (%s)" % (path.name, exc))
                return {}
            if not isinstance(data, list):
                report.errors.append("%s: must contain a JSON array" % path.name)
                return {}
            tokens: Dict[str, Dict[str, str]] = {}
            for index, entry in enumerate(data):
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "account-%d" % index).strip().lower()
                tokens[name] = {
                    "bot_token": str(entry.get("bot_token") or "").strip(),
                    "app_token": str(entry.get("app_token") or "").strip(),
                }
            return self._finalise_accounts(tokens, report)

        env = self._merged_env(layout)
        bot = str(env.get("SLACK_BOT_TOKEN") or "").strip()
        app = str(env.get("SLACK_APP_TOKEN") or "").strip()
        if not bot and not app:
            return {}
        return self._finalise_accounts(
            {"default": {"bot_token": bot, "app_token": app}}, report
        )

    def _finalise_accounts(
        self, tokens: Dict[str, Dict[str, str]], report: PortReport
    ) -> Dict[str, SlackAccount]:
        """Drop accounts the gateway would refuse, and SAY SO.

        The patched adapter skips any account missing either token. Silently
        dropping them here would make a partial port read as a complete one.
        """
        accounts: Dict[str, SlackAccount] = {}
        for name, pair in tokens.items():
            bot = str(pair.get("bot_token") or "").strip()
            app = str(pair.get("app_token") or "").strip()
            if bot and app:
                accounts[name] = SlackAccount(name=name, bot_token=bot, app_token=app)
            elif bot or app:
                report.accounts_incomplete.append(name)
        return accounts

    def _read_accounts(
        self, layout: InterfaceLayout, report: PortReport
    ) -> Dict[str, SlackAccount]:
        if layout.name == OPENCLAW:
            return self._read_openclaw_accounts(layout, report)
        return self._read_hermes_accounts(layout, report)

    def _write_hermes_accounts(
        self, accounts: Dict[str, SlackAccount]
    ) -> None:
        payload = [
            {"name": a.name, "bot_token": a.bot_token, "app_token": a.app_token}
            for a in accounts.values()
        ]
        _atomic_write(
            self.target.accounts_file, json.dumps(payload, indent=2) + "\n"
        )

    def _write_openclaw_accounts(
        self, accounts: Dict[str, SlackAccount], default: Optional[str]
    ) -> None:
        updates: Dict[str, str] = {}
        for account in accounts.values():
            upper = account.name.upper()
            updates[OPENCLAW_TOKEN_TEMPLATE % (upper, "BOT_TOKEN")] = account.bot_token
            updates[OPENCLAW_TOKEN_TEMPLATE % (upper, "APP_TOKEN")] = account.app_token
        existing = self._merged_env(self.target)
        if default and not str(existing.get(OPENCLAW_ACCOUNT_KEY) or "").strip():
            updates[OPENCLAW_ACCOUNT_KEY] = default
        upsert_env(self.target.env_file, updates)

    def _port_credentials(self, report: PortReport) -> None:
        source_accounts = self._read_accounts(self.source, report)
        target_accounts = self._read_accounts(self.target, report)

        # Union, source-preferred: the interface the agent used LAST holds the
        # freshest tokens, so it wins where both describe the same account. An
        # account only the target knows about is carried through untouched --
        # that is the invariant that stops a port losing a workspace.
        # Plain dicts: insertion order is guaranteed, and it is what fixes the
        # account order written back out, so the target's existing accounts are
        # seeded first and keep their positions.
        merged: Dict[str, SlackAccount] = {}
        for name, account in target_accounts.items():
            merged[name] = account
        for name, account in source_accounts.items():
            if name in target_accounts and target_accounts[name] == account:
                report.accounts_unchanged.append(name)
            else:
                report.accounts_ported.append(name)
            merged[name] = account
        report.accounts_preserved.extend(
            name for name in target_accounts if name not in source_accounts
        )

        if not source_accounts:
            report.credentials_missing.extend(FLAT_FALLBACK_KEYS)
            return

        if self.target.name == HERMES:
            # Socket Mode verifies no inbound signatures, so a signing secret
            # is not a gap in the port.
            report.credentials_not_required.extend(SOCKET_MODE_NOT_REQUIRED)
            if not report.dry_run:
                self._write_hermes_accounts(merged)
        else:
            default = None
            source_env = self._merged_env(self.source)
            for candidate in (str(source_env.get(OPENCLAW_ACCOUNT_KEY) or "").strip(),
                              next(iter(source_accounts), "")):
                if candidate:
                    default = candidate.lower()
                    break
            if not report.dry_run:
                self._write_openclaw_accounts(merged, default)

    # -- entry point ------------------------------------------------------

    def source_fingerprint(self) -> str:
        """Digest of the SOURCE identity documents this port would carry.

        A completion timestamp alone cannot say whether a port is still valid:
        the source keeps accumulating knowledge after it, and an hour-old port
        of a since-changed MEMORY.md is exactly as lossy as no port at all.
        Recording what the source looked like lets the gate answer the question
        that matters -- "has anything happened since?" -- instead of the
        question that is merely easy.
        """
        hasher = hashlib.sha256()
        for name in IDENTITY_FILES:
            # identity_path, not identity_dir: Hermes also keeps these under
            # memories/, and resolving the same way the port does is what makes
            # the fingerprint describe what would actually be carried.
            found = self.source.identity_path(name)
            hasher.update(name.encode("utf-8"))
            hasher.update(((found and _digest_file(found)) or "-").encode("utf-8"))
        return hasher.hexdigest()

    def run(self, *, dry_run: bool = True) -> Dict[str, Any]:
        report = PortReport(
            source=self.source.name, target=self.target.name, dry_run=bool(dry_run)
        )
        updates: Dict[str, str] = {}
        for name in IDENTITY_FILES:
            self._port_identity_file(name, report, updates)
        self._port_credentials(report)
        result = report.to_dict()
        if not dry_run:
            # Recorded even when `updates` is empty. A re-port that changed
            # nothing because everything was already in place IS a completed
            # port, and refusing to record it would make an idempotent port
            # look like one that never ran.
            updates[_completion_key(self.source.name, self.target.name)] = json.dumps(
                {
                    "at": _utcnow(),
                    "clean": bool(result.get("clean")),
                    "source_fingerprint": self.source_fingerprint(),
                },
                sort_keys=True,
            )
            self._save_state(updates)
        return result


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


# ---------------------------------------------------------------------------
# The switch-time gate
#
# Porting existing and porting being ENFORCED are different things. This module
# could port in both directions for four weeks and none of it ran, because
# nothing on the switch path called it: the operator rule "port before you
# switch" lived in a ticket, and a ticket cannot stop a deploy. That is the
# same shape as the defects around it -- a correct decision with no consumer --
# and it is what these functions close.
# ---------------------------------------------------------------------------


def switch_readiness(
    target: str,
    *,
    home: Optional[Path] = None,
    state_file: Optional[Path] = None,
    max_age_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Report whether switching TO ``target`` would lose the agent's profile.

    Read-only, and never raises for an unready state -- callers that must stop
    use :func:`assert_switch_ported`. Returns a dict with ``ready`` and, when
    it is False, a ``reason`` naming which of the conditions failed:

    ``never_ported``
        No completed port into ``target`` has ever run on this host.
    ``source_changed``
        One ran, but the source's identity documents have changed since, so
        the target is missing whatever accumulated after it. This is the
        condition a timestamp cannot see.
    ``stale``
        Older than ``max_age_seconds``, when the caller supplies one.
    ``unclean``
        The port completed with conflicts or errors, so an operator still has
        candidate files to reconcile and the target is not yet whole.
    """
    target = str(target or "").strip().lower()
    source = _other_interface(target)
    port = ProfilePort(source, target, home=home, state_file=state_file)
    record = _load_completion(port, source, target)
    result: Dict[str, Any] = {
        "schema": PROFILE_PORT_SCHEMA,
        "target": target,
        "source": source,
        "ready": False,
        "last_port": record,
    }
    if not record:
        result["reason"] = "never_ported"
        return result
    if not record.get("clean", False):
        result["reason"] = "unclean"
        return result
    current = port.source_fingerprint()
    if str(record.get("source_fingerprint") or "") != current:
        result["reason"] = "source_changed"
        result["current_source_fingerprint"] = current
        return result
    if max_age_seconds is not None:
        age = _age_seconds(str(record.get("at") or ""))
        result["age_seconds"] = age
        if age is None or age > float(max_age_seconds):
            result["reason"] = "stale"
            return result
    result["ready"] = True
    return result


def assert_switch_ported(
    target: str,
    *,
    home: Optional[Path] = None,
    state_file: Optional[Path] = None,
    max_age_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Raise :class:`ProfilePortError` unless ``target`` has a current profile.

    Fails LOUDLY and names the command that fixes it. A gate an operator
    cannot act on gets bypassed, and a bypassed gate is worse than none --
    it costs a deploy and still loses the memory.
    """
    readiness = switch_readiness(
        target, home=home, state_file=state_file, max_age_seconds=max_age_seconds
    )
    if readiness["ready"]:
        return readiness
    reason = readiness.get("reason")
    detail = {
        "never_ported": "no profile has ever been ported into %s on this host" % target,
        "source_changed": (
            "%s has changed since the last port, so %s is missing everything "
            "written since" % (readiness["source"], target)
        ),
        "stale": "the last port into %s is older than the allowed window" % target,
        "unclean": (
            "the last port into %s finished with conflicts that are still "
            "unreconciled" % target
        ),
    }.get(str(reason), "the profile in %s cannot be shown to be current" % target)
    raise ProfilePortError(
        "refusing to switch the human interface to %s: %s. "
        "Run `mac human-interface port --from %s --to %s --apply` first, or "
        "`mac human-interface check --to %s` to see what is missing."
        % (target, detail, readiness["source"], target, target)
    )


def _other_interface(interface: str) -> str:
    if interface == "hermes":
        return "openclaw"
    if interface == "openclaw":
        return "hermes"
    raise ProfilePortError(
        "unknown human interface %r (expected hermes or openclaw)" % interface
    )


def _completion_key(source: str, target: str) -> str:
    return "__port__:%s->%s" % (source, target)


def _load_completion(
    port: "ProfilePort", source: str, target: str
) -> Optional[Dict[str, Any]]:
    raw = port._previous.get(_completion_key(source, target))
    if not raw:
        return None
    try:
        record = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_seconds(stamp: str) -> Optional[float]:
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()
