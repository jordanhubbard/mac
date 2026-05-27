#!/usr/bin/env bash
# install-tokenhub-service.sh - install/start the hub TokenHub service.
#
# TokenHub is MAC's authority for upstream LLM/search/embed provider secrets
# and model routing. MAC and Hermes keep only TokenHub client credentials in
# their runtime env files.
set -euo pipefail

MAC_HOME="${MAC_HOME:-$HOME/.mac}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
FLEET_NAME="${FLEET_NAME:-mac}"
SERVICE_NAME="${FLEET_NAME}-tokenhub.service"
USER_SERVICE_NAME="${TOKENHUB_USER_SERVICE_NAME:-tokenhub.service}"
PROGRAM_NAME="${FLEET_NAME}-tokenhub"
SUPERVISOR_KIND="${TOKENHUB_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"

TOKENHUB_STATE_DIR="${TOKENHUB_STATE_DIR:-$HOME/.tokenhub}"
TOKENHUB_BIN_DIR="${TOKENHUB_BIN_DIR:-$HOME/.local/bin}"
TOKENHUB_REPO="${TOKENHUB_REPO:-$HOME/.mac/vendor/tokenhub}"
TOKENHUB_REPO_URL="${TOKENHUB_REPO_URL:-https://github.com/jordanhubbard/tokenhub.git}"
TOKENHUB_REF="${TOKENHUB_REF:-}"
TOKENHUB_PORT="${TOKENHUB_PORT:-${MAC_TOKENHUB_PORT:-8090}}"
TOKENHUB_GO_VERSION="${TOKENHUB_GO_VERSION:-1.24.0}"
TOKENHUB_CREDENTIALS_FILE="${TOKENHUB_CREDENTIALS_FILE:-$TOKENHUB_STATE_DIR/credentials}"
TOKENHUB_VAULT_ENABLED="${TOKENHUB_VAULT_ENABLED:-true}"
MAC_ENV_FILE="${MAC_ENV_FILE:-$MAC_HOME/mac.env}"
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HERMES_HOME/.env}"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
  else
    echo "[tokenhub] ERROR: python3 or python is required" >&2
    exit 1
  fi
fi

log() {
  printf '[tokenhub] %s\n' "$*"
}

detect_supervisor() {
  case "$SUPERVISOR_KIND" in
    systemd|systemd-user|launchd|supervisord)
      printf '%s\n' "$SUPERVISOR_KIND"
      return
      ;;
    auto|"")
      ;;
    *)
      echo "[tokenhub] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2
      exit 1
      ;;
  esac
  if command -v systemctl >/dev/null 2>&1 && systemctl --user --quiet is-active "$USER_SERVICE_NAME" >/dev/null 2>&1; then
    printf '%s\n' "systemd-user"
    return
  fi
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    printf '%s\n' "systemd"
    return
  fi
  if command -v launchctl >/dev/null 2>&1; then
    printf '%s\n' "launchd"
    return
  fi
  if command -v supervisorctl >/dev/null 2>&1; then
    printf '%s\n' "supervisord"
    return
  fi
  echo "[tokenhub] ERROR: could not detect systemd, launchd, or supervisord" >&2
  exit 1
}

run_supervisorctl() {
  if command -v sudo >/dev/null 2>&1; then
    sudo supervisorctl "$@" || supervisorctl "$@"
  else
    supervisorctl "$@"
  fi
}

supervisord_conf_dir() {
  if [ -n "${MAC_DEPLOY_SUPERVISOR_CONF_DIR:-}" ]; then
    printf '%s\n' "$MAC_DEPLOY_SUPERVISOR_CONF_DIR"
  elif [ -d /etc/supervisor/conf.d ]; then
    printf '%s\n' "/etc/supervisor/conf.d"
  elif [ -d /etc/supervisord.d ]; then
    printf '%s\n' "/etc/supervisord.d"
  else
    printf '%s\n' "/etc/supervisor/conf.d"
  fi
}

set_env_key() {
  local file="$1" key="$2" value="$3"
  "$PYTHON_BIN" - "$file" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1]).expanduser()
key = sys.argv[2]
value = sys.argv[3]
path.parent.mkdir(parents=True, exist_ok=True)
lines = []
seen = False
if path.exists():
    lines = path.read_text(encoding="utf-8").splitlines()
