"""真实模型评测：用 OpenAI 兼容通道（如 DeepSeek）跑一遍建档 + 商量 + 闲聊，把回复、抽取结果、草稿、整合报告落成一份评测文件。

用法（仓库根目录）：
    export ZHIJUN_OPENAI_BASE_URL=https://api.deepseek.com/v1 ZHIJUN_OPENAI_MODEL=deepseek-chat ZHIJUN_OPENAI_API_KEY=...
    backend/.venv/bin/python scripts/real_model_session.py --out docs/development/real-model-eval.md

- 密钥只从环境变量读取，不写入任何文件；评测文件里不含密钥。
- 数据根是一次性临时目录；结束后关闭后端。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
HEADERS = {"X-Requested-By": "centaur-vdb"}

ONBOARDING_ANSWERS = [
    "你好，我们开始吧。",
    "叫我阿远就行。我是一家 12 人小公司的创始人，做企业软件；也是两个孩子的父亲，老大八岁老二三岁。",
    "最占心思的是远川项目——我们给一家制造业客户做的生产排程系统，下个月要上线，我在带一个 5 人的小组做它，进度有点悬。",
    "最在意的是我太太林岚，还有合伙人老周。老周负责销售，我们认识十年了；林岚在一家外企做财务，家里的事基本是她在扛。",
    "最近纠结的是要不要接一个大客户的定制单，钱多但会拖慢产品。我最后拒了，因为我觉得产品化才是公司的命，不能被一个客户绑住。",
    "我做事有条原则：先看数据再拍板，不凭感觉决定大事。",
    "接下来一两年，我想把公司做到盈亏平衡，然后能把周末还给家里。我不想变成一个只会开会的人。",
    "健康和家里的矛盾这些话题不用主动提；重大人事决定不要替我拿主意。",
]

DELIBERATE_TURNS = [
    "我在纠结要不要把远川项目的测试外包出去，还是让小组自己做。",
    "我倾向自己做，因为外包的人不懂业务，返工更慢；但小组已经很累了。把握大概六成。预期是上线前把主要流程跑通。",
]

CHAT_TURNS = [
    "远川项目下周要上线了，我有点慌。",
    "老周觉得我们应该先接大客户的钱，我不这么想，我们吵了一架。",
]


def _sse(client: httpx.Client, url: str, payload: dict) -> tuple[list[tuple[str, dict]], str]:
    events: list[tuple[str, dict]] = []
    name, data = None, []
    with client.stream("POST", url, json=payload, headers=HEADERS, timeout=180) as res:
        if res.status_code != 200:
            raise RuntimeError(f"{url} -> {res.status_code}: {res.read().decode('utf-8', 'replace')[:300]}")
        for line in res.iter_lines():
            if line == "":
                if name is not None:
                    events.append((name, json.loads("\n".join(data) or "{}")))
                name, data = None, []
            elif line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
    if name is not None:
        events.append((name, json.loads("\n".join(data) or "{}")))
    reply = "".join(d["t"] for n, d in events if n == "token")
    return events, reply


def _wait_health(base: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/api/health", headers=HEADERS, timeout=3).status_code == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    raise RuntimeError("后端未就绪")


def _wait_jobs(client: httpx.Client, base: str, seconds: float = 300) -> None:
    """等本体 worker 把队列跑空（真实模型每条抽取 5–15 秒，建档 7 轮要一两分钟）。"""
    deadline = time.time() + seconds
    idle = 0
    while time.time() < deadline:
        status = client.get(f"{base}/api/mindos/zhijun/status", headers=HEADERS).json()
        pending = status.get("pendingJobs")
        idle = idle + 1 if not pending else 0
        if idle >= 2:
            return
        time.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/development/real-model-eval.md")
    parser.add_argument("--port", type=int, default=8618)
    args = parser.parse_args()
    for var in ("ZHIJUN_OPENAI_BASE_URL", "ZHIJUN_OPENAI_MODEL", "ZHIJUN_OPENAI_API_KEY"):
        if not os.environ.get(var):
            print(f"缺少环境变量 {var}", file=sys.stderr)
            return 2

    data_root = Path(tempfile.mkdtemp(prefix="zhijun-real-"))
    env = {
        **os.environ,
        "CENTAURAI_DATABASE_DATA_ROOT": str(data_root),
        "MINDOS_RUNTIME_ENV": "development",
        "MINDOS_LOCAL_WEB_DEBUG_ACCESS": "true",
        "ZHIJUN_PROVIDER": "openai",
        "ZHIJUN_MATERIAL_EVIDENCE": "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    log = open(data_root / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([str(BACKEND / ".venv" / "bin" / "python"), "server.py"], cwd=str(BACKEND), env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{args.port}"
    report: list[str] = [f"# 真实模型评测 · {os.environ['ZHIJUN_OPENAI_MODEL']} · {time.strftime('%Y-%m-%d %H:%M')}", ""]
    timings: list[float] = []
    try:
        _wait_health(base, 180)
        with httpx.Client(timeout=60) as client:
            status = client.get(f"{base}/api/mindos/zhijun/status", headers=HEADERS).json()
            report.append(f"通道：{status['provider']} / {status['model']} / external={status['external']} / extraction={status['extraction']}")
            report.append("")
            # ---- 建档
            report.append("## 一、建档对话（7 问）")
            conv = client.post(f"{base}/api/mindos/conversations", json={"mode": "onboarding"}, headers=HEADERS).json()
            for i, answer in enumerate(ONBOARDING_ANSWERS, start=1):
                t0 = time.time()
                events, reply = _sse(client, f"{base}/api/mindos/conversations/{conv['id']}/messages", {"content": answer})
                timings.append(time.time() - t0)
                report.append(f"**用户 {i}：** {answer}")
                report.append(f"**知君：** {reply.strip()}")
                report.append("")
            _wait_jobs(client, base)
            claims = client.get(f"{base}/api/mindos/ontology/claims", params={"trust": "confirmed,working", "limit": 500}, headers=HEADERS).json()["items"]
            report.append(f"### 建档后抽取到的理解（{len(claims)} 条）")
            report.append("| 分区 | 层 | 信任 | 内容 | 依据原话 |")
            report.append("|---|---|---|---|---|")
            for c in claims:
                quote = (c["evidence"][0]["quote"] if c.get("evidence") else "")[:40]
                report.append(f"| {c['section']} | {c['layer']} | {c['trustState']} | {c['content']} | {quote} |")
            report.append("")
            # ---- 商量
            report.append("## 二、商量（判断草稿）")
            dconv = client.post(f"{base}/api/mindos/conversations", json={"mode": "chat", "title": "商量：测试外包"}, headers=HEADERS).json()
            draft = None
            for turn in DELIBERATE_TURNS:
                t0 = time.time()
                events, reply = _sse(client, f"{base}/api/mindos/conversations/{dconv['id']}/messages", {"content": turn, "mode": "deliberate"})
                timings.append(time.time() - t0)
                draft = next((d for n, d in events if n == "decision_draft"), draft)
                report.append(f"**用户：** {turn}")
                report.append(f"**知君：** {reply.strip()}")
                report.append("")
            # 真实模型下草稿是后台任务：SSE 只发 state=queued，等 worker 跑完再取。
            _wait_jobs(client, base)
            got = client.get(f"{base}/api/mindos/conversations/{dconv['id']}/decision-draft", headers=HEADERS)
            final = got.json() if got.status_code == 200 else None
            shown = {k: (final or {}).get(k) for k in ("state", "fields", "relatedDecisionIds", "error")} if isinstance(final, dict) else final
            report.append(f"### 判断草稿（SSE 首次事件 state={draft.get('state') if draft else None}；等后台任务后取到）")
            report.append("```json")
            report.append(json.dumps(shown if final else None, ensure_ascii=False, indent=2))
            report.append("```")
            report.append("")
            # ---- 闲聊（看是否引用本体、标签是否规范）
            report.append("## 三、日常对话（看引用与标签）")
            cconv = client.post(f"{base}/api/mindos/conversations", json={"mode": "chat"}, headers=HEADERS).json()
            for turn in CHAT_TURNS:
                t0 = time.time()
                events, reply = _sse(client, f"{base}/api/mindos/conversations/{cconv['id']}/messages", {"content": turn})
                timings.append(time.time() - t0)
                prov = next((d for n, d in events if n == "provenance"), {})
                report.append(f"**用户：** {turn}")
                report.append(f"**知君：** {reply.strip()}")
                report.append(f"_出处：已确认 {len(prov.get('confirmedClaims', []))} 条 · 工作理解 {len(prov.get('workingClaims', []))} 条 · 避开 {prov.get('retractedNotices', 0)} 条_")
                report.append("")
            _wait_jobs(client, base)
            # ---- 整合
            consolidated = client.post(f"{base}/api/mindos/ontology/consolidate", headers=HEADERS).json()
            proposals = client.get(f"{base}/api/mindos/ontology/proposals", headers=HEADERS).json()
            stats = client.get(f"{base}/api/mindos/ontology/stats", headers=HEADERS).json()
            report.append("## 四、整合器")
            report.append(f"报告：`{json.dumps(consolidated, ensure_ascii=False)}`")
            for c in proposals["conflicts"]:
                report.append(f"- [{c['kind']}] 「{c['claimA']['content']}」 vs 「{c['claimB']['content']}」（{c['note']}）")
            for m in proposals["merges"]:
                report.append(f"- [merge] {m['fromName']} → {m['intoName']}（{m['reason']}）")
            report.append("")
            report.append("## 五、统计")
            report.append(f"- 理解：已确认 {stats['claims']['confirmed']}，待确认 {stats['claims']['working']}，实体 {stats['entities']}，待裁决 {stats.get('proposals', 0)}")
            report.append(f"- 每轮耗时：中位 {sorted(timings)[len(timings)//2]:.1f}s，最长 {max(timings):.1f}s，共 {len(timings)} 轮")
            entities = client.get(f"{base}/api/mindos/ontology/entities", headers=HEADERS).json()["items"]
            report.append("- 实体：" + "、".join(f"{e['canonicalName']}({e['type']})" for e in entities if e["type"] != "me"))
    except Exception as exc:  # noqa: BLE001
        report.append(f"\n**失败：** {type(exc).__name__}: {exc}")
        try:
            log.flush()
            report.append("\n```\n" + (data_root / "server.log").read_text(encoding="utf-8", errors="replace")[-2500:] + "\n```")
        except OSError:
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(report)
    assert os.environ["ZHIJUN_OPENAI_API_KEY"] not in text
    out.write_text(text + "\n", encoding="utf-8")
    shutil.rmtree(data_root, ignore_errors=True)
    print(f"评测写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
