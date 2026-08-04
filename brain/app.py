import asyncio
import json
import logging
import os
from collections import OrderedDict
from urllib.parse import urlparse

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from botbuilder.core import TurnContext
from botbuilder.schema import Activity
from botframework.connector.auth import ClaimsIdentity

from cards import build_intake_card, parse_card_submit
from followup import FollowupError, run_followup
from intake import IntakeField, StructuredSummary
from interviewer import InterviewerError, run_turn
from investigation import InvestigationError, InvestigationReport, run_investigation
from lifecycle import is_new_incident, is_retry_request
from redact import redact
from reply import build_adapter, send_card, send_text
from state_store import InterviewState, build_state_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("brain")

_NAMESPACE = os.environ["SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"]
_QUEUE_NAME = os.environ.get("SERVICE_BUS_QUEUE_NAME", "activities")
_TENANT_ID = os.environ["BOT_TENANT_ID"]
_APP_ID = os.environ["BOT_APP_ID"]

# Interview lifecycle states that count as "finished" -- a new free-text message
# while in one of these starts a fresh incident (see handle_envelope). Keep in
# sync with the statuses set in _run_interview_turn / _run_investigation_step.
_TERMINAL_STATUSES = {"complete", "investigated"}

_INTRO_LINE = (
    "Got it -- let's nail down the specifics. I've dropped a quick form below; "
    "fill in what you can (blanks are fine if something genuinely doesn't apply -- "
    "just tell me why in chat)."
)

# A submit arrived from a form belonging to an already-finished interview. Its
# fields still carry the *previous* incident's answers, so we deliberately don't
# merge them (that's what mixed two incidents together before).
_STALE_CARD_LINE = (
    "That form was from an earlier report, so I left the previous one as-is. "
    "If this is a new issue, just tell me what's happening (or say \"new issue\") "
    "and I'll start a fresh report."
)

# Shown when an interview is "complete" but its investigation failed and the
# user sends something that's neither a retry request nor a new incident.
_COMPLETE_NUDGE_LINE = (
    "That report is summarized above, but I couldn't finish the investigation. "
    "Reply \"retry\" and I'll take another run at it, or describe a new problem "
    "to start a fresh report."
)


def _activity_tenant_id(activity: Activity) -> str:
    channel_data = activity.channel_data if isinstance(activity.channel_data, dict) else {}
    tenant = channel_data.get("tenant")
    if isinstance(tenant, dict) and tenant.get("id"):
        return tenant["id"]
    return getattr(activity.conversation, "tenant_id", None)


def _activity_user_id(activity: Activity) -> str:
    """Prefer the AAD object id (stable, tenant-scoped identity); fall back to
    the channel-assigned `from.id` for channels/configurations where the AAD
    object id isn't populated."""
    from_property = activity.from_property
    aad_object_id = getattr(from_property, "aad_object_id", None)
    if aad_object_id:
        return aad_object_id
    return from_property.id


def _render_summary_markdown(summary: StructuredSummary) -> str:
    lines = [
        f"**Environment:** {summary.environment}",
        f"**Service:** {summary.service}",
    ]
    if summary.jenkins_job_url:
        lines.append(f"**Jenkins:** {summary.jenkins_job_url}")
    lines.append(f"**Symptom:** {summary.symptom}")
    lines.append(f"**What we know:** {summary.what_we_know}")
    if summary.open_questions:
        lines.append(f"**Open questions:** {summary.open_questions}")
    return "\n\n".join(lines)


# label/ref on an Evidence are model-controlled, and the model reads untrusted
# tool logs -- so a copied injection string could otherwise smuggle Teams
# mention/link markup into a citation. These keep a citation inert text, upholding
# the "rendering never becomes an action" guardrail (docs/00-overview.md #2).
_LABEL_MD_ESCAPE = str.maketrans({c: "\\" + c for c in "\\`*_[]()<>~|!#"})


def _sanitize_label(label: str) -> str:
    """Neutralize markdown/mention metacharacters so a label can't open a link,
    image, code span, or HTML. '@' is split with a zero-width space so a copied
    "@everyone" can't read as a mention."""
    return label.translate(_LABEL_MD_ESCAPE).replace("@", "@\u200b")


def _safe_ref(ref: str | None) -> str | None:
    """Allowlist real http(s) URLs as the only thing that may become a clickable
    link; anything else (javascript:/data:, a bare id, a model-invented scheme)
    is dropped so the citation degrades to an inert label."""
    if not ref:
        return None
    parsed = urlparse(ref)
    return ref if parsed.scheme in ("http", "https") and parsed.netloc else None


