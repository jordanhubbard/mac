"""Sync the Hermes runtime's chat config from the deploy-authoritative mac.env.

The vendored Hermes runtime resolves its chat provider from ``~/.hermes/.env`` +
``~/.hermes/config.yaml`` (and an ``auth.json`` credential pool) — NOT from
``mac.env``. After the TokenHub retirement those retained stale TokenHub state —
the ``:8090`` endpoint, ``model.provider: tokenhub``, and ``custom:*`` pool
entries — so the agent startup self-test (and task execution, same
``hermes_cli chat`` path) dialed the dead endpoint or sent a rejected bearer
(HTTP 403 "unknown bearer token"). ``write-mac-env`` fixes ``mac.env`` but never
touched these, so every redeploy left the agents degraded.

This module makes the runtime config mirror ``mac.env``'s in-mac-router endpoint
+ token:

1. ``~/.hermes/.env``: chat endpoint/key vars copied from ``mac.env``.
2. ``~/.hermes/config.yaml``: ``model.provider``/``base_url`` set to the router,
   plus a ``providers.custom`` entry carrying the bearer in the schema's
   ``api_key`` field (a bare ``key:`` is silently ignored by the provider
   normalizer, which was the bug that left a bogus bearer in play).
3. ``~/.hermes/auth.json``: stale ``custom:*`` credential-pool entries removed so
   ``resolve_runtime_provider`` falls through to the synced key.

Dependency-free (stdlib only) like ``mac.deploy_env``; the deploy runs it via the
mac venv after ``write-mac-env`` + ``ensure_hermes_home``, before the gateway and
agent start.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from pathlib import Path

from mac import mac_paths
from typing import Dict, List, Optional

# Explicit provider timeouts stamped into the deployed `custom` provider block.
#
# The router base URL is a Tailscale CGNAT / RFC-1918 address, which the
# vendored runtime's `is_local_endpoint()` heuristic classifies as a *local
# inference server* (the Ollama slow-prefill case). That heuristic then
# DISABLES the stale-stream detector and raises the httpx stream read timeout
# to HERMES_API_TIMEOUT (1800s) — so a silently dropped mid-stream connection
# wedges the turn until the executor's 900s kill (rc 124, evidence lost, task
# retried). Hosts whose tailnet path to the hub crosses NAT/DERP (the
# containerized GKE workers) hit this constantly; LAN-attached hosts never do.
# Per-provider config takes precedence over the heuristic in the runtime
# (`get_provider_request_timeout` / `get_provider_stale_timeout`), so stamping
# explicit values here re-enables stall detection + bounded reads fleet-wide.
# The router fronts cloud providers (~seconds per turn) — it is not a local
# inference engine, so local-prefill patience is never wanted here.
#
# Overridable via mac.env; a value <= 0 omits the key (explicit opt-out back
# into the runtime heuristic). Note: 180 must be avoided for the stale value —
# the runtime treats exactly 180.0 as "default" and re-applies the heuristic.
DEFAULT_PROVIDER_REQUEST_TIMEOUT_SECONDS = 600
DEFAULT_PROVIDER_STALE_TIMEOUT_SECONDS = 120
REQUEST_TIMEOUT_ENV = "MAC_HERMES_GATEWAY_REQUEST_TIMEOUT_SECONDS"
STALE_TIMEOUT_ENV = "MAC_HERMES_GATEWAY_STALE_TIMEOUT_SECONDS"

# Chat endpoint/credential vars the Hermes runtime reads; mirrored from mac.env.
CHAT_ENV_KEYS = (
    "OPENAI_BASE_URL",
    "CUSTOM_BASE_URL",
    "MAC_HERMES_GATEWAY_BASE_URL",
    "ACC_HERMES_GATEWAY_BASE_URL",
    "OPENAI_API_KEY",
    "MAC_HERMES_GATEWAY_API_KEY",
    "ACC_HERMES_GATEWAY_API_KEY",
    "MAC_HERMES_GATEWAY_PROVIDER",
    "ACC_HERMES_GATEWAY_PROVIDER",
    "HERMES_INFERENCE_PROVIDER",
)


def parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        key = key.replace("export ", "").strip()
        try:
            tokens = shlex.split(value)
            value = tokens[0] if tokens else ""
        except ValueError:
            value = value.strip()
        out[key] = value
    return out


def sync_hermes_env(hermes_home: Path, mac_env: Dict[str, str]) -> List[str]:
    """Overwrite the chat endpoint/key vars in ~/.hermes/.env with mac.env's."""
    henv = hermes_home / ".env"
    lines = henv.read_text(encoding="utf-8").splitlines() if henv.exists() else []
    out: List[str] = []
    seen, changed = set(), []
    for ln in lines:
        if "=" in ln and not ln.lstrip().startswith("#"):
            key = ln.split("=", 1)[0].strip()
            if key in CHAT_ENV_KEYS and key in mac_env:
                desired = "%s=%s" % (key, mac_env[key])
                if ln != desired:
                    changed.append(key)
                out.append(desired)
                seen.add(key)
                continue
        out.append(ln)
    for key in CHAT_ENV_KEYS:
        if key in mac_env and key not in seen:
            out.append("%s=%s" % (key, mac_env[key]))
            changed.append(key)
    henv.parent.mkdir(parents=True, exist_ok=True)
    henv.write_text("\n".join(out) + "\n", encoding="utf-8")
    _chmod_600(henv)
    return changed


