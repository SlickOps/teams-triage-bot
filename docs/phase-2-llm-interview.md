# Phase 2 — LLM brain + the grilling interview

> Turn the echo bot into an interviewer: it collects a well-formed, structured issue
> report from a low-effort starting point — still no investigation tools, still no
> channel post (drafts live in the DM).

## Goal
The bot conducts an **adaptive intake interview** and produces a structured, cited-back
issue summary the reporter can review.

## Objectives
- **Model integration:** MAF + `FoundryChatClient` (managed identity), streaming
  (`run_stream`). *(Run the MAF Python spike here if not already done.)*
- **Adaptive Card** for the essentials: environment, service, Jenkins job URL,
  symptom/error text. Submit → typed intake object.
- **Conversational follow-ups** for gaps, vagueness, or clarification.
- **Adaptive gate — not rigid.** "Required" fields flex: if Jenkins is down or a field
  genuinely doesn't apply, the bot accepts a justified N/A and proceeds. The gate is
  *"do we have enough to be useful?"*, expressed as a typed intake model with
  optional-with-reason semantics — **not** blind field-presence.
- **System prompt v1:** role + tone (grill for specifics, reject low-effort), error-
  pattern guides, and the hard rule: **never declare root cause**; output evidence +
  a labeled hypothesis only.
- **Interview state** in Cosmos/Table: TTL, resumable, one active interview per user.
- **Output:** structured summary object + a human-readable draft shown in the DM.

## Acceptance (demo)
1. DM the bot "dev37 is broken" → it refuses to accept that and grills for specifics via
   card + follow-ups.
2. Answer "Jenkins is down" → bot takes the adaptive path, doesn't dead-end on the
   missing job URL, and still produces a useful summary.
3. Final output is a clean, structured summary (env/service/symptom/what-we-know) with
   no autonomous root-cause claim.

## Limits (deferred)
- Summary is built from **user input only** — no Jenkins/Datadog investigation yet
  (Phase 3).
- No channel post, no routing, no Jira (Phase 4).
- No dedup or monitoring (Phase 5).
