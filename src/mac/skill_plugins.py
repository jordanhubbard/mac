"""One skill source, one rendering pipeline, a thin adapter per coding harness.

Implements ADR 0023. ``skills/`` stays the single source of truth; nothing in
this module authors guidance. Every adapter here is a *layout*: which path a
harness reads, and whether that path is a file mac owns outright or a delimited
block inside a file a human owns. The words come from ``skills/`` or they do
not exist.

Two kinds of content live in ``skills/`` and get different delivery:

* **reference** -- the whole skill document, copied verbatim into whatever
  on-demand surface the harness offers;
* **obligations** -- rules marked in the source with
  ``**OBLIGATION `<id>`** -- <rule>``, rendered additionally into the harness's
  always-on instruction surface, because an obligation that is only delivered
  when the session goes looking is the failure ADR 0023 exists to fix.

Three refusals are deliberate and load-bearing:

* installing never guesses a target (``global`` or an explicitly nominated
  repository, never "the tree you happen to be standing in");
* installing refuses this repository -- ``skills/`` rendered back into its own
  source is two copies that can disagree;
* rendering refuses a skill with no test, because a *published* wrong skill is
  an instruction every harness obeys.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

import yaml

from mac.coding_agent import AGENT_PRIORITY
from mac.mac_paths import mac_home
from mac.models import MACError

__all__ = [
    "HARNESSES",
    "PLUGIN_SCHEMA",
    "RECEIPT_SCHEMA",
    "Obligation",
    "Skill",
    "RenderedFile",
    "Plugin",
    "InstallReceipt",
    "SkillPluginError",
    "SourceVersion",
    "default_skills_root",
    "load_skills",
    "obligations_of",
    "source_version",
    "untested_skills",
    "render_plugin",
    "install",
    "uninstall",
    "read_receipts",
    "status",
]


PLUGIN_SCHEMA = "mac.skill_plugin.v1"
RECEIPT_SCHEMA = "mac.skill_plugin_install.v1"

#: The harnesses mac already knows. Deliberately imported rather than
#: re-declared: a second list of harnesses is how the two lists start to
#: disagree, and ``coding_agent`` is the one that routes real work.
HARNESSES: Tuple[str, ...] = AGENT_PRIORITY

#: Delimiters for the managed block mac writes into a file a human owns.
#: Everything outside them is preserved byte for byte, on install and on
#: uninstall. The revision lives INSIDE the block so the markers stay stable
#: across re-renders and an upgrade replaces rather than appends.
BLOCK_BEGIN = "<!-- BEGIN mac skill plugin -- generated from skills/, do not edit -->"
BLOCK_END = "<!-- END mac skill plugin -->"

#: Marker mac stamps into every file it owns outright, so uninstall (and a
#: refusal to clobber) can tell mac's file from a human's file of the same name.
OWNED_MARKER = "mac skill plugin"

_OBLIGATION_RE = re.compile(
    r"^\*\*OBLIGATION\s+`(?P<id>[a-z0-9][a-z0-9-]*)`\*\*\s*[—-]\s*(?P<text>.+?)(?=\n[ \t]*\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


class SkillPluginError(MACError):
    """A refusal a caller can print verbatim.

    A MACError rather than a bare RuntimeError so the CLI prints the sentence
    and exits 1: "refusing to install into your own source" is a decision mac
    made on purpose, and a traceback would present it as a crash.
    """


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


# --- Source model ----------------------------------------------------------


@dataclass(frozen=True)
class Obligation:
    """A rule that must arrive whether or not the session goes looking."""

    id: str
    text: str
    skill: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    text: str
    obligations: Tuple[Obligation, ...]


def default_skills_root() -> Path:
    """``skills/`` beside the installed source tree."""

    return Path(__file__).resolve().parents[2] / "skills"


def _parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise SkillPluginError("%s has no YAML frontmatter" % path)
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillPluginError("%s has an unterminated YAML frontmatter block" % path)
    front = yaml.safe_load(parts[1]) or {}
    name = str(front.get("name") or path.parent.name)
    description = _collapse(front.get("description") or "")
    obligations = tuple(
        Obligation(id=match.group("id"), text=_collapse(match.group("text")), skill=name)
        for match in _OBLIGATION_RE.finditer(text)
    )
    seen: Dict[str, str] = {}
    for obligation in obligations:
        if obligation.id in seen:
            raise SkillPluginError(
                "duplicate OBLIGATION id %r in %s" % (obligation.id, path)
            )
        seen[obligation.id] = obligation.text
    return Skill(
        name=name, description=description, path=path, text=text, obligations=obligations
    )


def load_skills(root: Optional[Path] = None) -> Tuple[Skill, ...]:
    """Every ``<root>/<skill>/SKILL.md``, in a stable order."""

    root = Path(root) if root is not None else default_skills_root()
    if not root.is_dir():
        raise SkillPluginError("skills root does not exist: %s" % root)
    skills = tuple(
        _parse_skill(path) for path in sorted(root.glob("*/SKILL.md"), key=lambda p: p.parent.name)
    )
    ids: Dict[str, str] = {}
    for skill in skills:
        for obligation in skill.obligations:
            if obligation.id in ids:
                raise SkillPluginError(
                    "OBLIGATION id %r is claimed by both %s and %s -- ids are "
                    "fleet-wide so a harness cannot receive two different rules "
                    "under one name" % (obligation.id, ids[obligation.id], skill.name)
                )
            ids[obligation.id] = skill.name
    return skills


def obligations_of(skills: Sequence[Skill]) -> Tuple[Obligation, ...]:
    return tuple(obligation for skill in skills for obligation in skill.obligations)


# --- Versioning ------------------------------------------------------------


@dataclass(frozen=True)
class SourceVersion:
    """What the rendered artifact records about where it came from.

    ``revision`` is the git commit ``skills/`` was read at, and is empty
    outside a checkout. ``digest`` is a content hash of the skill sources, so a
    dirty tree still renders a version distinguishable from the commit it sits
    on -- a harness carrying stale rules is worse than one carrying none, and
    "the commit said 1.2" is exactly how stale goes unnoticed.
    """

    revision: str
    digest: str

    def __str__(self) -> str:
        return "%s+%s" % (self.revision or "unversioned", self.digest)


def _git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def source_version(root: Optional[Path] = None, skills: Optional[Sequence[Skill]] = None) -> SourceVersion:
    root = Path(root) if root is not None else default_skills_root()
    skills = load_skills(root) if skills is None else skills
    digest = hashlib.sha256()
    for skill in sorted(skills, key=lambda s: s.name):
        digest.update(skill.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(skill.text.encode("utf-8"))
        digest.update(b"\0")
    return SourceVersion(revision=_git_revision(root), digest=digest.hexdigest()[:12])


# --- The publishing guard --------------------------------------------------


def untested_skills(root: Optional[Path] = None, skills: Optional[Sequence[Skill]] = None) -> Tuple[str, ...]:
    """Skills no test in ``tests/`` names.

    An unread skill that is wrong is a document nobody follows; a published one
    is an instruction every harness obeys. Coverage is discovered rather than
    listed, so a hand-maintained table cannot claim a test that was deleted.
    Returns ``()`` when there is no ``tests/`` tree to read -- the caller
    decides what to do about an unverifiable source (see ``render_plugin``).
    """

    root = Path(root) if root is not None else default_skills_root()
    skills = load_skills(root) if skills is None else skills
    tests_dir = root.parent / "tests"
    if not tests_dir.is_dir():
        return ()
    corpus = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(tests_dir.rglob("*.py"))
    )
    # A test names its skill either as a literal path ("skills/mac-cli/SKILL.md")
    # or as a composed one (ROOT / "skills" / "mac-cli"). Normalising quotes and
    # the spaces around the separator makes both forms the same string, so
    # coverage is discovered from how tests are actually written rather than
    # from one spelling of the path.
    normalized = re.sub(r"\s*/\s*", "/", corpus.replace('"', "").replace("'", ""))
    return tuple(
        skill.name
        for skill in skills
        if "skills/%s" % skill.path.parent.name not in normalized
    )


def _tests_are_readable(root: Path) -> bool:
    return (root.parent / "tests").is_dir()


# --- Rendering -------------------------------------------------------------


@dataclass(frozen=True)
class RenderedFile:
    """One artifact, at a path relative to the install target root.

    ``mode`` is ``"file"`` when mac owns the whole file, and ``"block"`` when
    the content is a delimited block inside a file a human owns.
    """

    path: str
    content: str
    mode: str


@dataclass(frozen=True)
class Plugin:
    harness: str
    scope: str
    version: SourceVersion
    files: Tuple[RenderedFile, ...]
    skills: Tuple[str, ...]
    obligations: Tuple[str, ...]


@dataclass(frozen=True)
class _Layout:
    """Where one harness reads, for each install scope.

    ``home`` is the harness's own configuration directory under ``$HOME`` and
    matches what ``coding_agent`` already probes for credentials. ``repo_dir``
    is the same harness's per-repository directory. ``instruction`` is the
    always-on surface: for the harnesses that read an agents file it is that
    file at the target root, and for cursor it is a dedicated always-apply rule.
    """

    home: str
    repo_dir: str
    instruction_global: str
    instruction_repo: str
    instruction_mode: str
    reference: str
    manifest: str


_LAYOUTS: Dict[str, _Layout] = {
    "claude": _Layout(
        home=".claude",
        repo_dir=".claude",
        instruction_global="CLAUDE.md",
        instruction_repo="CLAUDE.md",
        instruction_mode="block",
        reference="skills/{namespaced}/SKILL.md",
        manifest="plugins/mac-skills/plugin.json",
    ),
    "codex": _Layout(
        home=".codex",
        repo_dir=".codex",
        instruction_global="AGENTS.md",
        instruction_repo="AGENTS.md",
        instruction_mode="block",
        reference="skills/{namespaced}/SKILL.md",
        manifest="skills/mac-skills.json",
    ),
    "cursor": _Layout(
        home=".cursor",
        repo_dir=".cursor",
        instruction_global="rules/mac-fleet-obligations.mdc",
        instruction_repo="rules/mac-fleet-obligations.mdc",
        instruction_mode="file",
        reference="rules/mac-skill-{skill}.mdc",
        manifest="mac-skills/manifest.json",
    ),
    "opencode": _Layout(
        home=".config/opencode",
        repo_dir=".opencode",
        instruction_global="AGENTS.md",
        instruction_repo="AGENTS.md",
        instruction_mode="block",
        reference="skills/{namespaced}/SKILL.md",
        manifest="mac-skills.json",
    ),
    "pi": _Layout(
        home=".pi/agent",
        repo_dir=".pi",
        instruction_global="AGENTS.md",
        instruction_repo="AGENTS.md",
        instruction_mode="block",
        reference="skills/{namespaced}/SKILL.md",
        manifest="mac-skills.json",
    ),
}


def _namespaced(name: str) -> str:
    """``mac-`` prefixed, without doubling a name that already carries it.

    The prefix keeps mac's copies inside their own namespace so a human skill
    of the same name is never overwritten; the strip stops ``mac-cli`` from
    landing as ``mac-mac-cli``.
    """

    return "mac-" + name[4:] if name.startswith("mac-") else "mac-" + name


def _provenance(version: SourceVersion) -> str:
    # Opens with OWNED_MARKER so a later install can tell mac's file from a
    # human's file of the same name, and uninstall removes only its own.
    return (
        "mac skill plugin: rendered from skills/ at source revision %s. Do not "
        "edit here -- edit skills/ and re-render, or the copies disagree." % version
    )


def _provenance_comment(version: SourceVersion) -> str:
    """The provenance line, identically shaped in every artifact.

    One shape rather than two so a reader (and the test that traces rendered
    content back to skills/) sees the same line everywhere mac writes.
    """

    return "<!-- %s -->" % _provenance(version)


def _obligation_body(skills: Sequence[Skill], version: SourceVersion) -> str:
    """The always-on payload. Every word after the header comes from skills/."""

    lines = ["## MAC fleet obligations", "", _provenance_comment(version), ""]
    for skill in skills:
        if not skill.obligations:
            continue
        lines.append("### %s" % skill.name)
        lines.append("")
        for obligation in skill.obligations:
            lines.append("- **%s** %s" % (obligation.id, obligation.text))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _reference_body(skill: Skill, version: SourceVersion) -> str:
    return "%s\n%s" % (_provenance_comment(version), skill.text)


def _cursor_rule(body: str, description: str, *, always: bool) -> str:
    front = [
        "---",
        "description: %s" % description,
        "alwaysApply: %s" % ("true" if always else "false"),
        "---",
        "",
    ]
    return "\n".join(front) + body


def _manifest(harness: str, scope: str, skills: Sequence[Skill], version: SourceVersion) -> str:
    payload = {
        "schema": PLUGIN_SCHEMA,
        # Literally OWNED_MARKER: the manifest is a file mac owns outright, and
        # this is how a later install or uninstall knows that.
        "generated_by": OWNED_MARKER,
        "harness": harness,
        "scope": scope,
        "revision": version.revision,
        "digest": version.digest,
        "version": str(version),
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "obligations": [obligation.id for obligation in skill.obligations],
            }
            for skill in skills
        ],
        "obligations": [obligation.id for obligation in obligations_of(skills)],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_plugin(
    harness: str,
    *,
    scope: str = "global",
    skills_root: Optional[Path] = None,
    skills: Optional[Sequence[Skill]] = None,
    version: Optional[SourceVersion] = None,
    allow_unverified: bool = False,
) -> Plugin:
    """Render one harness's artifacts. Layout only -- no authored content."""

    if harness not in _LAYOUTS:
        raise SkillPluginError(
            "unknown harness %r; mac knows %s" % (harness, ", ".join(sorted(_LAYOUTS)))
        )
    if scope not in {"global", "repo"}:
        raise SkillPluginError("scope must be 'global' or 'repo', not %r" % scope)
    root = Path(skills_root) if skills_root is not None else default_skills_root()
    skills = load_skills(root) if skills is None else tuple(skills)
    version = source_version(root, skills) if version is None else version

    if not allow_unverified:
        if not _tests_are_readable(root):
            raise SkillPluginError(
                "refusing to publish from %s: no tests/ tree beside it, so no "
                "skill can be shown to have a test. Publish from a checkout, or "
                "pass --allow-unverified and own the consequence." % root
            )
        missing = untested_skills(root, skills)
        if missing:
            raise SkillPluginError(
                "refusing to publish untested skill(s): %s. A published wrong "
                "skill is an instruction every harness obeys -- add a test "
                "under tests/ that names skills/<name>, or drop the skill."
                % ", ".join(missing)
            )

    layout = _LAYOUTS[harness]
    home_prefix = layout.home + "/" if scope == "global" else layout.repo_dir + "/"
    instruction_path = (
        layout.instruction_global if scope == "global" else layout.instruction_repo
    )
    # An agents file is read from the target root; a cursor rule lives inside
    # the harness directory in both scopes.
    if layout.instruction_mode == "block":
        instruction_full = (
            layout.home + "/" + instruction_path if scope == "global" else instruction_path
        )
    else:
        instruction_full = home_prefix + instruction_path

    obligation_body = _obligation_body(skills, version)
    files = []
    if layout.instruction_mode == "block":
        files.append(RenderedFile(path=instruction_full, content=obligation_body, mode="block"))
    else:
        files.append(
            RenderedFile(
                path=instruction_full,
                content=_cursor_rule(
                    obligation_body,
                    "MAC fleet obligations rendered from skills/",
                    always=True,
                ),
                mode="file",
            )
        )
    for skill in skills:
        relative = layout.reference.format(
            skill=skill.name, namespaced=_namespaced(skill.name)
        )
        body = _reference_body(skill, version)
        if relative.endswith(".mdc"):
            body = _cursor_rule(body, skill.description, always=False)
        files.append(RenderedFile(path=home_prefix + relative, content=body, mode="file"))
    files.append(
        RenderedFile(
            path=home_prefix + layout.manifest,
            content=_manifest(harness, scope, skills, version),
            mode="file",
        )
    )
    return Plugin(
        harness=harness,
        scope=scope,
        version=version,
        files=tuple(files),
        skills=tuple(skill.name for skill in skills),
        obligations=tuple(obligation.id for obligation in obligations_of(skills)),
    )


