"""Launch the Fleet IDE with credentials from the active MAC client profile.

The browser cannot read ``~/.mac`` and should not need to.  This launcher keeps
the scoped client credential in the local Vite process, where the development
proxy attaches it to hub requests.  Only non-secret connection metadata is
exposed to the rendered application.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urlsplit

from mac.client_login import ClientLoginError, ensure_session
from mac.client_profiles import (
    ClientProfileError,
    active_profile_name,
    load_profile,
)


DEFAULT_API_URL = "http://127.0.0.1:8789"
DEFAULT_HUB_PORT = 8789
_AUTH_MODES = {"auto", "profile", "manual"}


class IdeLauncherError(RuntimeError):
    """Raised when the requested IDE authentication mode cannot be prepared."""


@dataclass(frozen=True)
class IdeConnection:
    api_url: str
    token: str = ""
    source: str = "manual"
    profile: Optional[str] = None
    hub_port: int = DEFAULT_HUB_PORT

    @property
    def managed(self) -> bool:
        return bool(self.token)


def _value(env: Mapping[str, str], key: str) -> str:
    return str(env.get(key) or "").strip()


def _legacy_token(env: Mapping[str, str]) -> tuple[str, str]:
    token = _value(env, "VITE_MAC_TOKEN")
    if token:
        return token, "VITE_MAC_TOKEN"

    fleet = _value(env, "IDE_FLEET") or _value(env, "MAC_FLEET")
    if fleet:
        suffix = "_".join(part for part in _normalize_fleet(fleet).split("_") if part)
        if suffix:
            key = "MAC_API_TOKEN__" + suffix
            token = _value(env, key)
            if token:
                return token, key

    for key in ("MAC_DEPLOY_HUB_TOKEN", "MAC_API_TOKEN"):
        token = _value(env, key)
        if token:
            return token, key
    return "", "manual"


def _normalize_fleet(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value.upper()).strip("_")


def resolve_ide_connection(env: Optional[Mapping[str, str]] = None) -> IdeConnection:
    values = os.environ if env is None else env
    auth_mode = _value(values, "IDE_AUTH") or "auto"
    if auth_mode not in _AUTH_MODES:
        raise IdeLauncherError(
            "IDE_AUTH must be one of auto, profile, or manual (got %r)" % auth_mode
        )

    explicit_api_url = _value(values, "IDE_API_URL")
    if auth_mode == "manual":
        return IdeConnection(api_url=explicit_api_url or DEFAULT_API_URL)

    explicit_token = _value(values, "IDE_TOKEN")
    if explicit_token:
        return IdeConnection(
            api_url=explicit_api_url or DEFAULT_API_URL,
            token=explicit_token,
            source="IDE_TOKEN",
        )

    requested_profile = _value(values, "IDE_PROFILE")
    try:
        selected_profile = requested_profile or active_profile_name()
    except (ClientProfileError, OSError) as exc:
        raise IdeLauncherError("could not inspect the active MAC login: %s" % exc) from exc

    if selected_profile:
        try:
            ensure_session(selected_profile)
            profile = load_profile(selected_profile, include_token=True)
        except (ClientLoginError, ClientProfileError, OSError) as exc:
            raise IdeLauncherError(
                "could not use MAC login profile %r: %s" % (selected_profile, exc)
            ) from exc
        connection = dict(profile.get("connection") or {})
        credential = dict(profile.get("credential") or {})
        api_url = explicit_api_url or str(connection.get("api_url") or "").strip()
        token = str(credential.get("token") or "").strip()
        try:
            hub_port = int(connection.get("remote_port") or DEFAULT_HUB_PORT)
        except (TypeError, ValueError) as exc:
            raise IdeLauncherError(
                "MAC login profile %r has an invalid remote hub port" % selected_profile
            ) from exc
        if not 1 <= hub_port <= 65535:
            raise IdeLauncherError(
                "MAC login profile %r has an invalid remote hub port" % selected_profile
            )
        if not api_url:
            raise IdeLauncherError(
                "MAC login profile %r has no API URL" % selected_profile
            )
        if not token:
            raise IdeLauncherError(
                "MAC login profile %r has no stored credential" % selected_profile
            )
        return IdeConnection(
            api_url=api_url,
            token=token,
            source="client-profile:%s" % selected_profile,
            profile=selected_profile,
            hub_port=hub_port,
        )

    if requested_profile or auth_mode == "profile":
        raise IdeLauncherError(
            "no active MAC login profile; run `mac login` before starting the GUI"
        )

    token, source = _legacy_token(values)
    return IdeConnection(
        api_url=explicit_api_url or DEFAULT_API_URL,
        token=token,
        source=source,
    )


def _validated_api_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IdeLauncherError(
            "hub URL must include http:// or https:// and a hostname"
        )
    if parsed.username or parsed.password:
        raise IdeLauncherError("hub URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise IdeLauncherError("hub URL must not contain a query string or fragment")
    if parsed.path not in {"", "/"}:
        raise IdeLauncherError("hub URL must not contain a path")
    return value


def _api_url_for_hub_target(raw: str, *, default_port: int) -> str:
    """Resolve an operator-entered host/IP without leaking tunnel port details.

    A login profile's API URL normally points at an ephemeral loopback SSH
    tunnel.  That local port is meaningful only on localhost.  Bare hostnames
    and IPs therefore use the profile's recorded remote hub port instead.
    Advanced operators can still enter a complete URL or an explicit port.
    """

    value = raw.strip().rstrip("/")
    if "://" in value:
        return _validated_api_url(value)

    parsed = urlsplit("//" + value)
    if not parsed.hostname:
        raise IdeLauncherError("enter a hub hostname or IP address")
    if parsed.username or parsed.password:
        raise IdeLauncherError("hub target must not contain credentials")
    if parsed.query or parsed.fragment:
        raise IdeLauncherError("hub target must not contain a query string or fragment")
    if parsed.path not in {"", "/"}:
        raise IdeLauncherError("hub target must not contain a path")
    try:
        port = parsed.port or default_port
    except ValueError as exc:
        raise IdeLauncherError("hub target has an invalid port") from exc
    if not 1 <= port <= 65535:
        raise IdeLauncherError("hub target has an invalid port")
    host = parsed.hostname
    if ":" in host:
        host = "[%s]" % host
    return "http://%s:%d" % (host, port)


def prompt_for_ide_connection(
    connection: IdeConnection,
    env: Optional[Mapping[str, str]] = None,
    *,
    input_fn: Optional[Callable[[str], str]] = None,
    interactive: Optional[bool] = None,
) -> IdeConnection:
    """Let an interactive operator select the hub before Vite starts.

    Explicit ``IDE_API_URL`` values and non-interactive launches remain
    prompt-free. Bare hosts use the hub's remote API port rather than the
    profile's ephemeral local tunnel port. The selected URL still flows through
    Vite's local proxy, so a managed profile token never enters browser storage.
    """

    values = os.environ if env is None else env
    if _value(values, "IDE_API_URL"):
        return connection
    should_prompt = sys.stdin.isatty() if interactive is None else interactive
    if not should_prompt:
        return connection

    read = input if input_fn is None else input_fn
    while True:
        try:
            entered = read(
                "Target hub host or IP "
                "[Enter keeps %s; direct port %d]: "
                % (connection.api_url, connection.hub_port)
            ).strip()
        except EOFError:
            return connection
        if not entered:
            return connection
        try:
            api_url = _api_url_for_hub_target(
                entered,
                default_port=connection.hub_port,
            )
        except IdeLauncherError as exc:
            print("Invalid hub target: %s" % exc, file=sys.stderr)
            continue
        return replace(connection, api_url=api_url)


def build_vite_environment(
    connection: IdeConnection,
    env: Optional[Mapping[str, str]] = None,
) -> MutableMapping[str, str]:
    child = dict(os.environ if env is None else env)
    child["MAC_API_URL"] = connection.api_url

    # Remove every browser-visible or legacy copy after resolving it.  The one
    # credential retained by the child is consumed only by vite.config.ts.
    for key in list(child):
        if key in {
            "IDE_TOKEN",
            "VITE_MAC_TOKEN",
            "MAC_API_TOKEN",
            "MAC_DEPLOY_HUB_TOKEN",
            "MAC_IDE_PROXY_TOKEN",
        } or key.startswith("MAC_API_TOKEN__"):
            child.pop(key, None)

    if connection.managed:
        child["MAC_IDE_PROXY_TOKEN"] = connection.token
        child["VITE_MAC_AUTH_MODE"] = "managed"
        child["VITE_MAC_AUTH_LABEL"] = (
            "CLI profile %s" % connection.profile
            if connection.profile
            else "launcher credential"
        )
    else:
        child.pop("VITE_MAC_AUTH_MODE", None)
        child.pop("VITE_MAC_AUTH_LABEL", None)
    return child


def vite_command(env: Mapping[str, str]) -> Sequence[str]:
    npm = shlex.split(_value(env, "NPM") or "npm")
    host = _value(env, "IDE_HOST") or "127.0.0.1"
    port = _value(env, "IDE_PORT") or "5273"
    return [*npm, "run", "dev", "--", "--host", host, "--port", port]


def run(env: Optional[Mapping[str, str]] = None) -> int:
    values = dict(os.environ if env is None else env)
    connection = resolve_ide_connection(values)
    connection = prompt_for_ide_connection(connection, values)
    child = build_vite_environment(connection, values)
    ide_dir = Path(_value(values, "IDE_DIR") or "ide").expanduser().resolve()
    if not (ide_dir / "package.json").is_file():
        raise IdeLauncherError("Fleet IDE package was not found at %s" % ide_dir)

    if connection.profile:
        print(
            "IDE connection: MAC login profile %s via %s"
            % (connection.profile, connection.api_url)
        )
    else:
        print("IDE connection: %s" % connection.api_url)
    if connection.managed:
        print(
            "IDE auth: managed by the local proxy from %s "
            "(credential is not exposed to the browser)" % connection.source
        )
    else:
        print(
            "IDE auth: no CLI profile or launcher credential; "
            "the browser will show the manual fallback"
        )

    completed = subprocess.run(vite_command(values), cwd=ide_dir, env=child, check=False)
    return int(completed.returncode)


def main() -> None:
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except IdeLauncherError as exc:
        print("Fleet IDE launch failed: %s" % exc, file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
