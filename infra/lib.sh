#!/usr/bin/env bash
# Shared config + helpers for the teams-triage-bot infra scripts
# (provision.sh, redeploy.sh). SOURCE this file; do not execute it directly.
#
# Assumes: az login, and the subscription set to the one holding
# rg-teams-triage-poc. Defines every resource name in ONE place so provision
# and redeploy can't drift apart.

# ---- static resource names ------------------------------------------------
RG="rg-teams-triage-poc"
LOCATION="eastus2"
TENANT_ID="ef5ff41e-4a26-4f61-98b6-8bada7ac8e2f"

# Pre-existing AI Foundry account from the MAF spike (spikes/azure-setup-log.md)
# -- NOT created by these scripts, only wired up with RBAC + env vars.
FOUNDRY_ACCOUNT="aif-triage-poc-567b31"
FOUNDRY_ENDPOINT="https://${FOUNDRY_ACCOUNT}.services.ai.azure.com/"
FOUNDRY_MODEL="gpt-5-mini"

BOT_NAME="teams-triage-poc-bot"
VNET_NAME="vnet-triage-poc"
SUBNET_NAME="snet-containerapps"
NSG_NAME="nsg-triage-relay-ingress"
BRAIN_IDENTITY="id-triage-brain"
RELAY_IDENTITY="id-triage-relay"
SB_QUEUE="activities"
CAE_NAME="cae-triage-poc"
RELAY_APP="ca-triage-relay"
BRAIN_APP="ca-triage-brain"

# Phase 3 mock MCP servers (mock-mcp/): one image, three personalities picked at
# runtime by MCP_SERVER_KIND + PORT (see mock-mcp/Dockerfile, mock-mcp/README.md).
MCP_JENKINS_APP="ca-mcp-jenkins"
MCP_DATADOG_APP="ca-mcp-datadog"
MCP_ARGOCD_APP="ca-mcp-argocd"
MCP_JENKINS_PORT=8801
MCP_DATADOG_PORT=8802
MCP_ARGOCD_PORT=8803

# ---- suffix + derived globally-unique names -------------------------------
# A random suffix is persisted next to this file so reruns target the SAME
# globally-unique resources (ACR, Service Bus namespace, storage account)
# rather than minting new ones. provision.sh creates it on first run; if you
# run redeploy.sh before provisioning, it will mint a suffix that points at
# resources that don't exist yet -- provision first.
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUFFIX_FILE="${LIB_DIR}/.suffix"
if [[ ! -f "$SUFFIX_FILE" ]]; then
  openssl rand -hex 3 > "$SUFFIX_FILE"
fi
SUFFIX="$(cat "$SUFFIX_FILE")"
SB_NAMESPACE="sb-triage-poc-${SUFFIX}"
ACR_NAME="acrtriagepoc${SUFFIX}"
ST_ACCOUNT="sttriagepoc${SUFFIX}"

# A build tag unique per invocation. Using a fresh, SPECIFIC tag (not :latest)
# with `az containerapp update --image` is what guarantees a new revision
# actually rolls out -- ACA will NOT roll if the image reference is
# byte-identical to what's already deployed (the classic ":latest doesn't
# redeploy" trap that bit us mid-development). Images are ALSO tagged :latest
# for human convenience.
BUILD_TAG="$(date -u +%Y%m%d%H%M%S)"

# ---- helpers --------------------------------------------------------------

# Run a command that's expected to fail only with an "already exists" error on
# reruns. Any other failure (missing RBAC permission, transient ARM error) is a
# real failure and must NOT be masked.
run_allow_exists() {
  local desc="$1"; shift
  local out
  if ! out=$("$@" 2>&1); then
    if grep -qiE 'already exists|alreadyexists|conflict' <<<"$out"; then
      echo "  (${desc} already exists, continuing)"
    else
      echo "ERROR: ${desc} failed:" >&2
      echo "$out" >&2
      exit 1
    fi
  fi
}

# Create a container app ONLY if it doesn't already exist (with the given
# create args). `az containerapp create` is NOT rerun-safe on its own -- it
# errors if the app exists, which under `set -e` aborts the whole script. This
# guard is why provision.sh is safe to run repeatedly; callers roll the image
# and upsert env vars AFTER this, so a rerun still converges to current config.
ensure_app_created() {
  local app="$1"; shift
  if az containerapp show -g "$RG" -n "$app" >/dev/null 2>&1; then
    echo "  (${app} already exists; will roll image + env)"
    return 0
  fi
  az containerapp create -g "$RG" -n "$app" "$@" >/dev/null
}

# The ACR login server (e.g. acrtriagepoc<suffix>.azurecr.io). Requires the
# registry to exist already.
acr_login_server() {
  az acr show -g "$RG" -n "$ACR_NAME" --query loginServer -o tsv
}

# Build an image in ACR from a source dir, tagged :$BUILD_TAG AND :latest, and
# echo the fully-qualified :$BUILD_TAG reference (for use with --image). Uses a
# preset $ACR_LOGIN_SERVER if the caller already resolved it, else looks it up.
build_image() {
  local repo="$1" src="$2"
  local server="${ACR_LOGIN_SERVER:-$(acr_login_server)}"
  az acr build -r "$ACR_NAME" -t "${repo}:${BUILD_TAG}" -t "${repo}:latest" "$src" >/dev/null
  echo "${server}/${repo}:${BUILD_TAG}"
}

# Roll a container app to a new image (new revision), leaving env vars untouched.
roll_image() {
  local app="$1" image="$2"
  az containerapp update -g "$RG" -n "$app" --image "$image" >/dev/null
}
