# harbourbank-ops-mcp

A **read-only** [MCP](https://modelcontextprotocol.io) server that lets Claude Desktop answer
operational questions about **Harbour Bank**, a fictional bank with synthetic data (see
[harbourbank-app](https://github.com/kuwid73/harbourbank-app) and
[harbourbank-gitops](https://github.com/kuwid73/harbourbank-gitops)):

- "Is the bank healthy?"
- "What changed in prod today, and who approved it?"
- "Did last night's books balance?"
- "Why did the dev rollout show Degraded yesterday?"

It fetches facts. It does no reasoning of its own (there's no LLM inside) and it **cannot change anything**.

## Tools

| Tool | Answers |
|---|---|
| `bank_status` | each environment on one page: ArgoCD sync/health and last deployment (with its smoke test), accounts-api version and replicas, database, last business day closed, ledger imbalance, firing alerts |
| `find_problems` | what's wrong now: unhealthy apps, crash-looping/pending/unready pods, recent restarts, failed Jobs, stuck rollouts, databases, nodes, alerts, warning events. Each comes with evidence and a suggested next call |
| `describe_workload` | one Deployment, CronJob, Job or Pod: images, replicas, conditions, pods, recent runs, events |
| `get_logs` | recent logs of a pod, Deployment or Job (or the previous container, where a crash's cause is) |
| `search_logs` | stored logs and the Kubernetes event history (Loki), across environments, up to 7 days back |
| `query_metrics` | any PromQL query, current or as a time series |
| `recent_changes` | deployments to an environment, each with its Git commit and pull request (author, merger, reviews) |
| `compare_environments` | what's in one environment's build that another doesn't have yet |
| `end_of_day_status` | the nightly batch: business day closed, interest paid, whether the books balance, recent runs and their results |
| `change_audit` | GitHub's record of every push to `main`: rules passed or bypassed, by whom, with the PR's reviews |

## Safety model

| Control | How |
|---|---|
| **Read-only identity** | It runs as the Kubernetes ServiceAccount `harbourbank-observer` ([its permissions](https://github.com/kuwid73/harbourbank-gitops/blob/main/platform/manifests/agent-access/harbourbank-observer.yaml), managed in Git). There are no write verbs and no Secrets or ConfigMaps, and Prometheus and Loki are reachable by HTTP GET only. |
| **Refuses any other identity** | At start-up the server asks the API "who am I?" and exits unless the answer is `harbourbank-observer`, so it can't run with admin credentials by mistake. |
| **Read-only code** | Every tool issues only GET requests; a test runs all ten against a fake cluster and fails on anything else. The tools also declare `readOnlyHint` to the client. |
| **Fixed scope** | Only `harbourbank-dev/qa/prod` and the two Harbour Bank repos. Object names are validated, and log searches are built by the server, not passed through from the model. |
| **Personal data and secrets masked** | Emails (`a***@domain`), account numbers (`HB******1234`), tokens and database passwords are masked in every result and in the audit log. |
| **Bounded output** | Time windows, line and series caps, and a hard limit of 40,000 characters per result. |
| **Audit log** | One JSON line per tool call (time, tool, masked arguments, outcome, duration, result size) in `~/.local/state/harbourbank-ops-mcp/audit.jsonl`. |

## Setup

Run these in WSL.

```bash
uv sync --locked
```

It needs two credentials, created by a person and kept outside the repo with mode `600`:

| File | Contents |
|---|---|
| `~/.config/harbourbank-ops/kubeconfig` | a kubeconfig holding a short-lived token for `harbourbank-observer` (`kubectl create token`) |
| `~/.config/harbourbank-ops/github-token` | a fine-grained, read-only GitHub token for the two Harbour Bank repos (optional; `change_audit` needs it) |

Override the paths with `HBOPS_KUBECONFIG`, `HBOPS_GITHUB_TOKEN_FILE` and `HBOPS_AUDIT_LOG`.

### Claude Desktop (Windows, with the server running in WSL)

Add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "harbourbank-ops": {
      "command": "wsl",
      "args": ["-e", "/home/<you>/work/ai/agentic/devops/agentic-lab/harbourbank-ops-mcp/.venv/bin/harbourbank-ops-mcp"]
    }
  }
}
```

## Development

```bash
uv run ruff check .
uv run pytest -q
```

To try the tools against a live cluster without creating a token, run `kubectl proxy --port=8099 --as=system:serviceaccount:agent-access:harbourbank-observer`. Every request then goes through with exactly the observer's permissions. Point `Kube("http://127.0.0.1:8099", "unused")` at it.
