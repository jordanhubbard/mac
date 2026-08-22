"""Data-driven provider CRUD wrapper: providers are JSON specs, not mac source.

mac's capacity providers used to be Python modules that hard-wired one vendor's
CLI (``mac.hgx_provider`` wraps NVIDIA's internal ``hgx`` tool). That makes mac
unusable outside the network where that tool exists, and it gives a user with a
different cloud no way to express their own provider short of patching mac.

This module is the other half of ``docs/adr/0028-a-provider-is-data-not-source.md``:
mac core carries an **interpreter**, and a *provider* is a JSON description of
which binary to invoke, with which arguments, for each CRUD verb, plus how to
read the result back into mac's provider model. ``docs/provider-specs.md`` is
the authoring guide.

Three properties are load-bearing and are enforced here rather than documented
and hoped for:

- **argv is built, never a shell string.** Every invocation is an explicit argv
  list executed with ``shell=False``. There is no interpolation into a string
  that a shell later re-splits, so shell metacharacters in a value are inert
  data. The residual injection risk for a shell-free ``execve`` is *argument*
  injection -- a value that begins with ``-`` and is read by the target tool as
  a flag -- so that is what :func:`_substitute` refuses by default.
- **Credentials never reach argv.** A spec names environment variables; it can
  never interpolate one into an argument. Secrets therefore cannot leak through
  a process table, a command log, or an error message that echoes argv.
- **Everything is bounded.** Binary names, argv widths, value shapes, timeouts
  and file sizes all have explicit ceilings, because a spec file is an execution
  surface and a third-party spec is untrusted input.

The LOCAL provider is deliberately out of scope: it is direct
``user@machine:directory`` connectivity over ssh, not a CLI to wrap.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from mac.fleet_deploy import SshTarget, parse_ssh_target
from mac.mac_paths import mac_home
from mac.models import MACError

__all__ = [
    "PROVIDER_SPEC_SCHEMA",
    "SHIPPED_SPEC_DIR",
    "USER_SPEC_DIR_NAME",
    "SPEC_PATH_ENV_VAR",
    "CRUD_VERBS",
    "ProviderSpecError",
    "ProviderSpecValidationError",
    "ProviderCommandError",
    "ProviderInstanceNotFoundError",
    "ProviderAmbiguousNameError",
    "ProviderCapabilityError",
    "ProviderEndpoint",
    "ProviderInstance",
    "VerbSpec",
    "ParameterSpec",
    "ProviderSpec",
    "SpecProvider",
    "spec_search_path",
    "discover_specs",
    "load_spec",
]


# Versioned schema constant for every spec file and emitted structure.
PROVIDER_SPEC_SCHEMA = "mac.provider_spec.v1"

# Shipped templates live beside the interpreter so a wheel carries them.
SHIPPED_SPEC_DIR = Path(__file__).resolve().parent / "data" / "provider-specs"

# User-authored specs live in the mac home, i.e. they are user CONFIGURATION.
USER_SPEC_DIR_NAME = "provider-specs"

# Colon-separated highest-precedence override, mainly for tests and operators.
SPEC_PATH_ENV_VAR = "MAC_PROVIDER_SPEC_PATH"

# The verb vocabulary an interpreter understands. A spec need not define all of
# them; a caller asking for one a spec omits gets an explicit capability error
# rather than a silent no-op.
CRUD_VERBS: Tuple[str, ...] = (
    "create",
    "list",
    "status",
    "update",
    "delete",
    "stop",
    "start",
    "exec",
)

# --- Bounds. A spec file is an execution surface; every dimension is capped. --
_MAX_SPEC_BYTES = 256 * 1024
_MAX_VERBS = 32
_MAX_ARGV_TOKENS = 64
_MAX_TOKEN_CHARS = 512
_MAX_PARAMETERS = 64
_MAX_VALUE_CHARS = 1024
_MAX_ENV_PASSTHROUGH = 64
_MAX_SPLAT_ITEMS = 64
_MIN_TIMEOUT_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 3600.0
_DEFAULT_TIMEOUT_SECONDS = 120.0

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BINARY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# ``{param}`` or the splat form ``{param...}``, which expands a list parameter
# into one argv item per element.
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})(\.\.\.)?\}")

# Deny-by-default value shape. Specs widen it per parameter with "pattern".
_DEFAULT_VALUE_PATTERN = r"^[A-Za-z0-9._@:/+=,-]{1,256}$"

# Field names that may carry a credential in provider output. Their VALUES are
# never copied into a returned structure; only the field name is recorded.
# Kept in step with the same list in :mod:`mac.hgx_provider`.
_SECRET_FIELD_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
)

_MODEL_FIELDS = ("id", "name", "flavor", "state", "host", "user", "port", "endpoint")


class ProviderSpecError(MACError):
    """Base class for every spec-driven provider failure."""


class ProviderSpecValidationError(ProviderSpecError):
    """A spec file is malformed, unbounded, or otherwise refused at load time.

    Validation is deliberately fail-closed: a spec that cannot be proved to be
    within bounds is not loaded at all, rather than loaded and trusted to be
    used carefully.
    """


class ProviderCommandError(ProviderSpecError):
    """The provider CLI could not be executed or exited non-zero.

    ``stderr`` is captured for the operator's terminal but never copied into an
    observable structure: provider stderr routinely carries credential hints.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str],
        returncode: Optional[int] = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.argv: List[str] = list(argv)
        self.returncode = returncode
        self.stderr = stderr