for idx, line in enumerate(lines):
    if line.startswith(key + "="):
        lines[idx] = "%s=%s" % (key, value)
        seen = True
if not seen:
    lines.append("%s=%s" % (key, value))
path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

delete_env_keys() {
  local file="$1"
  shift
  [ -f "$file" ] || return 0
  "$PYTHON_BIN" - "$file" "$@" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1]).expanduser()
blocked = set(sys.argv[2:])
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in blocked:
            continue
    lines.append(line)
path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

read_env_value() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 1
  "$PYTHON_BIN" - "$file" "$key" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1]).expanduser()
key = sys.argv[2]
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith(key + "="):
        print(line.split("=", 1)[1])
        raise SystemExit(0)
raise SystemExit(1)
PY
}

random_secret() {
  "$PYTHON_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

detect_tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -1
  fi
}

host_from_url() {
  "$PYTHON_BIN" - "$1" <<'PY'
import sys
import urllib.parse
parsed = urllib.parse.urlsplit(sys.argv[1])
print(parsed.hostname or "")
PY
}

default_tokenhub_bind_addr() {
  local host ts_ip
  if [ -n "${TOKENHUB_URL:-}" ]; then
    host="$(host_from_url "$TOKENHUB_URL")"
    case "$host" in
      ""|localhost|127.*|::1)
        ;;
      # Bare IP addresses (including Tailscale 100.x.x.x) are directly bindable.
      # DNS hostnames (e.g. K8s service FQDNs) resolve to IPs the pod can't bind
      # to from the inside, so fall through and use 0.0.0.0 for those.
      [0-9]*.*.*.*|[0-9a-fA-F:]*:*)
        printf '%s\n' "$host"
        return
        ;;
    esac
  fi
  ts_ip="$(detect_tailscale_ip || true)"
  if [ -n "$ts_ip" ]; then
    printf '%s\n' "$ts_ip"
  else
    printf '%s\n' "0.0.0.0"
  fi
}

TOKENHUB_BIND_ADDR="${TOKENHUB_BIND_ADDR:-${MAC_TOKENHUB_BIND_ADDR:-$(default_tokenhub_bind_addr)}}"
TOKENHUB_URL="${TOKENHUB_URL:-http://${TOKENHUB_BIND_ADDR}:${TOKENHUB_PORT}}"

if [ "${1:-}" = "--print-bind-addr" ]; then
  printf '%s\n' "$TOKENHUB_BIND_ADDR"
  exit 0
fi

ensure_go() {
  if command -v go >/dev/null 2>&1; then
    return
  fi
  case "$(uname -s)" in
    Linux) ;;
    *)
      echo "[tokenhub] ERROR: Go is required to build TokenHub on this platform" >&2
      exit 1
      ;;
  esac
  local arch go_arch tmp archive
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) go_arch="amd64" ;;
    aarch64|arm64) go_arch="arm64" ;;
    *)
      echo "[tokenhub] ERROR: unsupported Go architecture: $arch" >&2
      exit 1
      ;;
  esac
  archive="go${TOKENHUB_GO_VERSION}.linux-${go_arch}.tar.gz"
  tmp="$(mktemp -d)"
  log "installing Go ${TOKENHUB_GO_VERSION} for TokenHub build"
  curl -fsSL "https://go.dev/dl/${archive}" -o "$tmp/${archive}"
  sudo rm -rf /usr/local/go
  sudo tar -C /usr/local -xzf "$tmp/${archive}"
  rm -rf "$tmp"
  export PATH="/usr/local/go/bin:$PATH"
}

_tokenhub_github_owner_repo() {
  "$PYTHON_BIN" - "$TOKENHUB_REPO_URL" <<'PY'
import re, sys
url = sys.argv[1].rstrip("/")
m = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$', url)
print(m.group(1) if m else "")
PY
}

