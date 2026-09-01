"""外部 Agent Gateway 配置（AG-01）。

所有限制项均配置化，禁止写死在 handler 中；capabilities 返回的 limits
与限流默认值都以本模块为唯一事实来源。
"""
from __future__ import annotations

import json
import os


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def gateway_enabled() -> bool:
    """总开关（默认拒绝）。

    未配置 MINDOS_AGENT_GATEWAY_ENABLED 时一律关闭，/v1/agent/* 对任何
    请求（含有效凭证）返回 403/POLICY_DENIED。开发、测试与正式部署都必须
    显式设置 MINDOS_AGENT_GATEWAY_ENABLED=true 才对外提供能力。
    每次调用读取环境变量，便于部署期启停，也便于测试验证「环境变量缺失」。
    """
    return _env_bool("MINDOS_AGENT_GATEWAY_ENABLED", "false")


# V1 固定单工作区；保留概念以便未来多工作区改造无需重做 URL 与审计模型。
# 多工作区未实现前，任何客户端都不能自报 workspace，统一使用本值。
WORKSPACE_ID = os.getenv("MINDOS_AGENT_WORKSPACE_ID", "default")

# ---- capabilities.limits：内容上限（AG-01 起生效）----
SEARCH_PAGE_SIZE_MAX = int(os.getenv("MINDOS_AGENT_SEARCH_PAGE_SIZE_MAX", "20"))
EVIDENCE_CHARS_MAX = int(os.getenv("MINDOS_AGENT_EVIDENCE_CHARS_MAX", "12000"))
ANSWER_QUESTION_CHARS_MAX = int(os.getenv("MINDOS_AGENT_ANSWER_QUESTION_CHARS_MAX", "500"))

# ---- 限流默认值（请求/分钟/client；AG-07 将完善并发与时长维度）----
RATE_LIMITS_PER_MINUTE = {
    "capabilities": 120,
    "detail": 120,
    "search": 60,
    "evidence": 60,
    "answer": 10,
    "import": 10,
    "knowledge_draft": 30,
}


def rate_limits() -> dict:
    """返回生效限流配置。

    支持通过环境变量 MINDOS_AGENT_RATE_LIMITS_JSON 覆盖（JSON 对象，值为非负整数）。
    严格校验约定：
    - 仅接受 RATE_LIMITS_PER_MINUTE 中已知 action；
    - 仅接受大于等于 0 的整数（0 明确约定为「完全禁止」该动作）；
    - 出现任何非法项（未知 action / 负数 / 非整数 / 布尔值）时，整体回退到默认配置；
    - 覆盖后未列出的 action 保持默认值。
    """
    raw = os.getenv("MINDOS_AGENT_RATE_LIMITS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("限流配置必须是 JSON 对象")
            cleaned: dict = {}
            for key, value in parsed.items():
                if key not in RATE_LIMITS_PER_MINUTE:
                    raise ValueError(f"未知限流 action: {key}")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} 必须是非负整数")
                if int(value) != value or value < 0:
                    raise ValueError(f"{key} 必须是非负整数")
                cleaned[key] = int(value)
            return {**RATE_LIMITS_PER_MINUTE, **cleaned}
        except (ValueError, TypeError):
            pass
    return dict(RATE_LIMITS_PER_MINUTE)
