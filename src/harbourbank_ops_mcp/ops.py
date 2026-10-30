"""What each tool does. Pure functions of the API clients, so tests can feed them canned responses.

Scope is fixed here, not left to the model: Harbour Bank's three namespaces, ArgoCD's apps for
them, and the two Harbour Bank repositories. Every list is capped and every window bounded.
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .clients import ApiError, GitHub, Kube

Env = Literal["dev", "qa", "prod"]
ENVS: dict[str, str] = {"dev": "harbourbank-dev", "qa": "harbourbank-qa", "prod": "harbourbank-prod"}
REPOS = {"gitops": "kuwid73/harbourbank-gitops", "app": "kuwid73/harbourbank-app"}
PROMETHEUS = ("monitoring", "kube-prometheus-stack-prometheus:9090")
LOKI = ("monitoring", "loki:3100")
API = "accounts-api"

MAX_LOG_LINES = 500
MAX_WINDOW_MINUTES = 24 * 60
MAX_SERIES = 50
MAX_POINTS = 120
AUDIT_WORKERS = 8         # parallel GitHub lookups, well under its rate limits
QUIET_ALERTS = {"Watchdog", "InfoInhibitor"}    # always firing by design
NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")   # a Kubernetes object name


def _ns(env: str) -> str:
    if env not in ENVS:
        raise ValueError(f"unknown environment {env!r}; use one of {', '.join(ENVS)}")
    return ENVS[env]


def _name(value: str) -> str:
    if not NAME.match(value):
        raise ValueError(f"{value!r} is not a valid Kubernetes object name")
    return value


def _now() -> datetime:
    return datetime.now(UTC)


def _age(timestamp: str | None) -> str | None:
    if not timestamp:
        return None
    seconds = int((_now() - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _utc_date(timestamp: float) -> str:
    """Business days are UTC: a midnight timestamp must not shift a day in the local zone."""
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


def _split_image(image: str) -> dict:
    name, _, digest = image.partition("@")
    repo, _, tag = name.rpartition(":")
    return {"repository": repo, "tag": tag, "digest": digest or None}


def _items(kube: Kube, path: str, **params: Any) -> list[dict]:
    return kube.get(path, **params).get("items", [])


def _touches(paths: list[str]) -> list[str]:
    """Where a GitOps change lands, from its file paths. base/ renders into every environment."""
    where = set()
    for path in paths:
        parts = path.split("/")
        if path.startswith("apps/harbourbank/overlays/") and len(parts) > 4:
            where.add(parts[3])
        elif path.startswith("apps/harbourbank/base/"):
            where.update(ENVS)
        elif path.startswith("platform/"):
            where.add("platform")
        elif path.startswith("bootstrap/"):
            where.add("lab bootstrap (not deployed by ArgoCD)")
        else:
            where.add("docs/other (not deployed)")
    return sorted(where)


def _maker(author_name: str, author_email: str, author_type: str | None = None) -> str:
    """'bot' for CI and GitHub Actions, else 'human'. A push's GitHub actor is the credential's owner
    (a deploy key pushes as the repo owner), so the commit author is what tells them apart."""
    if author_type == "Bot" or "[bot]" in author_email or author_name in ("harbourbank-ci", "github-actions[bot]"):
        return "bot"
    return "human"


# --- Prometheus -------------------------------------------------------------------------

def _prom(kube: Kube, query: str) -> list[dict]:
    body = kube.proxy(*PROMETHEUS, "api/v1/query", query=query)
    if body.get("status") != "success":
        raise ApiError(400, body.get("error", "query failed"))
    return body["data"]["result"]


def _by_namespace(kube: Kube, query: str) -> dict[str, float]:
    return {r["metric"].get("namespace", ""): float(r["value"][1]) for r in _prom(kube, query)}


def _firing_alerts(kube: Kube) -> list[dict]:
    alerts = []
    for r in _prom(kube, 'ALERTS{alertstate="firing"}'):
        labels = r["metric"]
        if labels.get("alertname") in QUIET_ALERTS:
            continue
        namespace = labels.get("namespace")
        if namespace and namespace not in ENVS.values():
            continue   # other namespaces are platform concerns outside this server's scope
        alerts.append({"alert": labels.get("alertname"), "namespace": namespace,
                       "severity": labels.get("severity"),
                       "labels": {k: v for k, v in labels.items()
                                  if k in ("pod", "deployment", "job_name", "node", "container")}})
    return alerts


# --- building blocks --------------------------------------------------------------------

def _argo_app(kube: Kube, env: str) -> dict:
    app = kube.get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/harbourbank-{env}")
    status = app.get("status", {})
    operation = status.get("operationState", {})
    return {
        "sync": status.get("sync", {}).get("status"),
        "health": status.get("health", {}).get("status"),
        "git_revision": (status.get("sync", {}).get("revision") or "")[:7],
        "last_sync": {
            "phase": operation.get("phase"),
            "finished": operation.get("finishedAt"),
            "message": operation.get("message"),
            "hooks": [{"kind": r.get("kind"), "name": r.get("name"), "type": r.get("hookType"),
                       "phase": r.get("hookPhase")}
                      for r in operation.get("syncResult", {}).get("resources", []) if r.get("hookType")],
        },
    }


def _pod(pod: dict) -> dict:
    status = pod.get("status", {})
    containers = status.get("containerStatuses", [])
    summary = {
        "pod": pod["metadata"]["name"],
        "phase": status.get("phase"),
        "ready": bool(containers) and all(c.get("ready") for c in containers),
        "restarts": sum(c.get("restartCount", 0) for c in containers),
        "node": pod.get("spec", {}).get("nodeName"),
        "age": _age(pod["metadata"].get("creationTimestamp")),
    }
    for c in containers:
        if waiting := c.get("state", {}).get("waiting"):
            summary["waiting"] = {"reason": waiting.get("reason"), "message": (waiting.get("message") or "")[:300]}
        if last := c.get("lastState", {}).get("terminated"):
            summary["last_terminated"] = {"reason": last.get("reason"), "exit_code": last.get("exitCode"),
                                          "finished": last.get("finishedAt")}
    if status.get("phase") == "Pending":
        for cond in status.get("conditions", []):
            if cond.get("status") == "False" and cond.get("message"):
                summary["pending_reason"] = cond["message"][:300]
    return summary


def _selector(match_labels: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(match_labels.items()))


def _events(kube: Kube, namespace: str, name_prefix: str | None = None, limit: int = 20) -> list[dict]:
    events = _items(kube, f"/api/v1/namespaces/{namespace}/events")
    if name_prefix:
        events = [e for e in events if e.get("involvedObject", {}).get("name", "").startswith(name_prefix)]
    events.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
    return [{"when": e.get("lastTimestamp") or e.get("eventTime"), "type": e.get("type"), "reason": e.get("reason"),
             "object": f"{e['involvedObject'].get('kind')}/{e['involvedObject'].get('name')}",
             "count": e.get("count"), "message": (e.get("message") or "")[:300]} for e in events[:limit]]


# --- tools ------------------------------------------------------------------------------

def bank_status(kube: Kube) -> dict:
    imbalance = _by_namespace(kube, "max by (namespace) (bank_ledger_imbalance_cents)")
    closed = _by_namespace(kube, "max by (namespace) (bank_eod_last_business_date_timestamp_seconds)")
    alerts = _firing_alerts(kube)
    environments = {}
    for env, ns in ENVS.items():
        deploy = kube.get(f"/apis/apps/v1/namespaces/{ns}/deployments/{API}")
        db = kube.get_or_none(f"/apis/postgresql.cnpg.io/v1/namespaces/{ns}/clusters/accounts-db") or {}
        environments[env] = {
            "argocd": _argo_app(kube, env),
            "accounts_api": {
                "image": _split_image(deploy["spec"]["template"]["spec"]["containers"][0]["image"]),
                "replicas_ready": f"{deploy.get('status', {}).get('readyReplicas', 0)}"
                                  f"/{deploy['spec'].get('replicas')}",
            },
            "database": {"status": db.get("status", {}).get("phase"),
                         "instances_ready": f"{db.get('status', {}).get('readyInstances', 0)}"
                                            f"/{db.get('spec', {}).get('instances')}"},
            "end_of_day": {
                "last_business_day_closed": _utc_date(closed[ns]) if ns in closed else None,
                "ledger_imbalance_cents": imbalance.get(ns),
            },
            "firing_alerts": [a["alert"] for a in alerts if a["namespace"] == ns],
        }
    return {"checked_at": _now().isoformat(timespec="seconds"), "environments": environments,
            "cluster_alerts": [a for a in alerts if not a["namespace"]]}


def find_problems(kube: Kube, env: str | None = None) -> dict:
    envs = [env] if env else list(ENVS)
    problems: list[dict] = []

    def add(severity, where, kind, obj, summary, next_step=None, **evidence):
        problem = {"severity": severity, "env": where, "kind": kind, "object": obj, "summary": summary}
        if evidence:
            problem["evidence"] = evidence
        if next_step:
            problem["next_step"] = next_step
        problems.append(problem)

    for e in envs:
        ns = _ns(e)
        app = _argo_app(kube, e)
        if app["sync"] != "Synced" or app["health"] != "Healthy" or app["last_sync"]["phase"] in ("Failed", "Error"):
            add("high", e, "argocd", f"harbourbank-{e}", f"ArgoCD app is {app['sync']}/{app['health']}, "
                f"last sync {app['last_sync']['phase']}", message=app["last_sync"]["message"])
        for pod in _items(kube, f"/api/v1/namespaces/{ns}/pods"):
            p = _pod(pod)
            if p["phase"] == "Succeeded":
                continue
            if "waiting" in p and p["waiting"]["reason"] not in (None, "ContainerCreating", "PodInitializing"):
                add("high", e, "pod", p["pod"], f"container waiting: {p['waiting']['reason']}",
                    f"get_logs(env='{e}', workload='{p['pod']}', previous=True)", **p)
            elif p["phase"] == "Pending":
                add("medium", e, "pod", p["pod"], "pod is Pending", f"describe_workload(env='{e}', name='{p['pod']}')",
                    **p)
            elif p["phase"] == "Running" and not p["ready"]:
                add("medium", e, "pod", p["pod"], "running but not ready",
                    f"get_logs(env='{e}', workload='{p['pod']}')", **p)
            last = p.get("last_terminated")
            if last and last.get("finished") and _now() - datetime.fromisoformat(
                    last["finished"].replace("Z", "+00:00")) < timedelta(hours=1):
                add("medium", e, "pod", p["pod"], f"restarted in the last hour ({last['reason']}, "
                    f"exit {last['exit_code']})", f"get_logs(env='{e}', workload='{p['pod']}', previous=True)")
        for job in _items(kube, f"/apis/batch/v1/namespaces/{ns}/jobs"):
            for cond in job.get("status", {}).get("conditions", []):
                if cond.get("type") == "Failed" and cond.get("status") == "True":
                    add("high", e, "job", job["metadata"]["name"], f"Job failed: {cond.get('reason')}",
                        f"get_logs(env='{e}', workload='{job['metadata']['name']}')", message=cond.get("message"))
        for deploy in _items(kube, f"/apis/apps/v1/namespaces/{ns}/deployments"):
            for cond in deploy.get("status", {}).get("conditions", []):
                if cond.get("type") == "Progressing" and cond.get("status") == "False":
                    add("high", e, "deployment", deploy["metadata"]["name"], f"rollout stuck: {cond.get('reason')}",
                        f"describe_workload(env='{e}', name='{deploy['metadata']['name']}')",
                        message=cond.get("message"))
        db = kube.get_or_none(f"/apis/postgresql.cnpg.io/v1/namespaces/{ns}/clusters/accounts-db")
        if db and db.get("status", {}).get("phase") != "Cluster in healthy state":
            add("high", e, "database", "accounts-db", f"database cluster: {db.get('status', {}).get('phase')}")
        recent = [ev for ev in _events(kube, ns, limit=200) if ev["type"] == "Warning" and ev["when"]
                  and _now() - datetime.fromisoformat(ev["when"].replace("Z", "+00:00")) < timedelta(minutes=15)]
        if recent:
            add("low", e, "events", ns, f"{len(recent)} warning events in the last 15 minutes", events=recent[:10])

    for node in _items(kube, "/api/v1/nodes"):
        ready = next((c for c in node["status"].get("conditions", []) if c["type"] == "Ready"), {})
        if ready.get("status") != "True":
            add("high", None, "node", node["metadata"]["name"], f"node not Ready: {ready.get('reason')}")
    for alert in _firing_alerts(kube):
        if alert["namespace"] in [ENVS[x] for x in envs] or not alert["namespace"]:
            add("high" if alert["severity"] == "critical" else "medium",
                next((k for k, v in ENVS.items() if v == alert["namespace"]), None),
                "alert", alert["alert"], f"alert firing ({alert['severity']})", labels=alert["labels"])

    order = {"high": 0, "medium": 1, "low": 2}
    problems.sort(key=lambda p: order[p["severity"]])
    return {"checked_at": _now().isoformat(timespec="seconds"), "environments": envs,
            "healthy": not any(p["severity"] != "low" for p in problems), "problems": problems[:40]}


_PULLED = re.compile(r"Successfully pulled image .*? in ([0-9hms.]+) |(already present on machine)")


def _image_pull(kube: Kube, env: str, pod: str) -> dict:
    """From the pod's kubelet "Pulled" event (kept in Loki for 7 days). "already present on machine"
    means the node had the image, so no download or cache was involved."""
    found = search_logs(kube, env, contains=f"name={pod} kind=Pod ", regex="reason=Pulled ", source="events",
                        since_minutes=7 * MAX_WINDOW_MINUTES, limit=5)
    for entry in found["lines"]:
        if m := _PULLED.search(entry["line"]):
            if m.group(2):
                return {"source": "already on the node (no download)", "when": entry["time"]}
            return {"source": "downloaded (through the lab's pull-through cache when it has the image)",
                    "took": m.group(1), "when": entry["time"]}
    return {"source": "unknown (no Pulled event in the last 7 days)"}


def _pods_with_pulls(kube: Kube, env: str, pods: list[dict]) -> list[dict]:
    return [_pod(p) | {"image_pull": _image_pull(kube, env, p["metadata"]["name"])} for p in pods[:5]]


def describe_workload(kube: Kube, env: str, name: str) -> dict:
    ns, name = _ns(env), _name(name)
    if deploy := kube.get_or_none(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"):
        selector = _selector(deploy["spec"]["selector"].get("matchLabels", {}))
        return {"kind": "Deployment", "name": name,
                "images": [_split_image(c["image"]) for c in deploy["spec"]["template"]["spec"]["containers"]],
                "replicas": {k: deploy.get("status", {}).get(k, 0) for k in
                             ("replicas", "readyReplicas", "updatedReplicas", "unavailableReplicas")}
                | {"desired": deploy["spec"].get("replicas")},
                "conditions": [{k: c.get(k) for k in ("type", "status", "reason", "message")}
                               for c in deploy.get("status", {}).get("conditions", [])],
                "pods": _pods_with_pulls(kube, env, _items(kube, f"/api/v1/namespaces/{ns}/pods",
                                                           labelSelector=selector)),
                "events": _events(kube, ns, name)}
    if cron := kube.get_or_none(f"/apis/batch/v1/namespaces/{ns}/cronjobs/{name}"):
        jobs = [j for j in _items(kube, f"/apis/batch/v1/namespaces/{ns}/jobs")
                if j["metadata"]["name"].startswith(name)]
        jobs.sort(key=lambda j: j["metadata"]["creationTimestamp"], reverse=True)
        return {"kind": "CronJob", "name": name, "schedule": cron["spec"]["schedule"],
                "time_zone": cron["spec"].get("timeZone"), "suspended": cron["spec"].get("suspend", False),
                "last_schedule": cron.get("status", {}).get("lastScheduleTime"),
                "last_success": cron.get("status", {}).get("lastSuccessfulTime"),
                "recent_jobs": [_job(j) for j in jobs[:5]], "events": _events(kube, ns, name)}
    if job := kube.get_or_none(f"/apis/batch/v1/namespaces/{ns}/jobs/{name}"):
        pods = _items(kube, f"/api/v1/namespaces/{ns}/pods", labelSelector=f"batch.kubernetes.io/job-name={name}")
        return {"kind": "Job", **_job(job), "pods": [_pod(p) for p in pods], "events": _events(kube, ns, name)}
    if pod := kube.get_or_none(f"/api/v1/namespaces/{ns}/pods/{name}"):
        return {"kind": "Pod", **_pod(pod), "image_pull": _image_pull(kube, env, name),
                "images": [_split_image(c["image"]) for c in pod["spec"]["containers"]],
                "events": _events(kube, ns, name)}
    names = [d["metadata"]["name"] for d in _items(kube, f"/apis/apps/v1/namespaces/{ns}/deployments")]
    names += [c["metadata"]["name"] for c in _items(kube, f"/apis/batch/v1/namespaces/{ns}/cronjobs")]
    return {"error": f"no Deployment, CronJob, Job or Pod named {name!r} in {env}", "available": sorted(names)}


def _job(job: dict) -> dict:
    status = job.get("status", {})
    annotations = job["metadata"].get("annotations") or {}
    failed = next((c for c in status.get("conditions", []) if c["type"] == "Failed" and c["status"] == "True"), None)
    state = "failed" if failed else "succeeded" if status.get("succeeded") else "running" if status.get("active") \
        else "pending"
    scheduled = annotations.get("batch.kubernetes.io/cronjob-scheduled-timestamp")
    trigger = "schedule" if scheduled else "manual" if "cronjob.kubernetes.io/instantiate" in annotations else None
    summary = {"name": job["metadata"]["name"], "state": state, "trigger": trigger, "started": status.get("startTime"),
               "completed": status.get("completionTime"),
               # a Job retries in new pods: a "succeeded" Job may have failed attempts before it
               "failed_attempts": status.get("failed", 0)}
    if scheduled:
        summary["scheduled_for"] = scheduled
        if status.get("startTime"):
            late = datetime.fromisoformat(status["startTime"].replace("Z", "+00:00")) - \
                datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
            if late > timedelta(minutes=5):
                minutes = int(late.total_seconds()) // 60
                summary["started_late_by"] = f"{minutes // 60}h{minutes % 60:02d}m"
    if failed:
        summary["failure"] = {"reason": failed.get("reason"), "message": failed.get("message")}
    return summary


def _pods_for(kube: Kube, ns: str, workload: str) -> list[dict]:
    if pod := kube.get_or_none(f"/api/v1/namespaces/{ns}/pods/{workload}"):
        return [pod]
    if deploy := kube.get_or_none(f"/apis/apps/v1/namespaces/{ns}/deployments/{workload}"):
        selector = _selector(deploy["spec"]["selector"].get("matchLabels", {}))
    elif kube.get_or_none(f"/apis/batch/v1/namespaces/{ns}/jobs/{workload}"):
        selector = f"batch.kubernetes.io/job-name={workload}"
    else:
        return []
    pods = _items(kube, f"/api/v1/namespaces/{ns}/pods", labelSelector=selector)
    return sorted(pods, key=lambda p: p["metadata"]["creationTimestamp"], reverse=True)


def get_logs(kube: Kube, env: str, workload: str, since_minutes: int = 60, contains: str | None = None,
             max_lines: int = 200, previous: bool = False) -> dict:
    ns, workload = _ns(env), _name(workload)
    since_minutes = max(1, min(since_minutes, MAX_WINDOW_MINUTES))
    max_lines = max(1, min(max_lines, MAX_LOG_LINES))
    pods = _pods_for(kube, ns, workload)
    if not pods:
        return {"error": f"no running pods for {workload!r} in {env}",
                "hint": "a finished Job's pods may be gone; search_logs(app=...) has their logs for 7 days. "
                        "describe_workload lists the names that exist"}
    results = []
    for pod in pods[:3]:
        name = pod["metadata"]["name"]
        try:
            text = kube.text(f"/api/v1/namespaces/{ns}/pods/{name}/log", sinceSeconds=since_minutes * 60,
                             tailLines=2000, previous="true" if previous else None)
        except ApiError as exc:
            results.append({"pod": name, "error": str(exc)})
            continue
        lines = text.splitlines()
        if contains:
            lines = [line for line in lines if contains.lower() in line.lower()]
        results.append({"pod": name, "matched_lines": len(lines), "truncated": len(lines) > max_lines,
                        "lines": lines[-max_lines:]})
    return {"env": env, "workload": workload, "since_minutes": since_minutes, "previous_container": previous,
            "pods": results, **({"note": f"showing 3 of {len(pods)} pods"} if len(pods) > 3 else {})}


def _logql_string(value: str) -> str:
    return json.dumps(value)   # a JSON string literal is a valid, fully escaped LogQL string


def search_logs(kube: Kube, env: str | None = None, contains: str | None = None, regex: str | None = None,
                source: Literal["pods", "events", "argocd"] = "pods", app: str | None = None,
                since_minutes: int = 60, limit: int = 100) -> dict:
    """The query is built here, never taken from the model: Loki holds every namespace's logs, and
    this server may only show Harbour Bank's. source='argocd' reads ArgoCD's events about the
    harbourbank-<env> Applications only (health and sync changes), anchored on the event's object name."""
    if source == "argocd":
        apps = f"harbourbank-{env}" if env and _ns(env) else "harbourbank-(dev|qa|prod)"
        query = '{namespace="argocd", job="kubernetes-events"} |~ ' + _logql_string(f"^name={apps} kind=Application ")
    else:
        namespace = f'namespace="{_ns(env)}"' if env else f'namespace=~"{"|".join(ENVS.values())}"'
        parts = [namespace, 'job="kubernetes-events"' if source == "events" else 'job="loki.source.kubernetes.pods"']
        if app:
            parts.append(f'app="{_name(app)}"')
        query = "{" + ", ".join(parts) + "}"
    if contains:
        query += f" |= {_logql_string(contains)}"
    if regex:
        query += f" |~ {_logql_string(regex)}"
    since_minutes = max(1, min(since_minutes, 7 * MAX_WINDOW_MINUTES))
    limit = max(1, min(limit, MAX_LOG_LINES))
    end = time.time_ns()
    body = kube.proxy(*LOKI, "loki/api/v1/query_range", query=query, limit=limit, direction="backward",
                      start=end - since_minutes * 60 * 10**9, end=end)
    lines = []
    for stream in body.get("data", {}).get("result", []):
        labels = stream["stream"]
        for ts, line in stream["values"]:
            lines.append({"time": datetime.fromtimestamp(int(ts) / 1e9, UTC).isoformat(timespec="seconds"),
                          "env": next((k for k, v in ENVS.items() if v == labels.get("namespace")), None)
                          or next((k for k in ENVS if line.startswith(f"name=harbourbank-{k} ")), None),
                          "pod": labels.get("pod"), "line": line[:1000]})
    lines.sort(key=lambda x: x["time"], reverse=True)
    return {"query": query, "since_minutes": since_minutes, "returned": len(lines[:limit]),
            "lines": lines[:limit]}


