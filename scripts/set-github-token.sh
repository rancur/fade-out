#!/usr/bin/env bash
#
# set-github-token.sh — put the deployment-freshness credential where the
# container can read it, without it ever touching the repo or a terminal.
#
# WHY THIS EXISTS
#
# The freshness check needs a GitHub credential to compare the running build
# against the newest release of a PRIVATE repo. That credential must not be
# committed, must not be baked into the image, and must not be fetched from a
# secret manager on a request path.
#
# So it is fetched exactly ONCE — here, at deploy time — and written into the
# deploy host's .env, which docker compose reads at container start. The .env is
# the cache: the running container performs zero secret-manager reads, no matter
# how often the check runs. (A sibling project once put five live secret-manager
# reads on a cache fast path and burned a 1000/day quota in eighteen minutes.
# Nothing in the request path of this app reads 1Password at all.)
#
# USAGE
#
#   scripts/set-github-token.sh                       # write ./.env
#   scripts/set-github-token.sh --env-file /path/.env
#   scripts/set-github-token.sh --ssh nas --env-file /srv/fade-out/.env
#   scripts/set-github-token.sh --check               # is it wired? (no read)
#
# The credential is read from 1Password. Defaults, overridable by flag or env:
#
#   vault  FADEOUT_TOKEN_VAULT   Homelab
#   item   FADEOUT_TOKEN_ITEM    Homelab - nas - fade-out - GITHUB_TOKEN
#   field  FADEOUT_TOKEN_FIELD   credential
#
# WHAT THE CREDENTIAL SHOULD BE
#
# A fine-grained personal access token scoped to this repository ONLY, with
# Repository permissions -> Contents: Read-only. That is the entire privilege
# the check needs: it reads release and tag metadata and nothing else. Do not
# use a classic PAT (it cannot be scoped to a single repository) and do not
# reuse a developer or CLI token carrying `repo`, `workflow` or `delete_repo` —
# those hand a monitoring loop write access to the repo it is watching.
#
# After running this, recreate the container so it picks up the new env:
#   docker compose up -d --force-recreate fade-out
# then confirm the check actually works:
#   curl -s localhost:8500/api/upgrade/status
# `check_state` must be "ok" with a real `latest_version`. If it still reports
# "error", the credential cannot see the repo — the check says so on purpose
# rather than reporting a green it cannot justify.

set -euo pipefail

# Never let a shell trace print the value. A cron script in the sibling ops repo
# was once run under `bash -x` and dumped a cached credential set into a
# world-readable log; the guard is cheap and the failure is not.
set +x

VAR_NAME="GITHUB_TOKEN"
VAULT="${FADEOUT_TOKEN_VAULT:-Homelab}"
ITEM="${FADEOUT_TOKEN_ITEM:-Homelab - nas - fade-out - GITHUB_TOKEN}"
FIELD="${FADEOUT_TOKEN_FIELD:-credential}"
ENV_FILE=".env"
SSH_HOST=""
MODE="write"

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --ssh)      SSH_HOST="$2"; shift 2 ;;
    --vault)    VAULT="$2"; shift 2 ;;
    --item)     ITEM="$2"; shift 2 ;;
    --field)    FIELD="$2"; shift 2 ;;
    --check)    MODE="check"; shift ;;
    -h|--help)  sed -n '2,48p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Run one command string here or on the deploy host. The paths are meant to
# expand locally (shellcheck SC2029) — ENV_FILE is an operator-supplied path,
# not something the remote is expected to resolve.
run_remote() {
  if [ -n "$SSH_HOST" ]; then ssh "$SSH_HOST" "$1"; else sh -c "$1"; fi
}

if [ "$MODE" = "check" ]; then
  # Reports only whether the variable is PRESENT and non-empty. Never the value.
  if run_remote "grep -q '^${VAR_NAME}=..*' '$ENV_FILE' 2>/dev/null"; then
    echo "$VAR_NAME is set in $ENV_FILE${SSH_HOST:+ on $SSH_HOST}"
    exit 0
  fi
  echo "$VAR_NAME is NOT set in $ENV_FILE${SSH_HOST:+ on $SSH_HOST}" >&2
  echo "the freshness check will report 'cannot determine' until it is" >&2
  exit 1
fi

command -v op >/dev/null 2>&1 || {
  echo "fatal: 1Password CLI (op) not found. This script deliberately does not" >&2
  echo "accept a token as an argument or on stdin — both end up in shell" >&2
  echo "history and in process listings." >&2
  exit 1
}

# The one and only secret-manager read in the entire lifecycle.
TOKEN="$(op item get "$ITEM" --vault "$VAULT" --fields "$FIELD" --reveal 2>/dev/null || true)"
if [ -z "$TOKEN" ]; then
  echo "fatal: could not read field '$FIELD' of item '$ITEM' in vault '$VAULT'." >&2
  echo "Create the item first, storing the fine-grained read-only token there." >&2
  echo "See the header of this script for the exact permissions it needs." >&2
  exit 1
fi

# Verify the credential BEFORE writing it anywhere. A token that authenticates
# but cannot see the repo is the exact false green the check was hardened
# against, and it is cheaper to catch here than in production.
REPO="${GITHUB_REPO:-rancur/fade-out}"
CODE="$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/${REPO}/releases/latest" || echo 000)"
if [ "$CODE" != "200" ]; then
  unset TOKEN
  echo "fatal: credential cannot read releases on ${REPO} (HTTP ${CODE})." >&2
  echo "Nothing was written. 401/403 = rejected or unauthorized; 404 = the" >&2
  echo "token has no access to this private repo; 000 = no network." >&2
  exit 1
fi

TMP="$(mktemp)"
chmod 600 "$TMP"
trap 'rm -f "$TMP" "$TMP.new"' EXIT

if [ -n "$SSH_HOST" ]; then
  ssh "$SSH_HOST" "cat '$ENV_FILE' 2>/dev/null" > "$TMP" || true
else
  cat "$ENV_FILE" 2>/dev/null > "$TMP" || true
fi

# Replace any existing line rather than appending a second one — a duplicated
# key in a .env is resolved silently, and differently, by different readers.
grep -v "^${VAR_NAME}=" "$TMP" > "$TMP.new" 2>/dev/null || true
mv "$TMP.new" "$TMP"
{
  printf '\n# Deployment freshness check (read-only).'
  printf ' Managed by scripts/set-github-token.sh.\n'
  printf '%s=%s\n' "$VAR_NAME" "$TOKEN"
} >> "$TMP"
unset TOKEN

if [ -n "$SSH_HOST" ]; then
  # Mode 600 on the destination: this .env holds every credential the
  # deployment has, and a world-readable one is a finding in its own right.
  ssh "$SSH_HOST" "cat > '$ENV_FILE' && chmod 600 '$ENV_FILE'" < "$TMP"
else
  cat "$TMP" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

echo "$VAR_NAME written to $ENV_FILE${SSH_HOST:+ on $SSH_HOST} (mode 600)"
echo "credential verified against ${REPO} releases before writing"
echo "next: docker compose up -d --force-recreate fade-out"