_install_tokenhub_from_release() {
  local owner_repo="$1"
  local api_url="" release_json="" uname_s="" uname_m="" tmp_dir="" downloaded="" ok=0
  if [ -n "${TOKENHUB_REF:-}" ]; then
    api_url="https://api.github.com/repos/${owner_repo}/releases/tags/${TOKENHUB_REF}"
  else
    api_url="https://api.github.com/repos/${owner_repo}/releases/latest"
  fi
  log "checking for TokenHub release binaries at $api_url"
  release_json="$(curl -fsSL --connect-timeout 10 --max-time 30 \
    ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} \
    "$api_url" 2>/dev/null || true)"
  [ -n "$release_json" ] || return 1

  uname_s="$(uname -s)"
  uname_m="$(uname -m)"
  tmp_dir="$(mktemp -d)"

  downloaded="$("$PYTHON_BIN" - "$release_json" "$uname_s" "$uname_m" "$tmp_dir" "$TOKENHUB_BIN_DIR" <<'PY'
import json, os, re, subprocess, sys, tarfile, zipfile, urllib.request
from pathlib import Path

release = json.loads(sys.argv[1])
uname_s, uname_m, tmp_dir, bin_dir = sys.argv[2], sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])

# Normalise OS / arch into common naming variants
os_variants = {"Linux": ["Linux", "linux"], "Darwin": ["Darwin", "darwin", "macos"]}
arch_variants = {
    "x86_64": ["x86_64", "amd64"],
    "aarch64": ["arm64", "aarch64"],
    "arm64": ["arm64", "aarch64"],
}
os_keys = os_variants.get(uname_s, [uname_s.lower()])
arch_keys = arch_variants.get(uname_m, [uname_m.lower()])

assets = release.get("assets", [])
if not assets:
    raise SystemExit(1)

def score(name):
    n = name.lower()
    for ok in os_keys:
        for ak in arch_keys:
            if ok.lower() in n and ak.lower() in n:
                # prefer archives over raw binaries (easier to handle checksums later)
                return (2 if n.endswith(".tar.gz") else 1 if n.endswith(".zip") else 0)
    return -1

scored = sorted([(score(a["name"]), a) for a in assets], key=lambda x: -x[0])
best_score, best_asset = scored[0] if scored else (-1, None)
if best_score < 0 or best_asset is None:
    raise SystemExit(1)

tag = release.get("tag_name", "unknown")
url = best_asset["browser_download_url"]
name = best_asset["name"]
dest = tmp_dir / name
print("downloading %s (%s)" % (name, tag), flush=True)

req = urllib.request.Request(url)
token = os.environ.get("GITHUB_TOKEN", "")
if token:
    req.add_header("Authorization", "Bearer " + token)
with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
    f.write(resp.read())

bin_dir.mkdir(parents=True, exist_ok=True)

def extract_bins(src):
    names = ["tokenhub", "tokenhubctl"]
    if src.suffix == ".gz" and src.stem.endswith(".tar"):
        with tarfile.open(src) as tf:
            for member in tf.getmembers():
                base = Path(member.name).name
                if base in names:
                    out = bin_dir / base
                    with tf.extractfile(member) as mf, open(out, "wb") as of:
                        of.write(mf.read())
                    out.chmod(0o755)
    elif src.suffix == ".zip":
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                base = Path(info.filename).name
                if base in names:
                    out = bin_dir / base
                    out.write_bytes(zf.read(info.filename))
                    out.chmod(0o755)
    else:
        # bare binary — assume it's tokenhub itself
        out = bin_dir / "tokenhub"
        out.write_bytes(src.read_bytes())
        out.chmod(0o755)

extract_bins(dest)

# Verify at least tokenhub was installed
if not (bin_dir / "tokenhub").exists():
    raise SystemExit(1)

print("installed", flush=True)
PY
  )" || true

  if printf '%s' "$downloaded" | grep -q "^installed"; then
    ok=1
  fi
  rm -rf "$tmp_dir"
  if [ "$ok" = "1" ]; then
    log "TokenHub binaries installed from release"
    printf '%s\n' "$downloaded" | grep -v "^installed" | grep -v "^downloading" | while IFS= read -r line; do
      [ -n "$line" ] && log "$line"
    done
    return 0
  fi
  printf '%s\n' "$downloaded" | while IFS= read -r line; do [ -n "$line" ] && log "$line"; done
  return 1
}

