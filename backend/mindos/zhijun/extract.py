"""从用户原话抽取候选理解（Claim）。

入口规则（D2 的技术安全阀）：
- quote 必须是用户消息的精确子串，否则整条丢弃——「用户说了」≠「模型转述对了」。
- content ≤ 120 字；observed 不允许来自对话；aspirational / hypothesis 永远 working。
- self_declared 需要 quote 含第一人称，或紧接着助手的提问；当前整理流程只产生待确认候选。
- 校验保留旧接口上限 4 条；自动整理另做价值筛选，最多 1 条长期候选、2 条情境摘要片段。
- 去重：哈希或词面近似命中活跃理解 → 追加证据 + 刷新重申时间；旧 working 遇到用户再次亲口陈述 → 晋升 confirmed。
- 墓碑抑制：命中被撤回 / 被替代的理解 → 丢弃；用户本人再次陈述例外（新理解 supersedes 墓碑）。
抽取是独立用途；沿用统一路由逐次核对来源和外发授权。旧 persist 工具函数保留历史兼容行为。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace

from ..stores.ontology_store import (
    DEFAULT_PREDICATE,
    LAYERS,
    ME_ENTITY_ID,
    PREDICATES,
    SECTIONS,
    OntologyConflictError,
    OntologyError,
    OntologyStore,
    normalize_text,
)
from .provider import ChatProvider, ChatRequest, ProviderError
from .context_lookup import strip_citation_markers

logger = logging.getLogger(__name__)

MAX_CLAIMS_PER_TURN = 4
# 中文信息密度高：「我是产品经理」6 字已是完整自述；再短的（「好的」「嗯」）不值得调用模型。
MIN_TEXT_CHARS = 6
AUTO_CONFIRM_CONFIDENCE = 0.8
SIMILAR_THRESHOLD = 0.9

_FIRST_PERSON_RE = re.compile(r"我|咱|俺")
_ASPIRATION_RE = re.compile(r"想|希望|打算|目标|要成为|愿|计划")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；!?;\n]")


def _sentence_around(text: str, quote: str) -> str:
    """引用所在的整句：愿望词常在句首（「我想……，然后能把周末还给家里」），只看片段会误降层。"""
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        if quote and quote in sentence:
            return sentence
    return quote

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": list(SECTIONS)},
                    "layer": {"type": "string", "enum": ["self_declared", "aspirational", "hypothesis"]},
                    "predicate": {"type": "string"},
                    "subject": {"type": "string"},
                    "object": {"type": ["string", "null"]},
                    "content": {"type": "string"},
                    "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                    "scope_hint": {"type": "string", "enum": ["long_term", "context_only", "unknown"]},
                    "privacy_hint": {"type": "string", "enum": ["private", "sensitive"]},
                    "merge_into": {"type": ["string", "null"]},
                    "why_it_matters": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                },
                "required": ["section", "layer", "predicate", "subject", "object", "content", "quote", "confidence", "scope_hint", "privacy_hint", "merge_into", "why_it_matters", "date"],
                "additionalProperties": False,
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["person", "organization", "project", "place", "topic", "event", "term"]},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "type", "aliases"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims", "entities"],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM = """你是知君的记忆整理助手。任务：从「用户这一句话」里挑选少量有明确后续用途的内容，而不是把每句话都变成关于用户的理解，输出 JSON。

