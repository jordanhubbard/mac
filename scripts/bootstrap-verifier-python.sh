#!/usr/bin/env bash
# Source this explicit bridge before the normal contract test when the
# deployment-approved OpenShell image predates the reviewed Python baseline.
# Downloads use the image-owned binaries already allowed by the original
# policy. Task-owned Python and uv execute offline after hash verification.
set -euo pipefail

verifier_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(uname -s)" != Linux ] || [ ! -r /etc/openshell-tls/ca-bundle.pem ]; then
  echo "verifier Python preparation requires a Linux OpenShell sandbox" >&2
  return 2 2>/dev/null || exit 2
fi
if [ "$(id -u)" = 0 ]; then
  echo "verifier Python preparation must run as the sandbox user" >&2
  return 2 2>/dev/null || exit 2
fi
. "$verifier_repo/deploy/reviewed-tool-assets.sh"
test "$(cat "$verifier_repo/.python-version")" = "$MAC_REVIEWED_PYTHON_VERSION"
verifier_root=/sandbox/mac-python-bootstrap
mkdir -p "$verifier_root/tools" "$verifier_root/python" "$verifier_root/wheels"
mac_install_reviewed_uv "$verifier_root/tools/uv" "$verifier_root/assets"
mac_download_reviewed_asset python "$verifier_root/assets/python.tar.gz"
tar -xzf "$verifier_root/assets/python.tar.gz" -C "$verifier_root/python"
verifier_python="$verifier_root/python/python/bin/python3"
test "$("$verifier_python" --version)" = "Python $MAC_REVIEWED_PYTHON_VERSION"
export PATH="$verifier_root/tools:$verifier_root/python/python/bin:$PATH"
export UV_CACHE_DIR="$verifier_root/cache" UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never
export UV_FIND_LINKS="$verifier_root/wheels"
cd "$verifier_repo"

# Export hashes from the existing lock, including the editable-build tools.
# pip belongs to the trusted image and selects wheels for the target Python;
# it never installs or imports those packages into the image interpreter.
uv export --frozen --extra dev --extra docs --group verifier-bootstrap \
  --no-emit-project --no-annotate > "$verifier_root/requirements.txt"
/usr/local/bin/python3 -m pip download --disable-pip-version-check \
  --no-deps --require-hashes --only-binary=:all: \
  --python-version "$MAC_REVIEWED_PYTHON_VERSION" \
  --dest "$verifier_root/wheels" -r "$verifier_root/requirements.txt"
uv venv --clear --python "$verifier_python" .venv
uv pip install --python .venv/bin/python --offline --no-index \
  --find-links "$verifier_root/wheels" --require-hashes --no-deps \
  -r "$verifier_root/requirements.txt"
uv pip install --python .venv/bin/python --offline --no-deps --no-build-isolation -e .
uv sync --locked --offline --extra dev --extra docs --python "$verifier_python"
uv pip check --python .venv/bin/python

# The full test runner receives its prepared locked environment. In particular,
# a legacy `uv run` interpreter probe must not resynchronize it during tests.
export UV_NO_SYNC=1
echo "Verifier Python $MAC_REVIEWED_PYTHON_VERSION is prepared from uv.lock"
