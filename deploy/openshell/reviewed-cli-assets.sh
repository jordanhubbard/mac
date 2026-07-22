#!/usr/bin/env bash
# Repository-reviewed OpenShell CLI release assets.  This file is the single
# source of truth for both normal bootstrap and the pre-phase-1 upgrade bridge.

OPENSHELL_REVIEWED_CLI_VERSION="0.0.72"
OPENSHELL_REVIEWED_CLI_BASE_URL="https://github.com/NVIDIA/OpenShell/releases/download/v${OPENSHELL_REVIEWED_CLI_VERSION}"

reviewed_openshell_cli_asset() {
  local os_kind="$1" arch="$2"
  case "${os_kind}:${arch}" in
    darwin:arm64|darwin:aarch64)
      printf '%s|%s|%s\n' \
        'openshell-aarch64-apple-darwin.tar.gz' \
        '117b5354cc42d80bc4d5e070ea5ac4e341208ff6d3c29b516d8a9c80e2310f8d' \
        'a8cdaeddb19d6c7c6636a774f681886ad9c7106c1573d816afdf91d352be02c6'
      ;;
    linux:x86_64|linux:amd64)
      printf '%s|%s|%s\n' \
        'openshell-x86_64-unknown-linux-musl.tar.gz' \
        '37836c3b50383e03249c5e16512c1806e591fba8451408a84fb2f628ddb318c4' \
        'aeefd6f0f6555771d4bab7b9ddcfbc5e42e24cb7d3c717b28824a1d3b85dea71'
      ;;
    linux:aarch64|linux:arm64)
      printf '%s|%s|%s\n' \
        'openshell-aarch64-unknown-linux-musl.tar.gz' \
        'a5ff01a3240d73c72ec1700eda6cc6c752a86cf50c5dd1b5bdc459f544d03045' \
        'dee8f0606d7e4e60ec82396537aef178862ef50be81f7c04866a7bec339c18d7'
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
    'darwin:aarch64:openshell-aarch64-apple-darwin.tar.gz:117b5354cc42d80bc4d5e070ea5ac4e341208ff6d3c29b516d8a9c80e2310f8d:a8cdaeddb19d6c7c6636a774f681886ad9c7106c1573d816afdf91d352be02c6' \
    'linux:x86_64:openshell-x86_64-unknown-linux-musl.tar.gz:37836c3b50383e03249c5e16512c1806e591fba8451408a84fb2f628ddb318c4:aeefd6f0f6555771d4bab7b9ddcfbc5e42e24cb7d3c717b28824a1d3b85dea71' \
    'linux:aarch64:openshell-aarch64-unknown-linux-musl.tar.gz:a5ff01a3240d73c72ec1700eda6cc6c752a86cf50c5dd1b5bdc459f544d03045:dee8f0606d7e4e60ec82396537aef178862ef50be81f7c04866a7bec339c18d7'
}

# The gateway and CLI share protobuf storage types and therefore form one
# compatibility unit.  Keep the exact gateway archive and extracted-binary
# identities beside the CLI identities so a pre-storage repair cannot publish
# a new CLI while leaving an older gateway behind.
reviewed_openshell_gateway_specs() {
  printf '%s\n' \
    'linux:x86_64:openshell-gateway-x86_64-unknown-linux-gnu.tar.gz:03225fb9388b682af1a5f1614b26b75f828da6031e3ffc1fd920b6fbe5f70877:47e6c3a52432bbc699960228aabf5b4e368fd17678ff2410a3c96eb949e8ed69' \
    'linux:aarch64:openshell-gateway-aarch64-unknown-linux-gnu.tar.gz:a97dcb3acb04fb2d1170c1a2170228990c2337e25bb8c18817e5a6e952204108:6be41ecb95236556918cc462391e9bc996dc3dc40559211c593b90cf0b42784f'
}