# --- Coexistence: writing into somebody else's home ------------------------


def _write_block(path: Path, body: str) -> None:
    """Replace (or append) mac's delimited block, preserving everything else."""

    block = "%s\n%s%s\n" % (BLOCK_BEGIN, body, BLOCK_END)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        start = existing.find(BLOCK_BEGIN)
        end = existing.find(BLOCK_END)
        if start != -1 and end != -1 and end > start:
            updated = existing[:start] + block + existing[end + len(BLOCK_END) + 1 :]
        else:
            separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            updated = existing + separator + block
    else:
        updated = block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")


def _strip_block(path: Path) -> None:
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8")
    start = existing.find(BLOCK_BEGIN)
    end = existing.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        return
    updated = existing[:start].rstrip("\n") + existing[end + len(BLOCK_END) :].rstrip("\n")
    updated = updated.lstrip("\n")
    if updated.strip():
        path.write_text(updated + "\n", encoding="utf-8")
    else:
        path.unlink()


def _write_owned(path: Path, content: str, *, force: bool) -> None:
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="ignore")
        if OWNED_MARKER not in existing and not force:
            raise SkillPluginError(
                "refusing to overwrite %s: it exists and mac did not write it. "
                "Move it aside, or pass --force to take ownership." % path
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --- Install / uninstall ---------------------------------------------------


@dataclass(frozen=True)
class InstallReceipt:
    host: str
    harness: str
    scope: str
    target_root: str
    revision: str
    digest: str
    version: str
    installed_at: str
    paths: Tuple[str, ...]
    blocks: Tuple[str, ...]
    obligations: Tuple[str, ...]
    skills: Tuple[str, ...]

    def as_dict(self) -> Dict[str, object]:
        return {
            "schema": RECEIPT_SCHEMA,
            "host": self.host,
            "harness": self.harness,
            "scope": self.scope,
            "target_root": self.target_root,
            "revision": self.revision,
            "digest": self.digest,
            "version": self.version,
            "installed_at": self.installed_at,
            "paths": list(self.paths),
            "blocks": list(self.blocks),
            "obligations": list(self.obligations),
            "skills": list(self.skills),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "InstallReceipt":
        return cls(
            host=str(data.get("host") or ""),
            harness=str(data.get("harness") or ""),
            scope=str(data.get("scope") or ""),
            target_root=str(data.get("target_root") or ""),
            revision=str(data.get("revision") or ""),
            digest=str(data.get("digest") or ""),
            version=str(data.get("version") or ""),
            installed_at=str(data.get("installed_at") or ""),
            paths=tuple(str(item) for item in (data.get("paths") or ())),
            blocks=tuple(str(item) for item in (data.get("blocks") or ())),
            obligations=tuple(str(item) for item in (data.get("obligations") or ())),
            skills=tuple(str(item) for item in (data.get("skills") or ())),
        )


def receipts_path(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return mac_home() / "skill-plugins" / "installs.json"


def read_receipts(path: Optional[Path] = None) -> Tuple[InstallReceipt, ...]:
    target = receipts_path(path)
    if not target.exists():
        return ()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillPluginError("install receipt %s is not valid JSON: %s" % (target, exc)) from exc
    records = data.get("installs") if isinstance(data, dict) else data
    return tuple(InstallReceipt.from_dict(item) for item in (records or ()))


def _write_receipts(receipts: Sequence[InstallReceipt], path: Optional[Path] = None) -> Path:
    target = receipts_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": RECEIPT_SCHEMA,
        "installs": [receipt.as_dict() for receipt in receipts],
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def _resolve_target_root(scope: str, target: Optional[Path], skills_root: Path) -> Path:
    if scope == "global":
        root = Path(target) if target is not None else Path.home()
        return root.expanduser().resolve()
    if target is None:
        raise SkillPluginError(
            "repo-local install needs an explicit repository: mac never guesses "
            "a working tree to write into."
        )
    root = Path(target).expanduser().resolve()
    if not root.is_dir():
        raise SkillPluginError("target repository does not exist: %s" % root)
    if (root / "skills").resolve() == skills_root.resolve():
        raise SkillPluginError(
            "refusing to install into %s: it is the source of skills/. Rendering "
            "skills back into their own source produces two copies that can "
            "disagree, and adds nothing -- install 'global', or nominate another "
            "repository." % root
        )
    return root


def install(
    harness: str,
    *,
    scope: str,
    target: Optional[Path] = None,
    skills_root: Optional[Path] = None,
    receipts: Optional[Path] = None,
    force: bool = False,
    host: Optional[str] = None,
    now: Optional[datetime] = None,
    allow_unverified: bool = False,
) -> InstallReceipt:
    """Render ``harness`` and write it under the nominated target.

    Never guesses: ``scope='repo'`` requires ``target``, and this repository is
    refused outright.
    """

    root = Path(skills_root) if skills_root is not None else default_skills_root()
    target_root = _resolve_target_root(scope, target, root)
    plugin = render_plugin(
        harness, scope=scope, skills_root=root, allow_unverified=allow_unverified
    )

    written: list[str] = []
    blocks: list[str] = []
    for rendered in plugin.files:
        destination = target_root / rendered.path
        if rendered.mode == "block":
            _write_block(destination, rendered.content)
            blocks.append(rendered.path)
        else:
            _write_owned(destination, rendered.content, force=force)
            written.append(rendered.path)

    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    receipt = InstallReceipt(
        host=host or socket.gethostname(),
        harness=harness,
        scope=scope,
        target_root=str(target_root),
        revision=plugin.version.revision,
        digest=plugin.version.digest,
        version=str(plugin.version),
        installed_at=stamp,
        paths=tuple(written),
        blocks=tuple(blocks),
        obligations=plugin.obligations,
        skills=plugin.skills,
    )
    existing = [
        item
        for item in read_receipts(receipts)
        if not (
            item.harness == receipt.harness
            and item.host == receipt.host
            and item.target_root == receipt.target_root
        )
    ]
    _write_receipts([*existing, receipt], receipts)
    return receipt


def uninstall(
    harness: str,
    *,
    target_root: Path,
    receipts: Optional[Path] = None,
    host: Optional[str] = None,
) -> InstallReceipt:
    """Remove exactly what the receipt says mac wrote, and nothing else."""

    resolved = Path(target_root).expanduser().resolve()
    hostname = host or socket.gethostname()
    records = read_receipts(receipts)
    match = next(
        (
            item
            for item in records
            if item.harness == harness
            and item.target_root == str(resolved)
            and item.host == hostname
        ),
        None,
    )
    if match is None:
        raise SkillPluginError(
            "no install receipt for harness %s at %s on %s -- nothing to remove. "
            "`mac admin skills status` lists what this host installed."
            % (harness, resolved, hostname)
        )
    for relative in match.blocks:
        _strip_block(resolved / relative)
    for relative in match.paths:
        path = resolved / relative
        if not path.exists():
            continue
        if OWNED_MARKER not in path.read_text(encoding="utf-8", errors="ignore"):
            continue
        path.unlink()
        parent = path.parent
        while parent != resolved and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    _write_receipts([item for item in records if item is not match], receipts)
    return match


def status(
    *,
    skills_root: Optional[Path] = None,
    receipts: Optional[Path] = None,
    host: Optional[str] = None,
) -> Dict[str, object]:
    """What every recorded install carries, and whether it is stale."""

    root = Path(skills_root) if skills_root is not None else default_skills_root()
    current = source_version(root)
    records = read_receipts(receipts)
    if host is not None:
        records = tuple(item for item in records if item.host == host)
    installs = [
        {
            **item.as_dict(),
            "stale": item.digest != current.digest,
        }
        for item in records
    ]
    missing = tuple(
        harness
        for harness in HARNESSES
        if harness not in {item.harness for item in records}
    )
    return {
        "schema": RECEIPT_SCHEMA,
        "source_version": str(current),
        "source_revision": current.revision,
        "source_digest": current.digest,
        "installs": installs,
        "harnesses_without_install": list(missing),
        "stale": [item for item in installs if item["stale"]],
    }
