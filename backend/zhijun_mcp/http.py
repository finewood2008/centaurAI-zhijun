"""Shared HTTP protections, including consent routes outside the MCP transport."""
from urllib.parse import urlsplit
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response


class NoStore:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            maximum = 262144 if scope["path"] == "/external-agents/decision" else 65536
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > maximum:
                    return await Response(status_code=413, headers={"Cache-Control": "no-store"})(scope, receive, send)
                if not event.get("more_body"):
                    break
            original_receive, delivered = receive, False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()
            receive = replay
        async def protected_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]}
            await send(message)
        await self.app(scope, receive, protected_send)


def protect(app, resource):
    # Return the original Starlette app so its SDK lifespan is retained.
    app.add_middleware(NoStore)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(resource).hostname], www_redirect=False)
    return app
