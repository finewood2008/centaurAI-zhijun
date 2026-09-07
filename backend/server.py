"""FastAPI 服务器 — 本地向量数据库 API"""
import os

# 必须在所有其他导入之前清除代理——HF 直连可用
for _pv in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_pv, None)
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import json
import base64
import ipaddress
import functools
import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote, quote_plus

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Header, Depends, Body, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import (
    HOST,
    PORT,
    WATCH_FOLDER,
    PROJECT_ROOT,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_VIDEO_EXTENSIONS,
    VIDEO_FRAMES_DIR,
    MAX_UPLOAD_BYTES,
    RECALL_MULTIPLIER,
    RECALL_MIN_CANDIDATES,
    RERANK_MAX_CANDIDATES,
    BM25_EXTRA_CANDIDATES,
    RERANK_SCORE_THRESHOLD,
    VECTOR_SIM_THRESHOLD,
    CLIP_ENABLED,
    IMAGE_SIM_THRESHOLD,
    WHISPER_ENABLED,
    IMPORTANCE_WEIGHT_STEP,
    PIN_MIN_RELEVANCE,
    MEMORY_IMPORT_AUTO_SYNC,
    MEMORY_IMPORT_INTERVAL_SECONDS,
    LAN_ENABLED,
    ADMIN_PASSWORD,
)
from parser import is_supported, file_hash
from embedder import (
    embed_query, embed_query_clip, rerank, reranker_loadable, clip_available,
    whisper_loadable, warmup,
)
from video import ffmpeg_available
from vector_store import (
    IndexCorruptedError,
    search,
    search_images,
    get_chunks_by_ids,
    get_source_chunks,
    list_documents,
    list_all_documents,
    delete_document,
    get_stats,
    needs_migration,
    begin_rebuild,
    resume_rebuild,
    commit_rebuild,
    abort_rebuild,
    rebuild_status,
)
import lexical
import annotations
import memory_api
import memory_store
import rag_strategy
import wiki_store
import gbrain_store
import context_snapshot
import mcp_access
import tokenmanager_sync
from runtime_paths import (
    CONTEXT_PACKS_CONFIG_PATH,
    LAN_CONFIG_PATH,
    MOBILE_CONFIG_PATH,
    TRASH_DIR,
)
from watcher import (
    start_watcher, index_file, scan_existing, submit_index, get_job,
    list_active_jobs, list_jobs as list_index_jobs,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ========== LAN 访问（小功能：密码登录 + 文件上传 API）==========
import secrets as _secrets

_lan_tokens: dict[str, float] = {}


def _lan_active() -> bool:
    return LAN_ENABLED and bool(ADMIN_PASSWORD)


def _check_lan_token(token: Optional[str]) -> bool:
    if not token or token not in _lan_tokens:
        return False
    if time.time() > _lan_tokens[token]:
        _lan_tokens.pop(token, None)
        return False
    return True


def _get_lan_hosts() -> list[str]:
    """Return reachable non-loopback IPv4 addresses for the LAN import page."""
    hosts: set[str] = set()

    # VPN/proxy software can hijack hostname resolution and the UDP route probe.
    # Enumerate interface addresses first so a real Wi-Fi/Ethernet address is not lost.
    def add_host(value: str) -> None:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return
        benchmark_net = ipaddress.ip_network("198.18.0.0/15")
        if (
            address.version != 4
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address in benchmark_net
        ):
            return
        hosts.add(str(address))

    try:
        import fcntl
        import struct

        for _, interface_name in socket.if_nameindex():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    request = struct.pack("256s", interface_name.encode("utf-8")[:15])
                    response = fcntl.ioctl(sock.fileno(), 0x8915, request)  # SIOCGIFADDR
                    add_host(socket.inet_ntoa(response[20:24]))
            except OSError:
                continue
    except (ImportError, OSError):
        pass

    try:
        name = socket.gethostname()
        for info in socket.getaddrinfo(name, None, socket.AF_INET):
            add_host(info[4][0])
    except Exception:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            add_host(sock.getsockname()[0])
    except Exception:
        pass

    def sort_key(ip: str) -> tuple[int, tuple[int, ...]]:
        parts = tuple(int(p) for p in ip.split("."))
        if parts[:2] == (192, 168):
            rank = 0
        elif parts[:1] == (10,):
            rank = 1
        elif len(parts) == 4 and parts[0] == 172 and 16 <= parts[1] <= 31:
            rank = 2
        elif len(parts) == 4 and parts[0] == 100 and 64 <= parts[1] <= 127:
            rank = 3
        else:
            rank = 4
        return rank, parts

    return sorted(hosts, key=sort_key)


def _get_lan_urls() -> list[str]:
    remote = mcp_access.get_runtime_config()
    lan_ip = str(remote.get("lan_ip") or "").strip()
    if remote.get("enabled") and lan_ip:
        port = int(remote.get("lan_http_port", 8080))
        base = f"http://{lan_ip}" if port == 80 else f"http://{lan_ip}:{port}"
        return [f"{base}/lan"]
    return []


class LanLoginReq(BaseModel):
    password: str


# 提升 multipart 文件上传上限（默认1MB，视频远大于此）
import starlette.formparsers
starlette.formparsers.MultiPartParser.max_part_size = 1024 * 1024 * 1024  # 1GB per part

# P0-1 数据根目录独占锁（模块级保活；lifespan shutdown / 进程退出时释放）
_INSTANCE_LOCK = None


def _detect_worker_count() -> int:
    """探测多 worker 部署意图（gunicorn 的 WEB_CONCURRENCY / uvicorn --workers）。

    多 worker = 多进程共享同一 Chroma 数据目录，嵌入式 ChromaDB 不允许，
    必须在拿到任何 Chroma 连接前直接拒绝启动（索引可靠性方案 P0-1）。
    """
    raw = (os.environ.get("WEB_CONCURRENCY") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    for i, arg in enumerate(sys.argv):
        if arg == "--workers" and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
        elif arg.startswith("--workers="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                pass
    return 1


def _acquire_instance_lock_once() -> tuple[bool, str | None]:
    """获取数据根目录 OS 级独占锁（每进程幂等：已持有则直接成功）。

    返回 (ok, holder_hint)。main() 与 lifespan 共用：无论 python server.py
    还是 uvicorn server:app（ASGI）启动，锁都在任何 ChromaDB 连接之前获取。
    """
    global _INSTANCE_LOCK
    if _INSTANCE_LOCK is not None:
        return True, None
    from instance_lock import acquire as _acquire_instance_lock, holder_hint

    lock, holder = _acquire_instance_lock(role="backend")
    if lock is None:
        return False, holder_hint(holder)
    _INSTANCE_LOCK = lock
    return True, None


def _run_startup_health_check() -> None:
    """P0-5 启动自检：对受管 collection 做 count + 最小 get 探测。

    探测失败不阻断启动（退回可运行态），但记录明确的 corrupted 结论与恢复
    建议，避免让首个用户请求撞上 InternalError。
    """
    try:
        from vector_store import verify_chroma_health
        health = verify_chroma_health()
        if not health.get("ok"):
            for issue in health.get("issues", []):
                logger.error("ChromaDB 启动自检未通过：%s", issue)
            logger.error(
                "ChromaDB 索引自检失败（corrupted）：检索不可用，请按《MindOS索引可靠性"
                "问题分析与改进方案》§9 在副本上备份诊断后恢复，勿直接在故障索引上继续写入。"
            )
        else:
            checked = health.get("checked", [])
            healthy = sum(
                1 for c in checked if c.get("status") in ("ok", "not_created")
            )
            logger.info(
                "ChromaDB 启动自检通过：%d/%d 个受管 collection 健康（含未创建的空库集合）",
                healthy, len(checked),
            )
    except Exception as e:
        logger.warning(f"ChromaDB 启动自检异常（忽略，继续启动）: {e}")


# ========== 后台服务统一生命周期（修复5：两种启动方式等价） ==========
# 此前 watcher/预热/各守护线程只在 main() 中启动，uvicorn server:app（纯 ASGI）
# 启动时放入监控目录的材料不会被自动索引。统一由 lifespan 启动/停止。
_SERVICES_LOCK = threading.Lock()
_SERVICES_STARTED = False
_WATCHER_OBSERVER = None

# P0-4 完整性巡检（修复6）：启动后周期枚举已索引源做 verify_source_index，
# integrity_failed / read_error 的自动重新入队重建。VDB_INTEGRITY_PATROL=0 关闭。
INTEGRITY_PATROL_ENABLED = os.environ.get("VDB_INTEGRITY_PATROL", "1") != "0"
try:
    INTEGRITY_PATROL_DELAY_SECONDS = max(int(os.environ.get("VDB_INTEGRITY_PATROL_DELAY", "120")), 10)
except ValueError:
    INTEGRITY_PATROL_DELAY_SECONDS = 120
try:
    INTEGRITY_PATROL_INTERVAL_SECONDS = max(int(os.environ.get("VDB_INTEGRITY_PATROL_INTERVAL", "21600")), 600)
except ValueError:
    INTEGRITY_PATROL_INTERVAL_SECONDS = 21600
# 阶段B：索引损坏期间巡检只记录一次汇总告警，禁止逐文件重新入队（D4）。
_PATROL_CORRUPTED_LOGGED = False


def _load_lan_config() -> None:
    """从持久化配置文件加载 LAN 设置（优先生效于环境变量）。"""
    global LAN_ENABLED, ADMIN_PASSWORD
    if not _LAN_CFG.exists():
        return
    try:
        cfg = json.loads(_LAN_CFG.read_text())
        if cfg.get("enabled"):
            os.environ["VDB_LAN_ENABLED"] = "true"
            LAN_ENABLED = True
            if cfg.get("password"):
                os.environ["VDB_ADMIN_PASSWORD"] = cfg["password"]
                ADMIN_PASSWORD = cfg["password"]
            logger.info("🌐 LAN 访问已启用（通过 HTTPS 边缘服务转发）")
    except Exception:
        pass


def _verify_and_requeue_incomplete() -> dict:
    """P0-4 恢复入口（修复6）：枚举全部已索引源做完整性校验，损坏的重排队重建。

    verify_source_index 返回 integrity_failed / read_error 的源（含新代校验
    失败、缺块、维度不一致等）调用 submit_index(force=True) 重建——不再只在
    文件变化被 watcher 扫到时被动修复。文件已不存在的跳过（删除走正常链路）。
    """
    from vector_store import (
        VERIFY_INTEGRITY_FAILED,
        VERIFY_READ_ERROR,
        index_health_state,
        list_all_documents,
        verify_source_index,
    )
    # 阶段B 闸门：索引损坏时巡检只记录一次汇总告警，不得把每个源重新入队，
    # 避免 read_error 触发逐文件重入队把日志与资源耗尽（问题1 根因）。
    global _PATROL_CORRUPTED_LOGGED
    if index_health_state() == "corrupted":
        if not _PATROL_CORRUPTED_LOGGED:
            logger.error(
                "完整性巡检跳过：ChromaDB 索引已损坏（corrupted），禁止逐文件重入队。"
                "请先恢复或重建索引，完成后调用 POST /api/system/storage/recheck 复检。"
            )
            _PATROL_CORRUPTED_LOGGED = True
        return {"checked": 0, "ok": 0, "requeued": [], "incomplete": [], "gated": "index_corrupted"}
    _PATROL_CORRUPTED_LOGGED = False
    stats: dict = {"checked": 0, "ok": 0, "requeued": [], "incomplete": []}
    try:
        items = list_all_documents()
    except Exception as e:
        logger.error(f"完整性巡检：枚举已索引源失败: {e}")
        return stats
    for item in items:
        src = str(item.get("id") or "")
        if not src:
            continue
        stats["checked"] += 1
        try:
            status = verify_source_index(src)
        except Exception:
            status = VERIFY_READ_ERROR
        if status in (VERIFY_INTEGRITY_FAILED, VERIFY_READ_ERROR):
            stats["incomplete"].append({"path": src, "status": status})
            try:
                if Path(src).is_file():
                    submit_index(src, force=True)
                    stats["requeued"].append(src)
            except Exception as e:
                logger.warning(f"完整性巡检：重新入队失败 {src}: {e}")
        elif status == "ok":
            stats["ok"] += 1
    return stats


def _bg_integrity_patrol() -> None:
    """周期完整性巡检线程：启动延迟一轮（避开预热/初始扫描高峰），之后固定间隔。"""
    time.sleep(INTEGRITY_PATROL_DELAY_SECONDS)
    while True:
        try:
            result = _verify_and_requeue_incomplete()
            card_audit = {"checked": 0, "healthy": 0, "corrupted": 0, "repaired": 0}
            if not result.get("gated"):
                from mindos import knowledge as _knowledge
                card_audit = _knowledge.audit_active_card_vectors(repair=True)
            requeued = result.get("requeued") or []
            if requeued or card_audit.get("corrupted"):
                logger.warning(
                    "完整性巡检发现 %d 个损坏源、%d 张异常卡片；材料重建=%s，卡片修复=%d",
                    len(requeued), int(card_audit.get("corrupted") or 0), requeued[:10],
                    int(card_audit.get("repaired") or 0),
                )
            else:
                logger.info(
                    "完整性巡检完成：%d 个已索引源、%d 张已索引卡片全部健康",
                    result.get("checked", 0), card_audit.get("checked", 0),
                )
        except Exception as e:
            logger.warning(f"完整性巡检异常（下轮重试）: {e}")
        time.sleep(INTEGRITY_PATROL_INTERVAL_SECONDS)


def _bg_warmup() -> None:
    """后台预热模型 + BM25，不阻塞 API 启动。"""
    try:
        warmup()
        lexical.build_index()
        logger.info("后台模型预热完成")
    except Exception as e:
        logger.warning(f"后台模型预热失败: {e}")


def _bg_memory_import_sync() -> None:
    script = Path(PROJECT_ROOT) / "scripts" / "sync_agent_memories.py"
    if not script.exists():
        logger.warning(f"记忆同步脚本不存在: {script}")
        return
    # 等 uvicorn 端口起来后再调脚本；脚本会回调 /api/memory/reindex。
    time.sleep(10)
    interval = max(int(MEMORY_IMPORT_INTERVAL_SECONDS), 60)
    while True:
        if not tokenmanager_sync.should_use_legacy_memory_scanner():
            logger.debug("TokenManager 已提供 memories API，跳过旧文件直扫")
            time.sleep(interval)
            continue
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--include-context",
                    "--reindex-attempts",
                    "1",
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0:
                logger.info("Agent 记忆自动同步完成")
            else:
                logger.warning(
                    "Agent 记忆自动同步失败: %s%s",
                    proc.stdout[-1000:],
                    proc.stderr[-1000:],
                )
        except Exception as e:
            logger.warning(f"Agent 记忆自动同步异常: {e}")
        time.sleep(interval)


def _bg_schema_migration_commit(scan_result: dict, rebuild_session: str | None = None) -> None:
    """schema 迁移重建的延迟提交（修复1）：全量扫描完成后再切换。

    此前 main() 里 begin_rebuild() 后立即 commit_rebuild()，此时 __rebuild
    集合还是空的（scan_existing 尚未执行），正式集合被空集合顶替、违反 P1-2
    「全量扫描和校验后再切换」。现在等 start_watcher→scan_existing 提交的
    本轮强制全量扫描的每一项都必须成功，才允许切换；超时、排队冲突、索引失败
    或未知终态均 abort，旧 schema 集合保留在线，下次启动重试。不能以全局队列
    是否排空替代本次迁移的材料清单校验，否则会把半成品集合切到线上。
    """
    import rebuild_progress
    from watcher import finish_rebuild_barrier, get_job
    watched = list(scan_result.get("candidates") or [])
    already_pending = list(scan_result.get("already_pending") or [])
    if already_pending:
        # 这些任务可能在 begin_rebuild 前已开始写旧集合，不能把它们当作 rebuild 成功。
        logger.error(
            "schema 迁移中止：%d 个材料已有进行中的旧任务，等待其结束后下次启动重试: %s",
            len(already_pending), already_pending[:10],
        )
        abort_rebuild()
        if rebuild_session:
            rebuild_progress.finish(rebuild_session, "aborted")
        if rebuild_session:
            finish_rebuild_barrier(rebuild_session)
        return
    deadline = time.time() + 1800
    while time.time() < deadline:
        pending = [
            path for path in watched
            if get_job(path).get("state") in ("queued", "processing", "validating")
        ]
        if not pending:
            break
        time.sleep(2.0)
    pending = [
        path for path in watched
        if get_job(path).get("state") in ("queued", "processing", "validating")
    ]
    if pending:
        logger.error("schema 迁移超时（%d 个任务未完成），中止本次重建，旧集合保留在线", len(pending))
        abort_rebuild()
        if rebuild_session:
            rebuild_progress.update_states(rebuild_session, {p: get_job(p).get("state", "unknown") for p in watched})
            rebuild_progress.finish(rebuild_session, "aborted")
        if rebuild_session:
            finish_rebuild_barrier(rebuild_session)
        return
    failed = [
        {"path": path, "state": get_job(path).get("state"), "error": get_job(path).get("error") or ""}
        for path in watched if get_job(path).get("state") != "done"
    ]
    if failed:
        logger.error("schema 迁移中止：%d 个材料未成功索引，旧集合保留在线: %s", len(failed), failed[:10])
        abort_rebuild()
        if rebuild_session:
            rebuild_progress.update_states(rebuild_session, {p: get_job(p).get("state", "unknown") for p in watched})
            rebuild_progress.finish(rebuild_session, "aborted")
        if rebuild_session:
            finish_rebuild_barrier(rebuild_session)
        return
    try:
        committed = commit_rebuild()
    except Exception as e:
        logger.error(f"schema 迁移提交异常，中止本次重建（旧集合保留在线）: {e}")
        abort_rebuild()
        if rebuild_session:
            rebuild_progress.finish(rebuild_session, "aborted")
        if rebuild_session:
            finish_rebuild_barrier(rebuild_session)
        return
    if not committed.get("ok"):
        logger.error("schema 迁移提交失败（%s），旧集合保留在线", committed.get("error"))
        abort_rebuild()
        if rebuild_session:
            rebuild_progress.finish(rebuild_session, "aborted")
        if rebuild_session:
            finish_rebuild_barrier(rebuild_session)
        return
    logger.info("schema 迁移完成：%d 个材料均已通过索引后切换到新 schema 集合", len(watched))
    if rebuild_session:
        rebuild_progress.update_states(rebuild_session, {p: "done" for p in watched})
        rebuild_progress.finish(rebuild_session, "completed")
        finish_rebuild_barrier(rebuild_session)


def _start_background_services() -> None:
    """启动全部后台服务（watcher / schema 迁移 / 预热 / 各守护线程）。

    由 lifespan 调用：`python server.py` 与 `uvicorn server:app` 两种启动方式
    统一走这里。幂等，重复调用直接返回。
    """
    global _SERVICES_STARTED, _WATCHER_OBSERVER
    with _SERVICES_LOCK:
        if _SERVICES_STARTED:
            return
        _SERVICES_STARTED = True

    _load_lan_config()

    # 仅恢复清理类持久化事务。阶段 C 禁止启动扫描 Wiki 补建 card_vector_jobs；
    # 确认会话会在下方按其自身持久化状态恢复。
    try:
        from mindos import lifecycle as _lifecycle
        purge_result = _lifecycle.recover_pending_purges()
        _lifecycle.cleanup_purge_isolation()
        if purge_result.get("recovered"):
            logger.info("知识卡片清理恢复：purge=%s", purge_result)
    except Exception as exc:
        logger.error("知识卡片启动恢复失败: %s", type(exc).__name__)

    # schema 迁移（修复1）：begin_rebuild 只切写目标；scan_existing 由
    # start_watcher 触发写入 __rebuild 集合，跑完后由迁移线程校验并提交
    migration_started = False
    migration_session: str | None = None
    resume_record = None
    try:
        import rebuild_progress
        resume_record = rebuild_progress.active()
        if resume_record or needs_migration():
            logger.info("检测到旧 schema 数据，开始隔离重建（全量扫描完成后再切换）...")
            from watcher import begin_rebuild_barrier, finish_rebuild_barrier
            candidate_session = (resume_record or {}).get("session_id") or uuid.uuid4().hex
            barrier = begin_rebuild_barrier(candidate_session)
            if not barrier.get("ok") or barrier.get("active"):
                logger.warning(
                    "schema 迁移延后：索引提交栅栏不可用或已有 %d 个任务运行中",
                    len(barrier.get("active") or []),
                )
                if barrier.get("ok"):
                    finish_rebuild_barrier(candidate_session)
            else:
                started = resume_rebuild() if resume_record else begin_rebuild(candidate_session)
                if resume_record and not started.get("ok"):
                    # 留存记录已损坏或 rebuild 集合不全：丢弃半成品，安全地重新开始。
                    rebuild_progress.finish(candidate_session, "aborted")
                    started = begin_rebuild(candidate_session)
                migration_started = bool(started.get("ok"))
                if migration_started:
                    migration_session = candidate_session
                else:
                    finish_rebuild_barrier(candidate_session)
                    logger.warning("schema 迁移启动失败（%s），继续以现有集合运行", started.get("error"))
    except Exception as e:
        logger.warning(f"schema 迁移检查失败（忽略）: {e}")

    # §8.1 常规启动禁止 scan_existing（不为 watch_folder 历史材料自动建索引）；
    # 仅在受控迁移/续跑重建时对 __rebuild 集合全量扫描并校验后切换。
    scan_result = None
    if migration_started:
        # 迁移时必须 force 全量写入 __rebuild，不能依据仍在线的旧集合跳过文件。
        scan_result = scan_existing(
            force=not resume_record,
            rebuild_session=migration_session,
        )
        import rebuild_progress
        from watcher import restore_rebuild_done
        # 续跑时 scan_existing 已在 rebuild 集合上复核指纹；被跳过的材料即完整且未变，
        # 恢复为 done，其余候选重新索引。当前文件集合成为新的 manifest（删除的源不续跑）。
        if resume_record:
            from vector_store import delete_file
            previous_paths = set((resume_record.get("manifest") or {}).keys())
            current_paths = set(scan_result.get("sources") or [])
            # 停机期间被删除的源不会再触发 Watcher 事件，恢复时必须从 rebuild 清掉，
            # 否则旧材料会随会话提交重新出现。
            for deleted_path in previous_paths - current_paths:
                try:
                    delete_file(deleted_path)
                except Exception as e:
                    logger.warning("恢复重建时清理已删除源失败 %s: %s", deleted_path, type(e).__name__)
            done = [
                p for p in scan_result.get("sources", [])
                if p not in set(scan_result.get("candidates") or [])
            ]
            scan_result["candidates"] = list(scan_result.get("sources") or [])
        rebuild_progress.start(
            migration_session,
            "schema-migration" if needs_migration() else "reindex-resume",
            dict(scan_result.get("fingerprints") or {}),
        )
        if resume_record:
            restore_rebuild_done(migration_session, done)
            for path in done:
                rebuild_progress.set_path_state(migration_session, path, "done")
    # §8.2 启动恢复：material_jobs 历史 queued/processing 统一转 paused（service_interrupted），
    # 遗留 preparing 快照由 saga 回滚；绝不自动续跑。旧 index_jobs 的自动恢复
    # （recover_index_jobs 的 processing->queued 重放）已停用，防止重启后自动重放。
    try:
        from mindos.material_snapshot_saga import MaterialSnapshotSaga
        from mindos.stores.material_pipeline_store import MaterialPipelineStore
        recovery = MaterialSnapshotSaga(MaterialPipelineStore.instance()).recover_pipeline()
        if recovery.get("paused_jobs") or recovery.get("rolled_back"):
            logger.info("材料流水线启动恢复：%s", recovery)
    except Exception as e:
        logger.error("材料流水线启动恢复失败: %s", type(e).__name__)
    # 阶段 C：只恢复已持久化的确认会话；不扫描/迁移历史卡片或补建向量任务。
    try:
        from mindos.uploads import recover_material_confirmations
        confirmation_recovery = recover_material_confirmations()
        if confirmation_recovery["completed"] or confirmation_recovery["rolledBack"]:
            logger.info("知识卡片确认会话恢复：%s", confirmation_recovery)
    except Exception as e:
        logger.error("知识卡片确认会话恢复失败: %s", type(e).__name__)
    # 卡片索引是用户已确认 revision 的持久化 outbox。健康启动后只恢复这些
    # 已存在任务，不扫描 Wiki 创造新的确认意图。台账或 Chroma 不健康时停用 worker。
    try:
        from mindos import knowledge as _knowledge
        from mindos.stores import card_ledger_store
        from vector_store import index_health_blocked
        ledger_health = card_ledger_store.health_check()
        if not ledger_health.get("ok"):
            _knowledge.stop_vector_worker(wait=False)
            paused_card_jobs = card_ledger_store.pause_vector_jobs("ledger_corrupted")
            logger.error("卡片台账 quick_check 失败，索引 worker 未启动：%s", ledger_health)
        elif index_health_blocked():
            _knowledge.stop_vector_worker(wait=False)
            paused_card_jobs = card_ledger_store.pause_vector_jobs("index_corrupted")
            logger.error("ChromaDB 不健康，已暂停 %d 个卡片索引任务", paused_card_jobs)
        else:
            _knowledge.start_vector_worker()
            interrupted = _knowledge.recover_interrupted_vector_repairs()
            audit = _knowledge.audit_active_card_vectors(repair=True)
            if any(interrupted.values()) or audit.get("corrupted"):
                logger.info("卡片索引启动对账：jobs=%s cards=%s", interrupted, audit)
    except Exception as e:
        logger.error("卡片索引任务启动恢复失败: %s", type(e).__name__)

    _WATCHER_OBSERVER = start_watcher(initial_scan=False)

    # §6.1 单实例材料 worker：后台轮询只领取当前 epoch 或 resume_token 的任务；
    # 由启动恢复转 paused 的历史任务不会被隐式领取，须用户显式继续。
    try:
        from mindos.ollama_material_scheduler import start_scheduler
        from mindos.material_worker import MaterialWorker
        start_scheduler()
        MaterialWorker.instance().start()
    except Exception as e:
        logger.warning(f"材料 worker 启动失败: {e}")

    if migration_started:
        threading.Thread(
            target=_bg_schema_migration_commit,
            args=(scan_result, migration_session),
            daemon=True,
            name="schema-migration-rebuild",
        ).start()

    threading.Thread(target=_bg_warmup, daemon=True).start()

    if MEMORY_IMPORT_AUTO_SYNC:
        threading.Thread(target=_bg_memory_import_sync, daemon=True).start()

    threading.Thread(
        target=tokenmanager_sync.run_forever,
        daemon=True,
        name="tokenmanager-conversation-memory-sync",
    ).start()

    # 空闲自动卸载守护线程（模型 + ChromaDB 索引内存）
    threading.Thread(target=_idle_unload_loop, daemon=True, name="idle-unload").start()

    # P3：仅清理过期终态模型任务元数据；不删除模型文件、配置、索引或诊断。
    threading.Thread(
        target=_model_job_cleanup_loop, daemon=True, name="model-job-cleanup"
    ).start()

    try:
        wiki_store.start_maintenance_loop()
    except Exception as e:
        logger.warning(f"Wiki 维护线程启动失败: {e}")

    try:
        context_snapshot.start_snapshot_loop()
    except Exception as e:
        logger.warning(f"个人 Context 快照线程启动失败: {e}")

    if INTEGRITY_PATROL_ENABLED:
        threading.Thread(
            target=_bg_integrity_patrol, daemon=True, name="integrity-patrol"
        ).start()
        logger.info(
            "完整性巡检已启用：启动 %d 秒后首轮，之后每 %d 秒一轮",
            INTEGRITY_PATROL_DELAY_SECONDS, INTEGRITY_PATROL_INTERVAL_SECONDS,
        )

    # P2 §7.0.1：模型任务单 worker，在索引队列恢复之后启动；HTTP 路由只创建/查询/取消。
    try:
        from mindos.model_job_worker import start_worker

        start_worker()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"模型任务 worker 启动失败（任务将排队待恢复）: {e}")

    # 知君 P1：本体后台 worker（对话抽取 / 摘要 / 投影），单线程租约领取。
    try:
        from mindos.zhijun.jobs import start_worker as start_ontology_worker

        start_ontology_worker()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"知君本体 worker 启动失败: {e}")

    from mindos.chat_imports import start_worker as start_chat_import_worker
    start_chat_import_worker()

    # 阶段 2：连接票据 nonce 墓碑与活动会话周期清理。撤销后 5 秒内断开依赖
    # 每轮扫描把过期/epoch 失效会话置为关闭；新请求的撤销/重放校验在验签期即时生效。
    threading.Thread(
        target=_connectivity_state_gc_loop,
        daemon=True,
        name="connectivity-state-gc",
    ).start()


def _connectivity_state_gc_loop() -> None:
    interval = 5
    while True:
        time.sleep(interval)
        try:
            from mindos.stores import connectivity_store

            connectivity_store.close_stale_sessions()
            connectivity_store.prune_expired_nonces()
        except Exception as exc:  # noqa: BLE001
            logger.warning("连接状态清理失败: %s", type(exc).__name__)
        # 阶段 2：Consumer 撤销事件轮询（仅配置 base_url 后启用）。mark_revoked
        # 会立即关闭该 tuple 的活动会话，本线程只负责把云端事件翻译到本机。
        try:
            from mindos import consumer_adapter

            if consumer_adapter.is_revocation_sync_configured():
                result = consumer_adapter.sync_revocations()
                if result["applied"]:
                    logger.info("Consumer 撤销同步应用 %d 条事件（游标 %d）", result["applied"], result["cursor"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Consumer 撤销同步失败: %s", type(exc).__name__)


def _stop_background_services() -> None:
    """停止后台服务（幂等）：watcher、各线程池。lifespan shutdown 与 main 兜底共用。"""
    global _WATCHER_OBSERVER
    observer = _WATCHER_OBSERVER
    _WATCHER_OBSERVER = None
    if observer is not None:
        try:
            observer.stop()
            observer.join()
        except Exception:
            pass
    # 卡片索引持有独立 worker。必须先停止领取并等待当前 attempt 到达持久化
    # 安全点，之后才能关闭 Wiki pool 或释放 Chroma client。
    try:
        from mindos import knowledge as _knowledge
        _knowledge.stop_vector_worker(wait=True)
    except Exception as exc:
        logger.warning("服务停止时等待卡片索引 worker 失败: %s", type(exc).__name__)
    # 索引任务必须先到达安全终态，避免 HNSW 写入中途退出造成下次启动无法加载。
    from watcher import shutdown_pool
    shutdown_pool()
    wiki_store.shutdown_pool()
    # 停用单实例材料 worker（§6.1）：先停止领取，由下一轮启动恢复把 processing 转 paused。
    try:
        from mindos.material_worker import MaterialWorker
        MaterialWorker.instance().stop()
    except Exception:  # noqa: BLE001
        pass
    from mindos.derived import shutdown_pool as derived_shutdown_pool
    derived_shutdown_pool()
    # P2 §7.0.1 第 5 条：受控关闭模型任务 worker（先停止领取，安全点退出/租约兜底）。
    try:
        from mindos.model_job_worker import stop_worker

        stop_worker()
    except Exception:  # noqa: BLE001
        pass
    # 知君 P1：停止本体 worker（先停止领取；进行中的任务由租约兜底）。
    try:
        from mindos.zhijun.jobs import stop_worker as stop_ontology_worker

        stop_ontology_worker()
    except Exception:  # noqa: BLE001
        pass
    # 所有写入生产者停止后，等最后一段 Chroma 读写离开句柄再关闭 PersistentClient。
    from mindos.chat_imports import stop_worker as stop_chat_import_worker
    stop_chat_import_worker()
    # 不设置强制超时：超时后继续退出会重新引入 HNSW 写入中断风险。
    try:
        from vector_store import active_operations, release_chroma

        announced_wait = False
        while active_operations() > 0:
            if not announced_wait:
                logger.info("等待 %d 个活跃 Chroma 操作完成后安全关闭索引", active_operations())
                announced_wait = True
            time.sleep(0.1)
        if not release_chroma():
            # 理论上不应发生；保留保护以应对非受控第三方线程。
            logger.warning("服务停止时 Chroma 操作数变化，未能关闭 client")
        memory_store.release_memory_collection()
    except Exception as exc:
        logger.warning("服务停止时释放 ChromaDB 失败: %s", type(exc).__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """P0-1 单实例锁 + 后台服务随 ASGI 生命周期管理（覆盖所有启动方式）。

    - `python server.py`（main()）与 `uvicorn server:app`（纯 ASGI）共用此路径：
      此前锁与 watcher 都只在 main() 中处理，直接以 ASGI 方式启动会完全绕过；
    - 锁先于任何 ChromaDB 连接（lifespan startup 早于首个请求）；
    - 多 worker 部署直接拒绝（每个 worker 都是独立进程，共享 Chroma 目录必然竞态）；
    - main() 已提前持锁时此处幂等跳过获取，shutdown 时统一释放；
    - watcher / schema 迁移 / 预热 / 各守护线程统一在此启动（修复5）。
    """
    global _INSTANCE_LOCK
    workers = _detect_worker_count()
    if workers > 1:
        raise RuntimeError(
            f"检测到多 worker 部署（workers={workers}）：ChromaDB 嵌入式模式要求"
            "单进程独占数据目录，多 worker 共享会导致索引损坏（方案 P0-1）。"
            "请以单 worker 启动（uvicorn server:app 或 python server.py）。"
        )
    ok, holder = _acquire_instance_lock_once()
    if not ok:
        raise RuntimeError(
            f"数据目录已被其他进程占用（{holder}），拒绝启动。请先停止该进程；"
            "或为不同实例设置独立的 CENTAURAI_DATABASE_DATA_ROOT。"
        )
    _run_startup_health_check()
    # P1 模型运行时：启动时回收孤儿 secret_ref。失败不阻塞启动，页面只读降级。
    try:
        from mindos.runtime_config_provider import initialize_runtime_system

        initialize_runtime_system()
    except Exception as exc:  # noqa: BLE001
        print(f"[runtime] 运行时配置初始化失败（页面将只读/降级）：{exc}", flush=True)
    # 阶段 2：仅使用 Consumer API 的公开 JWKS 验证连接票据。配置不完整时保持
    # 默认拒绝；本机开发调试 Gate 仍可独立使用，不能据此推导生产票据有效。
    from mindos.connectivity_ticket import configure_ticket_verifier_from_environment

    ticket_verifier_status = configure_ticket_verifier_from_environment()
    logger.info("MindOS 连接票据验证器: %s", ticket_verifier_status)
    # 阶段 2：撤销同步接入生产启动（配置 MINDOS_CONSUMER_API_BASE_URL 后由
    # 连接状态 GC 线程每 5 秒轮询），使「撤销后 5 秒内关闭连接」可端到端生效。
    from mindos import consumer_adapter

    consumer_adapter.configure_revocation_sync(
        base_url=(os.environ.get("MINDOS_CONSUMER_API_BASE_URL") or "").strip() or None
    )
    logger.info("Consumer 撤销同步: %s", "enabled" if consumer_adapter.is_revocation_sync_configured() else "disabled")
    _start_background_services()
    try:
        yield
    finally:
        _stop_background_services()
        # 显式释放：进程退出 OS 也会兜底，这里让下一个实例可立即启动
        lock = _INSTANCE_LOCK
        _INSTANCE_LOCK = None
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


app = FastAPI(title="半人马AI 个人记忆库", version="1.0.0", lifespan=_lifespan)


@app.exception_handler(IndexCorruptedError)
async def index_corrupted_handler(_request: Request, _exc: IndexCorruptedError):
    """所有向量读写入口统一返回稳定错误，避免向浏览器泄漏 Chroma 内部异常。"""
    return JSONResponse(status_code=503, content={
        "detail": "index_corrupted",
        "code": "index_corrupted",
    })
_CURRENT_BIND_HOST = ""

# ---- 空闲自动卸载：记录最后 API 活动时间 ----
_LAST_ACTIVITY = time.monotonic()


# 内部周期任务发起的请求不算"活动"，否则空闲卸载永远不会触发
_ACTIVITY_EXEMPT_PATHS = {"/api/memory/reindex"}


@app.middleware("http")
async def _track_activity(request: Request, call_next):
    """每个 HTTP 请求都刷新最后活动时间戳——空闲卸载守护线程据此判断。"""
    global _LAST_ACTIVITY
    if request.url.path not in _ACTIVITY_EXEMPT_PATHS:
        _LAST_ACTIVITY = time.monotonic()
    return await call_next(request)


def _idle_unload_loop():
    """守护线程：连续无请求超过 IDLE_UNLOAD_MINUTES 分钟 → 释放模型 + ChromaDB 索引内存。

    释放后一切按需懒加载重建（embedder/vector_store getter 均为懒加载），
    服务功能不受影响，只是下次查询会多一次冷加载（索引 ~2GB，秒级）。
    """
    from config import IDLE_UNLOAD_MINUTES, IDLE_UNLOAD_CHECK_SECONDS

    if not IDLE_UNLOAD_MINUTES:
        logger.info("空闲自动卸载已禁用（IDLE_UNLOAD_MINUTES=0）")
        return
    interval = max(int(IDLE_UNLOAD_CHECK_SECONDS), 30)
    logger.info(f"空闲自动卸载已启用：连续 {IDLE_UNLOAD_MINUTES} 分钟无请求将释放模型与索引内存")
    while True:
        time.sleep(interval)
        idle_seconds = time.monotonic() - _LAST_ACTIVITY
        if idle_seconds < IDLE_UNLOAD_MINUTES * 60:
            continue
        # 有进行中的后台索引任务时跳过（避免关掉正在用的连接）
        try:
            from watcher import pending_jobs

            if pending_jobs() > 0:
                continue
        except Exception:
            pass
        logger.info(
            f"已空闲 {idle_seconds / 60:.0f} 分钟，释放模型 + 向量索引内存..."
        )
        try:
            from embedder import unload_all

            unload_all()
        except Exception as e:
            logger.warning(f"模型卸载失败（忽略）: {e}")
        try:
            from vector_store import release_chroma

            release_chroma()
            memory_store.release_memory_collection()
        except Exception as e:
            logger.warning(f"ChromaDB 释放失败（忽略）: {e}")


def _model_job_cleanup_loop():
    """P3 模型任务历史维护：启动时执行一次，之后按配置周期清理终态记录。"""
    from config import MODEL_JOB_CLEANUP_INTERVAL_SECONDS, MODEL_JOB_RETENTION_DAYS

    if MODEL_JOB_RETENTION_DAYS <= 0:
        logger.info("模型任务历史清理已禁用（CENTAUR_MODEL_JOB_RETENTION_DAYS=0）")
        return
    interval = max(int(MODEL_JOB_CLEANUP_INTERVAL_SECONDS), 3600)
    logger.info("模型任务终态历史保留 %d 天，清理间隔 %d 秒", MODEL_JOB_RETENTION_DAYS, interval)
    while True:
        try:
            from mindos.stores.model_job_store import ModelJobStore

            removed = ModelJobStore.instance().purge_expired_terminal_jobs(
                MODEL_JOB_RETENTION_DAYS
            )
            if removed:
                logger.info("已清理 %d 条过期模型任务历史", removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型任务历史清理失败: %s", type(exc).__name__)
        time.sleep(interval)


# 注册记忆管理路由
app.include_router(memory_api.router)

# 根路径重定向
@app.get("/")
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/lan")


# ========== LAN 路由 ==========

@app.post("/lan/login")
def lan_login(req: LanLoginReq):
    if not _lan_active():
        raise HTTPException(403, "LAN 未启用")
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(401, "密码错误")
    token = _secrets.token_urlsafe(32)
    _lan_tokens[token] = time.time() + 86400
    return {"token": token}


@app.get("/lan/status")
def lan_status(x_lan_token: Optional[str] = Header(default=None)):
    return {"ok": _check_lan_token(x_lan_token)}


@app.post("/lan/upload")
async def lan_upload(file: UploadFile = File(...),
                     x_lan_token: Optional[str] = Header(default=None)):
    if not _check_lan_token(x_lan_token):
        raise HTTPException(401, "未授权")
    if not file.filename:
        raise HTTPException(400, "文件名为空")
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"不支持: {ext}")
    dest = Path(WATCH_FOLDER) / file.filename
    if dest.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = Path(WATCH_FOLDER) / f"{Path(file.filename).stem}_{ts}{ext}"
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "文件过大")
    dest.write_bytes(content)
    submit_index(str(dest), force=True)
    return {"success": True, "path": str(dest), "name": dest.name}


@app.get("/lan")
def lan_page():
    """局域网文件导入页面"""
    from fastapi.responses import FileResponse
    page = Path(PROJECT_ROOT) / "frontend" / "renderer" / "lan_import.html"
    return FileResponse(str(page))


@app.get("/mobile")
@app.get("/mobile/")
@app.get("/mobile/share")
@app.post("/mobile/share")
def mobile_page():
    """手机端 PWA 入口。"""
    page = Path(PROJECT_ROOT) / "frontend" / "mobile" / "index.html"
    if not page.exists():
        raise HTTPException(404, "手机端页面不存在")
    return FileResponse(str(page))


@app.get("/mobile/{asset_path:path}")
def mobile_asset(asset_path: str):
    root_dir = (Path(PROJECT_ROOT) / "frontend" / "mobile").resolve()
    target = (root_dir / asset_path).resolve()
    if not target.is_file() or not target.is_relative_to(root_dir):
        raise HTTPException(404, "资源不存在")
    return FileResponse(str(target))


@app.get("/assets/{asset_path:path}")
def frontend_asset(asset_path: str):
    root_dir = (Path(PROJECT_ROOT) / "frontend" / "assets").resolve()
    target = (root_dir / asset_path).resolve()
    if not target.is_file() or not target.is_relative_to(root_dir):
        raise HTTPException(404, "资源不存在")
    return FileResponse(str(target))


# ========== MindOS 浏览器前端（Vue 3 + TypeScript + Vite）==========
# 生产环境由本入口在 /mindos 下提供构建产物；开发环境由 Vite dev server 代理 /api。
# Vite base 为 /mindos/，故 index.html 引用 /mindos/assets/...；此处同时处理 SPA 前端路由回退。
_MINDOS_DIST = (Path(PROJECT_ROOT) / "frontend" / "mindos-web" / "dist").resolve()


def _mindos_index():
    index = _MINDOS_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "MindOS 前端尚未构建，请先运行 npm run build")
    return FileResponse(str(index))


@app.get("/mindos")
@app.get("/mindos/")
def mindos_page():
    return _mindos_index()


@app.get("/mindos/{asset_path:path}")
def mindos_asset(asset_path: str):
    target = (_MINDOS_DIST / asset_path).resolve()
    # 命中真实构建资源（js/css/图片等）直接返回；否则作为 SPA 前端路由回退到 index.html
    if target.is_file() and target.is_relative_to(_MINDOS_DIST):
        return FileResponse(str(target))
    return _mindos_index()


# 不向任何跨域来源回 ACAO —— 真实浏览器(强制同源策略)无法【读取】本地库响应。
# 注意：CORS 不阻止跨域【发送】简单请求(无预检的 GET / 无自定义头的 POST)，故改动型
# 端点(upload/reindex/delete)另用 require_local 的自定义头做 CSRF 防护(见下)。
# Electron 渲染端走 webSecurity:false 直读；CentaurAI 经服务端代理(Node fetch)访问。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_methods=["*"],   # allow_origins=[] 下二者为 no-op（永不匹配任何 origin），仅占位
    allow_headers=["*"],
)


# CSRF 防护：改动型端点要求一个非安全列表(non-safelisted)自定义头。跨域简单请求无法
# 携带此头，加上后会触发 CORS 预检——而预检在 allow_origins=[] 下被拒(400)，于是恶意网页
# 既无法静默发起，也拿不到响应。本应用 preload.js 与 CentaurAI 服务端代理都会显式带上。
_CSRF_TOKEN = "centaur-vdb"


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_local(request: Request, x_requested_by: Optional[str] = Header(default=None)):
    if x_requested_by != _CSRF_TOKEN:
        raise HTTPException(403, "缺少 X-Requested-By 头（跨站请求防护）")
    if not _is_loopback_request(request):
        raise HTTPException(403, "本机管理接口仅允许 loopback 访问")


def require_loopback(request: Request):
    if not _is_loopback_request(request):
        raise HTTPException(403, "本机管理接口仅允许 loopback 访问")


# 阶段 1：未配置 AI 盒子时，Web 只能在显式开启的本机开发模式下访问 MindOS。
# 阶段 2 会在此唯一边界接入 Consumer Connectivity Ticket 校验，不能再让路由各自
# 增加临时回退。当前 Gate 关闭时 fail-closed，避免误把 loopback 当作生产鉴权。
from mindos.local_web_debug import ACCESS_MODE_LOCAL_DEBUG, access_context as local_web_access_context
from mindos.connectivity_ticket import ConnectivityTicketError
from mindos.connectivity_session import SESSION_HEADER, validate_session
from mindos.device_context import get_device_registry


def _mindos_web_access_context() -> dict[str, str | bool]:
    return local_web_access_context(bind_host=_CURRENT_BIND_HOST or HOST)


def require_mindos_web_access(request: Request):
    if not _is_loopback_request(request):
        raise HTTPException(403, "MindOS Web 仅允许 loopback 直连访问")
    context = _mindos_web_access_context()
    if context["mode"] == ACCESS_MODE_LOCAL_DEBUG:
        # 本机调试模式不创建真实设备上下文：不得声称或写入 Consumer device_id。
        request.state.mindos_access_context = context
        request.state.mindos_device_context = None
        return context
    # 票据是一次性交换凭证，业务请求必须携带交换得到的受控会话凭证；
    # 同一票据不能逐请求消费（nonce 单次使用），否则第二个请求即被判重放。
    session_token = request.headers.get(SESSION_HEADER)
    if not session_token:
        raise HTTPException(401, "需要连接会话凭证（请先用连接票据交换会话）")
    try:
        principal = validate_session(session_token, method=request.method, path=request.url.path)
    except ConnectivityTicketError as exc:
        # 仅返回稳定错误信息；会话凭证原文绝不能写入响应或日志。
        raise HTTPException(exc.status_code, exc.message) from None
    request.state.mindos_connectivity_principal = principal
    request.state.mindos_access_context = {"mode": "connectivity_ticket"}
    # 阶段 2：按真实 device_id 登记运行时上下文；业务缓存/任务/会话以
    # request.state.mindos_device_context 的命名空间为准，禁止自行拼接设备键。
    request.state.mindos_device_context = get_device_registry().get_or_create(principal)
    return principal


@app.get("/api/mindos/access-context", dependencies=[Depends(require_loopback)])
def get_mindos_web_access_context():
    """供 Web 首屏展示当前访问模式；不返回令牌、主机路径或用户身份。

    票据模式下附带本机 device_id（盒子自身标识，非用户身份），供前端完成
    「认领→票据→交换」闭环；本机调试模式不返回设备标识。
    """
    context = _mindos_web_access_context()
    if context["mode"] != ACCESS_MODE_LOCAL_DEBUG:
        context["deviceId"] = os.environ.get("MINDOS_DEVICE_ID") or ""
    return context


# 注册 MindOS 路由。写操作复用既有的 loopback + CSRF 防护；业务读写统一经过
# 本机调试/未来连接票据的单一 Gate，状态查询同样不能绕过该 Gate。
_MINDOS_WEB_DEPENDENCIES = [Depends(require_loopback), Depends(require_mindos_web_access)]
from mindos import uploads as mindos_uploads
mindos_uploads.configure_write_guard(require_local)
app.include_router(mindos_uploads.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import knowledge as mindos_knowledge
mindos_knowledge.configure_write_guard(require_local)
app.include_router(mindos_knowledge.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import search as mindos_search
app.include_router(mindos_search.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import qa as mindos_qa
mindos_qa.configure_write_guard(require_local)
app.include_router(mindos_qa.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import related as mindos_related
app.include_router(mindos_related.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import generations as mindos_generations
mindos_generations.configure_write_guard(require_local)
app.include_router(mindos_generations.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import corrections as mindos_corrections
mindos_corrections.configure_write_guard(require_local)
app.include_router(mindos_corrections.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import graph as mindos_graph
app.include_router(mindos_graph.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import governance as mindos_governance
mindos_governance.configure_write_guard(require_local)
app.include_router(mindos_governance.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
# 服务启动时恢复进程崩溃遗留的 processing 中间态（运行中不自动恢复，避免回收仍在执行的仲裁）。
mindos_governance.recover_stale_processing()
from mindos import lifecycle as mindos_lifecycle
mindos_lifecycle.configure_write_guard(require_local)
app.include_router(mindos_lifecycle.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import home as mindos_home
app.include_router(mindos_home.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import growth as mindos_growth
mindos_growth.configure_write_guard(require_local)
app.include_router(mindos_growth.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
# 知君 P1：对话（SSE）、本体（理解 / 复核 / 投影）、运行状态。契约见 docs/development/zhijun-api-contract.md。
from mindos import conversations as mindos_conversations
mindos_conversations.configure_write_guard(require_local)
app.include_router(mindos_conversations.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos.memory_routes import build_router as build_memory_router
app.include_router(build_memory_router(require_local), dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos.matters_routes import build_router as build_matters_router
app.include_router(build_matters_router(require_local), dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos.chat_import_routes import build_router as build_chat_import_router
app.include_router(build_chat_import_router(require_local), dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos.zhijun.charter import build_router as build_charter_router
app.include_router(build_charter_router(require_local), dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import ontology as mindos_ontology
mindos_ontology.configure_write_guard(require_local)
app.include_router(mindos_ontology.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import zhijun_status as mindos_zhijun_status
app.include_router(mindos_zhijun_status.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import zhijun_home as mindos_zhijun_home
app.include_router(mindos_zhijun_home.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import zhijun_onboarding as mindos_zhijun_onboarding
mindos_zhijun_onboarding.configure_write_guard(require_local)
app.include_router(mindos_zhijun_onboarding.router, dependencies=_MINDOS_WEB_DEPENDENCIES)
from mindos import nudges as mindos_nudges
mindos_nudges.configure_write_guard(require_local)
app.include_router(mindos_nudges.router, dependencies=_MINDOS_WEB_DEPENDENCIES)

# P1 模型运行时设置管理路由：全部 require_local（loopback + CSRF，GET 不放松，§6）。
# 统一错误响应 {code, message, details?}（§6.2.1）由 install_error_handlers 注册。
from mindos import model_runtime as mindos_model_runtime
mindos_model_runtime.configure_guards(require_local)
app.include_router(mindos_model_runtime.router)
mindos_model_runtime.install_error_handlers(app)

# 外部 Agent Gateway（AG-01）：/v1/agent 为唯一外部 REST 入口，使用 Bearer 凭证，
# 不依赖 loopback（外部接入经 HTTPS 边缘服务）。统一错误处理与 traceId 中间件由 install 注册。
from mindos.agent import router as agent_router
from mindos.agent import admin as agent_admin
agent_router.install(app)
app.include_router(agent_router.router)

# 令牌创建/轮换/停用与审计查看仅限本机（写 require_local，读 require_loopback）。
agent_admin.configure_admin_guards(require_local, require_loopback)
app.include_router(agent_admin.admin_router)

# 阶段 2：Consumer Connectivity 本机管理（撤销 / epoch 轮换 / 设备 ACL）。
# 生产事件源接入后，Consumer Webhook 复用同一 store API 并触发设备上下文失效。
from mindos import connectivity_admin as mindos_connectivity_admin
mindos_connectivity_admin.configure_admin_guards(require_local, require_loopback)
app.include_router(mindos_connectivity_admin.router)


# LAN 配置持久化
_LAN_CFG = LAN_CONFIG_PATH
_MOBILE_CFG = MOBILE_CONFIG_PATH
_CONTEXT_PACKS_CFG = CONTEXT_PACKS_CONFIG_PATH


class LanConfigReq(BaseModel):
    enabled: bool = False
    password: str = ""


class MobileConfigReq(BaseModel):
    enabled: bool = True
    token: str = ""
    generate: bool = False
    clear_token: bool = False


class McpRemoteConfigReq(BaseModel):
    enabled: bool = True
    mode: Literal["basic", "advanced"] = "basic"
    admin_password: str = ""


class McpClientReq(BaseModel):
    label: str
    tier: str = "kb"


class MobileClipReq(BaseModel):
    title: str = ""
    url: str = ""
    content: str = ""
    text: str = ""
    tags: Optional[list[str]] = None


class ContextPackReq(BaseModel):
    name: str
    description: str = ""
    query: str = ""
    enabled: bool = True
    include_memory: bool = True
    include_wiki: bool = True
    include_documents: bool = True
    limit_chars: int = 6000
    token: str = ""
    generate: bool = False


class MobileContextQueryReq(BaseModel):
    query: str
    include_memory: bool = True
    include_wiki: bool = True
    include_documents: bool = True
    limit_chars: int = 8000


class A2AMessageReq(BaseModel):
    message: dict
    configuration: Optional[dict] = None


class WikiSearchReq(BaseModel):
    query: str
    n_results: int = 10


class WikiCreatePageReq(BaseModel):
    title: str
    folder: str = "Resources"
    content: str = ""
    tags: Optional[list[str]] = None
    page_type: str = ""


class WikiWritePageReq(BaseModel):
    content: str
    source_agent: str = "vector-db-ui"


class WikiOrganizeReq(BaseModel):
    source_path: str
    force: bool = False


class GBrainSearchReq(BaseModel):
    query: str
    mode: str = "hybrid"
    limit: int = 12


class GBrainCaptureReq(BaseModel):
    title: str
    content: str
    page_type: str = "note"
    tags: Optional[list[str]] = None
    slug: str = ""


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _write_json_file(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _mobile_cfg() -> dict:
    cfg = _read_json_file(_MOBILE_CFG)
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "token": str(cfg.get("token") or ""),
        "updated_at": cfg.get("updated_at", ""),
    }


def _mobile_active() -> bool:
    cfg = _mobile_cfg()
    return bool(cfg.get("enabled") and cfg.get("token"))


def _external_bind_active() -> bool:
    cfg = mcp_access.get_runtime_config()
    return bool(cfg.get("enabled") and cfg.get("public_base"))


def _get_mobile_app_urls() -> list[str]:
    remote = mcp_access.get_runtime_config()
    if remote.get("enabled") and remote.get("public_base"):
        return [f"{str(remote['public_base']).rstrip('/')}/mobile"]
    return []


def _get_mobile_api_urls() -> list[str]:
    remote = mcp_access.get_runtime_config()
    if remote.get("enabled") and remote.get("public_base"):
        return [f"{str(remote['public_base']).rstrip('/')}/api/mobile"]
    return []


def _qr_data_url(text: str) -> str:
    if not text:
        return ""
    try:
        import qrcode
        import qrcode.image.svg
        from io import BytesIO

        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
        buf = BytesIO()
        img.save(buf)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"
    except Exception as e:
        logger.debug(f"生成手机配对二维码失败: {e}")
        return ""


def require_mobile(
    authorization: Optional[str] = Header(default=None),
    x_mobile_token: Optional[str] = Header(default=None),
):
    cfg = _mobile_cfg()
    if not cfg.get("enabled") or not cfg.get("token"):
        raise HTTPException(403, "手机导入未启用")
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    token = token or (x_mobile_token or "").strip()
    if not token or not _secrets.compare_digest(token, cfg["token"]):
        raise HTTPException(401, "无效的 App Token")


def _normalize_pack(raw: dict) -> dict:
    pack_id = str(raw.get("id") or uuid.uuid4().hex[:12])
    token = str(raw.get("token") or "")
    now = datetime.now().isoformat()
    try:
        limit_chars = int(raw.get("limit_chars", 6000) or 6000)
    except (TypeError, ValueError):
        limit_chars = 6000
    return {
        "id": pack_id,
        "name": str(raw.get("name") or "Personal Context")[:80],
        "description": str(raw.get("description") or "")[:500],
        "query": str(raw.get("query") or "")[:1000],
        "enabled": bool(raw.get("enabled", True)),
        "include_memory": bool(raw.get("include_memory", True)),
        "include_wiki": bool(raw.get("include_wiki", True)),
        "include_documents": bool(raw.get("include_documents", True)),
        "limit_chars": max(1000, min(30000, limit_chars)),
        "token": token,
        "created_at": str(raw.get("created_at") or now),
        "updated_at": str(raw.get("updated_at") or now),
    }


def _read_context_packs() -> list[dict]:
    data = _read_json_file(_CONTEXT_PACKS_CFG)
    raw_items = data.get("packs") if isinstance(data, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    return [_normalize_pack(p) for p in raw_items if isinstance(p, dict)]


def _write_context_packs(packs: list[dict]) -> None:
    _write_json_file(_CONTEXT_PACKS_CFG, {"packs": packs, "updated_at": datetime.now().isoformat()})


def _pack_public(pack: dict, include_token: bool = False) -> dict:
    out = {k: v for k, v in pack.items() if k != "token"}
    token = pack.get("token", "")
    out["has_token"] = bool(token)
    out["token_suffix"] = token[-6:] if token else ""
    if include_token:
        out["token"] = token
    return out


def _get_context_pack(pack_id: str) -> dict:
    for pack in _read_context_packs():
        if pack["id"] == pack_id:
            return pack
    raise HTTPException(404, "Context Pack 不存在")


def require_a2a_pack(pack_id: str, authorization: Optional[str] = Header(default=None)) -> dict:
    pack = _get_context_pack(pack_id)
    if not pack.get("enabled"):
        raise HTTPException(403, "Context Pack 未启用")
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not pack.get("token") or not token or not _secrets.compare_digest(token, pack["token"]):
        raise HTTPException(401, "无效的 Context Token")
    return pack


@app.post("/api/lan/config", dependencies=[Depends(require_local)])
def set_lan_config(req: LanConfigReq):
    global LAN_ENABLED, ADMIN_PASSWORD

    existing = {}
    try:
        existing = json.loads(_LAN_CFG.read_text()) if _LAN_CFG.exists() else {}
    except Exception:
        existing = {}
    password = req.password or existing.get("password") or ADMIN_PASSWORD
    if req.enabled and not password:
        raise HTTPException(400, "启用 LAN 时必须设置密码")

    _LAN_CFG.write_text(json.dumps({"enabled": req.enabled, "password": password},
                                    ensure_ascii=False, indent=2))

    credentials_changed = LAN_ENABLED != req.enabled or ADMIN_PASSWORD != password
    LAN_ENABLED = req.enabled
    ADMIN_PASSWORD = password
    os.environ["VDB_LAN_ENABLED"] = "true" if req.enabled else "false"
    if password:
        os.environ["VDB_ADMIN_PASSWORD"] = password
    else:
        os.environ.pop("VDB_ADMIN_PASSWORD", None)
    if credentials_changed:
        _lan_tokens.clear()

    restart_required = req.enabled and not _external_bind_active()
    return {
        "success": True,
        "active": _lan_active() and _external_bind_active(),
        "restart_required": restart_required,
    }


@app.get("/api/lan/config")
def get_lan_config():
    """前端读取当前 LAN 配置"""
    try:
        cfg = json.loads(_LAN_CFG.read_text()) if _LAN_CFG.exists() else {}
    except Exception:
        cfg = {}
    configured_enabled = bool(cfg.get("enabled", LAN_ENABLED))
    configured_password = str(cfg.get("password") or "")
    runtime_in_sync = (
        configured_enabled == LAN_ENABLED
        and (not configured_enabled or configured_password == ADMIN_PASSWORD)
    )
    restart_required = (
        not runtime_in_sync
        or (configured_enabled and not _external_bind_active())
    )
    urls = _get_lan_urls()
    return {
        "enabled": configured_enabled,
        "password_set": bool(configured_password or ADMIN_PASSWORD),
        "active": configured_enabled and runtime_in_sync and _lan_active() and _external_bind_active(),
        "restart_required": restart_required,
        "urls": urls,
        "url": (urls or [""])[0],
        "port": int(mcp_access.get_runtime_config().get("lan_http_port", 8080)),
        "scheme": "http",
    }


# ========== 手机端 App Token + Tailscale 导入 ==========

@app.get("/api/mobile/config")
def get_mobile_config():
    cfg = _mobile_cfg()
    token = cfg.get("token", "")
    urls = _get_mobile_app_urls()
    api_urls = _get_mobile_api_urls()
    return {
        "enabled": cfg.get("enabled", False),
        "active": _mobile_active() and _external_bind_active(),
        "has_token": bool(token),
        "token_suffix": token[-6:] if token else "",
        "updated_at": cfg.get("updated_at", ""),
        "urls": urls,
        "url": (urls or [""])[0],
        "api_urls": api_urls,
        "api_url": (api_urls or [""])[0],
        "port": PORT,
        "restart_required": _mobile_active() and not _external_bind_active(),
    }


@app.post("/api/mobile/config", dependencies=[Depends(require_local)])
def set_mobile_config(req: MobileConfigReq):
    existing = _mobile_cfg()
    token = (req.token or "").strip()
    generated = False
    if req.clear_token:
        token = ""
    elif req.generate or (req.enabled and not token and not existing.get("token")):
        token = _secrets.token_urlsafe(32)
        generated = True
    if not token and not req.clear_token:
        token = existing.get("token", "")
    cfg = {"enabled": req.enabled, "token": token, "updated_at": datetime.now().isoformat()}
    _write_json_file(_MOBILE_CFG, cfg)
    data = get_mobile_config()
    if generated:
        data["token"] = token
    return {"success": True, **data}


@app.post("/api/mobile/pairing", dependencies=[Depends(require_local)])
def create_mobile_pairing():
    cfg = _mobile_cfg()
    token = cfg.get("token", "")
    generated = False
    if not token:
        token = _secrets.token_urlsafe(32)
        generated = True
    if not cfg.get("enabled") or generated:
        cfg = {"enabled": True, "token": token, "updated_at": datetime.now().isoformat()}
        _write_json_file(_MOBILE_CFG, cfg)
    data = get_mobile_config()
    urls = data.get("urls") or []
    pairing_urls = [
        f"{url}#token={quote_plus(token)}"
        for url in urls
        if url
    ]
    qr_urls = [_qr_data_url(u) for u in pairing_urls]
    return {
        "success": True,
        "generated": generated,
        "enabled": True,
        "token": token,
        "token_suffix": token[-6:] if token else "",
        "url": (pairing_urls or [""])[0],
        "urls": pairing_urls,
        "qr_data_url": (qr_urls or [""])[0],
        "qr_data_urls": qr_urls,
        "app_url": data.get("url", ""),
        "app_urls": urls,
        "restart_required": data.get("restart_required", False),
    }


def _safe_upload_dest(filename: str, subdir: str) -> tuple[Path, str]:
    if not filename:
        raise HTTPException(400, "文件名为空")
    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(400, "非法文件名")
    ext = Path(safe_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {ext}")
    dest_dir = (Path(WATCH_FOLDER) / subdir).resolve()
    watch_root = Path(WATCH_FOLDER).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}").resolve()
    if not dest.is_relative_to(watch_root):
        raise HTTPException(400, "非法文件名")
    return dest, safe_name


def _resolve_watch_doc(doc_id: str) -> Path:
    from urllib.parse import unquote

    watch_root = Path(WATCH_FOLDER).resolve()
    path = Path(unquote(doc_id)).resolve()
    if not path.is_relative_to(watch_root):
        raise HTTPException(400, "doc_id 不在监控目录内")
    return path


async def _save_upload(file: UploadFile, subdir: str, allowed_exts: Optional[set[str]] = None) -> tuple[Path, str]:
    dest, safe_name = _safe_upload_dest(file.filename or "", subdir)
    if allowed_exts is not None and dest.suffix.lower() not in allowed_exts:
        raise HTTPException(400, f"该接口仅支持: {', '.join(sorted(allowed_exts))}")
    staging_dir = Path(WATCH_FOLDER).resolve().parent / ".upload_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging = staging_dir / f"{uuid.uuid4().hex}.uploading"
    written = 0
    try:
        with open(staging, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"文件超过上限 {MAX_UPLOAD_BYTES // (1024 ** 3)}GB")
                f.write(chunk)
        if written == 0:
            raise HTTPException(400, "上传文件为空")
        os.replace(staging, dest)
    except HTTPException:
        staging.unlink(missing_ok=True)
        raise
    except Exception as e:
        staging.unlink(missing_ok=True)
        raise HTTPException(500, f"上传失败: {e}")
    return dest, safe_name


def _mobile_result_payload(path: Path) -> dict:
    source_path = str(path)
    index_job = get_job(source_path)
    wiki_jobs = [
        j for j in wiki_store.list_jobs(include_done=True)
        if j.get("source_path") == source_path
    ][:5]
    chunks = get_source_chunks(source_path, limit=200)
    transcript = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        transcript.append(
            {
                "text": chunk.get("text", ""),
                "start_time": meta.get("start_time"),
                "end_time": meta.get("end_time"),
                "modality": meta.get("modality", "text"),
            }
        )
    full_text = "\n".join(c["text"] for c in transcript if c.get("text")).strip()
    wiki_page = wiki_store.page_for_source(source_path)
    summary = ""
    if wiki_page:
        summary = wiki_page.get("summary") or ""
    if not summary and full_text:
        summary = full_text[:500]
    return {
        "doc_id": source_path,
        "file_name": path.name,
        "exists": path.exists(),
        "index": index_job,
        "wiki": wiki_jobs,
        "ready": bool(chunks) or bool(wiki_page),
        "summary": summary,
        "transcript": transcript,
        "text": full_text,
        "wiki_page": (
            {
                "path": wiki_page.get("path"),
                "title": wiki_page.get("title"),
                "summary": wiki_page.get("summary"),
            }
            if wiki_page
            else None
        ),
    }


_MOBILE_SOURCE_DIRS = {
    "recording": "mobile_recordings",
    "file": "mobile_uploads",
    "clip": "mobile_clips",
}


def _mobile_kind_for_path(path: Path) -> str:
    parent = path.parent.name
    for kind, subdir in _MOBILE_SOURCE_DIRS.items():
        if parent == subdir:
            return kind
    return "file"


def _mobile_item_payload(path: Path) -> dict:
    source_path = str(path)
    job = get_job(source_path)
    wiki_jobs = [
        j for j in wiki_store.list_jobs(include_done=True)
        if j.get("source_path") == source_path
    ][:3]
    chunks = get_source_chunks(source_path, limit=1)
    wiki_page = wiki_store.page_for_source(source_path)
    ann = annotations.get(source_path) or {}
    summary = ""
    if wiki_page:
        summary = wiki_page.get("summary") or ""
    if not summary and chunks:
        summary = (chunks[0].get("text") or "")[:240]
    title = path.name
    caption = (ann.get("caption") or "").strip()
    if caption:
        first_line = caption.splitlines()[0].strip()
        title = first_line.replace("录音标题：", "", 1).strip() or title
    ready = bool(chunks) or bool(wiki_page)
    state = job.get("state", "unknown")
    if ready and state in {"unknown", "done"}:
        state = "ready"
    stat = path.stat()
    return {
        "doc_id": source_path,
        "kind": _mobile_kind_for_path(path),
        "file_name": path.name,
        "title": title,
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "state": state,
        "index": job,
        "wiki": wiki_jobs,
        "ready": ready,
        "summary": summary,
        "wiki_page": (
            {
                "path": wiki_page.get("path"),
                "title": wiki_page.get("title"),
                "summary": wiki_page.get("summary"),
            }
            if wiki_page
            else None
        ),
        "result_url": f"/api/mobile/results/{quote(source_path, safe='')}",
    }


def _mobile_basic_item_payload(path: Path) -> dict:
    source_path = str(path)
    job = get_job(source_path)
    state = job.get("state", "unknown")
    stat = path.stat()
    return {
        "doc_id": source_path,
        "kind": _mobile_kind_for_path(path),
        "file_name": path.name,
        "title": path.name,
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "state": state,
        "ready": state == "done",
        "summary": "",
        "result_url": f"/api/mobile/results/{quote(source_path, safe='')}",
    }


def _list_mobile_source_paths(kind: str = "") -> list[Path]:
    requested = (kind or "").strip().lower()
    if requested and requested not in _MOBILE_SOURCE_DIRS:
        raise HTTPException(400, "kind 仅支持 recording/file/clip")
    watch_root = Path(WATCH_FOLDER).resolve()
    paths: list[Path] = []
    for item_kind, subdir in _MOBILE_SOURCE_DIRS.items():
        if requested and requested != item_kind:
            continue
        root = (watch_root / subdir).resolve()
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                target = path.resolve()
                if target.is_relative_to(watch_root):
                    paths.append(target)
            except Exception:
                continue
    paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return paths


@app.post("/api/mobile/uploads", dependencies=[Depends(require_mobile)])
async def mobile_upload(file: UploadFile = File(...)):
    dest, safe_name = await _save_upload(file, "mobile_uploads")
    submit_index(str(dest), force=True)
    return {
        "success": True,
        "queued": True,
        "file_name": safe_name,
        "saved_path": str(dest),
        "doc_id": str(dest),
    }


@app.post("/api/mobile/recordings", dependencies=[Depends(require_mobile)])
async def mobile_recording(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    note: str = Form(default=""),
    started_at: str = Form(default=""),
):
    dest, safe_name = await _save_upload(file, "mobile_recordings", SUPPORTED_AUDIO_EXTENSIONS | {".webm"})
    if dest.suffix.lower() == ".webm":
        audio_dest = dest.with_suffix(".opus")
        dest.rename(audio_dest)
        dest = audio_dest
        safe_name = Path(safe_name).with_suffix(".opus").name
    source_path = str(dest)
    caption_bits = []
    if title.strip():
        caption_bits.append(f"录音标题：{title.strip()}")
    if started_at.strip():
        caption_bits.append(f"录音时间：{started_at.strip()}")
    if note.strip():
        caption_bits.append(f"用户备注：{note.strip()}")
    if caption_bits:
        annotations.set_annotation(
            source_path,
            {
                "caption": "\n".join(caption_bits),
                "tags": ["手机录音"],
                "group": "手机录音",
            },
            merge=True,
        )
    submit_index(source_path, force=True)
    return {
        "success": True,
        "queued": True,
        "kind": "recording",
        "file_name": safe_name,
        "saved_path": source_path,
        "doc_id": source_path,
        "result_url": f"/api/mobile/results/{quote(source_path, safe='')}",
    }


@app.post("/api/mobile/clips", dependencies=[Depends(require_mobile)])
def mobile_clip(req: MobileClipReq):
    clip_text = (req.content or req.text or "").strip()
    if not clip_text:
        raise HTTPException(400, "剪藏内容为空")
    title = req.title.strip() or (req.url.strip() or "手机剪藏")
    safe_title = "".join(c if c.isalnum() or c in " -_（）()[]【】" else " " for c in title)
    safe_title = " ".join(safe_title.split())[:60] or "手机剪藏"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = (Path(WATCH_FOLDER) / "mobile_clips").resolve()
    watch_root = Path(WATCH_FOLDER).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / f"{stamp}_{safe_title}.md").resolve()
    if not dest.is_relative_to(watch_root):
        raise HTTPException(400, "非法标题")
    tags = req.tags or []
    front = {
        "title": title,
        "source": "mobile_clip",
        "url": req.url.strip(),
        "tags": tags,
        "created_at": datetime.now().isoformat(),
    }
    body = [
        "---",
        *[
            f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, str)) else v}"
            for k, v in front.items()
            if v
        ],
        "---",
        "",
        f"# {title}",
        "",
    ]
    if req.url.strip():
        body.extend([f"来源：{req.url.strip()}", ""])
    body.append(clip_text)
    dest.write_text("\n".join(body) + "\n", encoding="utf-8")
    submit_index(str(dest), force=True)
    return {"success": True, "queued": True, "saved_path": str(dest), "doc_id": str(dest)}


@app.get("/api/mobile/jobs/{doc_id:path}", dependencies=[Depends(require_mobile)])
def mobile_job_status(doc_id: str):
    path = _resolve_watch_doc(doc_id)
    return {
        "index": get_job(str(path)),
        "wiki": [
            j for j in wiki_store.list_jobs(include_done=True)
            if j.get("source_path") == str(path)
        ][:5],
    }


@app.get("/api/mobile/results/{doc_id:path}", dependencies=[Depends(require_mobile)])
def mobile_result(doc_id: str):
    return _mobile_result_payload(_resolve_watch_doc(doc_id))


@app.get("/api/mobile/items", dependencies=[Depends(require_mobile)])
def mobile_items(
    kind: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    detail: str = Query(default="full"),
):
    detail_mode = (detail or "full").strip().lower()
    if detail_mode not in {"basic", "full"}:
        raise HTTPException(400, "detail 仅支持 basic/full")
    paths = _list_mobile_source_paths(kind)
    selected_paths = paths[:limit]
    payload = _mobile_basic_item_payload if detail_mode == "basic" else _mobile_item_payload
    return {
        "items": [payload(path) for path in selected_paths],
        "total": len(paths),
        "limit": limit,
        "kind": (kind or "").strip().lower() or "all",
        "detail": detail_mode,
    }


@app.post("/api/mobile/context/query", dependencies=[Depends(require_mobile)])
def mobile_context_query(req: MobileContextQueryReq):
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(400, "查询内容为空")
    pack = _normalize_pack(
        {
            "id": "mobile-context",
            "name": "Mobile Context",
            "query": query,
            "enabled": True,
            "include_memory": req.include_memory,
            "include_wiki": req.include_wiki,
            "include_documents": req.include_documents,
            "limit_chars": req.limit_chars,
        }
    )
    payload = _build_context(pack, "")
    payload["mode"] = "mobile-context"
    return payload


@app.get("/api/mobile/context/snapshot", dependencies=[Depends(require_mobile)])
def mobile_context_snapshot():
    return context_snapshot.read_snapshot(auto_build=True)


@app.post("/api/mobile/context/snapshot/refresh", dependencies=[Depends(require_mobile)])
def mobile_refresh_context_snapshot():
    return {"success": True, "snapshot": context_snapshot.build_snapshot(force=True)}


@app.get("/api/mobile/context/packs", dependencies=[Depends(require_mobile)])
def mobile_list_context_packs():
    return {"packs": [_pack_public(p) for p in _read_context_packs()]}


@app.post("/api/mobile/context/packs", dependencies=[Depends(require_mobile)])
def mobile_create_context_pack(req: ContextPackReq):
    return create_context_pack(req)


@app.put("/api/mobile/context/packs/{pack_id}", dependencies=[Depends(require_mobile)])
def mobile_update_context_pack(pack_id: str, req: ContextPackReq):
    return update_context_pack(pack_id, req)


@app.delete("/api/mobile/context/packs/{pack_id}", dependencies=[Depends(require_mobile)])
def mobile_delete_context_pack(pack_id: str):
    return delete_context_pack(pack_id)


@app.get("/api/mobile/context/packs/{pack_id}/preview", dependencies=[Depends(require_mobile)])
def mobile_preview_context_pack(pack_id: str, q: str = Query(default="")):
    pack = _get_context_pack(pack_id)
    return _build_context(pack, q)


@app.get("/api/mobile/context/packs/{pack_id}/invite", dependencies=[Depends(require_mobile)])
def mobile_context_pack_invite(pack_id: str, request: Request):
    pack = _get_context_pack(pack_id)
    if not pack.get("enabled"):
        raise HTTPException(403, "Context Pack 未启用")
    return _a2a_invite_for_pack(pack, request)


class RagConfigReq(BaseModel):
    default_strategy: Optional[str] = None
    file_type_strategies: Optional[dict[str, str]] = None


@app.get("/api/rag/strategies")
def get_rag_strategies():
    return rag_strategy.config_payload()


@app.get("/api/rag/config")
def get_rag_config():
    return rag_strategy.config_payload()


@app.post("/api/rag/config", dependencies=[Depends(require_local)])
def set_rag_config(req: RagConfigReq):
    cfg = rag_strategy.save_config(req.dict(exclude_none=True))
    return {
        "success": True,
        "config": cfg,
        "effective": rag_strategy.config_payload()["effective"],
        "reindex_required": True,
    }


class SearchRequest(BaseModel):
    query: str
    n_results: int = 10
    file_type: Optional[str] = None
    # text=文本检索(默认,BGE+重排) | visual=以文搜图(Chinese-CLIP) | hybrid=两者合并
    mode: str = "text"
    # 标签筛选：仅返回带有「全部」这些标签的文件命中（AND 语义；空=不筛）
    tags: Optional[list[str]] = None


class RetrievalRequest(BaseModel):
    """面向应用端的稳定检索契约；屏蔽底层文档/Wiki/记忆的异构字段。"""
    query: str
    scope: Literal["knowledge", "memory", "all"] = "all"
    limit: int = 6
    mode: Literal["text", "visual", "hybrid"] = "text"
    file_type: Optional[str] = None
    tags: Optional[list[str]] = None


class AnnotationRequest(BaseModel):
    """标注写入/修改。merge=True(默认,patch 语义)只改给出的字段；False=整条覆盖。
    任一字段为 None 表示「本次不改」（merge 下）/「回默认」（覆盖下由规整处理）。"""
    source_path: str
    tags: Optional[list[str]] = None
    importance: Optional[int] = None
    pinned: Optional[bool] = None
    note: Optional[str] = None
    caption: Optional[str] = None
    group: Optional[str] = None
    merge: bool = True


class GroupRequest(BaseModel):
    name: str
    new_name: Optional[str] = None   # rename 用


class BatchAnnotationRequest(BaseModel):
    source_paths: list[str]
    patch: dict
    tags_mode: str = "replace"
    note_mode: str = "replace"
    dry_run: bool = False


class BatchDocumentRequest(BaseModel):
    source_paths: list[str]


class BatchReindexRequest(BaseModel):
    source_paths: list[str]
    strategy_id: Optional[str] = None
    force: bool = True
    dry_run: bool = False


# ========== 健康检查 ==========

@app.get("/api/health")
def health():
    from config import OCR_ENABLED, TEXT_MODEL_ID
    from vector_store import index_health_state, rebuild_status
    # 注意：不向浏览器返回宿主机物理路径（WATCH_FOLDER 等），仅提供脱敏逻辑字段。
    # 阶段B：公开健康摘要只暴露 healthy/rebuilding/corrupted，不含路径/代际细节。
    return {
        "status": "ok",
        "watch_folder_configured": bool(WATCH_FOLDER),
        "index": {
            "status": index_health_state(),
            "rebuilding": rebuild_status(),
        },
        "capabilities": {
            "text_model": TEXT_MODEL_ID,
            "reranker": reranker_loadable(),
            "visual": CLIP_ENABLED,
            "ocr": OCR_ENABLED,
            "hybrid_bm25": True,
            "video": ffmpeg_available(),                              # 抽帧/probe 能力
            "transcribe": WHISPER_ENABLED and whisper_loadable(),     # 语音转写(ASR)能力
        },
    }


# ========== 阶段B：索引健康闸门的管理接口（本机 require_local） ==========

@app.get("/api/system/storage/status", dependencies=[Depends(require_local)])
def system_storage_status():
    """索引存储状态（本机管理接口）：代际、路由、健康状态与恢复建议。

    不返回内部绝对路径、密钥或 HNSW 异常正文（阶段B §6.2）。
    """
    import index_registry
    from vector_store import index_health_state, rebuild_status, delta_merge_status

    info = index_registry.storage_status()
    state = index_health_state()
    info["index_health"] = state
    info["delta_merge"] = delta_merge_status()
    info["rebuilding"] = rebuild_status()
    info["recovery_suggestion"] = _storage_recovery_suggestion(state)
    return info


@app.post("/api/system/storage/recheck", dependencies=[Depends(require_local)])
def system_storage_recheck():
    """本机管理接口：重新执行 ChromaDB 健康自检并更新健康状态（恢复/重建后复检用）。"""
    from vector_store import index_health_state, verify_chroma_health

    result = verify_chroma_health()
    replayed = 0
    if result.get("ok") and index_health_state() == "healthy":
        from watcher import recover_infrastructure_failures
        replayed = recover_infrastructure_failures(limit=32)
    return {
        "success": result.get("ok", False),
        "index_health": index_health_state(),
        "collections": result.get("collections", 0),
        "checked": result.get("checked", []),
        "issues": result.get("issues", []),
        "replayed_infrastructure_failures": replayed,
    }


@app.get("/api/system/knowledge-card-jobs", dependencies=[Depends(require_local)])
def system_knowledge_card_jobs(limit: int = 100):
    """本机管理视图：只返回卡片派生任务状态，不返回卡片正文。"""
    from mindos.stores import card_ledger_store
    return {"vectorRepairJobs": card_ledger_store.list_vector_jobs(max(1, min(limit, 500))),
            "purgeJobs": card_ledger_store.list_purge_jobs()}


@app.post("/api/system/knowledge-card-repair", dependencies=[Depends(require_local)])
def system_knowledge_card_repair():
    from mindos import knowledge as _knowledge
    return _knowledge.recover_vector_repairs()


@app.post("/api/system/knowledge-card-purge-recover", dependencies=[Depends(require_local)])
def system_knowledge_card_purge_recover():
    from mindos import lifecycle as _lifecycle
    return _lifecycle.recover_pending_purges()


# ========== 阶段 D：材料流水线监控与旧 RAG collection 受控清理 ==========

@app.get("/api/system/mindos-pipeline/status", dependencies=[Depends(require_local)])
def system_mindos_pipeline_status():
    """本机管理监控：队列、Ollama 调度、卡片索引与旧集合清理状态。"""
    from mindos import stage_d_admin
    return stage_d_admin.monitoring_status()


@app.post("/api/system/mindos-legacy-rag-cleanup/plan", dependencies=[Depends(require_local)])
def system_mindos_legacy_rag_cleanup_plan():
    """创建旧材料 RAG collection 清理计划；只预检，不修改任何数据。"""
    from mindos import stage_d_admin
    try:
        return stage_d_admin.create_legacy_cleanup_plan()
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/system/mindos-legacy-rag-cleanup/{cleanup_token}/execute", dependencies=[Depends(require_local)])
def system_mindos_legacy_rag_cleanup_execute(cleanup_token: str):
    """执行已预检的清理：先备份和复核，再删除可验证的旧材料集合。"""
    from mindos import stage_d_admin
    try:
        return stage_d_admin.execute_legacy_cleanup(cleanup_token)
    except KeyError:
        raise HTTPException(404, "cleanup_plan_not_found")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


def _storage_recovery_suggestion(state: str) -> str:
    """恢复建议（脱敏文案，不含路径/文件名）。"""
    if state == "corrupted":
        return (
            "索引已损坏：检索与增量写入被闸门拒绝。请先检查索引目录并停止相关写入，"
            "通过管理端重建索引（POST /api/index/rebuild）或恢复备份后，"
            "再调用 POST /api/system/storage/recheck 复检。"
        )
    if state == "rebuilding":
        return "索引正在重建：写入目标已切到重建集合，完成后自动复检。"
    if state == "healthy":
        return "索引健康。"
    return "索引健康状态未知：等待启动自检或手动复检。"


# ========== MindOS 导入校验（P1）==========

class MindosValidateReq(BaseModel):
    filename: str
    size: int


@app.post("/api/mindos/validate")
def mindos_validate(req: MindosValidateReq):
    """前端与后端使用同一套导入校验规则（格式/大小），不暴露宿主机路径。

    仅做校验，不落盘、不上传；真实上传与处理在 P2 起实现。
    """
    from mindos.validation import validate_import
    return validate_import(req.filename, req.size)


# ========== 文件上传 ==========

@app.post("/api/upload", dependencies=[Depends(require_local)])
async def upload_file(file: UploadFile = File(...)):
    from vector_store import index_health_blocked
    # 阶段B：索引损坏时上传返回稳定 503 index_corrupted（不落盘、不入队）。
    if index_health_blocked():
        raise HTTPException(503, "index_corrupted: 索引已损坏，请先恢复或重建索引后再上传")
    if not file.filename:
        raise HTTPException(400, "文件名为空")

    # 取 basename 防路径注入（客户端可传 "../x.txt"）；扩展名校验
    safe_name = Path(file.filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(400, "非法文件名")
    ext = Path(safe_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {ext}")

    watch_root = Path(WATCH_FOLDER).resolve()
    dest = (watch_root / f"{uuid.uuid4().hex[:8]}_{safe_name}").resolve()
    if not dest.is_relative_to(watch_root):       # 兜底：落盘路径必须在监控目录内
        raise HTTPException(400, "非法文件名")

    # 流式落盘 + 累计字节上限（GB 级视频不一次性吃进内存、不撑爆磁盘）
    written = 0
    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"文件超过上限 {MAX_UPLOAD_BYTES // (1024 ** 3)}GB")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        logger.error(f"上传落盘失败: {e}")
        raise HTTPException(500, f"上传失败: {str(e)}")

    doc_id = str(dest)

    # 音视频走后台异步（转写/抽帧耗时，不能阻塞 HTTP 请求/事件循环）；前端轮询 /api/jobs
    if ext in (SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_AUDIO_EXTENSIONS):
        submit_index(doc_id)
        return {
            "success": True,
            "queued": True,
            "file_name": safe_name,
            "saved_path": doc_id,
            "doc_id": doc_id,
        }

    # 文本/图片同步等结果，但 index_file 放到线程池跑——避免在事件循环线程上
    # 做 parse+嵌入+Chroma 写而冻结其它请求。
    try:
        success = await run_in_threadpool(index_file, doc_id)
    except Exception as e:
        logger.error(f"向量化失败: {e}")
        raise HTTPException(500, f"向量化失败: {str(e)}")

    if not success:
        raise HTTPException(500, "向量化失败：文件内容为空或模型未就绪")

    return {
        "success": True,
        "file_name": safe_name,
        "saved_path": doc_id,
        "doc_id": doc_id,
    }


@app.get("/api/jobs")
def list_jobs(include_done: bool = Query(False)):
    """列出索引任务；默认同时保留失败项，便于文件中心巡检与重试。"""
    jobs = list_index_jobs(include_done=include_done)
    return {"jobs": jobs, "total": len(jobs)}


@app.get("/api/jobs/{doc_id:path}")
def job_status(doc_id: str):
    """后台索引任务状态：queued / processing / done / failed / unknown"""
    from urllib.parse import unquote
    return get_job(unquote(doc_id))


# ========== 语义搜索 ==========

def _is_pinned(item: dict) -> bool:
    ann = item.get("annotation")
    return bool(ann and ann.get("pinned"))


def _cap_keep_pinned(results: list[dict], top_k: int) -> list[dict]:
    """截到 top_k，但保证置顶项不被相关性更高的普通项挤掉（置顶必回）。

    置顶项优先占位（最多占满 top_k），其余名额给普通项按分填充；最后按分排序展示。
    """
    if len(results) <= top_k:
        return results
    pinned = [r for r in results if _is_pinned(r)]
    if not pinned:
        return results[:top_k]
    normal = [r for r in results if not _is_pinned(r)]
    out = pinned[:top_k] + normal[: max(0, top_k - len(pinned))]
    out.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return out[:top_k]


def _text_search(query: str, top_k: int, file_type: Optional[str]) -> tuple[list[dict], bool]:
    """文本检索：BGE 向量过度召回 → 重排精选 → 阈值过滤 → 标注叠加"""
    query_embedding = embed_query(query)
    if not query_embedding:
        raise HTTPException(500, "嵌入模型错误")

    q_strategy = rag_strategy.query_strategy(file_type)
    recall_k = max(int(top_k * float(q_strategy["recall_multiplier"])), int(q_strategy["recall_min"]))
    bm25_extra_count = int(q_strategy["bm25_extra"])
    rerank_max = int(q_strategy["rerank_max"])

    # 混合召回：稠密向量 ∪ BM25 词面（补稠密漏掉的专名/型号/数字等精确匹配）
    dense = search(query_embedding, n_results=recall_k, file_type=file_type)
    dense_ids = {c["id"] for c in dense}
    bm25_hits = lexical.search(query, n_results=bm25_extra_count)
    extra_ids = [cid for cid, _ in bm25_hits if cid not in dense_ids][:bm25_extra_count]
    extra = get_chunks_by_ids(extra_ids) if extra_ids else []
    if file_type:
        extra = [c for c in extra if c["metadata"].get("file_type") == file_type]

    # 标注一次性查全召回集（不只候选池）——否则被 cap 截掉的置顶/重要项拿不到标注
    ann_map = annotations.get_map_for({c["source_path"] for c in dense} | {c["source_path"] for c in extra})

    # 封顶重排候选池：为 BM25 预留名额，其余给稠密（保留向量排序优先级）
    dense_keep = dense[: max(1, rerank_max - len(extra))]
    keep_ids = {c["id"] for c in dense_keep} | {c["id"] for c in extra}
    # 把被 cap 截掉、但「置顶」的 dense 候选强制补回参与重排——否则置顶必回对排名靠后
    # 但确被召回的项失效（重要度仅微调排序，不强补，避免把边缘相关项顶上来）。
    forced = [c for c in dense if c["id"] not in keep_ids and (ann_map.get(c["source_path"]) or {}).get("pinned")]
    candidates = dense_keep + extra + forced

    if not candidates:
        return [], False

    probs = rerank(query, [c["text"] for c in candidates])
    reranked = probs is not None
    if reranked:
        for c, p in zip(candidates, probs):
            c["rerank_score"] = float(p)
            c["score"] = float(p)
    else:
        for c in candidates:
            c["score"] = c["vector_score"]

    # ---- 标注叠加：重要度加权 + 置顶必回（标注存 sidecar，reindex 不丢）----
    kept = []
    for c in candidates:
        ann = ann_map.get(c["source_path"])
        base = c["score"]
        candidate_type = c.get("metadata", {}).get("file_type") or file_type or "text"
        threshold = rag_strategy.threshold_for_file_type(candidate_type, reranked=reranked)
        if ann:
            c["annotation"] = ann
            # 重要度加权：每级 +IMPORTANCE_WEIGHT_STEP（小幅，不颠覆相关性排序；阈值仍用 base）
            c["score"] = base + ann["importance"] * IMPORTANCE_WEIGHT_STEP
        if base >= threshold:
            kept.append(c)
        elif ann and ann.get("pinned") and base >= PIN_MIN_RELEVANCE:
            # 置顶且与本查询「足够相关」（已被召回 + 过最低门槛）→ 绕过主阈值必回。
            c["pinned_bypass"] = True
            kept.append(c)
    kept.sort(key=lambda c: c["score"], reverse=True)
    top = _cap_keep_pinned(kept, top_k)   # 置顶项保证进 top_k，不被截掉
    for c in top:
        c["text"] = c["text"][:800]
        c["match_type"] = "text"
    return top, reranked


def _visual_search(query: str, top_k: int) -> list[dict]:
    """视觉检索：Chinese-CLIP 以文搜图 → 相似度阈值过滤 → 标注叠加"""
    if not (CLIP_ENABLED and clip_available()):
        return []
    qv = embed_query_clip(query)
    if not qv:
        return []
    q_strategy = rag_strategy.query_strategy()
    imgs = search_images(qv, n_results=max(top_k, int(q_strategy["recall_min"])))
    ann_map = annotations.get_map_for({im["source_path"] for im in imgs})
    kept = []
    for im in imgs:
        im["score"] = im["vector_score"]
        im["match_type"] = "visual"
        ann = ann_map.get(im["source_path"])
        base = im["score"]
        file_type = im.get("metadata", {}).get("file_type") or "image"
        threshold = rag_strategy.image_threshold_for_file_type(file_type)
        if ann:
            im["annotation"] = ann
            im["score"] = base + ann["importance"] * IMPORTANCE_WEIGHT_STEP
        if base >= threshold:
            kept.append(im)
        elif ann and ann.get("pinned") and base >= (threshold * 0.6):
            # 视觉空间分布偏低，置顶门槛按 CLIP 阈值的 0.6 折算（而非文本的 PIN_MIN_RELEVANCE）
            im["pinned_bypass"] = True
            kept.append(im)
    kept.sort(key=lambda x: x["score"], reverse=True)
    return _cap_keep_pinned(kept, top_k)


def _filter_by_tags(results: list[dict], tags: Optional[list[str]]) -> list[dict]:
    """标签筛选（AND 语义）：仅保留所属文件带有全部 required tags 的命中。
    某些命中项可能没 annotation 字段（未叠加），按 source_path 兜底查一次。"""
    req_tags = {t.strip() for t in (tags or []) if t and t.strip()}
    if not req_tags:
        return results
    # 命中项已大多带 annotation；缺的按 source_path 批量补
    need = {r["source_path"] for r in results if not r.get("annotation")}
    extra = annotations.get_map_for(need) if need else {}
    out = []
    for r in results:
        ann = r.get("annotation") or extra.get(r["source_path"])
        have = set(ann.get("tags", [])) if ann else set()
        if req_tags.issubset(have):
            out.append(r)
    return out


@app.post("/api/search")
def search_documents(req: SearchRequest):
    if not req.query.strip():
        raise HTTPException(400, "查询内容为空")

    mode = (req.mode or "text").lower()
    top_k = max(1, req.n_results)
    # 标签筛选会过滤掉部分命中——先多取候选，过滤后再截 top_k，避免筛后结果过少
    fetch_k = top_k * 4 if req.tags else top_k

    if mode == "visual":
        results = _cap_keep_pinned(_filter_by_tags(_visual_search(req.query, fetch_k), req.tags), top_k)
        return {"query": req.query, "results": results, "total": len(results), "mode": mode, "reranked": False}

    # text / hybrid 都先做文本检索
    text_results, reranked = _text_search(req.query, fetch_k, req.file_type)

    if mode == "hybrid":
        seen = {r["source_path"] for r in text_results}
        visual = [v for v in _visual_search(req.query, fetch_k) if v["source_path"] not in seen]
        # 文本命中在前（重排分精确），视觉命中补充在后
        results = text_results + visual
    else:
        results = text_results

    # 截 top_k 时保置顶项（hybrid 下视觉置顶项不被前置的文本命中挤掉）
    results = _cap_keep_pinned(_filter_by_tags(results, req.tags), top_k)

    # 融合记忆中心检索结果（文本模式下附加）
    if mode != "visual":
        wiki_results = wiki_store.search_wiki(req.query, n_results=min(top_k, 5))
        seen_wiki = {}
        for wr in wiki_results:
            pp = wr.get("page_path", "")
            if pp and (pp not in seen_wiki or wr.get("score", 0) > seen_wiki[pp].get("score", 0)):
                seen_wiki[pp] = wr
        seen_paths = {r.get("source_path", "") for r in results}
        for wr in seen_wiki.values():
            if wr["page_path"] in seen_paths:
                continue
            wr["metadata"] = {
                "file_name": wr.get("title") or wr.get("page_path", "Wiki"),
                "file_type": "wiki",
                "wiki_type": wr.get("wiki_type", ""),
                "page_path": wr.get("page_path", ""),
            }
            wr["source_path"] = wr.get("page_path", "")
            wr["score"] = wr.get("score", 0) * 0.88
            results.append(wr)

        mem_results = memory_store.search_memory(req.query, n_results=min(top_k, 5))
        # 转换记忆结果为统一格式，去重（同一文件只保留最高分）
        seen_mem = {}
        for mr in mem_results:
            rp = mr.get("rel_path", "")
            if rp not in seen_mem or mr.get("score", 0) > seen_mem[rp].get("score", 0):
                seen_mem[rp] = mr
        for mr in seen_mem.values():
            mr["match_type"] = "memory"
            mr["metadata"] = {
                "file_name": mr.get("rel_path", "").split("/")[-1] or "记忆",
                "file_type": "memory",
                "memory_type": mr.get("memory_type", ""),
            }
            mr["source_path"] = mr.get("rel_path", "")
            # 记忆分数归一化，降低权重不挤占文档
            mr["score"] = mr.get("score", 0) * 0.78
        # 记忆结果排在后面，不挤占文档 top_k
        seen_paths = {r.get("source_path", "") for r in results}
        for mr in seen_mem.values():
            if mr["source_path"] not in seen_paths:
                results.append(mr)
        results.sort(key=lambda r: r.get("score", 0), reverse=True)

    return {"query": req.query, "results": results, "total": len(results), "mode": mode, "reranked": reranked}


def _retrieval_source_type(item: dict) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    file_type = str(metadata.get("file_type") or item.get("file_type") or "").lower()
    match_type = str(item.get("match_type") or "").lower()
    if file_type == "memory" or match_type == "memory" or item.get("memory_type"):
        return "memory"
    if file_type == "wiki" or item.get("wiki_type") or item.get("page_path"):
        return "wiki"
    if match_type == "visual" or file_type in {"png", "jpg", "jpeg", "webp", "gif", "bmp", "svg"}:
        return "image"
    return "document"


def _normalize_retrieval_hit(item: dict, index: int) -> Optional[dict]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    text = str(item.get("text") or item.get("content") or item.get("caption") or "").strip()
    if text in {"", "#", "---"} or len(text) <= 2:
        return None
    if len(text) > 1200:
        text = text[:1197].rstrip() + "..."

    source_type = _retrieval_source_type(item)
    source_path = str(
        item.get("source_path")
        or item.get("rel_path")
        or item.get("page_path")
        or metadata.get("source_path")
        or metadata.get("page_path")
        or ""
    )
    title = str(
        item.get("title")
        or metadata.get("file_name")
        or (source_path.replace("\\", "/").rsplit("/", 1)[-1] if source_path else "")
        or {"memory": "记忆", "wiki": "Wiki", "image": "图片"}.get(source_type, "资料")
    )
    try:
        score = float(item.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0

    return {
        "id": str(item.get("id") or item.get("chunk_id") or f"{source_type}:{source_path}:{index}"),
        "source_type": source_type,
        "title": title,
        "source_path": source_path or None,
        "text": text,
        "score": score,
        "file_type": metadata.get("file_type") or item.get("file_type"),
        "match_type": item.get("match_type"),
    }


@app.post("/api/retrieve")
def retrieve_context(req: RetrievalRequest):
    """供 CentaurAI 应用调用的统一检索入口，返回有界、去重、结构化的命中。"""
    query = req.query.strip()
    if not query:
        raise HTTPException(400, "查询内容为空")

    limit = max(1, min(int(req.limit), 20))
    reranked = False
    effective_mode = "text" if req.scope == "memory" else req.mode

    if req.scope == "memory":
        raw_results = memory_store.search_memory(query, n_results=min(limit * 3, 40))
    else:
        response = search_documents(SearchRequest(
            query=query,
            n_results=limit,
            file_type=req.file_type,
            mode=effective_mode,
            tags=req.tags,
        ))
        raw_results = response.get("results", [])
        reranked = bool(response.get("reranked"))

    hits = []
    seen_text = set()
    for index, item in enumerate(raw_results):
        if not isinstance(item, dict):
            continue
        hit = _normalize_retrieval_hit(item, index)
        if not hit:
            continue
        if req.scope == "knowledge" and hit["source_type"] == "memory":
            continue
        normalized_text = " ".join(hit["text"].lower().split())
        if normalized_text in seen_text:
            continue
        seen_text.add(normalized_text)
        hits.append(hit)
        if len(hits) >= limit:
            break

    return {
        "query": query,
        "scope": req.scope,
        "mode": effective_mode,
        "hits": hits,
        "total": len(hits),
        "reranked": reranked,
    }


@app.post("/api/mobile/search", dependencies=[Depends(require_mobile)])
def mobile_search_documents(req: SearchRequest):
    return search_documents(req)


# ========== Context Pack + A2A 最小端点 ==========

def _extract_a2a_text(message: dict) -> str:
    parts = message.get("parts") if isinstance(message, dict) else []
    texts = []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif isinstance(part.get("textPart"), dict) and isinstance(part["textPart"].get("text"), str):
                texts.append(part["textPart"]["text"])
    return "\n".join(t.strip() for t in texts if t and t.strip())


def _trim_context(text: str, limit_chars: int) -> str:
    if len(text) <= limit_chars:
        return text
    return text[: max(0, limit_chars - 80)].rstrip() + "\n\n[context truncated]"


def _build_context(pack: dict, user_query: str = "") -> dict:
    effective_query = "\n".join(x for x in [pack.get("query", ""), user_query] if x.strip()).strip()
    if not effective_query:
        effective_query = pack.get("name") or "personal context"
    limit_chars = int(pack.get("limit_chars", 6000) or 6000)
    sections = []
    sources = []

    if pack.get("include_memory"):
        mem = memory_store.get_context(agent=f"a2a-{pack['id']}", limit_chars=max(800, limit_chars // 3))
        mem_text = (mem.get("context") or "").strip()
        if mem_text:
            sections.append("## Memory\n\n" + mem_text)
            sources.append({"type": "memory", "items": mem.get("files", [])})

    if pack.get("include_wiki"):
        wiki_hits = wiki_store.search_wiki(effective_query, n_results=6)
        if wiki_hits:
            lines = []
            for hit in wiki_hits:
                lines.append(
                    f"### {hit.get('title') or hit.get('page_path')}\n"
                    f"Path: {hit.get('page_path')}\n"
                    f"{(hit.get('text') or '').strip()[:900]}"
                )
            sections.append("## Wiki\n\n" + "\n\n".join(lines))
            sources.append(
                {
                    "type": "wiki",
                    "items": [
                        {"path": h.get("page_path"), "title": h.get("title"), "score": h.get("score")}
                        for h in wiki_hits
                    ],
                }
            )

    if pack.get("include_documents"):
        try:
            doc_hits, _ = _text_search(effective_query, 6, None)
        except Exception:
            doc_hits = []
        if doc_hits:
            lines = []
            for hit in doc_hits:
                meta = hit.get("metadata") or {}
                lines.append(
                    f"### {meta.get('file_name') or Path(hit.get('source_path', '')).name}\n"
                    f"Source: {hit.get('source_path')}\n"
                    f"{(hit.get('text') or '').strip()[:900]}"
                )
            sections.append("## Documents\n\n" + "\n\n".join(lines))
            sources.append(
                {
                    "type": "documents",
                    "items": [
                        {
                            "source_path": h.get("source_path"),
                            "file_name": (h.get("metadata") or {}).get("file_name"),
                            "score": h.get("score"),
                        }
                        for h in doc_hits
                    ],
                }
            )

    context = _trim_context("\n\n---\n\n".join(sections).strip(), limit_chars)
    return {
        "pack_id": pack["id"],
        "name": pack["name"],
        "query": effective_query,
        "context": context,
        "sources": sources,
        "total_chars": len(context),
    }


def _agent_card_for_pack(pack: dict) -> dict:
    base_path = f"/api/a2a/{pack['id']}"
    return {
        "name": pack.get("name") or "Personal Context Agent",
        "description": pack.get("description") or "Authorized personal AI context endpoint.",
        "url": base_path,
        "version": "0.1.0",
        "protocolVersion": "1.0",
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"},
        },
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": "personal-context",
                "name": "Personal Context Retrieval",
                "description": "Return a scoped context package from the owner's personal AI node.",
                "tags": ["context", "personal-knowledge", "retrieval"],
            }
        ],
    }


def _a2a_invite_for_pack(pack: dict, request: Request) -> dict:
    base_url = str(request.base_url).rstrip("/")
    pack_id = quote(pack["id"], safe="")
    agent_card_url = f"{base_url}/api/a2a/{pack_id}/agent-card.json"
    context_url = f"{base_url}/api/a2a/{pack_id}/context"
    message_url = f"{base_url}/api/a2a/{pack_id}/message:send"
    token = pack.get("token", "")
    invite = {
        "type": "centaur-a2a-context-pack",
        "version": "0.1.0",
        "pack": _pack_public(pack, include_token=True),
        "agent_card_url": agent_card_url,
        "context_url": context_url,
        "message_url": message_url,
        "authorization": {
            "type": "bearer",
            "token": token,
            "header": f"Authorization: Bearer {token}" if token else "",
        },
        "a2a": {
            "protocolVersion": "1.0",
            "message_send": {
                "method": "POST",
                "url": message_url,
                "content_type": "application/a2a+json",
            },
        },
    }
    invite["share_text"] = "\n".join(
        [
            f"A2A Context Pack: {pack.get('name') or pack['id']}",
            f"Agent Card: {agent_card_url}",
            f"Message: {message_url}",
            f"Context: {context_url}",
            f"Bearer Token: {token}" if token else "Bearer Token: <empty>",
        ]
    )
    return invite


@app.get("/api/context/packs", dependencies=[Depends(require_local)])
def list_context_packs():
    return {"packs": [_pack_public(p) for p in _read_context_packs()]}


@app.post("/api/context/packs", dependencies=[Depends(require_local)])
def create_context_pack(req: ContextPackReq):
    packs = _read_context_packs()
    token = (req.token or "").strip()
    generated = False
    if req.generate or not token:
        token = _secrets.token_urlsafe(32)
        generated = True
    now = datetime.now().isoformat()
    pack = _normalize_pack(
        {
            **req.dict(),
            "id": uuid.uuid4().hex[:12],
            "token": token,
            "created_at": now,
            "updated_at": now,
        }
    )
    packs.append(pack)
    _write_context_packs(packs)
    return {"success": True, "pack": _pack_public(pack, include_token=generated)}


@app.put("/api/context/packs/{pack_id}", dependencies=[Depends(require_local)])
def update_context_pack(pack_id: str, req: ContextPackReq):
    packs = _read_context_packs()
    updated = None
    for i, pack in enumerate(packs):
        if pack["id"] != pack_id:
            continue
        token = (req.token or "").strip() or pack.get("token", "")
        generated = False
        if req.generate:
            token = _secrets.token_urlsafe(32)
            generated = True
        new_pack = _normalize_pack(
            {
                **pack,
                **req.dict(),
                "id": pack_id,
                "token": token,
                "created_at": pack.get("created_at"),
                "updated_at": datetime.now().isoformat(),
            }
        )
        packs[i] = new_pack
        updated = _pack_public(new_pack, include_token=generated)
        break
    if not updated:
        raise HTTPException(404, "Context Pack 不存在")
    _write_context_packs(packs)
    return {"success": True, "pack": updated}


@app.delete("/api/context/packs/{pack_id}", dependencies=[Depends(require_local)])
def delete_context_pack(pack_id: str):
    packs = _read_context_packs()
    new_packs = [p for p in packs if p["id"] != pack_id]
    if len(new_packs) == len(packs):
        raise HTTPException(404, "Context Pack 不存在")
    _write_context_packs(new_packs)
    return {"success": True}


@app.get("/api/context/packs/{pack_id}/preview", dependencies=[Depends(require_local)])
def preview_context_pack(pack_id: str, q: str = Query(default="")):
    pack = _get_context_pack(pack_id)
    return _build_context(pack, q)


@app.get("/.well-known/agent-card.json")
def default_agent_card():
    packs = [p for p in _read_context_packs() if p.get("enabled")]
    if not packs:
        return {
            "name": "Centaur Personal AI Node",
            "description": "No enabled Context Pack is configured.",
            "url": "/api/a2a",
            "version": "0.1.0",
            "protocolVersion": "1.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [],
        }
    return _agent_card_for_pack(packs[0])


@app.get("/api/a2a/{pack_id}/agent-card.json")
def a2a_agent_card(pack_id: str):
    return _agent_card_for_pack(_get_context_pack(pack_id))


@app.get("/api/a2a/{pack_id}/context")
def a2a_context(pack: dict = Depends(require_a2a_pack), q: str = Query(default="")):
    return _build_context(pack, q)


@app.post("/api/a2a/{pack_id}/message:send")
def a2a_message_send(req: A2AMessageReq, pack: dict = Depends(require_a2a_pack)):
    user_text = _extract_a2a_text(req.message)
    context_payload = _build_context(pack, user_text)
    task_id = uuid.uuid4().hex
    text = context_payload["context"] or "No matching context is available for this pack."
    return {
        "task": {
            "id": task_id,
            "contextId": pack["id"],
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "message": {
                    "role": "ROLE_AGENT",
                    "parts": [{"text": text}],
                    "messageId": f"msg-{task_id}",
                },
            },
            "artifacts": [
                {
                    "artifactId": "personal-context",
                    "name": "Personal Context",
                    "parts": [{"text": text}],
                    "metadata": {"sources": context_payload["sources"]},
                }
            ],
        }
    }


# ========== 用户标注（标签/重要度/置顶/备注/说明）==========

def _require_in_watch(source_path: str) -> str:
    """标注的 source_path 必须落在监控目录内——把 sidecar 的 key 空间约束在真实语料上，
    防止任意字符串被写成键造成 sidecar 膨胀/孤儿污染（镜像 /api/image|video 的围栏）。"""
    if not source_path:
        raise HTTPException(400, "source_path 为空")
    try:
        target = Path(source_path).resolve()
        if not target.is_relative_to(Path(WATCH_FOLDER).resolve()):
            raise HTTPException(403, "source_path 不在监控目录内")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "非法 source_path")
    return str(target)


@app.get("/api/annotations")
def get_annotations(source_path: Optional[str] = Query(default=None)):
    """全部标注（{source_path: ann}）或单文件标注（?source_path=...）。"""
    if source_path:
        canonical = _require_in_watch(source_path)
        rows = annotations.get_map_for({source_path, canonical})
        return {"source_path": canonical, "annotation":
                rows.get(canonical) or rows.get(source_path) or annotations.get(canonical)}
    return {"annotations": annotations.get_all()}


@app.post("/api/annotations", dependencies=[Depends(require_local)])
def set_annotation(req: AnnotationRequest):
    """写/改某文件标注。caption 变化时对该文件 force 重索引（让说明进/出文本向量空间）。"""
    source_path = _require_in_watch(req.source_path)
    # Older callers could store an OS/symlink alias. Migrate only this validated
    # target; an existing canonical record remains authoritative.
    if source_path != req.source_path:
        annotations.rename(req.source_path, source_path)
    patch = {
        "tags": req.tags,
        "importance": req.importance,
        "pinned": req.pinned,
        "note": req.note,
        "caption": req.caption,
        "group": req.group,
    }
    ann, caption_changed = annotations.set_annotation(source_path, patch, merge=req.merge)

    reindex_queued = False
    if caption_changed:
        # 仅当文件仍存在于磁盘时才重索引（删除后残留标注会在 GET 中可见，但无文件可嵌）
        if Path(source_path).is_file():
            submit_index(source_path, force=True)
            reindex_queued = True

    return {"success": True, "annotation": ann, "caption_changed": caption_changed,
            "reindex_queued": reindex_queued}


@app.post("/api/annotations/batch", dependencies=[Depends(require_local)])
def set_annotations_batch(req: BatchAnnotationRequest):
    """单事务批量标注；返回 audit_id，前端可一键撤销整批修改。"""
    if len(req.source_paths) > 5000:
        raise HTTPException(400, "单次最多处理 5000 个文件")
    paths = [_require_in_watch(path) for path in req.source_paths]
    try:
        result = annotations.batch_set_annotations(
            paths, req.patch, tags_mode=req.tags_mode,
            note_mode=req.note_mode, dry_run=req.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not req.dry_run:
        for source_path in result["caption_changed"]:
            if Path(source_path).is_file():
                submit_index(source_path, force=True)
    return {"success": True, **result}


@app.get("/api/audit")
def get_audit(limit: int = Query(100, ge=1, le=500)):
    items = annotations.list_audit(limit)
    return {"items": items, "total": len(items)}


@app.post("/api/audit/{audit_id}/undo", dependencies=[Depends(require_local)])
def undo_audit(audit_id: int):
    try:
        result = annotations.undo_audit(audit_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    for source_path in result["caption_changed"]:
        if Path(source_path).is_file():
            submit_index(source_path, force=True)
    return {"success": True, **result}


@app.delete("/api/annotations/{source_path:path}", dependencies=[Depends(require_local)])
def delete_annotation(source_path: str):
    from urllib.parse import unquote
    source_path = unquote(source_path)
    had_caption = annotations.delete(source_path)
    reindex_queued = False
    # 之前有 caption → 重索引以清掉文本集合里的 caption 块（文件还在才有意义）
    if had_caption and Path(source_path).is_file():
        submit_index(source_path, force=True)
        reindex_queued = True
    return {"success": True, "reindex_queued": reindex_queued}


# ========== 分组（文件中心用：用户自建命名收纳夹）==========

@app.get("/api/groups")
def get_groups():
    """全部分组 [{name, count}]（含空分组）。"""
    return {"groups": annotations.list_groups()}


@app.post("/api/groups", dependencies=[Depends(require_local)])
def create_group(req: GroupRequest):
    """新建分组，或 new_name 非空时重命名 name→new_name。"""
    if req.new_name:
        ok = annotations.rename_group(req.name, req.new_name)
        return {"success": ok, "groups": annotations.list_groups()}
    if not (req.name or "").strip():
        raise HTTPException(400, "分组名为空")
    annotations.create_group(req.name)
    return {"success": True, "groups": annotations.list_groups()}


@app.delete("/api/groups/{name:path}", dependencies=[Depends(require_local)])
def delete_group(name: str):
    """删除分组（成员文件的 group 清空，文件本身不动）。"""
    from urllib.parse import unquote
    affected = annotations.delete_group(unquote(name))
    return {"success": True, "affected": affected, "groups": annotations.list_groups()}


# ========== 重建索引 ==========

@app.post("/api/reindex", dependencies=[Depends(require_local)])
def reindex():
    from vector_store import index_health_blocked
    # 阶段B：索引损坏时禁止发起重建（重建写目标在故障目录上无法恢复）。
    if index_health_blocked():
        raise HTTPException(503, "index_corrupted: 索引已损坏，禁止重建；请先恢复或重建索引")
    import time as _t
    import rebuild_progress
    from watcher import begin_rebuild_barrier, finish_rebuild_barrier, get_job, scan_existing
    # P1-2：重建写入 __rebuild 集合，旧集合全程在线可检索；校验通过后原子切换，
    # 失败则丢弃 rebuild、保留旧集合——绝不再「先删线上集合再重建」。
    rebuild_session = uuid.uuid4().hex
    barrier = begin_rebuild_barrier(rebuild_session)
    if not barrier.get("ok"):
        return {**get_stats(), "rebuilding": rebuild_status(), "error": barrier.get("error")}
    if barrier.get("active"):
        finish_rebuild_barrier(rebuild_session)
        return {
            **get_stats(), "rebuilding": rebuild_status(),
            "error": f"rebuild_conflict:{len(barrier['active'])}_pending",
            "pending": barrier["active"],
        }
    started = begin_rebuild(rebuild_session)
    if not started["ok"]:
        finish_rebuild_barrier(rebuild_session)
        return {**get_stats(), "rebuilding": rebuild_status(), "error": started.get("error")}
    try:
        # 提交到后台串行池（异步），写入 __rebuild 集合。本轮重建需要等待的任务
        # = 本轮新提交 + 提交时已在队列的（in-flight 任务同样写重建集合）。
        scan_result = scan_existing(force=True, rebuild_session=rebuild_session)
        rebuild_progress.start(
            rebuild_session, "api-reindex", dict(scan_result.get("fingerprints") or {})
        )
        watched = list(scan_result.get("candidates") or [])
        # 兼容旧调用方/测试 mock 未返回 candidates 的情形。
        if "candidates" not in scan_result:
            watched = list(scan_result.get("submitted") or []) + list(
                scan_result.get("already_pending") or []
            )
        already_pending = list(scan_result.get("already_pending") or [])
        if already_pending:
            abort_rebuild()
            logger.error("重建中止：%d 个材料已有进行中的旧任务", len(already_pending))
            return {
                **get_stats(), "rebuilding": rebuild_status(),
                "error": f"rebuild_conflict:{len(already_pending)}_pending", "pending": already_pending,
            }
        # reindex 是显式维护动作，等后台池把这批重建跑完再回报准确统计（可阻塞）。
        # 修复：超时 / 任一任务失败都必须 abort 保留旧索引——此前无论任务是否
        # 跑完都会 commit，半成品集合会顶替旧集合（P0-4 恢复入口约束）。
        deadline = _t.time() + 1800
        while _t.time() < deadline:
            pending = [
                p for p in watched
                if get_job(p).get("state") in ("queued", "processing", "validating")
            ]
            if not pending:
                break
            _t.sleep(0.5)
        else:
            pending = [
                p for p in watched
                if get_job(p).get("state") in ("queued", "processing", "validating")
            ]
        if pending:
            abort_rebuild()
            rebuild_progress.update_states(rebuild_session, {p: get_job(p).get("state", "unknown") for p in watched})
            rebuild_progress.finish(rebuild_session, "aborted")
            logger.error("重建超时（%d 个任务未完成），已中止，旧索引保留", len(pending))
            return {
                **get_stats(), "rebuilding": rebuild_status(),
                "error": f"rebuild_timeout:{len(pending)}_pending",
            }
        failed = [
            {"path": p, "state": get_job(p).get("state"), "error": get_job(p).get("error") or ""}
            for p in watched if get_job(p).get("state") != "done"
        ]
        if failed:
            # 任一材料失败（含新代完整性校验失败在任务层的体现）→ 不切换，
            # 旧索引保留在线；失败清单返回给调用方排查
            abort_rebuild()
            rebuild_progress.update_states(rebuild_session, {p: get_job(p).get("state", "unknown") for p in watched})
            rebuild_progress.finish(rebuild_session, "aborted")
            logger.error("重建中止：%d 个材料索引失败，旧索引保留", len(failed))
            return {
                **get_stats(), "rebuilding": rebuild_status(),
                "error": f"rebuild_failed:{len(failed)}_materials", "failed": failed,
            }
        committed = commit_rebuild()
        if not committed["ok"]:
            abort_rebuild()
            rebuild_progress.finish(rebuild_session, "aborted")
            return {**get_stats(), "rebuilding": rebuild_status(), "error": committed.get("error")}
        rebuild_progress.update_states(rebuild_session, {p: "done" for p in watched})
        rebuild_progress.finish(rebuild_session, "completed")
        return {**get_stats(), "rebuilding": rebuild_status()}
    except Exception as e:
        abort_rebuild()
        rebuild_progress.finish(rebuild_session, "aborted")
        return {**get_stats(), "rebuilding": rebuild_status(), "error": str(e)}
    finally:
        # commit/abort 后才解除栅栏，保证延后事件只写入稳定的正式集合。
        finish_rebuild_barrier(rebuild_session)


@app.post("/api/reindex/incomplete", dependencies=[Depends(require_local)])
def reindex_incomplete():
    """P0-4 恢复入口（修复6）：主动枚举全部已索引源做完整性校验并重建损坏项。

    与被动路径（watcher 扫到同一路径才触发）互补：检测到 integrity_failed /
    read_error 的源（缺块、维度不一致、代数不连续等）立即重新入队。返回
    统计与损坏明细供运维排查。
    """
    result = _verify_and_requeue_incomplete()
    return {"success": True, **result}


# ========== 文档管理 ==========

_TRASH_DIR = TRASH_DIR


def _file_type_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in SUPPORTED_IMAGE_EXTENSIONS:
        return "image"
    if ext in SUPPORTED_VIDEO_EXTENSIONS:
        return "video"
    if ext in SUPPORTED_AUDIO_EXTENSIONS:
        return "audio"
    return "text"


@functools.lru_cache(maxsize=2048)
def _computed_file_hash(path_key: str, mtime_ns: int, size: int) -> str:
    """带上限的文件哈希缓存（原 _INVENTORY_HASH_CACHE 无界增长；lru_cache 自动淘汰）。"""
    return file_hash(path_key) or ""


def _inventory_hash(path: Path, metadata: dict) -> str:
    indexed_hash = str(metadata.get("content_hash") or "").split(":", 1)[0]
    if indexed_hash:
        return indexed_hash
    try:
        stat = path.stat()
        return _computed_file_hash(str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return ""


def _all_document_inventory() -> list[dict]:
    """合并 Chroma 与监控目录，显示未索引、失败和磁盘已丢失的真实文件状态。"""
    indexed = {str(Path(item["id"]).absolute()): item for item in list_all_documents()}
    disk_paths: set[str] = set()
    root = Path(WATCH_FOLDER)
    if root.exists():
        disk_paths = {
            str(path.absolute()) for path in root.rglob("*")
            if path.is_file() and is_supported(str(path))
        }
    all_paths = set(indexed) | disk_paths
    annotation_map = annotations.get_map_for(all_paths)
    items: list[dict] = []

    for source_path in all_paths:
        path = Path(source_path)
        indexed_item = indexed.get(source_path)
        meta = dict((indexed_item or {}).get("metadata") or {})
        on_disk = source_path in disk_paths
        if on_disk:
            try:
                stat = path.stat()
                meta.update(
                    {
                        "file_name": path.name,
                        "file_path": source_path,
                        "file_size": stat.st_size,
                        "modified_time": stat.st_mtime,
                    }
                )
            except OSError:
                on_disk = False
        meta.setdefault("file_name", path.name)
        meta.setdefault("file_path", source_path)
        meta.setdefault("file_type", _file_type_for(path))
        job = get_job(source_path)
        job_state = job.get("state", "unknown")
        if not on_disk:
            status = "missing"
        elif job_state in {"queued", "processing", "failed"}:
            status = job_state
        elif indexed_item:
            status = "indexed"
        else:
            status = "unindexed"
        ann = annotation_map.get(source_path) or {
            "tags": [], "importance": 0, "pinned": False,
            "note": "", "caption": "", "group": "",
        }
        items.append(
            {
                "id": source_path,
                "metadata": meta,
                "chunk_count": int((indexed_item or {}).get("chunk_count") or 0),
                "poster": (indexed_item or {}).get("poster", ""),
                "annotation": ann,
                "status": status,
                "job": job if job_state != "unknown" else None,
                "on_disk": on_disk,
                "indexed": bool(indexed_item),
                "content_hash": _inventory_hash(path, meta) if on_disk else str(meta.get("content_hash") or "").split(":", 1)[0],
                "rag_strategy": annotations.get_rag_override(source_path) or meta.get("rag_strategy") or "",
            }
        )

    hash_counts: dict[str, int] = {}
    for item in items:
        digest = item["content_hash"]
        if digest:
            hash_counts[digest] = hash_counts.get(digest, 0) + 1
    for item in items:
        item["duplicate_count"] = hash_counts.get(item["content_hash"], 0) if item["content_hash"] else 0
    return items


def _filter_documents(
    items: list[dict], query: str = "", file_type: str = "", status: str = "",
    group: str = "", tag: str = "", duplicates: bool = False, special: str = "",
    sort_by: str = "modified", sort_dir: str = "desc",
) -> list[dict]:
    needle = (query or "").strip().casefold()
    out = []
    for item in items:
        meta = item["metadata"]
        ann = item["annotation"]
        if file_type and meta.get("file_type") != file_type:
            continue
        if status and item["status"] != status:
            continue
        if group and ann.get("group") != group:
            continue
        if tag and tag not in ann.get("tags", []):
            continue
        if duplicates and item.get("duplicate_count", 0) < 2:
            continue
        if special == "important" and not ann.get("importance"):
            continue
        if special == "pinned" and not ann.get("pinned"):
            continue
        if special == "ungrouped" and ann.get("group"):
            continue
        if needle:
            haystack = " ".join(
                [
                    str(meta.get("file_name") or ""), item["id"], str(ann.get("group") or ""),
                    str(ann.get("note") or ""), str(ann.get("caption") or ""),
                    " ".join(ann.get("tags") or []),
                ]
            ).casefold()
            if needle not in haystack:
                continue
        out.append(item)

    def key(item: dict):
        meta, ann = item["metadata"], item["annotation"]
        if sort_by == "name":
            return str(meta.get("file_name") or "").casefold()
        if sort_by == "size":
            return int(meta.get("file_size") or 0)
        if sort_by == "importance":
            return int(ann.get("importance") or 0)
        if sort_by == "type":
            return (str(meta.get("file_type") or ""), str(meta.get("file_name") or "").casefold())
        if sort_by == "status":
            return (item["status"], str(meta.get("file_name") or "").casefold())
        return float(meta.get("modified_time") or 0)

    out.sort(key=key, reverse=sort_dir != "asc")
    out.sort(key=lambda item: not bool(item["annotation"].get("pinned")))
    return out


def _document_facets(items: list[dict]) -> dict:
    def counts(values):
        out: dict[str, int] = {}
        for value in values:
            if value:
                out[str(value)] = out.get(str(value), 0) + 1
        return out

    return {
        "types": counts(item["metadata"].get("file_type") for item in items),
        "statuses": counts(item["status"] for item in items),
        "groups": counts(item["annotation"].get("group") for item in items),
        "tags": counts(tag for item in items for tag in item["annotation"].get("tags", [])),
        "duplicates": sum(1 for item in items if item.get("duplicate_count", 0) > 1),
        "important": sum(1 for item in items if item["annotation"].get("importance")),
        "pinned": sum(1 for item in items if item["annotation"].get("pinned")),
        "ungrouped": sum(1 for item in items if not item["annotation"].get("group")),
        "total": len(items),
    }


def _query_documents(
    query: str = "", file_type: str = "", status: str = "", group: str = "", tag: str = "",
    duplicates: bool = False, special: str = "", sort_by: str = "modified", sort_dir: str = "desc",
) -> tuple[list[dict], dict]:
    all_items = _all_document_inventory()
    return _filter_documents(
        all_items, query, file_type, status, group, tag, duplicates, special, sort_by, sort_dir
    ), _document_facets(all_items)


@app.get("/api/documents")
def list_docs(
    limit: int = Query(60, ge=1, le=500), offset: int = Query(0, ge=0),
    q: str = Query(""), file_type: str = Query(""), status: str = Query(""),
    group: str = Query(""), tag: str = Query(""), duplicates: bool = Query(False),
    special: str = Query(""),
    sort_by: str = Query("modified"), sort_dir: str = Query("desc"),
):
    items, facets = _query_documents(q, file_type, status, group, tag, duplicates, special, sort_by, sort_dir)
    return {
        "total": len(items), "items": items[offset:offset + limit], "offset": offset,
        "limit": limit, "has_more": offset + limit < len(items), "facets": facets,
    }


@app.get("/api/documents/ids")
def list_doc_ids(
    q: str = Query(""), file_type: str = Query(""), status: str = Query(""),
    group: str = Query(""), tag: str = Query(""), duplicates: bool = Query(False),
    special: str = Query(""),
    sort_by: str = Query("modified"), sort_dir: str = Query("desc"),
):
    items, _ = _query_documents(q, file_type, status, group, tag, duplicates, special, sort_by, sort_dir)
    return {"ids": [item["id"] for item in items], "total": len(items)}


@app.get("/api/documents/reconcile")
def reconcile_documents():
    items = _all_document_inventory()
    issues = [item for item in items if item["status"] != "indexed"]
    return {"items": issues, "total": len(issues), "facets": _document_facets(items)}


@app.post("/api/documents/reindex", dependencies=[Depends(require_local)])
def batch_reindex_documents(req: BatchReindexRequest):
    strategy_ids = {item["id"] for item in rag_strategy.list_strategies()}
    if req.strategy_id and req.strategy_id not in strategy_ids:
        raise HTTPException(400, "未知 RAG 策略")
    paths = list(dict.fromkeys(_require_in_watch(path) for path in req.source_paths))
    missing = [path for path in paths if not Path(path).is_file()]
    if missing:
        raise HTTPException(400, f"{len(missing)} 个文件已不在磁盘，无法重建")
    if req.dry_run:
        return {"success": True, "queued": 0, "eligible": len(paths), "dry_run": True}
    queued = 0
    for path in paths:
        if submit_index(path, force=req.force, strategy_id=req.strategy_id):
            queued += 1
    audit_id = annotations.add_audit(
        "batch_reindex", paths,
        {"strategy_id": req.strategy_id, "force": req.force, "queued": queued},
    )
    return {"success": True, "queued": queued, "total": len(paths), "audit_id": audit_id}


def _recycle_document(source_path: str) -> dict:
    supplied_path = source_path
    source_path = _require_in_watch(source_path)
    if supplied_path != source_path:
        annotations.rename(supplied_path, source_path)
    source = Path(source_path)
    if not source.is_file():
        # 磁盘已丢失时只清理孤立索引，审计仍保留。
        if delete_document(source_path):
            annotations.delete(source_path)
            audit_id = annotations.add_audit("purge_missing", [source_path])
            return {"source_path": source_path, "purged_missing": True, "audit_id": audit_id}
        raise HTTPException(404, "文件不存在")
    _TRASH_DIR.mkdir(parents=True, exist_ok=True)
    trash_id = uuid.uuid4().hex
    trash_path = _TRASH_DIR / f"{trash_id}_{source.name}"
    annotation = annotations.get(source_path)
    stat = source.stat()
    metadata = {
        "rag_strategy": (annotations.get_rag_override(source_path)
                         or annotations.get_rag_override(supplied_path)),
        "modified_time": stat.st_mtime,
    }
    try:
        shutil.move(str(source), str(trash_path))
        audit_id = annotations.record_trash(
            {
                "id": trash_id, "original_path": source_path, "trash_path": str(trash_path),
                "file_name": source.name, "size": stat.st_size, "annotation": annotation,
                "metadata": metadata,
            }
        )
    except Exception:
        if trash_path.exists() and not source.exists():
            shutil.move(str(trash_path), str(source))
        raise
    delete_document(source_path)
    annotations.delete(source_path)
    return {"source_path": source_path, "trash_id": trash_id, "audit_id": audit_id}


@app.get("/api/trash")
def get_trash(limit: int = Query(500, ge=1, le=2000)):
    items = annotations.list_trash(limit=limit)
    return {"items": items, "total": len(items)}


@app.post("/api/trash/{trash_id}/restore", dependencies=[Depends(require_local)])
def restore_trash(trash_id: str):
    record = annotations.get_trash(trash_id)
    if not record or record["status"] != "active":
        raise HTTPException(404, "回收站记录不存在")
    source = Path(record["trash_path"])
    if not source.is_file():
        raise HTTPException(404, "回收站文件已丢失")
    root = Path(WATCH_FOLDER).resolve()
    target = Path(record["original_path"]).resolve()
    if not target.is_relative_to(root):
        target = root / record["file_name"]
    if target.exists():
        target = target.with_name(f"{target.stem}_恢复_{trash_id[:6]}{target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    annotations.set_annotation(str(target), record["annotation"], merge=False)
    strategy_id = (record.get("metadata") or {}).get("rag_strategy")
    if strategy_id:
        annotations.set_rag_override(str(target), strategy_id)
    try:
        # 先恢复元数据再移动文件，避免 watcher 抢先索引到一个还没有 caption 的版本。
        shutil.move(str(source), str(target))
    except Exception:
        annotations.delete(str(target))
        annotations.set_rag_override(str(target), None)
        raise
    annotations.mark_trash_restored(trash_id, str(target))
    submit_index(str(target), force=True, strategy_id=strategy_id)
    return {"success": True, "source_path": str(target), "trash_id": trash_id}


@app.delete("/api/trash/{trash_id}", dependencies=[Depends(require_local)])
def purge_trash(trash_id: str):
    record = annotations.get_trash(trash_id)
    if not record or record["status"] != "active":
        raise HTTPException(404, "回收站记录不存在")
    path = Path(record["trash_path"])
    if path.is_file():
        path.unlink()
    annotations.delete_trash_record(trash_id)
    return {"success": True}


@app.post("/api/documents/batch-delete", dependencies=[Depends(require_local)])
def batch_delete_docs(req: BatchDocumentRequest):
    results = []
    failures = []
    for source_path in dict.fromkeys(req.source_paths):
        try:
            results.append(_recycle_document(source_path))
        except Exception as exc:
            failures.append({"source_path": source_path, "error": str(exc)})
    return {"success": not failures, "items": results, "failures": failures, "trashed": len(results)}


@app.delete("/api/documents/{doc_id:path}", dependencies=[Depends(require_local)])
def delete_doc(doc_id: str):
    from urllib.parse import unquote
    result = _recycle_document(unquote(doc_id))
    return {"success": True, "trashed": bool(result.get("trash_id")), **result}


# ========== Wiki 知识层 ==========

@app.get("/api/wiki/stats")
def wiki_stats():
    return wiki_store.stats()


@app.get("/api/wiki/pages")
def wiki_pages(
    folder: Optional[str] = Query(default=None),
    q: str = Query(default=""),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return wiki_store.list_pages(folder=folder, query=q, limit=limit, offset=offset)


@app.post("/api/wiki/pages", dependencies=[Depends(require_local)])
def wiki_create_page(req: WikiCreatePageReq):
    try:
        return {
            "success": True,
            "page": wiki_store.create_page(
                req.title,
                req.folder,
                req.content,
                req.tags,
                page_type=req.page_type or None,
            ),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/wiki/pages/{path:path}")
def wiki_read_page(path: str):
    from urllib.parse import unquote
    page = wiki_store.read_page(unquote(path))
    if not page:
        raise HTTPException(404, "Wiki 页面不存在")
    return page


@app.put("/api/wiki/pages/{path:path}", dependencies=[Depends(require_local)])
def wiki_write_page(path: str, req: WikiWritePageReq):
    from urllib.parse import unquote
    try:
        return {"success": True, "page": wiki_store.write_page(unquote(path), req.content, req.source_agent)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/wiki/search")
def wiki_search(req: WikiSearchReq):
    if not req.query.strip():
        raise HTTPException(400, "查询内容为空")
    results = wiki_store.search_wiki(req.query, n_results=req.n_results)
    return {"query": req.query, "results": results, "total": len(results)}


@app.get("/api/wiki/graph")
def wiki_graph(path: Optional[str] = Query(default=None)):
    return wiki_store.graph(path)


@app.get("/api/wiki/jobs")
def wiki_jobs(include_done: bool = Query(default=False)):
    jobs = wiki_store.list_jobs(include_done=include_done)
    return {"jobs": jobs, "total": len(jobs)}


@app.get("/api/wiki/organizer/status")
def wiki_organizer_status():
    return wiki_store.local_organizer_status()


@app.post("/api/wiki/organize", dependencies=[Depends(require_local)])
def wiki_organize(req: WikiOrganizeReq):
    try:
        job_id = wiki_store.submit_source(req.source_path, force=req.force)
        return {"success": True, "job_id": job_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/wiki/reindex", dependencies=[Depends(require_local)])
def wiki_reindex():
    return wiki_store.reindex_all_wiki()


@app.post("/api/wiki/maintenance", dependencies=[Depends(require_local)])
def wiki_maintenance():
    return wiki_store.run_maintenance()


# ========== GBrain（Wiki 的本地派生检索与关系索引）==========

def _gbrain_http_error(exc: Exception):
    message = str(exc)
    status_code = 404 if "not found" in message.lower() or "不存在" in message else 503
    raise HTTPException(status_code, message)


@app.get("/api/gbrain/status")
def gbrain_status():
    return gbrain_store.status()


@app.get("/api/gbrain/pages")
def gbrain_pages(
    page_type: str = Query(default=""),
    tag: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=100),
):
    try:
        return gbrain_store.list_pages(page_type=page_type, tag=tag, limit=limit)
    except gbrain_store.GBrainError as exc:
        _gbrain_http_error(exc)


@app.post("/api/gbrain/search")
def gbrain_search(req: GBrainSearchReq):
    try:
        return gbrain_store.search_pages(req.query, mode=req.mode, limit=req.limit)
    except gbrain_store.GBrainError as exc:
        _gbrain_http_error(exc)


@app.post("/api/gbrain/pages", dependencies=[Depends(require_local)])
def gbrain_capture(req: GBrainCaptureReq):
    try:
        folder_by_type = {
            "concept": "Concepts",
            "project": "Projects",
            "decision": "Areas",
        }
        page = wiki_store.create_page(
            title=req.title,
            folder=folder_by_type.get(req.page_type, "Resources"),
            content=req.content,
            tags=req.tags,
            page_type=req.page_type or "note",
        )
        sync = page.get("gbrain_sync") or {}
        return {
            "success": True,
            "source_of_truth": "wiki",
            "wiki_path": page.get("path"),
            "slug": sync.get("slug") or wiki_store.gbrain_slug_for_path(page["path"]),
            "page": page,
        }
    except (gbrain_store.GBrainError, ValueError) as exc:
        _gbrain_http_error(exc)


@app.post("/api/gbrain/sync-wiki", dependencies=[Depends(require_local)])
def gbrain_sync_wiki():
    try:
        return wiki_store.reindex_all_wiki()
    except (gbrain_store.GBrainError, ValueError) as exc:
        _gbrain_http_error(exc)


@app.get("/api/gbrain/graph")
def gbrain_graph(slug: str = Query(...), depth: int = Query(default=2, ge=1, le=4)):
    try:
        return gbrain_store.graph(slug, depth=depth)
    except gbrain_store.GBrainError as exc:
        _gbrain_http_error(exc)


@app.get("/api/gbrain/pages/{slug:path}")
def gbrain_read_page(slug: str):
    from urllib.parse import unquote
    try:
        return gbrain_store.get_page(unquote(slug))
    except gbrain_store.GBrainError as exc:
        _gbrain_http_error(exc)


# ========== 图片缩略图（限 watch_folder 内，防越权读文件）==========

@app.get("/api/file")
def get_file(path: str = Query(...), disposition: str = Query("inline")):
    """Serve an imported source file without allowing reads outside watch_folder."""
    target = Path(path).resolve()
    watch_root = Path(WATCH_FOLDER).resolve()
    if not target.is_relative_to(watch_root):
        raise HTTPException(403, "禁止访问监控目录外的文件")
    if not target.is_file() or not is_supported(str(target)):
        raise HTTPException(404, "文件不存在或格式不受支持")
    if disposition not in {"inline", "attachment"}:
        raise HTTPException(400, "disposition 必须是 inline 或 attachment")
    return FileResponse(
        str(target),
        filename=target.name,
        content_disposition_type=disposition,
    )

@app.get("/api/image")
def get_image(path: str = Query(...)):
    target = Path(path).resolve()
    watch_root = Path(WATCH_FOLDER).resolve()
    if not target.is_relative_to(watch_root):       # 规范化后防路径穿越（含软链）
        raise HTTPException(403, "禁止访问监控目录外的文件")
    if not target.is_file() or target.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise HTTPException(404, "图片不存在")
    return FileResponse(str(target))


# ========== 视频帧缩略图（限 video_frames 目录内，防越权读文件）==========

_FRAME_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@app.get("/api/frame")
def get_frame(path: str = Query(...)):
    root = Path(VIDEO_FRAMES_DIR).resolve()
    target = Path(path).resolve()
    if not target.is_relative_to(root):             # 防穿越（含软链）
        raise HTTPException(403, "禁止访问帧目录外的文件")
    if not target.is_file() or target.suffix.lower() not in _FRAME_EXTS:
        raise HTTPException(404, "帧不存在")
    return FileResponse(str(target))


# ========== 视频流（限 watch_folder 内；供前端命中时跳到时刻播放）==========

@app.get("/api/video")
def get_video(path: str = Query(...)):
    target = Path(path).resolve()
    watch_root = Path(WATCH_FOLDER).resolve()
    if not target.is_relative_to(watch_root):       # 防穿越
        raise HTTPException(403, "禁止访问监控目录外的文件")
    if not target.is_file() or target.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        raise HTTPException(404, "视频不存在")
    # FileResponse 自带 Range 支持（Accept-Ranges/206），<video> 才能 seek 到指定时刻
    return FileResponse(str(target))


# ========== 远程 MCP 管理（仅本机） ==========


def _tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _mcp_remote_payload() -> dict:
    cfg = mcp_access.get_runtime_config()
    urls = mcp_access.public_urls()
    store = mcp_access.get_store()
    mcp_port = int(cfg.get("mcp_port", 8620))
    https_port = int(cfg.get("https_port", 8443))
    lan_http_port = int(cfg.get("lan_http_port", 8080))
    lan_ip = str(cfg.get("lan_ip") or "127.0.0.1")
    mode = str(cfg.get("mode") or "basic")
    basic_client = store.get_basic_client()
    return {
        "enabled": bool(cfg.get("enabled")),
        "mode": mode,
        "admin_password_set": mcp_access.admin_password_is_set(),
        "transport": "streamable-http",
        "protocol": "2025-11-25",
        "oauth": mode == "advanced",
        "oauth_available_in_advanced": True,
        "oauth_discovery": f"{urls['issuer']}/.well-known/oauth-authorization-server",
        "urls": urls,
        "ca_fingerprint": mcp_access.ca_fingerprint(),
        "ca_installed": mcp_access.ca_certificate_path().is_file(),
        "mcp_service_reachable": _tcp_reachable("127.0.0.1", mcp_port),
        "https_service_reachable": _tcp_reachable(lan_ip, https_port),
        "https_port": https_port,
        "lan_http_port": lan_http_port,
        "lan_ip": lan_ip,
        "clients": store.list_clients(),
        "basic_key": {
            "exists": bool(basic_client and basic_client.get("token_suffix")),
            "token_suffix": basic_client.get("token_suffix", "") if basic_client else "",
            "created_at": basic_client.get("created_at") if basic_client else None,
            "last_used_at": basic_client.get("last_used_at") if basic_client else None,
        },
        "tools": {
            "basic": [
                "memory_get_context", "memory_search", "kb_search", "kb_get_stats",
                "kb_list_documents", "kb_health",
            ],
            "kb": ["kb_search", "kb_get_stats", "kb_list_documents", "kb_health"],
            "full": [
                "memory_get_user_profile", "memory_get_context", "memory_search",
                "memory_list_files", "memory_read_file", "kb_search", "kb_get_stats",
                "kb_list_documents", "kb_health",
            ],
        },
    }


@app.get("/api/mcp/remote", dependencies=[Depends(require_loopback)])
def get_mcp_remote_config():
    return _mcp_remote_payload()


@app.post("/api/mcp/remote/config", dependencies=[Depends(require_local)])
def set_mcp_remote_config(req: McpRemoteConfigReq):
    if req.admin_password:
        try:
            mcp_access.set_admin_password(req.admin_password)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    previous = mcp_access.get_runtime_config()
    mcp_access.save_runtime_config({"enabled": req.enabled, "mode": req.mode})
    if req.mode != "advanced" and previous.get("mode") != req.mode:
        mcp_access.get_store().clear_pending_oauth()
    return {"success": True, **_mcp_remote_payload()}


def _require_mcp_mode(mode: str) -> None:
    if mcp_access.get_runtime_config().get("mode") != mode:
        label = "普通" if mode == "basic" else "高级"
        raise HTTPException(409, f"请先切换到{label}模式")


def _basic_token_response(client: dict, token: str) -> dict:
    return {
        "success": True,
        "client": client,
        "token": token,
        "token_display_once": True,
        "endpoint": mcp_access.public_urls()["basic"],
    }


@app.post("/api/mcp/basic-token", dependencies=[Depends(require_local)])
def create_mcp_basic_token():
    _require_mcp_mode("basic")
    try:
        client, token = mcp_access.get_store().create_basic_token()
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return _basic_token_response(client, token)


@app.post("/api/mcp/basic-token/rotate", dependencies=[Depends(require_local)])
def rotate_mcp_basic_token():
    _require_mcp_mode("basic")
    try:
        client, token = mcp_access.get_store().rotate_basic_token()
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return _basic_token_response(client, token)


@app.post("/api/mcp/clients", dependencies=[Depends(require_local)])
def create_mcp_client(req: McpClientReq):
    _require_mcp_mode("advanced")
    try:
        client, token = mcp_access.get_store().create_compat_client(req.label, req.tier)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "success": True,
        "client": client,
        "token": token,
        "token_display_once": True,
        "endpoint": mcp_access.public_urls()["full" if req.tier == "full" else "kb"],
    }


@app.post("/api/mcp/clients/{client_id}/rotate", dependencies=[Depends(require_local)])
def rotate_mcp_client(client_id: str):
    _require_mcp_mode("advanced")
    try:
        token = mcp_access.get_store().rotate_compat_client(client_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    client = mcp_access.get_store().get_client_public(client_id) or {}
    tier = client.get("tier", "kb")
    return {
        "success": True,
        "client": client,
        "token": token,
        "token_display_once": True,
        "endpoint": mcp_access.public_urls()["full" if tier == "full" else "kb"],
    }


@app.delete("/api/mcp/clients/{client_id}", dependencies=[Depends(require_local)])
def revoke_mcp_client(client_id: str):
    _require_mcp_mode("advanced")
    if not mcp_access.get_store().revoke_client(client_id):
        raise HTTPException(404, "MCP 客户端不存在")
    return {"success": True, "revoked": True}


@app.get("/api/mcp/audit", dependencies=[Depends(require_loopback)])
def get_mcp_audit(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": mcp_access.get_store().list_audit(limit)}


@app.get("/api/mcp/ca.crt", dependencies=[Depends(require_loopback)])
def get_mcp_ca_certificate():
    path = mcp_access.ca_certificate_path()
    if not path.is_file():
        raise HTTPException(404, "MCP CA 证书尚未生成")
    return FileResponse(str(path), media_type="application/x-x509-ca-cert", filename="centaurai-memory-ca.crt")


# ========== 统计 ==========

@app.get("/api/stats")
def stats():
    return get_stats()


# ========== 配置 ==========

@app.get("/api/config")
def get_config():
    mcp_command = str(Path(PROJECT_ROOT) / "start-mcp.sh")
    mcp_tools = [
        {
            "name": "memory_get_user_profile",
            "description": "读取 memory/USER.md，用于用户身份、称呼、偏好。",
        },
        {
            "name": "memory_get_context",
            "description": "获取共享记忆、Agent 专属导入记忆和最近日记摘要。",
        },
        {
            "name": "memory_search",
            "description": "语义搜索记忆文件和日记。",
        },
        {
            "name": "memory_list_files",
            "description": "列出记忆文件。",
        },
        {
            "name": "memory_read_file",
            "description": "读取指定记忆文件。",
        },
        {
            "name": "kb_search",
            "description": "搜索本地向量知识库，覆盖文本、OCR、图片、视频等索引内容。",
        },
        {
            "name": "kb_get_stats",
            "description": "获取知识库索引统计。",
        },
        {
            "name": "kb_list_documents",
            "description": "列出已索引文档。",
        },
        {
            "name": "kb_health",
            "description": "检查后端状态和能力位。",
        },
    ]
    return {
        "watch_folder": WATCH_FOLDER,
        "api": {
            "base_url": f"http://{HOST}:{PORT}",
            "host": HOST,
            "port": PORT,
        },
        "mcp": {
            "name": "local-vector-db",
            "transport": "stdio + streamable-http",
            "command": mcp_command,
            "working_directory": str(PROJECT_ROOT),
            "backend_required": True,
            "backend_url": f"http://{HOST}:{PORT}",
            "server_script": str(Path(PROJECT_ROOT) / "backend" / "mcp_server.py"),
            "config_json": {
                "mcpServers": {
                    "local-vector-db": {
                        "command": mcp_command,
                    }
                }
            },
            "tools": mcp_tools,
            "remote": {
                "protocol": "2025-11-25",
                "transport": "streamable-http",
                "urls": mcp_access.public_urls(),
            },
        },
    }


# ========== 启动 ==========

def main():
    # P0-1 单实例锁：数据根目录 OS 级独占锁，第二个实例直接拒绝启动。
    # 此处提前拿锁是为了给 CLI 一个友好的退出提示；uvicorn.run 触发的 lifespan
    # 会幂等复用这把锁。锁先于任何 ChromaDB 连接（schema 迁移/recreate/scan
    # 都发生在锁之后）；后端进程内的 /api/reindex 天然持有同一把锁。
    _ok, _holder_hint = _acquire_instance_lock_once()
    if not _ok:
        logger.error(
            "数据目录已被其他进程占用（%s），拒绝启动。"
            "请先停止该进程；或为不同实例设置独立的 CENTAURAI_DATABASE_DATA_ROOT。",
            _holder_hint,
        )
        print(
            f"启动失败：数据目录已被其他进程占用（{_holder_hint}）。请先停止该进程，"
            "或为不同实例设置独立的 CENTAURAI_DATABASE_DATA_ROOT。",
            file=sys.stderr,
        )
        sys.exit(1)

    # LAN 配置在 lifespan 内统一加载（_start_background_services→_load_lan_config），
    # 与 uvicorn server:app 启动方式保持一致；schema 迁移、启动自检、watcher、
    # 预热与各守护线程同样由 lifespan 统一启动（修复5），main() 不再重复。

    import uvicorn
    # 存储后端始终只监听 loopback。LAN 导入使用独立 HTTP 白名单，
    # 手机/MCP 使用 HTTPS 白名单，避免普通 /api/* 对外暴露。
    bind_host = HOST
    global _CURRENT_BIND_HOST
    _CURRENT_BIND_HOST = bind_host
    logger.info(f"服务器启动: http://{bind_host}:{PORT}")
    logger.info(f"监控文件夹: {WATCH_FOLDER}")

    # 桌面端 P2P 通道：Electron 主进程以子进程方式拉起本后端并走 stdin/stdout 点对点，
    # 渲染进程不再直接依赖固定 8618 端口。仅当显式传 --stdio-rpc 才启用，否则行为不变。
    if "--stdio-rpc" in sys.argv[1:]:
        import rpc_stdio
        rpc_stdio.start_stdio_rpc(app)

    try:
        uvicorn.run(app, host=bind_host, port=PORT, log_level="info")
    finally:
        # lifespan shutdown 已做常规清理；此处兜底（异常退出路径），_stop 幂等
        _stop_background_services()


if __name__ == "__main__":
    main()
