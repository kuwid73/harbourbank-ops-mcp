"""A fake cluster and a fake GitHub, served through httpx.MockTransport.

Every request is recorded, so tests can assert what the server asked for, above all that it
only ever reads.
"""
import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from harbourbank_ops_mcp.clients import GitHub, Kube

NOW = datetime.now(UTC)


def ts(minutes_ago: float = 0) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


IMAGE = "ghcr.io/kuwid73/harbourbank-accounts-api:sha-493df0c@sha256:c8af1a2b"


def deployment(name="accounts-api", ready=1, image=IMAGE, conditions=None):
    return {"metadata": {"name": name}, "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                                                 "template": {"spec": {"containers": [{"image": image}]}}},
            "status": {"readyReplicas": ready, "replicas": 1, "conditions": conditions or []}}


def pod(name, phase="Running", ready=True, waiting=None, last=None, restarts=0):
    state = {"waiting": {"reason": waiting, "message": "back-off restarting"}} if waiting else {"running": {}}
    status = {"phase": phase, "containerStatuses": [{"ready": ready, "restartCount": restarts, "state": state,
                                                     **({"lastState": {"terminated": last}} if last else {})}]}
    return {"metadata": {"name": name, "creationTimestamp": ts(30)}, "spec": {"nodeName": "agent-0",
            "containers": [{"image": IMAGE}]}, "status": status}


def argo_app(sync="Synced", health="Healthy", phase="Succeeded"):
    return {"status": {"sync": {"status": sync, "revision": "60decb3abcdef"}, "health": {"status": health},
                       "operationState": {"phase": phase, "finishedAt": ts(5), "message": "ok",
                                          "syncResult": {"resources": [
                                              {"kind": "Job", "name": "accounts-api-smoke-test",
                                               "hookType": "PostSync", "hookPhase": "Succeeded"}]}},
                       "history": [{"deployedAt": ts(60), "revision": "60decb3abcdef",
                                    "initiatedBy": {"automated": True}}]}}


class Fake:
    """Routes: exact path -> JSON (or callable(request) -> JSON). Prometheus/Loki by query text."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, object] = {}
        self.prometheus: dict[str, list] = {}   # regex on the query -> result vector
        self.loki_result: list = []
        self.logs: dict[str, str] = {}
        self.identity = "system:serviceaccount:agent-access:harbourbank-observer"
        for env in ("dev", "qa", "prod"):
            ns = f"harbourbank-{env}"
            self.routes[f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/harbourbank-{env}"] = argo_app()
            self.routes[f"/apis/apps/v1/namespaces/{ns}/deployments/accounts-api"] = deployment()
            self.routes[f"/apis/apps/v1/namespaces/{ns}/deployments"] = {"items": [deployment()]}
            self.routes[f"/apis/postgresql.cnpg.io/v1/namespaces/{ns}/clusters/accounts-db"] = {
                "spec": {"instances": 1}, "status": {"phase": "Cluster in healthy state", "readyInstances": 1}}
            self.routes[f"/api/v1/namespaces/{ns}/pods"] = {"items": [pod("accounts-api-1")]}
            self.routes[f"/api/v1/namespaces/{ns}/events"] = {"items": []}
            self.routes[f"/apis/batch/v1/namespaces/{ns}/jobs"] = {"items": []}
            self.routes[f"/apis/batch/v1/namespaces/{ns}/cronjobs"] = {"items": []}
        self.routes["/api/v1/nodes"] = {"items": [{"metadata": {"name": "agent-0"},
                                                   "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/selfsubjectreviews"):
            return httpx.Response(201, json={"status": {"userInfo": {"username": self.identity}}})
        if "/proxy/api/v1/query" in path:
            query = request.url.params["query"]
            result = next((v for k, v in self.prometheus.items() if re.search(k, query)), [])
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})
        if "/proxy/loki/" in path:
            return httpx.Response(200, json={"status": "success", "data": {"result": self.loki_result}})
        if path.endswith("/log"):
            pod_name = path.split("/")[-2]
            return httpx.Response(200, text=self.logs.get(pod_name, ""))
        route = self.routes.get(path)
        if route is None:
            return httpx.Response(404, json={"message": f"{path} not found"})
        body = route(request) if callable(route) else route
        return httpx.Response(200, json=json.loads(json.dumps(body)))


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def kube(fake):
    return Kube("https://k8s.test", "token", transport=httpx.MockTransport(fake.handle))


@pytest.fixture
def gh(fake):
    return GitHub("ghtoken", transport=httpx.MockTransport(fake.handle))