def _render_investigation_markdown(report: InvestigationReport) -> str:
    """Render an InvestigationReport into a Teams-friendly markdown message.
    Rendering is pure string assembly from typed fields -- it never echoes
    raw tool output directly, and everything here still passes through
    redact() before being sent (docs/00-overview.md guardrail #6), same as
    _render_summary_markdown above. There is deliberately no "act on this"
    step: nothing here becomes an @mention or a routing decision (that's
    Phase 4, and even then it comes from the ownership map, never from this
    text) -- see docs/00-overview.md guardrail #2."""
    lines = [f"**Investigation: {report.environment} / {report.service}**"]
    if report.change:
        lines.append(f"**Deploy/change:** {report.change}")
    lines.append(f"**Impact:** {report.impact}")
    lines.append(f"**Hypothesis (unconfirmed):** {report.hypothesis}")
    if report.evidence:
        # Compact clickable citations on ONE line -- links where we have a URL,
        # bare labels otherwise. Deliberately NOT the detail text: the body must
        # stay short (a wall of text gets ignored), and the actual facts already
        # live in change/impact/hypothesis. ev.detail is still kept in the stored
        # report for a richer view later (e.g. an Adaptive Card of evidence).
        cites = " · ".join(
            f"[{_sanitize_label(ev.label)}]({ref})" if (ref := _safe_ref(ev.ref)) else _sanitize_label(ev.label)
            for ev in report.evidence
        )
        lines.append(f"**Evidence:** {cites}")
    if report.injection_flagged:
        lines.append("⚠️ A tool log contained a prompt-injection attempt; treated as data, not acted on.")
    if report.tools_unavailable:
        lines.append(f"**Tools unavailable:** {', '.join(report.tools_unavailable)}")
    return "\n\n".join(lines)


# Phase 1 stand-in for real dedup: an in-memory, bounded LRU, scoped to this
# process's lifetime only. It does NOT survive a pod restart -- true idempotency
# across restarts needs the Cosmos/Table state store planned for a later phase.
# What peek-lock buys us regardless: an uncompleted message (killed before this
# point) is redelivered rather than lost, with no double reply, because it's
# only ever fully processed once. Capped since redelivery only ever happens
# within a short window, so old IDs are safe to evict long before that.
_SEEN_ACTIVITY_IDS_MAX = 4096
_seen_activity_ids: "OrderedDict[str, None]" = OrderedDict()


def _mark_seen(activity_id: str) -> None:
    _seen_activity_ids[activity_id] = None
    _seen_activity_ids.move_to_end(activity_id)
    if len(_seen_activity_ids) > _SEEN_ACTIVITY_IDS_MAX:
        _seen_activity_ids.popitem(last=False)


async def _run_interview_turn(turn_context, state: InterviewState, store) -> None:
    """The interview logic proper. Runs inside the adapter's callback (so it
    has a turn_context to send with) but does its own state persistence, since
    persistence doesn't depend on the turn_context.

    A model failure (InterviewerError) is handled HERE -- persist what we have
    and tell the user once -- rather than re-raised. Re-raising would abandon the
    queue message, and a *deterministic* model error would then redeliver and
    re-fail up to Service Bus's max-delivery count (10x), spamming the user with
    identical snack messages before dead-lettering. Completing the message
    instead means a rare transient blip drops a single turn -- the user just
    sends again, and because the merged input was persisted the interview resumes
    with nothing lost. Genuine crashes (not InterviewerError) still propagate out
    of process_proactive -> abandon -> redelivery, preserving Phase 1's
    crash-safety (docs/00-overview.md guardrail #8)."""
    if not state.card_sent:
        # First turn of a brand-new interview: the card collects the essentials
        # directly, so we send it (plus a short grilling line) and skip the
        # model entirely this turn -- simpler than also asking the model to
        # react to the very first low-effort message.
        await send_text(turn_context, redact(_INTRO_LINE))
        await send_card(turn_context, build_intake_card())
        state.card_sent = True
        await store.put(state)
        return

    try:
        turn = await run_turn(state)
    except InterviewerError:
        logger.exception("interview turn failed for user %s", state.user_id)
        # Persist what we have (including any card answers just merged in
        # handle_envelope) so the reporter's input survives the failure, then
        # degrade gracefully with a single message. Do NOT re-raise -- see the
        # docstring for why abandoning here would spam the user 10x.
        await store.put(state)
        await send_text(
            turn_context,
            redact("I hit a snag reaching my brain -- please send that again in a moment."),
        )
        return

    state.intake = turn.intake
    state.transcript.append({"role": "bot", "text": turn.reply_to_user})

    if not (turn.enough_to_be_useful and turn.summary is not None):
        await send_text(turn_context, redact(turn.reply_to_user))
        await store.put(state)
        return

    state.status = "complete"
    # Persist the summary so a failed investigation can be retried from it
    # (see the "complete" branch in handle_envelope) without re-interviewing.
    state.structured_summary = turn.summary.model_dump_json()
    await send_text(turn_context, redact(turn.reply_to_user))
    await send_text(turn_context, redact(_render_summary_markdown(turn.summary)))
    await store.put(state)
    # Chained here (rather than left to the next inbound message) so the
    # investigation runs immediately once the interview has enough to go on
    # -- see _run_investigation_step's docstring for why its own persistence
    # and failure handling are split out from the interview's.
    await _run_investigation_step(turn_context, state, store, turn.summary)


