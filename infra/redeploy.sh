#!/usr/bin/env bash
# Fast redeploy: rebuild an app's image and roll a new revision, WITHOUT
# re-running full provisioning (no RBAC/VNet/bot/registry churn). Use this after
# a code change once provision.sh has stood everything up.
#
#   ./redeploy.sh            # brain only (the usual case)
#   ./redeploy.sh brain
#   ./redeploy.sh mocks      # all three mock MCP servers
#   ./redeploy.sh relay
#   ./redeploy.sh all        # relay + brain + mocks
#
# Env vars already set on the apps (Foundry/Storage/MCP URLs/etc.) are preserved
# -- this only swaps the image, so it never needs to know the wiring. To change
# env vars or add infrastructure, run provision.sh (which is idempotent).
#
# Requires: az login, and a prior successful provision.sh (infra/.suffix must
# exist and point at the deployed resources).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

TARGET="${1:-brain}"

if ! az acr show -g "$RG" -n "$ACR_NAME" >/dev/null 2>&1; then
  echo "ERROR: registry ${ACR_NAME} not found -- run provision.sh first." >&2
  exit 1
fi
# Resolve once so build_image doesn't look it up per call.
ACR_LOGIN_SERVER="$(acr_login_server)"

redeploy_one() {  # app, repo, srcdir
  local app="$1" repo="$2" src="$3" image
  echo "-- ${app} (${repo}) --"
  image="$(build_image "$repo" "${SCRIPT_DIR}/../${src}")"
  roll_image "$app" "$image"
  echo "  rolled ${app} -> ${image}"
}

redeploy_brain() { redeploy_one "$BRAIN_APP" brain brain; }
redeploy_relay() { redeploy_one "$RELAY_APP" relay relay; }
redeploy_mocks() {
  echo "-- mock MCP servers (jenkins/datadog/argocd) --"
  local image
  image="$(build_image mockmcp "${SCRIPT_DIR}/../mock-mcp")"
  local app
  for app in "$MCP_JENKINS_APP" "$MCP_DATADOG_APP" "$MCP_ARGOCD_APP"; do
    roll_image "$app" "$image"
    echo "  rolled ${app} -> ${image}"
  done
}

case "$TARGET" in
  brain) redeploy_brain ;;
  relay) redeploy_relay ;;
  mocks) redeploy_mocks ;;
  all)   redeploy_relay; redeploy_brain; redeploy_mocks ;;
  *) echo "usage: $0 [brain|relay|mocks|all]" >&2; exit 2 ;;
esac

echo "== redeploy done: ${TARGET} (build ${BUILD_TAG}) =="
echo "Tip: 'az containerapp logs show -g ${RG} -n ${BRAIN_APP} --tail 50 --type console' to watch the brain."
