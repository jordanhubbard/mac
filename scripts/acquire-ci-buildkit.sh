#!/usr/bin/env bash
# Resolve and preload the same immutable BuildKit image through either registry.
set -euo pipefail

digest=sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea
for repository in public.ecr.aws/vend/moby/buildkit docker.io/moby/buildkit; do
    image="$repository@$digest"
    if docker image inspect "$image" >/dev/null 2>&1 || timeout 120s docker pull "$image"; then
        printf 'image=%s\n' "$image" >> "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
        exit 0
    fi
    printf 'Could not acquire pinned BuildKit from %s\n' "$repository" >&2
done
printf 'Neither registry supplied BuildKit at %s\n' "$digest" >&2
exit 1
