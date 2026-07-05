# teams-triage-bot

An AI triage agent for a Microsoft Teams troubleshooting channel. When something breaks
in a lower environment, the bot grills the reporter for specifics, investigates via
**Jenkins** and **Datadog**, correlates the deploy to the symptom, and drafts a clean,
correctly-routed post (plus a proposed Jira ticket) on the user's behalf — separating the
*immediate* issue from the *underlying* cause so the same fire doesn't keep restarting.

> Its whole job: make the person prove where the fault actually is *before* the infra
> team gets volunteered to fix someone else's bad deploy.

**Status:** POC.

> **Persona / display name is configurable.** The bot's user-facing name is a config
> value, not hardcoded — set it per deployment. Docs refer to it generically as "the
> bot."

## Design docs

Start with the [overview](docs/00-overview.md) (architecture, stack, security, decisions
log, phase map). Each phase is independently demonstrable and uses a **G/O/A/L** template
(Goal · Objectives · Acceptance · Limits):

| Phase | Doc |
|---|---|
| 1 — Teams I/O (plumbing) | [phase-1-teams-io.md](docs/phase-1-teams-io.md) |
| 2 — LLM interview | [phase-2-llm-interview.md](docs/phase-2-llm-interview.md) |
| 3 — MCP investigation | [phase-3-mcp-investigation.md](docs/phase-3-mcp-investigation.md) |
| 4 — Routing / post / Jira | [phase-4-routing-posting-jira.md](docs/phase-4-routing-posting-jira.md) |
| 5 — Monitoring / dedup | [phase-5-monitoring-dedup.md](docs/phase-5-monitoring-dedup.md) |
| 6 — Productionization (backlog) | [phase-6-future-productionization.md](docs/phase-6-future-productionization.md) |

## Architecture at a glance

Two-plane design: an internet-facing, privilege-free **edge relay** validates the Teams
webhook and enqueues to Service Bus; the privileged **brain** (Microsoft Agent Framework,
Python) runs inside AKS next to Jenkins with **zero inbound** — it only makes outbound
calls. See the overview for the full diagram and trust model.
