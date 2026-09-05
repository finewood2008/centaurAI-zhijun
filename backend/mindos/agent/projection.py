"""将内部检索结果投影为 Agent 安全响应（AG-02-02）。

内部 SearchHit 含 source_path / chunk_id / score 等仅供服务端使用的字段，
本模块负责投影为外部 Agent 可见的安全字段：

- source_path、chunk_id、score 一律不返回；
- chunk/path 转换为 opaque、短期、client 绑定的 evidenceRef（证据句柄）；
- 只保留允许的业务定位字段（locator：表格/音频转写/图片，无法确认时为 None）；
- 任意结果全文不得出现本地路径、Wiki path、Chroma collection 名称等内部信息。
"""
from __future__ import annotations

from . import evidence
from ..services import search_service

# 外部响应中禁止出现的内部路径/标识片段（防御性兜底，防止未来字段泄漏）。
_BANNED_SUBSTRINGS = (
    "source_path",
    "D:\\",
    "/data/",
    "watch_folder",
    ".mindos_uploads",
    "chroma",
    ".wikis/",
)

# 服务端统一截断上限：搜索片段由服务端固定控制，不允许请求参数无限扩大。
_SNIPPET_CHARS_MAX = 700
_TITLE_CHARS_MAX = 300


def _truncate(text: str, limit: int) -> str:
    return str(text or "").strip()[:limit]


def _safe(text: str) -> str:
    """对投影字段做最终脱敏，任何残留的内部路径片段都视为空。"""
    text = str(text or "").strip()
    lowered = text.casefold()
    if any(banned.casefold() in lowered for banned in _BANNED_SUBSTRINGS):
        return ""
    return text


def project_search_item(hit: dict, *, client_id: str, include_locator: bool = True) -> dict:
    """内部 SearchHit → Agent 搜索结果条目。

    include_locator=False 时 locator 恒为 None（调用方请求不返回定位）。
    """
    source_type = str(hit.get("source_type") or "")
    if source_type == "material":
        return _project_material(hit, client_id=client_id, include_locator=include_locator)
    return _project_knowledge(hit, client_id=client_id, include_locator=include_locator)


def _project_material(hit: dict, *, client_id: str, include_locator: bool) -> dict:
    snippet = _truncate(hit.get("snippet") or "", _SNIPPET_CHARS_MAX)
    eligible = bool(hit.get("evidence_eligible"))
    evidence_ref = None
    # 有可定位 chunk 的材料才签发证据句柄；AG-02-03 resolve 时用其读取有限正文。
    if eligible and hit.get("chunk_id"):
        evidence_ref = evidence.sign_evidence_ref(
            client_id=client_id,
            source_type="material",
            source_id=str(hit.get("source_id") or ""),
            chunk_key=str(hit.get("chunk_id") or ""),
            source_path=str(hit.get("source_path") or ""),
            title=str(hit.get("title") or ""),
        )
    item = {
        "sourceType": "material",
        "id": str(hit.get("source_id") or ""),
        "title": _safe(_truncate(hit.get("title") or "", _TITLE_CHARS_MAX)),
        "fileType": str(hit.get("file_type") or "text"),
        "snippet": _safe(snippet),
        "evidenceRef": evidence_ref,
        "evidenceEligible": eligible,
        # 搜索命中直接携带真实定位（共享检索服务填充）；无法确认时为 None。
        "locator": hit.get("locator") if include_locator else None,
    }
    return item


def _project_knowledge(hit: dict, *, client_id: str, include_locator: bool) -> dict:
    snippet = _truncate(hit.get("snippet") or "", _SNIPPET_CHARS_MAX)
    eligible = bool(hit.get("evidence_eligible"))
    evidence_ref = None
    if eligible:
        evidence_ref = evidence.sign_evidence_ref(
            client_id=client_id,
            source_type="knowledge",
            source_id=str(hit.get("source_id") or ""),
            chunk_key=None,
            source_path=None,
            title=str(hit.get("title") or ""),
        )
    item = {
        "sourceType": "knowledge",
        "id": str(hit.get("source_id") or ""),
        "title": _safe(_truncate(hit.get("title") or "", _TITLE_CHARS_MAX)),
        "snippet": _safe(snippet),
        "evidenceRef": evidence_ref,
        "evidenceEligible": eligible,
        "locator": hit.get("locator") if include_locator else None,
    }
    return item


# ---- 材料 / 知识卡片详情投影（AG-02-04）-----------------------------------

