"""Bounded, explicit model-list discovery; no user content and no redirects."""
import json
import time
import urllib.error
import urllib.request

from .llm_transport import _check_destination, _NoModelRedirect
from .runtime_config_provider import validate_external_base_url, validate_model_name, ValidationError

MAX_BYTES = 1024 * 1024
TIMEOUT = 8
TOTAL_SECONDS = 20


class DiscoveryError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def discover_models(base_url, api_key):
    base_url = validate_external_base_url(base_url)
    url = base_url + "/models"
    _check_destination(url)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"}, method="GET")
    started = time.monotonic()
    response = None
    try:
        response = urllib.request.build_opener(_NoModelRedirect()).open(request, timeout=TIMEOUT)
        if getattr(response, "status", 200) != 200:
            raise DiscoveryError("models_rejected", "服务没有返回模型列表；仍可手动填写模型名")
        raw, count = [], 0
        while True:
            if time.monotonic() - started > TOTAL_SECONDS:
                raise DiscoveryError("models_timeout", "获取模型列表超时，可重试或手动填写")
            # HTTPResponse.read(n) waits to fill n bytes and permits a slow
            # drip to evade the total budget. read1 returns after one socket
            # read, so every chunk gets a deadline check.
            reader = getattr(response, "read1", response.read)
            chunk = reader(min(16384, MAX_BYTES + 1 - count))
            if time.monotonic() - started > TOTAL_SECONDS:
                raise DiscoveryError("models_timeout", "获取模型列表超时，可重试或手动填写")
            if not chunk:
                break
            raw.append(chunk)
            count += len(chunk)
            if count > MAX_BYTES:
                raise DiscoveryError("models_too_large", "模型列表响应过大；请手动填写模型名")
        payload = json.loads(b"".join(raw))
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("invalid list")
        models = set()
        for row in rows:
            try:
                name = row.get("id") if isinstance(row, dict) else None
                if not isinstance(name, str):
                    continue
                models.add(validate_model_name(name))
            except ValidationError:
                continue
            if len(models) > 1000:
                raise DiscoveryError("models_too_large", "服务返回的模型数量过多；请手动填写模型名")
        return sorted(models)
    except DiscoveryError:
        raise
    except urllib.error.HTTPError as exc:
        code = "models_redirect" if 300 <= exc.code < 400 else "models_unauthorized" if exc.code in (401, 403) else "models_rejected"
        raise DiscoveryError(code, "获取模型列表失败；请核对服务地址与凭证，或手动填写模型名") from None
    except (TimeoutError, urllib.error.URLError, OSError):
        raise DiscoveryError("models_unreachable", "无法连接模型服务，可重试或手动填写模型名") from None
    except (ValueError, TypeError, UnicodeError):
        raise DiscoveryError("models_invalid", "服务未返回兼容的模型列表；请手动填写模型名") from None
    finally:
        if response is not None:
            response.close()
