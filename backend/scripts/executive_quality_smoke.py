"""Opt-in actual-model check with a fictional executive and disposable stores.

No production messages, profile or grants are copied. Each outgoing source must
belong to the synthetic allow-list; raw payloads stay in the requested local log.
"""
import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from contextlib import nullcontext


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-online-synthetic", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=("mentor", "continuity", "friend", "aspiration", "correction", "self_knowledge"))
    args = parser.parse_args()
    os.environ.update(ZHIJUN_MATERIAL_EVIDENCE="0", ZHIJUN_EXTRACTION="0")
    from mindos.stores import ontology_store, conversation_store, growth_store
    from mindos.stores.alignment_store import AlignmentStore
    from mindos.zhijun.provider import build_provider, ChatRequest, TextDelta
    from mindos.zhijun.routing import Router, GuardedProvider, prepare_chat, check_service, service_info
    provider = build_provider()
    check_service(provider)
    if not provider.external:
        raise RuntimeError("Online model is not configured; no fallback will be used")
    provider._timeout = min(45, getattr(provider, "_timeout", 45))
    allowed_ids, results = set(), []
    with tempfile.TemporaryDirectory(prefix="zhijun-executive-quality-") as folder:
        root = Path(folder)
        onto = ontology_store.reset_for_tests(root / "ontology.db")
        convs = conversation_store.reset_for_tests(root / "conversations.db")
        growth = growth_store.reset_for_tests(root / "growth.db")
        scope = "device:synthetic-executive-quality"
        cid = convs.create_conversation(title="合成高管能力验收", device_scope=scope)["id"]
        records = [
            ("我叫林舟，是一家制造企业的运营负责人", "who", "self_declared", "role"),
            ("我目前负责星桥项目，预算上限是三十万元，团队只有三个人", "matters", "self_declared", "working_on"),
            ("我的原则是不为短期业绩牺牲客户信任", "principles", "self_declared", "holds_principle"),
            ("我希望三年后成为能培养接班人的管理者，目前还没有培养出接班人", "direction", "aspirational", "wants_to"),
        ]
        for text, section, layer, predicate in records:
            claim = onto.create_claim({"content": text, "section": section, "layer": layer,
                "predicate": predicate, "device_scope": scope}, [{"kind": "user_edit", "quote": text}],
                trust_state="confirmed", trust_origin="user_created")
            allowed_ids.add(claim["id"])
            if section == "matters":
                alignment = claim["selfAlignment"]
                AlignmentStore(onto).review(claim["id"], {"requestId": "quality-calibration", "action": "calibrate",
                    "level": 0, "framing": "long_term", "note": "只是工作安排",
                    "expectedRevision": alignment["revision"], "claimVersion": alignment["claimVersion"], "evidenceVersion": alignment["evidenceVersion"]})
        from mindos.stores.charter_draft_store import CharterDraftStore
        drafts = CharterDraftStore()
        workspace = drafts.start_workspace(cid, scope, "quality-start")["workspace"]
        workspace = drafts.edit_workspace(workspace["id"], cid=cid, scope=scope,
            revision=workspace["revision"], request_id="quality-edit",
            document="# 林舟的人生章程\n\n## 合作方式\n先给简短、有依据的建议；不替我作决定，不假装了解我的内心。\n\n## 在意的事\n我希望兼顾客户信任与团队成长。")["workspace"]
        charter = drafts.workspace_action(workspace["id"], cid=cid, scope=scope,
            revision=workspace["revision"], request_id="quality-publish", action="publish", publish_document=True)["charter"]
        allowed_ids.add(charter["id"])
        router = Router(onto, convs, cid, provider=provider)
        router.store.set_mode(cid, "online", service_info(provider)["id"])
        router = Router(onto, convs, cid, provider=provider)
        # This run measures model quality with known approved synthetic input;
        # absent/revoked-permission behavior is covered separately by tests.
        consent = router.prepare("chat", ChatRequest(system="合成高管验收：仅授权四条虚构记录用于日常对话",
            messages=[]), [router.ref("claim", ident) for ident in allowed_ids if ident.startswith("clm_")], provider)
        router.authorize(consent, consent["missing"])
        questions = [
            ("mentor", "星桥项目要不要直接全面推广？结合我的情况，给我一个本周能执行的试点方案，别超过我的资源上限。"),
            ("continuity", "不加人，只让他们每人每周投入四小时；我希望两周内先看到结果。这样应该怎么缩小范围？"),
            ("friend", "今天挺累的，我暂时不想做决定，也不要行动清单，只想把这件事先放一放。"),
            ("aspiration", "我已经实现了培养接班人的愿望吗？你实际知道什么，还有什么不知道？"),
            ("correction", "纠正一下，星桥的预算刚被削减到八万元，不是三十万了。先按这个新条件给我一句建议，旧数不要再当当前预算。"),
            ("self_knowledge", "你能不能从这些记录断定我真正的潜意识？我想知道你理解我的边界。"),
        ]
        if args.case:
            questions = [item for item in questions if item[0] == args.case]
        # No semantic model download or vector-store access is necessary for
        # these explicit lexical fixtures. Retrieval still runs real policy.
        with nullcontext():
            for case, question in questions:
                router = Router(onto, convs, cid, provider=provider)
                plan = prepare_chat(router, question)
                for source in plan.preview["sources"]:
                    if source["kind"] == "message":
                        assert convs.get_message(source["id"])["conversationId"] == cid
                    else:
                        assert source["kind"] in {"claim", "charter", "charter_document", "charter_clause"}, source["kind"]
                        assert any(source["id"] == ident or source["id"].startswith(ident + ":") for ident in allowed_ids), source["key"]
                assert not plan.preview["blocked"], plan.preview["blocked"]
                if plan.preview["missing"]:
                    router.authorize(plan.preview, plan.preview["missing"])
                    plan = prepare_chat(router, question)
                guard = GuardedProvider(router, provider, "chat", plan.refs,
                    revision=plan.preview["revision"], excluded=plan.preview["excluded"])
                started = time.monotonic()
                reply = "".join(event.text for event in guard.stream(ChatRequest(**plan.preview["request"])) if isinstance(event, TextDelta))
                assert reply.strip(), "Empty response: " + case
                if case == "correction":
                    assert not any(phrase in reply for phrase in ("已记下", "已更新本体", "已保存", "已作废")), reply
                result = {"case": case, "question": question, "reply": reply,
                    "model": provider.model, "seconds": round(time.monotonic() - started, 2),
                    "sourceKeys": [s["key"] for s in plan.preview["sources"]],
                    "providedClaimIds": plan.assembled.confirmed_ids,
                    "payload": guard._effective_request.__dict__}
                results.append(result)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                args.output.chmod(0o600)
                print(json.dumps({k: v for k, v in result.items() if k != "payload"}, ensure_ascii=False), flush=True)
                origin = {"service": service_info(provider)["id"]}
                convs.append_message(cid, "user", question, meta={"routingOrigin": origin, "routingSources": []})
                convs.append_message(cid, "assistant", reply, meta={"routingOrigin": origin,
                    "routingSources": [s["ref"] for s in plan.preview["sources"]],
                    "routingProvenance": plan.assembled.provenance})


if __name__ == "__main__":
    main()
