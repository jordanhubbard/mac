"""Read and write MAC's dotenv files (``~/.mac/mac.env``, ``~/.mac/.env``).

Dependency-free apart from ``mac.atomic_file``: install scripts such as
``deploy/hermes/install-hermes-gateway.sh`` import it before the rest of the
package is guaranteed to be importable.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
import re
import shlex
from typing import Dict, Iterator, Mapping, Optional

from mac.atomic_file import atomic_write_text


DEFAULT_WORKER_CAPABILITIES = (
    "ops,python,hermes,review,api,architecture,cli,docs,security,testing,"
    "typescript,ui,web_search,web_extract,web_crawl,firecrawl"
)


#: The removed chat-gateway runtime. Hermes is the only human interface, so a
#: configuration that still names this runtime is refused with an error rather
#: than silently treated as Hermes.
RETIRED_HUMAN_INTERFACE = "openclaw"


def reject_retired_human_interface(value: str, setting: str) -> None:
    """Raise ``ValueError`` when *setting* still names the removed runtime."""
    if str(value or "").strip().lower() == RETIRED_HUMAN_INTERFACE:
        raise ValueError(
            "%s=%s is no longer supported: that runtime was removed from MAC and "
            "Hermes is the only human interface. Use 'hermes' instead."
            % (setting, RETIRED_HUMAN_INTERFACE)
        )


def chat_gateway_implementation(env: Optional[Mapping[str, str]] = None) -> str:
    """The configured ``MAC_CHAT_GATEWAY_IMPL`` (lower-cased; ``""`` when unset)."""
    source = os.environ if env is None else env
    value = str(source.get("MAC_CHAT_GATEWAY_IMPL") or "").strip().lower()
    reject_retired_human_interface(value, "MAC_CHAT_GATEWAY_IMPL")
    return value


def normalize_worker_capabilities(value: str) -> str:
    """De-duplicate worker capabilities, defaulting when none are configured."""
    items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    if not items:
        return DEFAULT_WORKER_CAPABILITIES
    for item in items:
        reject_retired_human_interface(item, "worker capability")
    return ",".join(dict.fromkeys(items))


def _raw_env_assignment(line: str) -> Optional[tuple[str, str]]:
    """Plain ``KEY=VALUE`` split for quote-free lines (no shell interpretation)."""
    if line.startswith("export "):
        line = line[len("export ") :]
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    return key.strip(), value.strip()


def _parse_env_assignment(line: str) -> Optional[tuple[str, str]]:
    """Parse one ``KEY=VALUE`` (optionally ``export``-prefixed) env line.

    Well-formed lines go through ``shlex`` so quoted/escaped values round-trip
    exactly with ``render_env``'s ``shlex.quote``. Documented edge semantics:

    - **Malformed shell quoting** (e.g. an unbalanced quote): the line is treated
      as corrupt and skipped (``None``) rather than silently storing a
      half-parsed value — *unless* it is quote-free, in which case the plain
      ``KEY=VALUE`` split is unambiguous and is used.
    - **Trailing unquoted tokens** (``KEY=val extra``): the leading assignment
      wins (``val``); trailing tokens are ignored. ``render_env`` never emits
      this (unsafe values are quoted), so it only arises from hand-edited files.
    """
    try:
        tokens = shlex.split(line, comments=False, posix=True)
    except ValueError:
        if '"' in line or "'" in line:
            return None
        return _raw_env_assignment(line)
    if tokens:
        if tokens[0] == "export":
            tokens = tokens[1:]
        if tokens and "=" in tokens[0]:
            key, value = tokens[0].split("=", 1)
            return key.strip(), value
    return _raw_env_assignment(line)


def parse_env_text(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_env_assignment(line)
        if parsed is None:
            continue
        key, value = parsed
        values[key] = value
    return values


def read_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))


@contextmanager
def env_file_lock(path: Path) -> Iterator[None]:
    """Serialize read-modify-write access to a deployment env file.

    Several independent processes (worker-token installs,
    attestation-key installation) each read the whole env file, change one
    key, and write the whole file back. Without mutual exclusion, whichever
    write lands last silently discards the other's key -- observed in
    practice as a deploy generation write being clobbered back to a stale
    value by a concurrent attestation-key install, which then makes every
    later generation-match guard fail. Callers must perform their full
    read-modify-write cycle inside this context.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


_ENV_SAFE = re.compile(r"^[A-Za-z0-9_./:@=,+%-]*$")


def render_env(values: Mapping[str, str]) -> str:
    lines = [
        "# Generated by mac.",
        "# Contains bearer tokens; keep mode 0600.",
    ]
    for key in sorted(values):
        rendered = str(values[key])
        if _ENV_SAFE.match(rendered):
            lines.append("%s=%s" % (key, rendered))
        else:
            lines.append("%s=%s" % (key, shlex.quote(rendered)))
    return "\n".join(lines) + "\n"


def write_env_file(path: Path, values: Mapping[str, str]) -> None:
    atomic_write_text(path, render_env(values), mode=0o600)


def update_env_file(path: Path, updates: Mapping[str, str]) -> Dict[str, str]:
    """Atomically merge deployment-owned values without losing concurrent writes."""
    with env_file_lock(path):
        values = read_env_file(path)
        values.update({key: str(value) for key, value in updates.items()})
        write_env_file(path, values)
    return values
