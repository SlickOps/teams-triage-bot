# Phase 5 — Monitoring, dedup, resolution

> Close the loop: don't post duplicates, track threads to an explicit resolution, and
> nudge lightly — while staying aware that a symptomatic fix leaves the underlying cause
> open.

## Goal
The bot recognizes likely **duplicates**, tracks the threads it creates to an **explicit
resolution**, and offers a **lightweight proactive nudge**.

## Objectives
- **Pre-post dedup:** before posting, search recent channel threads / open issues for a
  likely match (env + service + symptom). If **confident**, suggest *"this looks like
  <existing thread> — is it the same?"* and merge/redirect on confirm; **ask** when
  unsure. (POC uses a heuristic match, not ML clustering.)
- **Open-issue store (Cosmos):** track threads the bot created, with the Teams
  **conversation reference** for proactive follow-up.
- **Resolution:** explicit **resolution statement** — invoker or responder marks
  resolved via card/reaction; bot records it. **Datadog-recovery** signal as a
  supporting hint. Timeout → gentle **nudge** → escalate/deprioritize per simple rules.
- **Lightweight proactive nudge:** optionally detect a low-effort post and offer
  *"want me to help triage this?"* (full auto-triage stays out of scope).
- **Root-cause awareness while monitoring:** if the thread shows a symptomatic fix, the
  bot flags that the **underlying cause remains open**.

## Acceptance (demo)
1. Post two similar reports → bot flags the second as a likely duplicate and asks before
   posting.
2. Track a thread → mark **resolved** via card → bot records the resolution.
3. Let a thread go quiet past the timeout → bot posts a gentle nudge.
4. A responder posts a symptomatic "restarted it, works now" → bot notes the underlying
   cause is still unaddressed.

## Limits (deferred)
- Sophisticated incident-clustering / ML dedup — future (Phase 6); POC is heuristic +
  confirm.
- Auto-escalation policies are minimal.
- Autonomous nudging breadth kept narrow to avoid channel noise.
