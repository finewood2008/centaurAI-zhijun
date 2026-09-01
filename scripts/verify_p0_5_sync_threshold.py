#!/usr/bin/env python3
"""P0-5：Chroma #7090 上游风险隔离复现与 sync_threshold 候选验证（一次性维护脚本）。

严格在隔离数据目录中执行（本进程独占设定的 CENTAURAI_DATABASE_DATA_ROOT=全新临时目录），
绝不触碰生产 data/chroma_data。覆盖方案 §P0-5 措施 1/2 与 §7.7 验收的可编码部分：
- 单 collection 超过 sync_threshold 默认值(1000) 的向量（默认 --records 1100）；
- 分别验证「默认配置」与「候选 hnsw:sync_threshold=100000」在
  「写后 count / query、正常停止+重启、强制终止+重启」下是否可读、恢复耗时、存储体积、失败情况；
- 全程走统一工厂 vector_store.get_or_create_collection（含集合登记），不做任何直接创建调用。

运行方式（仓库根目录下）：
    .venv\\Scripts\\python.exe scripts/verify_p0_5_sync_threshold.py --records 1100 --dim 512
默认结束时清理临时数据目录；传 --keep 保留以便归档复核。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# 必须在 import 任何 backend 模块（config/runtime_paths/vector_store）之前锁定隔离数据根
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="mindos-p05-repro-")).resolve()
os.environ["CENTAURAI_DATABASE_DATA_ROOT"] = str(_TMP_ROOT)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import vector_store as vs  # noqa: E402

# 候选阈值
CAND_THRESHOLD = "100000"

# backtrace: 让 Chroma 即使异常也能给出可归档的失败日志
os.environ["CHROMA_LOG_LEVEL"] = "ERROR"


# --------------------------------------------------------------------------- #
# 归档辅助
# --------------------------------------------------------------------------- #
def _dir_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _chroma_wal_bytes() -> int:
    root = Path(vs.CHROMA_DATA_DIR)
    total = 0
    for pat in ("chroma.sqlite3", "chroma.sqlite3-wal", "chroma.sqlite3-shm"):
        p = root / pat
        if p.exists():
            total += p.stat().st_size
    return total


def _probe(col, dim: int) -> dict:
    """执行 count + 最小 query；返回可归档结果。"""
    out = {"count": "?"}
    t0 = time.perf_counter()
    try:
        out["count"] = col.count()
    except Exception as e:  # noqa: BLE001
        out["count_error"] = f"{type(e).__name__}: {e}"
    try:
        ids = col.query(
            query_embeddings=[np.random.rand(dim).tolist()],
            n_results=5,
        ).get("ids", [[]])[0]
        out["query"] = len(ids)
        out["query_first_id"] = ids[0][:24] if ids else None
    except Exception as e:  # noqa: BLE001
        out["query_error"] = f"{type(e).__name__}: {e}"
    out["elapsed_s"] = round(time.perf_counter() - t0, 3)
    return out


def _report(title: str, rows: dict) -> None:
    print(f"\n===== {title} =====")
    for k, v in rows.items():
        print(f"  {k}: {v}")


# --------------------------------------------------------------------------- #
# 强制终止子进程：写入后不 close 直接退出（模拟崩溃/强杀）
# --------------------------------------------------------------------------- #
def _spawn_kill_on_write(root: str, collection: str, records: int, dim: int, cand: bool) -> None:
    env = dict(os.environ)
    env["CENTAURAI_DATABASE_DATA_ROOT"] = root
    code = (
        "import os,sys,numpy as np;"
        f"os.environ['CENTAURAI_DATABASE_DATA_ROOT']={root!r};"
        "sys.path.insert(0,"
        + repr(str(Path(__file__).resolve().parent.parent / "backend"))
        + ");"
        "import vector_store as vs;"
        f"vs.CHROMA_SYNC_THRESHOLD_ENABLED={cand!r};"
        f"vs.CHROMA_SYNC_THRESHOLD={CAND_THRESHOLD!r};"
        "col=vs.get_or_create_collection(" + repr(collection) + ");"
        f"d={dim};n={records};"
        "arr=np.random.RandomState(7).rand(n,d).astype('float32');"
        "col.add(ids=[f'i{j}' for j in range(n)],embeddings=arr.tolist(),"
        "metadatas=[{'j':j} for j in range(n)]);"
        "sys.stdout.flush();"
        "os._exit(0)"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        check=False,
        timeout=1200,
    )


def _run_kill_scenario(root: str, records: int, dim: int, cand: bool, tag: str) -> dict:
    collection = f"repro_kill_{tag}"
    print(f"\n>> 强杀场景[{tag}] 生成 {records} 条到 '{collection}' ...")
    t0 = time.perf_counter()
    _spawn_kill_on_write(root, collection, records, dim, cand)
    bytes_before = _dir_bytes(Path(vs.CHROMA_DATA_DIR))
    wal_before = _chroma_wal_bytes()
    print(f"   已写入并强杀退出。存储={_fmt(bytes_before)} WAL={_fmt(wal_before)}")
    # 重启加载
    col = vs.get_or_create_collection(collection)  # 复用当前 client
    probe = _probe(col, dim)
    probe["storage_bytes"] = bytes_before
    probe["wal_bytes"] = wal_before
    probe["total_s"] = round(time.perf_counter() - t0, 3)
    return probe


def _fmt(n: int) -> str:
    return f"{n / 1024 / 1024:.2f} MiB" if n else "0 B"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--records", type=int, default=1100, help="单 collection 写入向量条数(默认1100>1000)")
    ap.add_argument("--dim", type=int, default=512, help="向量维度(默认512, 与 bge-small 一致)")
    ap.add_argument("--keep", action="store_true", help="结束时保留隔离数据目录")
    args = ap.parse_args()

    n, dim = args.records, args.dim
    print(f"隔离数据根: {_TMP_ROOT}")
    print(f"Chroma 版本: {aps_version()}")
    print(f"records={n} dim={dim} 候选 sync_threshold={CAND_THRESHOLD}")

    results = {}

    # ---- 场景1/2：正常停止 + 重启（默认 vs 候选） ----
    for cand, tag in ((False, "default"), (True, "cand")):
        vs.CHROMA_SYNC_THRESHOLD_ENABLED = cand
        if cand:
            vs.CHROMA_SYNC_THRESHOLD = CAND_THRESHOLD
        collection = f"repro_{tag}"
        print(f"\n>> 正常场景[{tag}] 写 {n} 条到 '{collection}' (cand={cand}) ...")
        t0 = time.perf_counter()
        col = vs.get_or_create_collection(collection)
        ids = [f"i{j}" for j in range(n)]
        embs = np.random.RandomState(0 if not cand else 1).rand(n, dim).astype("float32")
        for b in range(0, n, 250):
            col.add(
                ids=ids[b : b + 250],
                embeddings=embs[b : b + 250].tolist(),
                metadatas=[{"j": j} for j in range(b, min(b + 250, n))],
            )
        written = col.count()
        print(f"   写入完成 count={written}")
        # 正常停止（优雅关闭）
        vs.release_chroma()
        data_bytes = _dir_bytes(Path(vs.CHROMA_DATA_DIR))
        wal_bytes = _chroma_wal_bytes()
        t_reopen = time.perf_counter()
        # 重启加载
        col2 = vs.get_or_create_collection(collection)
        probe = _probe(col2, dim)
        probe.update(
            write_s=round(time.perf_counter() - t0, 3),
            storage_bytes=data_bytes,
            wal_bytes=wal_bytes,
        )
        results[f"normal_{tag}"] = probe
        inputs = {
            "config": f"default(无阈值)" if not cand else f"cand={CAND_THRESHOLD}",
            "written_count": written,
            "restart_count": probe["count"],
            "query_n": probe.get("query"),
            "reopen_s": probe["elapsed_s"],
            "storage": _fmt(data_bytes),
            "wal": _fmt(wal_bytes),
            "errors": probe.get("count_error") or probe.get("query_error") or "无",
        }
        _report(f"正常停止+重启 [{tag}]", inputs)

    # ---- 场景3/4：强制终止(模拟崩溃) + 重启（默认 vs 候选） ----
    try:
        for cand, tag in ((False, "default"), (True, "cand")):
            vs.release_chroma()  # 释放句柄，交给子进程独占重开
            vs.CHROMA_SYNC_THRESHOLD_ENABLED = cand
            if cand:
                vs.CHROMA_SYNC_THRESHOLD = CAND_THRESHOLD
            probe = _run_kill_scenario(
                str(_TMP_ROOT), n, dim, cand, tag
            )
            results[f"kill_{tag}"] = probe
            inputs = {
                "config": "default" if not cand else f"cand={CAND_THRESHOLD}",
                "restart_count": probe["count"],
                "query_n": probe.get("query"),
                "total_s": probe["total_s"],
                "storage": _fmt(probe["storage_bytes"]),
                "wal": _fmt(probe["wal_bytes"]),
                "errors": probe.get("count_error") or probe.get("query_error") or "无",
            }
            _report(f"强制终止+重启 [{tag}]", inputs)
    except Exception as e:  # noqa: BLE001
        print(f"\n!! 强杀场景中止: {type(e).__name__}: {e}")

    # ---- 汇总 ----
    print("\n================ 汇总 ================")
    for key, p in results.items():
        ok = "count" in p and p["count"] is not None
        err = p.get("count_error") or p.get("query_error") or "无"
        print(f"{key:16} count={p.get('count')} query={p.get('query')} "
              f"elapsed_s={p.get('elapsed_s', p.get('total_s'))} errors={err}")

    if not args.keep:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
        print(f"\n已清理隔离数据根: {_TMP_ROOT}")
    else:
        print(f"\n保留隔离数据根(--keep): {_TMP_ROOT}")
    return 0


def aps_version() -> str:
    try:
        import chromadb

        return getattr(chromadb, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())