_install_tokenhub_from_source() {
  mkdir -p "$(dirname "$TOKENHUB_REPO")" "$TOKENHUB_BIN_DIR"
  if [ -d "$TOKENHUB_REPO/.git" ]; then
    log "updating TokenHub source at $TOKENHUB_REPO"
    git -C "$TOKENHUB_REPO" fetch --quiet --all --tags
  else
    log "cloning TokenHub source to $TOKENHUB_REPO"
    rm -rf "$TOKENHUB_REPO"
    git clone --quiet "$TOKENHUB_REPO_URL" "$TOKENHUB_REPO"
  fi
  if [ -n "${TOKENHUB_REF:-}" ]; then
    git -C "$TOKENHUB_REPO" checkout --quiet "$TOKENHUB_REF"
  else
    git -C "$TOKENHUB_REPO" checkout --quiet main 2>/dev/null || true
    git -C "$TOKENHUB_REPO" pull --ff-only --quiet origin main 2>/dev/null || true
  fi
  ensure_go
  log "building TokenHub binaries from source"
  (cd "$TOKENHUB_REPO" && env GOTOOLCHAIN=auto go build -o "$TOKENHUB_BIN_DIR/tokenhub" ./cmd/tokenhub)
  (cd "$TOKENHUB_REPO" && env GOTOOLCHAIN=auto go build -o "$TOKENHUB_BIN_DIR/tokenhubctl" ./cmd/tokenhubctl)
  chmod 755 "$TOKENHUB_BIN_DIR/tokenhub" "$TOKENHUB_BIN_DIR/tokenhubctl"
}

install_tokenhub_binaries() {
  mkdir -p "$TOKENHUB_BIN_DIR"
  local owner_repo
  owner_repo="$(_tokenhub_github_owner_repo)"
  if [ -n "$owner_repo" ] && _install_tokenhub_from_release "$owner_repo"; then
    return 0
  fi
  log "falling back to TokenHub source build"
  _install_tokenhub_from_source
}

