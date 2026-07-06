# infra/ -- Phase 1 + Phase 2 provisioning

`provision.sh` is a plain `az cli` script (matches the style of
`spikes/azure-setup-log.md`, which has Terraform-conversion notes for later). Run it
after `az login` with the right subscription selected
(`c180582e-08f0-4c3c-98c6-4a9ba572f061`, "Azure subscription 1").

It provisions Phase 1 (Teams I/O plumbing) and Phase 2 (LLM brain: Foundry model
access + interview state storage) in one idempotent run.

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

### Phase 2 additions (LLM brain: Foundry + interview state)

| Resource | Purpose |
|---|---|
| AI Foundry RBAC (`Cognitive Services OpenAI User` + `Azure AI Developer` on `aif-triage-poc-567b31`, granted to `id-triage-brain`) | The brain calls the existing `gpt-5-mini` deployment (created by hand during the MAF spike, see `spikes/azure-setup-log.md`) via `FoundryChatClient` + managed identity. **provision.sh does not create the Foundry account or model deployment** -- it only grants RBAC, and fails loudly with a pointer to the spike log if the account is missing. Per the spike: the unified `services.ai.azure.com` endpoint 401s unless the caller has **both** roles; either alone is insufficient. |
| Storage account (`sttriagepoc<suffix>`) + `interviews` table | Interview state persistence (resumable, one active interview per user). RBAC-only (`Storage Table Data Contributor` on `id-triage-brain`) -- no account keys. The suffix matches the one in `infra/.suffix` used for Service Bus/ACR names. The `interviews` table is created lazily by the brain at runtime (create-if-not-exists); provision.sh best-effort attempts to create it too, but a failure there (RBAC propagation) is non-fatal. |

Resource provider registrations required first (one-time per subscription, already done
this session): `Microsoft.Network`, `Microsoft.App`, `Microsoft.ContainerRegistry`,
`Microsoft.ServiceBus`, `Microsoft.BotService`, `Microsoft.OperationalInsights`,
`Microsoft.Storage`, `Microsoft.CognitiveServices`.

## Env vars produced (printed at the end of the script)

- `BOT_APP_ID` -- also `id-triage-brain`'s client ID; used by both apps for JWT/claims
  validation, and by `teams-app/manifest.json`'s `id`/`bots[].botId`.
- `BOT_TENANT_ID`
- The relay's messaging endpoint (`https://<fqdn>/api/messages`) -- set on the Azure Bot
  resource automatically by the script; needed if you re-register the bot manually.
- Phase 2, set on `ca-triage-brain` (via `--env-vars` on create, and again idempotently
  via `containerapp update --set-env-vars` so reruns stay correct even if the app
  already existed):
  - `FOUNDRY_PROJECT_ENDPOINT` -- `https://aif-triage-poc-567b31.services.ai.azure.com/`
  - `FOUNDRY_MODEL` -- `gpt-5-mini`
  - `STATE_STORAGE_ACCOUNT` -- the generated `sttriagepoc<suffix>` name
  - `INTERVIEW_TABLE_NAME` -- `interviews`
  - (`AZURE_CLIENT_ID` continues to be `id-triage-brain`'s client ID; the brain's
    `DefaultAzureCredential` uses it for Service Bus, Foundry, and Storage alike.)

New outbound egress the brain now needs (beyond Service Bus, from Phase 1):
`*.services.ai.azure.com` (Foundry) and `*.table.core.windows.net` (Storage). No egress
allowlist is implemented yet -- this is the same deferred seam noted in the Phase 1 VNet
comments in `provision.sh`.

## Verifying

```bash
./infra/verify_phase1.sh   # Teams I/O plumbing (relay auth, ingress lockdown)
./infra/verify_phase2.sh   # Foundry RBAC, storage RBAC, brain env vars
```

`verify_phase2.sh` only checks static config (role assignments, resource existence, env
vars on the Container App spec) -- it doesn't exercise the brain's actual runtime calls
to Foundry or Storage.

## Known gotcha (per the MAF spike's Azure log)

RBAC role assignments can take a few minutes to propagate. If `az acr build`, the
container app's first pull, a Service Bus send/receive, or a Foundry/Storage call 401s
right after this script finishes, wait ~2-5 min and rerun -- it's idempotent.

## Teardown

```bash
az group delete -n rg-teams-triage-poc --yes --no-wait
```

This deletes **everything** in the resource group, including the MAF spike's AI
Foundry account -- split it out first if you want to keep that around independently.
