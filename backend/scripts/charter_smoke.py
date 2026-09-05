"""Explicit opt-in real-model checks using fictional messages and temporary stores."""
import argparse
import json
import tempfile
import time
from pathlib import Path

from mindos.stores.conversation_store import ConversationStore
from mindos.stores.ontology_store import OntologyStore
from mindos.stores.growth_store import GrowthStore
from mindos.zhijun.charter import DraftRequest, generate
from mindos.zhijun.provider import build_provider
from mindos.zhijun.routing import Router, check_service, service_info

CASES = [
    ("安排的工作与愿望", "我是小林，目前负责的项目是公司安排，不代表我的个人追求。我希望今年留出更多时间陪伴家人。"),
    ("原则与协作边界", "诚实是我一直认同的原则。希望你先听我说，再帮我列出选项，不要替我做决定。不要主动提起家庭关系。"),
    ("临时状态不是原则", "今天太累，所以我推掉了一次加班。长期想成为什么样的人，我还没想清楚。"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-online-synthetic", action="store_true", required=True)
    args = parser.parse_args()
    if not args.allow_online_synthetic:
        parser.error("Only explicitly authorized synthetic checks")
    provider = build_provider()
    check_service(provider)
    provider._timeout = min(45, getattr(provider, "_timeout", 45))
    with tempfile.TemporaryDirectory(prefix="zhijun-charter-quality-") as folder:
        root = Path(folder)
        onto, convs, growth = OntologyStore(root / "onto.db"), ConversationStore(root / "convs.db"), GrowthStore(root / "growth.db")
        previous_growth = GrowthStore._instance
        GrowthStore._instance = growth
        try:
            for index, (label, text) in enumerate(CASES):
                cid = convs.create_conversation(title="合成章程质量检查")["id"]
                convs.append_message(cid, "user", text, meta={"routingSources": []})
                router = Router(onto, convs, cid, provider=provider)
                if provider.external:
                    router.store.set_mode(cid, "online", service_info(provider)["id"])
                    router = Router(onto, convs, cid, provider=provider)
                req = DraftRequest(requestId=f"synthetic-quality-{index}")
                preview = generate(router, req.model_copy(update={"previewOnly": True}))["routePreview"]
                if provider.external:
                    router.authorize(preview, preview["missing"])
                    preview = generate(router, req.model_copy(update={"previewOnly": True}))["routePreview"]
                started = time.monotonic()
                try:
                    draft = generate(router, req.model_copy(update={"routeRevision": preview["revision"]}))["draft"]
                    print(json.dumps({"case": label, "model": provider.model, "seconds": round(time.monotonic() - started, 2),
                        "fields": {k: {"text": v["text"], "quote": v["quote"]} for k, v in draft["fields"].items()}, "formalCharter": growth.current_charter()}, ensure_ascii=False), flush=True)
                except Exception as exc:
                    print(json.dumps({"case": label, "error": str(exc)}, ensure_ascii=False), flush=True)
        finally:
            GrowthStore._instance = previous_growth


if __name__ == "__main__": main()
