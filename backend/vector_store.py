"""ChromaDB 向量存储封装（分块 schema v2）"""
import logging
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
import chromadb
from chromadb.config import Settings
from config import (
    CHROMA_DATA_DIR,
    CHROMA_COLLECTION,
    SCHEMA_VERSION,
    IMAGE_COLLECTION,
    TEXT_MODEL_ID,
    VIDEO_FRAMES_DIR,
    CHROMA_SYNC_THRESHOLD_ENABLED,
    CHROMA_SYNC_THRESHOLD,
    MEMORY_COLLECTION,
    INDEX_DELTA_MAX_DOCUMENTS, INDEX_DELTA_MAX_VECTORS, INDEX_DELTA_MAX_AGE_SECONDS,
    INDEX_DELTA_MAX_BYTES, INDEX_MIN_FREE_RATIO,
)
import generation_store
import index_registry

logger = logging.getLogger(__name__)

# ======================= 阶段B：索引健康闸门（D4 损坏策略） =======================
# 状态机：unknown -> healthy / corrupted；begin_rebuild -> rebuilding；
# commit 成功后重新自检置 healthy，abort 回落 unknown。
# 仅 corrupted 禁止一切写入与巡检重入队；rebuilding 不阻断写目标
# （写路径在重建期间仍指向 __rebuild 集合），故不视为损坏。
INDEX_STATE_UNKNOWN = "unknown"
INDEX_STATE_HEALTHY = "healthy"
INDEX_STATE_REBUILDING = "rebuilding"
INDEX_STATE_CORRUPTED = "corrupted"
_index_state = INDEX_STATE_UNKNOWN
_index_state_lock = threading.Lock()


class IndexCorruptedError(RuntimeError):
    """索引损坏闸门：故障索引禁止写入/读取重建。调用方应映射为稳定错误（503 index_corrupted）。"""


_INDEX_CORRUPTION_MARKERS = (
    "error loading hnsw index",
    "error constructing hnsw segment reader",
    "error creating hnsw segment reader",
    "error sending backfill request to compactor",
    "hnsw index",
    "hnsw segment",
)


def index_health_state() -> str:
    with _index_state_lock:
        return _index_state


def index_health_blocked() -> bool:
    """索引处于 corrupted 时，任何写路径都不得触碰 Chroma。"""
    with _index_state_lock:
        return _index_state == INDEX_STATE_CORRUPTED


def set_index_health_state(state: str, error_code: str | None = None,
                           error_detail: str | None = None) -> None:
    """更新进程内健康状态并持久化到 index_registry（损坏结论必须落库）。"""
    global _index_state
    with _index_state_lock:
        _index_state = state
    # 运行时写入异常可能先于启动健康检查发生；此时也必须先创建 legacy 路由，
    # 否则 mark_active_corrupted 找不到 base generation，只会留下进程内状态。
    try:
        index_registry.ensure_registry()
    except Exception as exc:
        logger.warning("index_registry 初始化失败，索引状态可能无法持久化: %s", type(exc).__name__)
    if state == INDEX_STATE_CORRUPTED:
        index_registry.mark_active_corrupted(error_code=error_code, error_detail=error_detail)
    else:
        routing = index_registry.get_routing()
        base_id = (routing or {}).get("base_generation_id") if routing else None
        if base_id:
            index_registry.set_generation_status(base_id, state, error_code=error_code,
                                                 error_detail=error_detail)


def _check_index_writable() -> None:
    """写路径统一闸门：损坏时抛稳定异常，绝不向故障索引写入。"""
    if index_health_blocked():
        raise IndexCorruptedError("index_corrupted: 索引已损坏，禁止写入。请先恢复或重建索引。")


def ensure_index_writable() -> None:
    """供记忆、知识卡片等独立 collection 的写路径复用健康闸门。"""
    _raise_if_index_maintenance()
    _check_index_writable()


def ensure_index_readable() -> None:
    """读路径统一闸门：故障索引不得继续触发 Chroma InternalError。"""
    _raise_if_index_maintenance()
    if index_health_blocked():
        raise IndexCorruptedError("index_corrupted: 索引已损坏，禁止读取。请先恢复或重建索引。")


class IndexMaintenanceError(RuntimeError):
    """受控维护期间拒绝新的 Chroma 操作。"""


def _raise_if_index_maintenance() -> None:
    with _MAINTENANCE_STATE_LOCK:
        if _MAINTENANCE_ACTIVE:
            raise IndexMaintenanceError("index_maintenance_in_progress")


def is_index_storage_corruption(exc: BaseException) -> bool:
    """只识别 Chroma/HNSW 存储层故障，不把解析或模型失败误标为索引损坏。"""
    if isinstance(exc, IndexCorruptedError):
        return True
    name = type(exc).__name__.lower()
    detail = str(exc).lower()
    return name == "internalerror" or any(marker in detail for marker in _INDEX_CORRUPTION_MARKERS)


def record_index_operation_failure(exc: BaseException, operation: str) -> bool:
    """识别到存储层损坏时原子关闭索引入口；返回是否已切入 corrupted。"""
    if not is_index_storage_corruption(exc):
        return False
    if not index_health_blocked():
        logger.error("ChromaDB 存储故障，索引进入 corrupted 状态 operation=%s error=%s",
                     operation, type(exc).__name__)
        set_index_health_state(
            INDEX_STATE_CORRUPTED,
            error_code="index_corrupted",
            error_detail=f"{operation}:{type(exc).__name__}",
        )
    return True


def raise_if_index_storage_corruption(exc: BaseException, operation: str) -> None:
    """将已识别的存储层异常转换为稳定的 API/任务错误。"""
    if isinstance(exc, IndexCorruptedError):
        raise exc
    if record_index_operation_failure(exc, operation):
        raise IndexCorruptedError(
            "index_corrupted: ChromaDB 索引存储不可用，已停止后续读写。"
        ) from exc


def reset_index_health() -> None:
    """测试/复检用：清空进程内健康状态（不触碰 Chroma），由下次自检重新判定。"""
    global _index_state
    with _index_state_lock:
        _index_state = INDEX_STATE_UNKNOWN
# ====================================================================================

# ======================= P0-3 三态读取契约（索引可靠性方案） =======================
# 读取状态：ok=正常读到数据；empty=确认无记录（合法未索引态）；
# read_error=ChromaDB 读取失败（索引损坏/句柄失效等，绝不与 empty 混淆）。
READ_OK = "ok"
READ_EMPTY = "empty"
READ_ERROR = "read_error"

# verify_source_index 的返回状态（完整性校验）：
# ok=索引存在且完整；not_indexed=无记录（合法未索引）；
# integrity_failed=有记录但不完整（缺块/重复/chunk_count 错/hash 不一致）；
# read_error=读取失败。integrity_failed 与 read_error 都必须触发安全重建，
# 绝不能被判为「内容未变，跳过」。
VERIFY_OK = "ok"
VERIFY_NOT_INDEXED = "not_indexed"
VERIFY_INTEGRITY_FAILED = "integrity_failed"
VERIFY_READ_ERROR = "read_error"
# ====================================================================================

_client = None
_collection = None
_image_collection = None

# 集合单例锁：get_collection/get_image_collection/recreate_collection 从多线程触发
# （/api/* 在 anyio 线程、后台索引池在 worker 线程）。无锁时 recreate 的「先 delete 后
# 重建」窗口里其它线程会拿到指向已删集合的旧句柄 → 报错。RLock 串行化并保证发布原子。
_COLLECTION_LOCK = threading.RLock()
# 路由栅栏：公开 Chroma 操作持锁直到 batch/调用结束，目录级 commit 在同一锁内
# 更新 routing，因此不会出现“检查旧 epoch 后、写入前被切换”的窗口。使用 RLock
# 保持现有公开 API 的嵌套调用兼容；后续可在不改变契约的前提下替换为 RWLock。
_ROUTING_LOCK = threading.RLock()
_MAINTENANCE_STATE_LOCK = threading.Lock()
_MAINTENANCE_ACTIVE = False
_DOCUMENT_CACHE_SIGNATURE: tuple[int, int] | None = None
_DOCUMENT_CACHE_ITEMS: list[dict] | None = None

# ======================= P1-3 Chroma 访问生命周期（引用计数） =======================
# 空闲卸载（release_chroma）不能在还有进行中操作时关闭 client，否则会关掉正在被
# 查询/写入/图谱构建持有的句柄 → 运行时报错。用活跃操作计数把关：查询、写入、
# 图谱构建在持 Chroma 句柄期间进入 operation()，release_chroma 只在计数为 0 时
# 真正 close client，并在持有 _COLLECTION_LOCK 期间完成释放（禁止释放期间新建句柄）。
_ACTIVE_OPS = 0
_OPS_LOCK = threading.Lock()


def active_operations() -> int:
    """当前持 Chroma 句柄的活跃操作数（0 时空闲卸载才允许真正释放）。"""
    with _OPS_LOCK:
        return _ACTIVE_OPS


def _inc_operation() -> None:
    global _ACTIVE_OPS
    with _OPS_LOCK:
        _ACTIVE_OPS += 1


def _dec_operation() -> None:
    global _ACTIVE_OPS
    with _OPS_LOCK:
        _ACTIVE_OPS -= 1


class _active_operation:
    """一段持 Chroma 句柄的活跃操作（查询/写入/图谱构建）。"""

    __slots__ = ()

    def __enter__(self) -> "_active_operation":
        _inc_operation()
        return self

    def __exit__(self, *exc) -> bool:
        _dec_operation()
        return False


def operation():
    """P1-3：把一段 Chroma 操作标记为活跃，供 `with operation():` 显式包裹
    （图谱构建等长操作也可用）。返回上下文管理器。"""
    return _active_operation()


@contextmanager
def routed_operation():
    """Track a complete raw-collection operation under the routing fence.

    Callers that need to keep a Chroma collection handle across get/upsert/get
    must use this instead of obtaining a handle from a tracked factory and then
    operating on it after the factory's reference count has already ended.
    """
    _raise_if_index_maintenance()
    with _ROUTING_LOCK:
        _raise_if_index_maintenance()
        with operation():
            yield


@contextmanager
def index_maintenance():
    """拒绝新的访问并独占路由锁，供受控集合维护使用。"""
    global _MAINTENANCE_ACTIVE
    with _MAINTENANCE_STATE_LOCK:
        if _MAINTENANCE_ACTIVE:
            raise IndexMaintenanceError("index_maintenance_in_progress")
        _MAINTENANCE_ACTIVE = True
    try:
        with _ROUTING_LOCK:
            yield
    finally:
        with _MAINTENANCE_STATE_LOCK:
            _MAINTENANCE_ACTIVE = False


def _tracked_operation(fn):
    """装饰器：把持 Chroma 句柄的公开入口包裹进 operation() 引用计数。"""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _raise_if_index_maintenance()
        with _ROUTING_LOCK:
            _raise_if_index_maintenance()
            with operation():
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    raise_if_index_storage_corruption(exc, fn.__name__)
                    raise

    return wrapper


# ====================================================================================


def _invalidate_document_cache() -> None:
    global _DOCUMENT_CACHE_SIGNATURE, _DOCUMENT_CACHE_ITEMS
    with _COLLECTION_LOCK:
        _DOCUMENT_CACHE_SIGNATURE = None
        _DOCUMENT_CACHE_ITEMS = None


_base_client = None
_delta_client = None
_base_client_generation_id: str | None = None
_delta_client_generation_id: str | None = None
_rebuild_client = None
_rebuild_generation_id: str | None = None


def _collection_metadata(space: str = "cosine") -> dict:
    metadata: dict = {"hnsw:space": space}
    if CHROMA_SYNC_THRESHOLD_ENABLED:
        metadata["hnsw:sync_threshold"] = int(CHROMA_SYNC_THRESHOLD)
    return metadata


def _get_base_client():
    """只读 base 代际的 Chroma client（读取/重建/健康检查主目录）。

    阶段 B 下 base 即 legacy chroma_data（迁移来源，保持只读）。
    """
    global _base_client, _base_client_generation_id
    routing = index_registry.ensure_registry()
    generation_id = (routing or {}).get("base_generation_id")
    if _base_client is None or _base_client_generation_id != generation_id:
        with _COLLECTION_LOCK:
            if _base_client is None or _base_client_generation_id != generation_id:
                base_path, _ = index_registry.active_generation_paths()
                path = str(base_path) if base_path else str(CHROMA_DATA_DIR)
                _base_client = chromadb.PersistentClient(
                    path=path,
                    settings=Settings(anonymized_telemetry=False),
                )
                _base_client_generation_id = generation_id
    return _base_client