def query_metrics(kube: Kube, promql: str, range_minutes: int = 0, step_seconds: int = 60) -> dict:
    if range_minutes <= 0:
        results = _prom(kube, promql)
        series = [{"labels": r["metric"], "value": float(r["value"][1])} for r in results[:MAX_SERIES]]
        return {"query": promql, "type": "instant", "series_total": len(results), "series": series}
    range_minutes = min(range_minutes, 7 * MAX_WINDOW_MINUTES)
    step_seconds = max(step_seconds, range_minutes * 60 // MAX_POINTS, 15)
    end = time.time()
    body = kube.proxy(*PROMETHEUS, "api/v1/query_range", query=promql, start=end - range_minutes * 60, end=end,
                      step=step_seconds)
    if body.get("status") != "success":
        raise ApiError(400, body.get("error", "query failed"))
    results = body["data"]["result"]
    series = [{"labels": r["metric"],
               "points": [[datetime.fromtimestamp(t, UTC).strftime("%H:%M"), float(v)] for t, v in r["values"]]}
              for r in results[:MAX_SERIES]]
    return {"query": promql, "type": "range", "range_minutes": range_minutes, "step_seconds": step_seconds,
            "series_total": len(results), "series": series}


# --- change history (GitHub) ------------------------------------------------------------

_PR_NUMBER = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+) ")


