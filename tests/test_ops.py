import time
from datetime import UTC, datetime, timedelta

import anyio
import pytest

from conftest import commit, deployment, pod, ts
from harbourbank_ops_mcp import ops, server
from harbourbank_ops_mcp.safety import AuditLog


def call_every_tool(kube, gh, tmp_path):
    """Call each tool once through the real MCP server, as Claude Desktop would."""
    mcp = server.build(kube, gh, AuditLog(tmp_path / "audit.jsonl"))
    calls = {
        "bank_status": {}, "find_problems": {}, "describe_workload": {"env": "prod", "name": "accounts-api"},
        "get_logs": {"env": "prod", "workload": "accounts-api"}, "search_logs": {"contains": "error"},
        "query_metrics": {"promql": "up"}, "recent_changes": {"env": "prod"},
        "compare_environments": {"ahead": "qa", "behind": "prod"}, "end_of_day_status": {"env": "prod"},
        "change_audit": {},
    }

    async def run():
        names = {t.name for t in await mcp.list_tools()}
        assert names == set(calls)
        for name, args in calls.items():
            await mcp.call_tool(name, args)
    anyio.run(run)


# --- read-only, by construction ---------------------------------------------------------

def test_every_tool_only_ever_reads(fake, kube, gh, tmp_path):
    fake.routes["/repos/kuwid73/harbourbank-gitops/rulesets/rule-suites"] = []
    fake.routes["/repos/kuwid73/harbourbank-gitops/commits/60decb3abcdef"] = {
        "commit": {"message": "prod: accounts-api sha-493df0c", "committer": {"date": ts()}, "author": {"name": "x"}}}

    call_every_tool(kube, gh, tmp_path)

    methods = {(r.method, r.url.path) for r in fake.requests}
    assert methods, "no requests were made"
    assert {m for m, _ in methods} == {"GET"}, f"non-GET requests: {[x for x in methods if x[0] != 'GET']}"


def test_every_tool_call_is_audited(fake, kube, gh, tmp_path):
    call_every_tool(kube, gh, tmp_path)
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 10


# --- scope ------------------------------------------------------------------------------

@pytest.mark.parametrize("env", ["staging", "harbourbank-prod", "kube-system", ""])
def test_unknown_environments_are_refused(kube, env):
    with pytest.raises(ValueError, match="unknown environment"):
        ops.get_logs(kube, env, "accounts-api")


@pytest.mark.parametrize("name", ["../secrets", "accounts-api/../../secrets", "a b", "UPPER", "x?watch=1", ""])
def test_object_names_cannot_escape_their_path(kube, name):
    with pytest.raises(ValueError, match="not a valid Kubernetes object name"):
        ops.describe_workload(kube, "prod", name)


def test_log_search_is_confined_to_harbour_bank_and_escapes_user_text(fake, kube):
    ops.search_logs(kube, contains='"} or {namespace="kube-system', since_minutes=60)
    query = fake.requests[-1].url.params["query"]
    assert query.startswith('{namespace=~"harbourbank-dev|harbourbank-qa|harbourbank-prod", '
                            'job="loki.source.kubernetes.pods"}')
    # The injection attempt stays inside one quoted string literal
    assert query.endswith(r' |= "\"} or {namespace=\"kube-system"')


def test_event_search_uses_the_event_stream(fake, kube):
    ops.search_logs(kube, env="dev", source="events")
    assert fake.requests[-1].url.params["query"] == '{namespace="harbourbank-dev", job="kubernetes-events"}'


# --- bounded ----------------------------------------------------------------------------

def test_logs_are_capped_and_the_window_is_bounded(fake, kube):
    fake.logs["accounts-api-1"] = "\n".join(f"line {i}" for i in range(2000))
    result = ops.get_logs(kube, "prod", "accounts-api", since_minutes=10_000, max_lines=10_000)
    log_request = next(r for r in fake.requests if r.url.path.endswith("/log"))
    assert log_request.url.params["sinceSeconds"] == str(24 * 60 * 60)
    lines = result["pods"][0]["lines"]
    assert len(lines) == ops.MAX_LOG_LINES and lines[-1] == "line 1999"
    assert result["pods"][0]["truncated"] is True


def test_logs_filter_is_case_insensitive(fake, kube):
    fake.logs["accounts-api-1"] = "INFO ok\nERROR boom\nerror again"
    result = ops.get_logs(kube, "prod", "accounts-api", contains="Error")
    assert result["pods"][0]["lines"] == ["ERROR boom", "error again"]


def test_metric_series_are_capped(fake, kube):
    fake.prometheus["up"] = [{"metric": {"i": str(i)}, "value": [0, "1"]} for i in range(200)]
    result = ops.query_metrics(kube, "up")
    assert result["series_total"] == 200 and len(result["series"]) == ops.MAX_SERIES


# --- diagnosis --------------------------------------------------------------------------

def test_find_problems_reports_a_healthy_bank_as_healthy(kube):
    assert ops.find_problems(kube)["healthy"] is True


