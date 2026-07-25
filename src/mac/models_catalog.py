"""Native models.dev catalog access for MAC.

This module replaces the last MAC-owned runtime dependency on the vendored
Hermes models.dev helper. It exposes the two accessors MAC uses with matching
signatures:

* ``list_agentic_models(provider)``
* ``get_model_info(provider_id, model_id)``

The public provider catalog is served at https://models.dev/api.json. Runtime
lookups use a short in-memory cache, a MAC-owned disk cache, then the network,
falling back to stale disk data if the network is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from mac import mac_paths
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
_CACHE_TTL_SECONDS = 3600

_catalog_cache: Dict[str, Any] = {}
_catalog_cache_time: float = 0.0
_catalog_lock = threading.RLock()


@dataclass
class ModelInfo:
    """Full metadata for a single provider/model entry from models.dev."""

    id: str
    name: str
    family: str
    provider_id: str
    reasoning: bool = False
    tool_call: bool = False
    attachment: bool = False
    temperature: bool = False
    structured_output: bool = False
    open_weights: bool = False
    input_modalities: Tuple[str, ...] = ()
    output_modalities: Tuple[str, ...] = ()
    context_window: int = 0
    max_output: int = 0
    max_input: Optional[int] = None
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: Optional[float] = None
    cost_cache_write: Optional[float] = None
    knowledge_cutoff: str = ""
    release_date: str = ""
    status: str = ""
    interleaved: Any = False

    def has_cost_data(self) -> bool:
        return self.cost_input > 0 or self.cost_output > 0

    def supports_vision(self) -> bool:
        return self.attachment or "image" in self.input_modalities

    def supports_pdf(self) -> bool:
        return "pdf" in self.input_modalities

    def supports_audio_input(self) -> bool:
        return "audio" in self.input_modalities

    def format_cost(self) -> str:
        if not self.has_cost_data():
            return "unknown"
        parts = ["$%.2f/M in" % self.cost_input, "$%.2f/M out" % self.cost_output]
        if self.cost_cache_read is not None:
            parts.append("cache read $%.2f/M" % self.cost_cache_read)
        return ", ".join(parts)


# Provider aliases used in MAC router configuration, mapped to models.dev IDs.
PROVIDER_TO_MODELS_DEV: Dict[str, str] = {
    "openrouter": "openrouter",
    "novita": "novita-ai",
    "anthropic": "anthropic",
    "openai": "openai",
    "openai-codex": "openai",
    "zai": "zai",
    "kimi": "kimi-for-coding",
    "kimi-coding": "kimi-for-coding",
    "moonshot": "kimi-for-coding",
    "stepfun": "stepfun",
    "kimi-coding-cn": "kimi-for-coding",
    "minimax": "minimax",
    "minimax-oauth": "minimax",
    "minimax-cn": "minimax-cn",
    "deepseek": "deepseek",
    "alibaba": "alibaba",
    "qwen": "alibaba",
    "qwen-oauth": "alibaba",
    "copilot": "github-copilot",
    "opencode-zen": "opencode",
    "opencode-go": "opencode-go",
    "kilocode": "kilo",
    "fireworks": "fireworks-ai",
    "huggingface": "huggingface",
    "gemini": "google",
    "google": "google",
    "xai": "xai",
    "xai-oauth": "xai",
    "xiaomi": "xiaomi",
    "nvidia": "nvidia",
    "groq": "groq",
    "mistral": "mistral",
    "togetherai": "togetherai",
    "perplexity": "perplexity",
    "cohere": "cohere",
    "ollama-cloud": "ollama-cloud",
}


def _cache_path() -> Path:
    configured = str(os.environ.get("MAC_MODELS_DEV_CACHE_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    home = str(os.environ.get("MAC_HOME") or "").strip()
    base = Path(home).expanduser() if home else mac_paths.mac_home()
    return base / "models-dev-cache.json"


def _load_disk_cache() -> Dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("failed to load models.dev disk cache: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _disk_cache_age_seconds() -> Optional[float]:
    try:
        mtime = _cache_path().stat().st_mtime
    except OSError as exc:
        logger.debug("failed to stat models.dev disk cache: %s", exc)
        return None
    age = time.time() - mtime
    return age if age >= 0 else None


def _save_disk_cache(data: Dict[str, Any]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, sort_keys=True, separators=(",", ":")))
                fh.write("\n")
            os.replace(tmp_name, str(path))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("failed to save models.dev disk cache: %s", exc)


def _fetch_url_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "mac-models-catalog/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def fetch_models_dev(force_refresh: bool = False) -> Dict[str, Any]:
    """Fetch the models.dev provider registry.

    Cache order when ``force_refresh`` is false: memory, fresh disk, network.
    If the network fails, stale disk data is still returned when available.
    """
    global _catalog_cache, _catalog_cache_time
    now = time.time()
    with _catalog_lock:
        if (
            not force_refresh
            and _catalog_cache
            and (now - _catalog_cache_time) < _CACHE_TTL_SECONDS
        ):
            return _catalog_cache

        if not force_refresh:
            disk_age = _disk_cache_age_seconds()
            if disk_age is not None and disk_age < _CACHE_TTL_SECONDS:
                disk_data = _load_disk_cache()
                if disk_data:
                    _catalog_cache = disk_data
                    _catalog_cache_time = time.time() - disk_age
                    return _catalog_cache

        try:
            data = _fetch_url_json(MODELS_DEV_URL)
        except Exception as exc:  # noqa: BLE001
            logger.debug("failed to fetch models.dev catalog: %s", exc)
            data = {}
        if data:
            _catalog_cache = data
            _catalog_cache_time = time.time()
            _save_disk_cache(data)
            return _catalog_cache

        disk_data = _load_disk_cache()
        if disk_data:
            _catalog_cache = disk_data
            _catalog_cache_time = time.time() - _CACHE_TTL_SECONDS + 300
        return _catalog_cache


def _models_dev_provider_id(provider_id: str) -> str:
    raw = str(provider_id or "").strip()
    return PROVIDER_TO_MODELS_DEV.get(raw.lower(), raw)


def _get_provider_models(provider_id: str) -> Optional[Dict[str, Any]]:
    data = fetch_models_dev()
    raw = data.get(_models_dev_provider_id(provider_id))
    if not isinstance(raw, dict):
        return None
    models = raw.get("models")
    return models if isinstance(models, dict) else None


_NOISE_PATTERNS = re.compile(
    r"-tts\b|embedding|live-|-(preview|exp)-\d{2,4}[-_]|"
    r"-image\b|-image-preview\b|-customtools\b",
    re.IGNORECASE,
)

_GOOGLE_HIDDEN_MODELS = frozenset(
    {
        "gemma-4-31b-it",
        "gemma-4-26b-it",
        "gemma-4-26b-a4b-it",
        "gemma-3-1b",
        "gemma-3-1b-it",
        "gemma-3-2b",
        "gemma-3-2b-it",
        "gemma-3-4b",
        "gemma-3-4b-it",
        "gemma-3-12b",
        "gemma-3-12b-it",
        "gemma-3-27b",
        "gemma-3-27b-it",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash-8b",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    }
)


def _should_hide_from_provider_catalog(provider_id: str, model_id: str) -> bool:
    provider = _models_dev_provider_id(provider_id).lower()
    model = str(model_id or "").strip().lower()
    return provider == "google" and model in _GOOGLE_HIDDEN_MODELS


def list_agentic_models(provider: str) -> List[str]:
    """Return tool-capable model IDs for ``provider`` from models.dev."""
    models = _get_provider_models(provider)
    if models is None:
        return []
    result: List[str] = []
    for mid, entry in models.items():
        if not isinstance(entry, dict):
            continue
        if _should_hide_from_provider_catalog(provider, str(mid)):
            continue
        if not entry.get("tool_call", False):
            continue
        if _NOISE_PATTERNS.search(str(mid)):
            continue
        result.append(str(mid))
    return result


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _positive_int(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


def _parse_model_info(model_id: str, raw: Dict[str, Any], provider_id: str) -> ModelInfo:
    limit = raw.get("limit") if isinstance(raw.get("limit"), dict) else {}
    cost = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
    modalities = raw.get("modalities") if isinstance(raw.get("modalities"), dict) else {}
    input_mods = modalities.get("input") or []
    output_mods = modalities.get("output") or []

    cache_read = _number(cost.get("cache_read"))
    cache_write = _number(cost.get("cache_write"))
    input_limit = _positive_int(limit.get("input"))

    return ModelInfo(
        id=model_id,
        name=str(raw.get("name") or model_id),
        family=str(raw.get("family") or ""),
        provider_id=provider_id,
        reasoning=bool(raw.get("reasoning", False)),
        tool_call=bool(raw.get("tool_call", False)),
        attachment=bool(raw.get("attachment", False)),
        temperature=bool(raw.get("temperature", False)),
        structured_output=bool(raw.get("structured_output", False)),
        open_weights=bool(raw.get("open_weights", False)),
        input_modalities=tuple(input_mods) if isinstance(input_mods, list) else (),
        output_modalities=tuple(output_mods) if isinstance(output_mods, list) else (),
        context_window=_positive_int(limit.get("context")),
        max_output=_positive_int(limit.get("output")),
        max_input=input_limit or None,
        cost_input=_number(cost.get("input")) or 0.0,
        cost_output=_number(cost.get("output")) or 0.0,
        cost_cache_read=cache_read,
        cost_cache_write=cache_write,
        knowledge_cutoff=str(raw.get("knowledge") or ""),
        release_date=str(raw.get("release_date") or ""),
        status=str(raw.get("status") or ""),
        interleaved=raw.get("interleaved", False),
    )


def get_model_info(provider_id: str, model_id: str) -> Optional[ModelInfo]:
    """Return provider-specific model metadata, or None when absent."""
    mdev_id = _models_dev_provider_id(provider_id)
    models = _get_provider_models(mdev_id)
    if models is None:
        return None

    raw = models.get(model_id)
    if isinstance(raw, dict):
        return _parse_model_info(str(model_id), raw, mdev_id)

    wanted = str(model_id or "").lower()
    for mid, entry in models.items():
        if str(mid).lower() == wanted and isinstance(entry, dict):
            return _parse_model_info(str(mid), entry, mdev_id)
    return None


__all__ = [
    "MODELS_DEV_URL",
    "ModelInfo",
    "PROVIDER_TO_MODELS_DEV",
    "fetch_models_dev",
    "get_model_info",
    "list_agentic_models",
]