seed_or_merge_credentials() {
  mkdir -p "$TOKENHUB_STATE_DIR" "$LOG_DIR"
  chmod 700 "$TOKENHUB_STATE_DIR"
  "$PYTHON_BIN" - "$TOKENHUB_CREDENTIALS_FILE" "$MAC_ENV_FILE" "$HERMES_ENV_FILE" "$TOKENHUB_STATE_DIR" "$LOG_DIR" <<'PY'
from __future__ import annotations

from pathlib import Path
import json
import os
import time
import urllib.parse
import sys

credentials_path = Path(sys.argv[1]).expanduser()
env_files = [Path(sys.argv[2]).expanduser(), Path(sys.argv[3]).expanduser()]
state_dir = Path(sys.argv[4]).expanduser()
log_dir = Path(sys.argv[5]).expanduser()

def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

env: dict[str, str] = dict(os.environ)
for path in env_files:
    env.update(read_env(path))

existing: dict[str, object] = {"providers": [], "models": []}
if credentials_path.exists():
    try:
        loaded = json.loads(credentials_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    except json.JSONDecodeError:
        backup = credentials_path.with_suffix(credentials_path.suffix + ".invalid-%s" % int(time.time()))
        credentials_path.rename(backup)

providers = existing.get("providers")
if not isinstance(providers, list):
    providers = []
models = existing.get("models")
if not isinstance(models, list):
    models = []

by_id: dict[str, dict[str, object]] = {
    str(item.get("id")): dict(item)
    for item in providers
    if isinstance(item, dict) and item.get("id")
}

def clean_base(raw: str, default: str) -> str:
    value = (raw or default).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.netloc:
        return value
    return default.rstrip("/")

def maybe_provider(provider_id: str, provider_type: str, api_key_name: str, base_names: tuple[str, ...], default_base: str) -> None:
    api_key = (env.get(api_key_name) or "").strip()
    if not api_key:
        return
    if api_key == (env.get("TOKENHUB_API_KEY") or "").strip():
        return
    if api_key == (env.get("TOKENHUB_AGENT_KEY") or "").strip():
        return
    base = ""
    for name in base_names:
        base = (env.get(name) or "").strip()
        if base:
            break
    record = {
        "id": provider_id,
        "type": provider_type,
        "base_url": clean_base(base, default_base),
        "api_key": api_key,
        "autoload_models": True,
        "enabled": True,
    }
    if provider_id in by_id:
        merged = dict(by_id[provider_id])
        merged.update({k: v for k, v in record.items() if k != "api_key" or v})
        by_id[provider_id] = merged
    else:
        by_id[provider_id] = record

maybe_provider(
    "nvidia",
    "openai",
    "NVIDIA_API_KEY",
    ("NVIDIA_API_BASE", "NVIDIA_BASE_URL"),
    "https://integrate.api.nvidia.com/v1",
)
maybe_provider(
    "openai",
    "openai",
    "OPENAI_API_KEY",
    ("OPENAI_BASE_URL",),
    "https://api.openai.com/v1",
)
maybe_provider(
    "anthropic",
    "anthropic",
    "ANTHROPIC_API_KEY",
    ("ANTHROPIC_BASE_URL",),
    "https://api.anthropic.com",
)
maybe_provider(
    "perplexity",
    "openai",
    "PERPLEXITY_API_KEY",
    ("PERPLEXITY_BASE_URL", "PERPLEXITY_API_BASE"),
    "https://api.perplexity.ai",
)

ordered = [by_id[key] for key in sorted(by_id)]
credentials_path.parent.mkdir(parents=True, exist_ok=True)
credentials_path.write_text(
    json.dumps({"providers": ordered, "models": models}, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
credentials_path.chmod(0o600)

report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "credentials_file": str(credentials_path),
    "provider_ids": sorted(by_id),
    "provider_count": len(by_id),
    "models_count": len(models),
    "api_key_values_in_report": False,
}
log_dir.mkdir(parents=True, exist_ok=True)
(log_dir / "tokenhub-migration.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print("providers=%d ids=%s" % (len(by_id), ",".join(sorted(by_id)) or "none"))
PY
}

write_service_env() {
  mkdir -p "$TOKENHUB_STATE_DIR" "$TOKENHUB_BIN_DIR" "$MAC_HOME/bin"
  chmod 700 "$TOKENHUB_STATE_DIR"
  if [ -z "${TOKENHUB_VAULT_PASSWORD:-}" ]; then
    if [ -f "$TOKENHUB_STATE_DIR/service.env" ] && grep -q '^TOKENHUB_VAULT_PASSWORD=' "$TOKENHUB_STATE_DIR/service.env"; then
      TOKENHUB_VAULT_PASSWORD="$(read_env_value "$TOKENHUB_STATE_DIR/service.env" TOKENHUB_VAULT_PASSWORD || true)"
    else
      TOKENHUB_VAULT_PASSWORD="$(random_secret)"
    fi
  fi
  if [ -z "${TOKENHUB_ADMIN_TOKEN:-}" ]; then
    if [ -f "$TOKENHUB_STATE_DIR/env" ] && grep -q '^TOKENHUB_ADMIN_TOKEN=' "$TOKENHUB_STATE_DIR/env"; then
      TOKENHUB_ADMIN_TOKEN="$(read_env_value "$TOKENHUB_STATE_DIR/env" TOKENHUB_ADMIN_TOKEN || true)"
    elif [ -f "$TOKENHUB_STATE_DIR/.admin-token" ]; then
      TOKENHUB_ADMIN_TOKEN="$(tr -d '\r\n' < "$TOKENHUB_STATE_DIR/.admin-token")"
    else
      TOKENHUB_ADMIN_TOKEN="$(random_secret)"
    fi
  fi

  umask 077
  {
    printf 'TOKENHUB_LISTEN_ADDR=%s:%s\n' "$TOKENHUB_BIND_ADDR" "$TOKENHUB_PORT"
    printf 'TOKENHUB_DB_DSN=%s\n' "$TOKENHUB_STATE_DIR/tokenhub.db"
    printf 'TOKENHUB_CREDENTIALS_FILE=%s\n' "$TOKENHUB_CREDENTIALS_FILE"
    printf 'TOKENHUB_ADMIN_TOKEN=%s\n' "$TOKENHUB_ADMIN_TOKEN"
    printf 'TOKENHUB_VAULT_ENABLED=%s\n' "$TOKENHUB_VAULT_ENABLED"
    printf 'TOKENHUB_VAULT_PASSWORD=%s\n' "$TOKENHUB_VAULT_PASSWORD"
    printf 'TOKENHUB_CORS_ORIGINS=%s\n' "${TOKENHUB_CORS_ORIGINS:-*}"
  } > "$TOKENHUB_STATE_DIR/service.env"
  chmod 600 "$TOKENHUB_STATE_DIR/service.env"

  cat > "$MAC_HOME/bin/tokenhub-run" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
[ -f "$TOKENHUB_STATE_DIR/service.env" ] && . "$TOKENHUB_STATE_DIR/service.env"
[ -f "$TOKENHUB_STATE_DIR/env" ] && . "$TOKENHUB_STATE_DIR/env"
set +a
export PATH="$TOKENHUB_BIN_DIR:/usr/local/go/bin:\$PATH"
exec "$TOKENHUB_BIN_DIR/tokenhub"
EOF
  chmod 700 "$MAC_HOME/bin/tokenhub-run"
}

install_service() {
  SUPERVISOR_KIND="$(detect_supervisor)"
  log "installing TokenHub under $SUPERVISOR_KIND"
  case "$SUPERVISOR_KIND" in
    systemd)
      sudo tee "/etc/systemd/system/${SERVICE_NAME}" >/dev/null <<EOF
[Unit]
Description=mac TokenHub secret and model routing authority
After=network-online.target
Wants=network-online.target
Before=${FLEET_NAME}.service ${FLEET_NAME}-hermes-gateway.service ${FLEET_NAME}-agent.service

[Service]
Type=simple
User=${USER}
WorkingDirectory=${TOKENHUB_STATE_DIR}
ExecStart=${MAC_HOME}/bin/tokenhub-run
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
      sudo systemctl daemon-reload
      sudo systemctl enable "${SERVICE_NAME}" >/dev/null
      sudo systemctl restart "${SERVICE_NAME}"
      ;;
    systemd-user)
      mkdir -p "$HOME/.config/systemd/user"
      cat > "$HOME/.config/systemd/user/${USER_SERVICE_NAME}" <<EOF
[Unit]
Description=mac TokenHub secret and model routing authority
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=HOME=%h
WorkingDirectory=${TOKENHUB_STATE_DIR}
ExecStart=${MAC_HOME}/bin/tokenhub-run
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitNOFILE=65536

[Install]
WantedBy=default.target
EOF
      systemctl --user daemon-reload
      systemctl --user enable "${USER_SERVICE_NAME}" >/dev/null
      systemctl --user restart "${USER_SERVICE_NAME}"
      ;;
    supervisord)
      conf_dir="$(supervisord_conf_dir)"
      sudo install -d -m 0755 "$conf_dir"
      sudo tee "$conf_dir/${PROGRAM_NAME}.conf" >/dev/null <<EOF
[program:${PROGRAM_NAME}]
command=${MAC_HOME}/bin/tokenhub-run
directory=${TOKENHUB_STATE_DIR}
user=${USER}
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=20
stdout_logfile=${LOG_DIR}/tokenhub.log
stderr_logfile=${LOG_DIR}/tokenhub.log
environment=HOME="${HOME}"
EOF
      run_supervisorctl reread >/dev/null
      run_supervisorctl update >/dev/null
      run_supervisorctl restart "${PROGRAM_NAME}" >/dev/null 2>&1 || run_supervisorctl start "${PROGRAM_NAME}" >/dev/null
      ;;
    launchd)
      plist="$HOME/Library/LaunchAgents/com.${FLEET_NAME}.tokenhub.plist"
      mkdir -p "$HOME/Library/LaunchAgents"
      cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.${FLEET_NAME}.tokenhub</string>
  <key>ProgramArguments</key>
  <array><string>${MAC_HOME}/bin/tokenhub-run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>${TOKENHUB_STATE_DIR}</string>
  <key>StandardOutPath</key><string>${LOG_DIR}/tokenhub.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/tokenhub.log</string>
