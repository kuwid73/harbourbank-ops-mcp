"""Delivery metrics (DORA): the calculator, the CSV exchange format, the scenario simulator, and the
live source that reads them from Git."""
import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from conftest import NOW, ts
from harbourbank_ops_mcp import delivery, ops, simulate
from harbourbank_ops_mcp.delivery import Deployment, Incident

T0 = datetime(2026, 10, 1, tzinfo=UTC)
SCENARIOS = Path(__file__).parent.parent / "scenarios"


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


# --- the calculator ------------------------------------------------------------------------------

def test_the_four_metrics_with_their_evidence():
    deployments = [Deployment("d1", "prod", at(24), (at(0), at(12))),
                   Deployment("d2", "prod", at(48), (at(40),)),
                   Deployment("q1", "qa", at(30), (at(1),))]                    # another environment
    incidents = [Incident("i1", "prod", started=at(48), detected=at(48.25), restored=at(49), caused_by="d2"),
                 Incident("i2", "prod", started=at(60), detected=at(60), restored=at(62))]   # not a change

    m = delivery.metrics(deployments, incidents, "prod", T0, at(7 * 24))

    assert m["deployment_frequency"]["deployments"] == 2 and m["deployment_frequency"]["per_week"] == 2
    assert m["lead_time_for_changes_hours"]["median"] == 12 and m["lead_time_for_changes_hours"]["count"] == 3
    assert m["change_failure_rate"] == {"failed_deployments": 1, "deployments": 2, "rate": 0.5,
                                        "level": "low (indicative)", "failed": ["d2"]}
    assert m["time_to_restore_hours"]["median"] == 1 and m["time_to_restore_hours"]["level"] == "elite (indicative)"
    assert m["time_to_restore_hours"]["time_to_detect"]["median"] == 0.25
    assert [i["id"] for i in m["other_incidents"]] == ["i2"]
    assert any("only 2 deployments" in n for n in m["notes"])   # too few to call it a baseline
    assert any("only 7.0 days" in n for n in m["notes"])
    assert next(iter(m)) == "read_first" and m["read_first"].startswith("not a baseline yet")


def test_a_baseline_quotes_its_levels_plainly():
    deployments = [Deployment(f"d{n}", "prod", at(24 * 3 * n + 1), (at(24 * 3 * n),)) for n in range(12)]
    m = delivery.metrics(deployments, [], "prod", T0, at(24 * 35))
    assert "read_first" not in m and m["lead_time_for_changes_hours"]["level"] == "elite"


def test_reference_levels():
    assert delivery.level("deployment_frequency_per_week", 10) == "elite"
    assert delivery.level("deployment_frequency_per_week", 1) == "high"
    assert delivery.level("deployment_frequency_per_week", 0.1) == "low"
    assert delivery.level("lead_time_hours", 3) == "elite"
    assert delivery.level("lead_time_hours", 24 * 40) == "low"
    assert delivery.level("change_failure_rate", 0.12) == "medium"
    assert delivery.level("restore_hours", 30) == "medium"


def test_csv_round_trip(tmp_path):
    deployments = [Deployment("d1", "prod", at(24), (at(0), at(12)), kind="release", title="Release 1"),
                   Deployment("d2", "prod", at(48), ())]
    incidents = [Incident("i1", "prod", at(48), at(49), None, caused_by="d2", title="still open")]
    delivery.write_csv(tmp_path, deployments, incidents, delivery.Effort({"testing": 4.0}))

    back, back_incidents, effort = delivery.read_csv(tmp_path)

    assert back == deployments and back_incidents == incidents and effort.hours == {"testing": 4.0}


def test_csv_times_need_a_utc_offset(tmp_path):
    (tmp_path / "deployments.csv").write_text("deployment_id,env,deployed_at,kind,title,change_made_at\n"
                                              "d1,prod,2026-10-01T10:00:00,,,\n")
    try:
        delivery.read_csv(tmp_path)
    except ValueError as exc:
        assert "no UTC offset" in str(exc)
    else:
        raise AssertionError("a time without an offset was accepted")


