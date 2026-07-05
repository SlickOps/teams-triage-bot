# Teams Triage Bot — Design Overview

An AI agent in Microsoft Teams that intercepts low-effort "it's broken, please fix"
posts, **grills the reporter** for specifics, **investigates** via Jenkins + Datadog,
**routes** to the right team, and **drafts a clean post + Jira ticket** on the user's
behalf — while separating the *immediate* issue from the *underlying* cause.

This is a **POC**. Where a capability is expensive (a live service-catalog integration,
per-user authz, autonomous posting), we build the simple version now and **design the
seam** so the real thing drops in later.

---

## The product in one paragraph

A dev pings the bot (or the bot offers to help on a low-effort post). The bot runs an
**adaptive intake interview** (Adaptive Card for the essentials + conversational
follow-ups), pulls the **Jenkins job params + log** and **Datadog** (pod status,
version/deploy events, errors, APM, logs), builds an **evidence-cited timeline**, and
produces a **labeled hypothesis** — never an authoritative root-cause claim. It then
drafts a channel-ready post routed to the owning team, **flags symptomatic "cowboy"
fixes and any other issues** mentioned, and proposes a Jira ticket. A human confirms
before anything is posted (**shadow mode**). The bot then **monitors the thread to an
explicit resolution** and recognizes likely **duplicates**.

---

## Architecture — two planes, brain has zero inbound

The Teams channel is *push*: Microsoft's Bot Connector POSTs to a messaging endpoint.
Only that incoming activity needs an inbound door. Everything else the bot does is
*outbound*. So we split into a privilege-free edge and a privileged brain.

```
   Teams / Bot Connector (Microsoft cloud)
        │  inbound webhook (the ONLY inbound in the system)
        ▼
 ┌──────────────────────────────┐
 │  Edge relay (Container App)   │  ← internet-facing, no secrets, no infra access
 │  • validate Bot Framework JWT │
 │  • IP-allowlist Bot Connector │
 │  • tenant/channel pinning     │
 │  • enqueue activity           │
 └───────────────┬──────────────┘
                 │ Service Bus (Private Link)
   ══════════════▼═══════════════  AKS boundary (NO inbound)
 ┌──────────────────────────────┐
 │  Brain (MAF / Python)         │
 │  • pull from Service Bus (out)│
 │  • Jenkins MCP → in-cluster   │──► Jenkins (ClusterIP, never public)
 │  • Datadog / Jira / Foundry(out)
 │  • reply via Bot Connector(out)│──► Teams
 │  • state → Cosmos/Table        │
 └───────────────┬──────────────┘
                 │ egress allowlist only ↓
     Bot Connector · Service Bus · Datadog · Jira · AI Foundry
```

**Trust gradient:** internet → privilege-free relay → queue → privileged brain. The
brain has no inbound path; if the relay is compromised, the worst case is a forged
Teams activity, which the brain re-authenticates anyway.

---

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Teams front door | Azure Bot Service + Bot Framework / M365 Agents SDK plumbing | Relay uses the low-level activity/JWT layer, **not** the Teams AI Library AI layer |
| Edge relay | Azure Container Apps | Only internet-facing piece; stateless; no secrets |
| Queue | Azure Service Bus (Private Link) | Decouples relay from brain; gives brain outbound-only |
| Brain | **Microsoft Agent Framework (Python)** | Pydantic AI is the fallback if the MAF spike disappoints |
| Model | Azure AI Foundry via `FoundryChatClient` + managed identity | Verified Python support (MCP, Foundry client) |
| Tools | MCP (Datadog, Jenkins) + Jira REST | Extensible tool registry (add server = config) |
| State | Cosmos DB / Table Storage | Interview state, open issues, conversation references |
| Secrets | Azure Key Vault + AKS workload identity | No static creds in cluster |
| Hosting (brain) | AKS (co-located with Jenkins) | In-cluster reach to Jenkins; egress allowlist |
| Observability | OpenTelemetry → **Datadog** | Uses tooling you already run |

> **Open risk to close first:** a 30-min spike confirming MAF Python `run_stream` +
> one `MCPStreamableHTTPTool` against your real Foundry endpoint. If it's rough, swap
> the brain to Pydantic AI — everything else (topology, tools, state) is unchanged.

---

## Cross-cutting design principles & guardrails

These apply to **every** phase; they are not a phase of their own.