def _provider_timeout(value: Optional[str], default: int) -> Optional[int]:
    """Resolve a provider timeout override: unset -> default, <= 0 -> omit."""
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        parsed = int(float(raw))
    except ValueError:
        return default
    return parsed if parsed > 0 else None


def sync_config_yaml(
    hermes_home: Path,
    base_url: str,
    api_key: str,
    *,
    request_timeout_seconds: Optional[int] = DEFAULT_PROVIDER_REQUEST_TIMEOUT_SECONDS,
    stale_timeout_seconds: Optional[int] = DEFAULT_PROVIDER_STALE_TIMEOUT_SECONDS,
) -> bool:
    """Point model.{provider,base_url} at the router and (re)define a `custom`
    provider with the bearer in `api_key` (the schema field). Line-based to
    preserve the rest of the file. Returns True if the custom provider was set.

    Robust to a freshly-initialized config.yaml that is only a stub (e.g. just a
    `web:` block): when the `model:` block or the `providers:` section is absent
    it is *created*, not just patched. The original patch-only version silently
    no-op'd on such configs, leaving fresh nodes with no chat provider (HTTP 403
    "unknown bearer token").

    The explicit timeouts keep the runtime's local-endpoint heuristic from
    treating the tailnet-addressed router as a slow local inference server
    (which disables stall recovery — see DEFAULT_PROVIDER_*_TIMEOUT_SECONDS)."""
    cfg = hermes_home / "config.yaml"
    if not cfg.exists() or not base_url or not api_key:
        return False
    base = base_url.rstrip("/")

    def custom_block() -> List[str]:
        block = [
            "  custom:",
            "    api: %s/" % base,
            "    name: custom",
            "    transport: chat_completions",
            "    api_key: %s" % api_key,
        ]
        if request_timeout_seconds is not None:
            block.append("    request_timeout_seconds: %d" % request_timeout_seconds)
        if stale_timeout_seconds is not None:
            block.append("    stale_timeout_seconds: %d" % stale_timeout_seconds)
        return block

    # 1) drop any existing top-level `  custom:` provider block (idempotent).
    pruned: List[str] = []
    skip = False
    for ln in cfg.read_text(encoding="utf-8").splitlines():
        if re.match(r"^  custom:\s*$", ln):
            skip = True
            continue
        if skip:
            if re.match(r"^    \S", ln) or ln.strip() == "":
                continue
            skip = False
        pruned.append(ln)

    # 2) patch the model: block + insert providers.custom after `providers:`,
    #    backfilling provider/base_url lines if the existing model block lacks them.
    res: List[str] = []
    model_seen = in_model = did_provider = did_base = did_custom = False
    for ln in pruned:
        if re.match(r"^model:\s*$", ln):
            model_seen = in_model = True
            res.append(ln)
            continue
        if in_model and re.match(r"^\S", ln):
            if not did_provider:
                res.append("  provider: custom")
                did_provider = True
            if not did_base:
                res.append("  base_url: %s/" % base)
                did_base = True
            in_model = False
        if in_model and not did_provider and re.match(r"^\s+provider:\s", ln):
            res.append(re.sub(r"(provider:\s*).*", r"\g<1>custom", ln))
            did_provider = True
            continue
        if in_model and not did_base and re.match(r"^\s+base_url:\s", ln):
            res.append(re.sub(r"(base_url:\s*).*", r"\g<1>%s/" % base, ln))
            did_base = True
            continue
        res.append(ln)
        if re.match(r"^providers:\s*$", ln) and not did_custom:
            res += custom_block()
            did_custom = True

    # close an open model block at EOF
    if in_model:
        if not did_provider:
            res.append("  provider: custom")
        if not did_base:
            res.append("  base_url: %s/" % base)

    # 3) create whichever top-level structures were missing entirely.
    if not model_seen:
        res += ["model:", "  provider: custom", "  base_url: %s/" % base]
    if not did_custom:
        res += ["providers:"] + custom_block()
        did_custom = True

    cfg.write_text("\n".join(res) + "\n", encoding="utf-8")
    _chmod_600(cfg)
    return did_custom


