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
from typing import Dict, List

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


def sync_config_yaml(hermes_home: Path, base_url: str, api_key: str) -> bool:
    """Point model.{provider,base_url} at the router and (re)define a `custom`
    provider with the bearer in `api_key` (the schema field). Line-based to
    preserve the rest of the file. Returns True if the custom provider was set.

    Robust to a freshly-initialized config.yaml that is only a stub (e.g. just a
    `web:` block): when the `model:` block or the `providers:` section is absent
    it is *created*, not just patched. The original patch-only version silently
    no-op'd on such configs, leaving fresh nodes with no chat provider (HTTP 403
    "unknown bearer token")."""
    cfg = hermes_home / "config.yaml"
    if not cfg.exists() or not base_url or not api_key:
        return False
    base = base_url.rstrip("/")

    def custom_block() -> List[str]:
        return [
            "  custom:",
            "    api: %s/" % base,
            "    name: custom",
            "    transport: chat_completions",
            "    api_key: %s" % api_key,
        ]

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
                res.append("  provider: custom"); did_provider = True
            if not did_base:
                res.append("  base_url: %s/" % base); did_base = True
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


def ensure_image_gen_provider(hermes_home: Path, default: str = "nvidia") -> str:
    """Default image generation to the hub-routed ``nvidia`` provider when the
    operator hasn't picked one.

    That backend routes text-to-image through the in-mac router's ``/v1/genai``
    proxy (using the hub's escrowed image key), so agents render images with NO
    per-agent ``FAL_KEY`` — and it works on spokes, which carry no raw provider
    keys. Without this default, the registry's legacy fallback prefers ``fal``
    (which reports available via the managed gateway but has no usable key here),
    so image generation fails fleet-wide even though ``/v1/genai`` works.

    Respects an explicit ``image_gen.provider`` (never overrides it). Line-based
    to preserve the rest of the file. Returns the provider now in effect (``""``
    when there is no config.yaml).
    """
    cfg = hermes_home / "config.yaml"
    if not cfg.exists():
        return ""
    lines = cfg.read_text(encoding="utf-8").splitlines()

    # Already chosen? Find the top-level `image_gen:` block and look for a
    # `provider:` line within it; respect any explicit value.
    n = len(lines)
    for i, ln in enumerate(lines):
        if re.match(r"^image_gen:\s*$", ln):
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j].startswith((" ", "\t"))):
                m = re.match(r"^\s+provider:\s*(\S.*?)\s*$", lines[j])
                if m:
                    return m.group(1).strip().strip("'\"")
                j += 1
            break

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
    base_url = (mac_env.get("MAC_HERMES_GATEWAY_BASE_URL") or mac_env.get("OPENAI_BASE_URL") or "").strip()
    api_key = (mac_env.get("MAC_HERMES_GATEWAY_API_KEY") or mac_env.get("OPENAI_API_KEY") or "").strip()
    return {
        "base_url": base_url,
        "key_present": bool(api_key),
        "env_synced": sync_hermes_env(hermes_home, mac_env),
        "config_custom_provider": sync_config_yaml(hermes_home, base_url, api_key),
        "pool_cleared": clear_stale_custom_pool(hermes_home),
        "image_gen_provider": ensure_image_gen_provider(hermes_home),
    }


def main(argv: object = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.hermes_chat_config")
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"),
    )
    parser.add_argument("--mac-env", default=str(Path.home() / ".mac" / "mac.env"))
    ns = parser.parse_args(argv)
    result = sync(Path(ns.hermes_home), Path(ns.mac_env))
    # Never print key material — only booleans/keys/counts.
    printable = dict(result)
    printable["pool_cleared"] = len(result["pool_cleared"])  # type: ignore[arg-type]
    print("hermes chat config synced:", printable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
