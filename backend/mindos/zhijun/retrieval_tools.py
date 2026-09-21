"""Deterministic query planning for the single read-only material tool.

This module deliberately does not ask a model to invent search terms.  Its
history input has already passed the caller's authorization checks; even then,
only an earlier user message may disambiguate a genuinely dependent follow-up.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fastapi import HTTPException


MAX_QUERY_CHARS = 1000
MAX_HISTORY_CHARS = 400
MAX_MATERIAL_IDS = 100
TOP_K = 5

NATIVE_MODEL_TOOLS_SUPPORTED = False
# Lower-case alias is intentional: callers can expose this value directly in a
# capability response without implying that providers execute native tools.
native_model_tools_supported = NATIVE_MODEL_TOOLS_SUPPORTED

_URL_RE = re.compile(r"(?:https?|ftp|file)://", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_CREDENTIAL_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret)"
    r"\s*[:=]\s*[^\s,;]{4,}",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[^\s]{4,}", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MATERIAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_INTERACTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

_TOPIC_SWITCH_RE = re.compile(
    r"换(?:个|一个)话题|换句话题|另外(?:问|一个问题)|另一个问题|顺便问|"
    r"不说这个|先不谈|与(?:上面|前面|刚才|此)无关|new topic",
    re.IGNORECASE,
)
_SLOT_FOLLOW_UP_RE = re.compile(
    r"^(?:那|那么|这个|该项|该项目|这项)?\s*(?:的)?\s*"
    r"(?:验收(?:\s*requirements?|要求)?|requirements?|风险|依赖|条件|步骤|进度|结果|范围|"
    r"性能|成本|时间|原因|负责人|下一步)"
    r"\s*(?:呢|是什么|有哪些|怎么做|如何|怎么样)?[?？。！!\s]*$",
    re.IGNORECASE,
)
_SHORT_FOLLOW_UP_RE = re.compile(
    r"^(?:具体(?:呢|点|一点)?|再具体一点|展开(?:说说)?|继续|接着|"
    r"还有(?:呢|吗|哪些)?|然后呢|结果呢)[?？。！!\s]*$",
    re.IGNORECASE,
)
# A concrete ticket/device/build identifier is a safer topic anchor than an
# earlier conversation.  Do not append an unrelated previous subject to it.
_IDENTIFIER_RE = re.compile(
    r"\b(?:[A-Za-z]{2,}[A-Za-z0-9]*[-_:][A-Za-z0-9][A-Za-z0-9._:-]*|"
    r"[A-Za-z]+\d{2,}[A-Za-z0-9._:-]*)\b"
)
_NAMED_IDENTIFIER_RE = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*[-_:][A-Za-z0-9][A-Za-z0-9._:-]*|"
    r"[A-Za-z]*[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*|[A-Za-z]+OS)\b"
)
_SUBJECT_SUFFIX_RE = re.compile(
    r"(?:项目|方案|计划|报告|合同|文档|材料|文件|手册|记录|产品|系统|平台|"
    r"模型|服务|组件|模块|设备|盒子|Remote Agent)(?:的(?:升级|发布|部署|验收)方案)?$",
    re.IGNORECASE,
)
_QUESTION_SUBJECT_RE = re.compile(
    r"^(?P<subject>.{1,120}?)\s*(?:是(?:什么|谁)|有(?:哪些|什么)|包括(?:哪些|什么)|"
    r"能做什么|有哪些核心功能|的(?:负责人|风险|要求|依赖|进度|结果)(?:是|有|包括)?)",
    re.IGNORECASE,
)
_LEADING_REFERENCE_RE = re.compile(
    r"^(?:关于\s*)?(?:这个方案|该方案|这套方案|这个项目|该项目|这项工作|"
    r"这个系统|该系统|这个产品|该产品|这个服务|该服务|这个模型|该模型|"
    r"这个组件|该组件|这个设备|该设备|这个盒子|该盒子|"
    r"这个材料|这份材料|这份文档|这个文件|这份文件|这些材料|这些文档|"
    r"它|这个|这项|这件事|上述|前述|上面(?:提到的)?|刚才(?:提到的)?)",
    re.IGNORECASE,
)
_SELECTED_MATERIAL_RE = re.compile(
    r"(?:这|该)(?:一|两|几)?(?:份|个)?(?P<label>[^，,。！？?\s]{0,12}?)"
    r"(?P<kind>材料|文档|文件|报告|记录|附件)"
)
_AMBIGUOUS_SUBJECT_RE = re.compile(
    r"(?:、|以及|或者|或是)|.{1,60}(?:和|与|及).{1,60}(?:区别|差异|比较|对比|各自|分别)|"
    r"(?:项目|方案|计划|报告|合同|文档|材料|文件|系统|平台).{0,10}(?:和|与|及)"
    r".{0,60}(?:项目|方案|计划|报告|合同|文档|材料|文件|系统|平台)",
    re.IGNORECASE,
)
_LEADING_REQUEST_RE = re.compile(
    r"^(?:请问|请|帮我|麻烦|介绍(?:一下)?|说明(?:一下)?|解释(?:一下)?|"
    r"查找|检索|搜索|关于)\s*",
    re.IGNORECASE,
)
_LEADING_POLITE_RE = re.compile(r"^(?:请问|请|麻烦(?:你)?|帮我)\s*", re.IGNORECASE)
_LEADING_SEARCH_RE = re.compile(
    r"^(?:帮我\s*)?(?:查找|检索|搜索|查询|查一下)(?:一下)?\s*[,，:：]?\s*",
    re.IGNORECASE,
)
_LEADING_MATERIAL_ACTION_RE = re.compile(
    r"^(?:梳理|复现|还原|阅读|读取|总结|分析)(?:一下)?\s*", re.IGNORECASE,
)
_FILENAME_RE = re.compile(
    r"[^\s/\\《》“”\"，,。！？?：:；;]{1,200}\.(?:docx?|pdf|pptx?|xlsx?|txt|md|csv)"
    r"(?=$|[\s》”\"，,。！？?：:；;的中里内])", re.IGNORECASE,
)


def should_search_materials(content: str) -> bool:
    """Recognize concrete file/project requests even without the word '资料'.

    This only opens Search and review, never authorizes result use. Generic
    project creation and discussion do not inspect the material library.
    """
    if _FILENAME_RE.search(content):
        return True
    value = _planning_view(content)
    action = re.search(r"梳理|复现|还原", content)
    return bool(action and re.search(r"[^\s，,。！？?]{2,100}项目", value)
                and not re.search(r"(?:一个|某个|什么|新)项目", value))


SEARCH_MATERIALS_TOOL = {
    "name": "search_materials",
    "description": "只读检索当前应用已授权的资料；不接受网址、凭据或任意工具参数。",
    "readOnly": True,
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
            "materialIds": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_MATERIAL_IDS,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        },
    },
}


def _unprocessable(code: str, detail: str) -> HTTPException:
    return HTTPException(422, {"code": code, "detail": detail})


def _query(value: Any) -> str:
    if not isinstance(value, str):
        raise _unprocessable("RAG_QUERY_INVALID", "检索问题必须是文字")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        raise _unprocessable("RAG_QUERY_EMPTY", "请补充需要检索的问题")
    if len(value) > MAX_QUERY_CHARS:
        raise _unprocessable(
            "RAG_QUERY_TOO_LONG",
            "检索问题超过 1000 个字符，请在界面中缩短后重试",
        )
    if _CONTROL_RE.search(value):
        raise _unprocessable("RAG_QUERY_INVALID", "检索问题包含不支持的控制字符")
    if (_URL_RE.search(value) or _PRIVATE_KEY_RE.search(value)
            or _CREDENTIAL_RE.search(value) or _BEARER_RE.search(value)):
        raise _unprocessable("RAG_QUERY_UNSAFE", "检索问题不能包含网址或凭据")
    return value


def _material_ids(value: Any, *, supplied: bool) -> list[str] | None:
    if not supplied or value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _unprocessable("RAG_MATERIAL_SCOPE_INVALID", "资料范围必须是资料编号列表")
    result = list(value)
    if not result:
        # Empty is not the same as omitted: widening it to the whole authorized
        # library would violate the user's explicit scope.
        raise _unprocessable("RAG_MATERIAL_SCOPE_EMPTY", "已选择的资料范围为空，请重新选择")
    if len(result) > MAX_MATERIAL_IDS:
        raise _unprocessable("RAG_MATERIAL_SCOPE_TOO_LARGE", "一次最多选择 100 份资料")
    if any(not isinstance(item, str) or _MATERIAL_ID_RE.fullmatch(item) is None for item in result):
        raise _unprocessable("RAG_MATERIAL_ID_INVALID", "资料编号格式无效")
    if len(set(result)) != len(result):
        raise _unprocessable("RAG_MATERIAL_SCOPE_DUPLICATE", "资料范围不能包含重复编号")
    return result


def validate_search_arguments(arguments: Any) -> dict[str, Any]:
    """Validate the entire fixed tool input; unknown fields are never ignored."""
    if not isinstance(arguments, Mapping):
        raise _unprocessable("RAG_TOOL_ARGUMENTS_INVALID", "检索工具参数必须是对象")
    allowed = {"query", "materialIds"}
    if "query" not in arguments or set(arguments) - allowed:
        raise _unprocessable("RAG_TOOL_ARGUMENTS_INVALID", "检索工具只接受 query 和 materialIds")
    query = _query(arguments["query"])
    material_ids = _material_ids(
        arguments.get("materialIds"), supplied="materialIds" in arguments
    )
    return {"query": query, "materialIds": material_ids}


def _dependent_follow_up(content: str) -> bool:
    if _TOPIC_SWITCH_RE.search(content) or _IDENTIFIER_RE.search(content):
        return False
    compact = re.sub(r"\s+", "", content)
    if len(compact) > 120:
        return False
    return bool(
        _LEADING_REFERENCE_RE.match(content.strip())
        or _SLOT_FOLLOW_UP_RE.fullmatch(content.strip())
        or _SHORT_FOLLOW_UP_RE.fullmatch(content.strip())
    )


def _planning_view(content: str, *, keep_action: bool = False) -> str:
    """Remove request framing only for dependency analysis and rewriting.

    A self-contained query is returned to the caller verbatim.  This view is
    used only when the remaining text proves that the query is referential.
    """
    value = content.strip()
    value = _LEADING_POLITE_RE.sub("", value, count=1)
    value = _LEADING_POLITE_RE.sub("", value, count=1)
    value = _LEADING_SEARCH_RE.sub("", value, count=1)
    if not keep_action:
        value = _LEADING_MATERIAL_ACTION_RE.sub("", value, count=1)
    return value.strip()


def _clarification() -> HTTPException:
    return _unprocessable(
        "RAG_QUERY_CLARIFICATION_REQUIRED",
        "当前问题缺少可确定的材料或主题，请明确写出要检索的材料、项目或对象后重试",
    )


def _subject_candidate(content: str) -> str | None:
    """Extract one explicit user-named topic, never a summary of their message."""
    value = content.strip().strip("。！？!? ")
    if (not value or "\n" in value or ";" in value or "；" in value
            or re.search(r"[。！？!?]", value)):
        return None
    if _TOPIC_SWITCH_RE.search(value) or _AMBIGUOUS_SUBJECT_RE.search(value):
        return None
    quoted = re.findall(r"[《“\"]([^》”\"]{1,120})[》”\"]", value)
    if len(quoted) == 1:
        return quoted[0].strip()
    if len(quoted) > 1:
        return None
    cleaned = _LEADING_REQUEST_RE.sub("", _planning_view(value)).strip()
    filenames = _FILENAME_RE.findall(cleaned)
    if len(filenames) == 1:
        return filenames[0]
    if len(filenames) > 1:
        return None
    match = _QUESTION_SUBJECT_RE.match(cleaned)
    if match:
        candidate = match.group("subject").strip(" ，,的")
        return candidate or None
    identifiers = list(dict.fromkeys(_NAMED_IDENTIFIER_RE.findall(cleaned)))
    if len(identifiers) == 1:
        return identifiers[0]
    named_noun = re.match(
        r"^(?P<subject>.{1,100}?(?:项目|方案|计划|报告|合同|文档|材料|文件|手册|记录|"
        r"产品|系统|平台|模型|服务|组件|模块|设备|盒子))"
        r"(?:(?:的.{1,40})|(?:(?:中|里|内)?(?:写了|写着|提到|显示|包含|包括|讲了|说明了).{0,40}))?$",
        cleaned,
        re.IGNORECASE,
    )
    if named_noun:
        return named_noun.group("subject").strip()
    if len(identifiers) > 1:
        # A noun phrase such as "家庭盒 Remote Agent 的升级方案" remains one
        # explicit topic despite containing more than one English word.
        return cleaned if len(cleaned) <= 120 and _SUBJECT_SUFFIX_RE.search(cleaned) else None
    if len(cleaned) <= 120 and _SUBJECT_SUFFIX_RE.search(cleaned):
        return cleaned
    return None


def _history_subject(allowed_history: Iterable[Any]) -> tuple[str, str] | None:
    messages = list(allowed_history)[-12:]
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        ident, content = message.get("id"), message.get("content")
        if (not isinstance(ident, str) or not ident or len(ident) > 128
                or not isinstance(content, str)):
            continue
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            continue
        if len(content) > MAX_HISTORY_CHARS or _TOPIC_SWITCH_RE.search(content):
            return None
        if _dependent_follow_up(content):
            # A chain of short user follow-ups may still have one earlier,
            # explicit user anchor. Assistant prose is never such an anchor.
            continue
        try:
            content = _query(content)
        except HTTPException:
            return None
        subject = _subject_candidate(content)
        return (ident, subject) if subject else None
    return None


def _rewrite_follow_up(current: str, subject: str) -> str:
    slot = _SLOT_FOLLOW_UP_RE.fullmatch(current.strip())
    if slot:
        text = current.strip().strip("?？。！! ")
        text = re.sub(r"^(?:那|那么|这个|该项|该项目|这项)?\s*(?:的)?\s*", "", text)
        text = re.sub(r"(?:呢|是什么|有哪些|怎么做|如何|怎么样)$", "", text).strip()
        return f"{subject}的{text}是什么？"
    if _SHORT_FOLLOW_UP_RE.fullmatch(current.strip()):
        if re.fullmatch(r"(?:具体(?:呢|点|一点)?|再具体一点|展开(?:说说)?|继续|接着)[?？。！!\s]*", current):
            return f"详细说明{subject}"
        if re.fullmatch(r"还有(?:呢|吗|哪些)?[?？。！!\s]*", current):
            return f"{subject}还有哪些相关内容？"
        if re.fullmatch(r"(?:然后呢|结果呢)[?？。！!\s]*", current):
            return f"{subject}的后续结果是什么？"
    rewritten, count = _LEADING_REFERENCE_RE.subn(subject, current, count=1)
    if count == 1 and not re.search(
        r"它|这个|那个|上述|前述|上面(?:提到的)?|刚才(?:提到的)?",
        rewritten[len(subject):],
    ):
        return rewritten
    raise _clarification()


# 注：`plan_search` 目前没有调用方——它原本服务于已删除的盒端 RAG 通路。
# 刻意保留而不是一起删掉：它是纯粹的查询改写（把依赖上文的追问重写成独立查询、
# 从历史里解析指代），不依赖盒子，也有二十多条用例钉着。独立版的资料检索现在走
# `qa.build_evidence` 的原始查询，接上这层是后面该做的事，不是现在该删的东西。
def plan_search(
    content: str,
    allowed_history: Iterable[Mapping[str, Any]] = (),
    material_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Plan one bounded search while keeping the user's current request whole."""
    current = _query(content)
    planning = _planning_view(current)
    if not planning:
        raise _clarification()
    selected = _material_ids(material_ids, supplied=material_ids is not None)
    query = current
    history_used: list[str] = []
    selected_query, selected_count = _SELECTED_MATERIAL_RE.subn(
        lambda match: "所选" + match.group("label") + match.group("kind"), planning
    ) if selected is not None else (planning, 0)
    if selected_count:
        if ((re.search(r"(?:这(?:一)?份|这个|该份|该个).{0,12}(?:材料|文档|文件|报告|记录|附件)", planning)
             and len(selected) != 1)
                or (re.search(r"这两份", planning) and len(selected) != 2)):
            raise _clarification()
        query = selected_query
    else:
        topic_switch = _TOPIC_SWITCH_RE.search(planning)
        remainder = planning[topic_switch.end():].lstrip("，,。:： ") if topic_switch else ""
        if topic_switch and _LEADING_REFERENCE_RE.match(remainder):
            raise _clarification()
    if _dependent_follow_up(planning) and not selected_count:
        if selected is not None and _SLOT_FOLLOW_UP_RE.fullmatch(planning):
            query = _rewrite_follow_up(planning, "所选材料")
        else:
            previous = _history_subject(allowed_history)
            if previous is None:
                raise _clarification()
            ident, subject = previous
            query = _rewrite_follow_up(planning, subject)
            history_used.append(ident)
    action = _LEADING_MATERIAL_ACTION_RE.match(_planning_view(current, keep_action=True))
    if action and query != current:
        query = action.group(0) + query
    query = _query(query)
    return {
        "query": query,
        "materialIds": selected,
        "topK": TOP_K,
        "scopeLabel": "授权资料库" if selected is None else "所选资料",
        "historyUsed": history_used,
    }


def _validated_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or set(plan) != {
        "query", "materialIds", "topK", "scopeLabel", "historyUsed"
    }:
        raise _unprocessable("RAG_SEARCH_PLAN_INVALID", "检索计划字段无效")
    values = validate_search_arguments({
        "query": plan["query"],
        **({"materialIds": plan["materialIds"]} if plan["materialIds"] is not None else {}),
    })
    expected_label = "授权资料库" if values["materialIds"] is None else "所选资料"
    if plan["topK"] != TOP_K or plan["scopeLabel"] != expected_label:
        raise _unprocessable("RAG_SEARCH_PLAN_INVALID", "检索计划包含可变执行参数")
    history = plan["historyUsed"]
    if (not isinstance(history, list) or len(history) > 1
            or any(not isinstance(item, str) or not item or len(item) > 128 for item in history)):
        raise _unprocessable("RAG_SEARCH_PLAN_INVALID", "检索计划的历史依据无效")
    return {**values, "topK": TOP_K, "scopeLabel": expected_label, "historyUsed": list(history)}


