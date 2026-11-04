"""GitHub App authentication: the App's private key signs a short JWT, which buys a 1-hour token for
the App's installation. The token is kept in memory only and renewed a few minutes before it expires.
"""
import time
from pathlib import Path

import httpx
import jwt

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class AppToken:
    def __init__(self, app_id: str, private_key: str, transport: httpx.BaseTransport | None = None):
        self.app_id, self._key = app_id, private_key
        self._http = httpx.Client(base_url=API, headers=HEADERS, timeout=10, transport=transport)
        self._token, self._expires = "", 0.0

    @classmethod
    def from_files(cls, key_file: Path, id_file: Path) -> "AppToken":
        return cls(id_file.read_text().strip(), key_file.read_text())

    def _jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 30, "exp": now + 540, "iss": self.app_id}, self._key, algorithm="RS256")

    def _as_app(self, method: str, path: str) -> dict | list:
        response = self._http.request(method, path, headers={"Authorization": f"Bearer {self._jwt()}"})
        response.raise_for_status()
        return response.json()

    def identity(self) -> tuple[str, set[str]]:
        """The App's slug and the repositories its installation can reach."""
        slug = self._as_app("GET", "/app")["slug"]
        repos = self._http.get("/installation/repositories", headers={"Authorization": f"token {self()}"})
        repos.raise_for_status()
        return slug, {r["full_name"] for r in repos.json()["repositories"]}

    def __call__(self) -> str:
        if time.time() > self._expires - 300:
            installation = self._as_app("GET", "/app/installations")[0]["id"]
            self._token = self._as_app("POST", f"/app/installations/{installation}/access_tokens")["token"]
            self._expires = time.time() + 3600
        return self._token
