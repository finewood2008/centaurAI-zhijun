"""Metadata-only account-service adapter. HTTP redirects are never followed."""
import time
from urllib.parse import urlencode, urlsplit
import httpx
import jwt
from .models import AccessError


class AccountBroker:
    def __init__(self, issuer, resource, ssl_context, key_resolver):
        if urlsplit(issuer).scheme != "https":
            raise ValueError("ACCOUNT_HTTPS_REQUIRED")
        self.issuer, self.resource = issuer.rstrip("/"), resource
        self.ssl_context, self.key_resolver = ssl_context, key_resolver

    async def _post(self, path, payload):
        async with httpx.AsyncClient(verify=self.ssl_context, trust_env=False, timeout=15,
                                     follow_redirects=False) as client:
            async with client.stream("POST", self.issuer + path, json=payload) as response:
                if response.status_code != 200:
                    raise AccessError("ACCOUNT_UNAVAILABLE", 503)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 16384:
                        raise AccessError("ACCOUNT_RESPONSE_INVALID", 502)
        import json
        return json.loads(raw)

    async def exchange(self, ticket):
        result = await self._post("/v1/zhijun/consents/exchange", {"ticket": ticket, "resource": self.resource})
        token = result["assertion"]
        header = jwt.get_unverified_header(token)
        if header.get("typ") != "zhijun-consent+jwt" or header.get("alg") not in ("RS256", "ES256"):
            raise AccessError()
        claims = jwt.decode(token, self.key_resolver(token), algorithms=["RS256", "ES256"],
            issuer=self.issuer, audience=self.resource,
            options={"require": ["iss", "aud", "sub", "exp", "iat", "jti"]})
        if (claims["aud"] != self.resource or claims["exp"] - claims["iat"] > 300
                or claims["exp"] <= time.time() or claims["iat"] > time.time()):
            raise AccessError()
        return {"accountId": claims["sub"], "boxId": claims["box_id"],
                "workspaceId": claims["workspace_id"], "ownershipEpoch": claims["ownership_epoch"],
                "agentId": claims["client_id"], "agentName": claims["client_name"],
                "consentId": claims["consent_id"], "requestId": claims.get("request_id"),
                "state": claims.get("state")}

    async def complete(self, consent_id, grant_id, expires):
        return await self._post("/v1/zhijun/consents/complete", {
            "consentId": consent_id, "resource": self.resource, "grantId": grant_id, "expiresAt": expires})

    def login_url(self, callback, state, request_id):
        return self.issuer + "/zhijun/login?" + urlencode({"resource": self.resource,
            "callback": callback, "state": state, "requestId": request_id})

    def resume_url(self, consent_id):
        return self.issuer + "/zhijun/resume?" + urlencode({"consentId": consent_id})
