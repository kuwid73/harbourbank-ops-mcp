"""The change server: the agent's only write path. What it may change, how much, and that it can only
ever propose (branch, commit, pull request), never merge."""
import base64
import json
from datetime import UTC, datetime, timedelta

import anyio
import httpx
import pytest
import yaml

from harbourbank_ops_mcp import change_server, changes
from harbourbank_ops_mcp.changes import Refused
from harbourbank_ops_mcp.safety import AuditLog

REPO = "/repos/kuwid73/harbourbank-gitops"
PROD = "apps/harbourbank/overlays/prod/kustomization.yaml"
OVERLAY = "configMapGenerator:\n  - name: accounts-api-config\n    literals:\n      - ENVIRONMENT=prod\n"


class FakeGitHub:
    """Files by (path, ref); records every request; answers the Git Data and Pulls endpoints."""

    def __init__(self):
        self.files: dict[tuple[str, str], str] = {(PROD, "main"): OVERLAY}
        self.pulls: list[dict] = []
        self.merged: dict[int, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path.removeprefix(REPO)
        self.requests.append((request.method, path, body))
        if path.startswith("/contents/"):
            text = self.files.get((path.removeprefix("/contents/"), request.url.params.get("ref", "main")))
            if text is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"content": base64.b64encode(text.encode()).decode()})
        routes = {
            ("GET", "/git/ref/heads/main"): {"object": {"sha": "main1"}},
            ("GET", "/git/commits/main1"): {"tree": {"sha": "tree1"}},
            ("POST", "/git/trees"): {"sha": "tree2"},
            ("POST", "/git/commits"): {"sha": "commit2"},
            ("POST", "/git/refs"): {"ref": body.get("ref")},
            ("GET", "/pulls"): self.pulls,
            ("POST", "/pulls"): {"number": 99, "html_url": "https://github.com/kuwid73/harbourbank-gitops/pull/99"},
        }
        if path.startswith("/pulls/") and request.method == "GET":
            return httpx.Response(200, json=self.merged[int(path.split("/")[2])])
        if path.startswith("/commits/"):
            return httpx.Response(200, json=self.merged["commit"])
        return httpx.Response(200, json=routes[(request.method, path)])

    def sent(self, method: str, path: str) -> dict:
        return next(b for m, p, b in self.requests if (m, p) == (method, path))


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def writer(gh):
    return changes.Writer(lambda: "test-token", transport=httpx.MockTransport(gh.handle))


def mine(state="open", minutes_ago=600, number=1):
    created = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"number": number, "title": "t", "html_url": "u", "state": state, "created_at": created,
            "user": {"login": changes.BOT}, "merged_at": None}


# --- what it may change ---------------------------------------------------------------------------

def test_an_edit_becomes_a_branch_a_commit_and_a_pull_request(gh, writer):
    result = changes.propose_change(writer, "prod: fail fast on pool exhaustion", "why it helps",
                                    "evidence from the ops tools", [{"path": PROD, "find": "      - ENVIRONMENT=prod\n",
                                     "replace": "      - ENVIRONMENT=prod\n      - DB_POOL_TIMEOUT_SECONDS=5\n"}])

    tree = gh.sent("POST", "/git/trees")
    assert tree["base_tree"] == "tree1" and tree["tree"][0]["path"] == PROD
    assert "DB_POOL_TIMEOUT_SECONDS=5\n" in tree["tree"][0]["content"]
    assert gh.sent("POST", "/git/commits")["parents"] == ["main1"]
    assert gh.sent("POST", "/git/refs")["ref"].startswith("refs/heads/agent/")
    pr = gh.sent("POST", "/pulls")
    assert pr["base"] == "main" and "## Evidence\nevidence from the ops tools" in pr["body"]
    assert "cannot approve or merge" in pr["body"]
    assert result["pull_request"] == 99


def test_it_never_merges_approves_or_writes_to_main(gh, writer):
    changes.propose_change(writer, "t", "w", "e", [{"path": PROD, "find": "prod\n", "replace": "prod \n"}])
    writes = {(m, p) for m, p, _ in gh.requests if m != "GET"}
    assert writes == {("POST", "/git/trees"), ("POST", "/git/commits"), ("POST", "/git/refs"), ("POST", "/pulls")}
    assert all("main" not in b.get("ref", "") for m, p, b in gh.requests if p == "/git/refs")


@pytest.mark.parametrize("path", [".github/workflows/promote.yml", ".github/CODEOWNERS", "bootstrap/lab-start.sh",
                                  "platform/values/kube-prometheus-stack.yaml", "README.md",
                                  "apps/harbourbank/overlays/prod/../../../platform/x.yaml",
                                  "apps/harbourbank/base/smoke_test.py", "incidents/README.md"])
def test_only_the_apps_manifests_and_incident_records_can_change(writer, path):
    with pytest.raises(Refused, match="outside what the agent may change"):
        changes.propose_change(writer, "t", "w", "e", [{"path": path, "find": "a", "replace": "b"}])


@pytest.mark.parametrize("find, problem", [("missing text", "matches 0 places"), ("      - ", "matches 2 places")])
def test_find_must_match_exactly_once(gh, writer, find, problem):
    gh.files[(PROD, "main")] = OVERLAY + "      - X=1\n"
    with pytest.raises(Refused, match=problem):
        changes.propose_change(writer, "t", "w", "e", [{"path": PROD, "find": find, "replace": "b"}])