# --- the simulator ------------------------------------------------------------------------------

def test_a_change_waits_for_the_first_window_whose_approval_cutoff_it_makes():
    release = {"weekdays": [3], "hour_utc": 18, "approval_cutoff_hours": 72}
    monday = datetime(2026, 1, 5, 9, tzinfo=UTC)
    assert simulate._next_window(monday, release) == datetime(2026, 1, 8, 18, tzinfo=UTC)      # this Thursday
    tuesday = monday + timedelta(days=1)
    assert simulate._next_window(tuesday, release) == datetime(2026, 1, 15, 18, tzinfo=UTC)    # missed the board


def test_a_run_is_reproducible_and_stays_in_its_window():
    scenario = simulate.load(SCENARIOS / "1-manual-release-train.yaml")
    a, b = simulate.run(scenario, 90, seed=7), simulate.run(scenario, 90, seed=7)
    assert a.deployments == b.deployments and a.incidents == b.incidents
    assert all(a.start <= d.deployed_at < a.end for d in a.deployments)
    assert all(d.deployed_at.weekday() == 3 and d.deployed_at.hour == 18 for d in a.deployments)


def test_batched_releases_fail_more_often_than_single_changes():
    scenario = simulate.load(SCENARIOS / "1-manual-release-train.yaml")
    single = scenario | {"release": {"mode": "on_demand", "deploy_hours": {"median": 1, "p90": 2}}}
    batched = simulate.compare(scenario, 90, runs=40)["metrics"]["change_failure_rate"]["median"]
    one_by_one = simulate.compare(single, 90, runs=40)["metrics"]["change_failure_rate"]["median"]
    assert batched > 5 * one_by_one   # same faulty changes, more of them per release


def test_every_shipped_scenario_loads_and_compares():
    results = [simulate.compare(simulate.load(p), 30, runs=5) for p in sorted(SCENARIOS.glob("*.yaml"))]
    assert len(results) == 3 and all(r["illustrative"] for r in results)
    assert simulate.render_comparison(results).startswith("ILLUSTRATIVE")


# --- the live source: Git, GitHub and the incident records -----------------------------------------

GITOPS = "/repos/kuwid73/harbourbank-gitops"
APP = "/repos/kuwid73/harbourbank-app"
HUMAN = {"name": "Kumudu Widyaratne", "email": "53670161+kuwid73@users.noreply.github.com"}
BOT = {"name": "harbourbank-promote", "email": "41898282+github-actions[bot]@users.noreply.github.com"}


def landed(sha, title, minutes_ago, files, author=HUMAN, parent="p"):
    return {"sha": sha, "commit": {"message": title, "committer": {"date": ts(minutes_ago)},
                                   "author": author | {"date": ts(minutes_ago)}},
            "files": [{"filename": f} for f in files], "parents": [{"sha": parent}]}


def pr_commits(author, minutes_ago):
    return [{"commit": {"author": author | {"date": ts(minutes_ago)}}}]


def contents(text):
    return {"content": base64.b64encode(text.encode()).decode()}


def overlay(tag):
    return contents(yaml.safe_dump({"images": [{"name": ops.IMAGE, "newTag": tag, "digest": "sha256:x"}]}))