</dict>
</plist>
EOF
      if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$plist"
      fi
      uid="$(id -u)"
      launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
      launchctl bootout "gui/$uid/com.${FLEET_NAME}.tokenhub" >/dev/null 2>&1 || true
      launchctl enable "gui/$uid/com.${FLEET_NAME}.tokenhub"
      if ! launchctl bootstrap "gui/$uid" "$plist"; then
        launchctl kickstart -k "gui/$uid/com.${FLEET_NAME}.tokenhub"
      fi
      ;;
  esac
}

wait_for_tokenhub() {
  local health_urls=("http://127.0.0.1:${TOKENHUB_PORT}/healthz")
  local health_url
  if [ -n "${TOKENHUB_BIND_ADDR:-}" ]; then
    health_urls+=("http://${TOKENHUB_BIND_ADDR}:${TOKENHUB_PORT}/healthz")
  fi
  health_urls+=("${TOKENHUB_URL%/}/healthz")
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    for health_url in "${health_urls[@]}"; do
      if curl -fsS --connect-timeout 2 --max-time 5 "$health_url" >/dev/null 2>&1; then
        log "TokenHub ready at $health_url"
        return
      fi
    done
    sleep 2
  done
  echo "[tokenhub] ERROR: TokenHub did not become ready at ${health_urls[*]}" >&2
  exit 1
}

