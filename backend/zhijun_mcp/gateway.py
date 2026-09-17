"""Restricted box-local IPC to the DE Zhijun Gateway; never forwards OAuth tokens."""
import json
from pathlib import Path
import httpx
from zhijun_worker.auth import sign
from .models import AccessError


class Gateway:
    def __init__(self, socket: Path, key: bytes, binding):
        if not socket.is_absolute() or socket.is_symlink() or len(key) != 32:
            raise ValueError("MCP_GATEWAY_CONFIG_INVALID")
        self.socket, self.key, self.binding = socket, key, binding

    async def _post(self, operation, envelope):
        path = "/internal/zhijun/external-agent-" + operation
        body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        proof = sign(self.key, self.binding.workspaceId, self.binding.ownershipEpoch, "POST", path, body)
        # Key is a separate, Gateway-registered MCP service key, NOT a worker key.
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(self.socket)),
                                     trust_env=False, timeout=60) as client:
            async with client.stream("POST", "http://localhost" + path, content=body,
                headers={"Content-Type": "application/json", "X-Zhijun-MCP-Proof": proof}) as response:
                if response.status_code != 200:
                    raise AccessError("GATEWAY_ACCESS_REJECTED", response.status_code)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 524288:
                        raise AccessError("GATEWAY_RESPONSE_TOO_LARGE", 502)
        try:
            return json.loads(raw)
        except ValueError:
            raise AccessError("GATEWAY_RESPONSE_INVALID", 502) from None

    async def call(self, principal, tool, arguments):
        if any(getattr(principal, key) != value for key, value in self.binding.model_dump().items()):
            raise AccessError()
        return await self._post("tools", {"principal": principal.model_dump(), "tool": tool, "arguments": arguments})

    async def owner(self, action, arguments):
        if action not in {"status", "preview", "create_grant", "request_preview", "decide", "revoke_grant"}:
            raise AccessError()
        return await self._post("owner", {"subject": self.binding.model_dump(), "action": action, "arguments": arguments})
