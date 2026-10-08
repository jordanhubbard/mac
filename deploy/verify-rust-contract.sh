#!/usr/bin/env bash
# Verify the Rust tools actually execute through the runtime's public PATH.
set -euo pipefail
expected="${1:?expected Rust release is required}"
for tool in cargo rustc rustfmt; do
  command -v "$tool" >/dev/null || {
    echo "missing required Rust tool: $tool" >&2
    exit 1
  }
done
case "$(rustc --version)" in
  "rustc $expected "*) ;;
  *) echo "unexpected rustc release; expected $expected" >&2; exit 1 ;;
esac
case "$(cargo --version)" in
  "cargo $expected "*) ;;
  *) echo "unexpected Cargo release; expected $expected" >&2; exit 1 ;;
esac
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir "$scratch/src"
printf '[package]\nname = "mac_runtime_rust_probe"\nversion = "0.0.0"\nedition = "2024"\n' > "$scratch/Cargo.toml"
printf 'fn main(){println!("mac-rust-toolchain-ready");}\n' > "$scratch/src/main.rs"
cargo fmt --manifest-path "$scratch/Cargo.toml"
cargo fmt --manifest-path "$scratch/Cargo.toml" -- --check
test "$(cargo run --quiet --offline --manifest-path "$scratch/Cargo.toml")" = mac-rust-toolchain-ready
echo "Rust $expected: Cargo, compiler, standard library, linker and formatter passed"
