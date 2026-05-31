"""Owned, in-process resolution of the per-agent Hermes runtime provider.

This module is the keystone of ADR 0001 (``docs/adr/0001-unify-hermes-runtime-into-mac.md``):
it replaces the *intent* of the runtime string-surgery shim in
``hermes_startup._apply_gateway_runtime_shim`` with first-class, tested mac
code that produces a legible rationale.

Why this exists
---------------
Today the per-agent model/provider override — the mechanism that keeps the
fleet off a single model family (anti-"agent monoculture") — is injected by
reading upstream ``gateway/run.py`` as text and doing exact-string ``.replace()``
on needles. When upstream drifts, the needle silently misses, the override
never applies, and **nothing in the agent runtime can explain why** the agent
ended up on the wrong provider. That is the "inexplicable provider behavior"
dark spot.

This module computes the override decision *independently of whether any
source surgery succeeded*, from the same environment inputs the shim reads, and
returns:

- ``decision.override_kwargs(...)`` — the override fields mac forces on top of
  Hermes' ``resolve_runtime_provider`` result (the contract is
  ``{provider, base_url, api_key, source}`` layered onto Hermes' dict). Once the
  Hermes runtime is vendored in-process, the gateway calls this directly instead
  of being string-patched.
- ``decision.observable()`` — a **secret-free** dict (the api key is never
  included; only the env var name that supplied it and a presence flag) suitable
  for the ``/startup/hermes`` report and for ``ObservabilityService``.
- ``decision.rationale`` — human-readable lines explaining which env var won
  each field and whether an override is active at all.

It is intentionally dependency-free (stdlib only) and does **not** import
Hermes, so it is unit-testable in mac's own venv before the runtime is vendored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "ProviderDecision",
    "resolve_agent_provider",
    "record_provider_decision",
    "MODEL_ENV_KEYS",
    "PROVIDER_ENV_KEYS",
    "BASE_URL_ENV_KEYS",
    "API_KEY_ENV_KEYS",
]

# Precedence mirrors hermes_startup._apply_gateway_runtime_shim /
# _configured_gateway_*. Keep these lists as the single source of truth so the
# owned resolver and the (soon-to-be-deleted) shim cannot drift apart.
MODEL_ENV_KEYS: Tuple[str, ...] = (
    "MAC_HERMES_GATEWAY_MODEL",
    "ACC_HERMES_GATEWAY_MODEL",
    "HERMES_INFERENCE_MODEL",
    "ACC_LLM_MODEL",
)
PROVIDER_ENV_KEYS: Tuple[str, ...] = (
    "MAC_HERMES_GATEWAY_PROVIDER",
    "ACC_HERMES_GATEWAY_PROVIDER",
    "HERMES_INFERENCE_PROVIDER",
)
# TOKENHUB_URL is special: it is a hub root, not a chat base_url, so it gets a
# "/v1" suffix appended (matching the shim).
BASE_URL_ENV_KEYS: Tuple[str, ...] = (
    "MAC_HERMES_GATEWAY_BASE_URL",
    "ACC_HERMES_GATEWAY_BASE_URL",
    "TOKENHUB_URL",
    "OPENAI_BASE_URL",
)
API_KEY_ENV_KEYS: Tuple[str, ...] = (
    "MAC_HERMES_GATEWAY_API_KEY",
    "ACC_HERMES_GATEWAY_API_KEY",
    "TOKENHUB_API_KEY",
    "TOKENHUB_AGENT_KEY",
    "OPENAI_API_KEY",
)

_TOKENHUB_ROOT_KEY = "TOKENHUB_URL"


def _first_set(env: Mapping[str, str], keys: Tuple[str, ...]) -> Tuple[str, Optional[str]]:
    """Return (value, winning_key) for the first env key with a non-blank value."""
    for key in keys:
        raw = env.get(key)
        if raw is not None and raw.strip():
            return raw.strip(), key
    return "", None


@dataclass(frozen=True)
class ProviderDecision:
    """The per-agent provider override mac decides, plus why.

    The api key value itself is held privately and never appears in
    ``observable()`` or ``rationale`` — only ``api_key_present`` and the env var
    name that supplied it.
    """

    override_active: bool
    requested_provider: str
    model: str
    base_url: str
    source: str
    rationale: List[str] = field(default_factory=list)
    # secret-free provenance: field name -> env var that supplied it
    won_by: Dict[str, str] = field(default_factory=dict)
    api_key_present: bool = False
    _api_key: str = field(default="", repr=False)

    def override_kwargs(self, base_kwargs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Layer mac's forced override fields onto Hermes' resolve result.

        ``base_kwargs`` is the dict returned by Hermes'
        ``resolve_runtime_provider``; pass ``None`` (or omit) when calling
        standalone, in which case the override fields stand on their own. This
        is the exact contract the deleted string-surgery shim produced: force
        ``api_key``/``source`` when a key is configured, and force ``base_url``
        when one is configured.
        """
        kwargs: Dict[str, Any] = dict(base_kwargs or {})
        if not self.override_active:
            return kwargs
        kwargs.setdefault("provider", self.requested_provider)
        kwargs.setdefault("requested_provider", self.requested_provider)
        if self.base_url:
            kwargs["base_url"] = self.base_url.rstrip("/")
        if self._api_key:
            kwargs["api_key"] = self._api_key
            kwargs["source"] = "mac-gateway-explicit"
        elif "source" not in kwargs:
            kwargs["source"] = self.source
        return kwargs

    def observable(self) -> Dict[str, Any]:
        """Secret-free view for the startup report and observability."""
        return {
            "schema": "mac.agent_provider.decision.v1",
            "override_active": self.override_active,
            "requested_provider": self.requested_provider or None,
            "model": self.model or None,
            "base_url": self.base_url or None,
            "source": self.source,
            "api_key_present": self.api_key_present,
            "won_by": dict(self.won_by),
            "rationale": list(self.rationale),
        }


