"""MindOS 统一检索服务（AG-02-01）。

Web 搜索（mindos/search.py）、QA（mindos/qa.py）与 Agent Gateway 都复用本模块，
避免出现第二套向量/BM25 检索实现：

- 生命周期规则（归档/回收/无正文卡片）只在 `is_material_excluded` 一处维护；
- 原材料候选构建（向量 + BM25 + 结构化上下文补全 + 多样性选择）主体在
  `build_material_candidates`，I/O 依赖以参数注入，调用方（qa.py）传入其模块级
  引用，保证既有测试的 patch（mindos.qa.* / mindos.search.*）仍可生效；
- `search_knowledge` / `search_material_chunks` / `search_unified` 供 Web、
  QA 与 Agent 共同调用，返回内部统一 SearchHit，由 projection 层转换为对外响应。
"""
import logging
import re
from dataclasses import dataclass
from typing import Callable, Literal

import lexical
import rag_strategy
from embedder import embed_query
from vector_store import (
    search as vector_search,
    get_chunks_by_ids,
    get_source_chunks,
)

from .. import knowledge
from . import ingestion

logger = logging.getLogger(__name__)

# ---- 原材料混合检索核心常量（qa.py re-export 保持既有测试引用） ----
VECTOR_CANDIDATES = 40
MAX_CHUNKS_PER_MATERIAL = 3
MATERIAL_SNIPPET_CHARS = 560
KNOWLEDGE_SNIPPET_CHARS = 460
PREFERRED_REFERENCED_MATERIAL_BONUS = 0.08
# 表格通常承载阶段、清单、金额、对比等可直接作答的事实；相较普通列表给予稍高
# 的通用证据质量加分，不依赖任何具体问题意图。
TABLE_STRUCTURE_BONUS = 0.22
LIST_STRUCTURE_BONUS = 0.10
# 查询已定位到某份资料的标题或章节时，最多为两份最相关资料补入结构化上下文。
MAX_CONTEXT_ENRICHED_MATERIALS = 3
MAX_CONTEXT_CHUNKS_PER_MATERIAL = 2
# 片段默认截断上限（qa._truncate_snippet 的既有默认值）
MAX_SNIPPET_CHARS = 700


# ---- 生命周期规则（单一维护点） -------------------------------------------
def is_material_excluded(material_id: str, archived: set[str], recycled: set[str]) -> bool:
    """统一生命周期过滤：仅已回收材料不出现在检索、证据与详情。

    ``archived`` 参数仅为内部调用兼容保留，不再参与判断；判断规则只在本函数维护，
    Web、QA 与 Agent 不得各自复制回收状态条件。
    """
    return material_id in recycled


# ---- 材料可检索状态策略（单一维护点） -------------------------------------
def material_searchable(
    material_id: str,
    archived: set[str],
    recycled: set[str],
    status_of_callable: Callable,
) -> bool:
    """统一判断材料是否可作为可检索/可作证据内容（Agent 检索 fail-closed）。

    规则：
    - 已回收材料不可检索；
    - 仅当状态查询明确返回 available 时才可检索；
    - processing / uploaded / failed 等状态不可检索（不能伪装为可检索内容）；
    - 状态服务异常或返回空值（无法确认状态）时按不可检索处理（fail-closed），
      避免暴露过期或未确认状态的索引内容。

    QA / Web 的候选构建保留既有行为（require_available=False 时不调用本函数）；
    Agent 检索统一复用本函数，禁止各自复制 available 判定。
    """
    if is_material_excluded(material_id, archived, recycled):
        return False
    try:
        public = status_of_callable(material_id)
    except Exception:
        return False  # fail-closed：状态无法确认时不可检索
    if public is None:
        return False
    return str(public.get("status") or "") == "available"


