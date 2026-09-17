"""Strict server-side client for the Data Agent RAG V2 application API.

Only ``/v1/agent/apps/*`` routes are reachable through this client.  App
credentials are loaded once from a root-managed file and are never placed in
URLs, response errors, or ambient proxy traffic.
"""
from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit


MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 9_007_199_254_740_991  # Largest exact JSON integer in the renderer.

_APP_ID = re.compile(r"agc_[a-f0-9]{12,32}")
_APP_SECRET = re.compile(r"[A-Za-z0-9_-]{43,128}")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_.-]{8,128}")
_CONFIRM_TOKEN = re.compile(r"scf_[A-Za-z0-9_-]{32,252}")
_INTERACTION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_JOB_ID = re.compile(r"aij_[a-f0-9]{32}")
_SUPPORTED_UPLOAD_SUFFIXES = {
    ".pdf",
    ".docx",
    ".txt",
    ".md",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".mp3",
    ".wav",
    ".m4a",
}
_JOB_STATES = {"queued", "processing", "ready", "failed", "canceled"}
_JOB_STAGES = {"receiving", "parsing", "snapshot", "chunking", "embedding", "indexing", "completed"}
_INDEX_STATES = {"none", "building", "indexed", "failed", "stale"}
_FAILURE_STAGES = {"registration", "parsing", "snapshot", "chunking", "embedding", "indexing"}
_JOB_ACTIONS = {"poll", "retry", "cancel", "contact_admin"}
_RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
_DELIVERY_MODES = {"confirm", "always_mask", "block"}
_RULE_INPUT_REQUIRED = {
    "name", "description", "examples", "enabled", "deliveryMode",
    "allowOriginalAfterConfirm",
}
_RULE_INPUT_OPTIONAL = {"counterExamples", "acknowledgeSimilarRuleId"}


