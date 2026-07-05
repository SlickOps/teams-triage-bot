# Phase 4 — Routing, on-behalf post, Jira propose

> The bot becomes useful to the channel: it routes to the owning team, writes the crux
> (not the logs), separates immediate vs underlying, flags cowboy fixes, and proposes a
> Jira ticket — with a human hitting Send (shadow mode).

## Goal
The bot composes a **channel-ready post** routed to the right team, separating the
immediate issue from the underlying cause, and **proposes** a Jira ticket — a human
confirms before anything posts.

## Objectives
- **Ownership map:** POC **hardcoded** mapping (env/service → team) in config/prompt,
  behind an interface so the **Datadog service-catalog** integration can replace it
  later without touching callers. *(Designed-for, not built.)*
- **Routing:** map lookup **in code** → owning team; @mentions constructed by code from
  the map (injection-safe).
- **On-behalf post:** concise **crux** + correlation + evidence links + recommended
  owner. Full logs are **not** pasted — just the crux.
- **Immediate vs. underlying:** output structure that separates the immediate issue from
  the underlying cause, and explicitly flags:
  - symptomatic / **"cowboy" manual fixes** that will recur without addressing the root;
  - **other issues** mentioned in the thread worth a follow-up.
- **Shadow mode:** bot posts the draft to the invoker (card with **Send / Edit /
  Cancel**); the human confirms before it hits the channel.
- **Jira propose-and-confirm:** draft ticket (title/description/links); user confirms to
  create. Ticket base fields (project, issue type, labels) defined in this phase.

## Acceptance (demo)
1. End-to-end on a broken env: bot drafts a channel-ready post with the **correct team
   @mention** (from the map), an **immediate + underlying** breakdown, a flagged
   cowboy-fix note, and a **proposed Jira ticket**. You click **Send**.
2. Change the mapping config → routing target changes accordingly.
3. Edit the draft before sending → edits are respected.

## Limits (deferred)
- **Autonomous** (non-shadow) posting — deferred to Phase 6.
- Real Datadog service-catalog integration — deferred (Phase 6); interface is in place.
- No duplicate detection or thread monitoring yet (Phase 5).