# ---- 定位引用（单一维护点，供 Agent 搜索 / 证据展开 / 详情投影共用） ------
def finite_seconds(value) -> float | None:
    """返回有限、非负的秒数；None/NaN/Infinity/负数一律返回 None（不产出伪定位）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        return None
    return v


def _int_or_none(value):
    """安全整数转换；损坏/异常数据返回 None（定位字段非法时省略，避免 500）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def locator_for_part(part: dict) -> dict:
    """由派生 part 构建真实定位；不得伪造页码/表格索引。

    同时兼容 detail_of 输出的 contentParts（partId/partType）与 DerivedStore
    原始 part（id/part_type）两种键名，供 Agent 搜索 / 证据展开 / 详情投影共用。
    页码 / 表格索引 / 图片尺寸等字段做安全转换，非法值时省略或整体不产出定位。
    """
    part_id = part.get("id") or part.get("partId") or ""
    location = part.get("location") or {}
    part_type = part.get("part_type") or part.get("partType")
    page = _int_or_none(location.get("page"))
    if part_type == "table":
        loc: dict = {"kind": "table", "partId": part_id}
        if page is not None:
            loc["page"] = page
        table_index = _int_or_none(location.get("table"))
        if table_index is not None:
            loc["tableIndex"] = table_index
        rows = [row for row in (part.get("text") or "").split("\n") if row]
        if rows:
            loc["rowStart"] = 1
            loc["rowEnd"] = len(rows)
            loc["columnStart"] = 0
            loc["columnEnd"] = len(rows[0].split("\t"))
        return loc
    if part_type == "image":
        meta = part.get("image_meta") or {}
        loc = {
            "kind": "embedded_image",
            "partId": part_id,
            "ocrStatus": meta.get("ocr_status", "empty"),
        }
        width = _int_or_none(meta.get("width"))
        height = _int_or_none(meta.get("height"))
        if width is not None:
            loc["width"] = width
        if height is not None:
            loc["height"] = height
        if page is not None:
            loc["page"] = page
        return loc
    kind = part_type if part_type in ("paragraph", "page") else "paragraph"
    loc = {"kind": kind, "partId": part_id}
    if page is not None:
        loc["page"] = page
    return loc


def build_material_locator(material_id: str, metadata: dict) -> dict | None:
    """依据 chunk 元数据与派生 part 构建真实定位；无法确定时返回 None。

    - 音频/视频转写：返回有限、非负、递增的时间秒数；无效时间不返回伪定位；
    - 文档 part：按 part_id 读取派生 part 的真实页码 / 表格 / 段落定位；
    - 无法确认（无 part_id、派生 part 缺失）时返回 None，不使用 chunk 序号代替。
    """
    metadata = metadata or {}
    modality = str(metadata.get("modality") or "")
    if modality == "transcript":
        start = finite_seconds(metadata.get("start_time"))
        end = finite_seconds(metadata.get("end_time"))
        if start is not None and end is not None and end > start:
            return {"kind": "transcript", "start": round(start, 3), "end": round(end, 3)}
        return None
    part_id = metadata.get("part_id")
    if not part_id:
        return None
    try:
        from ..stores.derived_store import DerivedStore

        part = DerivedStore.instance().get_part(material_id, str(part_id))
    except Exception:
        return None
    if part is None:
        return None
    return locator_for_part(part)


# ---- 评分辅助函数（qa.py 保留同名下划线别名，保证既有测试直接调用可用） ----
def query_terms(question: str) -> list[str]:
    """提取通用词面信号；不推断问题意图，也不包含业务领域词表。"""
    try:
        # 覆盖率只使用基础词边界；BM25 自身会额外使用中文双字词元，以兼容
        # 「开发阶段」与表格中「开发 / 阶段」这类不同排版写法。
        raw_terms = lexical._base_tokenize(question)
    except Exception:
        raw_terms = [part.strip().lower() for part in question.split()]
    stop = {
        "的", "了", "吗", "呢", "是", "会", "分", "个", "几个", "那几个", "哪个", "怎样", "怎么",
        "什么", "哪些", "多少", "请", "帮我",
    }
    terms: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        term = str(term).strip().casefold()
        if len(term) < 2 or term in stop or term in seen:
            continue
        # 偶数长度的中文连续词通常是多个双字概念黏连后的结果（例如
        # 「开发阶段」「项目预算」）。拆成双字信号用于覆盖率/结构质量判断，
        # 使相邻单元格、换行和连续书写保持一致；这不是业务同义词扩展。
        if len(term) >= 4 and len(term) % 2 == 0 and not term.isascii() and all("\u4e00" <= ch <= "\u9fff" for ch in term):
            term_parts = [term[index:index + 2] for index in range(0, len(term), 2)]
        else:
            term_parts = [term]
        for part in term_parts:
            if part in stop or part in seen:
                continue
            seen.add(part)
            terms.append(part)
    return terms[:12]


