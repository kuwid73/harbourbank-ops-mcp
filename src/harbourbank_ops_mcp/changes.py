"""The agent's only way to change Harbour Bank: a pull request on harbourbank-gitops that a person approves.

Policy, enforced here rather than left to the model:
- one repository (harbourbank-gitops), and in it only the app's manifests and the incident records:
  never CI (.github/), the lab bootstrap, the platform add-ons or the code-owner rules
- small, exact edits: each `find` must match exactly once, and the result must still be valid YAML
- at most 3 of the agent's PRs open at once, and 5 opened per hour
- it can open PRs, never approve or merge them: the App has no such permission, and the branch rules
  need a code owner's approval
Everything it writes to GitHub is masked first, because the repository is public.
"""
import base64
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import yaml

from .appauth import API, HEADERS
from .safety import mask_text

REPO = "kuwid73/harbourbank-gitops"
BOT = "harbourbank-agent[bot]"
ALLOWED = [
    re.compile(r"^apps/harbourbank/overlays/(dev|qa|prod)/[\w.-]+\.ya?ml$"),
    re.compile(r"^apps/harbourbank/base/[\w.-]+\.ya?ml$"),
    re.compile(r"^incidents/\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.yaml$"),
]
MAX_FILES = 5
MAX_CHANGED_LINES = 200
MAX_OPEN = 3
MAX_PER_HOUR = 5
ENVIRONMENTS = {"dev", "qa", "prod"}
INCIDENT_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*$")
CHANGE_REF = re.compile(r"^gitops(#\d+|@[0-9a-f]{7,40})$")


class Refused(ValueError):
    """The policy says no; the message says why, for the model to relay."""


class Writer:
    def __init__(self, token: Callable[[], str], transport: httpx.BaseTransport | None = None):
        self._token = token
        self._http = httpx.Client(base_url=API, headers=HEADERS, timeout=15, transport=transport)

    def call(self, method: str, path: str, **json) -> dict | list:
        response = self._http.request(method, f"/repos/{REPO}{path}", json=json or None,
                                      headers={"Authorization": f"token {self._token()}"})
        response.raise_for_status()
        return response.json() if response.content else {}

    def get(self, path: str, **params) -> dict | list:
        response = self._http.get(f"/repos/{REPO}{path}", params=params,
                                  headers={"Authorization": f"token {self._token()}"})
        response.raise_for_status()
        return response.json()

    def file(self, path: str, ref: str = "main") -> str | None:
        response = self._http.get(f"/repos/{REPO}/contents/{path}", params={"ref": ref},
                                  headers={"Authorization": f"token {self._token()}"})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return base64.b64decode(response.json()["content"]).decode()


# --- the policy -------------------------------------------------------------------------------

def _check_path(path: str) -> None:
    if ".." in path.split("/") or not any(p.match(path) for p in ALLOWED):
        raise Refused(f"{path!r} is outside what the agent may change (the app's manifests under "
                      "apps/harbourbank/ and incidents/). A person must change it.")


def _check_volume(old: dict[str, str | None], new: dict[str, str | None]) -> None:
    if len(new) > MAX_FILES:
        raise Refused(f"{len(new)} files in one proposal; the limit is {MAX_FILES}. Split it.")
    changed = sum(len(set((old[p] or "").splitlines()) ^ set((new[p] or "").splitlines())) for p in new)
    if changed > MAX_CHANGED_LINES:
        raise Refused(f"about {changed} changed lines; the limit is {MAX_CHANGED_LINES}. Propose something smaller.")


def _check_rate(w: Writer) -> None:
    mine = [p for p in w.get("/pulls", state="all", per_page=50, sort="created", direction="desc")
            if p["user"]["login"] == BOT]
    open_now = [p for p in mine if p["state"] == "open"]
    if len(open_now) >= MAX_OPEN:
        numbers = ", ".join(f"#{p['number']}" for p in open_now)
        raise Refused(f"{len(open_now)} of the agent's PRs are still open ({numbers}); the limit is {MAX_OPEN}. "
                      "Wait for a person to review them.")
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    recent = [p for p in mine if datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) > hour_ago]
    if len(recent) >= MAX_PER_HOUR:
        raise Refused(f"{len(recent)} PRs opened in the last hour; the limit is {MAX_PER_HOUR}.")


def _valid_yaml(path: str, text: str) -> None:
    try:
        list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise Refused(f"the change would leave {path} as invalid YAML: {exc}") from None


# --- opening the pull request ------------------------------------------------------------------

def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "change"


def _open_pr(w: Writer, title: str, why: str, evidence: str, files: dict[str, str | None],
             kind: str) -> dict:
    """Commit `files` (path -> new content, None = delete) on a new branch from main, and open a PR."""
    _check_rate(w)
    title, why, evidence = mask_text(title), mask_text(why), mask_text(evidence)
    head = w.get("/git/ref/heads/main")["object"]["sha"]
    base_tree = w.get(f"/git/commits/{head}")["tree"]["sha"]
    entries = [{"path": p, "mode": "100644", "type": "blob", **({"content": c} if c is not None else {"sha": None})}
               for p, c in files.items()]
    tree = w.call("POST", "/git/trees", base_tree=base_tree, tree=entries)["sha"]
    commit = w.call("POST", "/git/commits", message=f"{title}\n\n{why}", tree=tree, parents=[head])["sha"]
    branch = f"agent/{datetime.now(UTC):%Y%m%d-%H%M}-{_slug(title)}"
    w.call("POST", "/git/refs", ref=f"refs/heads/{branch}", sha=commit)
    listing = "\n".join(f"- `{p}`" + (" (deleted)" if c is None else "") for p, c in files.items())
    body = (f"## Why\n{why}\n\n## Evidence\n{evidence}\n\n## Change ({kind})\n{listing}\n\n---\n"
            "Proposed by **harbourbank-agent**, the Harbour Bank operations agent in Claude Desktop. It can "
            "open pull requests but cannot approve or merge them: a code owner reviews this and decides.")
    pr = w.call("POST", "/pulls", title=title, head=branch, base="main", body=body)
    return {"pull_request": pr["number"], "url": pr["html_url"], "branch": branch, "files": list(files),
            "next": "A code owner must review and merge it; the agent cannot. Tell the user the link."}


