"""资料 → 观察型理解：把 derived.py 已产出的实体 / 关系记录，转成本体里的实体与 ``observed`` 工作理解。

- 不重跑模型：只读取 ``derived_records``（ENTITY_EXTRACTION / RELATION_EXTRACTION，status=ok）。
- 每份资料最多 20 条；证据是 ``material_span``（quote = 关系记录里的原文片段），定位是尽力而为。
- 资料是关于第三方的（「远川项目 属于 X 公司」），所以主语是资料实体而不是「我」；
  分区按端点类型：涉及人 → 我的人（relationship），否则 → 我的事（happened）。
- 幂等：同资料同关系用 content_hash 去重；资料被永久删除时由 ``OntologyStore.detach_material`` 脱钩。
"""
from __future__ import annotations

import logging

from ..stores.ontology_store import OntologyConflictError, OntologyError, OntologyStore

logger = logging.getLogger(__name__)

MAX_CLAIMS_PER_MATERIAL = 20
_TYPE_MAP = {"person": "person", "organization": "organization", "place": "place", "term": "topic", "project": "project", "event": "event"}


def _records(material_id: str) -> tuple[dict | None, dict | None]:
    from ..stores.derived_store import DerivedStore

    store = DerivedStore.instance()
    ent = store.get_derived_record("material", material_id, "ENTITY_EXTRACTION")
    rel = store.get_derived_record("material", material_id, "RELATION_EXTRACTION")
    return ent, rel


def run(material_id: str, *, store: OntologyStore | None = None, entity_record: dict | None = None, relation_record: dict | None = None) -> dict:
    store = store or OntologyStore.instance()
    if entity_record is None and relation_record is None:
        entity_record, relation_record = _records(material_id)
    report = {"materialId": material_id, "entities": 0, "created": [], "reaffirmed": 0, "skipped": 0}
    entity_types: dict[str, str] = {}
    for item in (entity_record or {}).get("content", {}).get("items", []) if entity_record and entity_record.get("status") == "ok" else []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        etype = _TYPE_MAP.get(str(item.get("type") or "term"), "topic")
        entity_types[name] = etype
        try:
            store.upsert_entity(name, etype)
            report["entities"] += 1
        except OntologyError:
            continue
    if not relation_record or relation_record.get("status") != "ok":
        return report
    for rel in (relation_record.get("content") or {}).get("items", [])[:MAX_CLAIMS_PER_MATERIAL]:
        sub = (rel.get("subject") or {}).get("name")
        obj = (rel.get("object") or {}).get("name")
        predicate = str(rel.get("predicate") or "").strip()
        if not sub or not obj or not predicate:
            report["skipped"] += 1
            continue
        sub_type = entity_types.get(sub) or _TYPE_MAP.get(str((rel.get("subject") or {}).get("type") or "term"), "topic")
        obj_type = entity_types.get(obj) or _TYPE_MAP.get(str((rel.get("object") or {}).get("type") or "term"), "topic")
        try:
            subject = store.upsert_entity(sub, sub_type)
            target = store.upsert_entity(obj, obj_type)
        except OntologyError:
            report["skipped"] += 1
            continue
        people = "person" in (sub_type, obj_type)
        content = f"{sub} {predicate} {obj}"[:120]
        evidence = [{"kind": "material_span", "material_id": material_id, "quote": str(rel.get("evidence") or "")[:300]}]
        existing = store.find_active_by_hash(subject["id"], "relationship" if people else "happened", content)
        if existing:
            if not any(ev.get("materialId") == material_id for ev in existing["evidence"]):
                store.add_evidence(existing["id"], evidence, reaffirm=True)
            report["reaffirmed"] += 1
            continue
        try:
            claim = store.create_claim(
                {
                    "subject_entity_id": subject["id"],
                    "object_entity_id": target["id"],
                    "predicate": "relationship" if people else "happened",
                    "content": content,
                    "section": "people" if people else "matters",
                    "layer": "observed",
                    "confidence": float(rel.get("confidence") or 0.5),
                },
                evidence,
                trust_state="working",
                trust_origin="material",
                surface="import",
                note="来自资料的关系抽取",
            )
            report["created"].append(claim["id"])
        except OntologyConflictError:
            report["reaffirmed"] += 1
        except OntologyError as exc:
            logger.debug("资料理解写入被拒：%s", exc)
            report["skipped"] += 1
    return report


def notify_material(material_id: str) -> str | None:
    """derived.py 在关系记录写成 ok 后调用：入队 extract_material（幂等）。"""
    try:
        from .jobs import enqueue_material_extraction

        return enqueue_material_extraction(material_id)
    except Exception as exc:  # noqa: BLE001 - 本体侧故障不影响材料流水线
        logger.debug("入队资料理解抽取失败：%s", type(exc).__name__)
        return None