def term_coverage(text: str, terms: list[str]) -> float:
    """问题有效词在正文中的覆盖比例，作为向量/BM25 之外的可解释信号。"""
    if not terms:
        return 0.0
    folded = (text or "").casefold()
    return sum(_term_in_text(folded, term) for term in terms) / len(terms)


def _term_in_text(folded_text: str, folded_term: str) -> bool:
    """判断检索词是否真实出现，避免 ASCII 产品名被版本/文件名子串误命中。"""
    if folded_term.isascii() and any(char.isalnum() for char in folded_term):
        return re.search(
            rf"(?<![a-z0-9_.-]){re.escape(folded_term)}(?![a-z0-9_.-])",
            folded_text,
        ) is not None
    return folded_term in folded_text


def has_ascii_identifier(terms: list[str]) -> bool:
    """问题是否包含需要精确落证的 ASCII 专有名词/型号。"""
    return any(term.isascii() and any(char.isalpha() for char in term) for term in terms)


def structure_bonus(text: str, terms: list[str]) -> float:
    """对表格、列表和标题式内容做轻量加分，使结构化答案不被长说明文淹没。"""
    text = text or ""
    if term_coverage(text, terms) <= 0:
        return 0.0
    # 仅当结构化内容自身命中问题有效词时加分，避免无关表格抢占证据。制表符
    # 表格（解析 XLSX/DOCX/PDF 后的统一形式）优先于普通列表；Markdown 表格
    # 同样纳入，以覆盖导入的方案和说明文档。
    if "\t" in text or "\n|" in text:
        return TABLE_STRUCTURE_BONUS
    if "\n- " in text or "\n* " in text or "\n1." in text:
        return LIST_STRUCTURE_BONUS
    return 0.0


def is_structured_context(text: str) -> bool:
    """判断分块是否适合作为同源资料的补充上下文（不依赖业务关键词）。"""
    text = (text or "").strip()
    # 短表格也可能完整包含“阶段—日程—目标”这样的事实行，不能和普通短段落
    # 一起按 40 字过滤。表格至少应有表头/一行内容（换行）及两个以上字段；
    # 列表仍保留长度门槛，避免仅有一条无信息的项目符号被补入上下文。
    if ("\t" in text and "\n" in text) or ("\n|" in text and text.count("|") >= 3):
        return True
    if len(text) < 40:
        return False
    return "\n- " in text or "\n* " in text or "\n1." in text


def truncate_snippet(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]


# ---- 原材料候选构建核心（QA 与 Agent 共用，I/O 依赖参数注入） --------------
def _enrich_structured_context(
    best: dict[str, dict],
    terms: list[str],
    get_source_chunks_callable: Callable,
) -> None:
    """为已被混合检索定位的资料补入表格/清单分块。

    这不是按“排期”“流程”等意图词做特判：任何先由向量或 BM25 定位到的资料，
    只要含有表格或清单，都会把有限数量的结构化分块加入候选。由此解决标题、
    章节名和表格正文词面不相同（如“详细计划”标题对应“阶段/日程”表格）时的
    上下文断裂，同时不从无关材料中盲目拉取表格。
    """
    sources: dict[str, dict] = {}
    for candidate in best.values():
        source_path = str(candidate.get("source_path") or "")
        material_id = str(candidate.get("material_id") or "")
        if not source_path or not material_id:
            continue
        current = sources.get(material_id)
        if current is None or float(candidate["score"]) > float(current["score"]):
            sources[material_id] = candidate

    # 文档标题是用户查询与“同源上下文”之间的可靠桥梁。标题词面高度匹配的资料
    # （如“开发排期”）应先补表格，避免泛化的产品介绍以较高向量分抢走上下文预算。
    def source_rank(item: dict) -> float:
        return float(item["score"]) + term_coverage(str(item.get("title") or ""), terms) * 0.60

    for source in sorted(sources.values(), key=source_rank, reverse=True)[:MAX_CONTEXT_ENRICHED_MATERIALS]:
        material_id = source["material_id"]
        source_path = source["source_path"]
        added = 0
        try:
            chunks = get_source_chunks_callable(source_path, limit=200)
        except Exception:
            continue
        for chunk in chunks:
            text = str(chunk.get("text") or "").strip()
            if not is_structured_context(text):
                continue
            chunk_id = str(chunk.get("id") or f"{source_path}\x1f{text[:300]}")
            key = f"{material_id}\x1f{chunk_id}"
            if key in best:
                continue
            # 同源资料已经由原始检索命中；结构化正文优先于孤立标题，但只补有限条。
            best[key] = {
                "material_id": material_id,
                "title": source["title"],
                "source_path": source_path,
                "snippet": truncate_snippet(text, MATERIAL_SNIPPET_CHARS),
                "score": float(source["score"]) + TABLE_STRUCTURE_BONUS + 0.01,
                "chunk_id": chunk_id,
            }
            added += 1
            if added >= MAX_CONTEXT_CHUNKS_PER_MATERIAL:
                break