tokenhub_admin_base_url() {
  if curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:${TOKENHUB_PORT}/healthz" >/dev/null 2>&1; then
    printf 'http://127.0.0.1:%s\n' "$TOKENHUB_PORT"
  else
    printf '%s\n' "${TOKENHUB_URL%/}"
  fi
}

configure_aliases() {
  local admin_token api_key models_json selected_models admin_base_url
  admin_token="$(read_env_value "$TOKENHUB_STATE_DIR/env" TOKENHUB_ADMIN_TOKEN || read_env_value "$TOKENHUB_STATE_DIR/service.env" TOKENHUB_ADMIN_TOKEN || true)"
  api_key="$(read_env_value "$TOKENHUB_STATE_DIR/env" TOKENHUB_API_KEY || true)"
  [ -n "$admin_token" ] || return 0
  admin_base_url="$(tokenhub_admin_base_url)"
  models_json="$(curl -fsS --connect-timeout 2 --max-time 10 -H "Authorization: Bearer ${api_key:-$admin_token}" "${admin_base_url%/}/v1/models" || true)"
  selected_models="$("$PYTHON_BIN" - "$models_json" <<'PY'
import json
import sys
raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    data = json.loads(raw)
except Exception:
    print("")
    raise SystemExit(0)
models = data.get("data") if isinstance(data, dict) else []
if not isinstance(models, list):
    models = []
preferred = [
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-5",
    "claude-sonnet-4-5",
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "meta/llama-3.1-70b-instruct",
]
ids = [str(item.get("id") or "") for item in models if isinstance(item, dict)]
selected = []
for pref in preferred:
    for model_id in ids:
        if model_id and (model_id == pref or model_id.endswith("/" + pref)) and model_id not in selected:
            selected.append(model_id)
        if len(selected) >= 3:
            print(json.dumps(selected))
            raise SystemExit(0)
for model_id in ids:
    if model_id and model_id not in selected:
        selected.append(model_id)
    if len(selected) >= 3:
        break
print(json.dumps(selected))
PY
)"
  [ -n "$selected_models" ] && [ "$selected_models" != "[]" ] || return 0
  "$PYTHON_BIN" - "$admin_token" "$selected_models" "$admin_base_url" <<'PY'
import json
import sys
import urllib.request

