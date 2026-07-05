# Phase 6 — Productionization (design backlog)

> Not scheduled work — a living backlog of the seams we deliberately left in the POC.
> Each item has a designed-for hook in earlier phases.

## Goal (future)
Graduate the POC into a production-grade, governed, autonomous service.

## Objectives (designed-for, not built)
- **Live ownership map:** replace the hardcoded mapping with the **Datadog service-
  catalog** integration, behind the Phase-4 interface.
- **Per-user authz / data-exposure controls:** if scope widens beyond lower envs, gate
  what the service-account access can surface per invoker.
- **Exit shadow mode:** autonomous posting gated on **confidence thresholds**.
- **Audit + cost:** full audit log (what was accessed / posted / ticketed), token/cost
  quotas, abuse protection.
- **Prod-env support:** integrate with the existing incident / on-call process
  (explicitly out of POC scope).
- **Ops hardening:** on-call for the bot itself, SLOs, richer degradation, retry /
  idempotency hardening.
- **Richer dedup:** incident correlation beyond heuristic matching.

## Acceptance
N/A — this is a backlog, prioritized when the POC proves value.

## Limits
Everything here is **out of scope** for the POC. The point of listing it is to keep the
earlier phases honest about where each seam leads.
