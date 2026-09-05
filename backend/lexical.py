"""BM25 词面检索 — 与稠密向量互补的稀疏召回（混合检索）。

稠密向量擅长语义，但对专有名词/型号/数字/罕见词的「字面精确匹配」常漏召回；
BM25 正好补这块。最终把 稠密 ∪ BM25 的并集交给交叉编码器重排精选。

实现说明：以 ChromaDB 文本集合为准重建内存索引；语料变更时 invalidate()。
当前为全量重建（语料小，O(N)），大规模可改增量。
"""
import logging
import re
import threading
import jieba
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

# 关闭 jieba 启动日志
jieba.setLogLevel(logging.WARNING)

_bm25 = None
_ids: list[str] | None = None
# 保护 _bm25/_ids：索引 worker 线程(每次写都 invalidate)与 /api/search(anyio 线程,惰性
# build)并发触碰；无锁会观察到 _bm25 与 _ids 来自不同语料(分两条语句赋值)→ 配错 chunk_id。
_LOCK = threading.Lock()
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _base_tokenize(text: str) -> list[str]:
    """Jieba 基础分词，供需要保留原始词边界的调用方复用。"""
    return [t for t in jieba.lcut((text or "").lower()) if t.strip()]


def _tokenize(text: str) -> list[str]:
    """BM25 分词，并补充中文连续字串的双字词元。

    中文资料中，同一概念可能写成「开发阶段」、也可能在表格单元格或换行处
    拆成「开发 / 阶段」。仅依赖词典分词会让这两种写法无法互相召回；补充双字
    词元可作为通用词面桥梁，不依赖任何业务词表或问题类型判断。
    """
    normalized = (text or "").lower()
    tokens = _base_tokenize(normalized)
    # 不重复添加 jieba 已经产出的词元，避免改变原有词频权重；仅补齐原分词
    # 缺失的双字词元，例如「开发阶段」中的「开发」「阶段」。
    existing = set(tokens)
    grams: list[str] = []
    for run in _CJK_RUN_RE.findall(normalized):
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if gram not in existing:
                grams.append(gram)
                existing.add(gram)
    return tokens + grams


def build_index() -> None:
    """从文本集合重建 BM25 索引（P0-2：只索引各源当前有效代，孤儿旧代不进入词面索引）"""
    global _bm25, _ids
    from vector_store import get_union_collection_records
    import generation_store
    try:
        data = get_union_collection_records("documents", include=["documents", "metadatas"])
        all_ids = data.get("ids") or []
        all_docs = data.get("documents") or []
        all_metas = data.get("metadatas") or []
        gens = generation_store.current_generations(generation_store.COLLECTION_TEXT)
        ids: list[str] = []
        docs: list[str] = []
        if gens:
            from vector_store import _keep_current
            for i, cid in enumerate(all_ids):
                meta = all_metas[i] if i < len(all_metas) else {}
                if _keep_current(meta, gens):
                    ids.append(cid)
                    docs.append(all_docs[i] if i < len(all_docs) else "")
        else:
            ids, docs = list(all_ids), list(all_docs)
        if not ids:
            with _LOCK:
                _bm25, _ids = None, None
            return
        corpus = [_tokenize(d) for d in docs]
        bm25 = BM25Okapi(corpus)
        with _LOCK:                       # 成对原子发布，避免读到错配的 (bm25, ids)
            _bm25, _ids = bm25, ids
        logger.info(f"BM25 索引已重建, 文档块数: {len(ids)}")
    except Exception as e:
        logger.warning(f"BM25 索引重建失败: {e}")
        with _LOCK:
            _bm25, _ids = None, None


def invalidate() -> None:
    """语料变更后调用，下次检索惰性重建"""
    global _bm25, _ids
    with _LOCK:
        _bm25, _ids = None, None


def search(query: str, n_results: int = 10) -> list[tuple[str, float]]:
    """返回 [(chunk_id, bm25_score), ...]（仅正分）"""
    with _LOCK:
        bm25, ids = _bm25, _ids
    if bm25 is None:
        build_index()
        with _LOCK:
            bm25, ids = _bm25, _ids        # 快照成对取出，整段在本地变量上操作
    if bm25 is None or not ids:
        return []
    try:
        scores = bm25.get_scores(_tokenize(query))
        ranked = sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)
        return [(cid, float(s)) for cid, s in ranked[:n_results] if s > 0]
    except Exception as e:
        logger.warning(f"BM25 检索失败: {e}")
        return []
