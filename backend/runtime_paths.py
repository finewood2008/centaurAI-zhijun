"""Filesystem contract for immutable code and mutable Database state."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


# pytest 的根 conftest 会在导入本模块前设置独立数据根；而裸 unittest discover
# 没有该钩子。后者若继续使用默认 data/ 会让测试读写手工/生产索引，因此在路径
# 解析前明确拒绝，强制改用 scripts/run_tests.py 或 python -m pytest。
if "unittest" in sys.modules and not os.environ.get("CENTAURAI_DATABASE_DATA_ROOT"):
    raise RuntimeError(
        "检测到未隔离的 unittest 运行。请使用 scripts/run_tests.py 或 python -m pytest；"
        "禁止 unittest discover 访问默认生产数据根。"
    )


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return (Path(value).expanduser() if value else default).resolve()


# 默认把所有可变数据与源码隔离到项目内的 data/；部署环境仍可通过环境变量
# 将整个数据根迁到代码目录之外。
DATA_ROOT = _env_path("CENTAURAI_DATABASE_DATA_ROOT", PROJECT_ROOT / "data")
CONFIG_ROOT = DATA_ROOT / "config"
DB_ROOT = DATA_ROOT / "db"

CHROMA_DATA_DIR = DATA_ROOT / "chroma_data"
# C1：索引代际物理目录（一代一个完整 Chroma PersistentClient 路径）。
INDEXES_DIR = DATA_ROOT / "indexes"
INDEXES_GENERATIONS_DIR = INDEXES_DIR / "generations"
WATCH_FOLDER = DATA_ROOT / "watch_folder"
VIDEO_FRAMES_DIR = DATA_ROOT / "video_frames"
VIDEO_WORK_DIR = DATA_ROOT / "video_work"
MEMORY_DIR = DATA_ROOT / "memory"
WIKI_DIR = DATA_ROOT / "wiki"
WIKI_DB_PATH = DB_ROOT / "wiki.sqlite3"
FILE_CENTER_DB_PATH = _env_path("CENTAUR_METADATA_DB", DB_ROOT / "file_center.db")
GOVERNANCE_DB_PATH = DB_ROOT / "governance.db"
JOB_STORE_DB_PATH = DB_ROOT / "job_store.db"
# P1 模型运行时设置库（配置 + secret_ref + 迁移元数据），与索引任务同数据根但独立库，
# 保证「配置写入」与「索引任务」的事务域互不干扰。
RUNTIME_SETTINGS_DB_PATH = DB_ROOT / "runtime_settings.db"
# 知君成长闭环：人生章程、判断、结果与复盘的独立本地事务域。
GROWTH_DB_PATH = DB_ROOT / "growth.db"
# 加密 secret store 默认位于数据根之外（不随业务备份复制），可用 CENTAUR_SECRET_STORE_DIR 覆盖。
SECRET_STORE_DIR = _env_path("CENTAUR_SECRET_STORE_DIR", PROJECT_ROOT / "secrets")
GENERATION_REGISTRY_DB_PATH = DB_ROOT / "generation_registry.db"
INDEX_REGISTRY_DB_PATH = DB_ROOT / "index_registry.db"
CARD_LEDGER_DB_PATH = DB_ROOT / "card_ledger.db"
AGENT_DB_PATH = DB_ROOT / "agent_gateway.db"
# 阶段 2：Consumer Connectivity 本地状态（nonce 墓碑、撤销、连接 epoch、活动会话、ACL）。
# 与业务数据同数据根但独立库，保证「票据校验」与「业务写入」的事务域互不干扰。
CONNECTIVITY_DB_PATH = DB_ROOT / "connectivity.db"
# 阶段 2：Consumer API Mock 状态库（登录/Client/Session/设备/同步/票据）。
# 仅 Mock 联调进程使用；生产 runtime 包排除整个 consumer_api 包。
CONSUMER_MOCK_DB_PATH = DB_ROOT / "consumer_mock.db"
DERIVED_STORE_DB_PATH = DB_ROOT / "derived_content.db"
DERIVED_IMAGES_DIR = DATA_ROOT / "derived_images"
# 阶段A：原材料正文快照大文件目录（material_content_snapshots 引用受控相对路径）。
MATERIAL_SNAPSHOTS_DATA_DIR = DATA_ROOT / "derived_content" / "materials"
# 阶段A：材料处理流水线 SQLite（material_jobs + material_content_snapshots 同库，
# 保证「任务终态」与「快照切换」共享事务域，避免跨库不一致）。
MATERIAL_PIPELINE_DB_PATH = DB_ROOT / "material_pipeline.db"
# 阶段 D：旧原材料 RAG collection 清理计划、审计与回滚备份的持久化状态。
STAGE_D_MAINTENANCE_DB_PATH = DB_ROOT / "stage_d_maintenance.db"
STAGE_D_BACKUPS_DIR = DATA_ROOT / "maintenance_backups" / "legacy_rag_cleanup"
LEGACY_ANNOTATIONS_PATH = DATA_ROOT / "annotations.json"
LEGACY_GROUPS_PATH = DATA_ROOT / "groups.json"
GBRAIN_HOME = _env_path("CENTAUR_GBRAIN_HOME", DATA_ROOT / "gbrain_data")
LAN_CONFIG_PATH = DATA_ROOT / ".lan_config.json"
MOBILE_CONFIG_PATH = DATA_ROOT / ".mobile_config.json"
CONTEXT_PACKS_CONFIG_PATH = DATA_ROOT / ".context_packs.json"
TRASH_DIR = DATA_ROOT / ".trash"
CONTEXT_SNAPSHOT_JSON_PATH = DATA_ROOT / ".personal_context_snapshot.json"
RAG_CONFIG_PATH = MEMORY_DIR / "rag_config.json"
TOKENMANAGER_CONFIG_DIR = CONFIG_ROOT / "centaurai-memory"
MCP_DATA_DIR = _env_path("CENTAUR_MCP_DATA_DIR", DATA_ROOT / "mcp" / "data")
MCP_CONFIG_DIR = _env_path("CENTAUR_MCP_CONFIG_DIR", DATA_ROOT / "mcp" / "config")
