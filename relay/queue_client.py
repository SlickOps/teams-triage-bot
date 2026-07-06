"""Enqueue validated activities to Service Bus for the brain to pick up."""
import json
import os

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient
from botframework.connector.auth import AuthenticateRequestResult

_NAMESPACE = os.environ["SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"]
_QUEUE_NAME = os.environ.get("SERVICE_BUS_QUEUE_NAME", "activities")

_credential = DefaultAzureCredential()
_client = ServiceBusClient(_NAMESPACE, _credential)
_sender = _client.get_queue_sender(_QUEUE_NAME)


async def enqueue_activity(raw_activity: dict, auth_result: AuthenticateRequestResult) -> None:
    """Envelope: the raw activity JSON (as received -- see app.py for why it must
    not be round-tripped through the SDK models) plus the claims/audience validated
    here at the relay. The brain re-validates these claims (defense in depth) since
    it has no way to re-check the original JWT signature after the queue hop.

    Reuses a persistent sender (kept alive alongside the module-level client)
    instead of attaching/detaching an AMQP link per message -- this is the
    relay's only hot path and link setup is otherwise ~100-300ms per call.
    """
    envelope = {
        "activity": raw_activity,
        "claims": auth_result.claims_identity.claims,
        "audience": auth_result.audience,
    }
    await _sender.send_messages(ServiceBusMessage(json.dumps(envelope)))
