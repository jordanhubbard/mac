#!/usr/bin/env bash
# bootstrap-openshell.sh — stand up OpenShell sandbox enforcement on a mac node.
#
# Run ON the target node (it touches the local user's ~/.mac, ~/.local, Docker
# Engine/Moby, and — with sudo — the CDI spec + a firewall). Idempotent: safe to
# re-run. MAC standardizes on the OpenShell Docker driver only. Do not use
# Docker Desktop, Podman, or podman-docker for production fleet nodes; use the
# OSS Docker Engine/Moby daemon package supplied by the Linux distribution or a
# nested Docker daemon in containerized environments that support DinD.
#
# It does NOT flip enforcement by default. After it succeeds, validate a real
# task, then enable:  --enable (MAC_OPENSHELL_SANDBOX=1) and, once validated,
# --fail-closed (MAC_ALLOW_UNSANDBOXED_YOLO=0).
#
# Knobs (env):
#   OPENSHELL_VERSION   default 0.0.62        — CLI + gateway version (must match)
#   MAC_HOME            default $HOME/.mac
#   MAC_SRC             default $MAC_HOME/src/mac    — mac source tree (image build context)
#   OSH_DOCKER_BIN      default docker       — Docker Engine/Moby CLI path
#   OSH_GPU             auto|yes|no          — auto: detect nvidia-smi
#   OSH_HUB_URL         default from mac.env MAC_HUB_URL — the hub the sandbox egresses to
#   OSH_FIREWALL_NIC    default: the default-route NIC
# Flags: --enable  --fail-closed  --skip-image
set -euo pipefail

OPENSHELL_VERSION="${OPENSHELL_VERSION:-0.0.62}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_SRC="${MAC_SRC:-$MAC_HOME/src/mac}"
OSH_DOCKER_BIN="${OSH_DOCKER_BIN:-docker}"
OSH_GPU="${OSH_GPU:-auto}"
ENVF="$MAC_HOME/mac.env"
OSH_DIR="$MAC_HOME/openshell"
BIN="$HOME/.local/bin"
ARCH="$(uname -m)"   # x86_64 | aarch64
DO_ENABLE=0; DO_FAILCLOSED=0; SKIP_IMAGE=0
for a in "$@"; do case "$a" in
  --enable) DO_ENABLE=1;; --fail-closed) DO_FAILCLOSED=1; DO_ENABLE=1;; --skip-image) SKIP_IMAGE=1;;
  *) echo "unknown arg: $a" >&2; exit 2;; esac; done
log(){ printf '[bootstrap-openshell] %s\n' "$*"; }
export PATH="$BIN:$PATH" XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$OSH_DIR" "$BIN"

# --- Docker Engine/Moby + GPU detection -------------------------------------
[ "$OSH_GPU" = auto ] && { command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 && OSH_GPU=yes || OSH_GPU=no; }
if [ "${OSH_DRIVER:-docker}" != docker ]; then
  echo "OSH_DRIVER is no longer supported; MAC/OpenShell uses Docker Engine/Moby only" >&2
  exit 2
fi

install_docker_engine() {
  if command -v apt-get >/dev/null; then
    log "installing Docker Engine/Moby from distro packages (docker.io)"
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io acl
    return 0
  fi
  echo "Docker Engine/Moby is required, but '$OSH_DOCKER_BIN' is missing and this host has no supported package installer" >&2
  echo "Install an OSS Docker Engine/Moby daemon, then rerun this bootstrap." >&2
  exit 1
}

ensure_docker_engine() {
  if ! command -v "$OSH_DOCKER_BIN" >/dev/null 2>&1; then
    install_docker_engine
  fi
  docker_version="$("$OSH_DOCKER_BIN" --version 2>/dev/null || true)"
  case "$docker_version" in
    *[Pp]odman*)
      echo "'$OSH_DOCKER_BIN' resolves to a Podman compatibility shim, not Docker Engine/Moby: $docker_version" >&2
      echo "Remove podman-docker or set OSH_DOCKER_BIN to a real Docker Engine/Moby CLI." >&2
      exit 1
      ;;
  esac
  if command -v systemctl >/dev/null; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  if ! "$OSH_DOCKER_BIN" info >/dev/null 2>&1; then
    if getent group docker >/dev/null 2>&1; then
      sudo usermod -aG docker "$USER" >/dev/null 2>&1 || true
    fi
    if command -v setfacl >/dev/null && [ -S /var/run/docker.sock ]; then
      sudo setfacl -m "u:$(id -u):rw" /var/run/docker.sock >/dev/null 2>&1 || true
    fi
  fi
  if ! "$OSH_DOCKER_BIN" info >/dev/null 2>&1; then
    echo "Docker Engine/Moby is installed but this user cannot reach the daemon." >&2
    echo "Ensure docker.service is running and '$USER' can read/write /var/run/docker.sock, then rerun." >&2
    exit 1
  fi
}

