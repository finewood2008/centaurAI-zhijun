"""ChatProvider implemented by the authenticated DE capability service."""
from dataclasses import asdict

from .capabilities import require, CapabilityError


def _stream_error_message(code, *, local_only=False):
    if code == "MODEL_RESPONSE_EMPTY":
        return "模型没有返回可显示的正文，请重试当前模式；原消息和章程草稿仍保留"
    if code in {"MODEL_PROVIDER_FAILED", "CAPABILITY_UNAVAILABLE"}:
        return ("盒子的本地模型服务没有运行或尚未就绪，请检查本地模型运行状态后重试"
                if local_only else "当前模型服务暂时不可用，请检查模型运行状态后重试")
    if code in {"MODEL_TIMEOUT", "MODEL_TOTAL_TIMEOUT", "MODEL_QUEUE_TIMEOUT"}:
        return ("盒子的本地模型等待超时，请稍后重试；原消息仍保留"
                if local_only else "当前模型等待超时，请稍后重试；原消息仍保留")
    if code == "MODEL_CONFIGURATION_CHANGED":
        return "模型设置刚刚发生变化，请重新读取设置后重试；原消息仍保留"
    return "模型能力调用失败"


class CapabilityProvider:
    def __init__(self, *, local_only=False):
        self.local_only = local_only
        info = require().call("model.describe", {"localOnly": local_only})
        if not isinstance(info, dict) or any(key not in info for key in ("name", "model", "external", "configurationRevision", "serviceId")) or type(info["external"]) is not bool:
            raise CapabilityError("CAPABILITY_MODEL_CONTRACT", 502)
        if local_only and info["external"]:
            raise CapabilityError("CAPABILITY_LOCAL_MODEL_REQUIRED", 502)
        self.name, self.model, self.external = info["name"], info["model"], info["external"]
        self.configuration_revision = info["configurationRevision"]
        self.service_id = info["serviceId"]
        self.last_usage = None

    def _payload(self, request):
        from mindos.zhijun.routing import EGRESS_PERMIT
        from mindos.zhijun.provider import ProviderError
        permit = EGRESS_PERMIT.get()
        preview = permit() if callable(permit) else None
        if self.external and not preview:
            raise ProviderError("在线任务尚未通过来源授权检查", code="EGRESS_NOT_AUTHORIZED", retryable=False)
        payload = {"request": asdict(request), "localOnly": self.local_only,
                   "configurationRevision": self.configuration_revision}
        if preview:
            payload["purpose"] = preview.get("purpose", (request.debug or {}).get("task", "chat"))
            payload["sourceRefs"] = [{"sourceType": item["kind"], "id": item["id"],
                "version": item["ref"].get("materialVersion", item["version"])} for item in preview.get("sources", [])]
            payload["egressPermit"] = {"authorized": True}
            if self.external:
                from .consent import receipt
                payload.update(receipt(preview, self))
        return payload

    def stream(self, request):
        from mindos.zhijun.provider import TextDelta, Usage, Done, ProviderError
        terminal = False
        try:
            for event in require().stream("model.stream", self._payload(request)):
                if terminal or type(event) is not dict:
                    raise CapabilityError("CAPABILITY_STREAM_CONTRACT", 502)
                if event.get("type") == "text" and isinstance(event.get("text"), str):
                    yield TextDelta(event["text"])
                elif event.get("type") == "usage":
                    self.last_usage = {key: event.get(key) for key in ("input_tokens", "output_tokens")}
                    yield Usage(**self.last_usage)
                elif event.get("type") == "done":
                    terminal = True
                    yield Done(event.get("stop_reason"))
                elif event.get("type") == "error":
                    raise CapabilityError(event.get("code", "CAPABILITY_MODEL_ERROR"), 502)
                else:
                    raise CapabilityError("CAPABILITY_STREAM_CONTRACT", 502)
            if not terminal:
                raise CapabilityError("CAPABILITY_STREAM_INTERRUPTED", 502)
        except CapabilityError as exc:
            raise ProviderError(_stream_error_message(exc.code, local_only=self.local_only), status_code=exc.status, code=exc.code,
                                retryable=exc.status in {429, 502, 503, 504}) from None

    def complete_json(self, request):
        from mindos.zhijun.provider import ProviderError
        try:
            result = require().call("model.complete_json", self._payload(request))
            if type(result) is not dict:
                raise CapabilityError("CAPABILITY_MODEL_CONTRACT", 502)
            return result
        except CapabilityError as exc:
            raise ProviderError("模型能力调用失败", status_code=exc.status, code=exc.code, retryable=exc.status in {429, 502, 503, 504}) from None