async def _run_investigation_step(turn_context, state: InterviewState, store, summary: StructuredSummary) -> None:
    """Phase 3: once the interview produced a usable summary, hand it to the
    MCP-backed investigator and post the result. Split out from
    _run_interview_turn so the interview's own persistence (state.status=
    "complete") is already durable before we attempt the (network-heavy,
    more failure-prone) investigation step -- a failure here should not cost
    the user the summary they already have.

    Same InvestigationError-catch-and-degrade shape as run_turn's
    InterviewerError handling above: a deterministic model/tool failure gets
    ONE apologetic message and the turn ends cleanly, rather than re-raising
    (which would abandon the queue message and redeliver up to 10x, per
    _run_interview_turn's docstring)."""
    await send_text(
        turn_context,
        redact(f"\U0001f50d Investigating {summary.environment}/{summary.service}..."),
    )
    try:
        report = await run_investigation(summary)
    except InvestigationError:
        logger.exception("investigation failed for user %s", state.user_id)
        await send_text(
            turn_context,
            redact(
                "I couldn't complete the investigation (hit a snag reaching my tools or "
                "brain) -- the summary above is still good; a human can take it from there. "
                "Reply \"retry\" and I'll take another run at it."
            ),
        )
        return

    state.status = "investigated"
    state.investigation_report = report.model_dump_json()
    await send_text(turn_context, redact(_render_investigation_markdown(report)))
    await store.put(state)


async def _reply_once(adapter, store, state, activity, claims, audience, callback) -> None:
    """Run `callback` inside a proactive turn, then durably record this activity
    as completed on the user's state row and put() once more. That durable
    marker (unlike _seen_activity_ids, which dies with the process) makes a
    redelivery after a pod restart a no-op. The put also refreshes the state's
    TTL. It re-persists any mutations the callback made to `state`."""
    claims_identity = ClaimsIdentity(claims=claims, is_authenticated=True)
    await adapter.process_proactive(claims_identity, activity, audience, callback)
    state.last_completed_activity_id = activity.id
    await store.put(state)
    _mark_seen(activity.id)
    logger.info("replied to activity %s", activity.id)


