# infra/ -- Phase 1 provisioning

`provision.sh` is a plain `az cli` script (matches the style of
`spikes/azure-setup-log.md`, which has Terraform-conversion notes for later). Run it
after `az login` with the right subscription selected
(`c180582e-08f0-4c3c-98c6-4a9ba572f061`, "Azure subscription 1").

```bash
./infra/provision.sh
```

Re-running it is safe: identities, the bot, Service Bus, ACR, and the container apps
are all `create` calls that are idempotent/update-in-place; role assignments and the
Teams channel toggle print a note and continue if they already exist. ACR image builds
(`az acr build`) always rebuild from current `relay/`/`brain/` source.

A resource-name suffix is generated once and cached in `infra/.suffix` (gitignored) so
reruns target the same Service Bus namespace / ACR name rather than creating new ones.

## What it creates

| Resource | Purpose |
|---|---|
| `id-triage-brain`, `id-triage-relay` (managed identities) | No client secrets anywhere. `id-triage-brain`'s client ID **is** the bot's Microsoft App ID (UserAssignedMSI app type needs no separate AAD app registration). |
| Azure Bot (`teams-triage-poc-bot`) | Single-tenant, UserAssignedMSI, Teams channel enabled. |
| Service Bus namespace + `activities` queue | Decouples relay from brain. RBAC scoped to the queue: relay gets Sender, brain gets Receiver. |
| ACR | `az acr build` (cloud build -- no local Docker on this machine). |
| VNet + delegated subnet + NSG | **Not the ingress enforcement point.** Per Microsoft's ACA firewall-integration docs, on an *external* environment public inbound traffic bypasses the VNet, so inbound NSG rules don't apply to it. The VNet integration is kept anyway: free, and it's the seam for later-phase egress filtering (NSG *outbound* rules do apply) and private endpoints. The NSG's `AzureBotService` inbound rule only covers VNet-routed traffic (defense in depth). |
| Container Apps environment + `ca-triage-relay` + `ca-triage-brain` | VNet-integrated (must be chosen at creation), Consumption. Relay has external HTTP ingress; brain has none (queue consumer only). Both fixed at 1 replica -- brain scale-to-zero via KEDA is a cost nicety, not a Phase 1 requirement, so it's deferred. |
| Relay ingress allowlist (`ipSecurityRestrictions`) | **The actual ingress enforcement point**, applied at ACA's public endpoint (non-allowed IPs get 403 before reaching app code). CIDR-only (no service-tag support -- verified against the ARM spec), so provision.sh snapshots the `AzureBotService` tag's IPv4 ranges and applies all ~124 rules as one bulk ARM PATCH (per-rule CLI calls take ~20s each). The snapshot goes stale as Microsoft rotates ranges: **rerun provision.sh periodically to refresh** (the PATCH replaces the whole list). |

Resource provider registrations required first (one-time per subscription, already done
this session): `Microsoft.Network`, `Microsoft.App`, `Microsoft.ContainerRegistry`,
`Microsoft.ServiceBus`, `Microsoft.BotService`, `Microsoft.OperationalInsights`.

## Env vars produced (printed at the end of the script)

- `BOT_APP_ID` -- also `id-triage-brain`'s client ID; used by both apps for JWT/claims
  validation, and by `teams-app/manifest.json`'s `id`/`bots[].botId`.
- `BOT_TENANT_ID`
- The relay's messaging endpoint (`https://<fqdn>/api/messages`) -- set on the Azure Bot
  resource automatically by the script; needed if you re-register the bot manually.

## Known gotcha (per the MAF spike's Azure log)

RBAC role assignments can take a few minutes to propagate. If `az acr build`, the
container app's first pull, or a Service Bus send/receive 401s right after this script
finishes, wait ~2-5 min and rerun -- it's idempotent.

## Teardown

```bash
az group delete -n rg-teams-triage-poc --yes --no-wait
```

This deletes **everything** in the resource group, including the MAF spike's AI
Foundry account -- split it out first if you want to keep that around independently.