def build_material_candidates(
    query: str,
    limit: int,
    *,
    terms: list[str],
    archived: set[str],
    recycled: set[str],
    preferred_material_ids: set[str] | None = None,
    require_available: bool = False,
    source_ids: set[str] | None = None,
    # I/O 依赖：调用方传入其模块级引用，使既有测试的 patch 保持生效。
    embed_query_callable: Callable = embed_query,
    vector_search_callable: Callable = vector_search,
    lexical_search_callable: Callable = lexical.search,
    get_chunks_by_ids_callable: Callable = get_chunks_by_ids,
    get_source_chunks_callable: Callable = get_source_chunks,
    material_for_source_callable: Callable = ingestion.material_for_source,
    source_path_of_callable: Callable = ingestion.source_path_of,
    threshold_for_file_type_callable: Callable = rag_strategy.threshold_for_file_type,
    status_of_callable: Callable = ingestion.status_of,
) -> list[dict]:
    """统一构建原材料证据候选：向量 + BM25 + 生命周期过滤 + 结构化补全。

    返回按 score 降序、且按材料与片段多样性去重后的内部 rows，每条含
    material_id / title / source_path / snippet / score / chunk_id / file_type /
    metadata 等字段。QA 层将其转换为 Evidence，Agent 层转换为 SearchHit。

    - require_available=True 时，所有候选路径（向量 / BM25 / 优选补召回）都按
      `material_searchable` 统一策略过滤 processing / failed 材料（Agent 检索用；
      QA / Web 保持既有行为，传 False）；
    - source_ids 非空时作为检索范围在候选构建阶段过滤，避免 top-k 之后才过滤
      导致合法低排名材料丢失（过滤发生在排序与截断之前）。
    """
    preferred_material_ids = preferred_material_ids or set()
    source_ids = source_ids or None
    # 状态判断缓存：同一材料在多条召回路径中只查一次状态。
    status_cache: dict[str, bool] = {}

    def searchable(material_id: str) -> bool:
        if not require_available:
            return True
        if material_id not in status_cache:
            status_cache[material_id] = material_searchable(
                material_id, archived, recycled, status_of_callable
            )
        return status_cache[material_id]

    def within_range(material_id: str) -> bool:
        return source_ids is None or material_id in source_ids

    # 仅“单一产品名/型号”的定义型短问句要求证据直接出现该名称。像
    # “MindOS 整体流程是什么”这类多关键词问题仍可从未重复产品名的流程段落
    # 得到语义召回，避免把正确的后续步骤过滤掉。
    requires_identifier = len(terms) == 1 and has_ascii_identifier(terms)

    # 所有问题按真实 chunk 去重；是否需要统计、总结或比较由模型根据证据决定。
    best: dict[str, dict] = {}
    try:
        embedding = embed_query_callable(query)
        chunks = vector_search_callable(embedding, n_results=VECTOR_CANDIDATES) if embedding else []
    except Exception:
        chunks = []

    for chunk in chunks:
        source_path = str(chunk.get("source_path") or "")
        record = material_for_source_callable(source_path)
        if record is None:
            continue
        material_id = record["material_id"]
        if is_material_excluded(material_id, archived, recycled):
            continue
        if not searchable(material_id) or not within_range(material_id):
            continue
        text = str(chunk.get("text") or "").strip()
        if not text or text == record.get("file_name"):
            continue
        if requires_identifier and term_coverage(text, terms) <= 0:
            continue
        score = float(chunk.get("vector_score") or 0.0)
        # 按资料类型使用 rag_strategy 阈值过滤低相关命中
        file_type = str(record.get("file_type") or "text")
        try:
            threshold = threshold_for_file_type_callable(file_type, reranked=False)
        except Exception:
            threshold = 0.0
        if score < threshold:
            continue
        chunk_key = str(chunk.get("id") or f"{source_path}\x1f{text[:300]}")
        candidate = {
            "material_id": material_id,
            "title": record["file_name"],
            "file_type": file_type,
            "source_path": source_path,
            "snippet": truncate_snippet(text, MATERIAL_SNIPPET_CHARS),
            "score": score + term_coverage(text, terms) * 0.12 + structure_bonus(text, terms)
            + (PREFERRED_REFERENCED_MATERIAL_BONUS if material_id in preferred_material_ids else 0.0),
            "chunk_id": chunk_key,
            "metadata": chunk.get("metadata") or {},
        }
        key = f"{material_id}\x1f{chunk_key}"
        if key not in best or candidate["score"] > best[key]["score"]:
            best[key] = candidate

    # 关键词兜底：向量召回不足时，用 BM25 词面匹配补充（专有名词/数字/精确词常漏召）。
    try:
        bm25_hits = lexical_search_callable(query, n_results=VECTOR_CANDIDATES)
        bm25_max = max((score for _, score in bm25_hits), default=0.0)
        for chunk_id, bm25_score in bm25_hits:
            found = get_chunks_by_ids_callable([chunk_id])
            chunk = found[0] if found else None
            if not chunk:
                continue
            source_path = str(chunk.get("source_path") or "")
            record = material_for_source_callable(source_path)
            if record is None:
                continue
            material_id = record["material_id"]
            if is_material_excluded(material_id, archived, recycled):
                continue
            if not searchable(material_id) or not within_range(material_id):
                continue
            text = str(chunk.get("text") or "").strip()
            if not text or text == record.get("file_name"):
                continue
            if requires_identifier and term_coverage(text, terms) <= 0:
                continue
            normalized_bm25 = float(bm25_score) / bm25_max if bm25_max > 0 else 0.0
            candidate = {
                "material_id": material_id,
                "title": record["file_name"],
                "file_type": str(record.get("file_type") or "text"),
                "source_path": source_path,
                "snippet": truncate_snippet(text, MATERIAL_SNIPPET_CHARS),
                # 保留真实归一化 BM25 分数，精确词/编号/表头可与向量结果公平竞争。
                "score": normalized_bm25 + term_coverage(text, terms) * 0.12 + structure_bonus(text, terms)
                + (PREFERRED_REFERENCED_MATERIAL_BONUS if material_id in preferred_material_ids else 0.0),
                "chunk_id": str(chunk_id),
                "metadata": chunk.get("metadata") or {},
            }
            key = f"{material_id}\x1f{chunk_id}"
            if key not in best or candidate["score"] > best[key]["score"]:
                best[key] = candidate
    except Exception:
        # BM25 不可用时保持向量检索结果
        pass

    # 知识卡片明确引用的原材料是一手依据。可从其已索引分块补足问题相关
    # 内容，避免标题相近但无来源关系的材料挤掉原始方案、制度或需求文档。
    if preferred_material_ids:
        for material_id in preferred_material_ids:
            source_path = source_path_of_callable(material_id)
            if not source_path:
                continue
            record = material_for_source_callable(source_path)
            if record is None or is_material_excluded(material_id, archived, recycled):
                continue
            if not searchable(material_id) or not within_range(material_id):
                continue
            try:
                source_chunks = get_source_chunks_callable(source_path, limit=200)
            except Exception:
                continue
            for chunk in source_chunks:
                text = str(chunk.get("text") or "").strip()
                bonus = term_coverage(text, terms)
                if not text or text == record.get("file_name") or bonus <= 0:
                    continue
                chunk_id = str(chunk.get("id") or f"{source_path}\x1f{text[:300]}")
                key = f"{material_id}\x1f{chunk_id}"
                candidate = {
                    "material_id": material_id,
                    "title": record["file_name"],
                    "file_type": str(record.get("file_type") or "text"),
                    "source_path": source_path,
                    "snippet": truncate_snippet(text, MATERIAL_SNIPPET_CHARS),
                    # 这是用户确认的卡片来源，不是模型猜测；只补充实际命中问题词的内容。
                    "score": 0.55 + bonus * 0.12 + structure_bonus(text, terms)
                    + PREFERRED_REFERENCED_MATERIAL_BONUS,
                    "chunk_id": chunk_id,
                    "metadata": chunk.get("metadata") or {},
                }
                if key not in best or candidate["score"] > best[key]["score"]:
                    best[key] = candidate

    # 标题/章节命中只负责定位资料；补入同源表格/清单后再统一排序，保证模型看到
    # 可以直接归纳的事实，而不是只看到“详细计划”之类的章节标题。
    _enrich_structured_context(best, terms, get_source_chunks_callable)

    ranked = sorted(best.values(), key=lambda item: item["score"], reverse=True)
    rows: list[dict] = []
    per_material: dict[str, int] = {}
    seen_snippets: set[tuple[str, str]] = set()
    for row in ranked:
        material_id = row["material_id"]
        fingerprint = (material_id, row["snippet"])
        if fingerprint in seen_snippets or per_material.get(material_id, 0) >= MAX_CHUNKS_PER_MATERIAL:
            continue
        seen_snippets.add(fingerprint)
        per_material[material_id] = per_material.get(material_id, 0) + 1
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