def _get_delta_client():
    """可写 delta 代际的 Chroma client（首次访问自动创建 delta 目录）。

    所有增量写入（vector/记忆/知识卡片/wiki 集合）只进 delta；base 永不直接写入。
    若路由 delta 的注册状态非 healthy（corrupted/building），作废缓存并交给
    ensure_delta() 创建新 delta，避免继续写入已损坏的 delta 升级为全索引损坏。
    """
    global _delta_client, _delta_client_generation_id
    routing = index_registry.ensure_registry()
    generation_id = (routing or {}).get("delta_generation_id")
    routed_healthy = False
    if generation_id:
        row = index_registry.get_generation(generation_id)
        routed_healthy = bool(row and row.get("status") == index_registry.STATUS_HEALTHY)
    if _delta_client is None or _delta_client_generation_id != generation_id or not routed_healthy:
        with _COLLECTION_LOCK:
            if _delta_client is None or _delta_client_generation_id != generation_id or not routed_healthy:
                result = index_registry.ensure_delta()
                if not result.get("ok"):
                    raise RuntimeError(f"无法创建 delta 代际: {result.get('error')}")
                _delta_client = chromadb.PersistentClient(
                    path=result["delta_path"],
                    settings=Settings(anonymized_telemetry=False),
                )
                _delta_client_generation_id = result["delta_generation_id"]
    return _delta_client


def _probe_delta_client():
    """打开当前路由 delta 目录的临时 client（不触发替换），供健康检查探测损坏。

    与 _get_delta_client 不同：绝不调用 ensure_delta()（那会静默替换损坏 delta），
    从而让损坏在探测前可见、可被记录。调用方用后必须 close()。
    返回 None 表示 delta 尚未创建（全新库 = 健康空态，不是损坏）。
    """
    routing = index_registry.ensure_registry()
    generation_id = (routing or {}).get("delta_generation_id")
    if not generation_id:
        return None
    generation = index_registry.get_generation(generation_id)
    if not generation or not generation.get("path"):
        return None
    if not Path(generation["path"]).is_dir():
        return None
    return chromadb.PersistentClient(
        path=generation["path"],
        settings=Settings(anonymized_telemetry=False),
    )


def _get_client():
    """兼容别名：base client（读取/重建主目录）。"""
    return _get_base_client()


def resolve_index_target(operation: str = "query") -> dict:
    """C1：返回当前活跃 base/delta 的路由快照（唯一入口，禁止业务方自行拼接 client）。

    所有 vector_store 读写、watcher 任务与重建代码只通过它获取 client/collection；
    禁止缓存裸 PersistentClient。operation: query | write | maintenance
    （路由读锁/epoch 围栏的完整实现属后续增量，此处仅固化快照契约）。
    """
    base_client = _get_base_client()
    delta_client = _get_delta_client()
    routing = index_registry.ensure_registry()
    return {
        "operation": operation,
        "routing_epoch": (routing or {}).get("routing_epoch", 0),
        "base_generation_id": (routing or {}).get("base_generation_id"),
        "delta_generation_id": (routing or {}).get("delta_generation_id"),
        "base_client": base_client,
        "delta_client": delta_client,
    }


# ---- P0-5 统一 collection 工厂 ----
# 禁止业务代码直接调用 client.get_or_create_collection。所有集合必须经此工厂创建，
# 以便统一注入 collection metadata（space / 候选 sync_threshold）并登记集合名，
# 供启动自检（verify_chroma_health）自动枚举探测，避免部分集合生效、部分遗漏。
_REGISTERED_COLLECTIONS: dict[str, None] = {}


@_tracked_operation
def get_or_create_collection(name: str, space: str = "cosine") -> object:
    """统一创建/获取 Chroma 集合（C1：工厂默认写 delta，base 只读）。

    memory/knowledge/wiki 等所有写者经此工厂自动进 delta；读侧 union 用
    get_base_collection/get_delta_collection 显式取双侧。登记集合名供启动自检枚举。
    """
    ensure_index_readable()
    metadata = _collection_metadata(space)
    try:
        # 全量重建期间，记忆/知识卡片等独立 collection 同样必须进入 building base；
        # 否则提交后它们会遗留在 retired delta，造成 Web 检索缺失。
        with _REBUILD_LOCK:
            rebuild_client = _rebuild_client
        client = rebuild_client or _get_delta_client()
        col = client.get_or_create_collection(name=name, metadata=metadata)
    except Exception as exc:
        raise_if_index_storage_corruption(exc, "get_or_create_collection")
        raise
    with _COLLECTION_LOCK:
        _REGISTERED_COLLECTIONS[name] = None
    return col


def get_base_collection(name: str, space: str = "cosine") -> object:
    """只读 base 集合（union 读取 / 目录级重建目标）。"""
    metadata = _collection_metadata(space)
    return _get_base_client().get_or_create_collection(name=name, metadata=metadata)


def get_delta_collection(name: str, space: str = "cosine") -> object:
    """可写 delta 集合（写路径目标；工厂 get_or_create_collection 的底层）。"""
    metadata = _collection_metadata(space)
    return _get_delta_client().get_or_create_collection(name=name, metadata=metadata)


def _union_collections(name: str) -> list:
    """返回该集合名的双代际读取集合：[主(delta)集合, base 集合]（缺侧自动跳过）。

    主集合即写路径主集合（文本→get_collection / 图片→get_image_collection / 其它→
    get_or_create_collection），base 只读侧从只读 client 取。只读路径使用。
    """
    if name == CHROMA_COLLECTION:
        home = get_collection()
    elif name == IMAGE_COLLECTION:
        home = get_image_collection()
    elif state != INDEX_STATE_REBUILDING:
        home = get_or_create_collection(name)
    cols = [home]
    try:
        base_col = _get_base_client().get_collection(name=name)
        if base_col not in cols:
            cols.append(base_col)
    except Exception:
        pass
    return cols


@_tracked_operation
def get_union_collection_records(name: str, include: list[str] | None = None) -> dict:
    """读取一个业务 collection 的 base+delta 并集，供非材料索引复用。

    以 id 去重时 delta 优先，避免同一稳定 id 的新写入被 legacy base 覆盖。
    """
    merged: dict[str, tuple[object, dict]] = {}
    for col in _union_collections(name):
        try:
            result = col.get(include=include or ["documents", "metadatas"])
        except Exception as exc:
            raise_if_index_storage_corruption(exc, f"union_get:{name}")
            continue
        ids = result.get("ids") or []
        for index, item_id in enumerate(ids):
            if item_id in merged:
                continue
            row = {key: (value[index] if isinstance(value, list) and index < len(value) else None)
                   for key, value in result.items() if key != "ids"}
            merged[item_id] = (col, row)
    return {
        "ids": list(merged),
        "documents": [row.get("documents") for _, row in merged.values()],
        "metadatas": [row.get("metadatas") for _, row in merged.values()],
    }


def delta_merge_status() -> dict:
    """C2 合并触发判定与空间预检。仅观测，不直接在请求线程发起重建。"""
    import os
    base_client = _get_base_client()
    delta_client = _get_delta_client()
    # 先确保 delta 已创建，再读取路由快照；否则首个任务会保存创建 delta 前的
    # epoch，领取时被误判为路由切换而重投。
    base_client = _get_base_client()
    delta_client = _get_delta_client()
    routing = index_registry.ensure_registry()
    delta_id = (routing or {}).get("delta_generation_id")
    generation = index_registry.get_generation(delta_id) if delta_id else None
    if not generation:
        return {"ready": False, "reason": "no_delta"}
    path = Path(generation["path"])
    vectors = documents = 0
    try:
        client = _get_delta_client()
        for name in managed_collection_names():
            try:
                count = int(client.get_collection(name=name).count())
            except Exception:
                count = 0
            vectors += count
            if name == CHROMA_COLLECTION:
                documents = count
    except Exception as exc:
        return {"ready": False, "reason": f"delta_unreadable:{type(exc).__name__}"}
    bytes_used = sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.is_dir() else 0
    age = max(0, int(time.time() - float(generation.get("created_at") or time.time())))
    disk = shutil.disk_usage(path if path.exists() else CHROMA_DATA_DIR)
    free_ratio = disk.free / max(disk.total, 1)
    reasons = []
    if documents >= INDEX_DELTA_MAX_DOCUMENTS: reasons.append("documents")
    if vectors >= INDEX_DELTA_MAX_VECTORS: reasons.append("vectors")
    if age >= INDEX_DELTA_MAX_AGE_SECONDS: reasons.append("age")
    if bytes_used >= INDEX_DELTA_MAX_BYTES: reasons.append("bytes")
    # 合并峰值至少需要一份当前 base 的近似空间；空间不足时不启动。
    base_path, _ = index_registry.active_generation_paths()
    base_bytes = sum(item.stat().st_size for item in base_path.rglob("*") if item.is_file()) if base_path and base_path.is_dir() else 0
    return {"ready": bool(reasons) and free_ratio >= INDEX_MIN_FREE_RATIO and disk.free >= base_bytes,
            "reasons": reasons, "documents": documents, "vectors": vectors, "age_seconds": age,
            "bytes": bytes_used, "base_bytes": base_bytes, "free_bytes": disk.free,
            "free_ratio": round(free_ratio, 4), "space_ok": free_ratio >= INDEX_MIN_FREE_RATIO and disk.free >= base_bytes}


@_tracked_operation
def query_union_collection(name: str, query_embedding: list[float], n_results: int,
                           where: dict | None = None) -> dict:
    """同集合类型内合并 base/delta 候选，按距离统一排序后截断。"""
    rows: dict[str, tuple[str, dict, float]] = {}
    for col in _union_collections(name):
        try:
            result = col.query(query_embeddings=[query_embedding], n_results=max(n_results, 1),
                               where=where, include=["documents", "metadatas", "distances"])
        except Exception as exc:
            raise_if_index_storage_corruption(exc, f"union_query:{name}")
            continue
        for i, item_id in enumerate((result.get("ids") or [[]])[0] or []):
            if item_id in rows:
                continue
            docs = (result.get("documents") or [[]])[0] or []
            metas = (result.get("metadatas") or [[]])[0] or []
            dists = (result.get("distances") or [[]])[0] or []
            rows[item_id] = (docs[i] if i < len(docs) else "", metas[i] if i < len(metas) else {},
                             float(dists[i] if i < len(dists) else 0.0))
    ordered = sorted(rows.items(), key=lambda item: item[1][2])[:n_results]
    return {"ids": [[item_id for item_id, _ in ordered]],
            "documents": [[row[0] for _, row in ordered]],
            "metadatas": [[row[1] for _, row in ordered]],
            "distances": [[row[2] for _, row in ordered]]}


def registered_collection_names() -> list[str]:
    """返回所有经统一工厂登记的集合名（供启动自检枚举）。"""
    with _COLLECTION_LOCK:
        return sorted(_REGISTERED_COLLECTIONS.keys())


# 启动自检结果状态：ok=集合可读；not_created=集合尚未创建（健康空库）；
# corrupted=读取异常（损坏/句柄失效）。
HEALTH_OK = "ok"
HEALTH_NOT_CREATED = "not_created"
HEALTH_CORRUPTED = "corrupted"

# mindos/knowledge_index.COLLECTION_NAME 的镜像常量：不能直接 import 该模块
# （会形成 vector_store → knowledge_index → vector_store 循环依赖），
# 由 test_mindos_p05_factory.py 断言两者一致，防止漂移。
KNOWLEDGE_CARDS_COLLECTION = "mindos_knowledge_cards_v2"


def managed_collection_names() -> list[str]:
    """静态受管集合清单（P0-5 启动自检的枚举依据）。

    记忆/知识卡片集合是惰性创建（首次访问才 get_or_create），启动时通常不会
    进入工厂登记表；自检不能依赖「已触发过工厂」，必须按静态清单枚举。
    新增业务集合时必须同步登记到这里。
    """
    return sorted({
        CHROMA_COLLECTION,          # 原材料文本块
        IMAGE_COLLECTION,           # 图片/视频帧 CLIP 向量
        MEMORY_COLLECTION,          # Agent 记忆（memory_store，惰性创建）
        KNOWLEDGE_CARDS_COLLECTION, # 知识卡片（mindos/knowledge_index，惰性创建）
    })


