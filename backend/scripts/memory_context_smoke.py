"""Real provider smoke with only fictional records in disposable databases."""
import argparse
import json
import tempfile
from pathlib import Path

from mindos.stores.conversation_store import ConversationStore
from mindos.stores.ontology_store import OntologyStore
from mindos.stores.growth_store import GrowthStore
from mindos.zhijun.provider import build_provider, ChatRequest, TextDelta
from mindos.zhijun.routing import Router, GuardedProvider, prepare_chat, check_service, service_info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-online-synthetic", action="store_true", required=True)
    parser.parse_args()
    provider = build_provider()
    check_service(provider)
    provider._timeout = min(45, getattr(provider, "_timeout", 45))
    with tempfile.TemporaryDirectory(prefix="zhijun-memory-quality-") as folder:
        root = Path(folder)
        onto, convs, growth = OntologyStore(root / "onto.db"), ConversationStore(root / "convs.db"), GrowthStore(root / "growth.db")
        previous_growth = GrowthStore._instance
        GrowthStore._instance = growth
        try:
            growth.create_charter({"roles": ["教师"], "goals": ["今年完成一次社区阅读活动"],
                "vision": "", "principles": [], "boundaries": [], "quietDomains": [], "challengeStyle": ""})
            cid = convs.create_conversation(title="合成章程检查")["id"]
            for text in ("人生章程里还有哪些栏目没有填写？", "对，看看哪些还空着"):
                router = Router(onto, convs, cid, provider=provider)
                router.store.set_mode(cid, "online" if provider.external else "local", service_info(provider)["id"])
                router = Router(onto, convs, cid, provider=provider)
                plan = prepare_chat(router, text)
                if provider.external:
                    router.authorize(plan.preview, plan.preview["missing"])
                    plan = prepare_chat(router, text)
                result = "".join(e.text for e in GuardedProvider(router, provider, "chat", plan.refs,
                    revision=plan.preview["revision"], excluded=plan.preview["excluded"]).stream(ChatRequest(**plan.preview["request"]))
                    if isinstance(e, TextDelta))
                origin = {"service": service_info(provider)["id"] if provider.external else ""}
                convs.append_message(cid, "user", text, meta={"routingOrigin": origin, "routingSources": []})
                convs.append_message(cid, "assistant", result, meta={"routingOrigin": origin,
                    "routingSources": [s["ref"] for s in plan.preview["sources"]], "routingProvenance": plan.assembled.provenance})
                print(json.dumps({"question": text, "model": provider.model, "reply": result,
                    "memory": plan.assembled.provenance["memoryContext"]}, ensure_ascii=False), flush=True)
                assert plan.assembled.provenance["memoryContext"]["charterComplete"]
                assert all(label in result for label in ("我想成为", "长期原则", "希望怎样帮助我", "不交给 AI 决定", "暂不主动触碰")), "Response missed actual unfilled fields"
        finally:
            GrowthStore._instance = previous_growth


if __name__ == "__main__":
    main()
