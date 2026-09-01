"""Agent API Pydantic 请求/响应模型（AG-01）。

请求/响应统一走 {traceId, data} / {traceId, error} 信封；所有资源 ID 使用
既有 materialId / knowledgeId / jobId，不返回服务器绝对路径。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False


class AgentErrorEnvelope(BaseModel):
    traceId: str
    error: AgentErrorBody


class AgentDataEnvelope(BaseModel):
    traceId: str
    data: dict


class CapabilitiesData(BaseModel):
    apiVersion: str = "v1"
    workspaceId: str = "default"
    tools: list[str] = []
    writeModes: dict = {}
    limits: dict = {}
    supportedFileTypes: list[str] = []


class AnswerOptions(BaseModel):
    """AG-03 问答选项（强类型；拒绝未知字段，防止注入模型参数/提示词指令）。

    - sourceIds：检索范围限定（≤20 个，服务端只在该范围内检索，不能绕过可见性）；
    - maxEvidence：证据数量上限（1–6，服务端执行，禁止无限扩大）；
    - includeEvidence：false 时隐藏完整片段，但保留 citation 元数据 / evidenceRef /
      locator。
    """

    model_config = ConfigDict(extra="forbid")
    sourceIds: list[str] = Field(default_factory=list, max_length=20)
    maxEvidence: Optional[int] = Field(default=None, ge=1, le=6)  # ≤ qa.MAX_EVIDENCE
    includeEvidence: bool = True


class AnswerRequest(BaseModel):
    """AG-03 带引用的问答请求；options 为强类型问答选项。"""

    question: str = Field(..., min_length=1)
    options: Optional[AnswerOptions] = None


class SearchInclude(BaseModel):
    """AG-02-02 搜索响应内容开关。"""

    snippet: bool = True
    locator: bool = True


class SearchRequest(BaseModel):
    """AG-02-02 统一搜索请求。

    query 2–500 字、limit ≤ capabilities 声明的上限、types 只允许
    knowledge/material、sourceIds 最多 20 个、cursor 使用服务端签发游标。
    业务校验在 agent/search_service.py::_validate 执行（不落入 router.py）。
    """

    query: str = Field(..., min_length=1)
    types: Optional[list[str]] = None
    limit: int = Field(default=10, ge=1)
    cursor: Optional[str] = None
    sourceIds: list[str] = Field(default_factory=list)
    include: Optional[SearchInclude] = None


class EvidenceResolveRequest(BaseModel):
    """AG-02-03 证据展开请求。

    一次最多 10 个 ref；maxCharsPerItem 最大 3000（超出/总数超限在
    agent/evidence.py::resolve_evidence_batch 统一处理）。
    """

    evidenceRefs: list[str] = Field(..., min_length=1)
    maxCharsPerItem: int = Field(default=3000, ge=1)
    includeLocator: bool = True
