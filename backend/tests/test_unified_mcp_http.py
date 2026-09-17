import asyncio
import json
import time
from types import SimpleNamespace

import httpx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from zhijun_mcp.models import Principal, Subject, AccessError
from zhijun_mcp.auth import VerifiedToken
from zhijun_mcp.server import create_server
from zhijun_mcp.browser import ConsentBrowser

RESOURCE = "https://box.example.com/mcp"
SUBJECT = Subject(accountId="account-a", boxId="box-a", workspaceId="a" * 64, ownershipEpoch=1)


class Verifier:
    async def verify_token(self, token):
        if token not in ("alice", "bob"):
            return None
        principal = Principal(**SUBJECT.model_dump(), agentId=token, grantId="grant-" + token,
            audience=RESOURCE, expiresAt=int(time.time()) + 900)
        return VerifiedToken(token=token, client_id=token, scopes=["zhijun:read"], resource=RESOURCE,
                             expires_at=principal.expiresAt, principal=principal)


class Gateway:
    binding = SUBJECT

    async def call(self, principal, tool, arguments):
        await asyncio.sleep(.005)
        return {"caller": principal.agentId, "tool": tool}


def server():
    return create_server(resource=RESOURCE, issuer="https://account.example.com",
                          verifier=Verifier(), gateway=Gateway())


def test_streamable_http_negotiation_discovery_auth_and_readonly_tool_list():
    with TestClient(server().streamable_http_app(), base_url="https://box.example.com") as client:
        headers = {"Accept": "application/json, text/event-stream"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "reference", "version": "1"}}}
        denied = client.post("/mcp", headers=headers, json=body)
        assert denied.status_code == 401
        assert "resource_metadata" in denied.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["resource"] == RESOURCE
        headers["Authorization"] = "Bearer alice"
        result = client.post("/mcp", headers=headers, json=body)
        assert result.json()["result"]["protocolVersion"] == "2025-11-25"
        tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
        assert len(tools) == 5 and all(t["annotations"]["readOnlyHint"] for t in tools)
        assert all("approve" not in t["name"] and "confirm" not in t["name"] for t in tools)
        assert client.post("/mcp", headers={**headers, "Origin": "https://evil.example"}, json=body).status_code == 403


def test_concurrent_http_requests_do_not_share_caller_identity():
    async def scenario():
        mcp = server()
        app = mcp.streamable_http_app()
        async with mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://box.example.com") as client:
                async def call(name):
                    result = await client.post("/mcp", headers={"Authorization": "Bearer " + name,
                        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "zhijun_get_access", "arguments": {}}})
                    payload = result.json()["result"]["structuredContent"]
                    assert payload["caller"] == name
                await asyncio.gather(*(call(name) for name in ["alice", "bob"] * 10))
    asyncio.run(scenario())


class OwnerGateway:
    binding = SUBJECT

    def __init__(self):
        self.calls = []

    async def owner(self, action, arguments):
        self.calls.append((action, arguments))
        if action == "preview":
            return {"personal": [{"id": "claim-a", "content": "PRIVATE_PERSONAL", "section": "ways", "requiresLegacyConfirmation": True}],
                    "materials": [{"id": "doc-a", "title": "PRIVATE_TITLE", "version": 1}]}
        if action == "create_grant":
            return {"id": "gr_a", "expiresAt": time.time() + 86400, "revision": 1}
        return {"state": "pending", "agentName": "WorkBuddy"}


class Account:
    def __init__(self):
        self.used, self.completed = False, []

    async def exchange(self, ticket):
        if self.used or ticket != "one-use":
            raise AccessError()
        self.used = True
        return {**SUBJECT.model_dump(), "agentId": "workbuddy", "agentName": "WorkBuddy", "consentId": "c_a"}

    async def complete(self, *args):
        self.completed.append(args)

    def resume_url(self, cid):
        return "https://account.example.com/resume?consentId=" + cid


def browser():
    gateway, account = OwnerGateway(), Account()
    consent = ConsentBrowser(gateway, account, "https://box.example.com")
    client = TestClient(Starlette(routes=consent.routes()), base_url="https://box.example.com")
    return consent, client, gateway, account


def test_consent_defaults_unselected_body_remains_on_box_and_csrf_prevents_writes():
    import re
    consent, client, gateway, account = browser()
    response = client.get("/external-agents/authorize?ticket=one-use")
    assert response.status_code == 200 and "PRIVATE_PERSONAL" in response.text
    assert " checked" not in response.text
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    csrf = re.search('name="csrf" value="([^"]+)"', response.text)[1]
    rejected = client.post("/external-agents/decision", data={"csrf": csrf, "decision": "approve"}, headers={"Origin": "https://evil.example"})
    assert rejected.status_code == 409 and not account.completed
    body = {"csrf": csrf, "decision": "approve", "sections": "ways", "materials": "doc-a",
            "legacy": "yes", "days": "30", "disclosure": "yes"}
    result = client.post("/external-agents/decision", data=body, headers={"Origin": "https://box.example.com"}, follow_redirects=False)
    assert result.status_code == 303
    grant = next(args for name, args in gateway.calls if name == "create_grant")
    assert grant["acknowledgedLegacyIds"] == ["claim-a"]
    assert len(account.completed) == 1
    assert "PRIVATE" not in json.dumps(account.completed)
    assert client.post("/external-agents/decision", data=body, headers={"Origin": "https://box.example.com"}).status_code == 409
    assert client.get("/external-agents/authorize?ticket=one-use").status_code == 403


def test_cancelling_consent_does_not_create_grant():
    import re
    _, client, gateway, account = browser()
    response = client.get("/external-agents/authorize?ticket=one-use")
    csrf = re.search('name="csrf" value="([^"]+)"', response.text)[1]
    response = client.post("/external-agents/decision", data={"csrf": csrf, "decision": "cancel"},
                           headers={"Origin": "https://box.example.com"}, follow_redirects=False)
    assert response.status_code == 303
    assert not any(name == "create_grant" for name, _ in gateway.calls)
    assert account.completed == [("c_a", None, None)]


def test_wrong_account_ticket_cannot_open_private_preview():
    consent, client, gateway, account = browser()
    async def wrong(ticket):
        return {**SUBJECT.model_dump(), "accountId": "other", "agentId": "agent", "agentName": "Agent", "consentId": "c_a"}
    account.exchange = wrong
    result = client.get("/external-agents/authorize?ticket=one-use")
    assert result.status_code == 403 and not gateway.calls