def _pull_request(gh: GitHub, repo: str, number: int) -> dict:
    pr = gh.get(f"/repos/{repo}/pulls/{number}")
    reviews = gh.get(f"/repos/{repo}/pulls/{number}/reviews")
    author = pr["user"]["login"]
    approvers = sorted({r["user"]["login"] for r in reviews if r["state"] == "APPROVED"} - {author})
    return {"number": number, "title": pr["title"], "author": author,
            "made_by": "bot (CI)" if _maker(author, "", pr["user"].get("type")) == "bot" else "human",
            "merged_by": (pr.get("merged_by") or {}).get("login"), "merged_at": pr.get("merged_at"),
            "reviews": [f"{r['user']['login']}: {r['state']}" for r in reviews],
            # GitHub forbids approving your own PR, so an approval here is always someone else's
            "independent_approval_by": approvers or None}


def _commit(gh: GitHub, repo: str, sha: str) -> dict:
    commit = gh.get(f"/repos/{repo}/commits/{sha}")
    title = commit["commit"]["message"].splitlines()[0]
    author = commit["commit"]["author"]
    entry = {"sha": sha[:7], "title": title, "date": commit["commit"]["committer"]["date"],
             "author": author["name"], "made_by": _maker(author["name"], author.get("email", "")),
             "touches": _touches([f["filename"] for f in commit.get("files", [])])}
    if m := _PR_NUMBER.search(title):
        number = int(m.group(1) or m.group(2))
    else:   # e.g. a branch commit inside a merge-commit PR; a direct push has none
        pulls = gh.get(f"/repos/{repo}/commits/{sha}/pulls")
        number = pulls[0]["number"] if pulls else None
    entry["pull_request"] = _pull_request(gh, repo, number) if number else "none: a direct push"
    return entry


