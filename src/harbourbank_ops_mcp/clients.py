"""Thin read-only HTTP clients for the Kubernetes API (incl. its service proxy) and GitHub.

Plain httpx rather than the kubernetes/PyGithub SDKs: every request the server can make is
visible here, which is what a reviewer needs to check "read-only" for themselves. The only
non-GET request is the identity check at start-up (a SelfSubjectReview, which changes nothing).
"""
import base64
import ssl
from pathlib import Path
from typing import Any

import httpx
import yaml

TIMEOUT_SECONDS = 15


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _raise_for(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        body = response.json()
        message = body.get("message") or body.get("error") or response.text
    except ValueError:
        message = response.text
    raise ApiError(response.status_code, str(message)[:500])


class Kube:
    def __init__(self, server: str, token: str, ca_pem: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        verify: ssl.SSLContext | bool = ssl.create_default_context(cadata=ca_pem) if ca_pem else True
        self._http = httpx.Client(base_url=server, headers={"Authorization": f"Bearer {token}"},
                                  verify=verify, timeout=TIMEOUT_SECONDS, transport=transport)

    @classmethod
    def from_kubeconfig(cls, path: Path) -> "Kube":
        config = yaml.safe_load(path.read_text())
        cluster = config["clusters"][0]["cluster"]
        user = config["users"][0]["user"]
        if "token" not in user:
            raise SystemExit(f"{path}: expected a ServiceAccount token, found no 'token' for the user")
        ca = base64.b64decode(cluster["certificate-authority-data"]).decode() \
            if "certificate-authority-data" in cluster else None
        return cls(cluster["server"], user["token"], ca)

    def get(self, path: str, **params: Any) -> dict:
        response = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        _raise_for(response)
        return response.json()

    def get_or_none(self, path: str, **params: Any) -> dict | None:
        try:
            return self.get(path, **params)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def text(self, path: str, **params: Any) -> str:
        response = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        _raise_for(response)
        return response.text

    def proxy(self, namespace: str, service: str, path: str, **params: Any) -> dict:
        """GET through the API server's service proxy (Prometheus, Loki). RBAC allows GET only."""
        return self.get(f"/api/v1/namespaces/{namespace}/services/{service}/proxy/{path}", **params)

    def whoami(self) -> str:
        response = self._http.post("/apis/authentication.k8s.io/v1/selfsubjectreviews",
                                   json={"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"})
        _raise_for(response)
        return response.json()["status"]["userInfo"]["username"]


class GitHub:
    API = "https://api.github.com"

    def __init__(self, token: str | None, transport: httpx.BaseTransport | None = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.authenticated = bool(token)
        self._http = httpx.Client(base_url=self.API, headers=headers, timeout=TIMEOUT_SECONDS, transport=transport)

    def get(self, path: str, **params: Any) -> Any:
        response = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        _raise_for(response)
        return response.json()
