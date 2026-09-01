"""MindOS 知识图谱数据组装（P10）。

聚合 MindOS 原材料与知识卡片为节点，构建三类边：
- source（已确认事实关系）：卡片 frontmatter 中登记的 mindos_source_material_ids。
- shared-tag（关联候选）：共享至少一个标签。
- similar（关联候选）：向量内容相似。

不创建、不确认任何关系；所有候选关系仅用于展示。
浏览器只接触业务 ID，不接触物理路径。
"""
import logging

from fastapi import APIRouter, HTTPException

from embedder import embed_query
from vector_store import search as vector_search, get_source_embedding

from . import knowledge
from .derived import OWNER_MATERIAL, KIND_ENTITY_EXTRACTION, KIND_RELATION_EXTRACTION
from .services import ingestion
from .stores import derived_store

router = APIRouter(prefix="/api/mindos", tags=["mindos-graph"])
logger = logging.getLogger(__name__)

# 候选关系控制：每节点最多保留的 shared-tag / similar 边数
_SEMANTIC_LIMIT = 5   # 每材料最多保留的语义边数（控噪，按 confidence 降序取 top）
_SHARED_LIMIT = 3
_SIMILAR_LIMIT = 3
_SIMILAR_MIN_SCORE = 0.25


def _collect_material_nodes() -> dict[str, dict]:
    """原材料节点；已回收材料不再展示。"""
    nodes: dict[str, dict] = {}
    recycled = ingestion.recycled_material_ids()
    for rec in ingestion.JobStore.instance().list():
        mid = rec["material_id"]
        if mid in recycled:
            continue
        nodes[mid] = {
            "id": mid,
            "type": "material",
            "label": rec["file_name"],
            "fileType": rec["file_type"],
            "tags": ingestion.material_tags(mid),
        }
    return nodes


def _collect_knowledge_nodes() -> tuple[dict[str, dict], dict[str, list[str]]]:
    nodes: dict[str, dict] = {}
    card_sources: dict[str, list[str]] = {}
    for item in knowledge.wiki_store.list_pages(limit=500).get("items", []):
        detail = knowledge.wiki_store.read_page(str(item["path"])) or item
        # 已归档/已合并卡片（mindos_archived / mindos_merged_into）不进入图谱。
        if not knowledge._is_active_mindos_card(detail) or not knowledge._is_rag_eligible_page(detail):
            continue
        public = knowledge._public(detail)
        kid = public["knowledgeId"]
        nodes[kid] = {
            "id": kid,
            "type": "knowledge",
            "label": public["title"],
            "tags": public["tags"],
        }
        card_sources[kid] = knowledge._source_ids(detail)
    return nodes, card_sources


def _add_edge(edges: list[dict], pairs: set[tuple[str, str]], a: str, b: str, relation: str, reason: str, directed: bool = True) -> bool:
    key = tuple(sorted((a, b)))
    if key in pairs:
        return False
    pairs.add(key)
    edges.append({"source": a, "target": b, "relation": relation, "reason": reason, "directed": directed})
    return True


def _load_material_relations() -> dict[str, list[dict]]:
    """material_id -> 关系三元组列表（仅 status=ok 计入）。

    防御式清洗：content.items 可能不是 list、单项可能不是 dict，逐条校验，
    坏记录丢弃，绝不让单条脏派生记录拖垮整张图谱接口。
    """
    store = derived_store.DerivedStore.instance()
    out: dict[str, list[dict]] = {}
    for rec in store.list_derived_records(owner_type=OWNER_MATERIAL, kind=KIND_RELATION_EXTRACTION):
        if rec.get("status") != "ok":
            continue
        content = rec.get("content") or {}
        items = content.get("items") if isinstance(content.get("items"), list) else []
        valid = [it for it in items if isinstance(it, dict)]
        if valid:
            out.setdefault(rec["owner_id"], []).extend(valid)
    return out


