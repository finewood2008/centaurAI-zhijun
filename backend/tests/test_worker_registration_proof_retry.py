"""Only an explicitly unexecuted registration may renew its short-lived proof."""
import io
import json
import urllib.error

import pytest

from zhijun_worker import auth
from zhijun_worker.capabilities import CapabilityError, HttpCapabilities, execution
from tests.test_zhijun_worker import workspace


def rejected(code="WORKER_PROOF_EXPIRED", status=401, marker=True):
    error = {"code": code}
    if marker is not None:
        error["rejectedBeforeDispatch"] = marker
    return urllib.error.HTTPError("http://127.0.0.1/internal/zhijun/capabilities", status,
                                  "rejected", {}, io.BytesIO(json.dumps({"ok": False, "error": error}).encode()))


def test_delayed_registration_renews_once_with_same_body_and_fresh_nonce(tmp_path, monkeypatch):
    w = workspace(tmp_path)
    port = HttpCapabilities(w, "http://127.0.0.1:1")
    clock = [100]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    requests, effects, nonces = [], [], auth.NonceStore()

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                clock[0] += 6  # server admission was delayed, not model execution
            try:
                auth.verify(request.get_header(auth.HEADER.capitalize()), w.key, w.workspace_id,
                            w.ownership_epoch, request.data, nonces,
                            expected_relative_path="/internal/zhijun/capabilities")
            except auth.ProofError as exc:
                assert str(exc) == "WORKER_PROOF_EXPIRED"
                assert not effects and not nonces._values
                raise rejected() from None
            effects.append(json.loads(request.data))
            return io.BytesIO(b'{"ok":true,"result":{"registered":true}}')

    port.opener = Opener()
    token = execution.set({"requestId": "origin-fixture", "operationId": "chat-fixture"})
    try:
        assert port.call("domain.background.register", {"jobId": "synthetic-job"}) == {"registered": True}
    finally:
        execution.reset(token)
    assert len(requests) == 2 and len(effects) == 1
    assert requests[0].data == requests[1].data
    proofs = [json.loads(auth._decode(req.headers[auth.HEADER.capitalize()].split(".")[1])) for req in requests]
    assert proofs[0]["nonce"] != proofs[1]["nonce"]
    assert proofs[0]["iat"] == 100 and proofs[1]["iat"] == 106
    assert all(proof["exp"] - proof["iat"] == 5 for proof in proofs)
    assert effects[0]["executionRequestId"] == "origin-fixture"
    assert effects[0]["operationId"] == "chat-fixture"


@pytest.mark.parametrize("code,status,marker", [
    ("WORKER_SIGNATURE_INVALID", 401, True),
    ("WORKER_PROOF_REPLAYED", 401, True),
    ("WORKSPACE_WORKER_PROOF_INVALID", 401, True),
    ("WORKER_PROOF_EXPIRED", 500, True),
    ("WORKER_PROOF_EXPIRED", 401, None),
    ("WORKER_PROOF_EXPIRED", 401, "true"),
    ("WORKER_PROOF_EXPIRED", 401, False),
    ("bad secret value!", 401, True),
])
def test_uncertain_or_other_rejection_is_never_retried(tmp_path, code, status, marker):
    port = HttpCapabilities(workspace(tmp_path), "http://127.0.0.1:1")
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise rejected(code, status, marker)
    port.opener = Opener()
    with pytest.raises(CapabilityError) as exc:
        port.call("domain.background.register", {})
    assert len(calls) == 1
    assert exc.value.code == ("CAPABILITY_REJECTED" if code == "bad secret value!" else code)


@pytest.mark.parametrize("name,path", [
    ("domain.background.finish", "/internal/zhijun/capabilities"),
    ("materials.get", "/internal/zhijun/capabilities"),
    ("domain.background.register", "/internal/zhijun/model-stream"),
])
def test_retry_is_limited_to_registration_port(tmp_path, name, path):
    port = HttpCapabilities(workspace(tmp_path), "http://127.0.0.1:1")
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise rejected()
    port.opener = Opener()
    with pytest.raises(CapabilityError, match="WORKER_PROOF_EXPIRED"):
        port._request(name, {}, path)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["expiry", "timeout", "connection", "normal"])
def test_registration_retry_bound_and_transport_failure(tmp_path, failure):
    port = HttpCapabilities(workspace(tmp_path), "http://127.0.0.1:1")
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            if failure == "expiry":
                raise rejected()
            if failure == "timeout":
                raise TimeoutError()
            if failure == "connection":
                raise ConnectionResetError()
            return io.BytesIO(b'{"ok":true,"result":{}}')
    port.opener = Opener()
    if failure == "normal":
        assert port.call("domain.background.register", {}) == {}
    else:
        with pytest.raises(CapabilityError):
            port.call("domain.background.register", {})
    assert len(calls) == (2 if failure == "expiry" else 1)
