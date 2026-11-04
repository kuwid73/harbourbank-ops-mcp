"""Harbour Bank change MCP server: the agent proposes fixes as pull requests; a person approves them.

Run over stdio (Claude Desktop starts it):  harbourbank-change-mcp

It holds the harbourbank-agent GitHub App's key and nothing else: no Kubernetes access, so evidence
comes from the read-only harbourbank-ops server. It refuses to start as any other GitHub identity.

Configuration (environment variables, with defaults):
  HBCHANGE_APP_KEY     ~/.config/harbourbank-ops/github-app-agent.pem
  HBCHANGE_APP_ID      ~/.config/harbourbank-ops/github-app-agent.id
  HBCHANGE_AUDIT_LOG   ~/.local/state/harbourbank-ops-mcp/change-audit.jsonl
"""
import logging
import os
import sys
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import changes
from .appauth import AppToken
from .safety import AuditLog, configure_logging, guarded

EXPECTED_APP = "harbourbank-agent"
log = logging.getLogger("harbourbank_change_mcp")

INSTRUCTIONS = """\
You can propose changes to Harbour Bank, never make them. Every tool here opens a pull request on
the harbourbank-gitops repository, which a code owner reviews and merges, or rejects. You cannot
approve or merge, and only the app's manifests and incident records can be changed.

Before proposing, find the cause with the harbourbank-ops tools and quote that evidence in the
proposal: what you saw, from which tool, with times. Prefer the smallest fix. When a recent change
caused the problem, propose_revert of that PR is usually right. One problem, one pull request.
Ask the user before opening a PR unless they already asked for one. Afterwards give them the link
and say that a code owner must approve it before anything changes. After service is restored,
offer to record the incident (record_incident) with the times from the evidence."""

PROPOSES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                           open_world_hint=True)
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=True)


def _path(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable) or default).expanduser()


def build(writer: changes.Writer, audit: AuditLog) -> MCPServer:
    server = MCPServer("harbourbank-change", instructions=INSTRUCTIONS)
    tool = server.tool
    g = guarded(audit)

    @tool(annotations=PROPOSES)
    @g
    def propose_change(title: str, why: str, evidence: str, edits: list[dict]) -> dict:
        """Open a PR that edits existing files in harbourbank-gitops. edits: a list of
        {"path", "find", "replace"}, where `find` is exact text that occurs once in the file on main.
        Only apps/harbourbank/overlays/<env>/, apps/harbourbank/base/ (YAML) and incidents/ can be
        changed; at most 5 files. `why` says what is wrong and how this fixes it; `evidence` quotes
        what the harbourbank-ops tools showed."""
        return changes.propose_change(writer, title, why, evidence, edits)

    @tool(annotations=PROPOSES)
    @g
    def propose_revert(pull_request: int, why: str, evidence: str) -> dict:
        """Open a PR that undoes a merged harbourbank-gitops PR, restoring every file it changed.
        Refused if the files changed again since, or if the PR touched files the agent may not change."""
        return changes.propose_revert(writer, pull_request, why, evidence)

    @tool(annotations=PROPOSES)
    @g
    def record_incident(incident_id: str, title: str, environments: list[Literal["dev", "qa", "prod"]],
                        started: str, detected: str, detected_by: str, remediation: str, impact: str,
                        evidence: str, restored: str | None = None, caused_by: str | None = None,
                        follow_up: str | None = None) -> dict:
        """Open a PR adding an incident record (incidents/<incident_id>.yaml), which the DORA metrics
        read. incident_id: YYYY-MM-DD-short-name. Times in UTC ISO 8601 (…Z): started = service first
        affected, detected = a person or alert knew, restored = back to normal (leave out if still
        open). caused_by: gitops#<PR> when a change caused it. remediation: rollback, fix forward,
        manual recovery, or none (recovered by itself)."""
        return changes.record_incident(writer, incident_id, title, environments, started, detected,
                                       detected_by, remediation, impact, evidence, restored, caused_by,
                                       follow_up)

    @tool(annotations=READ_ONLY)
    @g
    def my_proposals() -> dict:
        """The agent's recent pull requests: open, merged or closed without merging."""
        return changes.my_proposals(writer)

    return server


def main() -> None:
    configure_logging()
    key, app_id = _path("HBCHANGE_APP_KEY", "~/.config/harbourbank-ops/github-app-agent.pem"), \
        _path("HBCHANGE_APP_ID", "~/.config/harbourbank-ops/github-app-agent.id")
    audit = AuditLog(_path("HBCHANGE_AUDIT_LOG", "~/.local/state/harbourbank-ops-mcp/change-audit.jsonl"))
    if not key.exists() or not app_id.exists():
        sys.exit(f"harbourbank-change-mcp: {key} or {app_id} not found (the harbourbank-agent App's key and ID)")
    token = AppToken.from_files(key, app_id)
    # Refuse any other identity, above all a broader one: the limits must not depend on good intentions
    slug, repos = token.identity()
    if slug != EXPECTED_APP or repos != {changes.REPO}:
        sys.exit(f"harbourbank-change-mcp: refusing to start as {slug!r} with access to {sorted(repos)}; "
                 f"expected {EXPECTED_APP!r} with access to {changes.REPO} only")
    log.info("started as %s (repos: %s); audit log %s", slug, ", ".join(sorted(repos)), audit.path)
    build(changes.Writer(token), audit).run("stdio")


if __name__ == "__main__":
    main()