def _entity_name_index(nodes: dict[str, dict]) -> dict[str, set[str]]:
    """实体名(小写) -> 持有该名字的节点 id 集合。

    数据源：各材料 ENTITY_EXTRACTION 产物（主，status=ok 且节点仍在图谱中）
    + 卡片标题/材料文件名 label（辅，兼容「实体名≈资源名」的精确命中）。
    """
    idx: dict[str, set[str]] = {}
    store = derived_store.DerivedStore.instance()
    for rec in store.list_derived_records(owner_type=OWNER_MATERIAL, kind=KIND_ENTITY_EXTRACTION):
        if rec.get("status") != "ok":
            continue
        mid = rec["owner_id"]
        if mid not in nodes:  # 回收材料不参与桥接（双保险）
            continue
        content = rec.get("content")
        items = content.get("items") if isinstance(content, dict) and isinstance(content.get("items"), list) else []
        for it in items:
            if not isinstance(it, dict):  # 防御脏实体记录：非字典项跳过
                continue
            raw_name = it.get("name")
            name = str(raw_name or "").strip().lower()
            if name:
                idx.setdefault(name, set()).add(mid)
    for nid, node in nodes.items():
        label = str(node.get("label") or "").strip().lower()
        if label:
            idx.setdefault(label, set()).add(nid)
    return idx


def _safe_confidence(rel: dict) -> float:
    """置信度防御解析：非法/越界统一按 0.0（排序靠后，且不抛异常）。"""
    try:
        v = float(rel.get("confidence"))
    except (TypeError, ValueError):
        return 0.0
    return v if 0.0 <= v <= 1.0 else 0.0


def _semantic_edges(
    nodes: dict[str, dict],
    material_relations: dict[str, list[dict]],
    pairs: set[tuple[str, str]],
) -> list[dict]:
    """把材料的关系三元组桥接为 semantic 边（插在 source 之后、shared-tag 之前）。

    桥接锚点：实体产物索引 + label 索引合成。三元组 (S, p, O) 来自材料 M：
    - S、O 均命中其他资源 → 资源↔资源，direction=S→O（directed=True）；
    - 仅一端命中资源 R → M↔R（材料断言的事实涉及 R，无明确方向，directed=False）；
    - 均未命中 → 不产边（三元组仅作为派生产物存在）。

    确定性保证（评审 P1）：
    1. 先收集所有候选（每材料 confidence 降序 / relationId 升序取 top _SEMANTIC_LIMIT）；
    2. 再按 confidence DESC、material_id ASC、relationId ASC 全局排序；
    3. 最后才按排序结果逐条 _add_edge 进入全局 pair 去重。
    这样同一节点对在多材料/多谓词并存时，由更高 confidence、更小 material_id 的
    候选**确定性地**胜出，不再受 list_derived_records 返回顺序（updated_at）影响。
    """
    idx = _entity_name_index(nodes)

    def _hits(name: str, exclude: str) -> list[str]:
        found = idx.get(str(name or "").strip().lower(), set())
        return sorted(nid for nid in found if nid != exclude)  # 排序保证确定性

    candidates: list[dict] = []
    for mid, rels in material_relations.items():
        if mid not in nodes:
            continue
        # 每材料内确定性排序（confidence DESC，relationId ASC）
        ranked = sorted(rels, key=lambda r: (-_safe_confidence(r), str(r.get("relationId") or "")))
        # 先过滤出可桥接候选，再应用 _SEMANTIC_LIMIT：无法桥接/无效的关系不计入上限，
        # 避免「前 5 条都不桥接、第 6 条可形成有效语义边却被丢弃」。
        per_material_hits = 0
        for rel in ranked:
            if not isinstance(rel, dict):
                continue
            sub_rec = rel.get("subject")
            obj_rec = rel.get("object")
            if not isinstance(sub_rec, dict) or not isinstance(obj_rec, dict):
                continue
            sub = str(sub_rec.get("name") or "").strip()
            obj = str(obj_rec.get("name") or "").strip()
            pred = str(rel.get("predicate") or "").strip()
            if not (sub and obj and pred):
                continue  # 主体/客体/谓词任一缺失 → 丢弃
            s_hits, o_hits = _hits(sub, mid), _hits(obj, mid)
            if s_hits and o_hits:
                # 首选资源可能相同；继续按既定排序寻找首个不同资源对，避免在
                # 存在有效跨资源桥接时因自环而错误丢弃整条关系。
                pair = next(
                    ((s, o) for s in s_hits for o in o_hits if s != o),
                    None,
                )
                if pair is None:
                    continue
                a, b, directed = pair[0], pair[1], True  # 资源↔资源，方向 S→O
            elif s_hits or o_hits:
                a, b, directed = mid, (s_hits or o_hits)[0], False  # 材料↔资源，无方向
            else:
                continue  # 均未命中 → 不可桥接，不产边且不占用上限
            if a == b:
                continue  # 两端落在同一资源 → 不建自环
            if per_material_hits >= _SEMANTIC_LIMIT:
                break  # 已达该材料语义边上限
            per_material_hits += 1
            candidates.append({
                "confidence": _safe_confidence(rel),
                "mid": mid,
                "relation_id": str(rel.get("relationId") or ""),
                "a": a,
                "b": b,
                "directed": directed,
                "reason": f"{sub} {pred} {obj}",
            })

    # 全局确定性排序后逐条入图：同一节点对只保留最先命中的（高 confidence 优先）
    candidates.sort(key=lambda c: (-c["confidence"], c["mid"], c["relation_id"]))
    edges: list[dict] = []
    for c in candidates:
        added = _add_edge(edges, pairs, c["a"], c["b"], "semantic", c["reason"], directed=c["directed"])
        if not added:
            logger.info(
                "语义边因节点对冲突被丢弃: material_id=%s relation_id=%s reason=%s pair=%s",
                c["mid"], c["relation_id"], c["reason"],
                tuple(sorted((c["a"], c["b"]))),
            )
    return edges


