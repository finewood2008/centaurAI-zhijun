#!/usr/bin/env python3
"""为所有已入库材料补算 RELATION_EXTRACTION 派生产物（一次性维护脚本）。

原理：复用 mindos.derived.refresh_analysis 的「缺失/失败/不可用才投递 + hash 未变跳过」机制，
天然幂等；关系任务进入 _ANALYSIS_POOL 后台池异步执行，不阻塞脚本主流程。

可恢复运行（评审要求，backfill 必须支持中途打断安全重跑）：
- 分批：--batch-size 限制单批投递数量；
- 进度：--resume <progress_file> 记录已确认完成的 material_id，重跑时跳过；
  脚本仅把稳定为 ok / skipped 的材料写入进度文件，failed / unavailable 不写，下轮重试；
- 等待：--wait 等待本批后台任务落定并输出 成功/跳过/失败 统计；
- 失败明细：只输出 materialId 与派生状态（错误码/类型），绝不打印正文或物理路径。

运行方式（在仓库根目录下，脚本位于 scripts/）：
    .venv\\Scripts\\python.exe scripts/backfill_relations.py --batch-size 20 --wait --resume .progress.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from mindos import derived  # noqa: E402
from mindos.derived import KIND_RELATION_EXTRACTION, OWNER_MATERIAL  # noqa: E402
from mindos.services import ingestion  # noqa: E402
from mindos.stores import derived_store  # noqa: E402

# 关系派生记录的"稳定终态"：ok=成功（含 fallback 空结果）、skipped=空文本。
# failed / unavailable 不算完成，不回写进度文件，便于下轮重试。
_DONE = {"ok", "skipped"}
_TERMINAL = set(_DONE) | {"failed", "unavailable"}
_WAIT_INTERVAL = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="回填所有已入库材料的 RELATION_EXTRACTION 派生产物（幂等、可恢复）。"
    )
    parser.add_argument("--batch-size", type=int, default=10, help="单批投递的材料数（默认 10）")
    parser.add_argument("--resume", type=Path, help="进度文件路径；已完成的 material_id 会被跳过")
    parser.add_argument("--wait", action="store_true", help="等待本批后台任务落定并输出成功/跳过/失败统计")
    parser.add_argument("--timeout", type=float, default=120.0, help="单个材料等待终态的秒数上限（默认 120）")
    return parser.parse_args()


def load_progress(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"[进度警告] 读取进度文件失败，将全部重跑：{exc}", file=sys.stderr)
        return set()
    items = data if isinstance(data, list) else []
    return {str(i) for i in items if isinstance(i, str)}


def save_progress(path: Path | None, done: set[str]) -> None:
    if path is None:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _record_version(rec: dict | None) -> tuple | None:
    """用于区分回填前的稳定记录和本次任务产生的新记录。"""
    if rec is None:
        return None
    return (rec.get("updated_at"), rec.get("input_hash"), rec.get("status"))


def _wait_terminal(store, material_id: str, timeout: float, previous_version: tuple | None = None) -> str | None:
    """等待关系记录到达终态；有旧记录时必须确认本次任务已更新它。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION)
        status = rec.get("status") if rec else None
        if status in _TERMINAL and (
            previous_version is None or _record_version(rec) != previous_version
        ):
            return status
        time.sleep(_WAIT_INTERVAL)
    return None


def main() -> int:
    args = parse_args()
    if args.batch_size < 1:
        print("错误：--batch-size 必须 >= 1。", file=sys.stderr)
        return 2

    # P0-1 环境预检（锁先于连接）：必须在打开任何 ChromaDB 之前获取数据根目录
    # 独占锁——后端正在运行（或另一维护进程持锁）时直接拒绝。旧预检（先打开
    # ChromaDB 再 count()）在并发场景下自身就是第二个索引破坏源，已废弃。
    from instance_lock import acquire as acquire_instance_lock, holder_hint

    lock, holder = acquire_instance_lock(role="backfill-relations")
    if lock is None:
        print(
            f"错误：数据目录被其他进程占用（{holder_hint(holder)}）。"
            "请先停止正在运行的后端进程（并发访问 ChromaDB 会损坏 HNSW 索引），"
            "再重试本脚本。",
            file=sys.stderr,
        )
        return 2

    try:
        return _run(args)
    finally:
        lock.release()  # 正常结束主动释放；异常退出由 OS 兜底回收