def test_an_edit_that_breaks_the_yaml_is_refused(writer):
    with pytest.raises(Refused, match="invalid YAML"):
        changes.propose_change(writer, "t", "w", "e", [{"path": PROD, "find": "  - name", "replace": "\t- name: ["}])


def test_open_and_hourly_limits(gh, writer):
    edit = [{"path": PROD, "find": "prod\n", "replace": "prod \n"}]
    gh.pulls = [mine(number=n) for n in range(changes.MAX_OPEN)]
    with pytest.raises(Refused, match="still open"):
        changes.propose_change(writer, "t", "w", "e", edit)
    gh.pulls = [mine(state="closed", minutes_ago=10, number=n) for n in range(changes.MAX_PER_HOUR)]
    with pytest.raises(Refused, match="last hour"):
        changes.propose_change(writer, "t", "w", "e", edit)


def test_secrets_never_reach_the_public_repository(gh, writer):
    changes.propose_change(writer, "t", "why", "token ghp_abcdefghijklmnopqrstuvwxyz0123 in a log line",
                           [{"path": PROD, "find": "prod\n", "replace": "prod \n"}])
    assert "ghp_" not in gh.sent("POST", "/pulls")["body"]


# --- reverts and incident records ---------------------------------------------------------------

def merged_pr(gh, files):
    gh.merged[20] = {"title": "prod: fail fast", "merged_at": "2026-11-02T13:02:22Z", "merge_commit_sha": "m20"}
    gh.merged["commit"] = {"parents": [{"sha": "p19"}], "files": files}


def test_a_revert_restores_each_file_to_before_the_pr(gh, writer):
    merged_pr(gh, [{"filename": PROD, "status": "modified"}])
    gh.files[(PROD, "m20")] = gh.files[(PROD, "main")] = OVERLAY + "      - DB_POOL_TIMEOUT_SECONDS=5s\n"
    gh.files[(PROD, "p19")] = OVERLAY

    result = changes.propose_revert(writer, 20, "the typo stalled the rollout", "crash loop in describe_workload")

    assert gh.sent("POST", "/git/trees")["tree"][0]["content"] == OVERLAY
    assert gh.sent("POST", "/pulls")["title"] == 'Revert "prod: fail fast" (#20)'
    assert result["files"] == [PROD]


def test_a_revert_is_refused_when_the_file_changed_since(gh, writer):
    merged_pr(gh, [{"filename": PROD, "status": "modified"}])
    gh.files[(PROD, "m20")] = OVERLAY + "      - A=1\n"
    gh.files[(PROD, "main")] = OVERLAY + "      - A=1\n      - B=2\n"
    with pytest.raises(Refused, match="changed again"):
        changes.propose_revert(writer, 20, "w", "e")


def test_a_revert_of_a_change_outside_the_agents_scope_is_refused(gh, writer):
    merged_pr(gh, [{"filename": "bootstrap/lab-start.sh", "status": "modified"}])
    with pytest.raises(Refused, match="outside what the agent may change"):
        changes.propose_revert(writer, 20, "w", "e")


def test_an_incident_record_follows_the_schema(gh, writer):
    changes.record_incident(writer, "2026-11-05-drill-3", "a drill", ["prod"], "2026-11-05T10:00:00Z",
                            "2026-11-05T10:02:00+00:00", "Slack alert", "rollback", "none for customers",
                            "find_problems at 10:03", restored="2026-11-05T10:20:00Z", caused_by="gitops#30")
    entry = gh.sent("POST", "/git/trees")["tree"][0]
    record = yaml.safe_load(entry["content"])
    assert entry["path"] == "incidents/2026-11-05-drill-3.yaml"
    assert record["detected"] == "2026-11-05T10:02:00Z" and record["caused_by"] == "gitops#30"
    assert "follow_up" not in record


@pytest.mark.parametrize("kwargs, problem", [
    ({"incident_id": "drill-3"}, "must look like"),
    ({"started": "2026-11-05 10:00"}, "time zone"),
    ({"detected": "2026-11-05T09:00:00Z"}, "before started"),
    ({"caused_by": "PR 30"}, "gitops#"),
    ({"environments": ["staging"]}, "dev, qa, prod"),
])
def test_bad_incident_records_are_refused(writer, kwargs, problem):
    args = {"incident_id": "2026-11-05-drill-3", "title": "t", "environments": ["prod"],
            "started": "2026-11-05T10:00:00Z", "detected": "2026-11-05T10:02:00Z", "detected_by": "alert",
            "remediation": "rollback", "impact": "none", "evidence": "e"} | kwargs
    with pytest.raises(Refused, match=problem):
        changes.record_incident(writer, **args)


# --- the server ------------------------------------------------------------------------------------

def test_the_server_offers_exactly_these_tools_and_audits_refusals(writer, tmp_path):
    mcp = change_server.build(writer, AuditLog(tmp_path / "change-audit.jsonl"))

    async def run():
        names = {t.name for t in await mcp.list_tools()}
        assert names == {"propose_change", "propose_revert", "record_incident", "my_proposals"}
        await mcp.call_tool("propose_change", {"title": "t", "why": "w", "evidence": "e",
                                               "edits": [{"path": ".github/CODEOWNERS", "find": "a", "replace": "b"}]})
    anyio.run(run)
    entry = json.loads((tmp_path / "change-audit.jsonl").read_text().splitlines()[0])
    assert entry["tool"] == "propose_change" and entry["outcome"] == "error"