# ---- 知识卡片检索（复用 knowledge.search_cards，统一排除无正文卡片） -------
def search_knowledge(
    query: str,
    limit: int = 10,
    *,
    for_qa: bool = True,
    include_snippet: bool = True,
    source_ids: set[str] | None = None,
) -> list[dict]:
    """检索 active MindOS 知识卡片，返回内部统一 SearchHit。

    for_qa=True 时经 knowledge.search_cards(for_qa=True) 只看正文有效卡片
    且 evidenceEligible 恒为 True；for_qa=False 时保留标题命中的导航能力，
    evidenceEligible 按片段是否含 ≥16 字正文标记。

    source_ids 非空时作为检索范围做严格前置过滤：经
    knowledge.search_cards_by_ids 按指定 ID 精确读取（不经过 top-k 截断），
    确保指定但排名较低的卡片仍能命中，且不会出现范围外的卡片。
    """
    try:
        if source_ids:
            cards = knowledge.search_cards_by_ids(source_ids, query, for_qa=for_qa)
        else:
            cards = knowledge.search_cards(query, limit=limit, for_qa=for_qa)
    except Exception:
        logger.exception("知识卡片检索异常，按无结果降级")
        return []
    hits: list[dict] = []
    for card in cards:
        snippet = str(card.get("snippet") or "")
        if not include_snippet:
            snippet = ""
        evidence_eligible = for_qa or len(snippet.strip()) >= 16
        hits.append({
            "source_type": "knowledge",
            "source_id": str(card.get("knowledgeId") or ""),
            "title": str(card.get("title") or "未命名知识卡片"),
            "snippet": truncate_snippet(snippet, KNOWLEDGE_SNIPPET_CHARS),
            "score": float(card.get("score") or 0.0),
            "chunk_id": None,
            "source_path": None,
            "metadata": {},
            "locator": None,
            "evidence_eligible": evidence_eligible,
        })
    return hits