async def handle_envelope(adapter, store, envelope: dict) -> None:
    activity = Activity.deserialize(envelope["activity"])
    claims = envelope["claims"]
    audience = envelope["audience"]

    # Re-validate what the relay handed us -- defense in depth, since the brain
    # can't re-check the original JWT signature after the queue hop. Channel
    # tokens carry no tid claim; the Teams tenant lives on the activity
    # (channelData.tenant.id), and the token's audience must be our app.
    if claims.get("aud") not in (_APP_ID, f"api://{_APP_ID}"):
        logger.warning("dropping activity %s: audience mismatch in queued claims", activity.id)
        return
    if _activity_tenant_id(activity) != _TENANT_ID:
        logger.warning("dropping activity %s: tenant mismatch on activity", activity.id)
        return
    if activity.channel_id != "msteams":
        logger.warning("dropping activity %s: channel mismatch", activity.id)
        return
    if activity.type != "message":
        logger.info("dropping activity %s: not a message activity (type=%s)", activity.id, activity.type)
        return
    if activity.from_property is None:
        # A well-formed message activity always carries a sender. A malformed one
        # would otherwise raise in _activity_user_id and abandon->redeliver in a
        # loop until dead-letter; drop it like the other validation failures.
        logger.warning("dropping activity %s: missing from_property (sender identity)", activity.id)
        return

    if activity.id in _seen_activity_ids:  # in-process fast path
        logger.info("skipping already-processed activity %s", activity.id)
        return

    user_id = _activity_user_id(activity)
    is_card_submit = isinstance(activity.value, dict) and bool(activity.value)
    # In channels the incoming text includes the "@Bot Name" mention markup;
    # strip it so we only see what the user actually typed.
    user_text = "" if is_card_submit else (
        TurnContext.remove_recipient_mention(activity) or activity.text or ""
    ).strip()

    # Storage errors now propagate (state_store.get only swallows "not found"),
    # so a transient Table outage abandons the message for redelivery rather
    # than looking like "no interview" and wiping one in progress.
    state = await store.get(user_id)

    # Durable dedup: this exact activity was already fully handled. Survives the
    # restart that _seen_activity_ids does not.
    if state is not None and activity.id == state.last_completed_activity_id:
        logger.info("skipping already-completed activity %s (durable marker)", activity.id)
        _mark_seen(activity.id)
        return

    # A free-text message on a TERMINAL interview that looks like a genuinely new
    # problem starts a fresh incident. This replaces the old "any message after
    # terminal resets" rule, which merged unrelated incidents (an earlier symptom
    # bleeding into a new environment). Card submits carry no fresh description,
    # so they never trigger this -- a stale one is handled just below instead.
    if (
        not is_card_submit
        and state is not None
        and state.status in _TERMINAL_STATUSES
        and is_new_incident(user_text, state)
    ):
        state = InterviewState(user_id=user_id)
    elif state is None:
        state = InterviewState(user_id=user_id)

    # --- Short-circuit lifecycle branches (no interview turn) ---

    if is_card_submit and state.status in _TERMINAL_STATUSES:
        # Stale card from a finished interview: don't merge its previous-incident
        # values; nudge toward a fresh report.
        async def _stale_cb(turn_context) -> None:
            await send_text(turn_context, redact(_STALE_CARD_LINE))
        await _reply_once(adapter, store, state, activity, claims, audience, _stale_cb)
        return

    if not is_card_submit and state.status == "investigated":
        # Post-investigation follow-up: answer from the stored summary + report
        # only (no MCP, no re-intake). Same catch-and-degrade shape as the
        # interviewer so a transient model blip doesn't redeliver 10x.
        async def _followup_cb(turn_context) -> None:
            try:
                answer = await run_followup(state, user_text)
            except FollowupError:
                logger.exception("follow-up failed for user %s", state.user_id)
                answer = "I hit a snag pulling that up -- please ask again in a moment."
            await send_text(turn_context, redact(answer))
        await _reply_once(adapter, store, state, activity, claims, audience, _followup_cb)
        return

    if not is_card_submit and state.status == "complete":
        # Interview finished but investigation failed. Retry ONLY when asked,
        # reusing the stored summary so the reporter skips intake.
        if is_retry_request(user_text) and state.structured_summary:
            summary = StructuredSummary.model_validate_json(state.structured_summary)

            async def _retry_cb(turn_context) -> None:
                await _run_investigation_step(turn_context, state, store, summary)
            await _reply_once(adapter, store, state, activity, claims, audience, _retry_cb)
        else:
            async def _nudge_cb(turn_context) -> None:
                await send_text(turn_context, redact(_COMPLETE_NUDGE_LINE))
            await _reply_once(adapter, store, state, activity, claims, audience, _nudge_cb)
        return

    # --- Normal interview turn (interviewing, or a fresh new-incident state) ---
    # Incorporate this activity into state, unless we're resuming an activity we
    # already started before a crash: its user turn is already persisted, so
    # re-appending would duplicate it (and corrupt the interviewer's context).
    resuming = activity.id == state.last_started_activity_id
    if not resuming:
        state.last_started_activity_id = activity.id
        if is_card_submit:
            # A submit means the card was already shown -- mark it sent so a turn
            # on freshly-created state (TTL expiry / in-memory restart lost the
            # row) processes the answers instead of re-sending the card.
            state.card_sent = True
            answers = parse_card_submit(activity.value)
            for field_name, text in answers.items():
                setattr(state.intake, field_name, IntakeField(value=text))
            rendered = ", ".join(f"{k}={v}" for k, v in answers.items()) or "(no fields filled in)"
            state.transcript.append({"role": "user", "text": f"Card answers: {rendered}"})
        else:
            state.transcript.append({"role": "user", "text": user_text})

    async def _interview_cb(turn_context) -> None:
        await _run_interview_turn(turn_context, state, store)
    await _reply_once(adapter, store, state, activity, claims, audience, _interview_cb)


async def main() -> None:
    adapter = build_adapter()
    store = build_state_store()
    credential = DefaultAzureCredential()
    async with ServiceBusClient(_NAMESPACE, credential) as client:
        # handle_envelope chains intake completion into a multi-tool Foundry
        # investigation in-process, which can outrun the SDK's default 5-min
        # peek-lock auto-renewal and let the message redeliver mid-flight. Renew
        # locks well past worst-case investigation time (value is seconds); a
        # lock genuinely lost past that still surfaces through the except below.
        async with client.get_queue_receiver(
            _QUEUE_NAME, max_auto_lock_renewal_duration=600
        ) as receiver:
            logger.info("listening on queue %s", _QUEUE_NAME)
            async for msg in receiver:
                try:
                    envelope = json.loads(str(msg))
                    await handle_envelope(adapter, store, envelope)
                    await receiver.complete_message(msg)
                except Exception:
                    logger.exception("failed to process message, abandoning for redelivery")
                    try:
                        await receiver.abandon_message(msg)
                    except Exception:
                        logger.exception("failed to abandon message %s", msg)


if __name__ == "__main__":
    asyncio.run(main())