def test_delivery_metrics_reads_deployments_from_git_and_failures_from_incident_records(fake, kube, gh):
    commits = [landed("c4", "README tweak (#24)", 60, ["README.md"]),
               landed("c3", "Add image caches (#23)", 600, ["bootstrap/lab-start.sh"]),
               landed("c2", "prod: accounts-api sha-bbbbbbb (#22)", 1440, ["apps/harbourbank/overlays/prod/"
                                                                              "kustomization.yaml"], author=BOT,
                      parent="c1"),
               landed("c1", "Raise the pool size (#21)", 2880, ["apps/harbourbank/base/accounts-api.yaml"])]
    fake.routes[f"{GITOPS}/commits"] = commits
    for c in commits:
        fake.routes[f"{GITOPS}/commits/{c['sha']}"] = c
    fake.routes[f"{GITOPS}/pulls/21/commits"] = pr_commits(HUMAN, 3000)         # written 2 h before merging
    fake.routes[f"{GITOPS}/pulls/22/commits"] = pr_commits(BOT, 1500)           # the pipeline's: not a change
    fake.routes[f"{GITOPS}/pulls/23/commits"] = pr_commits(HUMAN, 700)
    fake.routes[f"{GITOPS}/contents/apps/harbourbank/overlays/prod/kustomization.yaml"] = \
        lambda r: overlay({"c1": "sha-aaaaaaa", "c2": "sha-bbbbbbb"}[r.url.params["ref"]])
    fake.routes[f"{APP}/compare/aaaaaaa...bbbbbbb"] = {"commits": [
        {"commit": {"message": "Retry the database connection (#5)", "author": {"date": ts(1400)}}}]}
    fake.routes[f"{APP}/pulls/5/commits"] = pr_commits(HUMAN, 4320)              # the app change: 3 days old
    fake.routes[f"{GITOPS}/contents/incidents"] = [{"type": "file", "name": "x.yaml", "path": "incidents/x.yaml"}]
    fake.routes[f"{GITOPS}/contents/incidents/x.yaml"] = contents(yaml.safe_dump({
        "id": "2026-x", "title": "caches took the node addresses", "environments": ["dev", "qa", "prod"],
        "started": NOW - timedelta(minutes=480), "detected": ts(479), "restored": ts(476),   # a YAML datetime
        "caused_by": "gitops#23"}))
    fake.routes["/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/harbourbank-prod"] = {
        "status": {"history": [{"revision": "c2", "deployedAt": ts(1437)}]}}   # synced 3 minutes after merge
    fake.loki[r"argocd"] = [{"stream": {"namespace": "argocd"}, "values": [
        [str(int((NOW - timedelta(minutes=120)).timestamp() * 1e9)),
         'name=harbourbank-prod kind=Application msg="Updated health status: Progressing -> Degraded"']]}]

    m = ops.delivery_metrics(kube, gh, "prod", days=7)

    by_id = {d["id"]: d for d in m["deployments_newest_first"]}
    assert set(by_id) == {"gitops#21", "gitops#22", "gitops#23"}           # the README change deploys nothing
    assert by_id["gitops#21"]["kind"] == "config" and by_id["gitops#21"]["lead_time_hours"] == 2
    assert by_id["gitops#22"]["kind"] == "app version"
    assert by_id["gitops#22"]["version"] == {"from": "sha-aaaaaaa", "to": "sha-bbbbbbb"}
    assert by_id["gitops#22"]["deployed_at_from"] == "ArgoCD sync"
    assert by_id["gitops#22"]["changes"] == 1 and by_id["gitops#22"]["lead_time_hours"] == round(
        (4320 - 1437) / 60, 2)                                                # from the app PR's commit
    assert by_id["gitops#23"]["kind"] == "lab tooling" and by_id["gitops#23"]["caused_incident"] == "2026-x"
    assert m["change_failure_rate"]["failed"] == ["gitops#23"]
    assert m["time_to_restore_hours"]["median"] == round(4 / 60, 2)
    assert [u["time"][:16] for u in m["possible_unrecorded_failures"]] == [ts(120)[:16]]


def test_platform_changes_can_be_left_out(fake, kube, gh):
    c = landed("c3", "Add image caches (#23)", 600, ["bootstrap/lab-start.sh"])
    fake.routes[f"{GITOPS}/commits"] = [c]
    fake.routes[f"{GITOPS}/commits/c3"] = c
    fake.routes[f"{GITOPS}/pulls/23/commits"] = pr_commits(HUMAN, 700)
    assert ops.delivery_metrics(kube, gh, "prod", include_platform=False)["deployment_frequency"]["deployments"] == 0
    assert ops.delivery_metrics(kube, gh, "prod")["deployment_frequency"]["deployments"] == 1
