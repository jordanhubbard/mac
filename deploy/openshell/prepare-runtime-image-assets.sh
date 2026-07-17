#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REVIEWED_TOOL_ASSETS="$ROOT/deploy/reviewed-tool-assets.sh"
[ -r "$REVIEWED_TOOL_ASSETS" ] || {
  echo "ERROR: reviewed tool asset contract is missing: $REVIEWED_TOOL_ASSETS" >&2
  exit 1
}
# shellcheck disable=SC1090 -- repository-owned shared checksum contract.
. "$REVIEWED_TOOL_ASSETS"
OUTPUT="$ROOT/.mac-openshell-build-assets"
GH_VERSION="${GH_VERSION:-2.95.0}"
CODEGRAPH_VERSION="${CODEGRAPH_VERSION:-$MAC_REVIEWED_CODEGRAPH_VERSION}"
NODE_VERSION="${NODE_VERSION:-22.23.1}"
PNPM_VERSION="${PNPM_VERSION:-11.13.1}"
CODEX_VERSION="${CODEX_VERSION:-0.140.0}"
LEIN_COMMIT="40227328d4a9c8945362d6d626d19c2449175df6"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) OUTPUT="${2:?--output requires a directory}"; shift 2 ;;
    --output=*) OUTPUT="${1#--output=}"; shift ;;
    -h|--help)
      echo "usage: prepare-runtime-image-assets.sh [--output DIRECTORY]"
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$GH_VERSION" = "2.95.0" ] \
  && [ "$CODEGRAPH_VERSION" = "v1.1.6" ] \
  && [ "$NODE_VERSION" = "22.23.1" ] \
  && [ "$PNPM_VERSION" = "11.13.1" ] \
  && [ "$CODEX_VERSION" = "0.140.0" ] || {
    echo "ERROR: runtime tool version is unreviewed; update versions and exact hashes together" >&2
    exit 2
  }

download() {
  curl --retry 5 --retry-all-errors --retry-delay 2 \
    --connect-timeout 15 --max-time 180 -fsSL "$@"
}

digest() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

fetch() {
  local name="$1" expected="$2" url="$3"
  echo "[prepare-openshell-assets] $name"
  download -o "$TEMP/$name" "$url"
  observed="$(digest "$TEMP/$name")"
  [ "$observed" = "$expected" ] || {
    echo "ERROR: SHA-256 mismatch for $name" >&2
    exit 1
  }
  printf '%s  %s\n' "$expected" "$name" >> "$TEMP/SHA256SUMS"
}

mkdir -p "$(dirname "$OUTPUT")"
TEMP="$(mktemp -d "$(dirname "$OUTPUT")/.mac-openshell-assets.XXXXXX")"
cleanup() { rm -rf "$TEMP"; }
trap cleanup EXIT HUP INT TERM

fetch gh-amd64.tgz \
  25d1e4729e8808c9ed3d613e96ebd3f3e44446f2d368c89d878a71a36ddb3d8c \
  "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz"
fetch gh-arm64.tgz \
  d41e0b3b6218e5741c8bb4db39b16e53a59e0e06299a8489bd38f623ef7ebaae \
  "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz"
fetch node-amd64.tar.xz \
  9749e988f437343b7fa832c69ded82a312e41a03116d766797ac14f6f9eee578 \
  "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"
fetch node-arm64.tar.xz \
  0294e8b915ab75f92c7513d2fcb830ae06e10684e6c603e99a87dbf8835389c1 \
  "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz"
read -r _cg_amd64_name _cg_amd64_sha _cg_amd64_url _cg_amd64_root < <(
  mac_reviewed_asset_spec codegraph Linux x86_64
)
read -r _cg_arm64_name _cg_arm64_sha _cg_arm64_url _cg_arm64_root < <(
  mac_reviewed_asset_spec codegraph Linux aarch64
)
fetch codegraph-amd64.tgz "$_cg_amd64_sha" "$_cg_amd64_url"
fetch codegraph-arm64.tgz "$_cg_arm64_sha" "$_cg_arm64_url"
fetch lein \
  f8e1266c0c78c08bd4af6e111889ecc316c9dd56d1e8645bbee6c1703d351bc3 \
  "https://raw.githubusercontent.com/technomancy/leiningen/${LEIN_COMMIT}/bin/lein"
chmod 0755 "$TEMP/lein"
chmod 0644 "$TEMP/SHA256SUMS" "$TEMP"/*.tgz "$TEMP"/*.tar.xz

rm -rf "$OUTPUT"
mv -f "$TEMP" "$OUTPUT"
trap - EXIT HUP INT TERM
echo "[prepare-openshell-assets] verified multi-platform assets: $OUTPUT"
