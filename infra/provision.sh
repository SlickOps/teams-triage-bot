#!/usr/bin/env bash
# Phase 1 infra provisioning for teams-triage-bot -- az cli, POC-scoped.
# See infra/README.md for what this creates, env vars produced, and teardown.
#
# Requires: az login, subscription set to the one with rg-teams-triage-poc
# (the resource group already exists from the MAF spike).
set -euo pipefail

RG="rg-teams-triage-poc"
LOCATION="eastus2"
TENANT_ID="ef5ff41e-4a26-4f61-98b6-8bada7ac8e2f"

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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run a command that's expected to fail with a specific "already exists"
# error on reruns. Any other failure (missing RBAC permission, transient ARM
# error, etc.) is a real failure and must not be masked as "already exists".
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

# Persist a random suffix locally so reruns target the same globally-unique
# resource names (ACR, Service Bus namespace) instead of creating new ones.
SUFFIX_FILE="${SCRIPT_DIR}/.suffix"
if [[ ! -f "$SUFFIX_FILE" ]]; then
  openssl rand -hex 3 > "$SUFFIX_FILE"
fi
SUFFIX="$(cat "$SUFFIX_FILE")"
SB_NAMESPACE="sb-triage-poc-${SUFFIX}"
ACR_NAME="acrtriagepoc${SUFFIX}"

echo "== teams-triage-bot Phase 1 provisioning (suffix ${SUFFIX}) =="

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

az acr build -r "$ACR_NAME" -t "relay:latest" "${SCRIPT_DIR}/../relay" >/dev/null
az acr build -r "$ACR_NAME" -t "brain:latest" "${SCRIPT_DIR}/../brain" >/dev/null

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
az containerapp env create -g "$RG" -n "$CAE_NAME" -l "$LOCATION" \
  --infrastructure-subnet-resource-id "$SUBNET_ID" >/dev/null

SB_FQNS="${SB_NAMESPACE}.servicebus.windows.net"

echo "-- relay app --"
az containerapp create \
  -g "$RG" -n "$RELAY_APP" --environment "$CAE_NAME" \
  --image "${ACR_LOGIN_SERVER}/relay:latest" \
  --registry-server "$ACR_LOGIN_SERVER" --registry-identity "$RELAY_RESOURCE_ID" \
  --user-assigned "$RELAY_RESOURCE_ID" \
  --ingress external --target-port 3978 \
  --min-replicas 1 --max-replicas 1 \
  --env-vars \
    "BOT_APP_ID=${BRAIN_CLIENT_ID}" \
    "BOT_TENANT_ID=${TENANT_ID}" \
    "SERVICEBUS_FULLY_QUALIFIED_NAMESPACE=${SB_FQNS}" \
    "SERVICE_BUS_QUEUE_NAME=${SB_QUEUE}" \
    "AZURE_CLIENT_ID=${RELAY_CLIENT_ID}" \
  >/dev/null

echo "-- brain app --"
az containerapp create \
  -g "$RG" -n "$BRAIN_APP" --environment "$CAE_NAME" \
  --image "${ACR_LOGIN_SERVER}/brain:latest" \
  --registry-server "$ACR_LOGIN_SERVER" --registry-identity "$BRAIN_RESOURCE_ID" \
  --user-assigned "$BRAIN_RESOURCE_ID" \
  --min-replicas 1 --max-replicas 1 \
  --env-vars \
    "BOT_APP_ID=${BRAIN_CLIENT_ID}" \
    "BOT_TENANT_ID=${TENANT_ID}" \
    "SERVICEBUS_FULLY_QUALIFIED_NAMESPACE=${SB_FQNS}" \
    "SERVICE_BUS_QUEUE_NAME=${SB_QUEUE}" \
    "AZURE_CLIENT_ID=${BRAIN_CLIENT_ID}" \
  >/dev/null

RELAY_FQDN=$(az containerapp show -g "$RG" -n "$RELAY_APP" --query properties.configuration.ingress.fqdn -o tsv)

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
