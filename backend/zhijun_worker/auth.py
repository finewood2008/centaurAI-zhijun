"""Internal, request-bound authentication; shared with the DE dispatcher."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

HEADER = "X-Zhijun-Worker-Proof"
DOMAIN = b"ZHIJUN-WORKER-V1\n"
MAX_BODY = 1024 * 1024
FIELDS = {"v", "workspaceId", "ownershipEpoch", "method", "relativePath", "bodySha256", "iat", "exp", "nonce"}


class ProofError(ValueError):
    pass


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ProofError("WORKER_JSON_DUPLICATE")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ProofError("WORKER_JSON_INVALID")))
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ProofError("WORKER_JSON_INVALID") from exc


def _encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ProofError("WORKER_PROOF_INVALID")
    try:
        result = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise ProofError("WORKER_PROOF_INVALID") from exc
    if _encode(result) != value:
        raise ProofError("WORKER_PROOF_INVALID")
    return result


class NonceStore:
    """A key is replaced on worker restart; live nonces are never evicted."""
    def __init__(self, capacity=10000):
        self.capacity = capacity
        self._values = {}
        self._lock = threading.Lock()

    def consume(self, nonce, expiry, now):
        with self._lock:
            self._values = {key: end for key, end in self._values.items() if end > now}
            if nonce in self._values:
                raise ProofError("WORKER_PROOF_REPLAYED")
            if len(self._values) >= self.capacity:
                raise ProofError("WORKER_NONCE_CAPACITY")
            self._values[nonce] = expiry


def sign(key: bytes, workspace_id: str, ownership_epoch: int, method: str,
         relative_path: str, body: bytes, *, now=None, ttl=5, nonce=None):
    now = int(time.time()) if now is None else now
    payload = {"v": 1, "workspaceId": workspace_id, "ownershipEpoch": ownership_epoch,
               "method": method, "relativePath": relative_path,
               "bodySha256": hashlib.sha256(body).hexdigest(), "iat": now, "exp": now + ttl,
               "nonce": nonce or _encode(secrets.token_bytes(24))}
    if len(key) != 32 or len(body) > MAX_BODY:
        raise ProofError("WORKER_INPUT_INVALID")
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "v1." + _encode(raw) + "." + _encode(hmac.digest(key, DOMAIN + raw, "sha256"))


def verify(header: str, key: bytes, workspace_id: str, ownership_epoch: int,
           body: bytes, nonces: NonceStore, *, expected_method="POST",
           expected_relative_path="/v1/dispatch", now=None):
    now = int(time.time()) if now is None else now
    if not isinstance(header, str) or len(header) > 4096 or len(key) != 32 or len(body) > MAX_BODY:
        raise ProofError("WORKER_PROOF_INVALID")
    try:
        version, encoded, signature = header.split(".")
        raw, mac = _decode(encoded), _decode(signature)
    except (ValueError, TypeError) as exc:
        raise ProofError("WORKER_PROOF_INVALID") from exc
    if version != "v1" or not hmac.compare_digest(mac, hmac.digest(key, DOMAIN + raw, "sha256")):
        raise ProofError("WORKER_SIGNATURE_INVALID")
    payload = strict_json(raw)
    if type(payload) is not dict or set(payload) != FIELDS:
        raise ProofError("WORKER_FIELDS_INVALID")
    for name in ("v", "ownershipEpoch", "iat", "exp"):
        if type(payload[name]) is not int:
            raise ProofError("WORKER_FIELDS_INVALID")
    if payload["v"] != 1 or not re.fullmatch(r"[a-f0-9]{64}", str(payload["workspaceId"])):
        raise ProofError("WORKER_FIELDS_INVALID")
    if payload["workspaceId"] != workspace_id or payload["ownershipEpoch"] != ownership_epoch:
        raise ProofError("WORKER_SUBJECT_MISMATCH")
    if not 0 < payload["ownershipEpoch"] <= 9007199254740991:
        raise ProofError("WORKER_FIELDS_INVALID")
    if not 0 < payload["exp"] - payload["iat"] <= 5 or payload["iat"] > now + 2 or payload["exp"] <= now:
        raise ProofError("WORKER_PROOF_EXPIRED")
    if payload["method"] != expected_method or payload["relativePath"] != expected_relative_path:
        raise ProofError("WORKER_TARGET_MISMATCH")
    if payload["bodySha256"] != hashlib.sha256(body).hexdigest():
        raise ProofError("WORKER_BODY_MISMATCH")
    if not isinstance(payload["nonce"], str) or len(_decode(payload["nonce"])) != 24:
        raise ProofError("WORKER_NONCE_INVALID")
    nonces.consume(payload["nonce"], payload["exp"], now)
    return payload