def _shared_tag_edges(nodes: dict[str, dict], pairs: set[tuple[str, str]]) -> list[dict]:
    """Build shared-tag candidate edges, capped per node to avoid explosion.

    Uses the shared ``pairs`` set so a node pair already carrying a confirmed
    source edge never receives a candidate edge (confirmed takes priority).
    """
    edges: list[dict] = []
    tag_index: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        for tag in node.get("tags", []):
            tag_index.setdefault(tag.lower(), []).append(node_id)

    for node_id, node in nodes.items():
        # candidates = other nodes sharing at least one tag, by count desc
        my_tags = {t.lower() for t in node.get("tags", [])}
        if not my_tags:
            continue
        candidates: dict[str, int] = {}
        for tag in my_tags:
            for other in tag_index.get(tag, []):
                if other == node_id:
                    continue
                candidates[other] = candidates.get(other, 0) + 1
        ranked = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
        for other, count in ranked[:_SHARED_LIMIT]:
            _add_edge(edges, pairs, node_id, other, "shared-tag", f"共享标签×{count}")
    return edges


def _similar_edges(
    nodes: dict[str, dict],
    material_embeddings: dict[str, list[float]],
    card_texts: dict[str, str],
    pairs: set[tuple[str, str]],
) -> list[dict]:
    """Build content-similar candidate edges, capped per node.

    Uses the shared ``pairs`` set so a pair already carrying a confirmed source
    edge never receives a candidate edge (confirmed takes priority).
    """
    edges: list[dict] = []
    known_ids = set(nodes)

    def find_similar_materials(embedding: list[float], exclude_id: str) -> list[str]:
        if not embedding:
            return []
        chunks = vector_search(embedding, n_results=30)
        best: dict[str, float] = {}
        for chunk in chunks:
            record = ingestion.material_for_source(str(chunk.get("source_path") or ""))
            if record is None:
                continue
            mid = record["material_id"]
            if mid == exclude_id or mid not in known_ids:
                continue
            score = float(chunk.get("vector_score") or 0.0)
            if score < _SIMILAR_MIN_SCORE:
                continue
            if mid not in best or score > best[mid]:
                best[mid] = score
        return sorted(best, key=lambda x: best[x], reverse=True)[:_SIMILAR_LIMIT]

    # 材料 → 相似材料
    for node_id, node in nodes.items():
        if node["type"] != "material":
            continue
        embedding = material_embeddings.get(node_id)
        for other in find_similar_materials(embedding or [], node_id):
            _add_edge(edges, pairs, node_id, other, "similar", "内容相似")

    # 卡片 → 相似材料 / 相似卡片
    for node_id, node in nodes.items():
        if node["type"] != "knowledge":
            continue
        text = card_texts.get(node_id, "")
        if not text.strip():
            continue
        try:
            embedding = embed_query(text[:500])
        except Exception:
            embedding = []
        for other in find_similar_materials(embedding or [], node_id):
            _add_edge(edges, pairs, node_id, other, "similar", "内容相似")
        try:
            similar_cards = knowledge.search_cards(text, limit=_SIMILAR_LIMIT * 3)
        except Exception:
            similar_cards = []
        for card in similar_cards:
            other = card.get("knowledgeId")
            if other and other != node_id and other in known_ids:
                _add_edge(edges, pairs, node_id, other, "similar", "内容相似")
    return edges


