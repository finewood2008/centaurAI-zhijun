"""OAuth resource-server verification; identities come only from the trusted issuer."""
import time
import jwt
from mcp.server.auth.provider import AccessToken
from .models import Principal


class VerifiedToken(AccessToken):
    principal: Principal


class TokenVerifier:
    def __init__(self, issuer, resource, key_resolver, *, clock=time.time):
        self.issuer, self.resource, self.key_resolver, self.clock = issuer, resource, key_resolver, clock

    async def verify_token(self, token):
        try:
            header = jwt.get_unverified_header(token)
            if header.get("typ") != "at+jwt" or header.get("alg") not in ("RS256", "ES256"):
                return None
            claims = jwt.decode(token, self.key_resolver(token), algorithms=["RS256", "ES256"],
                audience=self.resource, issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "client_id", "jti"]})
            now = self.clock()
            if (type(claims["exp"]) is not int or type(claims["iat"]) is not int
                    or claims["exp"] - claims["iat"] > 900 or claims["iat"] > now
                    or claims["exp"] <= now or claims["aud"] != self.resource
                    or "zhijun:read" not in claims.get("scope", "").split()):
                return None
            principal = Principal(accountId=claims["sub"], boxId=claims["box_id"],
                workspaceId=claims["workspace_id"], ownershipEpoch=claims["ownership_epoch"],
                agentId=claims["client_id"], grantId=claims["grant_id"],
                audience=self.resource, expiresAt=claims["exp"])
            return VerifiedToken(token=token, client_id=principal.agentId, scopes=["zhijun:read"],
                expires_at=principal.expiresAt, resource=self.resource, principal=principal)
        except Exception:
            return None
