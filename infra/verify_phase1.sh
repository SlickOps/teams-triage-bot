#!/usr/bin/env bash
# Phase 1 acceptance checks (docs/phase-1-teams-io.md). Run after provision.sh.
# Steps 1 and 4 (real @mention in Teams, kill-mid-flight) are manual/semi-manual --
# this script covers the ones that are pure HTTP/CLI checks (2, 3, 3b) and prints
# guidance for the rest.
set -euo pipefail

RG="rg-teams-triage-poc"
RELAY_APP="ca-triage-relay"
TEMP_RULE="tempallow-verify-host"

RELAY_FQDN=$(az containerapp show -g "$RG" -n "$RELAY_APP" --query properties.configuration.ingress.fqdn -o tsv)
URL="https://${RELAY_FQDN}/api/messages"

# The ingress allowlist (ACA ipSecurityRestrictions, set by provision.sh from
# the AzureBotService service tag) blocks this machine too -- so the app-layer
# (JWT) checks need a temporary allow rule for our own IP, removed again
# before the network-layer check.
MY_IP=$(curl -s https://api.ipify.org)
echo "== opening temporary ingress allow rule for this machine (${MY_IP}) =="
az containerapp ingress access-restriction set -g "$RG" -n "$RELAY_APP" \
  --rule-name "$TEMP_RULE" --ip-address "${MY_IP}/32" --action Allow \
  --description "temporary, for verify_phase1.sh" >/dev/null
trap 'az containerapp ingress access-restriction remove -g "$RG" -n "$RELAY_APP" --rule-name "$TEMP_RULE" >/dev/null 2>&1 || true' EXIT

echo
echo "== 3: missing/invalid JWT -> expect 401 (app-layer validation) =="
curl -s -o /dev/null --max-time 30 -w "no auth header:      %{http_code}\n" -X POST "$URL" \
  -H "Content-Type: application/json" \
  -d '{"type":"message","id":"verify-1","channelId":"msteams","text":"hi"}'
curl -s -o /dev/null --max-time 30 -w "garbage auth header: %{http_code}\n" -X POST "$URL" \
  -H "Content-Type: application/json" -H "Authorization: Bearer garbage" \
  -d '{"type":"message","id":"verify-2","channelId":"msteams","text":"hi"}'

echo
echo "== 3b: removing the temp rule; direct hit should now be blocked (403) =="
az containerapp ingress access-restriction remove -g "$RG" -n "$RELAY_APP" \
  --rule-name "$TEMP_RULE" >/dev/null
sleep 20   # revision update propagation
curl -s -o /dev/null --max-time 20 -w "direct hit: %{http_code}   (expect 403: blocked at the platform edge)\n" -X POST "$URL" \
  -H "Content-Type: application/json" -d '{"type":"message","id":"verify-3","text":"hi"}' \
  || echo "direct hit: connection failed (also acceptable -- blocked before app code)"

echo
echo "== 1: real @mention echo -- manual =="
echo "@mention the bot in the target Teams channel/DM and confirm it replies"
echo "  \"you said: <text>\""
echo "Bot messaging endpoint under test: $URL"

echo
echo "== 4: kill-mid-flight idempotency -- manual =="
echo "1. Send a message to the bot."
echo "2. Immediately: az containerapp revision restart -g $RG -n ca-triage-brain --revision \$(az containerapp revision list -g $RG -n ca-triage-brain --query '[0].name' -o tsv)"
echo "3. Confirm exactly one reply eventually arrives (Service Bus redelivers the"
echo "   uncompleted message after the lock timeout if the restart happened"
echo "   before send_activity(); a restart after send but before complete_message()"
echo "   is a known gap -- see 'Explicitly deferred' in the Phase 1 plan)."
echo "4. Check queue metrics: az servicebus queue show -g $RG --namespace-name <ns> -n activities --query countDetails"