class DataAgentRagV2Error(RuntimeError):
    """Stable error exposed to the Zhijun integration layer."""

    def __init__(
        self,
        code: str,
        *,
        status: Optional[int] = None,
        message: Optional[str] = None,
        retryable: bool = False,
        retry_after: Optional[int] = None,
        trace_id: Optional[str] = None,
        similar_rule_id: Optional[str] = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message or code
        self.retryable = retryable
        self.retry_after = retry_after
        self.trace_id = trace_id
        self.similar_rule_id = similar_rule_id


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise DataAgentRagV2Error("REDIRECT_REFUSED")


def _upload_accepted(value: Any) -> Dict[str, Any]:
    required = {"jobId", "materialId", "materialVersion", "statusUrl"}
    if (not isinstance(value, dict) or set(value) != required
            or not isinstance(value.get("jobId"), str) or _JOB_ID.fullmatch(value["jobId"]) is None
            or not isinstance(value.get("materialId"), str) or not 1 <= len(value["materialId"]) <= 128
            or type(value.get("materialVersion")) is not int
            or not 1 <= value["materialVersion"] <= 2_147_483_647
            or not isinstance(value.get("statusUrl"), str)
            or value["statusUrl"] != "/v1/agent/apps/material-jobs/" + value["jobId"]):
        raise DataAgentRagV2Error("INVALID_UPLOAD_ACCEPTED_RESPONSE", status=502)
    return value


def _job_status(value: Any) -> Dict[str, Any]:
    required = {"jobId", "materialId", "materialVersion", "state", "stage", "indexState", "progress"}
    optional = {"allowedActions", "latestMaterialVersion", "updateInProgress", "errorCode", "failureStage", "retryable"}
    if not isinstance(value, dict) or not required <= set(value) or not set(value) <= required | optional:
        raise DataAgentRagV2Error("INVALID_JOB_STATUS_RESPONSE", status=502)
    progress = value.get("progress")
    progress_keys = {"parsedCharacters", "chunkCount", "indexedChunks"}
    if (not isinstance(progress, dict) or not set(progress) <= progress_keys
            or any(type(progress.get(key, 0)) is not int or progress.get(key, 0) < 0 for key in progress_keys)
            or progress.get("indexedChunks", 0) > progress.get("chunkCount", 0)):
        raise DataAgentRagV2Error("INVALID_JOB_STATUS_RESPONSE", status=502)
    actions = value.get("allowedActions", [])
    latest = value.get("latestMaterialVersion")
    error_code = value.get("errorCode")
    failure_stage = value.get("failureStage")
    retryable = value.get("retryable")
    state = value.get("state")
    if (not isinstance(value.get("jobId"), str) or _JOB_ID.fullmatch(value["jobId"]) is None
            or not isinstance(value.get("materialId"), str) or not 1 <= len(value["materialId"]) <= 128
            or type(value.get("materialVersion")) is not int
            or not 1 <= value["materialVersion"] <= 2_147_483_647
            or state not in _JOB_STATES or value.get("stage") not in _JOB_STAGES
            or value.get("indexState") not in _INDEX_STATES
            or not isinstance(actions, list) or len(actions) > 4 or len(set(actions)) != len(actions)
            or any(action not in _JOB_ACTIONS for action in actions)
            or type(value.get("updateInProgress", False)) is not bool
            or (latest is not None and (type(latest) is not int
                                        or not value["materialVersion"] <= latest <= 2_147_483_647))
            or (error_code is not None and (not isinstance(error_code, str)
                                            or re.fullmatch(r"[A-Z0-9_]{1,80}", error_code) is None))
            or (failure_stage is not None and failure_stage not in _FAILURE_STAGES)
            or (retryable is not None and type(retryable) is not bool)):
        raise DataAgentRagV2Error("INVALID_JOB_STATUS_RESPONSE", status=502)
    if state == "ready" and (
            value["stage"] != "completed" or value["indexState"] != "indexed"
            or progress.get("indexedChunks", 0) != progress.get("chunkCount", 0)
            or "poll" in actions or error_code is not None):
        raise DataAgentRagV2Error("INVALID_JOB_STATUS_RESPONSE", status=502)
    if state == "processing" and "poll" not in actions:
        raise DataAgentRagV2Error("INVALID_JOB_STATUS_RESPONSE", status=502)
    if state == "failed":
        if not error_code or not failure_stage or retryable is None:
            raise DataAgentRagV2Error("INVALID_JOB_STATUS_RESPONSE", status=502)
    elif any(item is not None for item in (error_code, failure_stage, retryable)):
        raise DataAgentRagV2Error("INVALID_JOB_STATUS_RESPONSE", status=502)
    return value


def _strict_opener() -> Any:
    # Do not inherit HTTP(S)_PROXY for requests bearing X-App-Secret.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _unique_object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DataAgentRagV2Error("INVALID_JSON_RESPONSE", status=502)
        result[key] = value
    return result


def _invalid_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_envelope(raw: bytes) -> Dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DataAgentRagV2Error("RESPONSE_TOO_LARGE", status=502)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_json_constant)
    except DataAgentRagV2Error:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DataAgentRagV2Error("INVALID_JSON_RESPONSE", status=502) from None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("traceId"), str)
        or not value["traceId"]
        or (("data" in value) == ("error" in value))
    ):
        raise DataAgentRagV2Error("INVALID_RESPONSE_ENVELOPE", status=502)
    if "data" in value and not isinstance(value["data"], dict):
        raise DataAgentRagV2Error("INVALID_RESPONSE_ENVELOPE", status=502)
    if "error" in value and not isinstance(value["error"], dict):
        raise DataAgentRagV2Error("INVALID_RESPONSE_ENVELOPE", status=502)
    return value


