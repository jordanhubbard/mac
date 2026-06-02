"""Single source of truth for the fleet's upstream LLM/generation providers and
the env vars that carry their credentials.

Before this module the provider set was enumerated in four drifting places —
setup-fleet's `_KNOWN_PROVIDERS`, the deploy escrow's `PROVIDER_KEY_ENV`, the
deploy `scrub_spoke_provider_secrets` regex, and the `deploy_host` key-blanking.
Adding a provider meant editing each, and a miss failed silently (a key not
scrubbed off a spoke, or not escrowed). They now all derive from here.

Consumers:
- scripts/setup-fleet.py imports ROUTER_PROVIDERS / router_secret_name to build
  MAC_ROUTER_PROVIDERS.
- deploy/deploy-mac-fleet.sh's escrow heredoc imports provider_key_env() /
  router_secret_name.
- deploy/deploy-mac-fleet.sh's scrub reads `python -m mac.providers scrub-regex`
  for the set of upstream secret env vars a spoke must not hold.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, NamedTuple


class Provider(NamedTuple):
    id: str
    key_env: str          # API-key env var, e.g. NVIDIA_API_KEY
    base_env: str         # base-url env var, e.g. NVIDIA_BASE_URL
    default_base_url: str  # OpenAI-compatible base URL the router fronts


# OpenAI-compatible chat/embedding providers the in-mac router can front.
# nvidia is preferred (priority 0 = first).
ROUTER_PROVIDERS: List[Provider] = [
    Provider("nvidia", "NVIDIA_API_KEY", "NVIDIA_BASE_URL", "https://inference-api.nvidia.com/v1"),
    Provider("openai", "OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
    Provider("anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    Provider("perplexity", "PERPLEXITY_API_KEY", "PERPLEXITY_BASE_URL", "https://api.perplexity.ai"),
]

# Additional upstream provider env vars managed with chat provider keys when the
# in-mac router owns routing. These are not first-class chat providers in
# ROUTER_PROVIDERS, but they are still upstream/provider credentials or alternate
# base-url spellings that must not survive stale local env state.
EXTRA_UPSTREAM_PROVIDER_ENV: List[str] = [
    "NVIDIA_API_BASE",
    "NVIDIA_IMAGE_BASE_URL",
    "PERPLEXITY_API_BASE",
    "FAL_KEY",
    "VLLM_API_KEY",
    "HAIMAKER_API_KEY",
    "LLM_KEY",
    "LLM_URL",
]

# External shared-service secrets scrubbed from spoke gateway envs. These are
# intentionally not cleared from mac.env's inproc routing state, because shared
# service setup may write benign local defaults such as FIRECRAWL_API_KEY=none.
EXTRA_SPOKE_SCRUB_SECRET_ENV: List[str] = [
    "QDRANT_API_KEY",
    "FIRECRAWL_API_KEY",
]


def router_secret_name(provider_id: str) -> str:
    """Vault secret name the (hub-only) router resolves a provider's key from."""
    return "%s-upstream" % provider_id


def provider_key_env() -> Dict[str, str]:
    """provider id -> source API-key env var (used by the deploy escrow step)."""
    return {p.id: p.key_env for p in ROUTER_PROVIDERS}


def _dedupe(names: Iterable[str]) -> List[str]:
    """Order-preserving de-duplication for env var tables."""
    seen = set()
    out: List[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def upstream_provider_env_vars() -> List[str]:
    """Provider/upstream env vars cleared when inproc routing owns credentials."""
    names: List[str] = []
    for provider in ROUTER_PROVIDERS:
        names.append(provider.key_env)
        names.append(provider.base_env)
    names.extend(EXTRA_UPSTREAM_PROVIDER_ENV)
    return _dedupe(names)


def spoke_scrub_env_vars() -> List[str]:
    """Every upstream secret env var a spoke's gateway env must be scrubbed of.

    A spoke's OPENAI_API_KEY is the hub token, re-supplied by mac.env, so
    stripping the gateway-env copy is safe.
    """
    return _dedupe([*upstream_provider_env_vars(), *EXTRA_SPOKE_SCRUB_SECRET_ENV])


if __name__ == "__main__":
    # Thin CLI so deploy bash can derive lists without re-hardcoding them.
    import sys

    what = sys.argv[1] if len(sys.argv) > 1 else "scrub-regex"
    if what == "scrub-regex":
        print("|".join(spoke_scrub_env_vars()))
    elif what == "scrub-vars":
        print(" ".join(spoke_scrub_env_vars()))
    elif what == "provider-key-envs":
        print(" ".join(p.key_env for p in ROUTER_PROVIDERS))
    else:
        sys.stderr.write("unknown: %s (scrub-regex|scrub-vars|provider-key-envs)\n" % what)
        raise SystemExit(2)