def _run(args: argparse.Namespace) -> int:
    store = derived_store.DerivedStore.instance()

    # 索引健康预检（持锁后无并发，count() 失败即索引损坏而非被占用）：
    # 损坏时 get_source_chunks 会静默返回空列表，导致 _input_text 拿到空文本、
    # 误写派生状态（历史事故：20 个材料的 ok 关系记录被覆盖成 skipped）。
    from vector_store import get_collection

    try:
        get_collection().count()
    except Exception as exc:
        print(
            f"错误：ChromaDB 索引不可读（{type(exc).__name__}）。"
            "索引疑似损坏，请先按《MindOS索引可靠性问题分析与改进方案》§9 的"
            "恢复流程处理（备份后诊断），再重试本脚本。",
            file=sys.stderr,
        )
        return 2

    done = load_progress(args.resume)

    # 只回填仍有源文件的材料；跳过已回收材料（不进图谱，避免无谓调用 LLM）。
    targets: list[tuple[str, str]] = []  # (material_id, file_name)
    for rec in ingestion.JobStore.instance().list():
        mid = rec["material_id"]
        if mid in done:
            continue
        if ingestion.is_recycled(mid):
            continue
        sp = ingestion.source_path_of(mid)
        if sp:
            targets.append((mid, rec.get("file_name") or mid))

    print(f"待回填材料：{len(targets)} 个（已跳过：{len(done)} 个完成项）")
    if not targets:
        return 0

    ok_count = skipped_count = failed_count = submitted = 0
    failures: list[str] = []

    batch_size = args.batch_size  # 已保证 >= 1，无需按 0 转换
    batches = [targets[i : i + batch_size] for i in range(0, len(targets), batch_size)]

    for batch in batches:
        submitted_ids: list[tuple[str, bool, tuple | None]] = []
        for mid, fname in batch:
            try:
                previous = _record_version(store.get_derived_record(
                    OWNER_MATERIAL, mid, KIND_RELATION_EXTRACTION,
                ))
                result = derived.refresh_analysis(mid, ingestion.source_path_of(mid))  # type: ignore[arg-type]
            except Exception as exc:
                # 失败明细仅 materialId 与错误类型，绝不输出 str(exc)（可能含路径/请求体等敏感信息）
                failures.append(f"[FAIL] {mid} error_type={type(exc).__name__}")
                failed_count += 1
                continue
            scheduled = isinstance(result, dict) and bool(result.get("relationScheduled"))
            submitted_ids.append((mid, scheduled, previous))
            submitted += 1
            print(f"[已投递] {mid} ({fname})")

        if args.wait:
            for mid, scheduled, previous in submitted_ids:
                # 未投递说明已有记录无需重算，直接读取其稳定状态；投递后则必须
                # 等待版本变化，不能把提交前残留的 ok 误记为本轮完成。
                if scheduled:
                    status = _wait_terminal(store, mid, args.timeout, previous)
                else:
                    rec = store.get_derived_record(OWNER_MATERIAL, mid, KIND_RELATION_EXTRACTION)
                    status = rec.get("status") if rec else None
                if status is None:
                    failures.append(f"[FAIL] {mid} status=timeout")
                    failed_count += 1
                elif status in _DONE:
                    done.add(mid)
                    if status == "ok":
                        ok_count += 1
                    else:
                        skipped_count += 1
                else:
                    failures.append(f"[FAIL] {mid} status={status}")
                    failed_count += 1
            save_progress(args.resume, done)

    save_progress(args.resume, done)
    print(f"完成：submitted={submitted}, ok={ok_count}, skipped={skipped_count}, failed={failed_count}")
    for line in failures:  # 失败明细仅 materialId 与状态/错误类型，不含正文与路径
        print(line, file=sys.stderr)
    return 1 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
