"""Explicit synchronous domain ports; the DE remains the capability owner."""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from contextvars import ContextVar
from typing import Protocol, Iterator
from urllib.parse import urlsplit

from .auth import HEADER, sign

execution = ContextVar("zhijun_worker_execution", default=None)


class CapabilityError(RuntimeError):
    def __init__(self, code="CAPABILITY_UNAVAILABLE", status=503):
        super().__init__(code)
        self.code, self.status = code, status


class Capabilities(Protocol):
    def call(self, name: str, payload: dict): ...
    def stream(self, name: str, payload: dict) -> Iterator[dict]: ...


class UnconfiguredCapabilities:
    def call(self, name, payload):
        raise CapabilityError()

    def stream(self, name, payload):
        raise CapabilityError()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CapabilityError("CAPABILITY_REDIRECT_DENIED")


class HttpCapabilities:
    def __init__(self, workspace, url):
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            raise ValueError("CAPABILITY_URL_INVALID") from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("CAPABILITY_URL_INVALID")
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"} or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("CAPABILITY_URL_INVALID")
        if parts.path not in {"", "/", "/internal/zhijun/capabilities"}:
            raise ValueError("CAPABILITY_URL_INVALID")
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.workspace = workspace
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, name, payload, path):
        action = execution.get() or {}
        body = json.dumps({"name": name, "payload": payload,
                           "executionRequestId": action.get("requestId"),
                           "operationId": action.get("operationId")}, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        for attempt in range(2):
            proof = sign(self.workspace.key, self.workspace.workspace_id, self.workspace.ownership_epoch, "POST", path, body)
            request = urllib.request.Request(self.origin + path, data=body, method="POST",
                                             headers={HEADER: proof, "Content-Type": "application/json"})
            try:
                return self.opener.open(request, timeout=180)
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    raw = exc.read(8193)
                finally:
                    exc.close()
                rejected_before_dispatch = False
                try:
                    error = json.loads(raw).get("error", {})
                    code = error.get("code", "CAPABILITY_REJECTED")
                    if not isinstance(code, str) or not code.isascii() or not code.replace("_", "").isalnum() or len(code) > 80:
                        code = "CAPABILITY_REJECTED"
                    rejected_before_dispatch = error.get("rejectedBeforeDispatch") is True
                except (ValueError, AttributeError):
                    code = "CAPABILITY_REJECTED"
                # Renew only a registration that DE explicitly rejected before
                # capability dispatch. Never replay uncertain writes, old-DE
                # generic 401s, network failures, or consumed/replayed proofs.
                if (attempt == 0 and name == "domain.background.register"
                        and path == "/internal/zhijun/capabilities" and status == 401
                        and code == "WORKER_PROOF_EXPIRED" and rejected_before_dispatch):
                    continue
                raise CapabilityError(code, status) from None
            except OSError:
                raise CapabilityError() from None

    def call(self, name, payload):
        with self._request(name, payload, "/internal/zhijun/capabilities") as response:
            maximum = 9 * 1024 * 1024 if name == "materials.read_ref" else 1024 * 1024
            raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise CapabilityError("CAPABILITY_RESPONSE_TOO_LARGE", 502)
            try:
                envelope = json.loads(raw)
            except (ValueError, UnicodeError):
                raise CapabilityError("CAPABILITY_CONTRACT_INVALID", 502) from None
            if type(envelope) is not dict or envelope.get("ok") is not True or "result" not in envelope:
                raise CapabilityError("CAPABILITY_CONTRACT_INVALID", 502)
            return envelope["result"]

    def stream(self, name, payload):
        with self._request(name, payload, "/internal/zhijun/model-stream") as response:
            while True:
                raw = response.readline(64 * 1024 + 1)
                if not raw:
                    break
                if len(raw) > 64 * 1024:
                    raise CapabilityError("CAPABILITY_RESPONSE_TOO_LARGE", 502)
                if raw.strip():
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise CapabilityError("CAPABILITY_CONTRACT_INVALID", 502) from None
                    yield event


_runtime = None
_process_workspace = None


def bind(workspace, capabilities):
    global _runtime, _process_workspace
    if _process_workspace is not None and _process_workspace != workspace.workspace_id:
        raise RuntimeError("WORKER_PROCESS_SUBJECT_IMMUTABLE")
    if _runtime is not None:
        raise RuntimeError("WORKER_ALREADY_RUNNING")
    _process_workspace = workspace.workspace_id
    _runtime = (workspace, capabilities)


def unbind():
    global _runtime
    _runtime = None


def current():
    return _runtime


def require():
    if _runtime is None:
        raise CapabilityError("WORKER_NOT_RUNNING")
    return _runtime[1]