def clear_stale_custom_pool(hermes_home: Path) -> List[str]:
    """Remove `custom:*` credential-pool entries from auth.json so resolution
    falls through to the synced key (they repopulate from config on next use)."""
    authj = hermes_home / "auth.json"
    if not authj.exists():
        return []
    try:
        data = json.loads(authj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    removed: List[str] = []

    def scrub(obj: object) -> None:
        if isinstance(obj, dict):
            for k in list(obj.keys()):
                if isinstance(k, str) and k.startswith("custom:"):
                    removed.append(k)
                    del obj[k]
                else:
                    scrub(obj[k])
        elif isinstance(obj, list):
            for item in obj:
                scrub(item)

    scrub(data)
    if removed:
        authj.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _chmod_600(authj)
    return removed


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


# Prior deploy-managed image_gen defaults that should migrate forward to the
# current `default` on redeploy. Both are hub-routed to the same upstream
# (``nvidia`` posts straight to /v1/genai; ``mac-hub`` goes via the canonical
# /v1/media), so the upgrade is behavior-preserving. A genuine alternative
# backend (fal/openai/krea/xai) is NOT in this set, so an operator's real choice
# is respected.
_DEPLOY_MANAGED_IMAGE_PROVIDERS = ("nvidia", "mac-hub")


def ensure_image_gen_provider(hermes_home: Path, default: str = "mac-hub") -> str:
    """Keep ``image_gen.provider`` on the hub-routed ``mac-hub`` provider.

    ``mac-hub`` (media-01) routes text-to-image through the in-mac router's
    canonical ``/v1/media/image.generate`` endpoint, which resolves the provider
    binding(s), adapts the request, and fails over — so agents render images with
    NO per-agent ``FAL_KEY``, and it works on spokes, which carry no raw provider
    keys. Without a hub-routed default the registry's legacy fallback prefers
    ``fal`` (reports available via the managed gateway but has no usable key
    here), so image generation fails fleet-wide even though the router works.

    Sets the default when unset, and **migrates a prior deploy-managed default
    forward** (e.g. ``nvidia`` → ``mac-hub``; behavior-preserving, both route to
    the same upstream). A genuine alternative backend an operator chose
    (fal/openai/krea/xai) is respected, never overridden. Line-based to preserve
    the rest of the file. Returns the provider now in effect (``""`` when there
    is no config.yaml).
    """
    cfg = hermes_home / "config.yaml"
    if not cfg.exists():
        return ""
    lines = cfg.read_text(encoding="utf-8").splitlines()

    # Find the top-level `image_gen:` block and the `provider:` line within it.
    n = len(lines)
    provider_idx: Optional[int] = None
    current: Optional[str] = None
    indent = "  "
    for i, ln in enumerate(lines):
        if re.match(r"^image_gen:\s*$", ln):
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j].startswith((" ", "\t"))):
                m = re.match(r"^(\s+)provider:\s*(\S.*?)\s*$", lines[j])
                if m:
                    provider_idx, indent, current = j, m.group(1), m.group(2).strip().strip("'\"")
                    break
                j += 1
            break

    if current is not None:
        # Respect a genuine alternative; migrate a deploy-managed default forward.
        if current == default or current not in _DEPLOY_MANAGED_IMAGE_PROVIDERS:
            return current
        lines[provider_idx] = "%sprovider: %s" % (indent, default)
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _chmod_600(cfg)
        return default

    # Not set: insert `provider:` under an existing `image_gen:` block, else
    # append a fresh block at EOF.
    res: List[str] = []
    inserted = False
    for ln in lines:
        res.append(ln)
        if not inserted and re.match(r"^image_gen:\s*$", ln):
            res.append("  provider: %s" % default)
            inserted = True
    if not inserted:
        res += ["image_gen:", "  provider: %s" % default]
    cfg.write_text("\n".join(res) + "\n", encoding="utf-8")
    _chmod_600(cfg)
    return default


def sync(hermes_home: Path, mac_env_path: Path) -> Dict[str, object]:
    mac_env = parse_env_file(mac_env_path)
    base_url = (
        mac_env.get("MAC_HERMES_GATEWAY_BASE_URL") or mac_env.get("OPENAI_BASE_URL") or ""
    ).strip()
    api_key = (
        mac_env.get("MAC_HERMES_GATEWAY_API_KEY") or mac_env.get("OPENAI_API_KEY") or ""
    ).strip()
    return {
        "base_url": base_url,
        "key_present": bool(api_key),
        "env_synced": sync_hermes_env(hermes_home, mac_env),
        "config_custom_provider": sync_config_yaml(
            hermes_home,
            base_url,
            api_key,
            request_timeout_seconds=_provider_timeout(
                mac_env.get(REQUEST_TIMEOUT_ENV),
                DEFAULT_PROVIDER_REQUEST_TIMEOUT_SECONDS,
            ),
            stale_timeout_seconds=_provider_timeout(
                mac_env.get(STALE_TIMEOUT_ENV),
                DEFAULT_PROVIDER_STALE_TIMEOUT_SECONDS,
            ),
        ),
        "pool_cleared": clear_stale_custom_pool(hermes_home),
        "image_gen_provider": ensure_image_gen_provider(hermes_home),
    }


def main(argv: object = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.hermes_chat_config")
    parser.add_argument(
        "--hermes-home",
        default=str(mac_paths.gateway_home()),
    )
    parser.add_argument("--mac-env", default=str(mac_paths.mac_env_file()))
    ns = parser.parse_args(argv)
    result = sync(Path(ns.hermes_home), Path(ns.mac_env))
    # Never print key material — only booleans/keys/counts.
    printable = dict(result)
    printable["pool_cleared"] = len(result["pool_cleared"])  # type: ignore[arg-type]
    print("hermes chat config synced:", printable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
