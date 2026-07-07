#!/usr/bin/env bash
# Phase 1 infra provisioning for teams-triage-bot -- az cli, POC-scoped.
# See infra/README.md for what this creates, env vars produced, and teardown.
#
# Requires: az login, subscription set to the one with rg-teams-triage-poc
# (the resource group already exists from the MAF spike).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# All resource names, the persisted suffix, and the helpers (run_allow_exists,
# ensure_app_created, build_image, roll_image, $BUILD_TAG) live in lib.sh so
# provision.sh and redeploy.sh share exactly one source of truth.
source "${SCRIPT_DIR}/lib.sh"

# This whole script is IDEMPOTENT -- safe to run repeatedly. Existing resources
# are detected and left in place (or rolled to the freshly-built image); only
# missing ones are created. So a rerun both (a) fills any gap from a partial
# earlier run and (b) redeploys current code. For a fast redeploy of just an
# app's image after a code change, prefer redeploy.sh.

echo "== teams-triage-bot provisioning (suffix ${SUFFIX}, build ${BUILD_TAG}) =="

# ---------------------------------------------------------------------------
# 1. Managed identities -- no client secrets anywhere. The brain's identity
#    doubles as the bot's own Microsoft App ID (see step 2): Bot Framework's
#    UserAssignedMSI app type needs no separate AAD app registration.
# ---------------------------------------------------------------------------
echo "-- identities --"
az identity create -g "$RG" -n "$BRAIN_IDENTITY" -l "$LOCATION" >/dev/null
az identity create -g "$RG" -n "$RELAY_IDENTITY" -l "$LOCATION" >/dev/null

BRAIN_CLIENT_ID=$(az identity show -g "$RG" -n "$BRAIN_IDENTITY" --query clientId -o tsv)
BRAIN_PRINCIPAL_ID=$(az identity show -g "$RG" -n "$BRAIN_IDENTITY" --query principalId -o tsv)
BRAIN_RESOURCE_ID=$(az identity show -g "$RG" -n "$BRAIN_IDENTITY" --query id -o tsv)
RELAY_CLIENT_ID=$(az identity show -g "$RG" -n "$RELAY_IDENTITY" --query clientId -o tsv)
RELAY_PRINCIPAL_ID=$(az identity show -g "$RG" -n "$RELAY_IDENTITY" --query principalId -o tsv)
RELAY_RESOURCE_ID=$(az identity show -g "$RG" -n "$RELAY_IDENTITY" --query id -o tsv)

# ---------------------------------------------------------------------------
# 2. Azure Bot resource -- single-tenant, UserAssignedMSI. Endpoint is set
#    later once the relay's Container App FQDN exists (step 5).
# ---------------------------------------------------------------------------
echo "-- bot registration --"
if az bot show -g "$RG" -n "$BOT_NAME" >/dev/null 2>&1; then
  echo "  (bot ${BOT_NAME} already exists, continuing)"
else
  az bot create \
    -g "$RG" -n "$BOT_NAME" \
    --app-type UserAssignedMSI \
    --appid "$BRAIN_CLIENT_ID" \
    --msi-resource-id "$BRAIN_RESOURCE_ID" \
    --tenant-id "$TENANT_ID" \
    --sku F0 \
    >/dev/null
fi

run_allow_exists "Teams channel" az bot msteams create -g "$RG" -n "$BOT_NAME"

# ---------------------------------------------------------------------------
# 3. Service Bus namespace + queue, RBAC scoped to the queue only.
# ---------------------------------------------------------------------------
echo "-- service bus --"
az servicebus namespace create -g "$RG" -n "$SB_NAMESPACE" -l "$LOCATION" --sku Basic >/dev/null
az servicebus queue create -g "$RG" --namespace-name "$SB_NAMESPACE" -n "$SB_QUEUE" >/dev/null
SB_QUEUE_ID=$(az servicebus queue show -g "$RG" --namespace-name "$SB_NAMESPACE" -n "$SB_QUEUE" --query id -o tsv)

