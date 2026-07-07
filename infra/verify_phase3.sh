#!/usr/bin/env bash
# Phase 3 acceptance checks (docs/phase-3-mcp-investigation.md), infra portion only.
# Sanity-checks the three mock MCP container apps and the brain's MCP env-var
# wiring provision.sh's Phase 3 additions are supposed to produce -- does NOT
# require reachability of the mocks from this script's host (they're internal
# ingress, unreachable from outside the Container Apps environment by design;
# see the "5c. Phase 3" comment block in provision.sh).
#
# Run after provision.sh. Doesn't need infra/.suffix (no suffixed resource
# names in this phase), but the brain app/identity names must match provision.sh.
set -euo pipefail

RG="rg-teams-triage-poc"
BRAIN_APP="ca-triage-brain"

MCP_JENKINS_APP="ca-mcp-jenkins"
MCP_DATADOG_APP="ca-mcp-datadog"
MCP_ARGOCD_APP="ca-mcp-argocd"
MCP_JENKINS_PORT=8801
MCP_DATADOG_PORT=8802
MCP_ARGOCD_PORT=8803

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PASS=0
FAIL=0

check() {
  local desc="$1"
  if [[ "$2" == "0" ]]; then
    echo "PASS: ${desc}"
    PASS=$((PASS + 1))
  else
    echo "FAIL: ${desc}"
    FAIL=$((FAIL + 1))
  fi
}

# ---------------------------------------------------------------------------
# 1: each mock MCP container app exists, has internal ingress at the right
#    target port, and MCP_SERVER_KIND/PORT env vars matching its kind.
# ---------------------------------------------------------------------------
echo "== 1: mock MCP container apps (ingress + env vars) =="
check_mock_app() {
  local app="$1" kind="$2" port="$3"
  local spec
  if ! spec=$(az containerapp show -g "$RG" -n "$app" -o json 2>/dev/null); then
    check "container app ${app} exists" 1
    return
  fi
  check "container app ${app} exists" 0

  local props
  props=$(python3 -c "
import json, sys
d = json.loads('''$spec''')
ingress = d['properties']['configuration'].get('ingress') or {}
env = d['properties']['template']['containers'][0].get('env') or []
def envval(name):
    for e in env:
        if e.get('name') == name:
            return e.get('value')
    return None
result = {
    'ingress_external': ingress.get('external'),
    'target_port': ingress.get('targetPort'),
    'kind': envval('MCP_SERVER_KIND'),
    'port': envval('PORT'),
}
print(json.dumps(result))
")

  local ingress_external target_port got_kind got_port
  ingress_external=$(python3 -c "import json,sys; print(json.loads('''$props''')['ingress_external'])")
  target_port=$(python3 -c "import json,sys; print(json.loads('''$props''')['target_port'])")
  got_kind=$(python3 -c "import json,sys; print(json.loads('''$props''')['kind'])")
  got_port=$(python3 -c "import json,sys; print(json.loads('''$props''')['port'])")

  [[ "$ingress_external" == "False" ]]
  check "${app} ingress is internal (external=False)" $?

  [[ "$target_port" == "$port" ]]
  check "${app} target port is ${port} (got ${target_port})" $?

  [[ "$got_kind" == "$kind" ]]
  check "${app} MCP_SERVER_KIND=${kind} (got ${got_kind})" $?

  [[ "$got_port" == "$port" ]]
  check "${app} PORT=${port} (got ${got_port})" $?
}

check_mock_app "$MCP_JENKINS_APP" jenkins "$MCP_JENKINS_PORT"
check_mock_app "$MCP_DATADOG_APP" datadog "$MCP_DATADOG_PORT"
check_mock_app "$MCP_ARGOCD_APP" argocd "$MCP_ARGOCD_PORT"

# ---------------------------------------------------------------------------
# 2: brain container app has the MCP URL + budget env vars set (non-empty).
# ---------------------------------------------------------------------------
echo
echo "== 2: brain container app MCP env vars =="
if BRAIN_ENV=$(az containerapp show -g "$RG" -n "$BRAIN_APP" --query "properties.template.containers[0].env" -o json 2>/dev/null); then
  check "brain container app ${BRAIN_APP} exists" 0
  for var in JENKINS_MCP_URL DATADOG_MCP_URL ARGOCD_MCP_URL TOOL_CALL_BUDGET; do
    PRESENT=$(python3 -c "
import json, sys
env = json.loads('''$BRAIN_ENV''')
print('1' if any(e.get('name') == '$var' and e.get('value') for e in env) else '0')
")
    [[ "$PRESENT" == "1" ]]
    check "brain env var ${var} present" $?
  done
else
  check "brain container app ${BRAIN_APP} exists" 1
fi

# ---------------------------------------------------------------------------
# 3: offline check -- brain/mcp_tools.json is valid JSON and lists jenkins +
#    datadog by default (argocd deliberately absent; acceptance #2 adds it by
#    config edit, not checked here since it's meant to not be present yet).
# ---------------------------------------------------------------------------
echo
echo "== 3: brain/mcp_tools.json registry (offline, source-of-truth check) =="
MCP_TOOLS_JSON="${SCRIPT_DIR}/../brain/mcp_tools.json"
if [[ -f "$MCP_TOOLS_JSON" ]]; then
  check "brain/mcp_tools.json exists" 0
  VALID=$(python3 -c "
import json
try:
    with open('${MCP_TOOLS_JSON}') as f:
        d = json.load(f)
    names = {s.get('name') for s in d.get('servers', [])}
    print('1' if {'jenkins', 'datadog'} <= names else '0')
except Exception:
    print('0')
")
  [[ "$VALID" == "1" ]]
  check "brain/mcp_tools.json is valid JSON listing jenkins+datadog" $?
else
  check "brain/mcp_tools.json exists" 1
fi

echo
echo "== summary: ${PASS} passed, ${FAIL} failed =="
[[ "$FAIL" -eq 0 ]]
