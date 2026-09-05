#!/usr/bin/env python3
"""一次性压缩 agent_memory collection 的 HNSW 索引。

背景：_index_memory_file 的 delete+add 模式会在 hnswlib 索引里留下墓碑，
索引文件只增不减；多次全量重建后 data_level0.bin 可膨胀到活数据的几十倍。
本脚本把活记录逐批拷贝到新 collection（get 只返回活记录，天然滤掉墓碑），
校验数量一致后删除旧 collection 并把新集合改回原名。向量逐位不变，零重嵌入。

必须在后端停止后运行（P0-1：脚本先获取数据根目录独占锁，锁先于 ChromaDB 连接；
后端正在运行时会直接拒绝执行）：
    systemctl --user stop centaur-vector-db.service
    cp -a chroma_data chroma_data.bak-$(date +%F)   # 建议先备份
    backend/.venv/bin/python scripts/compact_agent_memory.py
    systemctl --user start centaur-vector-db.service
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import chromadb
from chromadb.config import Settings

from config import CHROMA_DATA_DIR, MEMORY_COLLECTION
from instance_lock import acquire as acquire_instance_lock, holder_hint

TMP_NAME = MEMORY_COLLECTION + "_compact"
BATCH = 2000


def main() -> int:
    # P0-1 维护锁（锁先于连接）：后端或其他维护进程持锁时直接拒绝，
    # 不得在并发场景下打开 ChromaDB。
    lock, holder = acquire_instance_lock(role="compact-agent-memory")
    if lock is None:
        print(
            f"错误：数据目录被其他进程占用（{holder_hint(holder)}）。"
            "请先停止正在运行的后端进程，再重试本脚本。",
            file=sys.stderr,
        )
        return 2

    try:
        return _run()
    finally:
        lock.release()


def _run() -> int:
    client = chromadb.PersistentClient(
        path=CHROMA_DATA_DIR,
        settings=Settings(anonymized_telemetry=False),
    )

    try:
        src = client.get_collection(MEMORY_COLLECTION)
    except Exception as exc:
        print(f"错误：找不到 collection {MEMORY_COLLECTION}: {exc}", file=sys.stderr)
        return 1

    total = src.count()
    print(f"源 collection {MEMORY_COLLECTION}: {total} 条活记录")

    # 残留的半成品临时集合直接丢弃重来
    try:
        client.delete_collection(TMP_NAME)
        print(f"已清理上次残留的 {TMP_NAME}")
    except Exception:
        pass

    dst = client.create_collection(TMP_NAME, metadata={"hnsw:space": "cosine"})

    copied = 0
    offset = 0
    while True:
        batch = src.get(
            include=["embeddings", "documents", "metadatas"],
            limit=BATCH,
            offset=offset,
        )
        ids = batch.get("ids") or []
        if len(ids) == 0:
            break
        dst.add(
            ids=ids,
            embeddings=batch["embeddings"],
            documents=batch["documents"],
            metadatas=batch["metadatas"],
        )
        copied += len(ids)
        offset += len(ids)
        print(f"  已拷贝 {copied}/{total}")
        if len(ids) < BATCH:
            break

    dst_count = dst.count()
    if dst_count != total:
        print(
            f"错误：拷贝后数量不一致（源 {total}，新 {dst_count}），"
            f"保留旧 collection 不动，请删除 {TMP_NAME} 后重试",
            file=sys.stderr,
        )
        return 1

    print(f"校验通过（{dst_count} 条），删除旧 collection 并改名...")
    client.delete_collection(MEMORY_COLLECTION)
    dst.modify(name=MEMORY_COLLECTION)
    print(f"完成：{MEMORY_COLLECTION} 已压缩为 {dst_count} 条活记录的紧凑索引")
    return 0


if __name__ == "__main__":
    sys.exit(main())
