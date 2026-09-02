"""知君 P1 端到端：起真实后端（演示模型）→ 建会话 → 三轮 SSE → 抽取入 inbox → 确认 → 下一轮引用 → 撤回不回流 → 投影 → 前端页面可服务。

用法（仓库根目录）：
    backend/.venv/bin/python scripts/e2e_zhijun_phase1.py [--keep] [--port 8618]

- 数据根指向一次性临时目录，不碰 data/。
- ZHIJUN_PROVIDER=fake：不调用任何模型服务，不出网。
- 结束时打印每一步的结果与总结；任一断言失败以非 0 退出。
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


def _sse_events(client: httpx.Client, url: str, payload: dict) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = None
    data: list[str] = []
    with client.stream("POST", url, json=payload, headers=HEADERS, timeout=120) as res:
        if res.status_code != 200:
            body = res.read().decode("utf-8", errors="replace")
            raise AssertionError(f"POST {url} -> {res.status_code}: {body[:300]}")
        for line in res.iter_lines():
            if line == "":
                if name is not None:
                    events.append((name, json.loads("\n".join(data) or "{}")))
                name, data = None, []
                continue
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
    if name is not None:
        events.append((name, json.loads("\n".join(data) or "{}")))
    return events


def _reply(events: list[tuple[str, dict]]) -> str:
    return "".join(d["t"] for n, d in events if n == "token")


def _wait_health(base: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            res = httpx.get(f"{base}/api/health", headers=HEADERS, timeout=3)
            if res.status_code == 200:
                return
            last = f"{res.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        time.sleep(1)
    raise AssertionError(f"后端 {timeout}s 内未就绪：{last}")


def _poll_inbox(client: httpx.Client, base: str, expect_at_least: int, timeout: float = 30) -> list[dict]:
    deadline = time.time() + timeout
    items: list[dict] = []
    while time.time() < deadline:
        items = client.get(f"{base}/api/mindos/ontology/inbox", headers=HEADERS).json()["items"]
        stats = client.get(f"{base}/api/mindos/ontology/stats", headers=HEADERS).json()
        if len(items) >= expect_at_least and stats["claims"]["confirmed"] >= 1:
            return items
        time.sleep(1)
    return items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8618)
    parser.add_argument("--keep", action="store_true", help="结束后保留后端进程与数据目录")
    args = parser.parse_args()

    data_root = Path(tempfile.mkdtemp(prefix="zhijun-e2e-"))
    env = {
        **os.environ,
        "CENTAURAI_DATABASE_DATA_ROOT": str(data_root),
        "MINDOS_RUNTIME_ENV": "development",
        "MINDOS_LOCAL_WEB_DEBUG_ACCESS": "true",
        "ZHIJUN_PROVIDER": "fake",
        "ZHIJUN_MATERIAL_EVIDENCE": "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "MEMORY_IMPORT_AUTO_SYNC": "false",
    }
    python = BACKEND / ".venv" / "bin" / "python"
    log_path = data_root / "server.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([str(python), "server.py"], cwd=str(BACKEND), env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{args.port}"
    steps: list[tuple[str, str]] = []
    ok = True
    try:
        _wait_health(base, 180)
        steps.append(("后端就绪", base))
        with httpx.Client(timeout=30) as client:
            status = client.get(f"{base}/api/mindos/zhijun/status", headers=HEADERS).json()
            assert status["provider"] == "fake", status
            steps.append(("模型通道", f"{status['provider']} / extraction={status['extraction']} / worker={status['workerRunning']}"))

            conv = client.post(f"{base}/api/mindos/conversations", json={"mode": "chat"}, headers=HEADERS).json()
            events = _sse_events(client, f"{base}/api/mindos/conversations/{conv['id']}/messages", {"content": "我在做远川项目，压力很大。我想明年把公司做到盈利。"})
            names = [n for n, _ in events]
            assert names[:2] == ["meta", "provenance"] and names[-2:] == ["extraction", "message_done"], names
            assert events[-2][1]["state"] == "queued", events[-2]
            steps.append(("第 1 轮 SSE", f"{len(names)} 个事件，抽取已入队"))

            inbox = _poll_inbox(client, base, expect_at_least=1)
            stats = client.get(f"{base}/api/mindos/ontology/stats", headers=HEADERS).json()
            assert stats["claims"]["confirmed"] >= 1 and len(inbox) >= 1, (stats, inbox)
            steps.append(("抽取 → 本体", f"已确认 {stats['claims']['confirmed']} 条（用户原话直接确认），待确认 {len(inbox)} 条"))
            working = inbox[0]
            told = client.get(f"{base}/api/mindos/ontology/claims", params={"trust": "confirmed"}, headers=HEADERS).json()["items"][0]

            review = client.post(
                f"{base}/api/mindos/ontology/claims/{working['id']}/review",
                json={"action": "confirm", "surface": "conversation", "conversationId": conv["id"]},
                headers=HEADERS,
            )
            assert review.status_code == 200 and review.json()["claim"]["trustState"] == "confirmed", review.text
            steps.append(("一键确认", f"「{working['content']}」→ confirmed，并写入系统备注"))

            events = _sse_events(client, f"{base}/api/mindos/conversations/{conv['id']}/messages", {"content": "远川项目最近推进得怎么样"})
            prov = next(d for n, d in events if n == "provenance")
            ids = [c["id"] for c in prov["confirmedClaims"]]
            reply = _reply(events)
            assert told["id"] in ids and working["id"] in ids, ids
            assert "我记得你说过" in reply and "【你告诉我的】" in reply, reply
            steps.append(("第 2 轮引用本体", f"provenance 含 {len(ids)} 条已确认理解；回复带来源标签"))

            retract = client.post(f"{base}/api/mindos/ontology/claims/{told['id']}/review", json={"action": "retract", "surface": "ontology_page"}, headers=HEADERS)
            assert retract.status_code == 200, retract.text
            events = _sse_events(client, f"{base}/api/mindos/conversations/{conv['id']}/messages", {"content": "远川项目"})
            prov = next(d for n, d in events if n == "provenance")
            reply = _reply(events)
            assert told["id"] not in [c["id"] for c in prov["confirmedClaims"]], prov
            assert prov["retractedNotices"] >= 1, prov
            assert told["content"] not in reply, reply
            steps.append(("撤回后不回流", f"retractedNotices={prov['retractedNotices']}，回复不再复述被撤回的理解"))

            time.sleep(2)  # 等投影任务
            projection = client.get(f"{base}/api/mindos/ontology/projection", headers=HEADERS).json()
            assert working["content"] in projection["markdown"] and told["content"] not in projection["markdown"], projection["markdown"]
            profile = data_root / "memory" / "ZHIJUN_PROFILE.md"
            steps.append(("投影", f"ZHIJUN_PROFILE.md {'已生成' if profile.is_file() else '未生成（后台任务尚未跑完）'}"))

            detail = client.get(f"{base}/api/mindos/conversations/{conv['id']}", headers=HEADERS).json()
            roles = [m["role"] for m in detail["messages"]]
            assert roles.count("system") >= 1 and roles.count("assistant") == 3, roles
            steps.append(("会话记录", f"{len(roles)} 条消息（含 {roles.count('system')} 条系统备注）"))

            # ---------------- P2：商量 → 草稿 → 判断簿 → 提醒 → 回访 → 结果
            dconv = client.post(f"{base}/api/mindos/conversations", json={"mode": "chat", "title": "商量：远川项目怎么做"}, headers=HEADERS).json()
            events = _sse_events(client, f"{base}/api/mindos/conversations/{dconv['id']}/messages", {"content": "我在纠结要不要把远川项目外包出去还是自己招人做。", "mode": "deliberate"})
            draft = next(d for n, d in events if n == "decision_draft")
            assert len(draft["fields"]["options"]) == 2, draft["fields"]
            events = _sse_events(client, f"{base}/api/mindos/conversations/{dconv['id']}/messages", {"content": "我倾向自己招人，因为控制力更强，七成把握，预期三个月内团队到位。", "mode": "deliberate"})
            draft = next(d for n, d in events if n == "decision_draft")
            assert draft["revision"] == 2 and draft["fields"]["confidence"] == 70, draft
            steps.append(("商量 → 判断草稿", f"两轮后草稿 revision={draft['revision']}，选项 {len(draft['fields']['options'])} 个，把握 {draft['fields']['confidence']}%（都来自用户原话）"))
            yesterday = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400 * 2)))
            confirmed = client.post(
                f"{base}/api/mindos/conversations/{dconv['id']}/decision-draft/confirm",
                json={"choice": "自己招人", "expectedOutcome": "三个月内团队到位", "reviewAt": yesterday},
                headers=HEADERS,
            )
            assert confirmed.status_code == 200, confirmed.text
            decision = confirmed.json()["decision"]
            assert decision["status"] == "open" and decision["confidence"] == 70, decision
            steps.append(("一键入判断簿", f"判断「{decision['title']}」已写入 growth_decisions（回访日设为两天前以触发提醒）"))

            scan = client.post(f"{base}/api/mindos/nudges/scan", headers=HEADERS).json()
            today = client.get(f"{base}/api/mindos/nudges/today", headers=HEADERS).json()["items"]
            assert scan["created"] >= 1 and today and today[0]["triggerRef"]["decisionId"] == decision["id"], (scan, today)
            steps.append(("到期提醒", f"{today[0]['whyNow']}"))

            rconv = client.post(f"{base}/api/mindos/conversations", json={"mode": "review", "decisionId": decision["id"]}, headers=HEADERS).json()
            events = _sse_events(client, f"{base}/api/mindos/conversations/{rconv['id']}/messages", {"content": "招到了三个人，比预期晚了两周。"})
            assert "回访" in _reply(events), _reply(events)
            outcome = client.post(f"{base}/api/mindos/conversations/{rconv['id']}/outcome", json={"result": "招到了三个人，比预期晚两周", "notes": ""}, headers=HEADERS).json()
            assert outcome["decision"]["status"] == "outcome_recorded" and outcome["nudgesActed"] >= 1, outcome
            after = client.get(f"{base}/api/mindos/nudges/today", headers=HEADERS).json()["items"]
            assert not after, after
            events = _sse_events(client, f"{base}/api/mindos/conversations/{rconv['id']}/messages", {"content": "感觉当时低估了招人的周期。"})
            assert "复盘" in _reply(events), _reply(events)
            steps.append(("回访 → 记下结果 → 复盘引导", "判断状态 outcome_recorded，提醒已完成，知君按五段引导复盘"))

            # ---------------- P3：整合器 → 矛盾裁决 → 张力提醒 → 导出
            for content, section in (("我从不在周末加班", "principles"), ("我这周末在加班赶远川项目", "matters"), ("我不坚持先看数据再拍板", "principles"), ("我坚持先看数据再拍板", "principles")):
                r = client.post(f"{base}/api/mindos/ontology/claims", json={"content": content, "section": section}, headers=HEADERS)
                assert r.status_code == 200, r.text
            report = client.post(f"{base}/api/mindos/ontology/consolidate", headers=HEADERS).json()
            proposals = client.get(f"{base}/api/mindos/ontology/proposals", headers=HEADERS).json()
            assert report["conflicts"] >= 1 and report["tensions"] >= 1, (report, proposals)
            contradiction = next(c for c in proposals["conflicts"] if c["kind"] == "contradiction")
            resolved = client.post(f"{base}/api/mindos/ontology/proposals/conflicts/{contradiction['id']}/resolve", json={"keep": "a"}, headers=HEADERS)
            assert resolved.status_code == 200, resolved.text
            tension = [n for n in client.get(f"{base}/api/mindos/nudges/today", headers=HEADERS).json()["items"] if n["kind"] == "principle_tension"]
            assert tension and tension[0]["message"].endswith("？"), tension
            export = client.get(f"{base}/api/mindos/ontology/export", headers=HEADERS).json()
            assert export["claims"] and all(c["trustState"] == "confirmed" for c in export["claims"]), len(export["claims"])
            steps.append(("整合 → 裁决 → 张力提醒 → 导出", f"矛盾对 {report['conflicts']} 已裁决 1，张力提醒 1 条（问句），导出 {len(export['claims'])} 条已确认理解"))

            page = client.get(f"{base}/mindos/", headers=HEADERS)
            served = page.status_code == 200 and "<div id=\"app\"" in page.text
            steps.append(("前端页面", "已由后端在 /mindos/ 提供" if served else f"未提供（{page.status_code}），请先 npm run build"))
            assert served, page.status_code
    except AssertionError as exc:
        ok = False
        steps.append(("失败", str(exc)[:500]))
    except Exception as exc:  # noqa: BLE001
        ok = False
        steps.append(("异常", f"{type(exc).__name__}: {exc}"))
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
    print("\n知君 P1 端到端结果：")
    for name, detail in steps:
        print(f"  [{'ok' if name not in ('失败', '异常') else '!!'}] {name}：{detail}")
    print(f"\n{'全部通过' if ok else '未通过'}；服务日志：{log_path}")
    if not ok:
        try:
            print("\n--- server.log 末尾 ---")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
        except OSError:
            pass
    if args.keep:
        print(f"后端仍在运行（pid {proc.pid}），数据目录：{data_root}")
    else:
        shutil.rmtree(data_root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
