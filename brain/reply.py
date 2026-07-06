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


async def echo(turn_context) -> None:
    """The only bot logic in Phase 1: reply with what was said, from code, not from
    any model output (moot today, but keeps the "actions from code" shape from the
    start)."""
    from botbuilder.core import TurnContext

    # In channels the incoming text includes the "@Bot Name" mention markup;
    # strip it so the echo only reflects what the user actually typed.
    text = TurnContext.remove_recipient_mention(turn_context.activity) or ""
    await turn_context.send_activity(f"you said: {text.strip()}")