def _parallel(fn, items: list) -> list:
    """GitHub lookups are independent; run them side by side. map() keeps the order."""
    with ThreadPoolExecutor(max_workers=AUDIT_WORKERS) as pool:
        return list(pool.map(fn, items))


def recent_changes(kube: Kube, gh: GitHub, env: str, days: int = 7) -> dict:
    _ns(env)
    days = max(1, min(days, 30))
    app = kube.get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/harbourbank-{env}")
    since = _now() - timedelta(days=days)
    history = [h for h in app.get("status", {}).get("history", [])
               if datetime.fromisoformat(h["deployedAt"].replace("Z", "+00:00")) >= since]
    every = app.get("status", {}).get("history", [])
    previous = {h["revision"]: every[i - 1]["revision"] for i, h in enumerate(every) if i > 0}

    def deployment(h: dict) -> dict:
        entry = {"deployed_at": h["deployedAt"], "git_revision": h["revision"][:7],
                 "started_by": "automated" if (h.get("initiatedBy") or {}).get("automated") else
                 (h.get("initiatedBy") or {}).get("username")}
        # A deployment ships every commit since the previous one, not just its head commit
        shas = [h["revision"]]
        if base := previous.get(h["revision"]):
            try:
                compare = gh.get(f"/repos/{REPOS['gitops']}/compare/{base}...{h['revision']}")
                shas = [c["sha"] for c in compare.get("commits", [])][-10:][::-1] or shas
            except ApiError:
                pass
        try:
            commits = [_commit(gh, REPOS["gitops"], sha) for sha in shas]
        except ApiError as exc:
            return entry | {"commit_error": str(exc)}
        entry["commits_newest_first"] = commits
        entry["changed_this_environment"] = [c["sha"] for c in commits if env in c["touches"]]
        return entry

    changes = _parallel(deployment, list(reversed(history[-20:])))
    return {"env": env, "days": days, "deployments": changes,
            "how_to_read": f"each deployment ships every GitOps commit since the previous one. "
                           f"'touches' says where each commit lands: a commit that doesn't list '{env}' "
                           f"changed nothing in {env}, even though it was part of the deployment. "
                           f"'made_by' bot = CI pipeline; 'independent_approval_by' = who approved it"}


