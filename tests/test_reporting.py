"""Facts the tools must report so the model has nothing to guess. Each test is a case from the
2026-10-30 demo run where a missing fact led to a wrong or incomplete answer (NOTES.md)."""
import time

from conftest import commit, pod, ts
from harbourbank_ops_mcp import ops

GITOPS = "/repos/kuwid73/harbourbank-gitops"
CI = {"author": "harbourbank-ci", "email": "41898282+github-actions[bot]@users.noreply.github.com"}


def loki_line(line, pod_name="p", namespace=None):
    stream = {"pod": pod_name} | ({"namespace": namespace} if namespace else {})
    return [{"stream": stream, "values": [[str(time.time_ns()), line]]}]


# --- 1. end of day: retries and their errors, manual vs scheduled ---------------------------------

def end_of_day_jobs(fake, *jobs):
    fake.routes["/apis/batch/v1/namespaces/harbourbank-prod/cronjobs/end-of-day"] = {
        "spec": {"schedule": "15 0 * * *", "timeZone": "Etc/UTC"},
        "status": {"lastScheduleTime": ts(300), "lastSuccessfulTime": ts(260)}}
    fake.routes["/apis/batch/v1/namespaces/harbourbank-prod/jobs"] = {"items": list(jobs)}


def test_a_succeeded_run_that_needed_retries_reports_each_failed_attempt(fake, kube):
    end_of_day_jobs(fake, {
        "metadata": {"name": "end-of-day-29888655", "creationTimestamp": ts(300),
                     "annotations": {"batch.kubernetes.io/cronjob-scheduled-timestamp": "2026-10-30T00:15:00Z"}},
        "status": {"succeeded": 1, "failed": 1, "startTime": "2026-10-30T02:59:58Z"}})
    fake.loki[r"Error\|Exception"] = loki_line(
        "sqlalchemy.exc.OperationalError: (psycopg.OperationalError) failed to resolve host "
        "'accounts-db-rw.harbourbank-prod'", "end-of-day-29888655-997x4")

    result = ops.end_of_day_status(kube, "prod")

    run = result["recent_runs"][0]
    assert run["state"] == "succeeded" and run["failed_attempts"] == 1
    assert run["trigger"] == "schedule" and run["started_late_by"] == "2h44m"
    assert "failed to resolve host" in result["failed_attempts_newest_first"][0]["error"]
    assert any("needed retries" in n for n in result["notes"])


def test_a_manual_run_is_called_manual(fake, kube):
    end_of_day_jobs(fake, {"metadata": {"name": "end-of-day-manual-1", "creationTimestamp": ts(60),
                                        "annotations": {"cronjob.kubernetes.io/instantiate": "manual"}},
                           "status": {"succeeded": 1}})
    result = ops.end_of_day_status(kube, "prod")
    assert result["recent_runs"][0]["trigger"] == "manual"
    assert any("started by hand" in n for n in result["notes"])
    assert result["next_scheduled_run"].endswith("T00:15+00:00")


# --- 2 and 5. change audit: who really made it, blocked pushes, when the rules began ---------------

def test_change_audit_tells_the_ci_pipeline_from_the_human_whose_key_it_used(fake, gh):
    fake.routes[f"{GITOPS}/rulesets/rule-suites"] = [
        {"pushed_at": ts(5), "actor_name": "kuwid73", "result": "bypass", "before_sha": "b1", "after_sha": "816cd3b"},
        {"pushed_at": ts(9), "actor_name": "kuwid73", "result": "fail", "before_sha": "b0", "after_sha": "3d8154d"}]
    fake.routes[f"{GITOPS}/commits/816cd3b"] = commit("dev: accounts-api sha-2764763", CI["author"], CI["email"],
                                                      ["apps/harbourbank/overlays/dev/kustomization.yaml"])
    fake.routes[f"{GITOPS}/commits/816cd3b/pulls"] = []
    fake.routes[f"{GITOPS}/rulesets"] = [{"id": 7, "name": "main: reviewed changes only"}]
    fake.routes[f"{GITOPS}/rulesets/7"] = {"created_at": "2026-10-28T10:36:05+05:30"}

    result = ops.change_audit(gh)

    pipeline = result["pushes_to_main"][0]
    assert pipeline["github_actor"] == "kuwid73" and pipeline["made_by"] == "bot"
    assert pipeline["touches"] == ["dev"] and pipeline["pull_request"] == "none: a direct push"
    assert result["results"] == {"pass": 0, "bypass": 1, "fail": 1}
    assert [b["change"] for b in result["blocked_pushes"]] == ["b0 -> 3d8154d"]
    assert result["rules_in_force_since"] == {"main: reviewed changes only": "2026-10-28T10:36:05+05:30"}


def test_a_bot_made_pr_approved_by_a_person_has_an_independent_approval(fake, gh):
    fake.routes[f"{GITOPS}/pulls/5"] = {"title": "prod: accounts-api sha-2764763",
                                        "user": {"login": "github-actions[bot]", "type": "Bot"},
                                        "merged_by": {"login": "kuwid73"}, "merged_at": ts()}
    fake.routes[f"{GITOPS}/pulls/5/reviews"] = [{"user": {"login": "kuwid73"}, "state": "APPROVED"}]
    pr = ops._pull_request(gh, "kuwid73/harbourbank-gitops", 5)
    assert pr["made_by"] == "bot (CI)" and pr["independent_approval_by"] == ["kuwid73"]


