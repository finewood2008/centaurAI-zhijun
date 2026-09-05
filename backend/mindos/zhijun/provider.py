"""知君模型通道抽象。

- FakeProvider：确定性脚本，仅供开发与自动化测试（``ZHIJUN_PROVIDER=fake``，生产环境拒绝启用）。
- OllamaProvider：本地 ``/api/chat`` 流式 NDJSON；抽取用 ``format: json``。
- OpenAICompatibleProvider：``/chat/completions`` SSE 流式；抽取用 ``response_format: json_object``。
- AnthropicProvider：官方 ``anthropic`` SDK（延迟导入），流式 + ``output_config.format`` 结构化输出。
  注意：本机策略禁止访问 anthropic.com 时不得启用；本地联调只用 fake / ollama / openai-compatible。

本层不做隐私过滤（由 context.py 负责），不记录提示词正文。
"""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
from dataclasses import dataclass, field
from typing import Iterator, Protocol

from .. import llm_transport
from ..runtime_config_provider import get_provider

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_LOCAL_NUM_CTX = 8192


# ---------------------------------------------------------------- 类型
@dataclass(frozen=True)
class ChatRequest:
    system: str
    messages: list[dict]
    max_tokens: int = 1024
    temperature: float = 0.4
    json_schema: dict | None = None
    effort: str = "low"
    # 仅 FakeProvider 使用：上下文摘要，让演示回复能证明「记得」与「不复述被纠正的理解」。
    debug: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class Done:
    stop_reason: str | None = None


ChatEvent = TextDelta | Usage | Done


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        code: str = "PROVIDER_UNAVAILABLE",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class ChatProvider(Protocol):
    name: str
    model: str
    external: bool

    def stream(self, req: ChatRequest) -> Iterator[ChatEvent]: ...

    def complete_json(self, req: ChatRequest) -> dict: ...