def _existing_collection_names(client=None) -> set[str]:
    """返回指定 Chroma client 中已实际创建的集合名（兼容 list_collections 返回 str/对象）。"""
    client = client or _get_base_client()
    names: set[str] = set()
    for c in client.list_collections():
        names.add(c if isinstance(c, str) else str(getattr(c, "name", c)))
    return names


def verify_chroma_health() -> dict:
    """启动自检：对 base + delta 双代际的受管集合执行存在性 + count + 最小 get 探测。

    - 集合未创建 → not_created（健康空库，全新数据目录不得误报损坏）；
    - 已创建集合 count/get 抛异常 → corrupted 及稳定说明，注入恢复建议，
      避免让首个用户请求撞上 InternalError；
    - base/delta 任一 client 枚举失败或任一集合读取失败 → corrupted（D4）。
    - 另并入工厂已登记的集合名（覆盖 rebuild 等动态命名集合之外的一切入口）。
    """
    result: dict = {"ok": True, "collections": 0, "checked": [], "issues": [],
                    "base_ok": True, "delta_ok": True}
    index_registry.ensure_registry()
    existing: set[str] = set()
    existing_by_side: dict[str, set[str]] = {}
    clients: list[tuple[str, object]] = []
    probe_clients: list[object] = []
    try:
        clients.append(("base", _get_base_client()))
    except Exception as e:
        result["ok"] = False
        result["base_ok"] = False
        result["issues"].append(f"base 代际 client 初始化失败（{type(e).__name__}）")
    # delta 侧用探测 client（不触发 ensure_delta 替换），让损坏在探测前可见、可记录。
    try:
        delta_probe = _probe_delta_client()
        if delta_probe is not None:
            clients.append(("delta", delta_probe))
            probe_clients.append(delta_probe)
    except Exception as e:
        result["delta_ok"] = False
        result["issues"].append(f"delta 代际 client 初始化失败（{type(e).__name__}）")
    for side, client in clients:
        try:
            side_names = _existing_collection_names(client)
            existing |= side_names
            existing_by_side[side] = side_names
        except Exception as e:
            result[f"{side}_ok"] = False
            if side == "base":
                result["ok"] = False
            result["issues"].append(
                f"ChromaDB list_collections 失败（{type(e).__name__}）：无法枚举集合，"
                "索引疑似损坏或句柄失效。请停止访问 Chroma 的进程，备份 data/chroma_data 后按"
                "《MindOS索引可靠性问题分析与改进方案》§9 恢复流程处理。"
            )
    names = sorted(set(managed_collection_names()) | set(registered_collection_names()))
    for name in names:
        entry = {"name": name, "status": HEALTH_OK, "sides": []}
        if name not in existing:
            # 全新数据目录 / 功能尚未启用：集合尚未创建 = 健康空库，不是损坏
            entry["status"] = HEALTH_NOT_CREATED
            result["checked"].append(entry)
            continue
        for side, client in clients:
            # 该集合在某一侧不存在（如 text/image 只在 base、记忆只在 delta）→ 合法缺失，非损坏
            if name not in existing_by_side.get(side, set()):
                entry["sides"].append(HEALTH_NOT_CREATED)
                continue
            try:
                col = client.get_collection(name=name)
                col.count()
                # 最小真实读取探测：GET 空 where（空集合也不抛错）
                col.get(limit=1, include=["metadatas"])
                entry["sides"].append(HEALTH_OK)
            except Exception as e:
                entry["sides"].append(HEALTH_CORRUPTED)
                entry["status"] = HEALTH_CORRUPTED
                entry["error"] = type(e).__name__
                result[f"{side}_ok"] = False
                if side == "base":
                    result["ok"] = False
                result["issues"].append(
                    f"collection '{name}' 自检失败（{type(e).__name__}）：索引疑似损坏或句柄失效。"
                    "请停止访问 Chroma 的进程，备份 data/chroma_data 后按《MindOS索引可靠性"
                    "问题分析与改进方案》§9 恢复流程处理。"
                )
        result["checked"].append(entry)
    result["collections"] = len(result["checked"])
    # 关闭探测用临时 delta client（base client 由 _get_base_client 缓存，不在此关闭）。
    for probe in probe_clients:
        try:
            close = getattr(probe, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
    _finalize_health_state(result)
    return result


def _finalize_health_state(result: dict) -> None:
    """按自检结果落健康状态（D4）：ok -> healthy；失败 -> corrupted 并持久化注册表。"""
    if result.get("base_ok"):
        set_index_health_state(INDEX_STATE_HEALTHY)
    else:
        issue = (result.get("issues") or [""])[0]
        set_index_health_state(
            INDEX_STATE_CORRUPTED,
            error_code="index_corrupted",
            error_detail=issue[:500] if issue else "ChromaDB 健康自检失败",
        )
        return
    if not result.get("delta_ok"):
        issue = (result.get("issues") or [""])[0]
        routing = index_registry.get_routing() or {}
        corrupted_delta_id = routing.get("delta_generation_id")
        delta_generation = (
            index_registry.get_generation(corrupted_delta_id)
            if corrupted_delta_id else None
        )
        newly_corrupted = not delta_generation or (
            delta_generation.get("status") != index_registry.STATUS_CORRUPTED
        )
        # 单次汇总告警：delta 损坏只降级到 base，绝不逐文件重入队（D4）。
        logger.error(
            "delta 代际自检失败（corrupted）：检索降级到 base，已停止读取/写入该 delta，"
            "下一次写入将创建新 delta 并重放持久化任务。%s",
            (issue[:300] if issue else ""),
        )
        index_registry.mark_active_corrupted(
            error_code="index_corrupted", error_detail=issue[:500], role=index_registry.ROLE_DELTA,
        )
        # base 保持可读；丢弃故障 delta 的缓存，下一次写会 ensure_delta 创建新代。
        global _delta_client, _delta_client_generation_id, _collection, _image_collection
        _delta_client = None
        _delta_client_generation_id = None
        _collection = None
        _image_collection = None
        # 已完成任务作为事实来源重放清单，不能因旧 delta 损坏而永久停在 done。
        # 只回放写入该故障 delta 的任务；base 或其他健康代际中的材料不能因一次
        # 局部损坏被全量重建。不在健康检查中立即替换路由，下一次领取任务时由
        # ensure_delta 创建新代并绑定新 epoch。
        try:
            from mindos.stores.job_store import JobStore
            if corrupted_delta_id and newly_corrupted:
                replayed = JobStore.instance().requeue_done_index_jobs_for_generation(
                    corrupted_delta_id
                )
                logger.warning(
                    "delta 故障恢复已仅回放 %d 个受影响材料任务（generation=%s）",
                    len(replayed), corrupted_delta_id,
                )
            elif not corrupted_delta_id:
                logger.error("delta 故障恢复未找到路由代际，跳过完成任务重放")
            else:
                logger.info(
                    "delta 故障恢复已初始化，跳过重复任务回放（generation=%s）",
                    corrupted_delta_id,
                )
        except Exception as exc:
            logger.error("delta 故障后的持久任务重放初始化失败: %s", type(exc).__name__)


def release_chroma():
    """关闭并释放 ChromaDB client + 全部 collection 句柄，归还索引内存。

    所有 getter 都是懒加载——释放后下次调用自动重建连接与索引，服务功能不受影响。
    P1-3：仅在无活跃操作（active_operations()==0）时才真正 close client，并持有
    _COLLECTION_LOCK 完成释放（禁止释放期间新建查询句柄）；有活跃操作时跳过并
    返回 False，供调用方（空闲卸载循环）下一轮再试。

    Returns:
        bool: True=已释放；False=有活跃操作，跳过（连接保持可用）。
    """
    global _client, _collection, _image_collection
    global _base_client, _delta_client, _base_client_generation_id, _delta_client_generation_id
    with _COLLECTION_LOCK:
        if active_operations() > 0:
            logger.info(
                "P1-3 有 %d 个活跃操作，跳过 Chroma 释放（连接保持可用）",
                active_operations(),
            )
            return False
        base_client = _base_client
        delta_client = _delta_client
        _client = None
        _base_client = None
        _delta_client = None
        _collection = None
        _image_collection = None
    for client in (base_client, delta_client):
        if client is not None:
            try:
                # 类型存根未声明 close，但运行时存在（PersistentClient）
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            except Exception as e:
                logger.warning(f"ChromaDB 释放失败（忽略）: {e}")
    import gc

    gc.collect()
    logger.info("ChromaDB 索引已释放，内存已归还")
    return True


# ======================= P1-2 维护操作与普通查询隔离（索引可靠性方案） =======================
# reindex / schema 迁移不再「先删除线上集合再重建」。改为：把新索引写入单独的
# __rebuild 集合（读仍走旧的当前有效集合，查询零中断），全量校验通过后再原子
# 改名切换（旧集合改名 __obsolete → rebuild 改名为正式名 → 删 __obsolete）；
# 任意失败则删除 rebuild 集合、旧集合原样保留并在线可检索。
#
# 写/读双目标：
#   - 写路径（add_file_chunks / add_image_* / delete_*）与增量跳过校验
#     （get_source_hash）解析到 _write_collection() → 重建期间指向 __rebuild 集合，
#     保证重建能写满、而非被旧索引的 content_hash「已索引跳过」漏文件；
#   - 查询路径（search / list_documents / get_stats）仍走 get_collection() =
#     当前有效集合，重建期间对外继续返回旧索引，禁止把空/半成品集合误报为「无资料」。
# 重建期间 generation 注册表用隔离命名空间（{kind}::rebuild），与活跃集合代数互不干扰。

REBUILD_STATE_IDLE = "idle"
REBUILD_STATE_FULL = "rebuilding"
_REBUILD_LOCK = threading.Lock()
# {逻辑 kind:text/image -> 物理 rebuild 集合名}；非空即表示一次重建在进行中。
_REBUILD_TARGETS: dict[str, str] = {}


def rebuilding() -> bool:
    """是否处于重建中（写目标已被切到 __rebuild 集合）。"""
    with _REBUILD_LOCK:
        return bool(_REBUILD_TARGETS)


def _rebuild_physical_name(base: str) -> str:
    # Chroma collection 名仅允许 alnum/._-，用双下划线作分隔
    return f"{base}__rebuild"


def _gen_namespace(kind: str) -> str:
    """generation 注册表 token。重建期间用隔离命名空间，避免干扰活跃集合的代数。"""
    with _REBUILD_LOCK:
        if _REBUILD_TARGETS:
            return f"{kind}::rebuild"
    return kind


def _write_collection(kind: str):
    """写路径（含增量跳过校验）当前应写入的集合对象。

    C1：平时 == delta 可写集合（base 只读，绝不直接写）；重建期间 == base 的
    __rebuild 集合（重建以 base 为对象）。读取侧用 _union_collections 双代际并集。
    """
    with _REBUILD_LOCK:
        target = _REBUILD_TARGETS.get(kind)
        rebuild_client = _rebuild_client
    if target and rebuild_client is not None:
        return rebuild_client.get_or_create_collection(name=target, metadata=_collection_metadata())
    return get_collection() if kind == "text" else get_image_collection()


def begin_rebuild(rebuild_session_id: str | None = None) -> dict:
    """开启一次全量重建：建好 __rebuild 集合并把写目标切过去，旧集合保持在线可检索。"""
    global _REBUILD_TARGETS, _rebuild_client, _rebuild_generation_id
    with _REBUILD_LOCK:
        if _REBUILD_TARGETS:
            return {"ok": False, "status": REBUILD_STATE_FULL, "error": "already_rebuilding"}
        targets = {"text": CHROMA_COLLECTION, "image": IMAGE_COLLECTION}
        try:
            building = index_registry.create_building_base(rebuild_session_id)
            if not building.get("ok"):
                return {"ok": False, "status": REBUILD_STATE_IDLE, "error": building.get("error")}
            _rebuild_generation_id = building["generation_id"]
            _rebuild_client = chromadb.PersistentClient(
                path=building["path"], settings=Settings(anonymized_telemetry=False)
            )
            # 物理 rebuild 集合与 generation 注册表必须成对清理。否则崩溃遗留的
            # text::rebuild/image::rebuild 代数会在下一次 commit 时覆盖正式代数，
            # 让仍在 active 集合中的材料因代数不匹配而不可见。
            generation_store.clear_collection_token("text::rebuild")
            generation_store.clear_collection_token("image::rebuild")
            # 先在独立 building 目录建集合，全部成功后再切写目标。
            for name in (*targets.values(), MEMORY_COLLECTION, KNOWLEDGE_CARDS_COLLECTION):
                _rebuild_client.get_or_create_collection(name=name, metadata=_collection_metadata())
        except Exception as e:
            _REBUILD_TARGETS.clear()
            logger.error("P1-2 重建预处理失败（未切换写目标，旧集合不受影响）: %s", e)
            return {"ok": False, "status": REBUILD_STATE_IDLE, "error": str(e)}
        _REBUILD_TARGETS = targets
        set_index_health_state(INDEX_STATE_REBUILDING)
        logger.info("P1-2 重建开始：写入 __rebuild 集合，旧集合保持在线可检索")
        return {"ok": True, "status": REBUILD_STATE_FULL}


def resume_rebuild() -> dict:
    """重启后恢复持久化重建会话的写目标，不清空已校验的 __rebuild 内容。"""
    global _REBUILD_TARGETS, _rebuild_client, _rebuild_generation_id
    with _REBUILD_LOCK:
        if _REBUILD_TARGETS:
            return {"ok": False, "status": REBUILD_STATE_FULL, "error": "already_rebuilding"}
        building = index_registry.building_base()
        if not building:
            return {"ok": False, "status": REBUILD_STATE_IDLE, "error": "rebuild_collections_missing"}
        _rebuild_generation_id = building["generation_id"]
        _rebuild_client = chromadb.PersistentClient(path=building["path"], settings=Settings(anonymized_telemetry=False))
        targets = {"text": CHROMA_COLLECTION, "image": IMAGE_COLLECTION}
        if not all(name in _existing_collection_names(_rebuild_client) for name in targets.values()):
            return {"ok": False, "status": REBUILD_STATE_IDLE, "error": "rebuild_collections_missing"}
        _REBUILD_TARGETS = targets
    logger.info("P1-2 恢复未完成重建：继续使用已有 __rebuild 集合")
    return {"ok": True, "status": REBUILD_STATE_FULL}


class _CollectionSwitchError(RuntimeError):
    """集合切换失败（已尽力回滚）：正式集合保持旧数据或已恢复，rebuild 集合保留待 abort。"""


def _legacy_apply_collection_switch(kind: str, active_name: str) -> None:
    """已废弃的 collection rename 实现，仅供历史故障排查；不得在 C1 调用。

    双 rename 不是真原子：第二步（rebuild 上位）失败时正式名会短暂缺失。
    必须回滚（obsolete→正式名）保证「正式名下永远有集合」，旧数据不因
    切换失败而丢失（review 修复：此前第二步失败会让正式集合直接消失）。
    """
    client = _get_base_client()
    with _REBUILD_LOCK:
        rebuild_name = _REBUILD_TARGETS.get(kind)
    if not rebuild_name:
        return
    obsolete = f"{active_name}__obsolete"
    try:
        client.delete_collection(obsolete)  # 清理上一轮可能遗留的 obsolete
    except Exception:
        pass
    active_existed = active_name in _existing_collection_names()
    if active_existed:
        try:
            client.get_collection(active_name).modify(name=obsolete)  # 释放正式名
        except Exception as e:
            # 第一步失败：正式集合未被改动，数据安全；rebuild 集合留给 abort_rebuild 清理
            raise _CollectionSwitchError(
                f"{kind}: 旧集合改名失败（正式集合未动）: {type(e).__name__}: {e}"
            ) from e
    try:
        client.get_collection(rebuild_name).modify(name=active_name)  # 新集合上位
    except Exception as e:
        if not active_existed:
            # 全新库首建：正式名从未存在，无数据可丢；rebuild 集合留给 abort 清理
            raise _CollectionSwitchError(
                f"{kind}: 新集合上位失败（全新库，正式名从未存在）: {type(e).__name__}: {e}"
            ) from e
        try:
            client.get_collection(obsolete).modify(name=active_name)  # 回滚恢复旧集合
        except Exception as rb_err:
            logger.critical(
                "P1-2 切换失败且回滚失败：%s 正式集合缺失！旧数据保留在 %s（%s），"
                "请停止服务并按《MindOS索引可靠性问题分析与改进方案》§9 恢复流程处理",
                kind, obsolete, rb_err,
            )
            raise _CollectionSwitchError(
                f"{kind}: 新集合上位失败（{type(e).__name__}）且回滚失败"
                f"（{type(rb_err).__name__}）：旧数据保留在 {obsolete}，需人工恢复"
            ) from e
        logger.warning("P1-2 新集合上位失败，已回滚保留旧集合（%s）: %s", kind, e)
        raise _CollectionSwitchError(
            f"{kind}: 新集合上位失败，已回滚保留旧集合: {type(e).__name__}: {e}"
        ) from e
    try:
        client.delete_collection(obsolete)  # 旧数据下线
    except Exception:
        logger.warning("P1-2 旧集合删除失败（%s），留为 __obsolete 待清理", obsolete)


def _carry_over_delta_collections(old_clients, new_client) -> list[str]:
    """把旧路由 base+delta 中需保留的集合复制到新 delta。

    全量重建只重建 text/image（base）；memory/knowledge_cards 不是材料重建来源，
    必须在切换前搬运当前路由两侧的既有向量，否则旧 generation 退役后
    记忆与知识卡片不再被 union 读取。old_clients 按 delta、base 顺序传入，
    稳定 ID 冲突时保留 delta 的新记录。
    """
    if not isinstance(old_clients, (list, tuple)):
        old_clients = [old_clients]
    carried: list[str] = []
    for name in (MEMORY_COLLECTION, KNOWLEDGE_CARDS_COLLECTION):
        merged: dict[str, tuple[object, str, dict]] = {}
        collection_seen = False
        for old_client in old_clients:
            if old_client is None:
                continue
            try:
                old_col = old_client.get_collection(name=name)
            except Exception:
                continue
            collection_seen = True
            try:
                res = old_col.get(include=["embeddings", "documents", "metadatas"])
            except Exception as exc:
                raise _CollectionSwitchError(f"carry_read_failed:{name}:{type(exc).__name__}") from exc
            ids = res.get("ids") or []
            embeddings = res.get("embeddings")
            documents = res.get("documents") or [""] * len(ids)
            metadatas = res.get("metadatas") or [{}] * len(ids)
            if embeddings is None or len(embeddings) != len(ids):
                raise _CollectionSwitchError(f"carry_embeddings_invalid:{name}")
            for index, item_id in enumerate(ids):
                if item_id not in merged:
                    merged[str(item_id)] = (embeddings[index], documents[index], metadatas[index])
        if not collection_seen or not merged:
            continue
        ids = list(merged)
        embeddings = [merged[item_id][0] for item_id in ids]
        documents = [merged[item_id][1] for item_id in ids]
        metadatas = [merged[item_id][2] for item_id in ids]
        if not ids:
            continue
        # Knowledge vectors are versioned.  A retired/recycled card or an old
        # version must not be resurrected merely because a base/delta merge
        # copied its physical chunks into the next generation.
        if name == KNOWLEDGE_CARDS_COLLECTION:
            try:
                from mindos.stores import card_ledger_store
                keep = []
                for index, meta in enumerate(metadatas):
                    meta = meta if isinstance(meta, dict) else {}
                    # Opaque legacy/test records cannot be returned by the v2
                    # query filter (they lack a vector version), but retaining
                    # them avoids silently dropping unknown extension data.
                    if "knowledge_id" not in meta or "vector_version" not in meta:
                        keep.append(index)
                        continue
                    try:
                        active = card_ledger_store.should_preserve_vector(
                            str(meta.get("knowledge_id") or ""), int(meta.get("vector_version")),
                        )
                    except (TypeError, ValueError):
                        active = False
                    if active:
                        keep.append(index)
                ids = [ids[index] for index in keep]
                embeddings = [embeddings[index] for index in keep]
                documents = [documents[index] for index in keep]
                metadatas = [metadatas[index] for index in keep]
            except Exception as exc:
                raise _CollectionSwitchError(f"carry_ledger_filter_failed:{type(exc).__name__}") from exc
        if not ids:
            continue
        try:
            new_col = new_client.get_or_create_collection(name=name, metadata=_collection_metadata())
            writer = getattr(new_col, "upsert", None) or new_col.add
            writer(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
            verified = new_col.get(ids=ids, include=["metadatas"])
            if set(verified.get("ids") or []) != set(ids):
                raise _CollectionSwitchError(f"carry_verify_failed:{name}")
        except _CollectionSwitchError:
            raise
        except Exception as exc:
            raise _CollectionSwitchError(f"carry_write_failed:{name}:{type(exc).__name__}") from exc
        carried.append(name)
    if carried:
        logger.info("已搬运 %d 个集合到新 delta: %s", len(carried), ",".join(carried))
    return carried


def commit_rebuild() -> dict:
    """重建全部写入完成后：完整性冒烟校验 → 切换（失败回滚）→ 迁移 generation → 收尾。

    切换失败不落（该 kind 已回滚，旧集合继续在线）；返回 {"ok": False} 供调用方
    abort_rebuild() 清理残留 rebuild 集合与状态。
    """
    global _collection, _image_collection, _base_client, _delta_client, _rebuild_client, _rebuild_generation_id
    with _REBUILD_LOCK:
        if not _REBUILD_TARGETS:
            return {"ok": False, "status": REBUILD_STATE_IDLE, "error": "no_rebuild_in_progress"}
    # ① 冒烟校验：独立 building 目录的两个集合必须可读。
    for kind, active_name in (("text", CHROMA_COLLECTION), ("image", IMAGE_COLLECTION)):
        with _REBUILD_LOCK:
            rebuild_name = _REBUILD_TARGETS.get(kind)
        try:
            col = _rebuild_client.get_collection(rebuild_name)
            col.count()
        except Exception as e:
            logger.error("P1-2 rebuild 集合校验失败（held back，保留旧集合）%s: %s", kind, e)
            return {"ok": False, "status": REBUILD_STATE_FULL,
                    "error": f"rebuild_validation_failed:{kind}:{type(e).__name__}"}
    # ② 切换（持集合锁，其他线程在窗口内不重建集合句柄）。
    # 逐 kind 切换 + 各自 generation 迁移；跨 kind 极端部分提交（text 成功、image 失败）
    # 时 text 已是新集合（含全量数据）、image 保持旧集合，两者均在线可读、数据不丢，
    # 返回 not ok 由调用方 abort 清理 image 的 rebuild 残留。
    with _ROUTING_LOCK, _COLLECTION_LOCK:
        _collection = None
        _image_collection = None
        try:
            # 只读探测当前（旧）delta，不触发 ensure_delta 替换（损坏时才能先切换再处理）。
            old_delta_client = _probe_delta_client()
            old_base_client = _get_base_client()
            # 目录级提交：先创建并校验新 delta，再在 registry 内原子切换路由。
            new_delta = index_registry.create_delta(force_new=True)
            if not new_delta.get("ok"):
                raise _CollectionSwitchError("delta_create_failed")
            if old_delta_client is not None or old_base_client is not None:
                # 搬运旧路由 base+delta 的记忆/知识卡片向量到新 delta。
                new_delta_client = chromadb.PersistentClient(
                    path=new_delta["delta_path"], settings=Settings(anonymized_telemetry=False))
                try:
                    _carry_over_delta_collections([old_delta_client, old_base_client], new_delta_client)
                finally:
                    # old_base_client is the live cached read client; keep it
                    # open if the route switch fails so the old generation can
                    # continue serving queries.
                    for _c in (old_delta_client, new_delta_client):
                        try:
                            close = getattr(_c, "close", None)
                            if callable(close):
                                close()
                        except Exception:
                            pass
            if not index_registry.activate_base(_rebuild_generation_id, new_delta["delta_generation_id"]):
                raise _CollectionSwitchError("directory_route_switch_failed")
            generation_store.rename_collection("text::rebuild", generation_store.COLLECTION_TEXT)
            generation_store.rename_collection("image::rebuild", generation_store.COLLECTION_IMAGE)
        except _CollectionSwitchError as e:
            logger.error("P1-2 重建提交中止（切换失败，该集合已回滚/保持旧数据）: %s", e)
            return {"ok": False, "status": REBUILD_STATE_FULL,
                    "error": f"switch_failed:{e}"}
        with _REBUILD_LOCK:
            # 重建集合名已切为正式名（documents/images_clip），从登记表移除 __rebuild 名
            for n in set(_REBUILD_TARGETS.values()):
                _REGISTERED_COLLECTIONS.pop(n, None)
            _REBUILD_TARGETS.clear()
            _rebuild_client = None
            _rebuild_generation_id = None
        _base_client = None
        _delta_client = None
        _base_client_generation_id = None
        _delta_client_generation_id = None
        _invalidate_document_cache()
    logger.info("P1-2 重建提交成功：已切换到新集合")
    # 阶段B：提交后复检健康闸门（成功置 healthy，异常回落 corrupted 并持久化）。
    try:
        verify_chroma_health()
    except Exception as e:
        logger.warning("P1-2 重建后健康复检异常: %s", e)
        set_index_health_state(INDEX_STATE_UNKNOWN)
    return {"ok": True, "status": REBUILD_STATE_IDLE}


def abort_rebuild() -> dict:
    """重建失败/取消：删除 __rebuild 集合，丢弃隔离命名空间，旧集合保持在线。"""
    global _rebuild_client, _rebuild_generation_id
    with _REBUILD_LOCK:
        targets = dict(_REBUILD_TARGETS)
        _REBUILD_TARGETS.clear()
        client = _rebuild_client
        generation_id = _rebuild_generation_id
        _rebuild_client = None
        _rebuild_generation_id = None
    if client is not None:
        try:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
    if generation_id:
        generation = index_registry.get_generation(generation_id)
        if generation:
            try:
                shutil.rmtree(generation["path"], ignore_errors=True)
            except Exception:
                pass
        index_registry.set_generation_status(generation_id, index_registry.STATUS_RETIRED)
    for name in set(targets.values()):
        _REGISTERED_COLLECTIONS.pop(name, None)
    # 清理重建命名空间的 generation 残留（指向已丢弃的集合）
    generation_store.clear_collection_token("text::rebuild")
    generation_store.clear_collection_token("image::rebuild")
    # 阶段B：中止后回到 unknown，由下次自检/复检重新判定健康状态。
    set_index_health_state(INDEX_STATE_UNKNOWN)
    logger.info("P1-2 重建已中止：__rebuild 集合已清理，旧集合保持在线")
    return {"ok": True, "status": REBUILD_STATE_IDLE}


def rebuild_status() -> str:
    """对外暴露的维护状态：idle / rebuilding（空/半成品集合绝不对外误报为无资料）。"""
    return REBUILD_STATE_FULL if rebuilding() else REBUILD_STATE_IDLE


def get_collection():
    """delta 文本集合（写路径主集合；base 只读侧由 _union_collections 并集读取）。"""
    global _collection
    if _collection is None:
        with _COLLECTION_LOCK:
            if _collection is None:
                col = get_delta_collection(CHROMA_COLLECTION)
                logger.info(f"ChromaDB delta 已连接, 向量块数: {col.count()}")
                _collection = col
    return _collection


def get_image_collection():
    """delta 视觉检索集合（Chinese-CLIP 向量，与文本 BGE 空间隔离）"""
    global _image_collection
    if _image_collection is None:
        with _COLLECTION_LOCK:
            if _image_collection is None:
                _image_collection = get_delta_collection(IMAGE_COLLECTION)
    return _image_collection


def needs_migration() -> bool:
    """检测库中数据是否与当前 schema / 嵌入模型不一致（需清库重建）。
    换嵌入模型（如 bge-small→bge-m3）会导致向量空间不兼容，必须重建。
    跨 base+delta 采样：base（存量）与 delta（新写）任一不匹配即需迁移。"""
    for col in _union_collections(CHROMA_COLLECTION):
        try:
            if col.count() == 0:
                continue
            # 采样多条：任一条 schema/模型不符即需迁移（单条采样可能恰好命中新写入的块而漏判）
            sample = col.get(limit=16, include=["metadatas"])
            metas = sample.get("metadatas") or []
        except Exception:
            continue
        for m in metas:
            if m.get("schema_version") != SCHEMA_VERSION or m.get("model_id") != TEXT_MODEL_ID:
                return True
    return False


# 这些 metadata 键以数值（int/float）入库，不走 str()——保留未来数值过滤/排序能力
# （如 where={"start_time": {"$gte": 60}}）。其余键统一 str() 以兼容旧数据。
_NUMERIC_KEYS = {"start_time", "end_time", "chunk_index", "chunk_count", "frame_index", "frame_count", "generation"}


# ======================= P0-2 原子替换：generation 机制（索引可靠性方案） =======================
# 更新流程：清残留代 → 快照旧代 ids → 写新代（id 带 g{gen}）→ 校验新代 →
# 切注册表 → 删旧代（+旧帧目录）。任一步失败旧索引原样保留、仍可检索（§7.1/§7.2）。
# 存量兼容：注册表无记录（gen=0）的旧数据无 generation 字段，读取口径与改造前一致。

def _gen_of(meta: dict | None) -> int:
    """记录所属 generation；无字段（迁移前存量）视为 0。"""
    try:
        return int((meta or {}).get("generation") or 0)
    except (TypeError, ValueError):
        return 0


def _current_where(kind: str, source_path: str) -> dict:
    """构造「当前有效代」的 where 条件。

    gen=0（存量/未注册）→ 旧式口径（仅 source_path，可命中无 generation 字段的记录）；
    gen>=1 → $and 按代过滤，新旧代共存时只读当前代。
    重建期间用隔离命名空间，读取目标（__rebuild）与注册表 token 保持一致。
    """
    gen = generation_store.current_generation(_gen_namespace(kind), source_path)
    if gen <= 0:
        return {"source_path": source_path}
    return {"$and": [{"source_path": source_path}, {"generation": gen}]}


def _keep_current(meta: dict | None, gens: dict[str, int]) -> bool:
    """检索结果过滤：metadata 的代数须等于该源当前有效代（孤儿旧代不得外泄）。"""
    src = (meta or {}).get("source_path")
    return _gen_of(meta) == gens.get(src, 0)


def _atomic_prepare(col, kind: str, source_path: str) -> list[str]:
    """原子替换前置：删掉非当前代残留，返回当前代记录 ids（留待切换后删除）。

    残留来源：上次写入校验/切换失败的新代、切换成功但删旧失败的旧代。
    它们不参与读取（读取按当前代过滤），留着只占空间，写入前先清。
    """
    current = generation_store.current_generation(kind, source_path)
    res = col.get(where={"source_path": source_path}, include=["metadatas"])
    keep_ids: list[str] = []
    stale_ids: list[str] = []
    for cid, meta in zip(res.get("ids") or [], res.get("metadatas") or []):
        (keep_ids if _gen_of(meta) == current else stale_ids).append(cid)
    if stale_ids:
        col.delete(ids=stale_ids)
    return keep_ids


def _atomic_prepare_frames(
    col, source_path: str, keep_dirs: set[str], kind: str = "image"
) -> tuple[list[str], set[str]]:
    """图片集合版原子替换前置：额外收集当前代帧目录（切换后按需清理）。

    keep_dirs = 即将写入的新代帧目录：残留代的帧目录若与之相同（同内容重索引、
    帧文件刚被 extract_frames 重写到同一目录）绝不能删，否则新代引用落空。
    """
    current = generation_store.current_generation(_gen_namespace(kind), source_path)
    res = col.get(where={"source_path": source_path}, include=["metadatas"])
    old_ids: list[str] = []
    stale_ids: list[str] = []
    old_frame_dirs: set[str] = set()
    stale_frame_dirs: set[str] = set()
    for cid, meta in zip(res.get("ids") or [], res.get("metadatas") or []):
        fp = (meta or {}).get("frame_path")
        d = str(Path(fp).resolve().parent) if fp else None
        if _gen_of(meta) == current:
            old_ids.append(cid)
            if d:
                old_frame_dirs.add(d)
        else:
            stale_ids.append(cid)
            if d:
                stale_frame_dirs.add(d)
    if stale_ids:
        col.delete(ids=stale_ids)
        _cleanup_frame_dirs(stale_frame_dirs - old_frame_dirs - keep_dirs)
    return old_ids, old_frame_dirs


def _cleanup_frame_dirs(dirs: set[str]) -> None:
    """安全清理帧目录：仅删 video_frames/ 之内的目录（与 delete_image 同口径）。"""
    for d in dirs:
        try:
            p = Path(d)
            if p.is_relative_to(Path(VIDEO_FRAMES_DIR).resolve()):
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


def _verify_new_generation(
    col,
    source_path: str,
    new_gen: int,
    expected: int,
    index_key: str | None,
    require_document: bool = True,
) -> bool:
    """校验新代写入完整性（P0-3：不只看 metadata，逐条验证 document 与 embedding）。

    - 记录数（ids/metadatas/documents/embeddings 对齐后）恰为 expected；
    - 多记录时序号连续无重复；
    - 每条 document 非 None（require_document 时还须非空——文本块/帧路径必填；
      纯图记录的 document 是 file_name 装饰字段，允许为空串）；
    - 每条 embedding 非空且批次内维度一致。
    """
    try:
        res = col.get(
            where={"$and": [{"source_path": source_path}, {"generation": new_gen}]},
            include=["metadatas", "documents", "embeddings"],
        )
    except Exception as e:
        logger.warning(
            "新代校验读取失败 %s g%d: %s", source_path, new_gen, type(e).__name__
        )
        return False
    metas = [m for m in (res.get("metadatas") or []) if isinstance(m, dict)]
    docs = [d for d in (res.get("documents") or [])]
    embs_raw = res.get("embeddings")
    embs = list(embs_raw) if embs_raw is not None else []
    if len(metas) != expected or len(docs) != expected or len(embs) != expected:
        return False
    for d in docs:
        if not isinstance(d, str):
            return False
        if require_document and not d.strip():
            return False
    dims: set[int] = set()
    for e in embs:
        try:
            dim = len(e)
        except TypeError:
            return False
        if dim <= 0:
            return False
        dims.add(dim)
    if len(dims) > 1:
        return False  # 批内维度不一致 → 写入异常
    if expected > 1 and index_key:
        try:
            indexes = sorted(int(m.get(index_key)) for m in metas)
        except (TypeError, ValueError):
            return False
        if indexes != list(range(expected)):
            return False
    return True


def _delete_old_generation_with_retry(col, source_path: str, old_ids: list[str]) -> bool:
    """旧代记录清理（P0-2）：失败短暂重试，最终失败留待下次写入前 purge。

    固定单次删除失败会让孤儿旧代长期占据检索召回窗口（P0-2 读取过滤的代价），
    这里指数退避重试 3 次；仍失败则告警并依赖 _atomic_prepare 的下次写入前清理。
    """
    for attempt in range(3):
        try:
            col.delete(ids=old_ids)
            return True
        except Exception as e:
            if attempt >= 2:
                logger.warning(
                    "旧代清理重试后仍失败（孤儿记录不影响读取，下次写入前 purge）%s: %s",
                    source_path, e,
                )
            else:
                time.sleep(0.1 * (2 ** attempt))
    return False


def _delete_new_generation(col, source_path: str, new_gen: int) -> None:
    """新代校验失败时清理其残留记录（尽力而为；失败留待下次写入前 purge）。"""
    try:
        col.delete(where={"$and": [{"source_path": source_path}, {"generation": new_gen}]})
    except Exception:
        pass
# ====================================================================================


@_tracked_operation
def add_file_chunks(
    source_path: str,
    file_type: str,
    chunks: list[str],
    embeddings: list[list[float]],
    base_metadata: dict,
    per_chunk_metadata: list[dict] | None = None,
) -> bool:
    """以「源文件」为单位原子写入其全部分块（P0-2）。

    流程：清残留代 → 写新代（id={source_path}::g{gen}::{i}）→ 校验（数量+
    chunk_index 连续）→ 切 generation 注册表 → 删旧代。写入/校验/切代任一步
    失败，旧索引原样保留、仍可检索；重试成功后才完成替换（方案 §7.1）。
    per_chunk_metadata（可选，长度须 == len(chunks)）给每块附加独立 metadata，
    用于视频转写/帧OCR 块各带自己的 start_time/modality。
    """
    _check_index_writable()
    if not chunks or not embeddings:
        return False
    try:
        col = _write_collection("text")
        # ① 清非当前代残留，快照当前代 ids（切换成功后才删）
        old_ids = _atomic_prepare(col, _gen_namespace("text"), source_path)
        # ② 写入新代（与旧代 id 不同，可共存）
        new_gen = generation_store.next_generation(_gen_namespace("text"), source_path)
        n = len(chunks)
        ids = [f"{source_path}::g{new_gen}::{i}" for i in range(n)]
        metadatas = []
        for i in range(n):
            # base_metadata 全 str()，并跳过 None（Chroma 不接受 None 值）
            m = {k: str(v) for k, v in base_metadata.items() if v is not None}
            if per_chunk_metadata:
                for k, v in per_chunk_metadata[i].items():
                    if v is None:
                        continue
                    m[k] = v if k in _NUMERIC_KEYS else str(v)
            m.update(
                {
                    "file_type": file_type,
                    "source_path": source_path,
                    "chunk_index": i,
                    "chunk_count": n,
                    "modality": m.get("modality", "text"),  # 默认 text；视频块传 transcript/ocr
                    "schema_version": SCHEMA_VERSION,
                    "model_id": TEXT_MODEL_ID,
                    "generation": new_gen,
                }
            )
            metadatas.append(m)

        col.add(
            ids=ids,
            embeddings=embeddings,
            documents=[c[:2000] for c in chunks],
            metadatas=metadatas,
        )
        # ③ 校验新代完整性（数量 + chunk_index 连续无重复）
        if not _verify_new_generation(col, source_path, new_gen, n, "chunk_index"):
            logger.error(f"新代校验失败，保留旧索引: {source_path} (g{new_gen}, 期望 {n} 块)")
            _delete_new_generation(col, source_path, new_gen)
            return False
        # ④ 切换注册表（此后全部读取只见新代）
        generation_store.set_generation(_gen_namespace("text"), source_path, new_gen)
        # ⑤ 删旧代（新代已生效；失败重试后仍失败仅留孤儿记录，下次写入前 purge）
        if old_ids:
            _delete_old_generation_with_retry(col, source_path, old_ids)
        logger.info(f"已索引: {base_metadata.get('file_name', source_path)} ({n} 块, g{new_gen})")
        _invalidate_lexical()
        _invalidate_document_cache()
        return True
    except Exception as e:
        raise_if_index_storage_corruption(e, "add_file_chunks")
        logger.error(f"索引失败 {source_path}: {e}")
        return False


@_tracked_operation
def add_image_frames(
    source_path: str,
    embeddings: list[list[float]],
    per_frame_metadata: list[dict],
    base_metadata: dict,
) -> bool:
    """写一个视频的 K 个关键帧视觉向量（P0-2 原子替换）。

    流程：清残留代（连带其孤儿帧目录）→ 写新代（id={source_path}::g{gen}::frame::{k}）
    → 校验（数量+frame_index 连续）→ 切注册表 → 删旧代记录 + 旧帧目录。
    任一步失败旧帧索引保留；新代失败时刚抽的帧目录（旧代未引用者）一并清理，
    不产生孤儿 jpg（方案 §7.2）。
    """
    _check_index_writable()
    if not embeddings:
        return False
    index_registry.clear_tombstone(source_path)
    try:
        col = _write_collection("image")
        # 新代帧目录集合：残留代清理时不能误删（帧文件已被本轮 extract 写入其中）
        new_frame_dirs: set[str] = set()
        for pm in per_frame_metadata:
            fp = pm.get("frame_path")
            if fp:
                new_frame_dirs.add(str(Path(fp).resolve().parent))
        # ① 清残留代（保留与新代同目录的帧文件），快照当前代 ids + 帧目录
        old_ids, old_frame_dirs = _atomic_prepare_frames(col, source_path, new_frame_dirs, "image")
        # ② 写入新代
        new_gen = generation_store.next_generation(_gen_namespace("image"), source_path)
        n = len(embeddings)
        ids = [f"{source_path}::g{new_gen}::frame::{k}" for k in range(n)]
        metadatas = []
        for k in range(n):
            m = {kk: str(vv) for kk, vv in base_metadata.items() if vv is not None}
            for kk, vv in per_frame_metadata[k].items():
                if vv is None:
                    continue
                m[kk] = vv if kk in _NUMERIC_KEYS else str(vv)
            m.update(
                {
                    "file_type": "video",
                    "modality": "frame",
                    "source_path": source_path,
                    "frame_index": k,
                    "frame_count": n,
                    "schema_version": SCHEMA_VERSION,
                    "generation": new_gen,
                }
            )
            metadatas.append(m)
        # ③ 写入并校验新代完整性（add 异常或校验失败 → 清新代残留与孤儿帧目录，旧帧保留）
        try:
            col.add(
                ids=ids,
                embeddings=embeddings,
                documents=[str(per_frame_metadata[k].get("frame_path", "")) for k in range(n)],
                metadatas=metadatas,
            )
            verified = _verify_new_generation(col, source_path, new_gen, n, "frame_index")
        except Exception as e:
            raise_if_index_storage_corruption(e, "add_image_frames")
            logger.error(f"视频帧视觉索引失败 {source_path}: {e}")
            verified = False
        if not verified:
            logger.error(f"新代帧校验失败，保留旧帧索引: {source_path} (g{new_gen}, 期望 {n} 帧)")
            _delete_new_generation(col, source_path, new_gen)
            # 新代失败：刚抽的帧目录若旧代不引用则清理，避免孤儿 jpg
            _cleanup_frame_dirs(new_frame_dirs - old_frame_dirs)
            return False
        # ④ 切换注册表
        generation_store.set_generation(_gen_namespace("image"), source_path, new_gen)
        # ⑤ 删旧代记录 + 旧帧目录（新代未引用者；同目录复用时不动）
        if old_ids:
            _delete_old_generation_with_retry(col, source_path, old_ids)
        _cleanup_frame_dirs(old_frame_dirs - new_frame_dirs)
        logger.info(f"已索引视频帧: {base_metadata.get('file_name', source_path)} ({n} 帧, g{new_gen})")
        _invalidate_document_cache()
        return True
    except Exception as e:
        raise_if_index_storage_corruption(e, "add_image_frames")
        logger.error(f"视频帧视觉索引失败 {source_path}: {e}")
        return False


def _invalidate_lexical() -> None:
    """文本集合变更后让 BM25 索引失效（下次检索惰性重建）"""
    try:
        import lexical
        lexical.invalidate()
    except Exception:
        pass


def _read_effective(
    kind: str,
    source_path: str,
    include: tuple = ("metadatas", "documents", "embeddings"),
) -> dict | None:
    """跨 base+delta 读取某源的「当前有效代」记录（C1 union；任一侧 read_error 视为整体失败）。

    返回合并后的 {"ids","metadatas","documents","embeddings"}；任一侧读取失败返回 None。
    """
    col_name = CHROMA_COLLECTION if kind == "text" else IMAGE_COLLECTION
    ns = generation_store.COLLECTION_TEXT if kind == "text" else generation_store.COLLECTION_IMAGE
    where = _current_where(ns, source_path)
    out: dict = {"ids": [], "metadatas": [], "documents": [], "embeddings": []}
    for col in _union_collections(col_name):
        try:
            res = col.get(where=where, include=list(include))
        except Exception as e:
            record_index_operation_failure(e, f"read_effective_{kind}")
            logger.warning(
                "完整性校验读取失败（%s）source=%s: %s", kind, source_path, type(e).__name__
            )
            return None
        for key in out:
            # Chroma 在部分版本中将 embeddings 返回为 numpy.ndarray。
            # ndarray 不能参与布尔判断（``value or []`` 会抛 ValueError），
            # 因此只将 None 视作缺失值。
            values = res.get(key)
            if values is not None:
                out[key].extend(values)
    return out


def _verify_text_chunks(source_path: str, expected_hash: str | None) -> tuple[str, str | None]:
    """校验文本集合中某源的块完整性（base+delta union）。返回 (status, content_hash)。

    status: VERIFY_OK / VERIFY_NOT_INDEXED / VERIFY_INTEGRITY_FAILED / VERIFY_READ_ERROR
    校验规则（P0-3）：chunk_index 从 0 连续无重复；每块 chunk_count 与实际数一致；
    各块 content_hash 一致；expected_hash 给定时须一致；每块 document 非空、
    embedding 非空且维度一致（有效 document/embedding 是 P0-3 的明确要求）。
    """
    res = _read_effective("text", source_path)
    if res is None:
        return VERIFY_READ_ERROR, None
    metas = [m for m in (res.get("metadatas") or []) if isinstance(m, dict)]
    if not metas:
        return VERIFY_NOT_INDEXED, None

    n = len(metas)
    docs = list(res.get("documents") or [])
    embs_raw = res.get("embeddings")
    embs = list(embs_raw) if embs_raw is not None else []
    if len(docs) != n or len(embs) != n:
        return VERIFY_INTEGRITY_FAILED, None  # documents/embeddings 与记录数不对齐
    for d in docs:
        if not isinstance(d, str) or not d.strip():
            return VERIFY_INTEGRITY_FAILED, None  # document 缺失/为空 → 记录损坏
    dims: set[int] = set()
    for e in embs:
        try:
            dim = len(e)
        except TypeError:
            return VERIFY_INTEGRITY_FAILED, None
        if dim <= 0:
            return VERIFY_INTEGRITY_FAILED, None
        dims.add(dim)
    if len(dims) > 1:
        return VERIFY_INTEGRITY_FAILED, None  # 同一源的块维度必须一致

    indexes: list[int] = []
    hashes: set = set()
    for m in metas:
        try:
            indexes.append(int(m.get("chunk_index")))
        except (TypeError, ValueError):
            return VERIFY_INTEGRITY_FAILED, None  # chunk_index 缺失/非数值 → 记录损坏
        try:
            if int(m.get("chunk_count", -1)) != n:
                return VERIFY_INTEGRITY_FAILED, None  # 部分写入：声明数量与实际不符
        except (TypeError, ValueError):
            return VERIFY_INTEGRITY_FAILED, None
        h = m.get("content_hash")
        if h:
            hashes.add(str(h))
    # chunk_index 必须恰为 0..n-1（连续无重复、无缺块）
    if sorted(indexes) != list(range(n)):
        return VERIFY_INTEGRITY_FAILED, None
    # 同一源的所有块必须携带同一个 content_hash
    if len(hashes) != 1:
        return VERIFY_INTEGRITY_FAILED, None
    content_hash = hashes.pop()
    if expected_hash is not None and content_hash != expected_hash:
        return VERIFY_INTEGRITY_FAILED, content_hash
    return VERIFY_OK, content_hash


def _verify_image_record(source_path: str) -> tuple[str, str | None]:
    """校验图片集合中某源（纯图/视频帧）的记录完整性（P0-3；base+delta union）。

    - 纯图（单条记录）：存在 + content_hash + 有效 embedding；
    - 视频帧（多条记录）：读取全部帧而非只看第一条——frame_index 从 0 连续
      无重复；frame_count（新写入声明）与实际数量一致；所有记录 content_hash
      一致；每条 embedding 非空且维度一致。
    """
    # 按 source_path 查（视频多帧 id 是 {path}::g{gen}::frame::k，不能按 ids=[path] 查，
    # 否则纯无音轨视频的 hash 永远查不到 → 每次重启都重抽全部帧）；
    # P0-2：按当前有效代过滤，删旧失败留下的孤儿帧记录不参与校验
    res = _read_effective("image", source_path)
    if res is None:
        return VERIFY_READ_ERROR, None
    metas = [m for m in (res.get("metadatas") or []) if isinstance(m, dict)]
    if not metas:
        return VERIFY_NOT_INDEXED, None
    n = len(metas)
    docs = list(res.get("documents") or [])
    embs_raw = res.get("embeddings")
    embs = list(embs_raw) if embs_raw is not None else []
    if len(docs) != n or len(embs) != n:
        return VERIFY_INTEGRITY_FAILED, None
    dims: set[int] = set()
    for e in embs:
        try:
            dim = len(e)
        except TypeError:
            return VERIFY_INTEGRITY_FAILED, None
        if dim <= 0:
            return VERIFY_INTEGRITY_FAILED, None
        dims.add(dim)
    if len(dims) > 1:
        return VERIFY_INTEGRITY_FAILED, None
    hashes: set[str] = set()
    for m in metas:
        h = m.get("content_hash")
        if not h:
            return VERIFY_INTEGRITY_FAILED, None  # 帧/图记录缺 content_hash
        hashes.add(str(h))
    if len(hashes) != 1:
        return VERIFY_INTEGRITY_FAILED, None  # 同一源的记录 content_hash 必须一致
    if n > 1:
        # 多条记录 = 视频帧：序号连续 + 声明数量一致（legacy 无 frame_count 只查连续性）
        try:
            idxs = sorted(int(m.get("frame_index")) for m in metas)
        except (TypeError, ValueError):
            return VERIFY_INTEGRITY_FAILED, None
        if idxs != list(range(n)):
            return VERIFY_INTEGRITY_FAILED, None
        declared = metas[0].get("frame_count")
        if declared is not None:
            try:
                if int(declared) != n:
                    return VERIFY_INTEGRITY_FAILED, None
            except (TypeError, ValueError):
                return VERIFY_INTEGRITY_FAILED, None
    return VERIFY_OK, hashes.pop()


@_tracked_operation
def verify_source_index(
    source_path: str,
    expected_hash: str | None = None,
    expected_chunk_count: int | None = None,
) -> str:
    """索引完整性校验（P0-3 统一入口）。

    返回 VERIFY_OK / VERIFY_NOT_INDEXED / VERIFY_INTEGRITY_FAILED / VERIFY_READ_ERROR。
    文本集合优先（文本/OCR/转写块），纯图/纯帧视频回落图片集合。
    integrity_failed 与 read_error 对调用方语义相同：不可信，需触发安全重建，
    不得判为「内容未变」。expected_chunk_count 给定时额外校验块数。
    """
    if index_registry.is_tombstoned(source_path):
        return VERIFY_NOT_INDEXED
    status, content_hash = _verify_text_chunks(source_path, expected_hash)
    if status == VERIFY_NOT_INDEXED:
        # 纯图/视频帧只在图片集合
        img_status, _ = _verify_image_record(source_path)
        if img_status == VERIFY_OK:
            return VERIFY_OK
        if img_status == VERIFY_READ_ERROR:
            return VERIFY_READ_ERROR
        return VERIFY_NOT_INDEXED if img_status == VERIFY_NOT_INDEXED else VERIFY_INTEGRITY_FAILED
    if status == VERIFY_OK and expected_chunk_count is not None:
        res = _read_effective("text", source_path, include=("metadatas",))
        if res is None:
            return VERIFY_READ_ERROR
        actual = len(res.get("metadatas") or [])
        if actual != expected_chunk_count:
            return VERIFY_INTEGRITY_FAILED
    return status


def get_source_hash(source_path: str) -> str | None:
    """读取某源文件已存的 content_hash——仅在完整性校验通过时返回（P0-3）。

    integrity_failed / read_error / not_indexed 一律返回 None：
    watcher 的增量判断拿到 None 就会重新走完整索引（安全重建），
    绝不会把「半成品索引」误判为「内容未变，跳过」。
    """
    status, content_hash = _verify_text_chunks(source_path, None)
    if status == VERIFY_OK:
        return content_hash
    if status == VERIFY_NOT_INDEXED:
        img_status, img_hash = _verify_image_record(source_path)
        if img_status == VERIFY_OK:
            return img_hash
        if img_status == VERIFY_INTEGRITY_FAILED:
            logger.warning("图片集合记录不完整，触发安全重建: %s", source_path)
            return None
        if img_status == VERIFY_READ_ERROR:
            return None
        return None
    # integrity_failed / read_error：返回 None 触发安全重建
    logger.warning(
        "文本索引完整性校验未通过（%s），hash 判定失效、触发安全重建: %s",
        status, source_path,
    )
    return None


@_tracked_operation
def add_image_vector(source_path: str, embedding: list[float], base_metadata: dict) -> bool:
    """写入图片的 CLIP 视觉向量（P0-2 原子替换；id={source_path}::g{gen}，每图一条）。

    写新代 → 校验 → 切注册表 → 删旧代；失败时旧视觉向量原样保留。
    """
    _check_index_writable()
    if not embedding:
        return False
    index_registry.clear_tombstone(source_path)
    try:
        col = _write_collection("image")
        old_ids = _atomic_prepare(col, _gen_namespace("image"), source_path)
        new_gen = generation_store.next_generation(_gen_namespace("image"), source_path)
        m = {k: str(v) for k, v in base_metadata.items() if v is not None}
        m.update({"file_type": "image", "modality": "image", "source_path": source_path,
                  "schema_version": SCHEMA_VERSION, "generation": new_gen})
        col.add(
            ids=[f"{source_path}::g{new_gen}"],
            embeddings=[embedding],
            documents=[str(base_metadata.get("file_name", ""))],
            metadatas=[m],
        )
        # 纯图记录的 document 是 file_name（装饰字段，允许为空），只强制 embedding 有效
        if not _verify_new_generation(col, source_path, new_gen, 1, None, require_document=False):
            logger.error(f"新代视觉向量校验失败，保留旧索引: {source_path} (g{new_gen})")
            _delete_new_generation(col, source_path, new_gen)
            return False
        generation_store.set_generation(_gen_namespace("image"), source_path, new_gen)
        if old_ids:
            _delete_old_generation_with_retry(col, source_path, old_ids)
        _invalidate_document_cache()
        return True
    except Exception as e:
        raise_if_index_storage_corruption(e, "add_image_vector")
        logger.error(f"视觉索引失败 {source_path}: {e}")
        return False


def _query_current_generation(
    cols,
    query_embedding: list[float],
    n_results: int,
    collection_ns: str,
    where: dict | None = None,
) -> list[tuple[str, str, dict, float]]:
    """跨 base+delta 双代际、按当前有效代过滤的检索（C1 union），并自动扩大召回。

    固定「取 2×n_results 再内存过滤」在孤儿旧代恰好占据前排候选时，会把有效代
    结果挤出召回窗口（新代存在却检索不到）。这里改为指数扩样：2× → 4× → …，
    直至过滤后数量 ≥ n_results，或 fetch 已覆盖全部集合（耗尽条件）。
    同时按 tombstones 过滤删除材料，跨代际按 id 去重后按距离排序截断。

    cols: 单个集合或 [base_collection, delta_collection]。
    返回 (id, document, metadata, distance) 元组列表。
    """
    col_list = list(cols) if isinstance(cols, (list, tuple)) else [cols]
    ensure_index_readable()

    def _count(c) -> int:
        try:
            return int(c.count())
        except Exception as exc:
            raise_if_index_storage_corruption(exc, "query_count")
            return 0

    total = sum(_count(c) for c in col_list)
    if total <= 0:
        return []
    gens = generation_store.current_generations(collection_ns)
    tombstones = index_registry.list_tombstones()
    fetch = min(max(n_results * 2, 1), total)
    while True:
        kept: list[tuple[str, str, dict, float]] = []
        seen: set[str] = set()
        for col in col_list:
            n = _count(col)
            if n <= 0:
                continue
            try:
                res = col.query(
                    query_embeddings=[query_embedding],
                    n_results=min(fetch, n),
                    where=where,
                    include=["documents", "metadatas", "distances"],
                )
            except Exception as exc:
                # 单侧读取失败：跳过该侧（degrade），存储层故障统一转 corrupted
                if is_index_storage_corruption(exc):
                    raise_if_index_storage_corruption(exc, "query")
                continue
            ids = (res.get("ids") or [[]])[0] or []
            docs = (res.get("documents") or [[]])[0] or []
            metas = (res.get("metadatas") or [[]])[0] or []
            dists = (res.get("distances") or [[]])[0] or []
            for i, sid in enumerate(ids):
                meta = metas[i] if i < len(metas) and metas[i] else {}
                src = meta.get("source_path") or sid.split("::")[0]
                if src in tombstones:
                    continue
                if gens and not _keep_current(meta, gens):
                    continue
                if sid in seen:
                    continue
                seen.add(sid)
                kept.append((sid, docs[i] if i < len(docs) else "", meta,
                             dists[i] if i < len(dists) else 0.0))
        kept.sort(key=lambda r: float(r[3]))
        # 补满 n_results，或已取全集合（无更多候选可扩）即终止
        if len(kept) >= n_results or fetch >= total:
            return kept[:n_results]
        fetch = min(fetch * 2, total)


@_tracked_operation
def search_images(query_embedding: list[float], n_results: int = 10) -> list[dict]:
    """CLIP 视觉检索（图片集合，base+delta union）。

    C1：按当前有效代过滤（删旧失败的孤儿记录不得进入结果），tombstone 过滤删除项。
    """
    rows = _query_current_generation(
        _union_collections(IMAGE_COLLECTION), query_embedding, n_results,
        generation_store.COLLECTION_IMAGE,
    )
    items = []
    for sid, doc, meta, dist in rows:
        items.append({
            "id": sid,
            "source_path": meta.get("source_path", sid.split("::")[0]),
            "frame_path": meta.get("frame_path"),     # 纯图为 None；视频帧为帧文件路径
            "start_time": meta.get("start_time"),     # 视频帧的源时间戳（秒）
            "modality": meta.get("modality", "image"),
            "text": doc,
            "metadata": meta,
            "distance": dist,
            "vector_score": 1.0 - dist,
        })
    return items


@_tracked_operation
def delete_image(source_path: str) -> bool:
    _check_index_writable()
    try:
        col = _write_collection("image")
        # 先取一条该源的记录拿 frame_path → 推出帧目录，删向量后连带清掉磁盘帧（避免孤儿 jpg）
        frame_dir = None
        try:
            res = col.get(where={"source_path": source_path}, limit=1, include=["metadatas"])
            metas = res.get("metadatas") or []
            fp = metas[0].get("frame_path") if metas else None
            if fp:
                fp_dir = Path(fp).resolve().parent
                # 安全：仅清 video_frames/ 下的目录，绝不 rmtree 别处
                if fp_dir.is_relative_to(Path(VIDEO_FRAMES_DIR).resolve()):
                    frame_dir = str(fp_dir)
        except Exception:
            pass
        # 按 source_path 删，连带清掉视频的全部关键帧（id 为 {path}::g{gen}::frame::k）
        col.delete(where={"source_path": source_path})
        generation_store.clear_generation(_gen_namespace("image"), source_path)
        if frame_dir:
            shutil.rmtree(frame_dir, ignore_errors=True)
        _invalidate_document_cache()
        return True
    except Exception:
        return False


@_tracked_operation
def search(query_embedding: list[float], n_results: int = 10, file_type: str = None) -> list[dict]:
    """文本向量检索（C1：base+delta union；按当前有效代过滤，孤儿旧代不进入结果）。"""
    where = {"file_type": file_type} if file_type else None
    rows = _query_current_generation(
        _union_collections(CHROMA_COLLECTION), query_embedding, n_results,
        generation_store.COLLECTION_TEXT, where=where,
    )
    items = []
    for chunk_id, doc, meta, dist in rows:
        items.append(
            {
                "id": chunk_id,
                "source_path": meta.get("source_path", chunk_id.split("::")[0]),
                "text": doc,
                "metadata": meta,
                "distance": dist,
                # 向量余弦相似度（cosine 距离 → 相似度）
                "vector_score": 1.0 - dist,
            }
        )
    return items


@_tracked_operation
def get_chunks_by_ids(ids: list[str]) -> list[dict]:
    """按 chunk_id 批量取块（BM25 召回补取候选用；base+delta union）。

    C1：过滤非当前代记录与 tombstone——BM25 缓存的 id 可能指向刚被替换的旧代或已删源。
    """
    if not ids:
        return []
    gens = generation_store.current_generations(generation_store.COLLECTION_TEXT)
    tombstones = index_registry.list_tombstones()
    merged: dict[str, dict] = {}
    for col in _union_collections(CHROMA_COLLECTION):
        try:
            res = col.get(ids=ids, include=["documents", "metadatas"])
        except Exception as exc:
            raise_if_index_storage_corruption(exc, "get_chunks_by_ids")
            continue
        for i, cid in enumerate(res.get("ids") or []):
            meta = res["metadatas"][i] if res.get("metadatas") else {}
            src = meta.get("source_path") or cid.split("::")[0]
            if src in tombstones:
                continue
            if gens and not _keep_current(meta, gens):
                continue
            if cid in merged:
                continue
            merged[cid] = {
                "id": cid,
                "source_path": meta.get("source_path", cid.split("::")[0]),
                "text": res["documents"][i] if res.get("documents") else "",
                "metadata": meta,
                "distance": None,
                "vector_score": 0.0,
            }
    return list(merged.values())


def _chunk_sort_key(item: dict) -> tuple[float, int]:
    meta = item.get("metadata") or {}
    try:
        start = float(meta.get("start_time", 0) or 0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        idx = int(meta.get("chunk_index", 0) or 0)
    except (TypeError, ValueError):
        idx = 0
    return start, idx


def _build_chunks(res: dict) -> list[dict]:
    """把 Chroma get 结果构造为块列表并按 (start_time, chunk_index) 排序。"""
    chunks = []
    for i, cid in enumerate(res.get("ids") or []):
        meta = res["metadatas"][i] if res.get("metadatas") else {}
        chunks.append(
            {
                "id": cid,
                "text": res["documents"][i] if res.get("documents") else "",
                "metadata": meta,
            }
        )
    chunks.sort(key=_chunk_sort_key)
    return chunks


@_tracked_operation
def read_source_chunks(source_path: str, limit: int = 100) -> tuple[str, list[dict]]:
    """三态读取某源的已索引文本块（P0-3 契约；base+delta union）。

    返回 (status, chunks)：
    - (READ_OK, chunks)：正常读到数据（chunks 非空）；
    - (READ_EMPTY, [])：查询成功且确认无记录（合法未索引态 / 已 tombstone）；
    - (READ_ERROR, [])：ChromaDB 读取失败（索引损坏/句柄失效等）。
      调用方（尤其派生/写路径）必须把 read_error 与 empty 区分开，
      绝不能把读取失败当作空数据写进持久状态（2026-08-22 事故根因）。
    """
    if index_health_blocked():
        return READ_ERROR, []
    if index_registry.is_tombstoned(source_path):
        return READ_EMPTY, []
    where = _current_where(generation_store.COLLECTION_TEXT, source_path)
    merged: dict[str, dict] = {}
    saw_error = False
    for col in _union_collections(CHROMA_COLLECTION):
        try:
            res = col.get(where=where, limit=limit, include=["documents", "metadatas"])
        except Exception as e:
            record_index_operation_failure(e, "read_source_chunks")
            logger.warning(
                "ChromaDB 读取失败（read_error）source=%s: %s", source_path, type(e).__name__
            )
            saw_error = True
            continue
        for c in _build_chunks(res):
            merged[c["id"]] = c
    if saw_error:
        return READ_ERROR, []
    chunks = sorted(merged.values(), key=_chunk_sort_key)
    return (READ_OK if chunks else READ_EMPTY), chunks[:limit]


@_tracked_operation
def get_source_chunks(source_path: str, limit: int = 100) -> list[dict]:
    """Return indexed text chunks for one source file in chunk order.

    兼容包装（P0-3）：ok/empty 返回块列表；read_error 打 warning 后返回 []。
    注意：本函数无法区分「读取失败」与「未索引」，仅适用于检索展示类路径
    （读失败只降级当轮结果，不落持久状态）；派生/回填等写路径必须改用
    read_source_chunks 的三态契约。
    """
    _, chunks = read_source_chunks(source_path, limit)
    return chunks


@_tracked_operation
def get_source_embedding(source_path: str) -> list[float] | None:
    """Return the first chunk's embedding for a source file (base+delta union), or None."""
    if index_registry.is_tombstoned(source_path):
        return None
    where = _current_where(generation_store.COLLECTION_TEXT, source_path)
    for col in _union_collections(CHROMA_COLLECTION):
        try:
            res = col.get(where=where, limit=1, include=["embeddings"])
        except Exception as exc:
            record_index_operation_failure(exc, "get_source_embedding")
            continue
        # Chroma may return the outer result as a list while an individual
        # embedding is a NumPy array.  Do not use truth-value checks here: NumPy
        # vectors with more than one element deliberately reject boolean coercion.
        embeddings = res.get("embeddings")
        if embeddings is None or len(embeddings) == 0:
            continue
        embedding = embeddings[0]
        if embedding is None or len(embedding) == 0:
            continue
        return list(embedding)
    return None


def _collect_effective_metas(col_name: str, ns: str) -> list[dict]:
    """跨 base+delta 收集某集合「当前有效代」的全部 metadata（C1 union；tombstone 已过滤）。"""
    gens = generation_store.current_generations(ns)
    tombstones = index_registry.list_tombstones()
    metas: list[dict] = []
    for col in _union_collections(col_name):
        try:
            res = col.get(include=["metadatas"])
        except Exception:
            continue
        for m in res.get("metadatas") or []:
            if not isinstance(m, dict):
                continue
            src = m.get("source_path") or m.get("file_path")
            if not src or src in tombstones:
                continue
            if gens and not _keep_current(m, gens):
                continue
            metas.append(m)
    return metas


@_tracked_operation
def list_documents(limit: int | None = 100, offset: int = 0) -> dict:
    """按「源文件」聚合返回（一个文件一条；base+delta union）。

    覆盖全部已索引文件：文本集合(text/image/video 的文本块) ∪ 图片集合(纯图、纯帧视频)，
    所以无 OCR/无说明的纯图(如 LOGO)也会出现，可被打标签。视频项附一张海报帧(poster,
    最小 frame_index 的帧路径)供前端做缩略图。
    """
    global _DOCUMENT_CACHE_SIGNATURE, _DOCUMENT_CACHE_ITEMS
    text_metas = _collect_effective_metas(CHROMA_COLLECTION, generation_store.COLLECTION_TEXT)
    image_metas = _collect_effective_metas(IMAGE_COLLECTION, generation_store.COLLECTION_IMAGE)
    signature = (len(text_metas), len(image_metas))
    with _COLLECTION_LOCK:
        if _DOCUMENT_CACHE_SIGNATURE == signature and _DOCUMENT_CACHE_ITEMS is not None:
            cached = _DOCUMENT_CACHE_ITEMS
            items = cached[offset:] if limit is None else cached[offset:offset + limit]
            return {"total": len(cached), "items": items}
    files: dict[str, dict] = {}
    for meta in text_metas:
        src = meta.get("source_path") or meta.get("file_path")
        if not src:
            continue
        if src not in files:
            files[src] = {
                "id": src,
                "metadata": meta,
                "chunk_count": int(meta.get("chunk_count", 1) or 1),
            }

    # 图片集合：补纯图(无文本块) 与 纯帧视频；并给所有视频取一张海报帧
    frame_counts: dict[str, int] = {}
    posters: dict[str, tuple[int, str]] = {}  # src -> (min_frame_index, frame_path)
    for m in image_metas:
        src = m.get("source_path")
        if not src:
            continue
        mod = m.get("modality")
        if mod == "frame":
            frame_counts[src] = frame_counts.get(src, 0) + 1
            try:
                fi = int(m.get("frame_index", 0) or 0)
            except (TypeError, ValueError):
                fi = 0
            fp = m.get("frame_path")
            if fp and (src not in posters or fi < posters[src][0]):
                posters[src] = (fi, fp)
            if src not in files:
                files[src] = {
                    "id": src,
                    "metadata": {**m, "file_type": "video"},
                    "chunk_count": frame_counts[src],
                }
        elif mod == "image":
            if src not in files:
                files[src] = {
                    "id": src,
                    "metadata": {**m, "file_type": "image"},
                    "chunk_count": 0,
                }
    # 海报帧挂到对应视频项（供前端 /api/frame 缩略图）
    for src, (_, fp) in posters.items():
        if src in files:
            files[src]["poster"] = fp

    ordered = list(files.values())
    with _COLLECTION_LOCK:
        _DOCUMENT_CACHE_SIGNATURE = signature
        _DOCUMENT_CACHE_ITEMS = ordered
    total = len(ordered)
    items = ordered[offset:] if limit is None else ordered[offset : offset + limit]
    return {"total": total, "items": items}


@_tracked_operation
def list_all_documents() -> list[dict]:
    """Return every indexed source for server-side inventory/filtering."""
    return list_documents(limit=None, offset=0)["items"]


@_tracked_operation
def delete_text_chunks(source_path: str) -> bool:
    """仅删某源文件在「文本集合」的所有分块（不动图片/视觉向量）。

    用于：纯图/无正文文件在 caption 被清空后 force 重索引时，清掉残留的 caption 文本块，
    但保留其 CLIP 视觉向量（不能用 delete_file，那会连带删掉视觉向量）。
    """
    _check_index_writable()
    try:
        _write_collection("text").delete(where={"source_path": source_path})
        # where 按 source_path 命中全部代；注册表同步清零，读取回到旧式口径
        generation_store.clear_generation(_gen_namespace("text"), source_path)
        _invalidate_lexical()
        _invalidate_document_cache()
        return True
    except Exception as e:
        raise_if_index_storage_corruption(e, "delete_text_chunks")
        logger.error(f"清理文本块失败 {source_path}: {e}")
        return False


@_tracked_operation
def delete_file(source_path: str) -> bool:
    """删除某源文件的所有分块（文本集合）及其视觉向量（图片集合）。

    C1：base 只读不可删，故记录 tombstone 挡住 base 中的旧 chunk（防删除后复活）。
    """
    _check_index_writable()
    ok = True
    try:
        _write_collection("text").delete(where={"source_path": source_path})
        generation_store.clear_generation(_gen_namespace("text"), source_path)
        _invalidate_lexical()
    except Exception as e:
        raise_if_index_storage_corruption(e, "delete_file_text")
        logger.error(f"删除失败 {source_path}: {e}")
        ok = False
    delete_image(source_path)
    # C1：无论文本/视觉删除是否完全成功，都记录 tombstone（读取侧据此过滤）。
    index_registry.set_tombstone(source_path)
    _invalidate_document_cache()
    return ok


@_tracked_operation
def delete_document(doc_id: str) -> bool:
    """兼容旧接口：按源文件路径删除其全部分块"""
    return delete_file(doc_id)


@_tracked_operation
def get_stats() -> dict:
    text_metas = _collect_effective_metas(CHROMA_COLLECTION, generation_store.COLLECTION_TEXT)
    image_metas = _collect_effective_metas(IMAGE_COLLECTION, generation_store.COLLECTION_IMAGE)

    seen: dict[str, str] = {}
    for m in text_metas:
        src = m.get("source_path") or m.get("file_path")
        if src and src not in seen:
            seen[src] = m.get("file_type", "text")

    # 图片集合：分出纯图向量与视频帧向量，并补计「只有视觉向量（无 OCR 文本块）」的图片/视频
    visual_total = len(image_metas)
    frame_count = 0
    image_sources: set[str] = set()
    frame_video_sources: set[str] = set()
    for m in image_metas:
        src = m.get("source_path")
        if m.get("modality") == "image" and src:
            image_sources.add(src)
        elif m.get("modality") == "frame":
            frame_count += 1
            if src:
                frame_video_sources.add(src)

    video_sources = {s for s, t in seen.items() if t == "video"} | frame_video_sources
    audio_sources = {s for s, t in seen.items() if t == "audio"}
    indexed_image_sources = {s for s, t in seen.items() if t == "image"} | image_sources
    text_count = sum(1 for t in seen.values() if t not in ("image", "video", "audio"))

    return {
        "total_documents": len({*seen.keys(), *image_sources, *frame_video_sources}),
        "text_documents": text_count,
        "image_documents": len(indexed_image_sources),
        "video_documents": len(video_sources),
        "audio_documents": len(audio_sources),
        "visual_indexed_images": visual_total - frame_count,  # 纯图（不被视频帧污染）
        "video_frames": frame_count,
        "total_chunks": len(text_metas),
    }
