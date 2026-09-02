"""给其他 Agent 的只读个人上下文包（Context Pack）。

规则（PRD §8.7 / 本设计 D5）：
- 只含 ``confirmed ∧ export_allowed ∧ privacy ∈ {public, private}`` 的理解；工作理解、敏感、受限永不出现。
- 用途绑定：调用方必须说明 purpose；包里带 purpose 与生成时间；每次生成写一条回执（ontology_meta 计数 + 网关审计）。
- 最小化：默认最多 50 条，按最近重申排序；可按分区收窄；不带证据原文、不带会话 ID、不带资料路径。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..stores.ontology_store import LAYER_TITLES, SECTION_TITLES, SECTIONS, OntologyError, OntologyStore

DEFAULT_MAX_CLAIMS = 50
HARD_MAX_CLAIMS = 200
EXPORTABLE_PRIVACY = ("public", "private")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def exportable_claims(store: OntologyStore, *, sections: tuple[str, ...] | None = None, limit: int = DEFAULT_MAX_CLAIMS) -> list[dict]:
    claims = store.list_claims(trust_states=("confirmed",), limit=5000)
    allowed = set(sections or SECTIONS)
    picked = [
        c for c in claims
        if c.get("exportAllowed") and c.get("privacyLevel") in EXPORTABLE_PRIVACY and c["section"] in allowed and c.get("scope") != "context_only"
    ]
    return picked[: max(1, min(int(limit), HARD_MAX_CLAIMS))]


def build_pack(*, purpose: str, sections: list[str] | None = None, max_claims: int = DEFAULT_MAX_CLAIMS, store: OntologyStore | None = None, consumer: str = "") -> dict:
    store = store or OntologyStore.instance()
    purpose = (purpose or "").strip()
    if len(purpose) < 2 or len(purpose) > 200:
        raise OntologyError("purpose 需要 2–200 字，说明这次要用这些理解做什么")
    wanted = tuple(s for s in (sections or []) if s)
    bad = [s for s in wanted if s not in SECTIONS]
    if bad:
        raise OntologyError(f"section 不合法：{','.join(bad)}")
    claims = exportable_claims(store, sections=wanted or None, limit=max_claims)
    receipt_id = f"cpk_{uuid.uuid4().hex[:12]}"
    generated_at = _now()
    items = [
        {
            "id": c["id"],
            "section": c["section"],
            "sectionTitle": SECTION_TITLES.get(c["section"], c["section"]),
            "layer": c["layer"],
            "layerTitle": LAYER_TITLES.get(c["layer"], c["layer"]),
            "content": c["content"],
            "about": c.get("objectName"),
            "lastReaffirmed": c["lastReaffirmed"],
        }
        for c in claims
    ]
    # 回执：只记计数与用途，不记正文。
    try:
        count = int(store.meta_get("context_pack_count", "0") or 0) + 1
        store.meta_set("context_pack_count", str(count))
        store.meta_set("context_pack_last", f"{generated_at}|{consumer or '-'}|{purpose[:60]}|{len(items)}")
    except Exception:  # noqa: BLE001
        pass
    return {
        "receiptId": receipt_id,
        "purpose": purpose,
        "consumer": consumer or None,
        "generatedAt": generated_at,
        "sections": list(wanted) if wanted else list(SECTIONS),
        "claims": items,
        "counts": {"included": len(items), "excludedNotExportable": _count_not_exportable(store, wanted or None) },
        "notice": "只包含用户已确认且允许导出的理解；不含未确认印象、敏感或受限内容、证据原文。",
    }


def _count_not_exportable(store: OntologyStore, sections: tuple[str, ...] | None) -> int:
    claims = store.list_claims(trust_states=("confirmed",), limit=5000)
    allowed = set(sections or SECTIONS)
    return sum(1 for c in claims if c["section"] in allowed and not (c.get("exportAllowed") and c.get("privacyLevel") in EXPORTABLE_PRIVACY))


def receipt_summary(store: OntologyStore | None = None) -> dict:
    store = store or OntologyStore.instance()
    last = store.meta_get("context_pack_last") or ""
    parts = last.split("|") if last else []
    return {
        "count": int(store.meta_get("context_pack_count", "0") or 0),
        "last": {"generatedAt": parts[0], "consumer": parts[1], "purpose": parts[2], "included": int(parts[3])} if len(parts) == 4 else None,
    }