# ---------------------------------------------------------------- 工具
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def parse_json_object(text: str) -> dict:
    """从模型输出里取出第一个 JSON 对象（容忍代码围栏与前后杂讯）。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型输出为空")
    raw = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出不含 JSON 对象")
    obj = json.loads(raw[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return obj


def _chunks(text: str, size: int) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i : i + size]


def _http_error(exc: urllib.error.HTTPError, provider: str) -> ProviderError:
    code = int(exc.code)
    if code == 401 or code == 403:
        return ProviderError(f"{provider} 鉴权失败，请检查 API Key", status_code=503, code="PROVIDER_MISCONFIGURED", retryable=False)
    if code == 429:
        return ProviderError(f"{provider} 限流，请稍后重试", status_code=429, code="PROVIDER_BUSY", retryable=True)
    if 400 <= code < 500:
        return ProviderError(f"{provider} 返回 {code}，请检查模型配置", status_code=502, code="PROVIDER_REJECTED", retryable=False)
    return ProviderError(f"{provider} 返回 {code}，请稍后重试", status_code=502, code="PROVIDER_UNAVAILABLE", retryable=True)


def _open(url: str, body: dict, *, timeout: float, headers: dict, provider: str, channel: str):
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if any(host == h or host.endswith("." + h) for h in ("anthropic.com", "claude.ai")):
        raise ProviderError("本机禁止访问此服务", code="SERVICE_FORBIDDEN", retryable=False)
    if channel == "chat":
        from .routing import EGRESS_PERMIT
        permit = EGRESS_PERMIT.get()
        if not callable(permit):
            raise ProviderError("在线任务尚未通过来源授权检查，已暂停", code="EGRESS_NOT_AUTHORIZED", retryable=False)
        permit()  # Revalidate at the actual HTTP boundary, after queueing/serialization.
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        return llm_transport.allowed_urlopen(
            url,
            channel=channel,
            store=None,
            timeout=timeout,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, provider) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderError(f"{provider} 响应超时", status_code=504, code="PROVIDER_TIMEOUT", retryable=True) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise ProviderError(f"{provider} 响应超时", status_code=504, code="PROVIDER_TIMEOUT", retryable=True) from exc
        raise ProviderError(f"{provider} 不可用，请检查模型服务", status_code=503, code="PROVIDER_UNAVAILABLE", retryable=True) from exc
    except (OSError, ConnectionError) as exc:
        raise ProviderError(f"{provider} 不可用，请检查模型服务", status_code=503, code="PROVIDER_UNAVAILABLE", retryable=True) from exc


def _iter_lines(resp) -> Iterator[str]:
    """逐行读取 HTTP 响应体（NDJSON / SSE 共用），容忍连接中断。"""
    try:
        for raw in resp:
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderError("模型流式响应超时", status_code=504, code="PROVIDER_TIMEOUT", retryable=True) from exc
    except (OSError, ConnectionError, urllib.error.URLError) as exc:
        raise ProviderError("模型流式响应中断", status_code=502, code="PROVIDER_UNAVAILABLE", retryable=True) from exc


# ---------------------------------------------------------------- Fake
ONBOARDING_QUESTIONS: tuple[str, ...] = (
    "先认识一下：我该怎么称呼你？你现在的生活里主要扮演哪几个角色（工作、家庭，或你在意的身份）？",
    "你现在手头正在做的、最占心思的一件事是什么？",
    "你生活里最在意的人有哪些？他们和你是什么关系？",
    "最近一次让你纠结的判断是什么？你最后怎么选的，为什么？",
    "有没有一条你做事时一直坚持的原则？",
    "接下来一两年，你想成为什么样的人，或者想把什么事做成？",
    "有哪些话题你不希望我主动提起，或者不希望由 AI 来替你判断？",
)

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
_FIRST_PERSON_RE = re.compile(r"我|咱|俺")


def _fake_section(sentence: str) -> tuple[str, str, str]:
    """启发式分区：返回 (section, layer, predicate)。仅供演示模型。"""
    wishful = any(k in sentence for k in ("想成为", "想要", "希望", "打算", "目标", "想把", "想做", "愿望")) or (
        "想" in sentence and any(k in sentence for k in ("成为", "做到", "做成", "实现", "达到", "变成", "把"))
    )
    if wishful:
        return "direction", "aspirational", "wants_to"
    if any(k in sentence for k in ("原则", "绝不", "从不", "底线", "不能接受", "坚持")):
        return "principles", "self_declared", "holds_principle"
    if any(k in sentence for k in ("喜欢", "习惯", "一般", "总是", "通常", "偏好", "倾向", "不喜欢")):
        return "ways", "self_declared", "prefers"
    if any(
        k in sentence
        for k in ("朋友", "同事", "老婆", "妻子", "丈夫", "老公", "太太", "女儿", "儿子", "合伙人", "老板", "团队", "父母", "妈", "爸", "孩子", "搭档", "伙伴", "一起", "认识", "关系")
    ):
        return "people", "self_declared", "knows"
    if any(k in sentence for k in ("项目", "工作", "在做", "负责", "公司", "创业", "产品", "计划", "正在", "忙")):
        return "matters", "self_declared", "working_on"
    return "who", "self_declared", "is"


def fake_extract(user_text: str) -> dict:
    """无模型时的规则抽取：只从含第一人称的句子里取候选，原句即 quote，最多 4 条。"""
    claims: list[dict] = []
    for sentence in _SENTENCE_SPLIT_RE.split(user_text or ""):
        sentence = sentence.strip()
        if len(sentence) < 6 or not _FIRST_PERSON_RE.search(sentence):
            continue
        section, layer, predicate = _fake_section(sentence)
        claims.append(
            {
                "section": section,
                "layer": layer,
                "predicate": predicate,
                "subject": "me",
                "object": None,
                "content": sentence[:120],
                "quote": sentence,
                "confidence": 0.9 if layer == "self_declared" else 0.7,
                "scope_hint": "long_term",
                "privacy_hint": "private",
                "why_it_matters": {
                    "who": "以后讨论工作时可以结合用户明确的角色和背景",
                    "people": "以后讨论关系选择时可以区分用户提到的重要关系",
                    "matters": "继续讨论这件事时可以保留用户已经说明的事项和约束",
                    "principles": "以后比较选择时可以参考用户亲口说明的原则与边界",
                    "ways": "继续讨论选择时可以核对用户自己说明的做法和偏好",
                    "direction": "以后讨论行动方案时可以区分用户的愿望与当前经历",
                }.get(section, ""),
            }
        )
        if len(claims) >= 4:
            break
    return {"claims": claims, "entities": []}


_OPTION_SPLIT_RE = re.compile(r"还是|或者|或是")
_DRAFT_CONF_RE = re.compile(r"(\d{1,3})\s*%|([一二三四五六七八九])成")
_CN_TEN = {"一": 10, "二": 20, "三": 30, "四": 40, "五": 50, "六": 60, "七": 70, "八": 80, "九": 90}


def fake_draft(user_texts: list[str], assistant_text: str = "") -> dict:
    """无模型时的规则版判断草稿：只从用户原句里取字段，原句即 quote。"""
    text = "\n".join(user_texts or [])
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    options: list[str] = []
    leaning = choice = rationale = expected = None
    confidence = None
    quotes: list[str] = []
    for sentence in sentences:
        if not options and _OPTION_SPLIT_RE.search(sentence):
            parts = [p.strip(" ，,、？?") for p in _OPTION_SPLIT_RE.split(sentence)]
            parts = [re.sub(r"^(我|我们|现在|在纠结|纠结|到底|是|该|应该|要不要|要不|要|把|得|想)+", "", p).strip() for p in parts]
            options = [p[:40] for p in parts if p][:6]
            quotes.append(sentence)
        if leaning is None and any(k in sentence for k in ("倾向", "想选", "偏向", "更想")):
            leaning = sentence
            quotes.append(sentence)
        if choice is None and any(k in sentence for k in ("决定", "定了", "就选", "选择", "我选")):
            choice = sentence
            quotes.append(sentence)
        if rationale is None and any(k in sentence for k in ("因为", "理由", "原因", "主要是")):
            rationale = sentence
            quotes.append(sentence)
        if expected is None and any(k in sentence for k in ("预期", "希望", "期待", "应该能", "预计")):
            expected = sentence
            quotes.append(sentence)
        if confidence is None:
            m = _DRAFT_CONF_RE.search(sentence)
            if m:
                confidence = int(m.group(1)) if m.group(1) else _CN_TEN.get(m.group(2))
                quotes.append(sentence)
    view = None
    if "【知君的看法】" in (assistant_text or ""):
        view = assistant_text.split("【知君的看法】", 1)[1].strip().split("\n")[0][:300]
    return {
        "title": (user_texts[0].strip().replace("\n", " ")[:30] if user_texts else ""),
        "context": text.replace("\n", "；")[:300],
        "options": options,
        "leaning": leaning,
        "choice": choice,
        "rationale": rationale,
        "confidence": confidence,
        "expectedOutcome": expected,
        "reviewAt": None,
        "keyQuestion": "如果选了另一个，最坏的情况你能接受吗？",
        "zhijunView": view,
        "userQuotes": list(dict.fromkeys(quotes)),
    }


class FakeProvider:
    name = "fake"
    external = False

    def __init__(self, model: str = "fake-zhijun") -> None:
        self.model = model

    def _reply(self, req: ChatRequest) -> str:
        debug = req.debug or {}
        # 优先用本轮原话（历史里可能折叠了「（备注：…）」系统备注），避免把备注回显成话题。
        user_text = str(debug.get("userText") or "")
        if not user_text:
            for message in reversed(req.messages):
                if message.get("role") == "user":
                    user_text = str(message.get("content") or "")
                    break
        if debug.get("mode") == "review":
            decision = debug.get("decision") or {}
            title = decision.get("title") or "那件事"
            if debug.get("outcomeRecorded"):
                return (
                    f"结果记下了。我们按五段来复盘「{title}」：\n\n1. 观察：结果和你当时的预期之间有一段差距。\n"
                    f"2. 依据：【你告诉我的】当时你选了「{decision.get('choice', '')}」，把握 {decision.get('confidence', '?')}%。\n"
                    "3. 其他解释：也可能是外部条件变了，而不是判断本身错了。\n4. 想确认：当时最关键的那个假设，现在看还成立吗？\n"
                    "5. 可尝试：用一句话说出这次可复用的经验，我帮你记进判断簿的复盘里。"
                )
            return (
                f"这是对「{title}」的回访。当时你选了「{decision.get('choice', '')}」，预期是「{str(decision.get('expectedOutcome', ''))[:60]}」。\n\n"
                "实际发生了什么？和预期比差在哪？说完可以点「记下结果」。"
            )
        if debug.get("turnMode") == "deliberate":
            draft = fake_draft(list(debug.get("userTexts") or [user_text]), "")
            confirmed = [str(c) for c in (debug.get("confirmedClaims") or [])][:3]
            recall = ("我先把记得的放在一起：" + "；".join(f"【你告诉我的】{c}" for c in confirmed) + "。") if confirmed else "这件事的背景我还了解得不多。"
            options = draft["options"]
            option_line = ("你面前的选项：" + " / ".join(options) + "。") if options else "你还没说清有哪几个选项，先说说是哪几个？"
            return (
                f"{recall}\n\n{option_line}\n\n一个关键问题：{draft['keyQuestion']}\n\n"
                "【知君的看法】演示模型没有真正的看法；接入真实模型后这里会给出带依据的意见，但决定在你。\n\n"
                "你说出选择、理由和把握有几成，我就把它记进判断簿，到期再来回访。"
            )
        if debug.get("mode") == "onboarding":
            if debug.get("lightOnboarding"):
                from .charter import TOPICS
                question = next((t[2] for t in TOPICS if t[0] == debug.get("onboardingTopic")), "")
                return "我先把这些作为待核对的理解。" + (question or "我们可以先从这里开始。查看第一次认识小结，或直接开始使用，以后还可以继续完善。")
            n = int(debug.get("userTurns") or 1)
            if n <= len(ONBOARDING_QUESTIONS):
                prefix = "" if n == 1 else "记下了。"
                return f"{prefix}{ONBOARDING_QUESTIONS[n - 1]}"
            return "谢谢，这七个问题我都记下了。你可以去「我的本体」核对我记住的内容，不对的直接改。"
        parts: list[str] = []
        confirmed = [str(c) for c in (debug.get("confirmedClaims") or [])][:3]
        working = [str(w) for w in (debug.get("workingClaims") or [])][:2]
        if confirmed:
            parts.append("我记得你说过：" + "；".join(f"【你告诉我的】{c}" for c in confirmed) + "。")
        if working:
            parts.append("我印象里" + "；".join(f"【我推测的】{w}" for w in working) + "，对吗？")
        topic = user_text.strip().replace("\n", " ")[:40]
        parts.append(f"关于「{topic}」，你最在意的是什么？")
        if debug.get("depth") == "deep":
            parts.append("【知君的看法】这是演示模型，没有真正的看法；接入真实模型后这里会给出带依据的意见。")
        return "\n\n".join(parts)

    def stream(self, req: ChatRequest) -> Iterator[ChatEvent]:
        text = self._reply(req)
        for chunk in _chunks(text, 12):
            yield TextDelta(chunk)
        yield Usage(len(req.system) // 4, len(text) // 4)
        yield Done("end_turn")

    def complete_json(self, req: ChatRequest) -> dict:
        debug = req.debug or {}
        if debug.get("task") == "charter_draft":
            return {"proposals": []}  # Demo mode does not invent personal directions.
        if debug.get("task") == "decision_draft":
            return fake_draft(list(debug.get("userTexts") or []), str(debug.get("assistantText") or ""))
        if debug.get("task") == "summary":
            texts = [str(t) for t in (debug.get("userTexts") or []) if str(t).strip()]
            themes = list(dict.fromkeys(t.strip()[:24] for t in texts))[:4]
            loops = [t.strip()[:40] for t in texts if any(k in t for k in ("打算", "准备", "下周", "要做", "计划"))][:3]
            return {"summary": "；".join(t.strip()[:60] for t in texts)[:400], "themes": themes, "open_loops": loops}
        if debug.get("task") == "first_observation":
            basis = list(debug.get("basisClaims") or [])
            if len(basis) < 2:
                return {"content": None, "basis_claim_ids": [], "section": "ways", "question": None}
            a, b = basis[0], basis[1]
            return {
                "content": f"你提到「{a['content'][:16]}」和「{b['content'][:16]}」时都在先把人和事理清楚，我猜你做事习惯先搭人再搭事",
                "basis_claim_ids": [a["id"], b["id"]],
                "section": "ways",
                "question": "——对吗？这只是印象，你点头它才算数。",
            }
        user_text = str(debug.get("userText") or "")
        if not user_text:
            for message in reversed(req.messages):
                if message.get("role") == "user":
                    user_text = str(message.get("content") or "")
                    break
        return fake_extract(user_text)


# ---------------------------------------------------------------- Ollama
class OllamaProvider:
    name = "ollama"
    external = False

    def __init__(self, base_url: str, model: str, *, timeout: float, keep_alive: int = 0, num_ctx: int = DEFAULT_LOCAL_NUM_CTX) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._timeout = timeout
        self._keep_alive = keep_alive
        self._num_ctx = int(num_ctx)

    def _body(self, req: ChatRequest, *, stream: bool) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": req.system}, *req.messages],
            "stream": stream,
            "think": False,
            "keep_alive": self._keep_alive,
            "options": {"temperature": req.temperature, "num_ctx": self._num_ctx, "num_predict": req.max_tokens},
        }
        if req.json_schema is not None:
            body["format"] = "json"
        return body

    def stream(self, req: ChatRequest) -> Iterator[ChatEvent]:
        resp = _open(self._base_url + "/api/chat", self._body(req, stream=True), timeout=self._timeout, headers={}, provider="本地模型", channel="material")
        stop = None
        for line in _iter_lines(resp):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("error"):
                raise ProviderError(f"本地模型错误：{str(obj['error'])[:120]}", status_code=502, code="PROVIDER_REJECTED", retryable=False)
            content = (obj.get("message") or {}).get("content") or ""
            if content:
                yield TextDelta(content)
            if obj.get("done"):
                stop = obj.get("done_reason")
                yield Usage(obj.get("prompt_eval_count"), obj.get("eval_count"))
                break
        yield Done(stop or "stop")

    def complete_json(self, req: ChatRequest) -> dict:
        resp = _open(self._base_url + "/api/chat", self._body(req, stream=False), timeout=self._timeout, headers={}, provider="本地模型", channel="material")
        payload = json.loads(resp.read().decode("utf-8"))
        self.last_usage = {"input_tokens": payload.get("prompt_eval_count"), "output_tokens": payload.get("eval_count")}
        if payload.get("error"):
            raise ProviderError(f"本地模型错误：{str(payload['error'])[:120]}", status_code=502, code="PROVIDER_REJECTED", retryable=False)
        return parse_json_object((payload.get("message") or {}).get("content") or "")


# ---------------------------------------------------------------- OpenAI-compatible
JSON_TASK_MIN_TOKENS = 6000


class OpenAICompatibleProvider:
    name = "openai"
    external = True

    def __init__(self, base_url: str, model: str, api_key: str, *, timeout: float, task_model: str | None = None, thinking: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model
        # 后台 JSON 任务（抽取 / 草稿 / 摘要 / 矛盾判定）可用更快的模型；推理型模型会先花预算写 reasoning，
        # 预算太小时正文为空，所以 JSON 任务的 max_tokens 至少 JSON_TASK_MIN_TOKENS。
        self.task_model = task_model or model
        self._api_key = api_key
        self._timeout = timeout
        # 思考开关：DeepSeek 接受 ``thinking: {"type": "disabled"}``。effort=low（简短回复、抽取、摘要、草稿）
        # 关掉思考——不截断、更快；effort=medium/high（深聊、商量、第一次观察）保留思考并放宽预算。
        # 其它 OpenAI 兼容服务可能拒绝未知参数，所以只在 DeepSeek 或 ZHIJUN_OPENAI_THINKING=deepseek 时发送。
        self._thinking = thinking or ("deepseek" if "deepseek" in base_url.lower() else "off")

    def _apply_thinking(self, body: dict, req: ChatRequest) -> None:
        if self._thinking != "deepseek":
            return
        deep = (req.effort or "low") != "low"
        body["thinking"] = {"type": "enabled" if deep else "disabled"}
        if deep:
            body["max_tokens"] = max(int(body["max_tokens"]), JSON_TASK_MIN_TOKENS)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def stream(self, req: ChatRequest) -> Iterator[ChatEvent]:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": req.system}, *req.messages],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "stream": True,
        }
        self._apply_thinking(body, req)
        resp = _open(self._base_url + "/chat/completions", body, timeout=self._timeout, headers=self._headers(), provider="外部模型", channel="chat")
        finish = None
        usage: Usage | None = None
        for line in _iter_lines(resp):
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if obj.get("error"):
                raise ProviderError("外部模型返回错误", status_code=502, code="PROVIDER_REJECTED", retryable=False)
            if obj.get("usage"):
                usage = Usage(obj["usage"].get("prompt_tokens"), obj["usage"].get("completion_tokens"))
            for choice in obj.get("choices") or []:
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    yield TextDelta(delta)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        yield usage or Usage()
        yield Done(finish or "stop")

    def complete_json(self, req: ChatRequest) -> dict:
        lookup = (req.debug or {}).get("task") == "context_lookup"
        self.last_usage = None
        body = {
            "model": self.model if lookup else self.task_model,
            "messages": [{"role": "system", "content": req.system}, *req.messages],
            "temperature": req.temperature,
            "max_tokens": int(req.max_tokens) if lookup else max(int(req.max_tokens), JSON_TASK_MIN_TOKENS),
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        self._apply_thinking(body, req)
        resp = _open(self._base_url + "/chat/completions", body, timeout=self._timeout, headers=self._headers(), provider="外部模型", channel="chat")
        payload = json.loads(resp.read().decode("utf-8"))
        raw_usage = payload.get("usage") or {}
        self.last_usage = {"input_tokens": raw_usage.get("prompt_tokens"), "output_tokens": raw_usage.get("completion_tokens")}
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("外部模型返回异常", status_code=502, code="PROVIDER_REJECTED", retryable=True) from exc
        if content is not None and not isinstance(content, str):
            raise ProviderError("外部模型返回的 JSON 正文格式无效", status_code=502,
                                code="INVALID_JSON_REPLY", retryable=True)
        if not (content or "").strip():
            if choice.get("finish_reason") == "length":
                raise ProviderError(
                    "外部模型把输出预算花在推理上、正文为空：请把后台任务模型换成非推理模型（ZHIJUN_OPENAI_TASK_MODEL）或提高预算",
                    status_code=502,
                    code="EMPTY_REPLY",
                    retryable=False,
                )
            raise ProviderError("外部模型返回空内容", status_code=502, code="EMPTY_REPLY", retryable=True)
        try:
            return parse_json_object(content)
        except ValueError as exc:
            raise ProviderError("外部模型返回的 JSON 正文无效", status_code=502,
                                code="INVALID_JSON_REPLY", retryable=True) from exc


# ---------------------------------------------------------------- Anthropic
class AnthropicProvider:
    """官方 SDK 适配器。本机策略禁止访问 anthropic.com 的环境下不应被构造。"""

    name = "anthropic"
    external = True

    def __init__(self, model: str, api_key: str, *, timeout: float, fallbacks: bool = True) -> None:
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._fallbacks = fallbacks
        self._client = None

    def _sdk(self):
        try:
            import anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise ProviderError("未安装 anthropic SDK", status_code=503, code="PROVIDER_MISCONFIGURED", retryable=False) from exc
        return anthropic

    def _get_client(self):
        if self._client is None:
            sdk = self._sdk()
            self._client = sdk.Anthropic(api_key=self._api_key, timeout=self._timeout, max_retries=1)
        return self._client

    def _wrap(self, exc: Exception) -> ProviderError:
        sdk = self._sdk()
        if isinstance(exc, sdk.RateLimitError):
            return ProviderError("Anthropic 限流，请稍后重试", status_code=429, code="PROVIDER_BUSY", retryable=True)
        if isinstance(exc, sdk.AuthenticationError):
            return ProviderError("Anthropic 鉴权失败", status_code=503, code="PROVIDER_MISCONFIGURED", retryable=False)
        if isinstance(exc, sdk.APIStatusError):
            retryable = exc.status_code >= 500
            return ProviderError(f"Anthropic 返回 {exc.status_code}", status_code=502, code="PROVIDER_REJECTED", retryable=retryable)
        if isinstance(exc, sdk.APIConnectionError):
            return ProviderError("Anthropic 连接失败", status_code=503, code="PROVIDER_UNAVAILABLE", retryable=True)
        return ProviderError(f"Anthropic 调用失败：{type(exc).__name__}", status_code=502, code="PROVIDER_UNAVAILABLE", retryable=True)

    def _kwargs(self, req: ChatRequest) -> dict:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": req.max_tokens,
            "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
            "messages": req.messages,
            "output_config": {"effort": req.effort or "low"},
        }
        if req.json_schema is not None:
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": req.json_schema}
        if self._fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return kwargs

    def stream(self, req: ChatRequest) -> Iterator[ChatEvent]:
        client = self._get_client()
        messages_api = client.beta.messages if self._fallbacks else client.messages
        try:
            with messages_api.stream(**self._kwargs(req)) as stream:
                for text in stream.text_stream:
                    yield TextDelta(text)
                final = stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc) from exc
        usage = getattr(final, "usage", None)
        yield Usage(getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
        yield Done(getattr(final, "stop_reason", None))

    def complete_json(self, req: ChatRequest) -> dict:
        client = self._get_client()
        messages_api = client.beta.messages if self._fallbacks else client.messages
        try:
            response = messages_api.create(**self._kwargs(req))
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc) from exc
        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderError("模型拒绝了这次抽取请求", status_code=422, code="REFUSAL", retryable=False)
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        return parse_json_object(text)


# ---------------------------------------------------------------- 选择
def _fake_allowed() -> bool:
    return os.environ.get("MINDOS_RUNTIME_ENV", "").strip().lower() != "production"


def build_provider(snapshot=None) -> ChatProvider:
    """按环境变量与设置页快照选择模型通道。

    - ``ZHIJUN_PROVIDER=fake``：演示模型（生产环境拒绝）。
    - ``ZHIJUN_PROVIDER=anthropic``：本机网络边界禁止，明确报错。
    - ``ZHIJUN_PROVIDER=openai`` 或设置页「外部问答」已开启且 provider=openai：OpenAI 兼容通道。
    - 其余：本地 Ollama（沿用材料通道快照的地址与模型）。
    """
    override = os.environ.get("ZHIJUN_PROVIDER", "").strip().lower()
    if override == "fake":
        if not _fake_allowed():
            raise ProviderError("演示模型不能在生产环境启用", status_code=503, code="FAKE_FORBIDDEN", retryable=False)
        return FakeProvider()
    snap = snapshot or get_provider().get_chat_snapshot()
    if override == "anthropic":
        raise ProviderError("本机禁止访问 Anthropic；请使用已配置的允许通道", code="SERVICE_FORBIDDEN", retryable=False)
    selected = bool(getattr(snap, "external_provider_id", None))
    use_openai = ((snap.provider == "openai" and snap.external_enabled) if selected
                  else (override == "openai" or (not override and snap.provider == "openai" and snap.external_enabled)))
    if use_openai:
        # 用户已选定的供应商必须整体生效，不能把新端点与旧环境变量密钥混用。
        # 没有已保存供应商时，仍保留联调 / 评测的环境变量兼容路径。
        base_url = snap.base_url if selected else (os.environ.get("ZHIJUN_OPENAI_BASE_URL", "").strip() or snap.base_url)
        model = snap.model if selected else (os.environ.get("ZHIJUN_OPENAI_MODEL", "").strip() or snap.model)
        key = get_provider().resolve_api_key(snap) if selected else (os.environ.get("ZHIJUN_OPENAI_API_KEY", "").strip() or get_provider().resolve_api_key(snap))
        if not base_url or not model or not key:
            raise ProviderError(
                "外部模型配置不完整：请在设置里填写 BaseURL、API Key 与 Model",
                status_code=503,
                code="PROVIDER_MISCONFIGURED",
                retryable=False,
            )
        try:
            timeout = float(os.environ.get("ZHIJUN_OPENAI_TIMEOUT", "") or snap.timeout_seconds)
        except ValueError:
            timeout = float(snap.timeout_seconds)
        task_model = None if selected else (os.environ.get("ZHIJUN_OPENAI_TASK_MODEL", "").strip() or None)
        thinking = os.environ.get("ZHIJUN_OPENAI_THINKING", "").strip() or None
        result = OpenAICompatibleProvider(base_url, model, key, timeout=timeout, task_model=task_model, thinking=thinking)
        # Internal-only identity lets the dispatch guard notice a saved account
        # change even when the endpoint and model stay identical. Never a token.
        result.configuration_revision = (snap.external_provider_id, snap.secret_ref) if selected else None
        return result
    local = snap.local
    try:
        num_ctx = int(os.environ.get("ZHIJUN_LOCAL_NUM_CTX", "") or DEFAULT_LOCAL_NUM_CTX)
    except ValueError:
        num_ctx = DEFAULT_LOCAL_NUM_CTX
    return OllamaProvider(local.base_url, local.model, timeout=float(local.timeout_seconds), keep_alive=local.keep_alive, num_ctx=num_ctx)


def provider_status() -> dict:
    """状态端点用：不发起网络请求，只报告将会使用的通道。"""
    try:
        provider = build_provider()
    except ProviderError as exc:
        return {"provider": os.environ.get("ZHIJUN_PROVIDER", "").strip().lower() or "ollama", "model": None, "external": False, "configured": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"provider": "unknown", "model": None, "external": False, "configured": False, "error": type(exc).__name__}
    return {"provider": provider.name, "model": provider.model, "external": bool(provider.external), "configured": True, "error": None}