def test_an_owners_pr_with_only_comments_has_no_independent_approval(fake, gh):
    fake.routes[f"{GITOPS}/pulls/10"] = {"title": "Schedule the end-of-day batch", "user": {"login": "kuwid73"},
                                         "merged_by": {"login": "kuwid73"}, "merged_at": ts()}
    fake.routes[f"{GITOPS}/pulls/10/reviews"] = [{"user": {"login": "kuwid73"}, "state": "COMMENTED"}]
    pr = ops._pull_request(gh, "kuwid73/harbourbank-gitops", 10)
    assert pr["made_by"] == "human" and pr["independent_approval_by"] is None


# --- 4. deployments: every commit shipped, and where each one lands --------------------------------

def test_a_deployment_lists_every_commit_and_whether_it_changed_this_environment(fake, kube, gh):
    fake.routes["/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/harbourbank-prod"] = {"status": {
        "history": [{"deployedAt": ts(300), "revision": "4df088d"}, {"deployedAt": ts(200), "revision": "b158424"}]}}
    fake.routes[f"{GITOPS}/compare/4df088d...b158424"] = {"commits": [{"sha": "816cd3b"}, {"sha": "b158424"}]}
    fake.routes[f"{GITOPS}/commits/816cd3b"] = commit("dev: accounts-api sha-2764763", CI["author"], CI["email"],
                                                      ["apps/harbourbank/overlays/dev/kustomization.yaml"])
    fake.routes[f"{GITOPS}/commits/816cd3b/pulls"] = []          # a direct push belongs to no PR
    fake.routes[f"{GITOPS}/commits/b158424"] = commit("Merge pull request #2 from kuwid73/fix-rollout",
                                                      files=["apps/harbourbank/base/accounts-api.yaml"])
    fake.routes[f"{GITOPS}/commits/4df088d"] = commit("Add smoke test", files=["apps/harbourbank/base/smoke.yaml"])
    fake.routes[f"{GITOPS}/pulls/2"] = {"title": "Keep serving on shutdown", "user": {"login": "kuwid73"},
                                        "merged_by": {"login": "kuwid73"}, "merged_at": ts()}
    fake.routes[f"{GITOPS}/pulls/2/reviews"] = []

    newest = ops.recent_changes(kube, gh, "prod")["deployments"][0]

    assert [c["sha"] for c in newest["commits_newest_first"]] == ["b158424", "816cd3b"]
    assert newest["changed_this_environment"] == ["b158424"]          # the dev-only push changed nothing in prod
    assert newest["commits_newest_first"][0]["pull_request"]["number"] == 2   # found from a merge commit title
    assert newest["commits_newest_first"][1]["made_by"] == "bot"


def test_touches_maps_paths_to_where_they_land():
    assert ops._touches(["apps/harbourbank/overlays/qa/k.yaml"]) == ["qa"]
    assert ops._touches(["apps/harbourbank/base/a.yaml"]) == ["dev", "prod", "qa"]
    assert ops._touches(["bootstrap/lab-start.sh", "README.md"]) == [
        "docs/other (not deployed)", "lab bootstrap (not deployed by ArgoCD)"]


# --- 6. ArgoCD's own events, confined to the Harbour Bank apps -------------------------------------

def test_argocd_events_are_confined_to_the_harbour_bank_applications(fake, kube):
    fake.loki_result = loki_line('name=harbourbank-dev kind=Application reason=ResourceUpdated '
                                 'msg="Updated health status: Progressing -> Degraded"', namespace="argocd")
    result = ops.search_logs(kube, env="dev", source="argocd", since_minutes=3 * 24 * 60)
    query = fake.requests[-1].url.params["query"]
    assert query == '{namespace="argocd", job="kubernetes-events"} |~ "^name=harbourbank-dev kind=Application "'
    assert result["lines"][0]["env"] == "dev" and "Degraded" in result["lines"][0]["line"]


# --- 7. where a pod's image came from -----------------------------------------------------------

def test_describe_workload_says_whether_the_image_was_downloaded_or_already_on_the_node(fake, kube):
    fake.routes["/api/v1/namespaces/harbourbank-dev/pods"] = {"items": [pod("accounts-api-new"),
                                                                        pod("accounts-api-old")]}
    fake.loki[r"name=accounts-api-new "] = loki_line(
        r'name=accounts-api-new kind=Pod reason=Pulled msg="Successfully pulled image \"ghcr.io/x@sha256:ab\" '
        r'in 12m41.605s (12m41.605s including waiting). Image size: 73717528 bytes."')
    fake.loki[r"name=accounts-api-old "] = loki_line(
        r'name=accounts-api-old kind=Pod reason=Pulled msg="Container image \"ghcr.io/x\" already present on machine"')

    pods = {p["pod"]: p["image_pull"] for p in ops.describe_workload(kube, "dev", "accounts-api")["pods"]}

    assert pods["accounts-api-new"]["source"].startswith("downloaded")
    assert pods["accounts-api-new"]["took"] == "12m41.605s"
    assert pods["accounts-api-old"]["source"] == "already on the node (no download)"
