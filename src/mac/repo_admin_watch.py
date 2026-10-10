"""Notice repository-administration changes made while a task ran.

"Act, then tell" (:mod:`mac.human_reports`) asks agents to report what they
did. This module makes one class of it not depend on the agent remembering:
the worker snapshots a GitHub repository's administration -- its settings,
rulesets, default-branch protection and webhooks -- before a task attempt
and again after it, and files a report when anything changed. That is the
2026-10-02 shape: an agent in a sandbox created an active ruleset on
jordanhubbard/Aviation (id 24407548) with the fleet's forge credential, and no
person was told.

What is compared is what a person would want to hear about, not every field
the API returns: counters, timestamps and the default branch's own head move
on every push and are left out. A section the API would not show (no token,
no permission, a forge outage) is recorded as unreadable on that side and is
never reported as a change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional

from mac import gitops

JsonDict = Dict[str, Any]

#: Repository settings a person cares about when they change.
REPO_SETTING_FIELDS = (
    "default_branch",
    "visibility",
    "private",
    "archived",
    "disabled",
    "has_issues",
    "has_projects",
    "has_wiki",
    "has_discussions",
    "allow_squash_merge",
    "allow_merge_commit",
    "allow_rebase_merge",
    "allow_auto_merge",
    "allow_forking",
    "allow_update_branch",
    "delete_branch_on_merge",
    "web_commit_signoff_required",
    "security_and_analysis",
)

#: Ruleset fields that change what a ruleset does (not its bookkeeping).
RULESET_FIELDS = (
    "name",
    "target",
    "enforcement",
    "conditions",
    "rules",
    "bypass_actors",
)

HOOK_FIELDS = ("active", "events", "config")

MAX_LISTED_CHANGES = 12


class _Unreadable(Exception):
    pass


def _pick(item: Any, fields: tuple) -> JsonDict:
    if not isinstance(item, dict):
        return {}
    return {name: item.get(name) for name in fields if name in item}


def _hook_view(item: Any) -> JsonDict:
    view = _pick(item, HOOK_FIELDS)
    config = view.get("config")
    if isinstance(config, dict):
        # The URL and content type say where events go; the secret is never
        # returned in clear and must never be compared or reported.
        view["config"] = {
            key: config.get(key) for key in ("url", "content_type", "insecure_ssl") if key in config
        }
    return view


def snapshot(
    repo_url: str,
    *,
    token: Optional[str] = None,
    get_json: Optional[Callable[[str, dict], Any]] = None,
) -> Optional[JsonDict]:
    """The repository's administration now, or ``None`` when not a GitHub repo.

    Never raises: an unreadable section is recorded as ``{"unreadable": ...}``
    so the comparison skips it.
    """
    try:
        https = gitops.https_remote_for_token_auth(str(repo_url or "").strip())
        if not https or gitops.detect_host(https) != "github":
            return None
        owner, repo = gitops._parse_owner_repo(https)
        api_base = gitops._api_base_for("github", https)
    except Exception:  # noqa: BLE001 - not a GitHub repository we can read
        return None
    if token is None:
        try:
            token = gitops.forge_token("github")
        except Exception:  # noqa: BLE001
            token = ""
    if not token:
        return None
    headers = {
        "Authorization": "token " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "mac-repo-admin-watch",
    }
    fetch = get_json or gitops._http_get_json
    base = "%s/repos/%s/%s" % (api_base, owner, repo)

    def get(path: str) -> Any:
        try:
            return fetch(base + path, headers)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            raise _Unreadable(gitops._scrub_secret(str(exc), token)[:200]) from None

    sections: JsonDict = {}
    default_branch = ""
    try:
        meta = get("")
        sections["settings"] = _pick(meta, REPO_SETTING_FIELDS)
        default_branch = str((meta or {}).get("default_branch") or "")
    except _Unreadable as exc:
        sections["settings"] = {"unreadable": str(exc)}
    try:
        listed = get("/rulesets?includes_parents=false&per_page=100")
        rulesets: JsonDict = {}
        for item in listed if isinstance(listed, list) else []:
            ruleset_id = str((item or {}).get("id") or "")
            if not ruleset_id:
                continue
            rulesets[ruleset_id] = _pick(get("/rulesets/%s" % ruleset_id), RULESET_FIELDS)
        sections["rulesets"] = rulesets
    except _Unreadable as exc:
        sections["rulesets"] = {"unreadable": str(exc)}
    try:
        hooks = get("/hooks?per_page=100")
        sections["webhooks"] = {
            str(item.get("id")): _hook_view(item)
            for item in (hooks if isinstance(hooks, list) else [])
            if isinstance(item, dict) and item.get("id") is not None
        }
    except _Unreadable as exc:
        sections["webhooks"] = {"unreadable": str(exc)}
    if default_branch:
        try:
            protection = get("/branches/%s/protection" % default_branch)
            sections["branch_protection"] = _strip_urls(protection)
        except _Unreadable as exc:
            # An unprotected branch answers 404 "Branch not protected": that is
            # a state ("none"), not an unreadable section.
            text = str(exc).lower()
            sections["branch_protection"] = (
                {"protected": False} if "not protected" in text or "404" in text else {"unreadable": str(exc)}
            )
    return {"repository": "%s/%s" % (owner, repo), "sections": sections}


def _strip_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_urls(v) for k, v in value.items() if not str(k).endswith("url")}
    if isinstance(value, list):
        return [_strip_urls(v) for v in value]
    return value


def _readable(section: Any) -> bool:
    return isinstance(section, dict) and "unreadable" not in section


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def diff(before: Optional[JsonDict], after: Optional[JsonDict]) -> List[JsonDict]:
    """What changed between two snapshots of the same repository."""
    if not before or not after or before.get("repository") != after.get("repository"):
        return []
    changes: List[JsonDict] = []
    old_sections = before.get("sections") or {}
    new_sections = after.get("sections") or {}
    for section in ("settings", "branch_protection"):
        old, new = old_sections.get(section), new_sections.get(section)
        if not (_readable(old) and _readable(new)):
            continue
        if section == "settings":
            for name in sorted(set(old) | set(new)):
                if _canonical(old.get(name)) != _canonical(new.get(name)):
                    changes.append(
                        {
                            "section": "settings",
                            "change": "changed",
                            "item": name,
                            "before": old.get(name),
                            "after": new.get(name),
                        }
                    )
        elif _canonical(old) != _canonical(new):
            changes.append({"section": section, "change": "changed", "item": "default branch"})
    for section, label in (("rulesets", "ruleset"), ("webhooks", "webhook")):
        old, new = old_sections.get(section), new_sections.get(section)
        if not (_readable(old) and _readable(new)):
            continue
        for item_id in sorted(set(old) | set(new)):
            if item_id not in old:
                change = "added"
            elif item_id not in new:
                change = "removed"
            elif _canonical(old[item_id]) != _canonical(new[item_id]):
                change = "changed"
            else:
                continue
            current = new.get(item_id) or old.get(item_id) or {}
            entry = {"section": section, "change": change, "item": "%s %s" % (label, item_id)}
            if current.get("name"):
                entry["name"] = current["name"]
            if section == "rulesets" and current.get("enforcement"):
                entry["enforcement"] = current["enforcement"]
            changes.append(entry)
    return changes


def describe(changes: List[JsonDict]) -> str:
    """One line per change, for people."""
    lines = []
    for change in changes[:MAX_LISTED_CHANGES]:
        text = "%s %s" % (change["item"], change["change"])
        if change.get("name"):
            text += " (%s)" % change["name"]
        if change.get("enforcement"):
            text += ", enforcement %s" % change["enforcement"]
        if "before" in change or "after" in change:
            text += ": %s -> %s" % (_canonical(change.get("before")), _canonical(change.get("after")))
        lines.append("- %s" % text)
    if len(changes) > MAX_LISTED_CHANGES:
        lines.append("- ... and %d more" % (len(changes) - MAX_LISTED_CHANGES))
    return "\n".join(lines)


def change_key(repository: str, changes: List[JsonDict]) -> str:
    """The dedupe key two workers that saw the same change agree on."""
    digest = hashlib.sha256(_canonical(changes).encode("utf-8")).hexdigest()[:24]
    return "repo-admin:%s:%s" % (repository, digest)
