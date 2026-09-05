"""Explicit opt-in quality smoke. Only hard-coded fictional text leaves device.

Run from backend with PYTHONPATH=. and the configured data root in the environment.
All conversations, grants and audit rows are in a disposable database; no real
claims, messages or documents are read. The existing provider config is read only.
"""
import argparse
import json
import tempfile
import time
from pathlib import Path

from mindos.chat_imports import local_provider, service_info
from mindos.stores.ontology_store import OntologyStore
from mindos.stores.conversation_store import ConversationStore
from mindos.zhijun.provider import build_provider, ChatRequest, TextDelta
from mindos.zhijun.routing import Router, GuardedProvider, check_service
from mindos.zhijun import persona, alignment

CASES = [
    ("模糊意图", [{"role": "user", "content": "虚构案例：最近总觉得工作哪里不对劲，但又说不上来。我应该怎么办？"}],
     "先澄清不适的情境，不断言用户真正想辞职"),
    ("多重约束", [{"role": "user", "content": "虚构案例：一周内验证产品想法，预算500元，只有我一人、每天1小时，不能开发完整应用。请给一个可执行方案。"}],
     "遵守7小时、500元、单人、不完整开发，提出可核对结果"),
    ("跨轮追问", [{"role": "user", "content": "虚构案例：我周五要讲熟悉的主题，有三天准备，但担心现场提问。"},
                 {"role": "assistant", "content": "我们可以把准备演讲与现场问答分开。"},
                 {"role": "user", "content": "那如果有人问我不知道的呢？"}],
     "正确绑定现场问答，允许坦诚未知，不泛泛劝放松"),
    ("文件结合个人处境", [{"role": "user", "content": "以下仅为合成资料的已授权文字片段，并非完整文件：[虚构方案.txt，第2段] 首版需在两周内上线，研发只有1人，不做支付与多端。个人处境：这是工作安排，并非我最认同的方向，我最近精力有限。为何我迟迟不愿推进？请区分证据与推测，并引用片段。"}],
     "引用局部范围，考虑外部安排与精力；不声称完整阅读或确定内心"),
    ("愿望与行为", [{"role": "user", "content": "虚构案例：我过去常回避冲突；我现在希望更直接表达需要。昨天我还是没开口。这是不是说明我其实不想改变？"}],
     "区分过去行为、当前愿望、一次例外；不把一次行为等同真实意愿"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-online-synthetic", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not args.allow_online_synthetic:
        parser.error("Requires explicit --allow-online-synthetic consent for fixed fictional cases")
    online = build_provider()
    check_service(online)
    if not online.external:
        parser.error("Configured online channel unavailable; no automatic replacement")
    online._timeout = min(45, online._timeout)
    local = local_provider(num_ctx=4096, timeout=45)
    results = []
    with tempfile.TemporaryDirectory(prefix="zhijun-dual-smoke-") as root:
        onto = OntologyStore(Path(root) / "onto.db")
        convs = ConversationStore(Path(root) / "convs.db")
        for label, messages, criteria in CASES:
            for provider in (local, online):
                cid = convs.create_conversation(title="仅合成能力检查")["id"]
                router = Router(onto, convs, cid, provider=provider)
                if provider.external:
                    router.store.set_mode(cid, "online", service_info(provider)["id"])
                    router = Router(onto, convs, cid, provider=provider)
                req = ChatRequest(system=persona.PERSONA_CORE + "\n" + alignment.INSTRUCTION +
                                  "\n所有案例都是虚构测试资料。用户本轮明确陈述的事实不要误称为你的推测；方案先核对预算、总工时与禁止事项。请在150字以内回答。", messages=messages,
                                  max_tokens=350, temperature=0, effort="low")
                preview = router.prepare("chat", req, [], provider)
                guard = GuardedProvider(router, provider, "chat", [], revision=preview["revision"])
                started = time.monotonic()
                row = {"case": label, "model": provider.model, "external": provider.external, "criteria": criteria}
                try:
                    row["answer"] = "".join(e.text for e in guard.stream(req) if isinstance(e, TextDelta))
                    row["state"] = "complete" if row["answer"].strip() else "empty"
                except Exception as exc:
                    row.update(state="error", errorCode=getattr(exc, "code", type(exc).__name__))
                row["elapsedSeconds"] = round(time.monotonic() - started, 2)
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                # Preserve partial evidence if a later model call is interrupted.
                Path(args.output).write_text(json.dumps({"syntheticOnly": True, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
