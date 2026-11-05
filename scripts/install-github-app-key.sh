#!/usr/bin/env bash
# Install a GitHub App's private key for the lab and prove it works (run by the engineer).
#
#   scripts/install-github-app-key.sh <agent|builder|reviewer> <app-id> <downloaded .pem>
#
# The key goes to ~/.config/harbourbank-ops/github-app-<role>.pem (mode 600) with its App ID beside it,
# and the downloaded copy is removed. The check signs a 1-minute JWT, asks GitHub which App it is and
# where it is installed, and mints an installation token only to list the repos it can reach. No key
# or token is ever printed.
set -euo pipefail
role=${1:?role: agent, builder or reviewer}; app_id=${2:?App ID (a number, from the App settings page)}; src=${3:?path to the downloaded .pem}
case "$role" in agent|builder|reviewer) ;; *) echo "role must be agent, builder or reviewer" >&2; exit 1 ;; esac
[[ "$app_id" =~ ^[0-9]+$ ]] || { echo "App ID must be a number" >&2; exit 1; }
grep -Eq "BEGIN (RSA )?PRIVATE KEY" "$src" || { echo "$src is not a GitHub App private key" >&2; exit 1; }

dir=~/.config/harbourbank-ops; mkdir -p "$dir"; chmod 700 "$dir"
key=$dir/github-app-$role.pem
install -m 600 "$src" "$key"
printf '%s\n' "$app_id" > "$dir/github-app-$role.id"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
head=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
body=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 30)) $((now + 60)) "$app_id" | b64url)
jwt="$head.$body.$(printf '%s' "$head.$body" | openssl dgst -sha256 -sign "$key" | b64url)"
api() { curl -fsS -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "$@"; }

app=$(api -H "Authorization: Bearer $jwt" https://api.github.com/app)
echo "App:         $(jq -r '.slug' <<< "$app") (id $(jq -r '.id' <<< "$app"))"
echo "Permissions: $(jq -c '.permissions' <<< "$app")"
inst=$(api -H "Authorization: Bearer $jwt" https://api.github.com/app/installations | jq -r '.[0].id // empty')
[[ -n "$inst" ]] || { echo "Not installed anywhere yet: install the App on its repositories, then re-run" >&2; exit 1; }
token=$(api -X POST -H "Authorization: Bearer $jwt" "https://api.github.com/app/installations/$inst/access_tokens" | jq -r .token)
echo "Repos:       $(api -H "Authorization: token $token" https://api.github.com/installation/repositories | jq -r '[.repositories[].full_name] | join(", ")')"
api -X DELETE -H "Authorization: token $token" https://api.github.com/installation/token >/dev/null || true   # revoke it now
unset token jwt

rm -f "$src"
echo "Installed:   $key (mode 600), App ID in $dir/github-app-$role.id; removed $src"