def propose_change(w: Writer, title: str, why: str, evidence: str, edits: list[dict]) -> dict:
    """Exact find/replace edits to existing files, as one PR."""
    if not edits:
        raise Refused("no edits given")
    old: dict[str, str | None] = {}
    new: dict[str, str | None] = {}
    for edit in edits:
        path, find, replace = edit.get("path", ""), edit.get("find", ""), edit.get("replace", "")
        _check_path(path)
        if path not in new:
            current = w.file(path)
            if current is None:
                raise Refused(f"{path} doesn't exist on main; only existing files can be edited")
            old[path] = new[path] = current
        if not find:
            raise Refused(f"empty `find` for {path}")
        count = new[path].count(find)
        if count != 1:
            raise Refused(f"`find` text matches {count} places in {path}; it must match exactly one. "
                          "Quote more of the surrounding lines.")
        new[path] = new[path].replace(find, replace)
    for path, text in new.items():
        if text == old[path]:
            raise Refused(f"the edits leave {path} unchanged")
        _valid_yaml(path, text)
    _check_volume(old, new)
    return _open_pr(w, title, why, evidence, new, "edit")


def propose_revert(w: Writer, pull_request: int, why: str, evidence: str) -> dict:
    """Undo a merged PR: every file it changed goes back to how it was before it."""
    pr = w.get(f"/pulls/{pull_request}")
    if not pr.get("merged_at"):
        raise Refused(f"#{pull_request} isn't merged, so there is nothing to revert")
    merged = w.get(f"/commits/{pr['merge_commit_sha']}")
    if len(merged.get("parents", [])) != 1:
        raise Refused(f"#{pull_request} was merged with a merge commit; a person must revert it")
    before = merged["parents"][0]["sha"]
    old: dict[str, str | None] = {}
    new: dict[str, str | None] = {}
    for f in merged.get("files", []):
        for path in {f["filename"], f.get("previous_filename") or f["filename"]}:
            _check_path(path)
        current = w.file(f["filename"])
        if current != w.file(f["filename"], ref=pr["merge_commit_sha"]):
            raise Refused(f"{f['filename']} has changed again since #{pull_request} merged; a plain revert "
                          "would undo that too. Use propose_change for a targeted fix.")
        old[f["filename"]] = current
        if f["status"] == "renamed":
            old[f["previous_filename"]] = None
            new[f["filename"]] = None
            new[f["previous_filename"]] = w.file(f["previous_filename"], ref=before)
        else:
            new[f["filename"]] = w.file(f["filename"], ref=before)   # None for a file the PR added
    _check_volume(old, new)
    title = f"Revert \"{pr['title']}\" (#{pull_request})"
    return _open_pr(w, title, why, evidence, new, f"revert of #{pull_request}")


def _utc(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(f"{field} must be an ISO 8601 time, e.g. 2026-11-02T13:06:19Z") from None
    if when.tzinfo is None:
        raise Refused(f"{field} needs a time zone: end it with Z for UTC")
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_incident(w: Writer, incident_id: str, title: str, environments: list[str], started: str,
                    detected: str, detected_by: str, remediation: str, impact: str, evidence: str,
                    restored: str | None = None, caused_by: str | None = None,
                    follow_up: str | None = None) -> dict:
    """A new incident record in incidents/, in the schema incidents/README.md defines."""
    if not INCIDENT_ID.match(incident_id):
        raise Refused("incident_id must look like 2026-11-02-short-name")
    if not environments or not set(environments) <= ENVIRONMENTS:
        raise Refused("environments must be some of dev, qa, prod")
    if caused_by is not None and not CHANGE_REF.match(caused_by):
        raise Refused("caused_by must be gitops#<PR> or gitops@<sha>")
    times = {k: _utc(v, k) for k, v in (("started", started), ("detected", detected), ("restored", restored))}
    if times["detected"] < times["started"] or (times["restored"] and times["restored"] < times["started"]):
        raise Refused("detected and restored can't be before started")
    path = f"incidents/{incident_id}.yaml"
    _check_path(path)
    if w.file(path) is not None:
        raise Refused(f"{path} already exists; edit it with propose_change")
    record = {"id": incident_id, "title": title, "environments": environments, **times,
              "detected_by": detected_by, "caused_by": caused_by, "remediation": remediation,
              "impact": impact, "follow_up": follow_up, "evidence": evidence}
    text = mask_text(yaml.safe_dump({k: v for k, v in record.items() if v is not None}, sort_keys=False,
                                    allow_unicode=True, width=100))
    return _open_pr(w, f"Record incident {incident_id}", f"Incident record: {title}", evidence,
                    {path: text}, "new incident record")


def my_proposals(w: Writer) -> dict:
    """The agent's recent PRs and what became of them."""
    pulls = [p for p in w.get("/pulls", state="all", per_page=50, sort="created", direction="desc")
             if p["user"]["login"] == BOT][:20]
    return {"pull_requests": [{"number": p["number"], "title": p["title"], "url": p["html_url"],
                               "state": "merged" if p.get("merged_at") else p["state"],
                               "opened": p["created_at"], "closed": p.get("closed_at")} for p in pulls]}