def _read_credentials(filename: str) -> Tuple[Path, Dict[str, str]]:
    path = Path(filename)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise DataAgentRagV2Error("ABSOLUTE_REGULAR_CREDENTIAL_FILE_REQUIRED")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or not 1 <= info.st_size <= 4096):
            raise DataAgentRagV2Error("INVALID_CREDENTIAL_FILE")
        raw = bytearray()
        while len(raw) <= 4096:
            chunk = os.read(fd, min(4097 - len(raw), 4096))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) != info.st_size:
            raise DataAgentRagV2Error("CREDENTIAL_FILE_CHANGED_DURING_READ")
    except DataAgentRagV2Error:
        raise
    except OSError:
        raise DataAgentRagV2Error("CREDENTIAL_FILE_UNAVAILABLE") from None
    finally:
        if fd is not None:
            os.close(fd)
    try:
        value = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_json_constant)
    except DataAgentRagV2Error:
        raise DataAgentRagV2Error("INVALID_CREDENTIAL_FILE") from None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DataAgentRagV2Error("INVALID_CREDENTIAL_FILE") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"appId", "appSecret"}
        or not isinstance(value["appId"], str)
        or _APP_ID.fullmatch(value["appId"]) is None
        or not isinstance(value["appSecret"], str)
        or _APP_SECRET.fullmatch(value["appSecret"]) is None
    ):
        raise DataAgentRagV2Error("INVALID_CREDENTIAL_FILE")
    return path, value


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise DataAgentRagV2Error("INVALID_BASE_URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise DataAgentRagV2Error("INVALID_BASE_URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise DataAgentRagV2Error("INVALID_BASE_URL")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.casefold() == "localhost"
        if not loopback:
            raise DataAgentRagV2Error("HTTPS_REQUIRED_FOR_REMOTE_ENDPOINT")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _origin_from_capability_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise DataAgentRagV2Error("INVALID_BASE_URL") from None
    if not parsed.scheme or not parsed.netloc or parsed.username or parsed.password:
        raise DataAgentRagV2Error("INVALID_BASE_URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _bounded_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise DataAgentRagV2Error("INVALID_TIMEOUT") from None
    if not 1 <= timeout <= 600:
        raise DataAgentRagV2Error("INVALID_TIMEOUT")
    return timeout


def _idempotency_key(value: str) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise DataAgentRagV2Error("INVALID_IDEMPOTENCY_KEY")
    return value


def _identifier(value: str, code: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise DataAgentRagV2Error(code)
    return value


def _confirm_token(value: str) -> str:
    if not isinstance(value, str) or _CONFIRM_TOKEN.fullmatch(value) is None:
        raise DataAgentRagV2Error("INVALID_CONFIRM_TOKEN")
    return value


def _json_body(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise DataAgentRagV2Error("INVALID_REQUEST_BODY") from None


def _multipart_bytes(
    filename: str, file_bytes: bytes, fields: Mapping[str, str], *,
    max_file_bytes: int = MAX_FILE_BYTES,
    supported_suffixes: Optional[Iterable[str]] = None,
) -> Tuple[bytes, str]:
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or Path(filename).name != filename
        or "\r" in filename
        or "\n" in filename
    ):
        raise DataAgentRagV2Error("UPLOAD_FILE_NAME_INVALID")
    try:
        content = bytes(file_bytes)
    except (TypeError, ValueError):
        raise DataAgentRagV2Error("UPLOAD_FILE_TYPE_OR_SIZE_INVALID") from None
    suffix = Path(filename).suffix.casefold()
    allowed_suffixes = set(supported_suffixes or _SUPPORTED_UPLOAD_SUFFIXES)
    if not 1 <= len(content) <= max_file_bytes or suffix not in allowed_suffixes:
        raise DataAgentRagV2Error("UPLOAD_FILE_TYPE_OR_SIZE_INVALID")
    for name, value in fields.items():
        if name not in {"title", "externalReference"}:
            raise DataAgentRagV2Error("INVALID_MULTIPART_FIELD")
        if (
            not isinstance(value, str)
            or len(value) > 1000
            or "\r" in value
            or "\n" in value
        ):
            raise DataAgentRagV2Error("INVALID_MULTIPART_FIELD")

    boundary = "mindos-e5-" + secrets.token_hex(24)
    body = bytearray()
    for name, value in fields.items():
        body.extend(
            (
                '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, name, value)
            ).encode("utf-8")
        )
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    fallback = "upload" + suffix
    encoded_name = quote(filename, safe="")
    body.extend(
        (
            '--%s\r\nContent-Disposition: form-data; name="file"; '
            'filename="%s"; filename*=UTF-8\'\'%s\r\nContent-Type: %s\r\n\r\n'
            % (boundary, fallback, encoded_name, media_type)
        ).encode("ascii")
    )
    body.extend(content)
    body.extend(("\r\n--%s--\r\n" % boundary).encode("ascii"))
    return bytes(body), "multipart/form-data; boundary=" + boundary


def _retry_after(headers: Any) -> Optional[int]:
    try:
        raw = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        return None
    digits = raw.lstrip("0") or "0"
    # Preserve long waits; an unrepresentable delay still must not become an
    # absent hint followed by an early retry. The renderer pauses huge timers.
    if len(digits) > 16:
        return MAX_RETRY_AFTER_SECONDS
    return min(int(digits), MAX_RETRY_AFTER_SECONDS)


def sensitive_rule_status(value: Any) -> Dict[str, Any]:
    """Validate both DE contracts and preserve the existing desktop status shape.

    Newer DE versions add historicalScanRequired. It is a public boolean hint,
    not permission to start a scan. Older desktop clients require exactly the
    original two fields, so validate the hint without forwarding it or changing
    the meaning of applying. Internal scan diagnostics remain forbidden.
    """
    required = {"state", "applying"}
    if (not isinstance(value, dict)
            or not required <= set(value) <= required | {"historicalScanRequired"}
            or not isinstance(value.get("state"), str)
            or value["state"] not in {"active", "applying"}
            or type(value.get("applying")) is not bool
            or value["applying"] != (value["state"] == "applying")
            or ("historicalScanRequired" in value
                and (type(value["historicalScanRequired"]) is not bool
                     or (value["historicalScanRequired"] and not value["applying"])))):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_STATUS_RESPONSE", status=502)
    return {"state": value["state"], "applying": value["applying"]}


def _header(headers: Any, name: str) -> Optional[str]:
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _rule_id(value: Any, code: str = "INVALID_SENSITIVE_RULE_ID") -> str:
    if not isinstance(value, str) or _RULE_ID.fullmatch(value) is None:
        raise DataAgentRagV2Error(code)
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_REVISION")
    return value


def _clean_rule_text(value: Any, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_INPUT")
    result = value.strip()
    if not minimum <= len(result) <= maximum or any(ord(char) < 32 for char in result):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_INPUT")
    return result


def _clean_rule_examples(value: Any, *, required: bool) -> list[str]:
    if not isinstance(value, list) or not (1 if required else 0) <= len(value) <= 10:
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_INPUT")
    result: list[str] = []
    for item in value:
        cleaned = _clean_rule_text(item, 1, 200)
        if cleaned not in result:
            result.append(cleaned)
    if required and not result:
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_INPUT")
    return result


def _sensitive_rule_input(value: Any) -> Dict[str, Any]:
    if (not isinstance(value, Mapping)
            or not _RULE_INPUT_REQUIRED <= set(value)
            or not set(value) <= _RULE_INPUT_REQUIRED | _RULE_INPUT_OPTIONAL):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_INPUT")
    enabled = value.get("enabled")
    mode = value.get("deliveryMode")
    allow_original = value.get("allowOriginalAfterConfirm")
    if (type(enabled) is not bool or mode not in _DELIVERY_MODES
            or type(allow_original) is not bool
            or (allow_original and mode != "confirm")):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_INPUT")
    acknowledged = value.get("acknowledgeSimilarRuleId")
    if acknowledged is not None:
        acknowledged = _rule_id(acknowledged, "INVALID_SENSITIVE_RULE_INPUT")
    return {
        "name": _clean_rule_text(value.get("name"), 2, 50),
        "description": _clean_rule_text(value.get("description"), 10, 500),
        "examples": _clean_rule_examples(value.get("examples"), required=True),
        "counterExamples": _clean_rule_examples(value.get("counterExamples", []), required=False),
        "enabled": enabled,
        "deliveryMode": mode,
        "allowOriginalAfterConfirm": allow_original,
        "acknowledgeSimilarRuleId": acknowledged,
    }


def _masking(value: Any) -> Dict[str, Any]:
    required = {"strategy", "prefixCharacters", "suffixCharacters", "replacement"}
    if (not isinstance(value, dict) or set(value) != required
            or value.get("strategy") not in {"fixed", "keep_edges", "full"}
            or type(value.get("prefixCharacters")) is not int
            or not 0 <= value["prefixCharacters"] <= 200
            or type(value.get("suffixCharacters")) is not int
            or not 0 <= value["suffixCharacters"] <= 200
            or not isinstance(value.get("replacement"), str)
            or not 1 <= len(value["replacement"]) <= 32
            or any(ord(char) < 32 for char in value["replacement"])):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_RESPONSE", status=502)
    return {key: value[key] for key in required}


def _sensitive_rule(value: Any) -> Dict[str, Any]:
    required = {
        "ruleId", "source", "immutable", "revision", "name", "description",
        "examples", "counterExamples", "enabled", "deliveryMode",
        "allowOriginalAfterConfirm", "masking",
    }
    if not isinstance(value, dict) or not required <= set(value):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_RESPONSE", status=502)
    try:
        rule_id = _rule_id(value["ruleId"], "INVALID_SENSITIVE_RULE_RESPONSE")
        revision = _revision(value["revision"])
        name = _clean_rule_text(value["name"], 2, 50)
        description = _clean_rule_text(value["description"], 10, 500)
        examples = _clean_rule_examples(value["examples"], required=False)
        counter_examples = _clean_rule_examples(value["counterExamples"], required=False)
    except DataAgentRagV2Error:
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_RESPONSE", status=502) from None
    source = value.get("source")
    immutable = value.get("immutable")
    enabled = value.get("enabled")
    mode = value.get("deliveryMode")
    allow_original = value.get("allowOriginalAfterConfirm")
    if (source not in {"built_in", "custom"} or type(immutable) is not bool
            or immutable != (source == "built_in") or type(enabled) is not bool
            or mode not in _DELIVERY_MODES or type(allow_original) is not bool
            or (allow_original and mode != "confirm")
            or value["name"] != name or value["description"] != description
            or value["examples"] != examples or value["counterExamples"] != counter_examples
            or (source == "custom" and not examples)):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_RESPONSE", status=502)
    return {
        "ruleId": rule_id, "source": source, "immutable": immutable,
        "revision": revision, "name": name, "description": description,
        "examples": examples, "counterExamples": counter_examples,
        "enabled": enabled, "deliveryMode": mode,
        "allowOriginalAfterConfirm": allow_original,
        "masking": _masking(value["masking"]),
    }


def _sensitive_catalogue(value: Any) -> Dict[str, Any]:
    required = {
        "items", "total", "builtinCount", "customCount", "maxCustomRules",
        "enabledRuleCount", "detectorPromptTokens", "detectorPromptTokenLimit",
        "detectorPromptWithinLimit", "detectorPromptTokensRemaining", "epoch",
        "detectorRevision",
    }
    if not isinstance(value, dict) or set(value) != required or not isinstance(value.get("items"), list):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_CATALOGUE_RESPONSE", status=502)
    try:
        items = [_sensitive_rule(item) for item in value["items"]]
    except DataAgentRagV2Error:
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_CATALOGUE_RESPONSE", status=502) from None
    count_keys = (
        "total", "builtinCount", "customCount", "maxCustomRules", "enabledRuleCount",
        "detectorPromptTokens", "detectorPromptTokenLimit", "detectorPromptTokensRemaining", "epoch",
    )
    counts = {key: value.get(key) for key in count_keys}
    if (any(type(item) is not int or item < 0 for item in counts.values())
            or counts["maxCustomRules"] < 1 or counts["detectorPromptTokenLimit"] < 1
            or type(value.get("detectorPromptWithinLimit")) is not bool
            or not isinstance(value.get("detectorRevision"), str)
            or not 1 <= len(value["detectorRevision"]) <= 128
            or any(ord(char) < 32 for char in value["detectorRevision"])
            or counts["total"] != len(items)
            or len({item["ruleId"] for item in items}) != len(items)
            or counts["builtinCount"] != sum(item["source"] == "built_in" for item in items)
            or counts["customCount"] != sum(item["source"] == "custom" for item in items)
            or counts["total"] != counts["builtinCount"] + counts["customCount"]
            or counts["customCount"] > counts["maxCustomRules"]
            or counts["enabledRuleCount"] != sum(item["enabled"] for item in items)
            or value["detectorPromptWithinLimit"] != (
                counts["detectorPromptTokens"] <= counts["detectorPromptTokenLimit"])
            or counts["detectorPromptTokensRemaining"] != max(
                0, counts["detectorPromptTokenLimit"] - counts["detectorPromptTokens"])):
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_CATALOGUE_RESPONSE", status=502)
    return {"items": items, **{key: value[key] for key in required - {"items"}}}


def _etag_revision(headers: Any, expected: int) -> int:
    value = _header(headers, "ETag")
    if value != f'"{expected}"':
        raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_ETAG", status=502)
    return expected


class DataAgentRagV2Client:
    """Synchronous client used by the box-side Zhijun worker."""

    def __init__(
        self,
        base_url: str,
        credential_file: str,
        *,
        timeout: Any = 180,
        opener: Optional[Any] = None,
    ) -> None:
        self.base_url = _validated_base_url(base_url)
        self.credential_file, self._credentials = _read_credentials(credential_file)
        self.timeout = _bounded_timeout(timeout)
        self._opener = opener if opener is not None else _strict_opener()
        self.last_trace_id: Optional[str] = None

    @classmethod
    def from_environment(
        cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any
    ) -> "DataAgentRagV2Client":
        values = os.environ if env is None else env
        credential_file = values.get("ZHIJUN_DATA_AGENT_CREDENTIAL_FILE")
        if not credential_file:
            secret_dir = values.get("CENTAUR_SECRET_STORE_DIR")
            if not secret_dir:
                raise DataAgentRagV2Error("DATA_AGENT_CREDENTIAL_FILE_NOT_CONFIGURED")
            credential_file = str(Path(secret_dir) / "data-agent-rag-v2.json")

        base_url = values.get("ZHIJUN_DATA_AGENT_BASE_URL")
        if not base_url:
            capability_url = values.get("ZHIJUN_CAPABILITY_URL")
            base_url = (
                _origin_from_capability_url(capability_url)
                if capability_url
                else "http://127.0.0.1:8618"
            )
        return cls(base_url, credential_file, **kwargs)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        if_match_revision: Optional[int] = None,
        include_response_headers: bool = False,
    ) -> Any:
        if not path.startswith("/v1/agent/apps/"):
            raise DataAgentRagV2Error("INVALID_APPLICATION_PATH")
        headers = {
            "Accept": "application/json",
            "X-App-Id": self._credentials["appId"],
            "X-App-Secret": self._credentials["appSecret"],
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _idempotency_key(idempotency_key)
        if if_match_revision is not None:
            headers["If-Match"] = f'"{_revision(if_match_revision)}"'
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers
        )
        status: Optional[int] = None
        response_headers: Any = {}
        try:
            response = self._opener.open(request, timeout=self.timeout)
            try:
                status = int(response.status)
                response_headers = response.headers
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                response.close()
        except urllib.error.HTTPError as error:
            try:
                status = int(error.code)
                response_headers = error.headers
                raw = error.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, urllib.error.URLError):
                raise DataAgentRagV2Error(
                    "REQUEST_FAILED", status=503, retryable=True
                ) from None
            finally:
                try:
                    error.close()
                except OSError:
                    pass
        except DataAgentRagV2Error:
            raise
        except (OSError, urllib.error.URLError):
            raise DataAgentRagV2Error(
                "REQUEST_FAILED", status=503, retryable=True
            ) from None

        if self._credentials["appSecret"].encode("utf-8") in raw:
            raise DataAgentRagV2Error("SECRET_REFLECTION_BLOCKED", status=502)
        envelope = _decode_envelope(raw)
        trace_id = envelope["traceId"]
        self.last_trace_id = trace_id
        if "error" in envelope:
            error_value = envelope["error"]
            code = error_value.get("code")
            message = error_value.get("message")
            retryable = error_value.get("retryable")
            if (
                not isinstance(code, str)
                or not code
                or len(code) > 128
                or not code.isascii()
                or not code.replace("_", "").isalnum()
                or not isinstance(message, str)
                or not isinstance(retryable, bool)
            ):
                raise DataAgentRagV2Error(
                    "INVALID_RESPONSE_ENVELOPE", status=502, trace_id=trace_id
                )
            similar_rule_id = None
            if status == 409 and code == "CUSTOM_RULE_SIMILAR":
                raw_similar = _header(response_headers, "X-Similar-Rule-Id")
                try:
                    similar_rule_id = _rule_id(raw_similar, "INVALID_SIMILAR_RULE_RESPONSE")
                except DataAgentRagV2Error:
                    raise DataAgentRagV2Error(
                        "INVALID_SIMILAR_RULE_RESPONSE", status=502, trace_id=trace_id
                    ) from None
            raise DataAgentRagV2Error(
                code,
                status=status,
                message=message,
                retryable=retryable,
                retry_after=_retry_after(response_headers),
                trace_id=trace_id,
                similar_rule_id=similar_rule_id,
            )
        if status is None or not 200 <= status < 300:
            raise DataAgentRagV2Error(
                "INVALID_RESPONSE_ENVELOPE", status=502, trace_id=trace_id
            )
        data = envelope["data"]
        return (data, response_headers) if include_response_headers else data

    def capabilities(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/agent/apps/capabilities")

    def upload_constraints(self) -> Tuple[int, set[str]]:
        capabilities = self.capabilities()
        maximum = capabilities.get("maxFileBytes") if isinstance(capabilities, dict) else None
        file_types = capabilities.get("supportedFileTypes") if isinstance(capabilities, dict) else None
        enabled = capabilities.get("enabledCapabilities") if isinstance(capabilities, dict) else None
        if (type(maximum) is not int or not 1 <= maximum <= MAX_FILE_BYTES
                or not isinstance(file_types, list) or not file_types
                or any(not isinstance(value, str) or not value for value in file_types)
                or not isinstance(enabled, list) or "mindos.import" not in enabled):
            raise DataAgentRagV2Error("INVALID_CAPABILITIES_RESPONSE", status=502)
        supported = {"." + value.casefold().lstrip(".") for value in file_types}
        if not supported <= _SUPPORTED_UPLOAD_SUFFIXES:
            raise DataAgentRagV2Error("INVALID_CAPABILITIES_RESPONSE", status=502)
        return maximum, supported

    def upload_bytes(
        self,
        filename: str,
        file_bytes: bytes,
        *,
        idempotency_key: str,
        title: Optional[str] = None,
        external_reference: Optional[str] = None,
    ) -> Dict[str, Any]:
        fields: Dict[str, str] = {}
        if title is not None:
            fields["title"] = title
        if external_reference is not None:
            fields["externalReference"] = external_reference
        maximum, supported = self.upload_constraints()
        body, content_type = _multipart_bytes(
            filename, file_bytes, fields, max_file_bytes=maximum,
            supported_suffixes=supported,
        )
        return _upload_accepted(self._request(
            "POST",
            "/v1/agent/apps/material-jobs",
            body=body,
            content_type=content_type,
            idempotency_key=idempotency_key,
        ))

    def job_status(self, job_id: str) -> Dict[str, Any]:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise DataAgentRagV2Error("INVALID_JOB_ID")
        value = _job_status(self._request(
            "GET", "/v1/agent/apps/material-jobs/" + quote(job_id, safe="")
        ))
        if value["jobId"] != job_id:
            raise DataAgentRagV2Error("INVALID_JOB_STATUS_RESPONSE", status=502)
        return value

    def wait_indexed(
        self, job_id: str, *, timeout: Any = 180, poll_interval: Any = 0.5
    ) -> Dict[str, Any]:
        wait_timeout = _bounded_timeout(timeout)
        try:
            interval = float(poll_interval)
        except (TypeError, ValueError):
            raise DataAgentRagV2Error("INVALID_POLL_INTERVAL") from None
        if not 0.05 <= interval <= 30:
            raise DataAgentRagV2Error("INVALID_POLL_INTERVAL")
        deadline = time.monotonic() + wait_timeout
        while True:
            value = self.job_status(job_id)
            if (
                value.get("state") == "ready"
                and value.get("stage") == "completed"
                and value.get("indexState") == "indexed"
            ):
                return value
            if value.get("state") in {"failed", "canceled"}:
                error_code = value.get("errorCode")
                if not isinstance(error_code, str) or not error_code:
                    error_code = "MATERIAL_JOB_FAILED"
                raise DataAgentRagV2Error(
                    error_code,
                    status=409,
                    retryable=value.get("retryable") is True,
                    trace_id=self.last_trace_id,
                )
            if time.monotonic() >= deadline:
                raise DataAgentRagV2Error(
                    "MATERIAL_JOB_INDEX_TIMEOUT",
                    status=504,
                    retryable=True,
                    trace_id=self.last_trace_id,
                )
            time.sleep(interval)

    def search(
        self,
        query: str,
        interaction_id: str,
        *,
        top_k: int = 5,
        material_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(query, str):
            raise DataAgentRagV2Error("INVALID_SEARCH_REQUEST")
        normalized_query = query.strip()
        if not 1 <= len(normalized_query) <= 1000 or not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
            raise DataAgentRagV2Error("INVALID_SEARCH_REQUEST")
        if not isinstance(interaction_id, str) or _INTERACTION_ID.fullmatch(interaction_id) is None:
            raise DataAgentRagV2Error("INVALID_INTERACTION_ID")
        try:
            raw_material_ids = list(material_ids or [])
        except TypeError:
            raise DataAgentRagV2Error("INVALID_SEARCH_REQUEST") from None
        if (
            any(not isinstance(item, str) for item in raw_material_ids)
            or len(raw_material_ids) > 100
            or len(set(raw_material_ids)) != len(raw_material_ids)
        ):
            raise DataAgentRagV2Error("INVALID_SEARCH_REQUEST")
        checked_material_ids = [
            _identifier(item, "INVALID_SEARCH_REQUEST") for item in raw_material_ids
        ]
        payload: Dict[str, Any] = {
            "query": normalized_query,
            "topK": top_k,
            "types": ["material"],
            "clientContext": {"interactionId": interaction_id},
        }
        if checked_material_ids:
            payload["filters"] = {"materialIds": checked_material_ids}
        return self._request(
            "POST",
            "/v1/agent/apps/search",
            body=_json_body(payload),
            content_type="application/json",
        )

    def confirm(
        self,
        confirm_token: str,
        *,
        desensitize: bool,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if not isinstance(desensitize, bool):
            raise DataAgentRagV2Error("INVALID_CONFIRM_REQUEST")
        return self._request(
            "POST",
            "/v1/agent/apps/search/confirm",
            body=_json_body(
                {"confirmToken": _confirm_token(confirm_token), "desensitize": desensitize}
            ),
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def confirm_unverified(self, confirm_token: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/v1/agent/apps/search/confirm-unverified",
            body=_json_body(
                {
                    "confirmToken": _confirm_token(confirm_token),
                    "decision": "release_unverified_original",
                    "acknowledgeRisk": True,
                }
            ),
            content_type="application/json",
        )

    def evidence_resolve(self, evidence_refs: Iterable[str]) -> Dict[str, Any]:
        try:
            values = list(evidence_refs)
        except TypeError:
            raise DataAgentRagV2Error("INVALID_EVIDENCE_REFERENCE") from None
        if (
            any(not isinstance(value, str) for value in values)
            or
            not 1 <= len(values) <= 10
            or len(set(values)) != len(values)
            or any(
                not value.startswith("erv2_")
                or len(value) > 2048
                or value != value.strip()
                for value in values
            )
        ):
            raise DataAgentRagV2Error("INVALID_EVIDENCE_REFERENCE")
        return self._request(
            "POST",
            "/v1/agent/apps/evidence:resolve",
            body=_json_body({"evidenceRefs": values}),
            content_type="application/json",
        )

    def list_sensitive_rules(self) -> Dict[str, Any]:
        return _sensitive_catalogue(self._request(
            "GET", "/v1/agent/apps/sensitive-delivery/rules"
        ))

    def get_sensitive_rule_status(self) -> Dict[str, Any]:
        return sensitive_rule_status(self._request(
            "GET", "/v1/agent/apps/sensitive-delivery/status"
        ))

    def get_sensitive_rule(self, rule_id: str) -> Dict[str, Any]:
        checked_id = _rule_id(rule_id)
        data, headers = self._request(
            "GET", "/v1/agent/apps/sensitive-delivery/rules/" + quote(checked_id, safe=""),
            include_response_headers=True,
        )
        item = _sensitive_rule(data)
        if item["ruleId"] != checked_id:
            raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_RESPONSE", status=502)
        _etag_revision(headers, item["revision"])
        return item

    def create_sensitive_rule(
        self, rule: Mapping[str, Any], *, idempotency_key: str
    ) -> Dict[str, Any]:
        data, headers = self._request(
            "POST", "/v1/agent/apps/sensitive-delivery/rules/custom",
            body=_json_body(_sensitive_rule_input(rule)), content_type="application/json",
            idempotency_key=idempotency_key, include_response_headers=True,
        )
        item = _sensitive_rule(data)
        if item["source"] != "custom":
            raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_RESPONSE", status=502)
        _etag_revision(headers, item["revision"])
        return item

    def update_sensitive_rule(
        self, rule_id: str, rule: Mapping[str, Any], *, revision: int,
    ) -> Dict[str, Any]:
        checked_id = _rule_id(rule_id)
        data, headers = self._request(
            "PUT", "/v1/agent/apps/sensitive-delivery/rules/custom/" + quote(checked_id, safe=""),
            body=_json_body(_sensitive_rule_input(rule)), content_type="application/json",
            if_match_revision=revision, include_response_headers=True,
        )
        item = _sensitive_rule(data)
        if item["ruleId"] != checked_id or item["source"] != "custom":
            raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_RESPONSE", status=502)
        _etag_revision(headers, item["revision"])
        return item

    def delete_sensitive_rule(self, rule_id: str, *, revision: int) -> Dict[str, bool]:
        checked_id = _rule_id(rule_id)
        data = self._request(
            "DELETE", "/v1/agent/apps/sensitive-delivery/rules/custom/" + quote(checked_id, safe=""),
            if_match_revision=revision,
        )
        if not isinstance(data, dict) or set(data) != {"deleted"} or data["deleted"] is not True:
            raise DataAgentRagV2Error("INVALID_SENSITIVE_RULE_DELETE_RESPONSE", status=502)
        return {"deleted": True}


def configured_client(
    env: Optional[Mapping[str, str]] = None, **kwargs: Any
) -> DataAgentRagV2Client:
    """Build a client from box-managed environment and secret-store settings."""

    return DataAgentRagV2Client.from_environment(env, **kwargs)


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_RESPONSE_BYTES",
    "DataAgentRagV2Client",
    "DataAgentRagV2Error",
    "configured_client",
]