# ---- 统一检索入口 ----------------------------------------------------------
@dataclass(frozen=True)
class UnifiedSearchRequest:
    query: str
    limit: int = 10
    types: tuple[Literal["knowledge", "material"], ...] | None = None
    source_ids: tuple[str, ...] | None = None
    include_snippet: bool = True
    # for_qa=False 时保留「标题命中但正文为空」卡片的导航能力（evidenceEligible=false）。
    for_qa: bool = False


def search_material_chunks(
    query: str,
    limit: int = 10,
    *,
    include_snippet: bool = True,
    preferred_material_ids: set[str] | None = None,
    source_ids: set[str] | None = None,
) -> list[dict]:
    """原材料文本检索（向量 + BM25），返回内部统一 SearchHit。

    复用 build_material_candidates 的完整质量管线（阈值过滤、结构化补全、
    生命周期过滤、多样性选择），与 QA 证据检索核心命中一致。Agent 检索默认
    只返回 available 材料（require_available=True），processing / failed 不进入
    搜索结果；source_ids 作为检索范围在候选构建阶段过滤。
    """
    recycled = ingestion.recycled_material_ids()
    rows = build_material_candidates(
        query,
        limit,
        terms=query_terms(query),
        archived=set(),
        recycled=recycled,
        preferred_material_ids=preferred_material_ids,
        require_available=True,
        source_ids=source_ids,
        embed_query_callable=embed_query,
        vector_search_callable=vector_search,
        lexical_search_callable=lexical.search,
        get_chunks_by_ids_callable=get_chunks_by_ids,
        get_source_chunks_callable=get_source_chunks,
        material_for_source_callable=ingestion.material_for_source,
        source_path_of_callable=ingestion.source_path_of,
        threshold_for_file_type_callable=rag_strategy.threshold_for_file_type,
    )
    hits: list[dict] = []
    for row in rows:
        metadata = dict(row.get("metadata") or {})
        snippet = str(row["snippet"]) if include_snippet else ""
        # 搜索命中直接携带真实定位（表格 / 音频转写 / 图片），无法确认时为 None。
        locator = build_material_locator(row["material_id"], metadata)
        hits.append({
            "source_type": "material",
            "source_id": row["material_id"],
            "title": row["title"],
            "file_type": row.get("file_type") or "text",
            "snippet": snippet,
            "score": float(row["score"]),
            "chunk_id": row.get("chunk_id"),
            "source_path": row.get("source_path"),
            # chunk_id 内含物理路径，仅内部使用，禁止投影到 Agent 响应
            "metadata": {
                "modality": metadata.get("modality"),
                "start_time": metadata.get("start_time"),
                "end_time": metadata.get("end_time"),
                "part_id": metadata.get("part_id"),
            },
            "locator": locator,
            "evidence_eligible": True,
        })
    return hits


