#!/usr/bin/env python3
"""Publish a built .pptx to Google Drive as a native Google Slides deck.

Decks are not committed. docs/presentation/ keeps the SVG diagram sources and
the builder; the rendered PNGs and the .pptx are reproducible from those, and
the deck itself lives in Slides where it can actually be presented. This script
is the last step of that pipeline, and it exists so the fiddly part is a command
rather than a paragraph someone has to re-derive at release time.

WHY NOT gcloud DIRECTLY. There is no `gcloud slides` or `gcloud drive`; the
Google Cloud CLI manages GCP resources and has no Google Workspace surface at
all. gcloud's role here is exactly one thing -- minting an OAuth token -- and
the Drive REST API does the upload. Setting `mimeType` to
`application/vnd.google-apps.presentation` on a .pptx upload is what makes Drive
convert it on ingest rather than storing it as an attachment.

WHY THE SCOPE CHECK IS SEPARATE. gcloud's default credential carries
cloud-platform, compute and friends but NOT Drive, so the upload fails with a
403 that says "insufficient authentication scopes" and nothing about the fix.
The token is checked first so the error names the remedy instead.

    scripts/publish-deck-to-slides.py DECK.pptx --title "..."

Prerequisite, once per machine:

    gcloud auth login --enable-gdrive-access
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SLIDES_MIME = "application/vnd.google-apps.presentation"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DRIVE_SCOPES = {
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
}


class PublishError(RuntimeError):
    """A failure with an actionable message; never a bare traceback."""


def _token() -> str:
    try:
        done = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise PublishError("gcloud is not on PATH; install the Google Cloud CLI") from exc
    except subprocess.CalledProcessError as exc:
        raise PublishError(
            "gcloud could not mint a token. Run: gcloud auth login --enable-gdrive-access\n"
            + (exc.stderr or "").strip()
        ) from exc
    token = done.stdout.strip()
    if not token:
        raise PublishError("gcloud returned an empty access token")
    return token


def _require_drive_scope(token: str) -> None:
    url = f"https://www.googleapis.com/oauth2/v1/tokeninfo?access_token={token}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        info = json.load(resp)
    if not set(info.get("scope", "").split()) & DRIVE_SCOPES:
        raise PublishError(
            "this credential has no Drive scope, so the upload would fail with HTTP 403.\n"
            "Run once, complete the browser flow, then retry:\n"
            "    gcloud auth login --enable-gdrive-access"
        )


def _upload(token: str, deck: Path, title: str) -> dict:
    metadata = {"name": title, "mimeType": SLIDES_MIME}
    boundary = f"==============={uuid.uuid4().hex}=="
    dash = f"--{boundary}".encode()
    body = b"".join(
        [
            dash,
            b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n",
            json.dumps(metadata).encode(),
            b"\r\n",
            dash,
            f"\r\nContent-Type: {PPTX_MIME}\r\n\r\n".encode(),
            deck.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files"
        "?uploadType=multipart&supportsAllDrives=true"
        "&fields=id,name,webViewLink,mimeType",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise PublishError(
            f"Drive returned HTTP {exc.code}:\n{exc.read().decode(errors='replace')[:1000]}"
        ) from exc


def _slide_count(token: str, file_id: str) -> int:
    """Export the published deck back as PDF and count its pages.

    A conversion that silently produced an attachment rather than a deck, or
    dropped every slide, is otherwise invisible until someone opens the link in
    front of an audience. Counting pages needs no dependency beyond the export.
    """
    request = urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=application/pdf",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=300) as resp:
        pdf = resp.read()
    if not pdf.startswith(b"%PDF-"):
        raise PublishError("Drive did not return a PDF; the upload may not be a deck")
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("deck", type=Path, help="path to the built .pptx")
    parser.add_argument("--title", required=True, help="title of the Slides deck")
    parser.add_argument(
        "--expect-slides",
        type=int,
        default=None,
        help="fail unless the published deck has exactly this many slides",
    )
    args = parser.parse_args()

    try:
        if not args.deck.is_file():
            raise PublishError(f"no such deck: {args.deck} (run build_deck.py first)")
        token = _token()
        _require_drive_scope(token)
        info = _upload(token, args.deck, args.title)

        if info.get("mimeType") != SLIDES_MIME:
            raise PublishError(
                "uploaded, but Drive stored it as %s rather than converting it to a deck"
                % info.get("mimeType")
            )

        slides = _slide_count(token, info["id"])
        if args.expect_slides is not None and slides != args.expect_slides:
            raise PublishError(f"published deck has {slides} slides, expected {args.expect_slides}")
    except PublishError as exc:
        print(f"publish-deck-to-slides: {exc}", file=sys.stderr)
        return 1

    print(f"slides:    {slides}")
    print(f"id:        {info['id']}")
    print(f"published: {info['webViewLink']}")
    print(
        "\nThe deck is private to the uploading account. Share it explicitly if the "
        "release notes will link it for anyone else."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