def compare_environments(kube: Kube, gh: GitHub, ahead: str, behind: str) -> dict:
    images = {}
    for env in (ahead, behind):
        deploy = kube.get(f"/apis/apps/v1/namespaces/{_ns(env)}/deployments/{API}")
        images[env] = _split_image(deploy["spec"]["template"]["spec"]["containers"][0]["image"])
    a, b = images[ahead], images[behind]
    result = {"accounts_api": images, "same_version": a["digest"] == b["digest"] and a["tag"] == b["tag"]}
    if result["same_version"]:
        return result | {"summary": f"{ahead} and {behind} run the same accounts-api build"}
    sha_a, sha_b = a["tag"].removeprefix("sha-"), b["tag"].removeprefix("sha-")
    compare = gh.get(f"/repos/{REPOS['app']}/compare/{sha_b}...{sha_a}")
    commits = [{"sha": c["sha"][:7], "title": c["commit"]["message"].splitlines()[0],
                "author": c["commit"]["author"]["name"], "date": c["commit"]["author"]["date"]}
               for c in compare.get("commits", [])][-30:]
    return result | {"relationship": compare.get("status"), "commits_in_" + ahead + "_not_in_" + behind: commits,
                     "summary": f"{ahead} is {compare.get('ahead_by')} commit(s) ahead of {behind}"}


