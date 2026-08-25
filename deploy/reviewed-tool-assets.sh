#!/usr/bin/env bash
# Reviewed native tool assets shared by fleet bootstrap and image preparation.
#
# This file is sourced. Keep it side-effect free: callers may already hold
# deploy credentials in their environment. Only checksum-verified native
# release archives are ever extracted or executed.

MAC_REVIEWED_UV_VERSION="0.8.22"
MAC_REVIEWED_PYTHON_VERSION="3.12.11"

mac_reviewed_platform() {
  local raw_os="${1:-$(uname -s)}" raw_arch="${2:-$(uname -m)}" os="" arch=""
  case "$raw_os" in
    Linux|linux) os="linux" ;;
    Darwin|darwin|macOS|macos) os="darwin" ;;
    *)
      echo "ERROR: unsupported reviewed-tool operating system: $raw_os" >&2
      return 2
      ;;
  esac
  case "$raw_arch" in
    x86_64|amd64|x64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *)
      echo "ERROR: unsupported reviewed-tool architecture: $raw_arch" >&2
      return 2
      ;;
  esac
  printf '%s %s\n' "$os" "$arch"
}

# Print: filename sha256 URL top-level archive directory.
mac_reviewed_asset_spec() {
  local tool="${1:?tool is required}" raw_os="${2:-$(uname -s)}"
  local raw_arch="${3:-$(uname -m)}" os="" arch="" filename="" sha256=""
  local root="" url="" platform=""
  platform="$(mac_reviewed_platform "$raw_os" "$raw_arch")" || return $?
  read -r os arch <<< "$platform"
  case "$tool:$os:$arch" in
    uv:linux:amd64)
      filename="uv-x86_64-unknown-linux-gnu.tar.gz"
      sha256="741ff1f5742c5a4a25d2f829e8395355e43f7a5ae2ebc6368e9ae2df0efb69cf"
      ;;
    uv:linux:arm64)
      filename="uv-aarch64-unknown-linux-gnu.tar.gz"
      sha256="726b72a137fda33565143325f7d31c42cd30ff9ccdf067e00d124d37b4081cb2"
      ;;
    uv:darwin:amd64)
      filename="uv-x86_64-apple-darwin.tar.gz"
      sha256="76638fdcfa91357858771551a1c88de1f7c3b270b33ab1866f8a0618d9e442d8"
      ;;
    uv:darwin:arm64)
      filename="uv-aarch64-apple-darwin.tar.gz"
      sha256="3f61099e261e449527141dbf125629fab33ad696468c8c90cebbac40185a306c"
      ;;
    *)
      echo "ERROR: unsupported reviewed tool/platform: $tool $os/$arch" >&2
      return 2
      ;;
  esac
  root="${filename%.tar.gz}"
  case "$tool" in
    uv)
      url="https://github.com/astral-sh/uv/releases/download/${MAC_REVIEWED_UV_VERSION}/${filename}"
      ;;
  esac
  printf '%s %s %s %s\n' "$filename" "$sha256" "$url" "$root"
}

mac_reviewed_sha256() {
  local path="${1:?path is required}"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
  else
    echo "ERROR: sha256sum or shasum is required for reviewed tool assets" >&2
    return 1
  fi
}

mac_verify_reviewed_asset() {
  local path="${1:?path is required}" expected="${2:?expected SHA-256 is required}"
  local observed=""
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || {
    echo "ERROR: invalid reviewed SHA-256 for $(basename "$path")" >&2
    return 2
  }
  observed="$(mac_reviewed_sha256 "$path")" || return $?
  [ "$observed" = "$expected" ] || {
    echo "ERROR: SHA-256 mismatch for reviewed asset $(basename "$path")" >&2
    return 1
  }
}

mac_download_reviewed_asset() {
  local tool="${1:?tool is required}" destination="${2:?destination is required}"
  local raw_os="${3:-$(uname -s)}" raw_arch="${4:-$(uname -m)}"
  local filename="" expected="" url="" root="" temporary="" spec=""
  spec="$(mac_reviewed_asset_spec "$tool" "$raw_os" "$raw_arch")" || return $?
  read -r filename expected url root <<< "$spec"
  mkdir -p "$(dirname "$destination")"
  if [ -f "$destination" ] \
      && mac_verify_reviewed_asset "$destination" "$expected" >/dev/null 2>&1; then
    return 0
  fi
  rm -f "$destination"
  local curl_bin=""
  curl_bin="$(command -v curl 2>/dev/null || true)"
  [ -n "$curl_bin" ] || {
    echo "ERROR: curl is required to download reviewed tool assets" >&2
    return 1
  }
  temporary="${destination}.tmp.$$"
  rm -f "$temporary"
  # -q must be curl's first option: do not import user .curlrc behavior into a
  # credential-bearing deploy process.
  # Start curl with a fresh environment so hub, GitHub, provider, and worker
  # credentials loaded by the deploy wrapper cannot enter the downloader's
  # process environment. System trust roots remain available without env vars.
  if ! env -i PATH="${PATH:-/usr/bin:/bin}" HOME="${HOME:-/}" \
      TMPDIR="${TMPDIR:-/tmp}" \
      HTTPS_PROXY="${HTTPS_PROXY:-}" HTTP_PROXY="${HTTP_PROXY:-}" \
      NO_PROXY="${NO_PROXY:-}" \
      "$curl_bin" -q --retry 5 --retry-all-errors --retry-delay 2 \
      --connect-timeout 15 --max-time 300 -fsSL -o "$temporary" "$url"; then
    rm -f "$temporary"
    echo "ERROR: reviewed asset download failed: $filename" >&2
    return 1
  fi
  chmod 0600 "$temporary"
  if ! mac_verify_reviewed_asset "$temporary" "$expected"; then
    rm -f "$temporary"
    return 1
  fi
  mv -f "$temporary" "$destination"
}

mac_install_reviewed_uv() (
  set -euo pipefail
  local target="${1:?uv target is required}" cache_root="${2:?cache root is required}"
  local filename="" expected="" url="" root="" archive="" stage="" candidate="" spec=""
  spec="$(mac_reviewed_asset_spec uv)"
  read -r filename expected url root <<< "$spec"
  archive="$cache_root/$filename"
  mac_download_reviewed_asset uv "$archive"
  stage="$(mktemp -d "${TMPDIR:-/tmp}/mac-reviewed-uv.XXXXXX")"
  trap 'rm -rf "$stage"' EXIT HUP INT TERM
  tar -xzf "$archive" -C "$stage"
  candidate="$stage/$root/uv"
  [ -x "$candidate" ] || {
    echo "ERROR: reviewed uv archive has no executable uv binary" >&2
    exit 1
  }
  case "$($candidate --version)" in
    "uv $MAC_REVIEWED_UV_VERSION"|"uv $MAC_REVIEWED_UV_VERSION "*) ;;
    *) echo "ERROR: reviewed uv binary reports an unexpected version" >&2; exit 1 ;;
  esac
  mkdir -p "$(dirname "$target")"
  install -m 0755 "$candidate" "${target}.tmp.$$"
  mv -f "${target}.tmp.$$" "$target"
)
