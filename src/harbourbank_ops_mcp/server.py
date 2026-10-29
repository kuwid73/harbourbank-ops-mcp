"""Harbour Bank operations MCP server: read-only questions about the bank, answered from live data.

Run over stdio (Claude Desktop starts it):  harbourbank-ops-mcp

Configuration (environment variables, with defaults):
  HBOPS_KUBECONFIG          ~/.config/harbourbank-ops/kubeconfig     harbourbank-observer's token
  HBOPS_GITHUB_TOKEN_FILE   ~/.config/harbourbank-ops/github-token   read-only GitHub token (optional)
  HBOPS_AUDIT_LOG           ~/.local/state/harbourbank-ops-mcp/audit.jsonl
"""
import logging
import os
import sys
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import ops
from .clients import GitHub, Kube
from .safety import AuditLog, configure_logging, guarded

EXPECTED_IDENTITY = "system:serviceaccount:agent-access:harbourbank-observer"
log = logging.getLogger("harbourbank_ops_mcp")

INSTRUCTIONS = """\
You are looking at Harbour Bank, a fictional bank with synthetic data, run with GitOps on Kubernetes.
Environments: dev, qa and prod (namespaces harbourbank-<env>). Each runs accounts-api (customers,
accounts, double-entry transfers), a Postgres database (accounts-db), a customer simulator that
generates realistic traffic, and a nightly end-of-day batch (interest + ledger check).
Changes flow: app PR -> CI -> dev automatically -> promotion PR -> qa -> promotion PR approved by a
code owner -> prod. ArgoCD deploys from the harbourbank-gitops repository.

Every tool is read-only. Start broad (bank_status, find_problems), then narrow down
(describe_workload, get_logs, search_logs, query_metrics). Cite the evidence the tools return.
Personal data and secrets are masked in all results; say so if a question needs them."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)


def _path(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable) or default).expanduser()


def build(kube: Kube, gh: GitHub, audit: AuditLog) -> MCPServer:
    server = MCPServer("harbourbank-ops", instructions=INSTRUCTIONS)
    tool = server.tool
    g = guarded(audit)

    @tool(annotations=READ_ONLY)
    @g
    def bank_status() -> dict:
        """One-page health of every environment: ArgoCD sync/health and last deployment (with its
        PostSync smoke test), the accounts-api version (tag + digest) and ready replicas, database
        status, last business day closed and ledger imbalance, and firing alerts. Start here."""
        return ops.bank_status(kube)

    @tool(annotations=READ_ONLY)
    @g
    def find_problems(env: Literal["dev", "qa", "prod"] | None = None) -> dict:
        """What is wrong right now: unhealthy ArgoCD apps, crash-looping/pending/unready pods, recent
        restarts, failed Jobs, stuck rollouts, unhealthy databases, NotReady nodes, firing alerts and
        recent warning events. Each problem includes evidence and a suggested next tool call.
        Omit env to check all three."""
        return ops.find_problems(kube, env)

    @tool(annotations=READ_ONLY)
    @g
    def describe_workload(env: Literal["dev", "qa", "prod"], name: str) -> dict:
        """Details of one Deployment, CronJob, Job or Pod by name (e.g. accounts-api, end-of-day,
        customer-simulator, salary-day): images, replicas, rollout conditions, pods, recent runs and
        recent events. Lists the available names if it isn't found."""
        return ops.describe_workload(kube, env, name)

    @tool(annotations=READ_ONLY)
    @g
    def get_logs(env: Literal["dev", "qa", "prod"], workload: str, since_minutes: int = 60,
                 contains: str | None = None, max_lines: int = 200, previous: bool = False) -> dict:
        """Recent log lines of a pod, a Deployment's pods or a Job's pods (newest 3 pods). Filter with
        `contains` (case-insensitive). previous=true reads the container's previous run, which is where
        a crash's cause is. Window up to 24 h, up to 500 lines per pod. accounts-api logs are JSON."""
        return ops.get_logs(kube, env, workload, since_minutes, contains, max_lines, previous)

    @tool(annotations=READ_ONLY)
    @g
    def search_logs(env: Literal["dev", "qa", "prod"] | None = None, contains: str | None = None,
                    regex: str | None = None, source: Literal["pods", "events"] = "pods",
                    app: str | None = None, since_minutes: int = 60, limit: int = 100) -> dict:
        """Search stored logs (Loki) across Harbour Bank, newest first, including pods that no longer
        exist. source='events' searches the Kubernetes event history (kept for days; the live API keeps
        about an hour), e.g. image pulls, rollouts, scheduling. Filter by app (e.g. accounts-api),
        `contains` text and/or a `regex`. Window up to 7 days, up to 500 lines."""
        return ops.search_logs(kube, env, contains, regex, source, app, since_minutes, limit)

    @tool(annotations=READ_ONLY)
    @g
    def query_metrics(promql: str, range_minutes: int = 0, step_seconds: int = 60) -> dict:
        """Run a PromQL query. range_minutes=0 gives current values; >0 a time series (up to 120 points).
        Useful metrics: http_requests_total{namespace,route,status}, http_request_duration_seconds_bucket,
        bank_transfers_total{kind,status,reason}, db_pool_connections_in_use, app_info{version,git_sha},
        bank_ledger_imbalance_cents, kube_pod_container_status_restarts_total, ALERTS."""
        return ops.query_metrics(kube, promql, range_minutes, step_seconds)

    @tool(annotations=READ_ONLY)
    @g
    def recent_changes(env: Literal["dev", "qa", "prod"], days: int = 7) -> dict:
        """Deployments to an environment, newest first: when ArgoCD deployed each GitOps commit, the
        commit, and its pull request (author, who merged it, reviews/approvals). Answers "what changed
        and who approved it". Up to 30 days."""
        return ops.recent_changes(kube, gh, env, days)

    @tool(annotations=READ_ONLY)
    @g
    def compare_environments(ahead: Literal["dev", "qa", "prod"], behind: Literal["dev", "qa", "prod"]) -> dict:
        """Compare the accounts-api build running in two environments and list the app commits in
        `ahead` that `behind` doesn't have yet, e.g. ahead='qa', behind='prod' = what the next prod
        promotion would ship."""
        return ops.compare_environments(kube, gh, ahead, behind)

    @tool(annotations=READ_ONLY)
    @g
    def end_of_day_status(env: Literal["dev", "qa", "prod"]) -> dict:
        """The nightly end-of-day batch: last business day closed, interest paid, whether the books
        balance (ledger imbalance and inconsistencies must be 0), whether yesterday is closed, the
        schedule, recent runs and the latest run's own log."""
        return ops.end_of_day_status(kube, env)

    @tool(annotations=READ_ONLY)
    @g
    def change_audit(repo: Literal["gitops", "app"] = "gitops", days: int = 7) -> dict:
        """GitHub's record of every push to main: whether the change-control rules passed or were
        bypassed (and by whom), with the pull request's reviews. This is the change-approval audit
        trail. repo='gitops' covers deployments; 'app' covers code."""
        return ops.change_audit(gh, repo, days)

    return server


