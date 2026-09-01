"""P11 固定验收样例种子脚本。

创建三张 MindOS 知识卡片，用于端到端验收治理待办：
- duplicate-notes.md      主题：MindOS 本地知识管理（A）
- duplicate-notes-2.md    主题：与 A 高度相似（触发"疑似重复"候选）
- outdated-plan.md        引用一个不存在的来源材料（触发"可能过时"候选）

运行：python backend/_seed_governance_samples.py [--reset]
--reset 额外删除 governance.db，使治理候选可重新扫描生成（验收复现用）。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wiki_store

NOW = datetime.now(timezone.utc).isoformat()

COMMON_BODY = """# MindOS 本地优先知识管理

MindOS 是一个本地优先的个人多模态知识库，核心设计原则是"先本地、后同步"。

## 核心原则

- 原材料始终保持只读，任何派生内容（摘要、标签、关联、卡片）都先作为候选保存。
- AI 只能产生候选建议，最终采纳与修改必须由用户确认。
- 所有知识操作都以可审计的方式进行，便于回溯与治理。

## 数据分层

- 原材料：上传的原始文档、图片，只读。
- 知识成品：由用户确认的可编辑卡片，来源可追踪。
- 治理待办：AI 发现的重复、过时、待确认关联候选，供人工仲裁。
"""


def card(rel_path, title, tags, body, extra_frontmatter=""):
    meta = (
        "---\n"
        f'title: {__import__("json").dumps(title, ensure_ascii=False)}\n'
        f"type: note\n"
        f"tags: {__import__('json').dumps(tags, ensure_ascii=False)}\n"
        "maturity: seedling\n"
        "mindos_card: true\n"
        f"created_at: {__import__('json').dumps(NOW)}\n"
        f"updated_at: {__import__('json').dumps(NOW)}\n"
        + extra_frontmatter
        + "---\n\n"
        + body
    )
    wiki_store.write_page(rel_path, meta, source_agent="mindos")
    print(f"created {rel_path}")


def main():
    card(
        "Resources/duplicate-notes.md",
        "验收样例：重复笔记 A",
        ["验收", "重复样例"],
        COMMON_BODY,
    )
    # 内容高度相似（复用同一正文），触发"疑似重复"
    card(
        "Resources/duplicate-notes-2.md",
        "验收样例：重复笔记 B",
        ["验收", "重复样例"],
        COMMON_BODY,
    )
    # 引用不存在的来源材料，触发"可能过时"
    card(
        "Resources/outdated-plan.md",
        "验收样例：过时计划",
        ["验收", "过时样例"],
        "# 验收样例：过时计划\n\n本卡片登记了一份已不可用的来源资料，用于验证治理扫描对"
        "\u201c可能过时\u201d候选的识别。该来源已不在原材料库中。\n",
        extra_frontmatter='mindos_source_material_ids: ["mindos_seed_outdated_material"]\n',
    )
    print("seed complete")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        from runtime_paths import GOVERNANCE_DB_PATH
        if GOVERNANCE_DB_PATH.exists():
            GOVERNANCE_DB_PATH.unlink()
            print("governance.db removed (候选可重新扫描生成)")
    main()
