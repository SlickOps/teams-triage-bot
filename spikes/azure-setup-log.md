# Azure setup log — MAF spike (manual az cli, for later Terraform conversion)

Everything below was created by hand via `az cli` to unblock `spikes/maf_python_spike.py`.
This is **throwaway POC infra**, not the real deployment — but logged verbatim so a real
implementation can express the same resources as Terraform.

- Subscription: `c180582e-08f0-4c3c-98c6-4a9ba572f061` ("Azure subscription 1", tenant
  Slicknet Wireless, `ef5ff41e-4a26-4f61-98b6-8bada7ac8e2f`)
- Region: `eastus2`
- Model: `gpt-5-mini` (OpenAI format, version `2025-08-07`), `GlobalStandard` SKU
  (pay-per-token, no idle cost), capacity `1` (1000 TPM)

### Model choice detour

Originally planned `gpt-4o-mini` per the mini-tier decision, but as of this run (2026-07-05):
- `gpt-4o-mini` (2024-07-18) is **fully deprecated** since 2026-03-31 — rejected under both
  `GlobalStandard` and `Standard` SKUs.
- `gpt-4.1-mini` (2025-04-14) is in **deprecating** state — rejected for new deployments.
- `gpt-5.4-mini` (2026-03-17, the current latest mini) had **0 quota** on `GlobalStandard`
  for this fresh subscription (`az cognitiveservices usage list -l eastus2` showed
  `limit: 0`) and no `Standard` SKU offering at all for that model.
- `gpt-5-mini` (2025-08-07) had 500k TPM `GlobalStandard` quota available and deployed
  cleanly — used that instead.

Lesson for Terraform/real-implementation: **check `az cognitiveservices usage list -l
<region>` for actual quota before picking a model/SKU** — deprecation status and default
quota (especially `GlobalStandard` on brand-new subscriptions) vary and can't be assumed
from docs alone.

## Commands run, in order

```bash
# One-time: register the resource provider (subscription had never used Cognitive Services)
az provider register --namespace Microsoft.CognitiveServices --wait

# Resource group
az group create -n rg-teams-triage-poc -l eastus2

# AI Foundry account (Cognitive Services "AIServices" kind = unified Foundry resource,
# --allow-project-management true enables Foundry projects on top of it)
az cognitiveservices account create \
  -n aif-triage-poc-567b31 \
  -g rg-teams-triage-poc \
  -l eastus2 \
  --kind AIServices \
  --sku S0 \
  --custom-domain aif-triage-poc-567b31 \
  --allow-project-management true \
  --yes

# Checked quota before picking a model (see detour above)
az cognitiveservices usage list -l eastus2

# Model deployment — gpt-5-mini, GlobalStandard sku, capacity 1
az cognitiveservices account deployment create \
  -n aif-triage-poc-567b31 \
  -g rg-teams-triage-poc \
  --deployment-name gpt-5-mini \
  --model-name gpt-5-mini \
  --model-version 2025-08-07 \
  --model-format OpenAI \
  --sku-name GlobalStandard \
  --sku-capacity 1
```

## Resources created

| Resource | Name | Notes |
|---|---|---|
| Resource group | `rg-teams-triage-poc` | eastus2 |
| Cognitive Services / AI Foundry account | `aif-triage-poc-567b31` | kind=AIServices, sku=S0, custom domain = account name |
| Model deployment | `gpt-5-mini` (2025-08-07) | GlobalStandard sku, **capacity 10** (bumped from 1 — see rate-limit note below), deploymentState=Running |

## RBAC grant (required — resource creation alone is not enough)

Even as the resource owner, calling the OpenAI data-plane API (`/openai/v1/responses`)
returned 401 until an explicit data-plane role was granted:

```bash
# Graph lookup by email failed (Authorization_RequestDenied — no directory-read perm);
# resolved the signed-in user's own object id instead, which worked with no extra permission:
az ad signed-in-user show --query "{id:id, upn:userPrincipalName}" -o json

ACCT_ID=$(az cognitiveservices account show -n aif-triage-poc-567b31 -g rg-teams-triage-poc --query id -o tsv)
az role assignment create \
  --assignee-object-id bc5b0098-b2cd-4647-baad-ab1df2a5c737 \
  --assignee-principal-type User \
  --role "Cognitive Services OpenAI User" \
  --scope "$ACCT_ID"
```

`Cognitive Services OpenAI User` alone still 401'd with a generic "Principal does not have
access to API/Operation" — the Foundry unified endpoint (`services.ai.azure.com`) needed a
second, broader role:

```bash
az role assignment create \
  --assignee-object-id bc5b0098-b2cd-4647-baad-ab1df2a5c737 \
  --assignee-principal-type User \
  --role "Azure AI Developer" \
  --scope "$ACCT_ID"
```

Terraform note: model both as `azurerm_role_assignment` (role definition names
`Cognitive Services OpenAI User` and `Azure AI Developer`) scoped to the
`azurerm_cognitive_account`, assigned to whatever principal (user, or in the real
deployment the AKS workload identity) needs to call the model. Worth determining in the
real implementation whether both are actually required or just `Azure AI Developer` alone
would have sufficed — didn't isolate that here.

Also note: RBAC took several minutes to propagate. The auth+model check flip-flopped
(401 → pass) and the streaming check flip-flopped independently across retries within the
first ~5 minutes after granting roles — budget for propagation delay, don't assume an
immediate 401 after a role grant means the grant was wrong.

## Capacity / rate-limit note

`GlobalStandard` deployment capacity directly sets the rate limit, and it's aggressive at
the minimum: **capacity `1` = 1 request/minute** (and 1000 TPM). The 3-check spike makes 2
back-to-back real model calls (auth+model, then streaming) and tripped the 1 RPM limit on
the second call. Bumped capacity to `10` (10 req/min, 10k TPM) via the same
`deployment create` command (idempotent update-in-place) and both checks passed together.
Terraform note: don't default `azurerm_cognitive_deployment` capacity to 1 for anything
that makes more than one call per minute, even in a POC.

## Outcome (updated: MCP check re-run for real, not skipped)

**All three checks PASS for real** (2026-07-05), including a genuine MCP-tool exercise
against a local server — not the earlier skip. Per `spikes/README.md`: **MAF-in-Python
is locked; proceed to Phase 1.** Pydantic AI fallback is no longer needed.

### MCP check: ran against the local "everything" reference server

Used the official MCP reference test server
(github.com/modelcontextprotocol/servers, `src/everything`) over Streamable HTTP, run
locally via npx (no clone needed, already published to npm):

```bash
npx -y @modelcontextprotocol/server-everything streamableHttp
# listens on http://localhost:3001/mcp (path is fixed at /mcp; PORT env var overrides 3001)
```

Then `export MCP_URL="http://localhost:3001/mcp"` before running the spike.

**Found a real bug via this check** (not a flake): with `MCPStreamableHTTPTool`'s
default `load_prompts=True`, agent_framework converts MCP *prompts* (not just tools)
into OpenAI function-calling schemas too. The everything server's `args-prompt` (an MCP
prompt with an optional string arg) got translated into a malformed schema (`None`
instead of `'string'` for the optional arg's type), which Azure OpenAI's function-schema
validator rejected with a 400 on `tools[15].parameters` — this looked like an MCP
integration failure at first, but discovery/attachment worked fine; only the
prompt-as-tool schema conversion was broken.

**Fix applied to `spikes/maf_python_spike.py`:** pass `load_prompts=False` to
`MCPStreamableHTTPTool`. This is also the right setting for production — real
Jenkins/Datadog MCP servers should expose tools, not have their prompts silently
surfaced as agent-callable functions.

Terraform/production note: N/A here (no Azure resource involved), but worth carrying
into Phase 3 (MCP investigation): **verify `load_prompts=False` (or no prompts
registered) on every real MCP server we attach**, since this failure mode is silent
until a prompt with an optional/nullable arg happens to exist on the server.

## Env vars produced (for `spikes/maf_python_spike.py`)

```bash
export FOUNDRY_PROJECT_ENDPOINT="https://aif-triage-poc-567b31.services.ai.azure.com/"
export FOUNDRY_MODEL="gpt-5-mini"
```

## Cleanup

To tear down everything created for this spike:

```bash
az group delete -n rg-teams-triage-poc --yes --no-wait
```

## Terraform notes (for the real implementation later)

- `azurerm_resource_group` → `rg-teams-triage-poc` equivalent
- `azurerm_cognitive_account` (kind = `AIServices`) for the Foundry resource; set
  `custom_subdomain_name`, and check the provider's project-management flag equivalent
- `azurerm_cognitive_deployment` for the model deployment (`GlobalStandard` sku) — pick the
  actual model/version at implementation time via `az cognitiveservices usage list`, don't
  hardcode `gpt-5-mini`; it was this run's pick, not a fixed decision
- Resource provider registration is subscription-level, one-time, not usually modeled in TF
  (or use `azurerm_resource_provider_registration` if the TF service principal needs it)