def _project_entities(entities: dict | None) -> list[dict]:
    """实体投影：仅派生记录 status=ok 时返回，pending/failed 一律空数组（不伪造）。"""
    if not isinstance(entities, dict) or entities.get("status") != "ok":
        return []
    items: list[dict] = []
    for ent in entities.get("items") or []:
        name = str(ent.get("name") or "")
        typ = str(ent.get("type") or "")
        if name and typ in ("person", "place", "organization", "term"):
            items.append({"type": typ, "text": _safe(name)})
    return items


def _project_embedded_images(detail: dict) -> list[dict]:
    """内嵌图片安全投影：只返回 partId / OCR 状态 / 尺寸与定位，不返回 artifact key / 路径。"""
    images: list[dict] = []
    for img in detail.get("embeddedImages") or []:
        locator = search_service.locator_for_part({
            "id": img.get("partId") or "",
            "part_type": "image",
            "location": img.get("location") or {},
            "image_meta": {
                "ocr_status": img.get("ocrStatus"),
                "width": img.get("width"),
                "height": img.get("height"),
            },
        })
        images.append({
            "partId": img.get("partId") or "",
            "partType": "image",
            "ocrStatus": img.get("ocrStatus") or "empty",
            "width": img.get("width"),
            "height": img.get("height"),
            "location": locator,
        })
    return images


def project_material_detail(detail: dict, *, entities: dict | None = None) -> dict:
    """材料详情投影：只输出业务字段，不返回 source_path / previewUrl / 全文 text。"""
    metadata = detail.get("metadata") or {}
    version = {
        "materialFamilyId": detail.get("materialFamilyId"),
        "versionNumber": int(detail.get("versionNumber") or 1),
        "supersedesMaterialId": detail.get("supersedesMaterialId"),
        "supersededByMaterialId": detail.get("supersededByMaterialId"),
    }
    content_parts: list[dict] = []
    for part in detail.get("contentParts") or []:
        entry = {
            "partId": part.get("partId") or part.get("id") or "",
            "partType": part.get("partType") or part.get("part_type") or "paragraph",
            "ordinal": int(part.get("ordinal") or 0),
            "location": search_service.locator_for_part(part),
        }
        if part.get("rows"):
            entry["rows"] = part["rows"]
        content_parts.append(entry)
    summary = detail.get("summary") or {}
    return {
        "materialId": detail.get("materialId") or "",
        "fileName": _safe(str(detail.get("fileName") or "")),
        "fileType": str(detail.get("fileType") or "text"),
        "status": str(detail.get("status") or "processing"),
        "folderPath": str(detail.get("folderPath") or ""),
        "createdAt": str(detail.get("createdAt") or ""),
        "updatedAt": metadata.get("modifiedAt"),
        "version": version,
        "summary": {
            "status": summary.get("status", "pending"),
            "text": _safe(str(summary.get("text") or "")),
        },
        "tags": [_safe(str(t)) for t in (detail.get("tags") or [])],
        "entities": _project_entities(entities),
        "contentParts": content_parts,
        "embeddedImages": _project_embedded_images(detail),
        "transcript": list(detail.get("transcript") or []),
        "readOnly": True,
    }


def project_knowledge_detail(view: dict) -> dict:
    """知识卡片详情投影：正文用清理后的 body，来源由卡片来源关系派生。"""
    sources = view.get("sources") or []
    source_refs = [
        {
            "sourceType": s.get("sourceType") or "material",
            "id": _safe(str(s.get("id") or "")),
            "title": _safe(str(s.get("title") or "")),
            "archived": bool(s.get("archived")),
        }
        for s in sources
    ]
    return {
        "knowledgeId": str(view.get("knowledgeId") or ""),
        "title": _safe(str(view.get("title") or "")),
        "content": _safe(str(view.get("body") or "")),
        "tags": [_safe(str(t)) for t in (view.get("tags") or [])],
        "entities": [],  # 知识卡片当前无实体抽取服务，不伪造
        "sourceRefs": source_refs,
        "evidenceEligible": bool(view.get("evidenceEligible")),
        # revision / indexStatus 由知识卡片公共只读视图提供（AG-02-04）。
        "revision": str(view.get("revision") or ""),
        "indexStatus": str(view.get("indexStatus") or "unknown"),
        # knowledge_view 仅返回 active 卡片，故归档/回收恒为 False。
        "archived": False,
        "recycled": False,
        "updatedAt": str(view.get("updatedAt") or ""),
        "readOnly": True,
    }
