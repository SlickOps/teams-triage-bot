# Phase 1 — Teams I/O (the plumbing)

> Prove the hardest infrastructure first: a message can travel
> Teams → relay → queue → brain (in AKS) → back to Teams, fully locked down,
> with zero AI and zero tools.

## Goal
A user @mentions or DMs the bot in the target Teams channel and gets a reply,
exercising the entire two-plane path end to end.

## Objectives
- **Bot registration:** Azure Bot Service resource; single-tenant AAD app (App ID);
  Teams channel enabled; Teams app manifest with **minimal RSC** scopes.
- **Edge relay (Container App):**
  - Messaging endpoint that receives Bot Framework activities.
  - **Bot Framework JWT validation** (issuer, signing keys, audience = App ID).
  - **IP allowlist** to the `AzureBotService` service tag.
  - **Tenant + channel pinning** (reject anything not from our tenant/channel).
  - Enqueue the validated activity to Service Bus. **No secrets, no infra access.**
- **Service Bus:** namespace + activity queue, reachable via Private Link.
- **Brain (AKS deployment):**
  - Consume the queue.
  - Trivial echo handler ("you said: …").
  - Reply via the **Bot Connector API** (outbound) using bot creds from **Key Vault via
    workload identity**.
  - Re-validate the activity origin (defense in depth across the queue).
- **AKS wiring:** Deployment + workload-identity federation + **egress allowlist**
  (Bot Connector, Service Bus only, for now).

## Acceptance (demo)
1. In the target channel, @mention the bot → it replies `you said: <text>`.
2. Send from a non-pinned tenant/channel → **rejected** (show logs/trace).
3. POST to the relay with a missing/invalid JWT → **rejected** with 401.
4. Kill the brain pod mid-flight → message is redelivered from Service Bus, no double-reply
   (idempotency check).

## Limits (deferred)
- No LLM, no MCP tools, no interview — echo only.
- No channel post-on-behalf, no routing, no Jira, no monitoring.
- Autonomous posting not applicable yet (all replies are direct responses).
