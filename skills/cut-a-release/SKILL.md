---
name: cut-a-release
description: The documentation work that must land before a release is tagged — capture a capabilities deck pinned to the release commit, publish it to Google Slides, link it, and re-verify the docs gates. Use when every release gate is green and the next step is tagging a version and creating the GitHub release.
---

# Cut a release

The gates prove the code works. They prove nothing about whether the
documentation still describes it, and that is the part that rots silently: a
README sentence naming a deleted tree stays legible for weeks, and a slide
claiming a capability outlives the capability by longer. This skill is the
documentation pass that runs **after** the gates are green and **before**
anything is tagged.

Its centrepiece is a capabilities deck pinned to the exact commit being
released. Pinning is the point: a deck that is edited forever answers "what can
mac do?" with "it depends when you looked", while a deck bound to a SHA can be
checked against that SHA a year later.

**Scope.** This covers documentation and the tag/release mechanics. Fleet
cutover and image qualification are separate and are not in here — see
`docs/synchronized-fleet-cutover.md` and
`docs/image-publication-and-qualification.md`.

## 0. Do not start until the gates are actually green

Not "were green this morning". Run them, on the commit you intend to release:

```bash
make lint
make test
make docs-check
```

and confirm CI is green on `main` for that commit:

```bash
gh run list --branch main --limit 5
```

If `main` is red for an unrelated reason, say so and stop. Releasing on top of a
known-red trunk turns one person's broken test into everyone's release.

## 1. Pin the directory to the commit

```bash
git rev-parse --short=8 HEAD
date -u +%Y%m%dT%H%M%SZ
mkdir -p docs/presentation/<timestamp>-<sha>/images
```

The timestamp sorts, so `ls` is chronological, and it disambiguates two decks
cut from one commit for different audiences. The SHA is what makes a claim
checkable later. Neither alone is enough; together they cannot collide. The
convention is written up in `docs/presentation/README.md`.

Never revise a previous deck. Make a new directory.

## 2. Audit from source, never from memory

This is the step that carries the value, and the one it is tempting to skip
because you think you know what changed. Read:

```bash
# The capability surface, generated from the live parser and schema.
grep -c "^## mac" docs/reference/cli.md
grep -cE "^\| .(GET|POST|PUT|PATCH|DELETE)." docs/reference/openapi.md

# What landed since the last release, and which decisions are still open.
git log --oneline $(git describe --tags --abbrev=0)..HEAD
grep -l "Status: \*\*Proposed\*\*" docs/adr/*.md
```

Three rules that decide whether the deck is worth anything:

- **The generated references are authoritative for counts.** CI fails when they
  drift from the parser and the OpenAPI schema, so they are true by
  construction. Prose in `README.md` is not.
- **Where the README and the code disagree, follow the code, and record the
  discrepancy** in `AUDIT.md`. That is how the deck stays honest about a repo
  that is moving faster than its prose.
- **An ADR marked `Proposed` has not shipped.** Say so on the slide. A
  capabilities deck that presents proposals as capabilities is marketing, and
  the first engineer to read the code will find it.

Date every measured figure. Ledger and token-routing numbers are true for a
window, not forever.

## 3. Diagrams are SVG, and only the SVG is committed

Author or refresh `images/*.svg` by hand, then render to PNG for the deck:

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
cd docs/presentation/<timestamp>-<sha>/images
"$CHROME" --headless --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=1520,900 \
  --default-background-color=FFFFFF \
  --screenshot=01-object-model.png "file://$PWD/01-object-model.svg"
```

Render at 2× so the diagrams survive a projector. **Look at every PNG before
building the deck** — SVG text does not wrap, so an overlong line silently
overflows its box and nothing warns you.

`docs/` stays text-only: the PNGs and the `.pptx` are gitignored. See
`docs/presentation/README.md` for why that is a hard rule and not tidiness.

## 4. AUDIT.md, or the deck is unverifiable

Every figure and claim traced to a file, commit, or generated reference. Without
it, next year nobody can tell which slides are still true, and the deck becomes
folklore with a logo. It is also where the README/code discrepancies from step 2
are recorded.

## 5. Build and publish

```bash
python3 -m venv /tmp/deckvenv && /tmp/deckvenv/bin/pip install python-pptx
/tmp/deckvenv/bin/python docs/presentation/<timestamp>-<sha>/build_deck.py

