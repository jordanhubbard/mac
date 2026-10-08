#!/usr/bin/env bash
# Repository-reviewed OpenShell CLI release assets.  This file is the single
# source of truth for both normal bootstrap and the pre-phase-1 upgrade bridge.
# Archive digests come from the v0.1.2 release's openshell-checksums-sha256.txt
# and openshell-gateway-checksums-sha256.txt; the second digest of each entry is
# the extracted binary.

OPENSHELL_REVIEWED_CLI_VERSION="0.1.2"
OPENSHELL_REVIEWED_CLI_BASE_URL="https://github.com/NVIDIA/OpenShell/releases/download/v${OPENSHELL_REVIEWED_CLI_VERSION}"

reviewed_openshell_cli_asset() {
  local os_kind="$1" arch="$2"
  case "${os_kind}:${arch}" in
    darwin:arm64|darwin:aarch64)
      printf '%s|%s|%s\n' \
        'openshell-aarch64-apple-darwin.tar.gz' \
        'cdde7e92bd7eac664031cf171cfe80d29e7f122a6674917b25a4ce0bcbc33466' \
        '789093ba9278271f2617642cadfabe58d3c08f9f8a5a11c29dcbd4ab60d6611f'
      ;;
    linux:x86_64|linux:amd64)
      printf '%s|%s|%s\n' \
        'openshell-x86_64-unknown-linux-musl.tar.gz' \
        '7eb6917285331a09e3300266a0558616481a5e9927cae2612ea07c4045b6dd6f' \
        'f334da80f867776dde9034e0ca3c2406d48ba77b0d0abe64b5aa610490a34fca'
      ;;
    linux:aarch64|linux:arm64)
      printf '%s|%s|%s\n' \
        'openshell-aarch64-unknown-linux-musl.tar.gz' \
        '9880c5776688231d5242deb046cdee361734f94901b9123949a0baf29fdadd9e' \
        'aa6892a5d0f9c0f0b233a007ab907b72f7c1c1d3053f85a86d7665bd886f95df'
      ;;
    *)
      return 1
      ;;
  esac
}

reviewed_openshell_cli_identity_specs() {
  reviewed_openshell_cli_specs
}

reviewed_openshell_cli_specs() {
  printf '%s\n' \
    'darwin:aarch64:openshell-aarch64-apple-darwin.tar.gz:cdde7e92bd7eac664031cf171cfe80d29e7f122a6674917b25a4ce0bcbc33466:789093ba9278271f2617642cadfabe58d3c08f9f8a5a11c29dcbd4ab60d6611f' \
    'linux:x86_64:openshell-x86_64-unknown-linux-musl.tar.gz:7eb6917285331a09e3300266a0558616481a5e9927cae2612ea07c4045b6dd6f:f334da80f867776dde9034e0ca3c2406d48ba77b0d0abe64b5aa610490a34fca' \
    'linux:aarch64:openshell-aarch64-unknown-linux-musl.tar.gz:9880c5776688231d5242deb046cdee361734f94901b9123949a0baf29fdadd9e:aa6892a5d0f9c0f0b233a007ab907b72f7c1c1d3053f85a86d7665bd886f95df'
}

# The gateway and CLI share protobuf storage types and therefore form one
# compatibility unit.  Keep the exact gateway archive and extracted-binary
# identities beside the CLI identities so a pre-storage repair cannot publish
# a new CLI while leaving an older gateway behind.
reviewed_openshell_gateway_specs() {
  printf '%s\n' \
    'linux:x86_64:openshell-gateway-x86_64-unknown-linux-gnu.tar.gz:218d887845b3a020ab7535c9985eb9c666d6938f144044957f8b82b42892aadb:37c3d29f6305b42df349407ddc946b8c4755e464aaf875bdf2eb5af833b5de45' \
    'linux:aarch64:openshell-gateway-aarch64-unknown-linux-gnu.tar.gz:8ec1b6ca5b71ef5085fa51f3244d719a541e8f0d58cc569c7a0d6705b6204397:ac49a158c70fcb95bee98cc33f57eb6d36c7d2acbcc59cc144cee9e4460eb5c4'
}
