import asyncio
import json
import logging
import os
from collections import OrderedDict

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from botbuilder.schema import Activity
from botframework.connector.auth import ClaimsIdentity

from reply import build_adapter, echo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("brain")

_NAMESPACE = os.environ["SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"]
_QUEUE_NAME = os.environ.get("SERVICE_BUS_QUEUE_NAME", "activities")
_TENANT_ID = os.environ["BOT_TENANT_ID"]
_APP_ID = os.environ["BOT_APP_ID"]


def _activity_tenant_id(activity: Activity) -> str:
    channel_data = activity.channel_data if isinstance(activity.channel_data, dict) else {}
    tenant = channel_data.get("tenant")
    if isinstance(tenant, dict) and tenant.get("id"):
        return tenant["id"]
    return getattr(activity.conversation, "tenant_id", None)

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


async def handle_envelope(adapter, envelope: dict) -> None:
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

    claims_identity = ClaimsIdentity(claims=claims, is_authenticated=True)
    await adapter.process_proactive(claims_identity, activity, envelope["audience"], echo)
    # Mark seen only after a successful reply: marking earlier would make a
    # redelivery of a crashed attempt complete without ever replying.
    _mark_seen(activity.id)
    logger.info("replied to activity %s", activity.id)


async def main() -> None:
    adapter = build_adapter()
    credential = DefaultAzureCredential()
    async with ServiceBusClient(_NAMESPACE, credential) as client:
        async with client.get_queue_receiver(_QUEUE_NAME) as receiver:
            logger.info("listening on queue %s", _QUEUE_NAME)
            async for msg in receiver:
                try:
                    envelope = json.loads(str(msg))
                    await handle_envelope(adapter, envelope)
                    await receiver.complete_message(msg)
                except Exception:
                    logger.exception("failed to process message, abandoning for redelivery")
                    try:
                        await receiver.abandon_message(msg)
                    except Exception:
                        logger.exception("failed to abandon message %s", msg)


if __name__ == "__main__":
    asyncio.run(main())