def main() -> None:
    configure_logging()
    kubeconfig = _path("HBOPS_KUBECONFIG", "~/.config/harbourbank-ops/kubeconfig")
    token_file = _path("HBOPS_GITHUB_TOKEN_FILE", "~/.config/harbourbank-ops/github-token")
    audit = AuditLog(_path("HBOPS_AUDIT_LOG", "~/.local/state/harbourbank-ops-mcp/audit.jsonl"))

    if not kubeconfig.exists():
        sys.exit(f"harbourbank-ops-mcp: {kubeconfig} not found (harbourbank-observer's kubeconfig)")
    kube = Kube.from_kubeconfig(kubeconfig)
    # Refuse to run with any other identity, above all an admin's: read-only must not depend on good intentions
    identity = kube.whoami()
    if identity != EXPECTED_IDENTITY:
        sys.exit(f"harbourbank-ops-mcp: refusing to start as {identity!r}; expected {EXPECTED_IDENTITY!r}")
    gh = GitHub(token_file.read_text().strip() if token_file.exists() else None)
    log.info("started as %s; GitHub %s; audit log %s", identity,
             "authenticated" if gh.authenticated else "anonymous (change_audit unavailable)", audit.path)
    build(kube, gh, audit).run("stdio")


if __name__ == "__main__":
    main()