def _stats(nodes: dict[str, dict], edges: list[dict]) -> dict:
    connected: set[str] = set()
    counts = {"source": 0, "shared-tag": 0, "similar": 0, "semantic": 0}
    for edge in edges:
        connected.add(edge["source"])
        connected.add(edge["target"])
        counts[edge["relation"]] = counts.get(edge["relation"], 0) + 1
    material_count = sum(1 for n in nodes.values() if n["type"] == "material")
    knowledge_count = len(nodes) - material_count
    return {
        "totalNodes": len(nodes),
        "materials": material_count,
        "knowledge": knowledge_count,
        "totalEdges": len(edges),
        "sourceEdges": counts["source"],
        "sharedTagEdges": counts["shared-tag"],
        "similarEdges": counts["similar"],
        "semanticEdges": counts["semantic"],
        "isolatedNodes": sum(1 for nid in nodes if nid not in connected),
    }


def build_graph() -> dict:
    """Assemble the full MindOS knowledge graph payload."""
    material_nodes = _collect_material_nodes()
    knowledge_nodes, card_sources = _collect_knowledge_nodes()
    nodes = {**material_nodes, **knowledge_nodes}

    edges: list[dict] = []
    pairs: set[tuple[str, str]] = set()

    # 来源：已确认事实关系（卡片 → 来源材料）；同时统计各节点引用次数。
    reference_count: dict[str, int] = {}
    for kid, source_ids in card_sources.items():
        for mid in source_ids:
            if mid in material_nodes:
                _add_edge(edges, pairs, kid, mid, "source", "来源")
                reference_count[kid] = reference_count.get(kid, 0) + 1
                reference_count[mid] = reference_count.get(mid, 0) + 1

    for node_id, node in nodes.items():
        node["referenceCount"] = reference_count.get(node_id, 0)

    # 语义边（AI 抽取事实，优先级次于 source、高于统计候选边）
    edges += _semantic_edges(nodes, _load_material_relations(), pairs)

    edges += _shared_tag_edges(nodes, pairs)

    # 相似边所需的材料 embedding 与卡片正文（一次获取，多次复用）
    material_embeddings: dict[str, list[float]] = {}
    for node_id, node in material_nodes.items():
        sp = ingestion.source_path_of(node_id)
        if sp:
            material_embeddings[node_id] = get_source_embedding(sp) or []
    card_texts: dict[str, str] = {}
    for item in knowledge.wiki_store.list_pages(limit=500).get("items", []):
        detail = knowledge.wiki_store.read_page(str(item["path"])) or item
        # 与节点收集一致：仅当前已确认且已索引 revision 参与相似召回。
        if not knowledge._is_active_mindos_card(detail) or not knowledge._is_rag_eligible_page(detail):
            continue
        kid = knowledge._knowledge_id(str(detail["path"]))
        content = str(detail.get("content") or "")
        try:
            _, body = knowledge.wiki_store._parse_frontmatter(content)
            text = body.strip()
        except Exception:
            text = content
        card_texts[kid] = text[:500]

    edges += _similar_edges(nodes, material_embeddings, card_texts, pairs)

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": _stats(nodes, edges),
    }


@router.get("/graph")
def graph_view():
    return build_graph()
