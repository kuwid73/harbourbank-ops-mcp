#!/usr/bin/env bash
# Demo prop: plant one log line in dev that contains *synthetic* personal data, to show that the MCP
# server masks it before the model sees it. Harbour Bank's own logs never contain personal data (the
# API logs route templates, not values), so without this there is nothing to mask.
#
#   demo/planted-leak.sh plant    run a one-off pod in harbourbank-dev that prints the line
#   demo/planted-leak.sh remove   delete it
#
# Then ask Claude Desktop: "Search dev's logs for app planted-leak. What does it say?"
# Expected: the email shows as p***@sim.harbourbank.example and the account as HB******4242.
# Tell the audience it was planted. Runs as you (your kubectl context), never as the MCP server.
set -euo pipefail

CONTEXT=k3d-agentic-lab
NS=harbourbank-dev
POD=planted-leak

plant() {
  # Reuse the image already on the nodes, so nothing is downloaded
  local image
  image=$(kubectl --context "$CONTEXT" -n "$NS" get deploy accounts-api -o jsonpath='{.spec.template.spec.containers[0].image}')
  kubectl --context "$CONTEXT" -n "$NS" run "$POD" --image="$image" --restart=Never --labels=app="$POD" \
    --overrides='{"spec": {"securityContext": {"runAsNonRoot": true, "seccompProfile": {"type": "RuntimeDefault"}},
      "containers": [{"name": "planted-leak", "image": "'"$image"'",
        "command": ["python", "-c", "import json; print(json.dumps({\"level\": \"ERROR\", \"msg\": \"DEMO PLANTED LEAK: statement email bounced\", \"customer_email\": \"pat.planted@sim.harbourbank.example\", \"account_number\": \"HB0000004242\"}))"],
        "securityContext": {"allowPrivilegeEscalation": false, "capabilities": {"drop": ["ALL"]},
                            "readOnlyRootFilesystem": true}}]}}'
  kubectl --context "$CONTEXT" -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" --timeout=60s
  echo "planted. The raw line (unmasked, as stored):"
  kubectl --context "$CONTEXT" -n "$NS" logs "$POD"
}

remove() {
  kubectl --context "$CONTEXT" -n "$NS" delete pod "$POD" --ignore-not-found
}

case "${1:-}" in
  plant|remove) "$1" ;;
  *) echo "usage: $0 plant|remove" >&2; exit 2 ;;
esac
