"""Bot Framework CloudAdapter, wired for replying to activities pulled off the queue.

process_proactive() is the SDK's intended mechanism for sending a message from a
stored/continued activity rather than a live inbound HTTP request -- exactly our
queue-decoupled shape, so we reuse it rather than hand-rolling ConnectorClient calls.
"""
import os

from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botframework.connector.auth import AuthenticationConfiguration

TENANT_ID = os.environ["BOT_TENANT_ID"]


class _BotAuthConfig:
    APP_ID = os.environ["BOT_APP_ID"]
    APP_TYPE = "UserAssignedMSI"
    APP_TENANTID = TENANT_ID


def build_adapter() -> CloudAdapter:
    bot_framework_auth = ConfigurationBotFrameworkAuthentication(
        _BotAuthConfig(),
        auth_configuration=AuthenticationConfiguration(tenant_id=TENANT_ID),
    )
    return CloudAdapter(bot_framework_auth)


async def send_text(turn_context, text: str) -> None:
    """Send a plain text reply. Trivial wrapper, but keeps app.py's interview
    handler from needing to know the SDK call shape."""
    await turn_context.send_activity(text)


async def send_card(turn_context, card: dict) -> None:
    """Send an Adaptive Card as an attachment."""
    from botbuilder.core import CardFactory, MessageFactory

    await turn_context.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(card)))