class ProviderInstanceNotFoundError(ProviderSpecError):
    """No instance matched the requested selector."""


class ProviderAmbiguousNameError(ProviderSpecError):
    """A display name matched more than one instance; refuse to guess.

    Acting on an ambiguous name can operate on the wrong machine, so the caller
    must disambiguate with the immutable provider ID.
    """

    def __init__(self, name: str, instance_ids: Sequence[str]) -> None:
        self.name = name
        self.instance_ids: List[str] = sorted(set(instance_ids))
        super().__init__(
            "instance name %r is ambiguous; it maps to %d instances: %s. "
            "Select by immutable instance id instead."
            % (name, len(self.instance_ids), ", ".join(self.instance_ids))
        )


class ProviderCapabilityError(ProviderSpecError):
    """The spec does not describe the verb or capability the caller asked for."""


def _has_secret_hint(key: str) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in _SECRET_FIELD_KEYS)


# --------------------------------------------------------------------------
# The provider model the interpreter parses output back into.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderEndpoint:
    """A validated SSH endpoint for a spec-driven provider instance."""

    target: SshTarget
    raw: str = ""

    @property
    def user_host(self) -> str:
        return self.target.user_host

    @property
    def port(self) -> Optional[int]:
        return self.target.port

    def observable(self) -> Dict[str, Any]:
        return {"user_host": self.target.user_host, "port": self.target.port}


@dataclass(frozen=True)
class ProviderInstance:
    """A secret-free, structured view of one provider instance.

    The shape intentionally matches :class:`mac.hgx_provider.HgxSession` so the
    existing HGX consumers (elastic capacity, autoscaler) can be re-pointed at a
    spec-driven provider without changing what they read.
    """

    instance_id: str
    provider: str = ""
    name: str = ""
    flavor: str = ""
    state: str = ""
    endpoint: Optional[ProviderEndpoint] = None
    credential_env_var: Optional[str] = None
    credential_present: bool = False
    scrubbed_fields: List[str] = field(default_factory=list)

    def observable(self) -> Dict[str, Any]:
        """Secret-free dict suitable for logs, evidence and observability."""
        return {
            "schema": PROVIDER_SPEC_SCHEMA,
            "provider": self.provider or None,
            "instance_id": self.instance_id,
            "name": self.name or None,
            "flavor": self.flavor or None,
            "state": self.state or None,
            "endpoint": self.endpoint.observable() if self.endpoint else None,
            "credential_env_var": self.credential_env_var,
            "credential_present": self.credential_present,
            "scrubbed_fields": list(self.scrubbed_fields),
        }


# --------------------------------------------------------------------------
# Spec structures.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ParameterSpec:
    """One declared, shape-checked input to a verb's argv template.

    Every placeholder a verb uses must resolve to one of these. An undeclared
    placeholder is a load-time error, so a spec can never smuggle an
    unvalidated value into argv.
    """

    name: str
    required: bool = False
    default: Optional[Any] = None
    pattern: str = _DEFAULT_VALUE_PATTERN
    allow_leading_dash: bool = False
    splat: bool = False

    def compiled(self) -> "re.Pattern[str]":
        return re.compile(self.pattern)


