"""Paired real-model check; exclusively fictional data in disposable stores."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from mindos.stores.conversation_store import ConversationStore
from mindos.stores.ontology_store import OntologyStore
from mindos.stores.growth_store import GrowthStore
from mindos.zhijun.provider import build_provider, ChatRequest, TextDelta
from mindos.zhijun.routing import Router, GuardedProvider, prepare_chat, check_service, service_info
from mindos.zhijun.memory_context import build_focus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-online-synthetic", action="store_true", required=True)
    parser.parse_args()
    provider = build_provider()
    check_service(provider)
    if not provider.external:
        raise RuntimeError("需要现有在线主模型，不能静默切换模型")
    provider._timeout = 45
    with tempfile.TemporaryDirectory(prefix="zhijun-context-quality-") as folder:
        root = Path(folder)
        onto, convs, growth = OntologyStore(root / "onto.db"), ConversationStore(root / "convs.db"), GrowthStore(root / "growth.db")
        prior = GrowthStore._instance
        GrowthStore._instance = growth
        try:
            cid = convs.create_conversation(title="合成补充背景验收")["id"]
            router = Router(onto, convs, cid, provider=provider)
            router.store.set_mode(cid, "online", service_info(provider)["id"])
            router = Router(onto, convs, cid, provider=provider)
            statements = [
                ("who", "role", "我是星桥与青禾两个产品的负责人。"),
                ("matters", "working_on", "星桥是个人知识工具，青禾是团队协作工具；两者的核心代码目前只有正式研发成员可读。"),
                ("principles", "holds_principle", "我希望给新人真实的参与机会，但先划清项目的资料接触范围。"),
                ("ways", "tends_to", "我有时跳跃太快，同伴跟不上。"),
            ]
            claims = [onto.create_claim({"section": section, "predicate": predicate, "content": text, "layer": "self_declared"},
                [{"kind": "user_edit", "quote": text}], trust_state="confirmed", trust_origin="user_created") for section, predicate, text in statements]
            for claim in claims:
                router.store.grant("global", router.resolve(router.ref("claim", claim["id"])), service_info(provider)["id"], "chat")
            original = "我招了一个还没毕业的大学生，下周才来，负责星桥与青禾的辅助工作；目前还没有接触核心资料。"
            origin = {"service": service_info(provider)["id"]}
            user = convs.append_message(cid, "user", original, meta={"routingOrigin": origin, "routingSources": []})
            focus = build_focus(original, [])
            convs.append_message(cid, "assistant", "你担心的是核心方案被复制吗？", meta={"routingOrigin": origin,
                "routingSources": [router.resolve(router.ref("message", user["id"]))[0]["ref"]], "replyTo": user["id"],
                "routingProvenance": {"contextPlan": {"focus": focus}}})
            for question in ("自然是辅助性的角色", "对，就是担心这个。怎么安排接触范围比较合适？"):
                plan = prepare_chat(router, question)
                if plan.preview["missing"]:
                    router.authorize(plan.preview, plan.preview["missing"])
                    plan = prepare_chat(router, question)
                original_request = ChatRequest(**plan.preview["request"])
                context_text = original_request.debug["contextPlan"]["system"]
                # Baseline ablation: same model, same question/nearby dialogue,
                # without the new working situation and multi-source packet.
                for label in ("recent-history-baseline", "personal-context"):
                    request = replace(original_request, max_tokens=700, temperature=0)
                    if label == "recent-history-baseline":
                        request = replace(request, system=request.system.replace(context_text, ""))
                    preview = router.prepare("chat", request, plan.refs, provider)
                    guarded = GuardedProvider(router, provider, "chat", plan.refs,
                        revision=preview["revision"], excluded=preview["excluded"])
                    answer = "".join(event.text for event in guarded.stream(request) if isinstance(event, TextDelta))
                    print(json.dumps({"case": question, "variant": label, "model": provider.model,
                        "provided": [] if label == "recent-history-baseline" else [i["title"] for i in plan.assembled.provenance["contextPlan"]["background"] + plan.assembled.provenance["contextPlan"]["evidence"]],
                        "reply": answer}, ensure_ascii=False), flush=True)
        finally:
            GrowthStore._instance = prior


if __name__ == "__main__":
    main()