admin_token, selected_models_raw, base_url = sys.argv[1], sys.argv[2], sys.argv[3].rstrip("/")
selected_models = [str(item) for item in json.loads(selected_models_raw) if str(item)]
aliases = [
    "*",
    "mac-chat",
    "mac-embedding",
    "mac-search",
    "acc-chat",
    "acc-embedding",
    "acc-search",
]
body = json.dumps(
    {
        "enabled": True,
        "sticky_by": "round_robin" if len(selected_models) > 1 else "request",
        "variants": [
            {"model_id": model_id, "weight": max(1, 100 - index)}
            for index, model_id in enumerate(selected_models)
        ],
    }
).encode("utf-8")
for alias in aliases:
    req = urllib.request.Request(
        "%s/admin/v1/aliases/%s" % (base_url, alias),
        data=body,
        method="PUT",
        headers={
            "Authorization": "Bearer " + admin_token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
    except Exception:
        pass
PY
}

write_client_env() {
  local api_key tokenhub_v1
  if [ -f "$TOKENHUB_STATE_DIR/.host-api-key" ]; then
    api_key="$(tr -d '\r\n' < "$TOKENHUB_STATE_DIR/.host-api-key")"
  fi
  if [ -z "$api_key" ]; then
    api_key="$(read_env_value "$TOKENHUB_STATE_DIR/env" TOKENHUB_API_KEY || true)"
  fi
  [ -n "$api_key" ] || {
    echo "[tokenhub] ERROR: TokenHub did not provision a host API key" >&2
    exit 1
  }
  tokenhub_v1="${TOKENHUB_URL%/}/v1"
  for env_file in "$MAC_ENV_FILE" "$HERMES_ENV_FILE"; do
    mkdir -p "$(dirname "$env_file")"
    touch "$env_file"
    chmod 600 "$env_file"
    delete_env_keys "$env_file" \
      NVIDIA_API_KEY NVIDIA_API_BASE NVIDIA_BASE_URL \
      ANTHROPIC_API_KEY ANTHROPIC_BASE_URL \
      PERPLEXITY_API_KEY PERPLEXITY_BASE_URL PERPLEXITY_API_BASE \
      LLM_KEY LLM_URL
    set_env_key "$env_file" TOKENHUB_URL "$TOKENHUB_URL"
    set_env_key "$env_file" TOKENHUB_API_KEY "$api_key"
    set_env_key "$env_file" OPENAI_API_KEY "$api_key"
    set_env_key "$env_file" MAC_HERMES_GATEWAY_API_KEY "$api_key"
    set_env_key "$env_file" ACC_HERMES_GATEWAY_API_KEY "$api_key"
    set_env_key "$env_file" OPENAI_BASE_URL "$tokenhub_v1"
    set_env_key "$env_file" CUSTOM_BASE_URL "$tokenhub_v1"
    set_env_key "$env_file" MAC_HERMES_GATEWAY_BASE_URL "$tokenhub_v1"
    set_env_key "$env_file" ACC_HERMES_GATEWAY_BASE_URL "$tokenhub_v1"
    set_env_key "$env_file" MAC_HERMES_GATEWAY_PROVIDER "custom"
    set_env_key "$env_file" ACC_HERMES_GATEWAY_PROVIDER "custom"
    set_env_key "$env_file" HERMES_INFERENCE_PROVIDER "custom"
    set_env_key "$env_file" MAC_HERMES_GATEWAY_MODEL "${MAC_HERMES_GATEWAY_MODEL:-*}"
    set_env_key "$env_file" ACC_HERMES_GATEWAY_MODEL "${ACC_HERMES_GATEWAY_MODEL:-*}"
    set_env_key "$env_file" HERMES_INFERENCE_MODEL "${HERMES_INFERENCE_MODEL:-*}"
    set_env_key "$env_file" ACC_LLM_MODEL "${ACC_LLM_MODEL:-*}"
    set_env_key "$env_file" MAC_REQUIRE_TOKENHUB "1"
  done
}

verify_no_direct_provider_env() {
  "$PYTHON_BIN" - "$MAC_ENV_FILE" "$HERMES_ENV_FILE" <<'PY'
from pathlib import Path
import sys

blocked = {
    "NVIDIA_API_KEY",
    "ANTHROPIC_API_KEY",
    "PERPLEXITY_API_KEY",
    "LLM_KEY",
    "LLM_URL",
}
violations: list[str] = []
for raw in sys.argv[1:]:
    path = Path(raw).expanduser()
    if not path.exists():
        continue
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in blocked:
            violations.append("%s:%s" % (path, key))
if violations:
    print("direct provider secrets remain in runtime env: %s" % ", ".join(violations))
    raise SystemExit(1)
PY
}

mkdir -p "$MAC_HOME" "$HERMES_HOME" "$LOG_DIR"
log "TokenHub URL: ${TOKENHUB_URL}"
seed_or_merge_credentials
install_tokenhub_binaries
write_service_env
install_service
wait_for_tokenhub
configure_aliases
write_client_env
verify_no_direct_provider_env
log "TokenHub client env updated; provider secrets remain behind TokenHub"