scripts/publish-deck-to-slides.py \
  docs/presentation/<timestamp>-<sha>/mac-capabilities-<sha>.pptx \
  --title "MAC — <subject> (<sha>)" \
  --expect-slides <n>
```

`python-pptx` is deliberately not a repository dependency; the deck is a
documentation artifact, not part of the shipped runtime.

Pass `--expect-slides`. The script exports the published deck back out of Google
and counts its pages, so a conversion that dropped slides fails here instead of
in front of an audience.

The published deck is **private to the uploading account**. If the release notes
will link it for anyone else, share it explicitly.

## 6. Link it in all three places

A deck nobody can find was not published.

1. The deck's own `README.md` — the Slides URL, the slide list, how to rebuild.
2. `docs/presentation/README.md` — add a row to the existing-decks table.
3. The root `README.md` — the entry under `## Documentation`.

## 7. Re-verify, in this order

The order matters and is the trap that cost the most time:

```bash
git add docs/presentation <other changed files>

MAC_TEST_PG_URL=... uv run --extra dev --extra postgres pytest \
  tests/test_docs_no_operator_identity.py tests/test_guide_docs_are_true.py -q
make docs-check
```

**Stage before running.** `tests/test_docs_no_operator_identity.py` enumerates
`git ls-files`, so it only scans *tracked* files. Run it while the new directory
is untracked and it scans nothing you wrote and reports a confident pass over an
empty set.

## 8. Then, and only then, cut the release

The version is single-sourced. `pyproject.toml` declares `dynamic = ["version"]`
pointing at `mac.__version__`, and `mac.api` / `mac.a2a.card` import it, so one
line is the whole bump:

```bash
$EDITOR src/mac/__init__.py          # __version__ = "X.Y.Z"
git commit -am "Release vX.Y.Z"
```

Land it the way all work lands here — through a pull request, not a push to
`main` (`skills/mac-cli/SKILL.md`). Once it is on `main`:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
gh release create vX.Y.Z --title "mac vX.Y.Z" --notes-file notes.md
```

Write the notes from the deck's `AUDIT.md`, not from the commit log. The log
says what changed; the audit says what is now true, which is what a reader
wants, and it is already sourced.

## The traps, all of which have happened

- **`gcloud` cannot publish anything.** There is no `gcloud slides` or
  `gcloud drive` — the Cloud CLI has no Workspace surface. It mints a token; the
  Drive API does the upload. The default credential has no Drive scope, so
  `gcloud auth login --enable-gdrive-access` is a prerequisite.
- **Never commit the PNGs or the `.pptx`.** Beyond the repository weight,
  `tests/test_docs_no_operator_identity.py` greps every tracked file under `docs/` for
  fleet-identity tokens, and compressed image data matches one by coincidence
  eventually. The first PNG committed here did.
- **Do not name a forbidden identity token, even to explain one.** That gate
  does not care why the token is present, and it is right not to — a gate that
  accepts "I am only quoting it" accepts anything. Only the test file itself is
  exempt.
- **No `bash`/`sh`/`shell` fences anywhere under `docs/`** outside the
  executable book; `scripts/test-docs.py` rejects them. Use `console` for
  transcripts. (Skills like this one are outside `docs/`, so they may use
  `bash`.)
- **Do not quote a dead link.** `scripts/check-docs-accessibility.py` resolves relative
  links under `docs/`, so pasting a broken link into an audit as evidence
  reproduces the defect in a checked file. Describe the target instead.
- **A backticked repository path asserts that the path exists**
  (`tests/test_guide_docs_are_true.py`). Writing about something deliberately
  removed? Leave it unbackticked.