ensure_docker_engine
log "arch=$ARCH gpu=$OSH_GPU driver=docker-engine version=$OPENSHELL_VERSION docker=$("$OSH_DOCKER_BIN" --version 2>&1 | head -1)"

# --- 1. openshell CLI (uv tool, else pip venv) ------------------------------
if ! command -v openshell >/dev/null; then
  if command -v uv >/dev/null; then
    uv tool install "openshell==$OPENSHELL_VERSION"
  else
    python3 -m venv "$HOME/.openshell-cli-venv"
    "$HOME/.openshell-cli-venv/bin/pip" install -q "openshell==$OPENSHELL_VERSION"
    ln -sf "$HOME/.openshell-cli-venv/bin/openshell" "$BIN/openshell"
  fi
fi
log "openshell CLI: $(openshell --version 2>&1 | head -1)"

# --- 2. openshell-gateway daemon (prebuilt per-arch release asset) ----------
if ! [ -x "$BIN/openshell-gateway" ]; then
  case "$ARCH" in
    x86_64)  ga="openshell-gateway-x86_64-unknown-linux-gnu.tar.gz";;
    aarch64) ga="openshell-gateway-aarch64-unknown-linux-gnu.tar.gz";;
    *) echo "unsupported arch $ARCH" >&2; exit 1;;
  esac
  url="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION/$ga"
  log "fetching gateway: $url"
  tmp="$(mktemp -d)"; curl -fsSL -o "$tmp/gw.tgz" "$url"; tar -xzf "$tmp/gw.tgz" -C "$tmp"
  install -m755 "$(find "$tmp" -name openshell-gateway -type f | head -1)" "$BIN/openshell-gateway"
  rm -rf "$tmp"
fi
log "gateway bin: $(openshell-gateway --version 2>&1 | head -1)"

# --- 3. GPU: refresh the CDI spec to the current driver ---------------------
if [ "$OSH_GPU" = yes ]; then
  log "regenerating CDI spec for driver $(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
  sudo mkdir -p /etc/cdi && sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null 2>&1 || true
fi

# --- 4. mac-hermes image (native build; multi-arch Containerfile) -----------
if [ "$SKIP_IMAGE" = 0 ]; then
  cf="$(cd "$(dirname "$0")" && pwd)/mac-hermes.Containerfile"
  log "building localhost/mac-hermes:net with Docker Engine/Moby from $MAC_SRC"
  ( cd "$MAC_SRC" && "$OSH_DOCKER_BIN" build -t localhost/mac-hermes:net -f "$cf" . )
fi

# --- 5. JWT signing keys ----------------------------------------------------
[ -f "$OSH_DIR/pki/jwt/signing.pem" ] || openshell-gateway generate-certs --output-dir "$OSH_DIR/pki" >/dev/null 2>&1
log "jwt keys: $(ls "$OSH_DIR/pki/jwt" 2>/dev/null | tr '\n' ' ')"

# --- 6. gateway.toml (Docker driver) ----------------------------------------
cat > "$OSH_DIR/gateway.toml" <<EOF
[openshell]
version = 1
[openshell.gateway]
bind_address = "0.0.0.0:17670"
log_level = "info"
compute_drivers = ["docker"]
disable_tls = true
[openshell.gateway.auth]
allow_unauthenticated_users = true
[openshell.gateway.gateway_jwt]
signing_key_path = "$OSH_DIR/pki/jwt/signing.pem"
public_key_path = "$OSH_DIR/pki/jwt/public.pem"
kid_path = "$OSH_DIR/pki/jwt/kid"
[openshell.drivers.docker]
default_image = "localhost/mac-hermes:net"
supervisor_image = "ghcr.io/nvidia/openshell/supervisor:latest"
network_name = "openshell-docker"
grpc_endpoint = "http://host.openshell.internal:17670"
image_pull_policy = "IfNotPresent"
EOF

# --- 7. gateway systemd --user service + register ---------------------------
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/openshell-gateway.service" <<EOF
[Unit]
Description=OpenShell gateway (Docker Engine/Moby driver)
[Service]
ExecStart=%h/.local/bin/openshell-gateway --config %h/.mac/openshell/gateway.toml
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now openshell-gateway >/dev/null 2>&1
sleep 3
openshell gateway add http://127.0.0.1:17670 >/dev/null 2>&1 || true
openshell gateway select openshell >/dev/null 2>&1 || true
log "gateway: $(systemctl --user is-active openshell-gateway) | listening: $(ss -tlnp 2>/dev/null | grep -c :17670)"

# --- 8. firewall :17670 (block public/LAN NIC; persistent) ------------------
NIC="${OSH_FIREWALL_NIC:-$(ip route show default 2>/dev/null | grep -oP 'dev \K\S+' | head -1)}"
if [ -n "$NIC" ]; then
  sudo tee /usr/local/sbin/mac-openshell-firewall.sh >/dev/null <<EOF