def resolve_agent_provider(env: Optional[Mapping[str, str]] = None) -> ProviderDecision:
    """Resolve the per-agent provider override from the environment.

    Mirrors the env precedence the runtime shim applied, but as owned, tested
    code that always produces a legible decision — even when (under the old
    deployment) the string surgery would have silently failed.
    """
    if env is None:
        env = os.environ

    model, model_key = _first_set(env, MODEL_ENV_KEYS)
    provider, provider_key = _first_set(env, PROVIDER_ENV_KEYS)
    base_url_raw, base_url_key = _first_set(env, BASE_URL_ENV_KEYS)
    api_key, api_key_key = _first_set(env, API_KEY_ENV_KEYS)

    base_url = base_url_raw
    if base_url and base_url_key == _TOKENHUB_ROOT_KEY:
        base_url = base_url_raw.rstrip("/") + "/v1"

    # The shim triggers an override on model OR provider OR base_url. An api key
    # alone does not (it only augments an override that some other field opened).
    override_active = bool(model or provider or base_url)
    requested_provider = provider or ("custom" if override_active else "")
    source = "mac-gateway-explicit" if api_key else ("mac-gateway" if override_active else "hermes-default")

    won_by: Dict[str, str] = {}
    rationale: List[str] = []
    if model_key:
        won_by["model"] = model_key
        rationale.append("model %r from %s" % (model, model_key))
    if provider_key:
        won_by["provider"] = provider_key
        rationale.append("provider %r from %s" % (provider, provider_key))
    if base_url_key:
        won_by["base_url"] = base_url_key
        if base_url_key == _TOKENHUB_ROOT_KEY:
            rationale.append("base_url %r derived from %s (+/v1)" % (base_url, base_url_key))
        else:
            rationale.append("base_url %r from %s" % (base_url, base_url_key))
    if api_key_key:
        won_by["api_key"] = api_key_key
        rationale.append("api_key present from %s (value redacted)" % api_key_key)

    if not override_active:
        rationale.append(
            "no mac override configured; Hermes default provider resolution applies"
        )
    elif not provider_key:
        rationale.append("provider defaulted to 'custom' because an override field was set")

    return ProviderDecision(
        override_active=override_active,
        requested_provider=requested_provider,
        model=model,
        base_url=base_url,
        source=source,
        rationale=rationale,
        won_by=won_by,
        api_key_present=bool(api_key),
        _api_key=api_key,
    )


def record_provider_decision(
    observability: Any,
    decision: ProviderDecision,
    *,
    agent_id: Optional[str] = None,
    hermes_instance_id: Optional[str] = None,
) -> Any:
    """Emit the (secret-free) provider decision through ObservabilityService.

    Makes the anti-monoculture model/provider override an attributable event in
    the ``hermes`` layer, so an operator or the agent itself can answer "why am I
    on this provider?" instead of facing a silent shim miss. Returns the
    recorded ``ObservabilityEvent`` (or ``None`` if ``observability`` is falsy).
    """
    if not observability:
        return None
    detail = decision.observable()
    if hermes_instance_id:
        detail["hermes_instance_id"] = hermes_instance_id
    return observability.record_observation(
        kind="log",
        name="hermes.provider.resolved",
        layer="hermes",
        source="mac",
        level="info" if decision.override_active else "debug",
        subject_type="agent" if agent_id else None,
        subject_id=agent_id,
        detail=detail,
    )
