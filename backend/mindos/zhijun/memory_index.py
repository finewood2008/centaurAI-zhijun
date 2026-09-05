"""Rebuildable, process-local full-candidate embedding index.

Reuses the shared encoder, lazily loading only an installed local model directory.
No downloads, network calls, disk writes, or new evidence. Each namespace is one
ontology database/device; every refresh removes deleted/changed source versions.
"""
from collections import OrderedDict
import hashlib
import importlib
import logging
import math
import sys
import threading

CACHE = OrderedDict()
_LOCK = threading.Lock()
_BATCH = 32
_IMPORT_FAILED = False
logger = logging.getLogger(__name__)


def _local_encoder():
    global _IMPORT_FAILED
    module = sys.modules.get("embedder")
    if module is None and not _IMPORT_FAILED:
        try:
            module = importlib.import_module("embedder")
        except Exception:
            _IMPORT_FAILED = True
            logger.warning("本地检索组件不可用，使用关键词检索；不调用在线服务。")
    model = getattr(module, "_text_model", None)
    if model is None:
        loader = getattr(module, "get_local_text_embedder", None)
        if callable(loader):
            model = loader()
    return module, model


def scores(namespace, query, documents, source_versions=None):
    """Return cosine scores for *all* eligible documents, never a sample."""
    if not query or not documents:
        with _LOCK:
            CACHE.pop(namespace, None)
        return {}
    module, model = _local_encoder()
    with _LOCK:
        if model is None:
            CACHE.pop(namespace, None)
            return {}
        index = CACHE.get(namespace)
        if index is None or index["model"] is not model:
            index = {"model": model, "rows": {}, "queries": OrderedDict()}
        versions = {ident: hashlib.sha256((str((source_versions or {}).get(ident, "")) + "\0" + text).encode()).hexdigest()
                    for ident, text in documents.items()}
        # Drop obsolete vectors before any possibly failing encode operation.
        index["rows"] = {ident: row for ident, row in index["rows"].items()
                         if versions.get(ident) == row[0]}
        CACHE[namespace] = index
        CACHE.move_to_end(namespace)
        while len(CACHE) > 4:
            CACHE.popitem(last=False)
        prefix = getattr(module, "BGE_QUERY_INSTRUCTION", "") if getattr(module, "USE_QUERY_INSTRUCTION", False) else ""
        question = prefix + query[:2322]
        missing = [ident for ident in sorted(documents) if ident not in index["rows"]]
        pending_query = question not in index["queries"]
        try:
            for offset in range(0, max(1, len(missing)), _BATCH):
                ids = missing[offset:offset + _BATCH]
                texts = ([question] if pending_query else []) + [documents[ident] for ident in ids]
                if not texts:
                    continue
                vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
                if len(vectors) != len(texts):
                    return {}
                vectors = [tuple(float(v) for v in vector) for vector in vectors]
                if pending_query:
                    index["queries"][question] = vectors.pop(0)
                    pending_query = False
                index["rows"].update({ident: (versions[ident], vector) for ident, vector in zip(ids, vectors)})
            q = index["queries"][question]
            index["queries"].move_to_end(question)
            while len(index["queries"]) > 32:
                index["queries"].popitem(last=False)
            result = {}
            for ident, (_, vector) in index["rows"].items():
                if len(q) != len(vector):
                    continue
                denominator = math.sqrt(sum(v*v for v in q) * sum(v*v for v in vector))
                value = sum(a*b for a, b in zip(q, vector)) / denominator if denominator else 0
                if math.isfinite(value):
                    result[ident] = value
            return result
        except Exception:
            # Auxiliary indexing is optional; lexical recall remains available.
            return {}