#!/usr/bin/env bash
for ipt in iptables ip6tables; do command -v \$ipt >/dev/null || continue
  \$ipt -C INPUT -i $NIC -p tcp --dport 17670 -j DROP 2>/dev/null || \$ipt -I INPUT 1 -i $NIC -p tcp --dport 17670 -j DROP
done
EOF
  sudo chmod +x /usr/local/sbin/mac-openshell-firewall.sh
  sudo tee /etc/systemd/system/mac-openshell-firewall.service >/dev/null <<'EOF'
[Unit]
Description=Block external access to the OpenShell gateway (:17670)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/mac-openshell-firewall.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload && sudo systemctl enable --now mac-openshell-firewall.service >/dev/null 2>&1
  log "firewall: $NIC :17670 ($(sudo iptables -S INPUT 2>/dev/null | grep -c 17670) rules)"
fi

# --- 9. policy (egress to this node's hub) ----------------------------------
HUB_URL="${OSH_HUB_URL:-$(grep -oE '^MAC_HUB_URL=[^ ]+' "$ENVF" 2>/dev/null | head -1 | cut -d= -f2-)}"
HUB_HOST="$(printf '%s' "$HUB_URL" | sed -E 's#^https?://##; s#[:/].*##')"
case "$HUB_HOST" in 127.0.0.1|localhost|0.0.0.0|"") POLICY_HUB=host.openshell.internal;; *) POLICY_HUB="$HUB_HOST";; esac
HUB_PORT="$(printf '%s' "$HUB_URL" | grep -oE ':[0-9]+' | tr -d : | head -1)"; HUB_PORT="${HUB_PORT:-8789}"
log "policy egress hub: $POLICY_HUB:$HUB_PORT"
"$MAC_HOME/venv/bin/mac" openshell render-policy \
  --template "$(cd "$(dirname "$0")" && pwd)/mac-hermes-policy.yaml" \
  --agent-user "$USER" --hub-host "$POLICY_HUB" --hub-port "$HUB_PORT" \
  --image-runtime /opt/mac-venv --into "$MAC_HOME/openshell-policy.yaml" >/dev/null
chmod 600 "$MAC_HOME/openshell-policy.yaml"

# --- 10. rewritten hermes config for the sandbox ----------------------------
# Loopback base_url -> host.openshell.internal (a remote hub IP passes through).
sed 's#127\.0\.0\.1#host.openshell.internal#g' "$HOME/.hermes/config.yaml" > "$OSH_DIR/sandbox-hermes-config.yaml"
chmod 600 "$OSH_DIR/sandbox-hermes-config.yaml"

# --- 11. env recipe in mac.env (quoted — mac.env is shell-sourced) ----------
gpuarg=""; [ "$OSH_GPU" = yes ] && gpuarg=" --gpu"
cp -a "$ENVF" "$ENVF.bak-openshell-$(date +%Y%m%dT%H%M%S 2>/dev/null || echo bootstrap)"
sed -i '/^# OpenShell sandbox enforcement/d;/^MAC_OPENSHELL_SANDBOX=/d;/^MAC_HERMES_PYTHON=/d;/^MAC_OPENSHELL_POLICY=/d;/^MAC_OPENSHELL_BIN=/d;/^MAC_OPENSHELL_CREATE_ARGS=/d;/^MAC_ALLOW_UNSANDBOXED_YOLO=/d' "$ENVF"
{
  echo ""
  echo "# OpenShell sandbox enforcement (bootstrap-openshell.sh; Docker Engine/Moby driver, gpu=$OSH_GPU)"
  echo "MAC_OPENSHELL_SANDBOX=$DO_ENABLE"
  echo "MAC_HERMES_PYTHON=/opt/mac-venv/bin/python"
  echo "MAC_OPENSHELL_POLICY=$MAC_HOME/openshell-policy.yaml"
  echo "MAC_OPENSHELL_BIN=$BIN/openshell"
  echo "MAC_OPENSHELL_CREATE_ARGS=\"--from localhost/mac-hermes:net$gpuarg --upload $OSH_DIR/sandbox-hermes-config.yaml:/tmp/.hermes/config.yaml --env HOME=/tmp\""
  [ "$DO_FAILCLOSED" = 1 ] && echo "MAC_ALLOW_UNSANDBOXED_YOLO=0"
} >> "$ENVF"
# sanity: mac.env must still source cleanly (quoting)
( set -a; . "$ENVF" >/dev/null 2>&1; ) || { echo "ERROR: mac.env failed to source after edit" >&2; exit 1; }

log "DONE. sandbox-enabled=$DO_ENABLE fail-closed=$DO_FAILCLOSED"
[ "$DO_ENABLE" = 1 ] && log "restart the agent to apply: sudo systemctl restart mac-agent.service (then validate a real task)" \
                     || log "set up but NOT enforcing yet; re-run with --enable (then --fail-closed once a real task validates)"
