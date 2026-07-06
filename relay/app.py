import logging
import os

from aiohttp import web
from botbuilder.schema import Activity

from auth import AuthError, authenticate
from queue_client import enqueue_activity

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("relay")


async def messages(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        activity = Activity.deserialize(body)
    except Exception as exc:
        logger.warning("rejected malformed request body: %s", exc)
        return web.Response(status=400)
    auth_header = request.headers.get("Authorization", "")

    # X-Forwarded-For records the true client IP behind ACA's envoy proxy --
    # kept in the logs so the ingress allowlist can be built from observed
    # Bot Connector source IPs.
    forwarded_for = request.headers.get("X-Forwarded-For", "?")
    try:
        result = await authenticate(activity, auth_header)
    except AuthError as exc:
        logger.warning("rejected activity %s (from %s): %s", activity.id, forwarded_for, exc)
        return web.Response(status=401)

    # conversationUpdate, messageReaction, typing, etc. all pass auth just
    # fine -- only "message" activities carry user text worth replying to.
    if activity.type != "message":
        logger.info("dropping non-message activity %s (type=%s)", activity.id, activity.type)
        return web.Response(status=202)

    # Enqueue the raw inbound JSON, not activity.serialize(): the SDK's
    # serialize/deserialize round-trip drops entity additional_properties,
    # which is where Teams puts mention payloads.
    await enqueue_activity(body, result)
    logger.info("enqueued activity %s (from %s)", activity.id, forwarded_for)
    return web.Response(status=202)


async def health(_request: web.Request) -> web.Response:
    return web.Response(status=200, text="ok")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", health)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), port=int(os.environ.get("PORT", 3978)))