def test_find_problems_spots_crashes_failed_jobs_and_stuck_rollouts(fake, kube):
    finished = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake.routes["/api/v1/namespaces/harbourbank-prod/pods"] = {"items": [
        pod("accounts-api-2", ready=False, waiting="CrashLoopBackOff", restarts=4,
            last={"reason": "Error", "exitCode": 1, "finishedAt": finished})]}
    fake.routes["/apis/batch/v1/namespaces/harbourbank-prod/jobs"] = {"items": [
        {"metadata": {"name": "end-of-day-29"}, "status": {"conditions": [
            {"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded", "message": "3 failures"}]}}]}
    fake.routes["/apis/apps/v1/namespaces/harbourbank-prod/deployments"] = {"items": [deployment(conditions=[
        {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}])]}
    fake.prometheus["ALERTS"] = [{"metric": {"alertname": "AccountsApiUnavailable", "namespace": "harbourbank-prod",
                                             "severity": "critical"}, "value": [0, "1"]},
                                 {"metric": {"alertname": "Watchdog"}, "value": [0, "1"]}]

    result = ops.find_problems(kube, "prod")

    summaries = {(p["kind"], p["summary"]) for p in result["problems"]}
    assert result["healthy"] is False
    assert ("pod", "container waiting: CrashLoopBackOff") in summaries
    assert ("job", "Job failed: BackoffLimitExceeded") in summaries
    assert ("deployment", "rollout stuck: ProgressDeadlineExceeded") in summaries
    assert ("alert", "alert firing (critical)") in summaries
    assert not any(p["object"] == "Watchdog" for p in result["problems"])   # always fires by design
    crash = next(p for p in result["problems"] if p["summary"].startswith("container waiting"))
    assert crash["next_step"] == "get_logs(env='prod', workload='accounts-api-2', previous=True)"


def test_alerts_from_other_namespaces_are_out_of_scope(fake, kube):
    fake.prometheus["ALERTS"] = [{"metric": {"alertname": "KubeControllerDown", "namespace": "kube-system"},
                                  "value": [0, "1"]}]
    assert ops.bank_status(kube)["cluster_alerts"] == []


def test_end_of_day_status_reads_the_published_results(fake, kube):
    midnight = datetime.combine(datetime.now(UTC).date() - timedelta(days=1), datetime.min.time(), UTC)
    fake.prometheus["bank_ledger_imbalance_cents"] = [{"metric": {"namespace": "harbourbank-prod"}, "value": [0, "0"]}]
    fake.prometheus["bank_ledger_inconsistencies"] = [{"metric": {"namespace": "harbourbank-prod"}, "value": [0, "0"]}]
    fake.prometheus["bank_eod_last_business_date"] = [{"metric": {"namespace": "harbourbank-prod"},
                                                       "value": [0, str(midnight.timestamp())]}]
    fake.loki_result = [{"stream": {"namespace": "harbourbank-prod", "pod": "end-of-day-1-abc"}, "values": [
        [str(int(datetime.now(UTC).timestamp() * 1e9)), '{"msg": "business day closed", "interest_cents": 3180}']]}]
    result = ops.end_of_day_status(kube, "prod")
    loki_query = next(r for r in fake.requests if "/proxy/loki/" in r.url.path).url.params["query"]
    assert 'app="end-of-day"' in loki_query
    assert result["run_results_newest_first"] == [
        {"msg": "business day closed", "interest_cents": 3180, "pod": "end-of-day-1-abc"}]
    assert result["books_balance"] is True
    assert result["yesterday_closed"] is True
    assert result["metrics"]["last_business_day"] == midnight.date().isoformat()


def test_end_of_day_status_before_any_run(kube):
    result = ops.end_of_day_status(kube, "dev")
    assert result["books_balance"] is None and any("no end-of-day run" in n for n in result["notes"])


def test_change_audit_keeps_the_newest_push_first_when_lookups_finish_out_of_order(fake, gh):
    repo = "/repos/kuwid73/harbourbank-gitops"
    fake.routes[f"{repo}/rulesets/rule-suites"] = [
        {"pushed_at": ts(i), "actor_name": "kuwid73", "result": "bypass", "before_sha": f"b{i}", "after_sha": f"a{i}"}
        for i in range(5)]

    def pulls_for(number, delay):
        def route(request):
            time.sleep(delay)   # the newest push's lookup finishes last
            return [{"number": number}]
        return route

    for i in range(5):
        n = 20 - i
        fake.routes[f"{repo}/commits/a{i}"] = commit(f"change {i}", files=["apps/harbourbank/base/x.yaml"])
        fake.routes[f"{repo}/commits/a{i}/pulls"] = pulls_for(n, 0.2 if i == 0 else 0)
        fake.routes[f"{repo}/pulls/{n}"] = {"title": f"PR {n}", "user": {"login": "kuwid73"},
                                            "merged_by": {"login": "kuwid73"}, "merged_at": ts(i)}
        fake.routes[f"{repo}/pulls/{n}/reviews"] = [{"user": {"login": "reviewer"}, "state": "APPROVED"}]

    result = ops.change_audit(gh)

    assert [(p["change"], p["pull_request"]["number"]) for p in result["pushes_to_main"]] == [
        (f"b{i} -> a{i}", 20 - i) for i in range(5)]
    assert result["pushes_to_main"][0]["pull_request"]["reviews"] == ["reviewer: APPROVED"]


# --- identity ---------------------------------------------------------------------------

def test_server_refuses_to_start_with_any_other_identity(fake, kube, monkeypatch, tmp_path):
    fake.identity = "kubernetes-admin"
    (tmp_path / "kubeconfig").write_text("unused")
    monkeypatch.setenv("HBOPS_KUBECONFIG", str(tmp_path / "kubeconfig"))
    monkeypatch.setenv("HBOPS_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(server.Kube, "from_kubeconfig", classmethod(lambda cls, path: kube))
    with pytest.raises(SystemExit, match="refusing to start as 'kubernetes-admin'"):
        server.main()
