#!/usr/bin/env bash
# Create an immutable MAC release from main and optionally roll it out.
set -euo pipefail

usage() { echo 'usage: scripts/release.sh [major|minor|patch] --docs-dir DIR [--fleet NAME]'; }
die() { echo "release: $*" >&2; exit 1; }

bump=patch; fleet=""; docs_dir=""
while (($#)); do case "$1" in
  major|minor|patch) bump="$1";;
  --docs-dir) shift; (($#)) || die '--docs-dir requires a path'; docs_dir="$1";;
  --fleet) shift; (($#)) || die '--fleet requires a name'; fleet="$1";;
  -h|--help) usage; exit 0;;
  *) die "unknown argument: $1";;
esac; shift; done

command -v gh >/dev/null || die 'gh is required'
gh auth status >/dev/null 2>&1 || die 'gh is not authenticated'
git diff --quiet && git diff --cached --quiet || die 'working tree is not clean'
[ "$(git branch --show-current)" = main ] || die 'release must start from main'
git fetch origin --tags --prune
git diff --quiet HEAD origin/main || die 'local main differs from origin/main'
current="$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' | sed 's/^v//')"
IFS=. read -r major minor patch <<<"$current"
case "$bump" in major) major=$((major+1)); minor=0; patch=0;; minor) minor=$((minor+1)); patch=0;; patch) patch=$((patch+1));; esac
version="$major.$minor.$patch"; tag="v$version"; branch="release/$tag"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "$tag already exists"

# A release must carry its own audited documentation and rebuildable deck, not
# merely point at a prior release's artifacts.  Content remains deliberately
# human-authored, but this command proves it was updated before it can tag.
[ -n "$docs_dir" ] || die '--docs-dir is required for every release'
[ -f "$docs_dir/README.md" ] || die "$docs_dir lacks README.md"
[ -f "$docs_dir/AUDIT.md" ] || die "$docs_dir lacks AUDIT.md"
[ -f "$docs_dir/build_deck.py" ] || die "$docs_dir lacks build_deck.py"
git diff --quiet "v$current"..HEAD -- "$docs_dir" && die "$docs_dir was not updated since v$current"
grep -Fq "$tag" "$docs_dir/AUDIT.md" || die "$docs_dir/AUDIT.md does not identify $tag"
python3 scripts/generate-docs-reference.py --write
python3 "$docs_dir/build_deck.py"
python3 - "$version" <<'PY'
from pathlib import Path
import re, sys
p=Path('src/mac/__init__.py'); text=p.read_text(encoding='utf-8')
text, n=re.subn(r'__version__ = "[^"]+"', f'__version__ = "{sys.argv[1]}"', text, count=1)
if n != 1: raise SystemExit('single version declaration not found')
p.write_text(text, encoding='utf-8')
PY
make lint
make test
make docs-check
git add src/mac/__init__.py docs
git commit -m "Release $tag"
git switch -c "$branch"
git push --set-upstream origin "$branch"
pr_url="$(gh pr create --base main --head "$branch" --title "Release $tag" --body "Prepare $tag from a gated main commit.")"
gh pr checks "$pr_url" --watch --fail-fast
gh pr merge "$pr_url" --squash --delete-branch
git switch main
git pull --ff-only origin main
git tag -a "$tag" -m "Release $tag"
git push origin "$tag"
gh run watch "$(gh run list --workflow release.yml --branch "$tag" --limit 1 --json databaseId -q '.[0].databaseId')" --exit-status
if [ -n "$fleet" ]; then make deploy HUB="$fleet"; fi
echo "release: $tag is published"