run_allow_exists "sender role assignment" az role assignment create --assignee-object-id "$RELAY_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "Azure Service Bus Data Sender" --scope "$SB_QUEUE_ID"
run_allow_exists "receiver role assignment" az role assignment create --assignee-object-id "$BRAIN_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "Azure Service Bus Data Receiver" --scope "$SB_QUEUE_ID"

# ---------------------------------------------------------------------------
# 3b. Phase 2: RBAC on the pre-existing AI Foundry account (from the MAF spike,
#     spikes/azure-setup-log.md) so the brain's managed identity can call the
#     gpt-5-mini deployment via FoundryChatClient. This account is NOT created
#     here -- it's expected to already exist; fail loudly if it doesn't.
#
#     Per the spike log: the unified `services.ai.azure.com` endpoint 401s
#     unless the caller has BOTH "Cognitive Services OpenAI User" AND
#     "Azure AI Developer" on the account -- either role alone is insufficient.
#     Also per the spike: RBAC propagation takes a few minutes, so a 401 right
#     after this script runs isn't necessarily a misconfiguration.
# ---------------------------------------------------------------------------
echo "-- foundry rbac --"
if ! FOUNDRY_ID=$(az cognitiveservices account show -n "$FOUNDRY_ACCOUNT" -g "$RG" --query id -o tsv 2>/dev/null); then
  echo "ERROR: AI Foundry account '${FOUNDRY_ACCOUNT}' not found in resource group '${RG}'." >&2
  echo "       This script does not create it -- it's expected to already exist from the" >&2
  echo "       MAF spike. See spikes/azure-setup-log.md for how it was provisioned." >&2
  exit 1
fi

run_allow_exists "brain Cognitive Services OpenAI User assignment" az role assignment create \
  --assignee-object-id "$BRAIN_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope "$FOUNDRY_ID"
run_allow_exists "brain Azure AI Developer assignment" az role assignment create \
  --assignee-object-id "$BRAIN_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "Azure AI Developer" --scope "$FOUNDRY_ID"

# ---------------------------------------------------------------------------
# 3c. Phase 2: storage account + Table for interview state (resumable,
#     one-active-interview-per-user). RBAC-only access, no account keys.
# ---------------------------------------------------------------------------
echo "-- storage account (interview state) --"
if az storage account show -g "$RG" -n "$ST_ACCOUNT" >/dev/null 2>&1; then
  echo "  (storage account ${ST_ACCOUNT} already exists, continuing)"
else
  az storage account create -g "$RG" -n "$ST_ACCOUNT" -l "$LOCATION" \
    --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
    --allow-blob-public-access false >/dev/null
fi
ST_ID=$(az storage account show -g "$RG" -n "$ST_ACCOUNT" --query id -o tsv)

run_allow_exists "brain Storage Table Data Contributor assignment" az role assignment create \
  --assignee-object-id "$BRAIN_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "Storage Table Data Contributor" --scope "$ST_ID"