规则：
- 只抽用户亲口说的关于自己的事（self_declared）或明确表达的愿望、目标（aspirational）。本任务不输出 hypothesis；单次行为、模型复述、反复选择同一句候选都不是独立的长期模式证据。不要把资料或常识当作用户的事实。
- 每条理解：subject 用 "me" 表示用户本人，或写出具体人名 / 项目名；content 是一句 ≤ 60 字的原子陈述，用第一人称；quote 必须是用户原话里的一段精确文本（一字不改）。
- section 只能是：who（我是谁）、people（我的人）、matters（我的事）、principles（我的原则）、ways（我的做法）、direction（我的方向）。
- predicate 按分区选：who: is/has_trait/background/role；people: knows/works_with/relationship/attitude_toward；matters: working_on/committed_to/happened/owns；principles: holds_principle/boundary；ways: prefers/tends_to/decides_by；direction: wants_to/goal/avoids。
- 长期候选最多 1 条，情境片段最多 2 条。没有明确后续用途就返回空数组，不为填满数量而抽取。
- scope_hint=long_term 只用于用户明确说出的持续身份、重要关系、原则边界、长期目标或持续承担的项目；不能只因语气肯定或置信度高就认定长期有效。
- 问句中已经明确陈述的事实前提可以独立提取，例如「我负责研发，你建议怎么安排？」；不要把「我是不是适合负责研发？」当事实。每周、每个周末等重复安排不同于本周末、明天等一次安排。明确问题后的简短岗位、关系或原则答案可以结合上一问理解，quote 仍只引用用户原话；「好的／不知道／跳过」不是答案事实。
- 一次活动的目的、当天安排、临时筛选办法、对当前问题的补充，以及未说明有效范围的内容，用 context_only。它们只进入这段对话的可选摘要，不成为独立的长期记忆候选。例如「明天去黑客松找人才」「先看背景」「明天再看看作品」是同一件事的片段，不是三个稳定偏好。
- 用户一次「想／希望」可能只是眼下打算。必须区分「我希望三年后创办学校」这样的长期方向与「我想明天去现场看看」这样的情境安排。
- 如果某条与「已有的理解」说的是同一件事（换个说法、补一个细节），填 "merge_into": 那条的 id，不要新建。
- 每条加 "why_it_matters"：具体说明未来哪类帮助会因这条内容而改变；情境片段则说明它如何帮助当前这件事。不能只写「了解用户」「以后有帮助」「值得记住」，也不要重复 content；说不出具体用处就不抽。
- 用户明确说「想 / 希望 / 打算 / 目标是」的，是 aspirational（你想成为的），不要降成 hypothesis。
- entities 只收具体的人、组织、项目、地点、话题；不要把用户本人（他让你用的称呼）、「小组」「团队」「客户」这类泛指或临时团体当实体；公司 / 团队类用 organization。
- 用户提到期限（「三个月内」「下周五前」「年底」）时，把它换算成 ISO 日期填在 "date"（今天的日期在输入里给出）；没有就 null。
- entities 列出这句话里出现的人 / 组织 / 项目 / 地点名称。
输出格式：{"claims":[{"section":"...","layer":"...","predicate":"...","subject":"me","object":null,"content":"...","quote":"...","confidence":0.0-1.0,"scope_hint":"long_term|context_only|unknown","privacy_hint":"private|sensitive","merge_into":null,"why_it_matters":"...","date":null}],"entities":[{"name":"...","type":"person","aliases":[]}]}
只输出 JSON。"""

# 建档不是普通闲聊：每一问已经声明了它要建立的资料类型。模型仍负责把原话整理成
# 原子陈述，但不能把“未来方向”猜成当前事项、把“AI 边界”猜成用户身份。
_ONBOARDING_TARGETS: dict[int, tuple[str, str, str] | None] = {
    1: ("who", "self_declared", "role"),
    2: ("matters", "self_declared", "working_on"),
    3: ("people", "self_declared", "knows"),
    4: None,  # 一次具体选择进入判断草稿，不伪装成长期人格理解。
    5: ("principles", "self_declared", "holds_principle"),
    6: ("direction", "aspirational", "wants_to"),
    7: ("principles", "self_declared", "boundary"),
}


@dataclass
class ValidatedClaim:
    section: str
    layer: str
    predicate: str
    subject: str
    object: str | None
    content: str
    quote: str
    confidence: float
    scope: str
    privacy_level: str
    downgraded: bool = False
    merge_into: str | None = None
    why_it_matters: str = ""
    valid_to: str | None = None


_MEMORY_ACTION_RE = r"(?:记住|记下(?:来)?|记录下来|保存(?:一下|下来)?|存进(?:本体|记忆)|记到本体)"
_NO_MEMORY_RE = re.compile(r"(?:不要|不想|不必|不用|无需|不再|别|禁止).{0,12}" + _MEMORY_ACTION_RE + r"|(?:删除|撤回|忘掉|忘记).{0,12}(?:记忆|记录|理解|这条|这件事)")
_MEMORY_REQUEST_RE = re.compile(
    r"(?:^|[。！!?？；;\n，,])\s*(?:请|麻烦|劳驾)?\s*(?:你|知君)?\s*(?:帮我|替我|为我)?\s*(?:把[^。！？!?；;\n]{1,40})?" + _MEMORY_ACTION_RE
    + r"|(?:我希望你|我想让你|我需要你|我要求你|这点请|这条请|这件事请)\s*" + _MEMORY_ACTION_RE
)
_EPISODIC_RE = re.compile(
    r"今天|明天|后天|昨天|昨晚|今晚|这次|这一次|这场|当时|这件事|这一天|下周|本周|周末|"
    r"临时|暂时|刚刚|先(?:看看|试试|探探|了解|去|不|暂)|仅(?:在|限|用于)|只(?:在|用于|适用于)"
)
_RECURRING_RE = re.compile(r"每(?:个|逢)?(?:周末|星期[一二三四五六日天]?|周[一二三四五六日天]?|天|月|年|次)")
_STABLE_EXPRESSION_RE = re.compile(r"一直|长期|通常|一般|每次|一贯|原则|底线|绝不|始终|习惯|更看重|很看重|重视|对我.{0,12}(?:重要|看重)|我(?:更|最)?(?:在意|看重|喜欢|偏好|不接受|不能接受|不容忍)")
_IDENTITY_RE = re.compile(r"我(?:是|叫|的职业|的身份|的岗位)|(?:我在|我目前在|我现在在).{1,30}(?:做|担任|任职|工作|任(?=[A-Za-z]|总|副|主管|经理|董事|负责人|院长|校长))|我(?:目前|现在)?(?:担任|任职|负责)|叫我|称呼我")
_RELATIONSHIP_RE = re.compile(r"我(?:的)?(?:父|母|爸|妈|爱人|伴侣|丈夫|妻子|女儿|儿子|孩子|家人|合伙人|搭档|同事|朋友)|我和.{1,20}(?:一起|合作|共事|结婚|认识)|关系|合伙人|伴侣")
_ONGOING_RE = re.compile(r"我(?:目前|现在|正在|一直|主要)?(?:在做|在带|负责|承担|从事|分管|主管|管理)|长期|主业|主营|创业|项目负责人")
_LONG_GOAL_RE = re.compile(r"(?:未来|明年|年后|年底|几年|长期|人生|职业|目标|理想)|(?:成为|创办|转行|退休|定居|创业)")
_GENERIC_VALUE_RE = re.compile(r"^(?:这条|这个|该|此)?(?:信息|内容|理解|记录)?(?:有助于|帮助|便于|能够|可以|能)?(?:以后|未来|更好地|更好|进一步)?(?:了解用户|理解用户|了解他|理解他|提供帮助|个性化建议|个性化服务|做判断|对话|记住|有用|有帮助|很重要|值得记住)[。！!]*$")


def explicit_memory_request(user_text: str) -> bool:
    """Only a request to save counts; a discussion about remembering is not consent."""
    text = (user_text or "").strip()
    return bool(text and not _NO_MEMORY_RE.search(text) and not re.search(r"记住.{0,12}(?:是什么|什么意思|为什么|了吗|了什么)", text) and _MEMORY_REQUEST_RE.search(text))


def memory_request_declined(user_text: str) -> bool:
    """Respect a negative memory instruction before any automatic model call."""
    text = (user_text or "").strip()
    for match in _NO_MEMORY_RE.finditer(text):
        # Saving a file is a different operation from remembering a person.
        if "保存" in match.group(0) and re.match(r"(?:这[个份张]?|该|此)?(?:文件|图片|附件|文档|截图)", text[match.end():]):
            continue
        return True
    return False


def _specific_value(claim: ValidatedClaim) -> bool:
    reason = claim.why_it_matters.strip()
    normalized = normalize_text(reason)
    return bool(
        len(normalized) >= 6
        and not _GENERIC_VALUE_RE.fullmatch(reason)
        and normalized not in {normalize_text(claim.content), normalize_text(claim.quote)}
    )


_HYPOTHETICAL_RE = re.compile(r"(?:如果|假如|假设|设想|要是|倘若|万一|举例|比如|例如)")
_QUESTION_RE = re.compile(r"是不是|是否|会不会|能不能|要不要|该不该|适不适合|为什么|为何|怎么|如何|什么|哪[个位些种]|吗[呢呀啊]?$|[？?]")
_DIRECT_QUESTION_RE = re.compile(r"^\s*(?:那|所以|请问)?(?:我|我们|自己)?(?:到底|究竟|真的)?(?:是不是|是否|会不会|能不能|要不要|该不该|适不适合|为什么|为何|怎么|如何)")
_EMPTY_ANSWER_RE = re.compile(r"^(?:嗯+|哦+|啊+|好的?|是的?|对的?|不是|不对|可以|没错|谢谢|不用|都行|随便|不知道|不确定|没有|暂时没有|想不出|还不清楚|没想好|还没想好|跳过|先跳过|先不说|不想说|yes|no|ok)$", re.I)


def _answer_section(prev_assistant: str | None) -> str | None:
    """Only an explicit final question supplies a slot; not arbitrary chat context."""
    text = (prev_assistant or "").strip()
    if not text.endswith(("？", "?")):
        return None
    question = re.split(r"[。！？!?；;\n]", text[:-1])[-1]
    for section, pattern in (
        ("who", r"怎么称呼|如何称呼|叫什么|称呼你|你的.{0,4}(?:职业|岗位|身份|职位)|担任.{0,6}(?:职位|角色)|扮演.{0,10}角色"),
        ("people", r"(?:谁|哪些人).{0,10}(?:重要|在意)|(?:重要|在意).{0,8}(?:谁|哪些人)|你的(?:伴侣|女儿|儿子|家人)"),
        ("principles", r"原则|底线|边界|什么.{0,8}(?:最重要|最看重)"),
        ("ways", r"更喜欢|偏好|合作方式|希望我.{0,12}(?:帮助|配合|回应)"),
        ("direction", r"(?:长期|未来|几年|三年).{0,20}(?:目标|希望|想|成为)|想成为"),
        ("matters", r"(?:长期|目前|现在).{0,12}(?:负责|在做|承担)"),
    ):
        if re.search(pattern, question):
            return section
    return None


def _question_source(quote: str, user_text: str) -> bool:
    # A factual premise before a comma is not the question that follows it.
    # Conversely, a quote sliced out of a hypothetical remains hypothetical.
    needle = normalize_text(quote)
    for sentence, ending in re.findall(r"([^。！？!?；;\n]+)([。！？!?；;\n]|$)", user_text or ""):
        if needle not in normalize_text(sentence):
            continue
        through_quote = normalize_text(sentence).split(needle, 1)[0] + needle
        if _HYPOTHETICAL_RE.search(through_quote):
            return True
        parts = re.split(r"[，,]", sentence)
        for index, part in enumerate(parts):
            if needle in normalize_text(part):
                return bool(_DIRECT_QUESTION_RE.search(part) or (ending in ("？", "?") and
                            (_QUESTION_RE.search(part) or index == len(parts) - 1)))
        return bool(_DIRECT_QUESTION_RE.search(sentence) or ending in ("？", "?"))
    return bool(_DIRECT_QUESTION_RE.search(quote) or quote.rstrip().endswith(("？", "?")))


def _durable_expression(claim: ValidatedClaim, user_text: str, prev_assistant: str | None = None) -> bool:
    # Look at the source clause, never use the model's rewritten content as proof
    # of permanence. Short quotes still inherit time/context qualifiers nearby.
    clauses = re.split(r"[，,。！？；!?;\n]", user_text or "")
    clause = next((part for part in clauses if normalize_text(claim.quote) in normalize_text(part)), claim.quote)
    if _EPISODIC_RE.search(_RECURRING_RE.sub("", clause)):
        return False
    if (2 <= len(normalize_text(user_text)) < MIN_TEXT_CHARS and _answer_section(prev_assistant) == claim.section
            and not _EMPTY_ANSWER_RE.fullmatch(normalize_text(user_text))):
        return True
    if claim.section == "who":
        return bool(_IDENTITY_RE.search(clause) or _STABLE_EXPRESSION_RE.search(clause))
    if claim.section == "people":
        return bool(_RELATIONSHIP_RE.search(clause))
    if claim.section == "matters":
        return bool(_ONGOING_RE.search(clause))
    if claim.section in ("principles", "ways"):
        return bool(_STABLE_EXPRESSION_RE.search(clause) or _RECURRING_RE.search(clause))
    if claim.section == "direction":
        return bool(_LONG_GOAL_RE.search(clause) and _ASPIRATION_RE.search(clause))
    return False


def admission(
    valid: list[ValidatedClaim], user_text: str, input_origin: dict | None = None,
    *, prev_assistant: str | None = None,
) -> tuple[list[ValidatedClaim], list[ValidatedClaim]]:
    """Separate durable proposals from optional conversation-only summary fragments.

    This is deliberately stricter than schema validation. Extraction confidence
    says nothing about enduring personal value, and a selected AI answer is not
    independent evidence of a stable preference. Explicit requests still produce
    reviewable candidates, never formal confirmation.
    """
    long_term: list[ValidatedClaim] = []
    context: list[ValidatedClaim] = []
    seen: set[str] = set()
    for claim in valid:
        key = normalize_text(claim.content)
        if key in seen or claim.layer == "hypothesis" or _question_source(claim.quote, user_text) or not _specific_value(claim):
            continue
        seen.add(key)
        durable = claim.scope == "long_term" and not input_origin and _durable_expression(claim, user_text, prev_assistant)
        if durable:
            if not long_term:
                long_term.append(claim)
        elif len(context) < 2:
            context.append(replace(claim, scope="context_only"))
    return long_term, context


def should_extract(user_text: str, prev_assistant: str | None = None) -> tuple[bool, str]:
    text = (user_text or "").strip()
    if len(text) < MIN_TEXT_CHARS:
        if (len(normalize_text(text)) < 2 or not _answer_section(prev_assistant)
                or _EMPTY_ANSWER_RE.fullmatch(normalize_text(text)) or _question_source(text, text)):
            return False, "too_short"
    if not _FIRST_PERSON_RE.search(text) and text.rstrip().endswith(("？", "?")):
        return False, "pure_question"
    return True, "ok"


def build_request(
    user_text: str,
    prev_assistant: str | None,
    known_entities: list[str],
    *,
    existing_claims: list[dict] | None = None,
    debug: dict | None = None,
) -> ChatRequest:
    from datetime import date

    context_lines = [f"今天的日期：{date.today().isoformat()}"]
    if prev_assistant:
        context_lines.append(f"知君上一句话：{prev_assistant.strip()[-300:]}")
    if known_entities:
        context_lines.append("本次对话里已出现的名字：" + "、".join(known_entities[:20]))
    if existing_claims:
        context_lines.append("已有的理解（供去重与合并；同一件事请填 merge_into）：")
        for claim in existing_claims[:20]:
            context_lines.append(f"- {claim['id']}：{claim['content'][:60]}")
    context_lines.append(f"用户这一句话：{user_text.strip()}")
    system = _EXTRACT_SYSTEM
    # The guided flow now follows topics, not fixed turn numbers. Legacy callers
    # may still supply onboardingStep for diagnostics; it cannot relabel evidence.
    return ChatRequest(
        system=system,
        messages=[{"role": "user", "content": "\n".join(context_lines)}],
        max_tokens=800,
        temperature=0.0,
        json_schema=EXTRACTION_SCHEMA,
        effort="low",
        debug={**(debug or {}), "userText": user_text},
    )


def _quote_ok(quote: str, user_text: str) -> bool:
    quote = (quote or "").strip()
    if not quote or len(quote) > 300:
        return False
    if quote in user_text:
        return True
    return normalize_text(quote) != "" and normalize_text(quote) in normalize_text(user_text)


def _parse_date(value) -> str | None:
    """只接受 ISO 日期（模型已按输入里的今天换算）；返回 UTC Z 时间戳（当天 23:59）。"""
    if not value:
        return None
    from datetime import datetime, timezone

    text = str(value).strip()[:10]
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return day.replace(hour=23, minute=59, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


_SELF_NAME_RE = re.compile(r"(?:叫我|称呼我|喊我|我叫|我是)\s*([\u4e00-\u9fa5A-Za-z·]{1,8}?)(?:就行|就好|吧|即可|，|,|。|；|;|\s|$)")
_GENERIC_GROUP_RE = re.compile(r"^\d*\s*(?:人|个)?\s*(?:小组|团队|客户|同事|朋友|家人|员工|用户|公司)$")


def filter_entities(entities: list, *, user_text: str) -> list:
    """硬规则：用户本人的称呼（「叫我阿远」）和泛指团体（「5人小组」）不成为实体，模型提示词说了也常不听。"""
    self_names = {normalize_text(m.group(1)) for m in _SELF_NAME_RE.finditer(user_text or "") if m.group(1)}
    kept = []
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        if not name or normalize_text(name) in self_names or _GENERIC_GROUP_RE.match(name):
            continue
        kept.append(ent)
    return kept


def validate(raw: dict, *, user_text: str, prev_assistant: str | None, existing_ids: set[str] | None = None) -> list[ValidatedClaim]:
    items = raw.get("claims") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    prev_asked = bool(prev_assistant and prev_assistant.rstrip().endswith(("？", "?")))
    existing_ids = existing_ids or set()
    valid: list[ValidatedClaim] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        layer = item.get("layer")
        if section not in SECTIONS or layer not in LAYERS:
            continue
        if layer == "observed":
            continue  # 对话不产生资料观察
        content = str(item.get("content") or "").strip().replace("\n", " ")
        quote = str(item.get("quote") or "").strip()
        if not content or not _quote_ok(quote, user_text):
            continue
        if len(content) > 120:
            content = content[:120]
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        downgraded = False
        predicate = str(item.get("predicate") or "").strip()
        if predicate not in PREDICATES[section]:
            predicate = DEFAULT_PREDICATE[section]
            confidence = max(0.0, confidence - 0.1)
            downgraded = True
        if layer == "self_declared" and not (_FIRST_PERSON_RE.search(quote) or prev_asked):
            layer = "hypothesis"
            downgraded = True
        if layer == "aspirational" and not _ASPIRATION_RE.search(_sentence_around(user_text, quote)):
            layer = "self_declared" if _FIRST_PERSON_RE.search(quote) else "hypothesis"
            downgraded = True
        subject = str(item.get("subject") or "me").strip() or "me"
        obj = item.get("object")
        obj = str(obj).strip() if obj else None
        scope_hint = item.get("scope_hint") or "unknown"
        # Unknown duration is not evidence of an enduring trait.
        scope = "long_term" if scope_hint == "long_term" else "context_only"
        privacy = "sensitive" if item.get("privacy_hint") == "sensitive" else "private"
        merge_into = item.get("merge_into")
        merge_into = str(merge_into) if merge_into and str(merge_into) in existing_ids else None
        valid_to = _parse_date(item.get("date")) if predicate == "committed_to" else None
        valid.append(
            ValidatedClaim(
                section=section,
                layer=layer,
                predicate=predicate,
                subject=subject,
                object=obj,
                content=content,
                quote=quote,
                confidence=confidence,
                scope=scope,
                privacy_level=privacy,
                downgraded=downgraded,
                merge_into=merge_into,
                why_it_matters=str(item.get("why_it_matters") or "").strip()[:120],
                valid_to=valid_to,
            )
        )
    valid.sort(key=lambda c: c.confidence, reverse=True)
    return valid[:MAX_CLAIMS_PER_TURN]


def constrain_onboarding(valid: list[ValidatedClaim], step: int | None) -> list[ValidatedClaim]:
    """把建档答案限制在该问题承诺的数据类型中；第 4 问只进入判断草稿。"""
    if step not in _ONBOARDING_TARGETS:
        return valid
    target = _ONBOARDING_TARGETS[step]
    if target is None:
        return []
    section, layer, predicate = target
    for claim in valid:
        claim.section = section
        claim.layer = layer
        claim.predicate = predicate
        # 这是问题本身提供的语境，不是模型降级；仍需通过原话引用与置信度校验。
        claim.downgraded = False
    return valid


def _entity_id(store: OntologyStore, name: str, entity_types: dict[str, str], device_scope="global") -> str | None:
    norm = normalize_text(name)
    if not norm or norm in ("me", "我", "本人", "我自己", "用户"):
        return ME_ENTITY_ID
    try:
        entity = store.upsert_entity(name, entity_types.get(norm, "person"), device_scope=device_scope)
    except OntologyError:
        return None
    return entity["id"]


def persist(
    valid: list[ValidatedClaim],
    entities: list[dict],
    *,
    store: OntologyStore,
    conversation_id: str,
    message_id: str,
    routing_sources: list[dict] | None = None,
    input_origin: dict | None = None,
) -> dict:
    from ..stores.conversation_store import ConversationStore
    from .alignment import scope_for, visible
    conversations = ConversationStore.instance()
    device_scope = scope_for(conversation_id, conversations)
    entity_types: dict[str, str] = {}
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        etype = ent.get("type") if ent.get("type") in ("person", "organization", "project", "place", "topic", "event", "term") else "person"
        entity_types[normalize_text(name)] = etype
        try:
            store.upsert_entity(name, etype, aliases=[str(a) for a in (ent.get("aliases") or []) if a], device_scope=device_scope)
        except OntologyError:
            continue

    created: list[str] = []
    reaffirmed: list[str] = []
    promoted: list[str] = []
    suppressed = 0
    for claim in valid:
        subject_id = _entity_id(store, claim.subject, entity_types, device_scope)
        if subject_id is None:
            continue
        object_id = _entity_id(store, claim.object, entity_types, device_scope) if claim.object else None
        if object_id == subject_id:
            object_id = None
        auto_confirm = not input_origin and routing_sources is None and claim.layer == "self_declared" and claim.confidence >= AUTO_CONFIRM_CONFIDENCE and not claim.downgraded
        evidence = [{"kind": "conversation_turn", "conversation_id": conversation_id, "message_id": message_id, "quote": claim.quote}]
        if routing_sources is not None:
            evidence[0]["locator"] = {"routingSources": routing_sources, "localOnly": True}
        if input_origin:
            evidence[0].setdefault("locator", {})["replyAssistance"] = input_origin

        existing = None
        if claim.merge_into:
            existing = store.get_claim(claim.merge_into)
            if existing is not None and (existing["trustState"] not in ("working", "confirmed")
                    or (routing_sources is not None and not visible(existing, conversations, device_scope))
                    or existing.get("deviceScope", "global") != device_scope):
                existing = None
        if existing is None:
            existing = store.find_active_by_hash(subject_id, claim.predicate, claim.content, device_scope=device_scope) or store.find_similar_active(
                claim.content, threshold=SIMILAR_THRESHOLD, section=claim.section, device_scope=device_scope
            )
        if existing is not None and routing_sources is not None and not visible(existing, conversations, device_scope):
            existing = None
        if existing is not None:
            if routing_sources is not None or input_origin:
                # An interpretation never rewrites a formal record or its lineage.
                # The original user message remains available for explicit review.
                suppressed += 1
                continue
            store.add_evidence(existing["id"], evidence, reaffirm=True)
            reaffirmed.append(existing["id"])
            if existing["trustState"] == "working" and auto_confirm:
                try:
                    store.transition(
                        existing["id"],
                        "confirm",
                        surface="conversation",
                        conversation_id=conversation_id,
                        message_id=message_id,
                        note="用户再次亲口说到，视为确认",
                    )
                    promoted.append(existing["id"])
                except OntologyConflictError:
                    pass
            continue

        tombstone = store.find_tombstone_by_hash(subject_id, claim.predicate, claim.content, device_scope=device_scope)
        if tombstone is not None and not auto_confirm:
            suppressed += 1
            continue

        payload = {
            "subject_entity_id": subject_id,
            "device_scope": device_scope,
            "object_entity_id": object_id,
            "predicate": claim.predicate,
            "content": claim.content,
            "section": claim.section,
            "layer": claim.layer,
            "confidence": claim.confidence,
            "scope": claim.scope,
            "context_ref": conversation_id if claim.scope == "context_only" else None,
            "privacy_level": claim.privacy_level,
            "valid_to": claim.valid_to,
        }
        try:
            result = store.create_claim(
                payload,
                evidence,
                trust_state="confirmed" if auto_confirm else "working",
                trust_origin="utterance" if auto_confirm else "model",
                surface="conversation",
                conversation_id=conversation_id,
                message_id=message_id,
                supersedes_id=tombstone["id"] if tombstone else None,
                note=(("用户原话，抽取校验通过" if auto_confirm else "模型抽取的候选") + (f"；为何重要：{claim.why_it_matters}" if claim.why_it_matters else "")),
            )
        except OntologyConflictError:
            continue
        except OntologyError as exc:
            logger.debug("候选理解写入被拒：%s", exc)
            continue
        created.append(result["id"])
    return {"created": created, "reaffirmed": reaffirmed, "promoted": promoted, "suppressed": suppressed}


def run_extraction(
    *,
    provider: ChatProvider,
    store: OntologyStore,
    conversation_id: str,
    message_id: str,
    user_text: str,
    prev_assistant: str | None,
    debug: dict | None = None,
    input_origin: dict | None = None,
) -> dict:
    if input_origin and input_origin.get("kind") == "control":
        return {"state": "skipped", "reason": "conversation_control", "created": [], "reaffirmed": [], "promoted": [], "suppressed": 0}
    prev_assistant = strip_citation_markers(prev_assistant).strip()[-300:] if prev_assistant else None
    ok, reason = should_extract(user_text, prev_assistant)
    if not ok:
        return {"state": "skipped", "reason": reason, "created": [], "reaffirmed": [], "promoted": [], "suppressed": 0}
    known = store.entity_names_for_conversation(conversation_id)
    existing = store.list_claims(trust_states=("confirmed", "working"), limit=20)
    from .routing import GuardedProvider
    guarded = isinstance(provider, GuardedProvider)
    if guarded:
        # Extract the user's words, not a model's reconstruction of the whole profile.
        # Existing entities/claims are only used locally for deduplication at persistence.
        known, existing = [], []
        history = provider.router.convs.list_messages(conversation_id)
        previous = [m for m in history if m["role"] == "assistant" and m["seq"] < (provider.router.convs.get_message(message_id) or {}).get("seq", 0)]
        provider.refs = [provider.router.ref("message", message_id)]
        if prev_assistant and previous:
            provider.refs.append(provider.router.ref("message", previous[-1]["id"]))
    request = build_request(user_text, prev_assistant, known, existing_claims=existing, debug=debug)
    if input_origin:
        request = replace(request, system=request.system + "\n这段用户文字是 AI 候选辅助起草后发送，可能经用户修改。只能提出待核对的理解，不推断稳定人格或深层动机；不把选择候选当作独立自发证据。")
    raw = provider.complete_json(request)  # ProviderError 由 worker 分类
    valid = validate(raw, user_text=user_text, prev_assistant=prev_assistant, existing_ids={c["id"] for c in existing})
    entities = filter_entities(raw.get("entities") or [], user_text=user_text)
    sources = [s["ref"] for s in provider.last_preview["sources"]] if guarded else None
    from .memory import process_candidates
    if guarded:
        provider.assert_current()
    summary = process_candidates(valid, entities, store=store, conversation_id=conversation_id, message_id=message_id, user_text=user_text, routing_sources=sources, input_origin=input_origin, prev_assistant=prev_assistant)
    summary.update({"state": "done", "reason": "ok", "candidates": len(valid), "provider": provider.name})
    return summary


__all__ = [
    "EXTRACTION_SCHEMA",
    "MAX_CLAIMS_PER_TURN",
    "ProviderError",
    "ValidatedClaim",
    "admission",
    "build_request",
    "constrain_onboarding",
    "explicit_memory_request",
    "memory_request_declined",
    "persist",
    "run_extraction",
    "should_extract",
    "validate",
    "json",
]


FIRST_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": ["string", "null"]},
        "basis_claim_ids": {"type": "array", "items": {"type": "string"}},
        "section": {"type": "string", "enum": list(SECTIONS)},
        "question": {"type": ["string", "null"]},
    },
    "required": ["content", "basis_claim_ids", "section", "question"],
    "additionalProperties": False,
}

_FIRST_OBSERVATION_SYSTEM = """你是知君。建档刚结束，请基于用户已确认的理解给出「第一次观察」：恰好一条对他做事模式的推测。
规则：必须把至少两条已确认理解连起来（basis_claim_ids 填它们的 id），content 用第二人称写明依据（「你提到……和……，我猜你……」，≤ 80 字），section 选最贴切的分区，question 是一句邀请确认的话（以「对吗？」结尾）。依据不足就 content 填 null。只输出 JSON。"""


def first_observation(*, provider: ChatProvider, store: OntologyStore, conversation_id: str, message_id: str | None) -> dict:
    """建档收尾：一条【我推测的】工作理解，等用户点头。"""
    from .charter_policy import scope_policy, check_action
    from .alignment import scope_for
    from ..stores.conversation_store import ConversationStore
    policy = scope_policy(scope_for(conversation_id, ConversationStore.instance()))
    if not check_action(policy, "memory_auto")["allowed"]:
        return {"state": "skipped", "reason": "charter_memory_manual"}
    basis = store.list_claims(trust_states=("confirmed",), limit=12)
    from .routing import GuardedProvider
    guarded = isinstance(provider, GuardedProvider)
    if guarded:
        from .alignment import visible
        basis = [c for c in basis if visible(c, provider.router.convs, provider.router.scope)]
        provider.refs = [provider.router.ref("claim", c["id"]) for c in basis]
    if len(basis) < 2:
        return {"state": "skipped", "reason": "not_enough_basis"}
    lines = ["已确认的理解："] + [f"- {c['id']}：{c['content']}" for c in basis]
    request = ChatRequest(
        system=_FIRST_OBSERVATION_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        max_tokens=600,
        temperature=0.3,
        json_schema=FIRST_OBSERVATION_SCHEMA,
        effort="medium",
        debug={"task": "first_observation", "basisClaims": [{"id": c["id"], "content": c["content"]} for c in basis]},
    )
    raw = provider.complete_json(request)
    content = str(raw.get("content") or "").strip()[:120]
    ids = [str(i) for i in (raw.get("basis_claim_ids") or []) if str(i) in {c["id"] for c in basis}]
    if not content or len(ids) < 2:
        return {"state": "skipped", "reason": "no_observation"}
    section = raw.get("section") if raw.get("section") in SECTIONS else "ways"
    by_id = {c["id"]: c for c in basis}
    quote = "；".join(by_id[i]["content"] for i in ids)[:300]
    if guarded:
        provider.assert_current()
    from .charter_policy import assert_current
    assert_current(policy, scope_for(conversation_id, ConversationStore.instance()))
    try:
        claim = store.create_claim(
            {"subject_entity_id": ME_ENTITY_ID, "predicate": DEFAULT_PREDICATE[section], "content": content, "section": section, "layer": "hypothesis", "confidence": 0.5},
            [{"kind": "conversation_turn", "conversation_id": conversation_id, "message_id": message_id, "quote": quote,
              **({"locator": {"routingSources": [s["ref"] for s in provider.last_preview["sources"]], "localOnly": True}} if guarded else {})}],
            trust_state="working",
            trust_origin="model",
            surface="onboarding",
            conversation_id=conversation_id,
            message_id=message_id,
            note="建档收尾的第一次观察；依据：" + "，".join(ids),
        )
    except (OntologyConflictError, OntologyError) as exc:
        return {"state": "skipped", "reason": str(exc)[:80]}
    return {"state": "done", "claimId": claim["id"], "question": str(raw.get("question") or "")[:120]}
