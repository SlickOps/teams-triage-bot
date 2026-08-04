# Faster Triage & Safe Deploys — a 3-part proposal

**Applies to all teams:** QA, Development (Platform & Editor groups, 16 teams), and Infrastructure.

## The problem

We run 80+ environments, and Dev and QA almost always **"deploy from latest"** — pulling the
newest images out of the CI environments to do their work. When a bad merge to `main` puts faulty
images in CI, that fault propagates silently: developers innocently deploy `latest` for **weeks**,
unaware they're spreading a known-broken build. The result is cascading failures across
environments, a Troubleshooting channel flooded with downstream symptoms, and **no one owning the
original catastrophic merge**. This is our single largest source of prolonged, ownerless
disruption — and because our app code is often not schema-forward, we **cannot guarantee we can
roll it back** once it has spread.

This proposal is three changes that work together to fix it: a **guided intake bot** that makes
reporting easy and precise, a **triage flow** that routes by evidence, and a **CI deploy
interlock** that stops a bad `main` from spreading in the first place.

## Part 1 — Guided intake bot (Teams)

A Teams bot is the front door for issues. It **interviews the reporter** with follow-up questions
until it has env, service, symptoms, logs, and the Jenkins deploy link, then uses AI with
**read-only** MCP integrations to **gather the evidence itself**: Jenkins build/deploy logs, and
Datadog logs, APM traces, and pod status. It outputs a **clear, evidence-backed problem statement**
and **identifies the likely owning team** from the service-ownership catalog. *The reporter gets
faster help; the owning team gets a real ticket instead of "env broken, please fix."*

## Part 2 — Triage flow (routing by evidence)

- **Each team keeps someone on-deck** during working hours — Development and Infrastructure both.
- **QA → their Development team** directly for application behavior.
- **Development investigates first**, including **engaging other Dev teams** across service
  boundaries, before escalating outward.
- **The bot's evidence packet is the ticket of entry** for cross-team escalation.
- **Infra engages** on any bot-routed platform issue, anything platform-wide, or anything Infra
  spots first.

## Part 3 — CI deploy interlock ("andon cord")

When a merge to `main` fails to deploy to a CI environment, that environment likely contains bad
code that must not spread:

- The affected CI environment(s) are **automatically gated** (Jenkins Lockable Resources).
- **Image promotion to higher environments is blocked.**
- **`Deploy-from-latest` by Dev and QA is blocked** for the affected images — the fault can't be
  pulled into the 80+ environments while the gate is down.
- **PR and feature builds continue normally**, including the fix for `main`.
- *This is a safety interlock — fail-safe and quick to clear — not an incident and not a block on
  legitimate work.*

**Remediation.** The SOP is **forward-fix**: land a fix to `main` and go green. Because our schemas
aren't reliably backward-compatible, **rollback is not a standard procedure** — only the owning devs
can judge whether a change is safely reversible, and they're free to do so via a new commit
reverting the change. Rollback is one possible remediation, not a guarantee.

**The taboo, named directly.** While the gate is down, further merges to `main` will fail (we block
the post-merge build). "Blocking merges to main" carries cultural weight — but the merge isn't what
breaks `main`, **the bad code already did.** The gate only makes that breakage *visible and
contained* instead of silently spreading via `latest`. A red `main` *should* stop the line; that is
precisely the andon cord.

**Lifting the gate is a responsibility, not a button.** A gate is cleared by any **Development or
Infrastructure manager/director** once `main` is green. A manager who lifts the gate **before** a
fix is expected to:

- **Communicate project-wide** the status of the issue,
- State **what everyone must do to mitigate** until it's fixed, and
- **Have their team own and support** all downstream issues that arise from letting the bad code
  proliferate.

Shared authority, shared accountability.

## How we'll measure success

- **Weeks of cascading, ownerless disruption eliminated** — the propagation-via-`latest` failures
  the interlock prevents outright.
- Reduction in **mis-routed / low-context issues** and **time-to-right-owner**.
- Count of **bad-image promotions and `latest` deploys blocked**.

## What we're asking for

1. Each team **staffs the channel** with an on-deck watcher.
2. Issues **route by evidence** through the bot's intake.
3. Dev groups **increase service-catalog granularity to team level** so routing improves for
   everyone.
4. A **red `main` is a stop-the-line event**, cleared jointly and communicated broadly.