# ---- Web 搜索结果口径（每材料一条最高命中快照） ----------------------------
def search_material_results(
    query: str,
    limit: int,
    *,
    device_scope: str = "global",
    embed_query_callable: Callable = embed_query,
    vector_search_callable: Callable = vector_search,
    lexical_search_callable: Callable = lexical.search,
    get_chunks_by_ids_callable: Callable = get_chunks_by_ids,
    material_for_source_callable: Callable = ingestion.material_for_source,
    job_store_instance_callable: Callable = ingestion.JobStore.instance,
    status_of_callable: Callable = ingestion.status_of,
) -> list[dict]:
    """Web 搜索原材料口径：每份材料保留最高向量命中 + BM25/文件名兜底。

    Web 前端需要“每材料一条”的紧凑结果（既有 search.py._material_results
    语义，保持既有结果与测试）。与 QA/Agent 的按块质量管线（
    build_material_candidates）共用同一批检索原语与生命周期过滤，不新增
    第二套向量/BM25 实现。

    阶段 2：device_scope 由请求票据身份决定，跨设备/账号材料不进入召回与兜底。
    """
    best: dict[str, dict] = {}
    # Web 搜索与问答一样只使用已完成材料。历史向量可能仍存在，不能因为命中
    # 旧索引就把处理中、失败或暂停的材料作为可检索证据返回。
    recycled = ingestion.recycled_material_ids(device_scope=device_scope)
    availability: dict[str, bool] = {}

    def is_available(material_id: str) -> bool:
        if material_id not in availability:
            try:
                public = status_of_callable(material_id)
                availability[material_id] = bool(public and public.get("status") == "available")
            except Exception:
                availability[material_id] = False
        return availability[material_id]
    try:
        embedding = embed_query_callable(query)
        n_candidates = max(40, limit * 8)
        chunks = vector_search_callable(embedding, n_results=n_candidates) if embedding else []
    except Exception:
        chunks = []
    for chunk in chunks:
        record = material_for_source_callable(str(chunk.get("source_path") or ""))
        if record is None:
            continue
        material_id = record["material_id"]
        if is_material_excluded(material_id, set(), recycled) or not is_available(material_id):
            continue
        candidate = {
            "materialId": material_id,
            "title": record["file_name"],
            "fileType": record["file_type"],
            "snippet": str(chunk.get("text") or "")[:400],
            "score": float(chunk.get("vector_score") or 0.0),
        }
        if material_id not in best or candidate["score"] > best[material_id]["score"]:
            best[material_id] = candidate

    # 关键词兜底：BM25 词面匹配补充向量漏召（专有名词/精确词）
    try:
        n_candidates = max(40, limit * 8)
        for chunk_id, _ in lexical_search_callable(query, n_results=n_candidates):
            found = get_chunks_by_ids_callable([chunk_id])
            chunk = found[0] if found else None
            if not chunk:
                continue
            record = material_for_source_callable(str(chunk.get("source_path") or ""))
            if record is None:
                continue
            material_id = record["material_id"]
            if is_material_excluded(material_id, set(), recycled) or not is_available(material_id):
                continue
            if material_id in best:
                continue  # 已有向量命中，避免用弱 BM25 覆盖
            best[material_id] = {
                "materialId": material_id,
                "title": record["file_name"],
                "fileType": record["file_type"],
                "snippet": str(chunk.get("text") or "")[:400],
                "score": 0.5,
            }
    except Exception:
        # BM25 不可用时保持向量检索结果
        pass

    # Keep the page usable while embeddings are unavailable, and make filename search exact.
    needle = query.casefold()
    try:
        for record in job_store_instance_callable().list(device_scope=device_scope):
            material_id = record["material_id"]
            if is_material_excluded(material_id, set(), recycled) or not is_available(material_id):
                continue
            name = str(record["file_name"])
            if needle not in name.casefold() or material_id in best:
                continue
            best[material_id] = {
                "materialId": material_id, "title": name,
                "fileType": record["file_type"], "snippet": name,
                "score": 1.0,
            }
    except Exception:
        pass
    return sorted(best.values(), key=lambda item: item["score"], reverse=True)[:limit]


