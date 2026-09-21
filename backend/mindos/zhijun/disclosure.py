"""袒露的分级：普通 / 重话 / 危机。PRD V2 的 5.5「接住与边界」。

三条硬约束，都不依赖模型自觉：

1. **重话那一轮不抽取**。把袒露当场变成一条带状态标签的「理解」，会让人下次不敢说。
   用户继续说下去、或明确要求「记下来」，才在后续轮次进入。
2. **危机对话完全不抽取**，`explicit_memory_request` 也不能豁免。它止步于 L0 对话记录：
   不生成理解、不进画像、不计入任何指标。
3. **判定是纯本地的**：只有正则与词表，不调模型、不联网、不落盘。

误判的代价不对称，两个方向的倾斜因此相反：

- 重话：漏判会把一次袒露变成档案，误判只是少记一条理解。**向「判成重话」倾斜。**
- 危机：漏判会把一个人最脆弱的一句话当素材收走，误判只是少记一条并多给一次求助渠道。
  **向「判成危机」倾斜**，但要挡住中文里大量的「累死了」这类夸张用法。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ORDINARY = "ordinary"
HEAVY = "heavy"
CRISIS = "crisis"

_RESOURCES_PATH = Path(__file__).with_name("crisis_resources.json")

# ---------------------------------------------------------------------------
# 危机
# ---------------------------------------------------------------------------

# 明确的自我伤害表达。这些词组在中文里没有常见的良性用法，命中即判危机。
_CRISIS_STRONG = re.compile(
    r"自杀|自尽|轻生|自残|割腕|了结自己|一了百了|结束(?:自己的)?生命"
    r"|伤害自己|不想再醒(?:过)?来|从这个世界消失"
)

# 需要看上下文的表达。中文里「想死」「活不下去」常被用作夸张，必须排除。
_CRISIS_CONTEXTUAL = re.compile(r"想死|不想活|活不下去|撑不下去|熬不下去|活着没意思|没有活下去的理由")

# 夸张用法的前缀：累得想死、困得要死、忙得撑不下去……这些不是危机。
_HYPERBOLE_PREFIX = re.compile(r"(?:累|困|饿|渴|热|冷|气|笑|吓|忙|疼|痛|烦|无聊|挤|吵|香|甜|美|帅)(?:得|死)")

# 固定搭配里的「死」「活」，与自我伤害无关。
_DEAD_IDIOM = re.compile(r"死心|死磕|死线|死角|死循环|死锁|该死|死记|死板|至死不渝|不见不散|要死不活地")


def is_crisis(text: str) -> bool:
    """是否属于自我伤害或严重心理困扰的表达。"""
    body = (text or "").strip()
    if not body:
        return False
    if _CRISIS_STRONG.search(body):
        return True
    for match in _CRISIS_CONTEXTUAL.finditer(body):
        window = body[max(0, match.start() - 8):match.start()]
        if _HYPERBOLE_PREFIX.search(window):
            continue          # 累得想死 —— 夸张，不是危机
        if _DEAD_IDIOM.search(body[max(0, match.start() - 2):match.end() + 2]):
            continue          # 死心 / 死磕 —— 固定搭配
        return True
    return False


# ---------------------------------------------------------------------------
# 重话
# ---------------------------------------------------------------------------

# 强信号：命中任意一条即判重话。这些话本身就在说「我很少说这个」。
_HEAVY_STRONG = re.compile(
    # 隐秘性——最可靠的一类，用户自己在标注这句话的分量
    r"没(?:跟|对|和)(?:任何人|谁|别人|外人)说过|从来没(?:跟人|对人|和人)?说过|没人知道"
    r"|第一次(?:跟人|对人|和人)说|不好意思说|说出来(?:有点)?(?:丢人|难堪|矫情)"
    r"|憋了(?:很久|好久|很多年)|压在心里"
    # 强自我否定
    r"|像个(?:骗子|废物|失败者|笑话)|我是(?:个)?(?:失败者|废物|骗子)|我不配|我很没用|我恨我自己"
    # 重大关系创伤
    r"|离婚|出轨|背叛了我|被(?:抛弃|背叛)|(?:爸|妈|父亲|母亲|爷爷|奶奶|外公|外婆)[^，。！？；\n]{0,5}(?:走了|去世|不在了)"
)

# 弱信号：需要至少两类同时出现。单独一条太容易误伤普通抱怨。
_HEAVY_WEAK = {
    "孤独": re.compile(r"孤独|孤单|没有人(?:能|可以)?(?:懂|理解|商量)|一个人扛|没人可以说"),
    "撑不住": re.compile(r"撑不住|扛不住|快崩溃|受不了了|到极限了|喘不过气"),
    "恐惧": re.compile(r"我很(?:害怕|恐惧)|我怕(?:的是|自己)|不敢面对|一想到.{0,10}就慌"),
    "悔恨": re.compile(r"我后悔|对不起(?:他|她|你|team|团队|我的)|是我的错|我毁了"),
    "无解": re.compile(r"不知道(?:该)?怎么办|没有出路|看不到头|走投无路"),
    "失眠": re.compile(r"睡不着|失眠|一直在想这件事|翻来覆去"),
}


def is_heavy(text: str) -> bool:
    """是否属于「重的话」：值得先接住、这一轮不要抽取的袒露。"""
    body = (text or "").strip()
    if not body:
        return False
    if _HEAVY_STRONG.search(body):
        return True
    hits = sum(1 for pattern in _HEAVY_WEAK.values() if pattern.search(body))
    return hits >= 2


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------


def classify(text: str) -> str:
    """把一轮用户原话分成 ordinary / heavy / crisis 三档。危机优先。"""
    if is_crisis(text):
        return CRISIS
    if is_heavy(text):
        return HEAVY
    return ORDINARY


def extraction_blocked(text: str, *, explicit_request: bool = False) -> bool:
    """这一轮是否禁止进入抽取。

    ``explicit_request`` 是用户明确说了「记下来」。它能豁免重话，**不能豁免危机**：
    危机内容属于 L0 对话记录，止步于此。
    """
    kind = classify(text)
    if kind == CRISIS:
        return True
    return kind == HEAVY and not explicit_request


def crisis_resources(locale: str = "zh-CN") -> dict | None:
    """当地求助渠道。读不到就返回 None —— 宁可不给，也不编造号码。"""
    try:
        catalog = json.loads(_RESOURCES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    entry = catalog.get(locale) or catalog.get(catalog.get("default", ""))
    if not entry or not entry.get("lines"):
        return None
    return entry
