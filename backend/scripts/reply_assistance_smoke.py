"""Real-provider reply quality check with fixed fictional inputs, no user records.

All routing settings and receipts use temporary stores. Reads only the existing
provider configuration. Online calls require --allow-online-synthetic.
"""
import argparse
import json
import tempfile
import time
from pathlib import Path

from mindos.chat_imports import local_provider
from mindos.stores.conversation_store import ConversationStore
from mindos.stores.ontology_store import OntologyStore
from mindos.zhijun.provider import build_provider
from mindos.zhijun.reply_assistance import build_request, candidate_texts
from mindos.zhijun.routing import Router, GuardedProvider, check_service, service_info

CASES = [
    ("身份补充", "我是小林，一名独立开发者。", "好，先用这一句介绍你，以后可以再调整。"),
    ("具体取舍", "这次我一个人做产品验证，时间和预算都有限。", "你更在意尽快验证，还是让第一版足够完整？"),
    ("模糊感受", "最近做项目有些犹豫，但我还说不清原因。", "这份犹豫更接近对方向没把握，还是不确定自己能投入多少？"),
    ("拒绝回答", "这件事我暂时不想详细说。", "没关系，不必解释；我们可以先放下这个话题。"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-online-synthetic", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--case", choices=[row[0] for row in CASES])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not args.local and not args.allow_online_synthetic:
        parser.error("Online synthetic checks require --allow-online-synthetic")
    provider = local_provider(num_ctx=4096, timeout=45) if args.local else build_provider()
    check_service(provider)
    if provider.external == args.local:
        parser.error("Requested provider unavailable; no silent replacement")
    provider._timeout = min(45, getattr(provider, "_timeout", 45))
    results = []
    with tempfile.TemporaryDirectory(prefix="zhijun-reply-quality-") as root:
        onto = OntologyStore(Path(root) / "onto.db")
        convs = ConversationStore(Path(root) / "convs.db")
        for label, user, assistant in CASES:
            if args.case and label != args.case:
                continue
            cid = convs.create_conversation(title="合成回复质量检查")["id"]
            router = Router(onto, convs, cid, provider=provider)
            if provider.external:
                router.store.set_mode(cid, "online", service_info(provider)["id"])
                router = Router(onto, convs, cid, provider=provider)
            req = build_request([{"role": "user", "content": user}, {"role": "assistant", "content": assistant}])
            preview = router.prepare("reply_assistance", req, [], provider)
            guarded = GuardedProvider(router, provider, "reply_assistance", [], revision=preview["revision"])
            row = {"case": label, "model": provider.model, "external": provider.external}
            started = time.monotonic()
            try:
                raw = guarded.complete_json(req)
                row["raw"] = raw
                row["candidates"] = candidate_texts(raw)
                row["state"] = "passed_format"
            except Exception as exc:
                row.update(state="error", errorCode=getattr(exc, "code", type(exc).__name__))
            row["elapsedSeconds"] = round(time.monotonic() - started, 2)
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            Path(args.output).write_text(json.dumps({"syntheticOnly": True, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