def search_unavailable_material_results(
    query: str,
    limit: int,
    *,
    device_scope: str = "global",
    job_store_instance_callable: Callable = ingestion.JobStore.instance,
    status_of_callable: Callable = ingestion.status_of,
) -> list[dict]:
    """仅用安全元数据列出不可检索材料，绝不读取旧向量或正文片段。

    阶段 2：只列当前设备作用域内的不可检索材料，跨设备/账号不呈现。
    """
    needle = query.casefold().strip()
    if not needle:
        return []
    recycled = ingestion.recycled_material_ids(device_scope=device_scope)
    items: list[dict] = []
    try:
        records = job_store_instance_callable().list(device_scope=device_scope)
    except Exception:
        return []
    for record in records:
        material_id = str(record.get("material_id") or "")
        title = str(record.get("file_name") or "")
        if not material_id or material_id in recycled or needle not in title.casefold():
            continue
        try:
            public = status_of_callable(material_id)
        except Exception:
            public = None
        status = str((public or {}).get("status") or "")
        if status == "available" or not status:
            continue
        error_code = (public or {}).get("errorCode")
        actions: list[str] = []
        if error_code == "service_interrupted":
            actions.append("resume")
        elif status == "failed":
            actions.append("retry")
        items.append({
            "materialId": material_id,
            "title": title,
            "fileType": record.get("file_type") or "document",
            "status": status,
            "reason": (public or {}).get("errorMessage") or "资料尚未完成处理，暂不可检索",
            "errorCode": error_code,
            "actions": actions,
            "createdAt": (public or {}).get("createdAt") or record.get("created_at"),
        })
    return sorted(items, key=lambda item: str(item.get("createdAt") or ""), reverse=True)[:limit]


def search_unified(req: UnifiedSearchRequest) -> dict:
    """按类型与 source_ids 范围限定组合检索，返回统一 SearchHit 数组。

    source_ids 作为检索范围传入各来源的候选构建阶段（在排序与截断之前过滤），
    避免「先取 top-k 再过滤」导致指定但排名较低的合法对象被丢弃。
    """
    types = req.types or ("knowledge", "material")
    source_ids = set(req.source_ids) if req.source_ids else None
    items: list[dict] = []
    if "knowledge" in types:
        items.extend(
            search_knowledge(
                req.query,
                limit=req.limit,
                for_qa=req.for_qa,
                include_snippet=req.include_snippet,
                source_ids=source_ids,
            )
        )
    if "material" in types:
        items.extend(
            search_material_chunks(
                req.query,
                limit=req.limit,
                include_snippet=req.include_snippet,
                source_ids=source_ids,
            )
        )
    items.sort(key=lambda hit: float(hit["score"]), reverse=True)
    return {"items": items, "total": len(items)}