@dataclass(frozen=True)
class VerbSpec:
    """The argv template and output contract for one CRUD verb."""

    name: str
    args: Tuple[str, ...]
    parse_format: str = "json"
    select: Tuple[str, ...] = ()
    fields: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderSpec:
    """A validated, bounded provider description loaded from JSON."""

    name: str
    kind: str
    binary: str
    verbs: Mapping[str, VerbSpec]
    parameters: Mapping[str, ParameterSpec]
    fields: Mapping[str, Tuple[str, ...]]
    description: str = ""
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    env_passthrough: Tuple[str, ...] = ()
    credential_env_var: Optional[str] = None
    source_path: Optional[Path] = None

    def has_verb(self, verb: str) -> bool:
        return verb in self.verbs

    def observable(self) -> Dict[str, Any]:
        """Secret-free description, safe to print in ``mac`` output and evidence."""
        return {
            "schema": PROVIDER_SPEC_SCHEMA,
            "name": self.name,
            "kind": self.kind,
            "description": self.description or None,
            "binary": self.binary,
            "verbs": sorted(self.verbs),
            "parameters": sorted(self.parameters),
            "env_passthrough": list(self.env_passthrough),
            "credential_env_var": self.credential_env_var,
            "timeout_seconds": self.timeout_seconds,
            "source_path": str(self.source_path) if self.source_path else None,
        }

    # -- parsing ---------------------------------------------------------
    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, Any], *, source_path: Optional[Path] = None
    ) -> "ProviderSpec":
        """Validate ``payload`` and return a spec, or raise.

        Everything is checked here, once, at load time. The interpreter below
        may then assume its spec is well-formed and within bounds.
        """
        where = str(source_path) if source_path else "<memory>"
        if not isinstance(payload, Mapping):
            raise ProviderSpecValidationError("%s: provider spec must be a JSON object" % where)

        schema = payload.get("schema")
        if schema != PROVIDER_SPEC_SCHEMA:
            raise ProviderSpecValidationError(
                "%s: schema must be %r, got %r" % (where, PROVIDER_SPEC_SCHEMA, schema)
            )

        name = str(payload.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise ProviderSpecValidationError(
                "%s: name must be 1..64 lowercase letters, digits, '-' or '_'; got %r"
                % (where, name)
            )

        kind = str(payload.get("kind") or "external").strip()
        if kind not in ("external", "internal"):
            raise ProviderSpecValidationError(
                "%s: kind must be 'external' or 'internal', got %r" % (where, kind)
            )

        binary = str(payload.get("binary") or "").strip()
        if not _BINARY_RE.match(binary):
            # A bare command name only: no path separator, no '..', no absolute
            # path. Which binary a name resolves to is then the operator's PATH
            # decision, not something a downloaded spec file can choose.
            raise ProviderSpecValidationError(
                "%s: binary must be a bare command name matching %s; got %r"
                % (where, _BINARY_RE.pattern, binary)
            )

        timeout = _coerce_timeout(payload.get("timeout_seconds"), where)
        env_passthrough = _coerce_env_passthrough(payload.get("env_passthrough"), where)
        credential_env_var = _coerce_credential_env(payload.get("credential_env_var"), where)
        parameters = _coerce_parameters(payload.get("parameters"), where)
        spec_fields = _coerce_fields(payload.get("fields"), where, "fields")
        verbs = _coerce_verbs(payload.get("verbs"), where, parameters)

        if not verbs:
            raise ProviderSpecValidationError("%s: at least one verb is required" % where)

        return cls(
            name=name,
            kind=kind,
            binary=binary,
            verbs=verbs,
            parameters=parameters,
            fields=spec_fields,
            description=str(payload.get("description") or "").strip(),
            timeout_seconds=timeout,
            env_passthrough=env_passthrough,
            credential_env_var=credential_env_var,
            source_path=source_path,
        )

    @classmethod
    def from_file(cls, path: Path) -> "ProviderSpec":
        resolved = Path(path)
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise ProviderSpecValidationError(
                "%s: provider spec is unreadable: %s" % (resolved, exc)
            ) from exc
        if size > _MAX_SPEC_BYTES:
            raise ProviderSpecValidationError(
                "%s: provider spec is %d bytes, over the %d byte ceiling"
                % (resolved, size, _MAX_SPEC_BYTES)
            )
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderSpecValidationError(
                "%s: provider spec is not readable JSON: %s" % (resolved, exc)
            ) from exc
        return cls.from_mapping(payload, source_path=resolved)


# --------------------------------------------------------------------------
# Validation helpers.
# --------------------------------------------------------------------------
def _coerce_timeout(value: Any, where: str) -> float:
    if value is None:
        return _DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderSpecValidationError("%s: timeout_seconds must be a number" % where)
    if not _MIN_TIMEOUT_SECONDS <= float(value) <= _MAX_TIMEOUT_SECONDS:
        raise ProviderSpecValidationError(
            "%s: timeout_seconds must be within %g..%g"
            % (where, _MIN_TIMEOUT_SECONDS, _MAX_TIMEOUT_SECONDS)
        )
    return float(value)


def _coerce_env_passthrough(value: Any, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProviderSpecValidationError("%s: env_passthrough must be a list of names" % where)
    if len(value) > _MAX_ENV_PASSTHROUGH:
        raise ProviderSpecValidationError(
            "%s: env_passthrough has %d entries, over the %d ceiling"
            % (where, len(value), _MAX_ENV_PASSTHROUGH)
        )
    names: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if not _ENV_NAME_RE.match(text):
            raise ProviderSpecValidationError(
                "%s: env_passthrough entry %r is not a valid environment variable name"
                % (where, item)
            )
        names.append(text)
    return tuple(dict.fromkeys(names))


def _coerce_credential_env(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not _ENV_NAME_RE.match(text):
        raise ProviderSpecValidationError(
            "%s: credential_env_var %r is not a valid environment variable name" % (where, value)
        )
    return text


def _coerce_parameters(value: Any, where: str) -> Mapping[str, ParameterSpec]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProviderSpecValidationError("%s: parameters must be an object" % where)
    if len(value) > _MAX_PARAMETERS:
        raise ProviderSpecValidationError(
            "%s: %d parameters declared, over the %d ceiling"
            % (where, len(value), _MAX_PARAMETERS)
        )
    out: Dict[str, ParameterSpec] = {}
    for raw_name, raw in value.items():
        name = str(raw_name)
        if not _PARAM_NAME_RE.match(name):
            raise ProviderSpecValidationError(
                "%s: parameter name %r must match %s" % (where, name, _PARAM_NAME_RE.pattern)
            )
        if not isinstance(raw, Mapping):
            raise ProviderSpecValidationError(
                "%s: parameter %r must be an object" % (where, name)
            )
        pattern = str(raw.get("pattern") or _DEFAULT_VALUE_PATTERN)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ProviderSpecValidationError(
                "%s: parameter %r has an invalid pattern: %s" % (where, name, exc)
            ) from exc
        if _has_secret_hint(name):
            # Secrets travel in the child environment, by name. A spec that
            # wants one in argv is refused rather than redacted after the fact.
            raise ProviderSpecValidationError(
                "%s: parameter %r looks like a credential; credentials are passed "
                "via env_passthrough/credential_env_var and never interpolated into argv"
                % (where, name)
            )
        out[name] = ParameterSpec(
            name=name,
            required=bool(raw.get("required", False)),
            default=raw.get("default"),
            pattern=pattern,
            allow_leading_dash=bool(raw.get("allow_leading_dash", False)),
            splat=bool(raw.get("splat", False)),
        )
    return out


def _coerce_fields(value: Any, where: str, label: str) -> Mapping[str, Tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProviderSpecValidationError("%s: %s must be an object" % (where, label))
    out: Dict[str, Tuple[str, ...]] = {}
    for raw_key, raw in value.items():
        key = str(raw_key)
        if key not in _MODEL_FIELDS:
            raise ProviderSpecValidationError(
                "%s: %s key %r is not one of %s" % (where, label, key, ", ".join(_MODEL_FIELDS))
            )
        if isinstance(raw, str):
            candidates: Sequence[Any] = [raw]
        elif isinstance(raw, list):
            candidates = raw
        else:
            raise ProviderSpecValidationError(
                "%s: %s[%r] must be a source key or a list of source keys" % (where, label, key)
            )
        names = [str(item).strip() for item in candidates if str(item).strip()]
        if not names:
            raise ProviderSpecValidationError(
                "%s: %s[%r] names no source key" % (where, label, key)
            )
        out[key] = tuple(names)
    return out


def _coerce_verbs(
    value: Any, where: str, parameters: Mapping[str, ParameterSpec]
) -> Mapping[str, VerbSpec]:
    if not isinstance(value, Mapping):
        raise ProviderSpecValidationError("%s: verbs must be an object" % where)
    if len(value) > _MAX_VERBS:
        raise ProviderSpecValidationError(
            "%s: %d verbs declared, over the %d ceiling" % (where, len(value), _MAX_VERBS)
        )
    out: Dict[str, VerbSpec] = {}
    for raw_name, raw in value.items():
        verb = str(raw_name)
        if verb not in CRUD_VERBS:
            raise ProviderSpecValidationError(
                "%s: verb %r is not one of %s" % (where, verb, ", ".join(CRUD_VERBS))
            )
        if not isinstance(raw, Mapping):
            raise ProviderSpecValidationError("%s: verb %r must be an object" % (where, verb))
        args = raw.get("args")
        if not isinstance(args, list) or not args:
            raise ProviderSpecValidationError(
                "%s: verb %r needs a non-empty args list" % (where, verb)
            )
        if len(args) > _MAX_ARGV_TOKENS:
            raise ProviderSpecValidationError(
                "%s: verb %r has %d argv tokens, over the %d ceiling"
                % (where, verb, len(args), _MAX_ARGV_TOKENS)
            )
        tokens: List[str] = []
        for item in args:
            if not isinstance(item, str) or not item:
                raise ProviderSpecValidationError(
                    "%s: verb %r argv items must be non-empty strings" % (where, verb)
                )
            if len(item) > _MAX_TOKEN_CHARS:
                raise ProviderSpecValidationError(
                    "%s: verb %r has an argv token over %d characters"
                    % (where, verb, _MAX_TOKEN_CHARS)
                )
            for param_name, splat in _PLACEHOLDER_RE.findall(item):
                declared = parameters.get(param_name)
                if declared is None:
                    raise ProviderSpecValidationError(
                        "%s: verb %r references undeclared parameter %r"
                        % (where, verb, param_name)
                    )
                if bool(splat) != declared.splat:
                    raise ProviderSpecValidationError(
                        "%s: verb %r uses %s for parameter %r but its splat flag is %s"
                        % (
                            where,
                            verb,
                            "{%s...}" % param_name if splat else "{%s}" % param_name,
                            param_name,
                            declared.splat,
                        )
                    )
                if declared.splat and item != "{%s...}" % param_name:
                    raise ProviderSpecValidationError(
                        "%s: verb %r splat %r must be the whole argv token"
                        % (where, verb, item)
                    )
            tokens.append(item)

        parse = raw.get("parse") or {}
        if not isinstance(parse, Mapping):
            raise ProviderSpecValidationError("%s: verb %r parse must be an object" % (where, verb))
        parse_format = str(parse.get("format") or "json")
        if parse_format not in ("json", "none"):
            raise ProviderSpecValidationError(
                "%s: verb %r parse.format must be 'json' or 'none', got %r"
                % (where, verb, parse_format)
            )
        select_raw = parse.get("select")
        if select_raw is None:
            select: Tuple[str, ...] = ()
        elif isinstance(select_raw, str):
            select = tuple(part for part in select_raw.split(".") if part)
        elif isinstance(select_raw, list):
            select = tuple(str(part) for part in select_raw if str(part))
        else:
            raise ProviderSpecValidationError(
                "%s: verb %r parse.select must be a dotted string or list" % (where, verb)
            )
        verb_fields = _coerce_fields(parse.get("fields"), where, "verbs.%s.parse.fields" % verb)
        out[verb] = VerbSpec(
            name=verb,
            args=tuple(tokens),
            parse_format=parse_format,
            select=select,
            fields=verb_fields,
        )
    return out


# --------------------------------------------------------------------------
# Discovery and precedence.
# --------------------------------------------------------------------------
def spec_search_path(*, env: Optional[Mapping[str, str]] = None) -> List[Path]:
    """Ordered spec directories, nearest (highest precedence) first.

    1. ``$MAC_PROVIDER_SPEC_PATH`` -- colon-separated, for operators and tests.
    2. ``<mac home>/provider-specs`` -- the user's own specs. This is where a
       user's providers live, because a provider is configuration.
    3. the shipped templates inside the installed package -- examples to copy,
       and the profile the NVIDIA/hgx integration is expressed in.

    A name found in an earlier directory shadows the same name later, so a user
    can override a shipped template by dropping a file with the same name into
    their own directory without editing anything mac ships.
    """
    environ = os.environ if env is None else env
    out: List[Path] = []
    raw = (environ.get(SPEC_PATH_ENV_VAR) or "").strip()
    if raw:
        out.extend(Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip())
    out.append(mac_home() / USER_SPEC_DIR_NAME)
    out.append(SHIPPED_SPEC_DIR)
    # Preserve order while removing duplicates, so a directory named twice does
    # not report itself as shadowing itself.
    seen: set[str] = set()
    unique: List[Path] = []
    for path in out:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def discover_specs(
    *, env: Optional[Mapping[str, str]] = None, strict: bool = False
) -> Dict[str, ProviderSpec]:
    """Load every spec on the search path, nearest-wins, keyed by provider name.

    With ``strict=False`` (the default) an unparseable file in one directory
    does not stop the rest from loading -- one bad third-party spec must not
    make every provider disappear. ``strict=True`` re-raises, which is what
    ``mac``'s validation surface and the tests want.
    """
    found: Dict[str, ProviderSpec] = {}
    for directory in spec_search_path(env=env):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                spec = ProviderSpec.from_file(path)
            except ProviderSpecValidationError:
                if strict:
                    raise
                continue
            # A spec whose declared name disagrees with its filename is a
            # precedence trap: the operator would shadow a name they cannot see.
            if spec.name != path.stem:
                if strict:
                    raise ProviderSpecValidationError(
                        "%s: spec name %r must match its filename stem %r"
                        % (path, spec.name, path.stem)
                    )
                continue
            found.setdefault(spec.name, spec)
    return found


def load_spec(name: str, *, env: Optional[Mapping[str, str]] = None) -> ProviderSpec:
    """Return the highest-precedence spec named ``name``."""
    target = (name or "").strip()
    if not target:
        raise ProviderSpecValidationError("a provider name is required")
    specs = discover_specs(env=env)
    spec = specs.get(target)
    if spec is None:
        raise ProviderSpecValidationError(
            "no provider spec named %r on the search path: %s"
            % (target, ", ".join(str(p) for p in spec_search_path(env=env)))
        )
    return spec


# --------------------------------------------------------------------------
# The interpreter.
# --------------------------------------------------------------------------
def _substitute(spec: ProviderSpec, token: str, values: Mapping[str, Any]) -> List[str]:
    """Expand one argv template token into zero or more concrete argv items."""
    splat_match = re.fullmatch(r"\{([a-z][a-z0-9_]{0,31})\.\.\.\}", token)
    if splat_match:
        param = spec.parameters[splat_match.group(1)]
        raw = values.get(param.name, param.default)
        if raw is None:
            raw = []
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ProviderSpecValidationError(
                "parameter %r expands an argv list and needs a sequence of strings" % param.name
            )
        items = list(raw)
        if len(items) > _MAX_SPLAT_ITEMS:
            raise ProviderSpecValidationError(
                "parameter %r expanded to %d items, over the %d ceiling"
                % (param.name, len(items), _MAX_SPLAT_ITEMS)
            )
        return [_checked_value(param, item) for item in items]

    def replace(match: "re.Match[str]") -> str:
        param = spec.parameters[match.group(1)]
        raw = values.get(param.name, param.default)
        if raw is None:
            if param.required:
                raise ProviderSpecValidationError("parameter %r is required" % param.name)
            raise ProviderSpecValidationError(
                "parameter %r has no value and no default" % param.name
            )
        return _checked_value(param, raw)

    return [_PLACEHOLDER_RE.sub(replace, token)]


def _checked_value(param: ParameterSpec, raw: Any) -> str:
    """Return ``raw`` as a string, or refuse it."""
    if isinstance(raw, bool) or raw is None:
        raise ProviderSpecValidationError(
            "parameter %r must be a string or number, got %r" % (param.name, raw)
        )
    text = raw if isinstance(raw, str) else str(raw)
    if len(text) > _MAX_VALUE_CHARS:
        raise ProviderSpecValidationError(
            "parameter %r value is %d characters, over the %d ceiling"
            % (param.name, len(text), _MAX_VALUE_CHARS)
        )
    if "\x00" in text:
        raise ProviderSpecValidationError("parameter %r value contains a NUL byte" % param.name)
    if not param.allow_leading_dash and text.startswith("-"):
        # argv is executed without a shell, so shell metacharacters are inert.
        # The live risk is a value the target tool re-reads as a FLAG.
        raise ProviderSpecValidationError(
            "parameter %r value %r starts with '-'; set allow_leading_dash on the "
            "parameter if the provider really expects a flag here" % (param.name, text)
        )
    if not param.compiled().fullmatch(text):
        raise ProviderSpecValidationError(
            "parameter %r value %r does not match its declared pattern %s"
            % (param.name, text, param.pattern)
        )
    return text


class SpecProvider:
    """Executes a :class:`ProviderSpec` against a real provider CLI.

    The instance holds no credentials. It builds argv from the spec, runs it
    with ``shell=False``, and parses stdout back into
    :class:`ProviderInstance` values that are secret-free by construction.
    """

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.spec = spec
        self._environ = dict(os.environ if env is None else env)
        self._timeout = float(timeout) if timeout is not None else spec.timeout_seconds

    # -- plumbing --------------------------------------------------------
    def _child_env(self) -> Dict[str, str]:
        """The child environment: PATH plus exactly the declared passthroughs.

        A spec cannot read a variable it did not name, so adding a provider
        does not silently widen what the child process can see.
        """
        env: Dict[str, str] = {}
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"):
            value = self._environ.get(key)
            if value is not None:
                env[key] = value
        names = list(self.spec.env_passthrough)
        if self.spec.credential_env_var:
            names.append(self.spec.credential_env_var)
        for name in names:
            value = self._environ.get(name)
            if value is not None:
                env[name] = value
        return env

    def build_argv(self, verb: str, values: Optional[Mapping[str, Any]] = None) -> List[str]:
        """Return the concrete argv for ``verb``, without running anything.

        Exposed deliberately: an operator reviewing a third-party spec should be
        able to see exactly what it would execute before it executes.
        """
        template = self.spec.verbs.get(verb)
        if template is None:
            raise ProviderCapabilityError(
                "provider %r does not describe verb %r (it has: %s)"
                % (self.spec.name, verb, ", ".join(sorted(self.spec.verbs)))
            )
        supplied = dict(values or {})
        # Requiredness is per-verb, judged by what this verb's template actually
        # references: ``image_id`` is required to create an instance and
        # meaningless when deleting one.
        for token in template.args:
            for param_name, _splat in _PLACEHOLDER_RE.findall(token):
                param = self.spec.parameters[param_name]
                if param.required and supplied.get(param.name, param.default) is None:
                    raise ProviderSpecValidationError(
                        "provider %r verb %r requires parameter %r"
                        % (self.spec.name, verb, param.name)
                    )
        argv: List[str] = [self.spec.binary]
        for token in template.args:
            argv.extend(_substitute(self.spec, token, supplied))
        return argv

    def run(self, verb: str, values: Optional[Mapping[str, Any]] = None) -> str:
        """Execute ``verb`` and return stdout."""
        argv = self.build_argv(verb, values)
        env = self._child_env()
        resolved = shutil.which(argv[0], path=env.get("PATH"))
        if resolved is None:
            raise ProviderCommandError(
                "provider %r binary %r not found on PATH" % (self.spec.name, argv[0]),
                argv=argv,
            )
        try:
            completed = subprocess.run(
                [resolved, *argv[1:]],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=env,
            )
        except OSError as exc:
            raise ProviderCommandError(
                "provider %r %s could not be executed: %s" % (self.spec.name, verb, exc),
                argv=argv,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderCommandError(
                "provider %r %s timed out after %ss" % (self.spec.name, verb, self._timeout),
                argv=argv,
            ) from exc
        if completed.returncode != 0:
            raise ProviderCommandError(
                "provider %r %s failed (exit %d)"
                % (self.spec.name, verb, completed.returncode),
                argv=argv,
                returncode=completed.returncode,
                stderr=completed.stderr or "",
            )
        return completed.stdout or ""

    # -- output -> model -------------------------------------------------
    def _field_map(self, verb: str) -> Mapping[str, Tuple[str, ...]]:
        merged: Dict[str, Tuple[str, ...]] = dict(self.spec.fields)
        template = self.spec.verbs.get(verb)
        if template is not None:
            merged.update(template.fields)
        return merged

    def _rows(self, verb: str, stdout: str) -> List[Mapping[str, Any]]:
        template = self.spec.verbs[verb]
        if template.parse_format == "none":
            return []
        try:
            payload: Any = json.loads(stdout.strip() or "null")
        except ValueError:
            return []
        for key in template.select:
            if isinstance(payload, Mapping):
                payload = payload.get(key)
            elif isinstance(payload, list) and key.isdigit() and int(key) < len(payload):
                payload = payload[int(key)]
            else:
                return []
        if payload is None:
            return []
        if isinstance(payload, Mapping):
            return [payload]
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        return []

    def _instance(self, verb: str, row: Mapping[str, Any]) -> ProviderInstance:
        mapping = self._field_map(verb)

        def pick(model_field: str) -> str:
            for source in mapping.get(model_field, ()):  # declared candidates, in order
                value = row.get(source)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, int) and not isinstance(value, bool):
                    return str(value)
            return ""

        instance_id = pick("id")
        if not instance_id:
            raise ProviderSpecValidationError(
                "provider %r %s output has no immutable id; the spec must map "
                "fields.id to a source key" % (self.spec.name, verb)
            )
        scrubbed = sorted(str(key) for key in row if _has_secret_hint(key))
        endpoint = self._endpoint(row, mapping)
        return ProviderInstance(
            instance_id=instance_id,
            provider=self.spec.name,
            name=pick("name"),
            flavor=pick("flavor"),
            state=pick("state"),
            endpoint=endpoint,
            credential_env_var=self.spec.credential_env_var,
            credential_present=bool(scrubbed) or self.spec.credential_env_var is not None,
            scrubbed_fields=scrubbed,
        )

    def _endpoint(
        self, row: Mapping[str, Any], mapping: Mapping[str, Tuple[str, ...]]
    ) -> Optional[ProviderEndpoint]:
        def pick(model_field: str) -> str:
            for source in mapping.get(model_field, ()):
                value = row.get(source)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, int) and not isinstance(value, bool):
                    return str(value)
            return ""

        port_text = pick("port")
        port = int(port_text) if port_text.isdigit() else None
        for candidate in (pick("endpoint"), _compose(pick("user"), pick("host"))):
            if not candidate:
                continue
            try:
                target = parse_ssh_target(candidate, port=port)
            except ValueError:
                continue
            return ProviderEndpoint(target=target, raw=candidate)
        return None

    # -- CRUD verbs ------------------------------------------------------
    def create(self, **values: Any) -> ProviderInstance:
        rows = self._rows("create", self.run("create", values))
        if not rows:
            raise ProviderSpecError(
                "provider %r create returned no parseable instance" % self.spec.name
            )
        return self._instance("create", rows[0])

    def list(self, **values: Any) -> List[ProviderInstance]:
        rows = self._rows("list", self.run("list", values))
        return [self._instance("list", row) for row in rows]

    def status(self, instance_id: str, **values: Any) -> ProviderInstance:
        target = _require_instance_id(instance_id)
        rows = self._rows("status", self.run("status", {**values, "instance_id": target}))
        if not rows:
            raise ProviderInstanceNotFoundError(
                "provider %r has no instance %r" % (self.spec.name, target)
            )
        instance = self._instance("status", rows[0])
        if instance.instance_id != target:
            raise ProviderInstanceNotFoundError(
                "provider %r status returned id %r for requested %r"
                % (self.spec.name, instance.instance_id, target)
            )
        return instance

    def update(self, instance_id: str, **values: Any) -> str:
        target = _require_instance_id(instance_id)
        self.run("update", {**values, "instance_id": target})
        return target

    def delete(self, instance_id: str, **values: Any) -> str:
        target = _require_instance_id(instance_id)
        self.run("delete", {**values, "instance_id": target})
        return target

    def stop(self, instance_id: str, **values: Any) -> str:
        target = _require_instance_id(instance_id)
        self.run("stop", {**values, "instance_id": target})
        return target

    def start(self, instance_id: str, **values: Any) -> str:
        target = _require_instance_id(instance_id)
        self.run("start", {**values, "instance_id": target})
        return target

    def exec(self, instance_id: str, command: Sequence[str], **values: Any) -> str:
        """Run a non-interactive command on an instance through the spec's transport."""
        target = _require_instance_id(instance_id)
        if isinstance(command, (str, bytes)) or not command:
            raise ProviderSpecValidationError("exec requires a non-empty argv sequence")
        return self.run("exec", {**values, "instance_id": target, "command": list(command)})

    def attest(self, instance_id: str, **values: Any) -> str:
        """Prove real remote execution and return the instance ID.

        The readiness contract that ``mac.hgx_provider.attest_ssh`` implements
        in Python is expressed here in spec vocabulary: a provider that
        describes ``exec`` can be attested, and one that cannot is refused
        rather than assumed ready. A zero exit is never treated as proof --
        the remote side must echo an unpredictable nonce back.
        """
        target = _require_instance_id(instance_id)
        if not self.spec.has_verb("exec"):
            raise ProviderCapabilityError(
                "provider %r cannot be attested: its spec describes no 'exec' verb, "
                "so there is no transport to prove reachability over" % self.spec.name
            )
        nonce = "mac-provider-attest-" + secrets.token_hex(16)
        stdout = self.exec(target, ["printf", nonce], **values)
        if nonce not in stdout.splitlines():
            raise ProviderSpecError(
                "provider %r attestation nonce was not returned for instance %r"
                % (self.spec.name, target)
            )
        return target

    # -- name -> immutable id -------------------------------------------
    def resolve_instance_id(self, name: str, **values: Any) -> str:
        """Resolve a display ``name`` to exactly one immutable instance ID."""
        target = (name or "").strip()
        if not target:
            raise ProviderInstanceNotFoundError("a non-empty instance name is required")
        instances = self.list(**values)
        matches = sorted({item.instance_id for item in instances if item.name == target})
        if not matches:
            ids = [item.instance_id for item in instances if item.instance_id == target]
            if len(set(ids)) == 1:
                return ids[0]
            raise ProviderInstanceNotFoundError(
                "provider %r has no instance named %r" % (self.spec.name, target)
            )
        if len(matches) > 1:
            raise ProviderAmbiguousNameError(target, matches)
        return matches[0]


def _require_instance_id(instance_id: str) -> str:
    target = (instance_id or "").strip()
    if not target:
        raise ProviderInstanceNotFoundError("an immutable instance id is required")
    return target


def _compose(user: str, host: str) -> str:
    if not host:
        return ""
    return "%s@%s" % (user, host) if user else host
