import asyncio
import json
import logging
import os
from collections import OrderedDict

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from botbuilder.core import TurnContext
from botbuilder.schema import Activity
from botframework.connector.auth import ClaimsIdentity

from cards import build_intake_card, parse_card_submit
from intake import IntakeField, StructuredSummary
from interviewer import InterviewerError, run_turn
from investigation import InvestigationError, InvestigationReport, run_investigation
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
            f"[{ev.label}]({ev.ref})" if ev.ref else ev.label
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
                "brain) -- the summary above is still good; a human can take it from there."
            ),
        )
        return

    state.status = "investigated"
    state.investigation_report = report.model_dump_json()
    await send_text(turn_context, redact(_render_investigation_markdown(report)))
    await store.put(state)


async def handle_envelope(adapter, store, envelope: dict) -> None:
    activity = Activity.deserialize(envelope["activity"])
    claims = envelope["claims"]

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

    if activity.id in _seen_activity_ids:
        logger.info("skipping already-processed activity %s", activity.id)
        return

    user_id = _activity_user_id(activity)
    is_card_submit = isinstance(activity.value, dict) and bool(activity.value)

    state = await store.get(user_id)
    # A fresh free-text message after an interview has reached a TERMINAL state
    # starts a brand-new incident. Both "complete" (summary produced) and
    # "investigated" (Phase 3 investigation also done) are terminal -- the bug
    # this guards against: omitting "investigated" meant that once an interview
    # had been investigated, the next report reused the old transcript/intake and
    # merged the two incidents (e.g. an earlier "500 in browser" symptom bleeding
    # into a new, unrelated environment). A card submit is NOT a new incident --
    # it's answers to the card we just sent, so it never resets.
    if state is None or (state.status in _TERMINAL_STATUSES and not is_card_submit):
        state = InterviewState(user_id=user_id)

    if is_card_submit:
        # Receiving a submit means the card was already shown -- mark it sent so
        # a turn on freshly-created state (e.g. after TTL expiry or an in-memory
        # fallback restart lost the row) processes the answers instead of
        # re-sending the card and throwing away what the user just submitted.
        state.card_sent = True
        answers = parse_card_submit(activity.value)
        for field_name, text in answers.items():
            setattr(state.intake, field_name, IntakeField(value=text))
        rendered = ", ".join(f"{k}={v}" for k, v in answers.items()) or "(no fields filled in)"
        state.transcript.append({"role": "user", "text": f"Card answers: {rendered}"})
    else:
        # In channels the incoming text includes the "@Bot Name" mention markup;
        # strip it so we only see what the user actually typed.
        text = TurnContext.remove_recipient_mention(activity) or activity.text or ""
        state.transcript.append({"role": "user", "text": text.strip()})

    async def _callback(turn_context) -> None:
        await _run_interview_turn(turn_context, state, store)

    claims_identity = ClaimsIdentity(claims=claims, is_authenticated=True)
    await adapter.process_proactive(claims_identity, activity, envelope["audience"], _callback)
    # Mark seen only after a successful reply: marking earlier would make a
    # redelivery of a crashed attempt complete without ever replying.
    _mark_seen(activity.id)
    logger.info("replied to activity %s", activity.id)


async def main() -> None:
    adapter = build_adapter()
    store = build_state_store()
    credential = DefaultAzureCredential()
    async with ServiceBusClient(_NAMESPACE, credential) as client:
        async with client.get_queue_receiver(_QUEUE_NAME) as receiver:
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
