#!/usr/bin/env bash
# Phase 2 acceptance checks (docs/phase-2-llm-interview.md), infra portion only.
# Sanity-checks the RBAC + storage + env-var wiring provision.sh's Phase 2
# additions are supposed to produce -- does NOT require the brain app to
# actually be running or able to reach Foundry/Storage yet.
#
# Run after provision.sh. Re-derives names the same way provision.sh does
# (reads infra/.suffix for the storage account suffix).
set -euo pipefail

RG="rg-teams-triage-poc"
BRAIN_IDENTITY="id-triage-brain"
BRAIN_APP="ca-triage-brain"
FOUNDRY_ACCOUNT="aif-triage-poc-567b31"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUFFIX_FILE="${SCRIPT_DIR}/.suffix"
if [[ ! -f "$SUFFIX_FILE" ]]; then
  echo "ERROR: ${SUFFIX_FILE} not found -- run provision.sh first." >&2
  exit 1
fi
SUFFIX="$(cat "$SUFFIX_FILE")"
ST_ACCOUNT="sttriagepoc${SUFFIX}"

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

BRAIN_PRINCIPAL_ID=$(az identity show -g "$RG" -n "$BRAIN_IDENTITY" --query principalId -o tsv)

echo "== 1: foundry rbac -- both roles present on the brain's identity =="
FOUNDRY_ID=$(az cognitiveservices account show -n "$FOUNDRY_ACCOUNT" -g "$RG" --query id -o tsv 2>/dev/null) || FOUNDRY_ID=""
if [[ -z "$FOUNDRY_ID" ]]; then
  check "foundry account ${FOUNDRY_ACCOUNT} exists" 1
else
  check "foundry account ${FOUNDRY_ACCOUNT} exists" 0

  # Filter by principalId in JMESPath (over all assignments at the scope) rather
  # than `--assignee <id>`: the latter makes az resolve the principal via Microsoft
  # Graph, which this tenant's CLI login can't read -- producing noisy (harmless)
  # "Failed to query ... by invoking Graph API" warnings. This avoids the lookup.
  OPENAI_USER_COUNT=$(az role assignment list --scope "$FOUNDRY_ID" \
    --query "[?principalId=='$BRAIN_PRINCIPAL_ID' && roleDefinitionName=='Cognitive Services OpenAI User'] | length(@)" -o tsv)
  [[ "$OPENAI_USER_COUNT" -ge 1 ]]
  check "brain has 'Cognitive Services OpenAI User' on ${FOUNDRY_ACCOUNT}" $?

  AI_DEV_COUNT=$(az role assignment list --scope "$FOUNDRY_ID" \
    --query "[?principalId=='$BRAIN_PRINCIPAL_ID' && roleDefinitionName=='Azure AI Developer'] | length(@)" -o tsv)
  [[ "$AI_DEV_COUNT" -ge 1 ]]
  check "brain has 'Azure AI Developer' on ${FOUNDRY_ACCOUNT}" $?
fi

echo
echo "== 2: storage account + table rbac =="
if az storage account show -g "$RG" -n "$ST_ACCOUNT" >/dev/null 2>&1; then
  check "storage account ${ST_ACCOUNT} present" 0
  ST_ID=$(az storage account show -g "$RG" -n "$ST_ACCOUNT" --query id -o tsv)

  TABLE_ROLE_COUNT=$(az role assignment list --scope "$ST_ID" \
    --query "[?principalId=='$BRAIN_PRINCIPAL_ID' && roleDefinitionName=='Storage Table Data Contributor'] | length(@)" -o tsv)
  [[ "$TABLE_ROLE_COUNT" -ge 1 ]]
  check "brain has 'Storage Table Data Contributor' on ${ST_ACCOUNT}" $?
else
  check "storage account ${ST_ACCOUNT} present" 1
fi

echo
echo "== 3: brain container app env vars =="
if BRAIN_ENV=$(az containerapp show -g "$RG" -n "$BRAIN_APP" --query "properties.template.containers[0].env" -o json 2>/dev/null); then
  check "brain container app ${BRAIN_APP} exists" 0
  for var in FOUNDRY_PROJECT_ENDPOINT FOUNDRY_MODEL STATE_STORAGE_ACCOUNT INTERVIEW_TABLE_NAME; do
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

echo
echo "== summary: ${PASS} passed, ${FAIL} failed =="
[[ "$FAIL" -eq 0 ]]