1. **Tool output is raw data, never instructions.** Jenkins/Datadog content is passed
   to the model as a delimited data block (separate content), never concatenated into
   the system/instruction prompt. Neutralizes prompt injection from log text.
2. **Actions are constructed from code + maps, never from model free-text.** Team
   routing and @mentions come from the ownership-map lookup in code. An injected "ping
   @everyone" in a log can never become a real mention.
3. **No autonomous root-cause claims.** The model outputs an evidence-cited timeline
   and a **clearly labeled hypothesis**. Prompts include error-pattern guides to help
   classify, but the bot never asserts *the* cause on its own.
4. **Immediate vs. underlying.** Every triage separates the immediate issue from the
   underlying cause, and flags symptomatic/"cowboy" manual fixes and any other issues
   raised in the thread.
5. **Tool-call budget cap.** Bounds runaway loops from adversarial or noisy input.
6. **Output redaction pass.** Regex scrub of token-shaped strings before anything is
   user-visible (belt-and-suspenders even though creds aren't logged).
7. **Shadow mode.** The bot drafts; a human hits send. Autonomous posting is a later
   graduation, gated on confidence.
8. **Graceful degradation.** If a tool (e.g. Datadog MCP) is down, post a partial
   triage that says so, rather than failing silently.

---

## Security model (summary)

- **Inbound auth:** Bot Framework JWT validation at the relay *and* re-checked at the
  brain across the queue; IP allowlist to the `AzureBotService` service tag; single-
  tenant AAD app; tenant + channel pinning.
- **Who can invoke:** gate privileged actions behind an AAD group.
- **Access scope:** read-only investigation + Jira-create; **no remediation**;
  **lower environments only**.
- **Secrets/identity:** Key Vault + workload identity; least-privilege service accounts
  per tool (read-only Datadog/Jenkins, scoped Jira).
- **Egress:** allowlist to Bot Connector, Service Bus, Datadog, Jira, Foundry only.

---

## Decisions log (frozen for v1)

| Area | Decision |
|---|---|
| Brain | MAF (Python); Pydantic AI fallback pending spike |
| Topology | Two-plane; brain in AKS with zero inbound |
| Invocation | Invoked + lightweight proactive nudge; opt-in |
| Access | Read-only investigation + Jira-create; service-account exposure accepted |
| Environments | Lower envs only (dev/test); prod out of scope for POC |
| Interview UX | Adaptive Card for essentials + conversational follow-ups; **adaptive** required fields |
| Diagnosis | Evidence + labeled hypothesis; no autonomous root cause; error-pattern guides in prompt |
| Root-cause awareness | Separate immediate vs underlying; flag cowboy fixes + other issues |
| Ownership map | POC hardcoded mapping; Datadog service-catalog integration designed-for-later |
| Correlation | Jenkins job params (targets) + Datadog version/deploy events + timeline |
| Dedup | Heuristic match + ask/confirm; merge when confident |
| Resolution | Explicit resolution statement + Datadog-recovery hint + timeout nudge |
| Jira | Propose-and-confirm; ticket base fields added in that phase |
| Rollout | Shadow mode |
| Guardrails | Tool output as raw data; actions from code/maps; tool-call budget; redaction pass |
| Observability | OTel → Datadog; graceful degradation |
| Persona | Bot display/persona name is a **config value**, not hardcoded |

---

## Phase map (each phase is independently demonstrable)

| Phase | Delivers | Demo |
|---|---|---|
| **1 — Teams I/O** | Full inbound→relay→queue→brain→outbound path on AKS | @mention → echo reply; rejects bad JWT / wrong tenant |
| **2 — LLM interview** | Adaptive grilling → structured summary (in DM) | Bot grills you, handles "Jenkins is down" path, refuses low-effort |
| **3 — MCP investigation** | Extensible MCP tools + evidence-cited correlation | Investigates a real broken env; labeled hypothesis; add a tool via config |
| **4 — Routing / post / Jira** | Team routing, on-behalf draft, immediate-vs-underlying, Jira propose | Drafts channel post w/ correct team + cowboy-fix flag; you hit Send |
| **5 — Monitoring / dedup** | Duplicate detection, thread-to-resolution tracking, nudge | Flags a duplicate; marks resolution via card; nudges a quiet thread |
| **6 — Productionization** | Design backlog (catalog, authz, autonomous, audit, prod) | N/A (future) |

Each phase doc uses the **GOAL** template: **G**oal · **O**bjectives · **A**cceptance
(the demo) · **L**imits (deferred, with a pointer to where it lands).