def end_of_day_status(kube: Kube, env: str) -> dict:
    ns = _ns(env)
    metrics = {}
    for key, metric in (("ledger_imbalance_cents", "bank_ledger_imbalance_cents"),
                        ("ledger_inconsistencies", "bank_ledger_inconsistencies"),
                        ("interest_paid_cents", "bank_eod_interest_paid_cents"),
                        ("last_business_day", "bank_eod_last_business_date_timestamp_seconds"),
                        ("last_success", "bank_eod_last_success_timestamp_seconds")):
        values = _by_namespace(kube, f'max by (namespace) ({metric}{{namespace="{ns}"}})')
        metrics[key] = values.get(ns)
    if metrics["last_business_day"] is not None:
        metrics["last_business_day"] = _utc_date(metrics["last_business_day"])
    if metrics["last_success"] is not None:
        metrics["last_success"] = datetime.fromtimestamp(metrics["last_success"], UTC).isoformat(timespec="seconds")
    cron = describe_workload(kube, env, "end-of-day")
    # From Loki, not the pods: finished Job pods are removed by history limits and lab restarts
    found = search_logs(kube, env, contains="business day", app="end-of-day", since_minutes=7 * MAX_WINDOW_MINUTES,
                        limit=10)
    run_log: list[Any] = []
    for entry in found["lines"]:
        try:
            run_log.append(json.loads(entry["line"]) | {"pod": entry["pod"]})
        except ValueError:
            run_log.append(entry)
    # Failed attempts leave a Python traceback; its last line ("...Error: ...") says why
    errors = search_logs(kube, env, regex=r"^[A-Za-z_.]*(Error|Exception): ", app="end-of-day",
                         since_minutes=7 * MAX_WINDOW_MINUTES, limit=10)
    failed_attempts = list({e["pod"]: {"time": e["time"], "pod": e["pod"], "error": e["line"].strip()[:300]}
                            for e in reversed(errors["lines"])}.values())[::-1]   # one per attempt (pod)
    runs = cron.get("recent_jobs", [])
    notes = []
    if not cron.get("last_schedule"):
        notes.append("the CronJob has not run on its schedule yet; any runs listed were started by hand")
    if runs and runs[0].get("trigger") == "manual":
        notes.append("the latest run was started by hand, not by the schedule")
    if any(r.get("failed_attempts") for r in runs):
        notes.append("some runs needed retries: see failed_attempts_newest_first for each failed attempt's error")
    yesterday = (_now() - timedelta(days=1)).date().isoformat()
    return {"env": env, "metrics": metrics,
            "books_balance": None if metrics["ledger_imbalance_cents"] is None else
            metrics["ledger_imbalance_cents"] == 0 and metrics["ledger_inconsistencies"] == 0,
            "yesterday_closed": None if metrics["last_business_day"] is None else
            metrics["last_business_day"] >= yesterday,
            "schedule": {k: cron.get(k) for k in ("schedule", "time_zone", "suspended", "last_schedule",
                                                   "last_success")},
            "next_scheduled_run": _next_daily_run(cron.get("schedule")),
            "recent_runs": runs, "run_results_newest_first": run_log,
            "failed_attempts_newest_first": failed_attempts,
            "notes": notes + (["no end-of-day run has finished yet; metrics appear after the first run"]
                              if metrics["last_business_day"] is None else [])}


