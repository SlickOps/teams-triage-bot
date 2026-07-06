"""JWT validation + tenant/channel pinning for inbound Bot Framework activities.

Deliberately uses BotFrameworkAuthentication.authenticate_request() directly rather
than the full CloudAdapter turn-handling pipeline: the relay's only job is to
authenticate and enqueue, never to run bot logic or reply.
"""
import os

from botbuilder.integration.aiohttp import ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity
from botframework.connector.auth import (
    AuthenticateRequestResult,
    AuthenticationConfiguration,
)

TENANT_ID = os.environ["BOT_TENANT_ID"]
ALLOWED_CHANNEL_ID = "msteams"


class _BotAuthConfig:
    """Shape expected by ConfigurationServiceClientCredentialFactory."""

    APP_ID = os.environ["BOT_APP_ID"]
    APP_TYPE = "UserAssignedMSI"
    APP_TENANTID = TENANT_ID


_bot_framework_auth = ConfigurationBotFrameworkAuthentication(
    _BotAuthConfig(),
    auth_configuration=AuthenticationConfiguration(tenant_id=TENANT_ID),
)


class AuthError(Exception):
    """Raised on any auth/tenant/channel failure. Callers must not enqueue."""


async def authenticate(activity: Activity, auth_header: str) -> AuthenticateRequestResult:
    try:
        result = await _bot_framework_auth.authenticate_request(activity, auth_header)
    except Exception as exc:  # noqa: BLE001 - the SDK raises several distinct types here
        raise AuthError(f"JWT validation failed: {exc}") from exc

    # Tenant pinning: Bot Connector channel tokens are issued by
    # api.botframework.com and carry no tid claim -- the Teams tenant lives on
    # the activity (channelData.tenant.id). Trustworthy only because the JWT
    # above proves the activity came from the genuine Bot Connector.
    if _activity_tenant_id(activity) != TENANT_ID:
        raise AuthError(f"unexpected tenant: {_activity_tenant_id(activity)!r}")
    if activity.channel_id != ALLOWED_CHANNEL_ID:
        raise AuthError(f"unexpected channel_id: {activity.channel_id!r}")

    return result


def _activity_tenant_id(activity: Activity) -> str:
    channel_data = activity.channel_data if isinstance(activity.channel_data, dict) else {}
    tenant = channel_data.get("tenant")
    if isinstance(tenant, dict) and tenant.get("id"):
        return tenant["id"]
    return getattr(activity.conversation, "tenant_id", None)