# The `interviews` table itself is created lazily by the brain at runtime
# (create-if-not-exists on first use) rather than here, because AAD-based
# table creation needs the RBAC grant above to have propagated to the
# *caller* (this script's signed-in identity, not the brain's), which isn't
# guaranteed at this point in a fresh run. Best-effort attempt so a rerun
# after propagation still ends up with the table present; failure here is
# non-fatal.
az storage table create --name interviews --account-name "$ST_ACCOUNT" \
  --auth-mode login >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 4. ACR + cloud builds (no local Docker on this machine -- az acr build
#    builds in the cloud from source).
# ---------------------------------------------------------------------------
echo "-- container registry + builds --"
az acr create -g "$RG" -n "$ACR_NAME" -l "$LOCATION" --sku Basic --admin-enabled false >/dev/null
ACR_ID=$(az acr show -g "$RG" -n "$ACR_NAME" --query id -o tsv)
ACR_LOGIN_SERVER=$(az acr show -g "$RG" -n "$ACR_NAME" --query loginServer -o tsv)

run_allow_exists "relay AcrPull assignment" az role assignment create --assignee-object-id "$RELAY_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope "$ACR_ID"
run_allow_exists "brain AcrPull assignment" az role assignment create --assignee-object-id "$BRAIN_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope "$ACR_ID"

# Build with a unique tag (+ :latest) and capture the tagged ref, so the
# create/update calls below roll a fresh revision rather than silently reusing
# a cached :latest digest (see lib.sh BUILD_TAG).
RELAY_IMAGE="$(build_image relay "${SCRIPT_DIR}/../relay")"
BRAIN_IMAGE="$(build_image brain "${SCRIPT_DIR}/../brain")"

# ---------------------------------------------------------------------------
# 5. VNet (+ NSG), then a VNet-integrated Container Apps environment.
#
#    IMPORTANT scoping fact (learned the hard way; per Microsoft docs on
#    container-apps/firewall-integration): on an *external* environment,
#    public inbound traffic BYPASSES the VNet and inbound NSG rules do not
#    apply to it -- so the actual ingress allowlist is done in step 6 via
#    ACA's own ipSecurityRestrictions (enforced at the public endpoint, but
#    CIDR-only; no service-tag support there, verified against the ARM spec).
#
#    The VNet integration is still worth having (must be chosen at env
#    creation, can't be added later): it's free, and it's the seam for
#    later-phase *egress* filtering (NSG outbound rules DO apply) and private
#    endpoints. The NSG's AzureBotService inbound rule only affects
#    VNet-routed traffic -- defense in depth, not the enforcement point.
#
#    Both apps run at a fixed single replica: relay is HTTP-triggered anyway,
#    and scale-to-zero for brain (KEDA on queue depth) is a cost nicety
#    deferred for now, not a Phase 1 acceptance requirement.
# ---------------------------------------------------------------------------
echo "-- vnet + nsg --"
if az network vnet show -g "$RG" -n "$VNET_NAME" >/dev/null 2>&1; then
  echo "  (vnet ${VNET_NAME} already exists, continuing)"
else
  az network vnet create -g "$RG" -n "$VNET_NAME" -l "$LOCATION" \
    --address-prefixes 10.10.0.0/16 >/dev/null
fi

az network nsg create -g "$RG" -n "$NSG_NAME" -l "$LOCATION" >/dev/null
az network nsg rule create -g "$RG" --nsg-name "$NSG_NAME" \
  -n AllowAzureBotService --priority 100 --direction Inbound --access Allow \
  --protocol Tcp --source-address-prefixes AzureBotService \
  --destination-address-prefixes '*' --destination-port-ranges 443 >/dev/null

az network vnet subnet create -g "$RG" --vnet-name "$VNET_NAME" -n "$SUBNET_NAME" \
  --address-prefixes 10.10.0.0/27 \
  --delegations Microsoft.App/environments \
  --network-security-group "$NSG_NAME" >/dev/null
SUBNET_ID=$(az network vnet subnet show -g "$RG" --vnet-name "$VNET_NAME" -n "$SUBNET_NAME" --query id -o tsv)

echo "-- container apps environment --"
if az containerapp env show -g "$RG" -n "$CAE_NAME" >/dev/null 2>&1; then
  echo "  (container apps env ${CAE_NAME} already exists, continuing)"
else
  az containerapp env create -g "$RG" -n "$CAE_NAME" -l "$LOCATION" \
    --infrastructure-subnet-resource-id "$SUBNET_ID" >/dev/null
fi

SB_FQNS="${SB_NAMESPACE}.servicebus.windows.net"

echo "-- relay app --"
# ensure_app_created only creates if missing (rerun-safe); the update right
# after always rolls the freshly-built image + upserts env, so a rerun redeploys
# current code whether the app was just created or already existed.
ensure_app_created "$RELAY_APP" \
  --environment "$CAE_NAME" \
  --image "$RELAY_IMAGE" \
  --registry-server "$ACR_LOGIN_SERVER" --registry-identity "$RELAY_RESOURCE_ID" \
  --user-assigned "$RELAY_RESOURCE_ID" \
  --ingress external --target-port 3978 \
  --min-replicas 1 --max-replicas 1 \
  --env-vars \
    "BOT_APP_ID=${BRAIN_CLIENT_ID}" \
    "BOT_TENANT_ID=${TENANT_ID}" \
    "SERVICEBUS_FULLY_QUALIFIED_NAMESPACE=${SB_FQNS}" \
    "SERVICE_BUS_QUEUE_NAME=${SB_QUEUE}" \
    "AZURE_CLIENT_ID=${RELAY_CLIENT_ID}"
az containerapp update -g "$RG" -n "$RELAY_APP" \
  --image "$RELAY_IMAGE" \
  --set-env-vars \
    "BOT_APP_ID=${BRAIN_CLIENT_ID}" \
    "BOT_TENANT_ID=${TENANT_ID}" \
    "SERVICEBUS_FULLY_QUALIFIED_NAMESPACE=${SB_FQNS}" \
    "SERVICE_BUS_QUEUE_NAME=${SB_QUEUE}" \
    "AZURE_CLIENT_ID=${RELAY_CLIENT_ID}" \
  >/dev/null

echo "-- brain app --"
ensure_app_created "$BRAIN_APP" \
  --environment "$CAE_NAME" \
  --image "$BRAIN_IMAGE" \
  --registry-server "$ACR_LOGIN_SERVER" --registry-identity "$BRAIN_RESOURCE_ID" \
  --user-assigned "$BRAIN_RESOURCE_ID" \
  --min-replicas 1 --max-replicas 1 \
  --env-vars \
    "BOT_APP_ID=${BRAIN_CLIENT_ID}" \
    "BOT_TENANT_ID=${TENANT_ID}" \
    "SERVICEBUS_FULLY_QUALIFIED_NAMESPACE=${SB_FQNS}" \
    "SERVICE_BUS_QUEUE_NAME=${SB_QUEUE}" \
    "AZURE_CLIENT_ID=${BRAIN_CLIENT_ID}" \
    "FOUNDRY_PROJECT_ENDPOINT=${FOUNDRY_ENDPOINT}" \
    "FOUNDRY_MODEL=${FOUNDRY_MODEL}" \
    "STATE_STORAGE_ACCOUNT=${ST_ACCOUNT}" \
    "INTERVIEW_TABLE_NAME=interviews"

# ---------------------------------------------------------------------------
# 5b. Phase 2: roll the freshly-built brain image + (re)assert its Foundry/
#     Storage env vars. `--set-env-vars` is an upsert and `--image` rolls a new
#     revision, so this is rerun-safe whether the brain app was just created
#     above or already existed -- it's what makes a plain `provision.sh` rerun
#     also redeploy current brain code.
#
#     New outbound egress this introduces for the brain (beyond Service Bus):
#     *.services.ai.azure.com (Foundry) and *.table.core.windows.net
#     (Storage). No egress allowlist is implemented yet (deferred, per the
#     Phase 1 VNet/NSG comments above) -- noting the new destinations here for
#     when that seam is picked up.
# ---------------------------------------------------------------------------
az containerapp update -g "$RG" -n "$BRAIN_APP" \
  --image "$BRAIN_IMAGE" \
  --set-env-vars \
    "FOUNDRY_PROJECT_ENDPOINT=${FOUNDRY_ENDPOINT}" \
    "FOUNDRY_MODEL=${FOUNDRY_MODEL}" \
    "STATE_STORAGE_ACCOUNT=${ST_ACCOUNT}" \
    "INTERVIEW_TABLE_NAME=interviews" \
  >/dev/null

RELAY_FQDN=$(az containerapp show -g "$RG" -n "$RELAY_APP" --query properties.configuration.ingress.fqdn -o tsv)

# ---------------------------------------------------------------------------
# 5c. Phase 3: mock MCP servers (Jenkins/Datadog/Argo CD), one codebase deployed
#     3x, each with INTERNAL ingress only -- these mirror "Jenkins MCP in-cluster,
#     never public" from the design doc (docs/phase-3-mcp-investigation.md).
#     No secrets, no inbound from the internet: the brain reaches them over the
#     CAE's private network, same as any two container apps in one environment.
#     `--ingress internal` + `--target-port` confirmed against the current
#     `az containerapp create --help` (allowed ingress values: external,
#     internal); the internal FQDN pattern
#     (`<app>.internal.<environment-unique-id>.<region>.azurecontainerapps.io`)
#     is confirmed against Microsoft Learn's "Communicate between container
#     apps" doc, "External and internal FQDNs" table -- confirmed rather than
#     assumed, per this repo's ethos of verifying az/API surfaces.
# ---------------------------------------------------------------------------
echo "-- mock MCP servers (jenkins/datadog/argocd) --"
MOCK_IMAGE="$(build_image mockmcp "${SCRIPT_DIR}/../mock-mcp")"

deploy_mock_mcp() {
  local app="$1" kind="$2" port="$3"
  ensure_app_created "$app" \
    --environment "$CAE_NAME" \
    --image "$MOCK_IMAGE" \
    --registry-server "$ACR_LOGIN_SERVER" --registry-identity "$BRAIN_RESOURCE_ID" \
    --user-assigned "$BRAIN_RESOURCE_ID" \
    --ingress internal --target-port "$port" \
    --min-replicas 1 --max-replicas 1 \
    --env-vars \
      "MCP_SERVER_KIND=${kind}" \
      "PORT=${port}"
  # Roll the freshly-built image on rerun (create above is skipped if the app
  # already exists). MCP_SERVER_KIND/PORT are create-time and stable, so an
  # image-only update preserves them.
  roll_image "$app" "$MOCK_IMAGE"
}

deploy_mock_mcp "$MCP_JENKINS_APP" jenkins "$MCP_JENKINS_PORT"
deploy_mock_mcp "$MCP_DATADOG_APP" datadog "$MCP_DATADOG_PORT"
deploy_mock_mcp "$MCP_ARGOCD_APP" argocd "$MCP_ARGOCD_PORT"

CAE_DEFAULT_DOMAIN=$(az containerapp env show -g "$RG" -n "$CAE_NAME" --query properties.defaultDomain -o tsv)
JENKINS_MCP_URL="https://${MCP_JENKINS_APP}.internal.${CAE_DEFAULT_DOMAIN}/mcp"
DATADOG_MCP_URL="https://${MCP_DATADOG_APP}.internal.${CAE_DEFAULT_DOMAIN}/mcp"
ARGOCD_MCP_URL="https://${MCP_ARGOCD_APP}.internal.${CAE_DEFAULT_DOMAIN}/mcp"

# Upsert on the brain, same idempotent pattern as 5b: `--set-env-vars` is a
# rerun-safe upsert regardless of whether the brain app was just created above
# or already existed. ARGOCD_MCP_URL is set even though brain/mcp_tools.json's
# default registry doesn't list an "argocd" entry -- mcp_registry.py skips any
# entry not present in the config, so this is harmless and means the
# acceptance-#2 demo (add a 3rd server) is a pure mcp_tools.json edit + brain
# image rebuild, no infra change.
#
# New outbound egress this introduces for the brain: the three mock MCP apps'
# internal FQDNs above (*.internal.${CAE_DEFAULT_DOMAIN}) -- same deferred
# egress-allowlist seam noted in step 5b/the Phase 1 VNet comments; traffic to
# these stays inside the CAE regardless; noting it here for when that seam is
# picked up.
az containerapp update -g "$RG" -n "$BRAIN_APP" \
  --set-env-vars \
    "JENKINS_MCP_URL=${JENKINS_MCP_URL}" \
    "DATADOG_MCP_URL=${DATADOG_MCP_URL}" \
    "ARGOCD_MCP_URL=${ARGOCD_MCP_URL}" \
    "TOOL_CALL_BUDGET=8" \
  >/dev/null

# ---------------------------------------------------------------------------
# 6. Point the bot at the relay, and set the relay's ingress allowlist -- as
#    ONE bulk ARM PATCH of ipSecurityRestrictions (per-rule CLI calls take
#    ~20s each; ~130 rules would be an hour). ACA enforces this at the public
#    endpoint (unlike the NSG -- see step 5).
#
#    Ranges: the AzureBotService service tag is NOT sufficient -- Teams
#    delivers bot activities from the Microsoft Teams service ranges
#    (observed live: 52.112.116.x, User-Agent Microsoft-SkypeBotApi), which
#    are published as M365 network endpoints (id 11, "Microsoft Teams"), not
#    as an Azure service tag. So: Teams ranges + AzureBotService tag (other
#    Bot Framework channels/DirectLine). The tag part is a static snapshot
#    and goes stale as Microsoft rotates ranges; every rerun of this script
#    refreshes it, since the PATCH replaces the whole array.
# ---------------------------------------------------------------------------
echo "-- wiring bot endpoint + relay ingress allowlist --"
az bot update -g "$RG" -n "$BOT_NAME" --endpoint "https://${RELAY_FQDN}/api/messages" >/dev/null

TEAMS_RANGES='["52.112.0.0/14", "52.122.0.0/15"]'

RESTRICTIONS_PATCH="$(mktemp)"
az network list-service-tags -l "$LOCATION" \
  --query "values[?name=='AzureBotService'].properties.addressPrefixes[]" -o json \
  | python3 -c "
import json, sys
teams = json.loads('$TEAMS_RANGES')
tag = [c for c in json.load(sys.stdin) if ':' not in c]  # IPv4 only
rules = [
    {'name': f'teams-{i+1}', 'ipAddressRange': c, 'action': 'Allow',
     'description': 'Microsoft Teams service ranges (M365 endpoint id 11)'}
    for i, c in enumerate(teams)
] + [
    {'name': f'botconnector-{i+1}', 'ipAddressRange': c, 'action': 'Allow',
     'description': 'AzureBotService service tag (refreshed by provision.sh)'}
    for i, c in enumerate(tag)
]
json.dump({'properties': {'configuration': {'ingress': {'ipSecurityRestrictions': rules}}}}, sys.stdout)
print(f'  {len(rules)} allow rules ({len(teams)} Teams + {len(tag)} AzureBotService)', file=sys.stderr)
" > "$RESTRICTIONS_PATCH"

RELAY_ID=$(az containerapp show -g "$RG" -n "$RELAY_APP" --query id -o tsv)
az rest --method patch \
  --url "https://management.azure.com${RELAY_ID}?api-version=2025-01-01" \
  --body "@${RESTRICTIONS_PATCH}" >/dev/null
rm -f "$RESTRICTIONS_PATCH"

echo "== Done =="
echo "Bot messaging endpoint: https://${RELAY_FQDN}/api/messages"
echo "BOT_APP_ID:             ${BRAIN_CLIENT_ID}"
echo "BOT_TENANT_ID:          ${TENANT_ID}"
echo "Service Bus namespace:  ${SB_FQNS}"
echo "Storage account:        ${ST_ACCOUNT} (table: interviews)"
echo "Foundry endpoint:       ${FOUNDRY_ENDPOINT} (model: ${FOUNDRY_MODEL})"
echo "Mock MCP servers:       ${JENKINS_MCP_URL}"
echo "                        ${DATADOG_MCP_URL}"
echo "                        ${ARGOCD_MCP_URL} (not in default mcp_tools.json registry)"