def _next_daily_run(schedule: str | None) -> str | None:
    """Next run of a daily "M H * * *" schedule in UTC (the CronJob's time zone); None for other forms."""
    m = re.fullmatch(r"(\d+) (\d+) \* \* \*", schedule or "")
    if not m:
        return None
    now = _now()
    run = now.replace(hour=int(m.group(2)), minute=int(m.group(1)), second=0, microsecond=0)
    return (run if run > now else run + timedelta(days=1)).isoformat(timespec="minutes")


def change_audit(gh: GitHub, repo: Literal["gitops", "app"] = "gitops", days: int = 7) -> dict:
    if not gh.authenticated:
        return {"error": "the GitHub audit trail needs a token; none is configured"}
    full = REPOS[repo]
    period = "day" if days <= 1 else "week" if days <= 7 else "month"
    suites = gh.get(f"/repos/{full}/rulesets/rule-suites", ref="refs/heads/main", time_period=period)

    def push(s: dict) -> dict:
        entry = {"pushed_at": s.get("pushed_at"), "result": s.get("result"),
                 "github_actor": s.get("actor_name"),
                 "change": f"{(s.get('before_sha') or '')[:7]} -> {(s.get('after_sha') or '')[:7]}"}
        if s.get("result") == "fail":
            return entry | {"note": "blocked: this commit never reached main"}
        try:
            commit = _commit(gh, full, s["after_sha"])
            entry |= {"made_by": commit["made_by"], "commit_author": commit["author"], "title": commit["title"],
                      "touches": commit["touches"], "pull_request": commit["pull_request"]}
        except ApiError as exc:
            entry["lookup_error"] = str(exc)
        return entry

    entries = _parallel(push, suites[:20])
    counts = {r: sum(1 for s in suites if s.get("result") == r) for r in ("pass", "bypass", "fail")}
    return {"repo": full, "period": period, "pushes_total": len(suites), "results": counts,
            "blocked_pushes": [e for e in entries if e["result"] == "fail"],
            "rules_in_force_since": _rules_since(gh, full),
            "pushes_to_main": entries,
            "how_to_read": "result 'pass' = the rules were met (e.g. an approved PR); 'bypass' = an admin overrode "
                           "them and GitHub recorded it; 'fail' = the push was blocked (the control working). "
                           "github_actor is the account that owns the credential used: a CI deploy key pushes "
                           "as the repo owner, so made_by (from the commit author) says who really made it. "
                           "Pushes before rules_in_force_since were never subject to the rules"}


def _rules_since(gh: GitHub, repo: str) -> dict | None:
    try:
        return {r["name"]: gh.get(f"/repos/{repo}/rulesets/{r['id']}").get("created_at")
                for r in gh.get(f"/repos/{repo}/rulesets")}
    except ApiError:
        return None
