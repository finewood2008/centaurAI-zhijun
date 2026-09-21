"""Pure query planning and fixed read-only material tool dispatch."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from mindos.zhijun import retrieval_tools as tools


def message(ident, role, content):
    return {"id": ident, "role": role, "content": content}


def error_code(error):
    return error.value.detail["code"]


def test_contract_example_rewrites_to_one_independent_query():
    plan = tools.plan_search(
        "它有哪些核心功能？",
        [message("u-mindos", "user", "MindOS 是什么？")],
    )
    assert plan["query"] == "MindOS有哪些核心功能？"
    assert "是什么" not in plan["query"]
    assert "\n" not in plan["query"]
    assert plan["historyUsed"] == ["u-mindos"]


def test_explicit_search_prefix_is_removed_only_from_dependent_followup():
    history = [message("u-mindos", "user", "MindOS 是什么？")]
    plan = tools.plan_search("请检索它有哪些核心功能？", history)
    assert plan["query"] == "MindOS有哪些核心功能？"
    assert plan["historyUsed"] == ["u-mindos"]

    independent = "请检索 MindOS 的核心功能"
    plan = tools.plan_search(independent, history)
    assert plan["query"] == independent
    assert plan["historyUsed"] == []


def test_prefixed_unresolved_material_reference_fails_closed():
    with pytest.raises(HTTPException) as error:
        tools.plan_search("请检索这份文件的验收要求")
    assert error_code(error) == "RAG_QUERY_CLARIFICATION_REQUIRED"

    scoped = tools.plan_search(
        "请检索这份文件的验收要求", material_ids=["material-01"]
    )
    assert scoped["query"] == "所选文件的验收要求"
    assert scoped["historyUsed"] == []


def test_requirements_followup_uses_explicit_user_identifier_not_whole_sentence():
    history = [
        message("u-old", "user", "旧项目的预算安排"),
        message("a-made-up", "assistant", "我们正在讨论并不存在的火星迁移项目"),
        message("u-topic", "user", "为 AMD-A2A-248 制定外网连接发布方案"),
        message("a-question", "assistant", "要不要同时更换所有模型？"),
    ]
    plan = tools.plan_search("验收 requirements 呢？", history)
    assert plan == {
        "query": "AMD-A2A-248的验收 requirements是什么？",
        "materialIds": None,
        "topK": 5,
        "scopeLabel": "授权资料库",
        "historyUsed": ["u-topic"],
    }


def test_named_project_and_short_followup_are_rewritten_without_history_prose():
    plan = tools.plan_search(
        "再具体一点",
        [message("u-plan", "user", "星桥发布计划")],
    )
    assert plan["query"] == "详细说明星桥发布计划"
    assert plan["historyUsed"] == ["u-plan"]


def test_topic_switch_independent_question_and_identifier_do_not_pull_history():
    history = [message("u1", "user", "星桥项目的采购风险"),
               message("a1", "assistant", "虚构实体银河公司可能参与")]
    for current in (
        "换个话题，解释一下量子纠缠",
        "北京明天为什么降温？",
        "AMD-A2A-248 的验收要求是什么？",
    ):
        plan = tools.plan_search(current, history)
        assert plan["query"] == current
        assert plan["historyUsed"] == []


def test_assistant_fabrication_cannot_resolve_a_reference():
    history = [message("a1", "assistant", "负责人是从未由用户提到的张三")]
    with pytest.raises(HTTPException) as error:
        tools.plan_search("它的验收要求呢？", history)
    assert error_code(error) == "RAG_QUERY_CLARIFICATION_REQUIRED"
    assert "材料或主题" in error.value.detail["detail"]


def test_missing_or_ambiguous_user_subject_fails_closed():
    for history in (
        [],
        [message("u1", "user", "MindOS 和 CentaurOS 有什么区别？")],
        [message("u0", "user", "星桥项目"),
         message("u1", "user", "比较飞舟项目和银河项目")],
    ):
        with pytest.raises(HTTPException) as error:
            tools.plan_search("它的负责人是谁？", history)
        assert error_code(error) == "RAG_QUERY_CLARIFICATION_REQUIRED"


def test_topic_switch_with_unresolved_reference_does_not_reopen_old_topic():
    with pytest.raises(HTTPException) as error:
        tools.plan_search(
            "换个话题，这个有什么风险？",
            [message("u1", "user", "星桥项目")],
        )
    assert error_code(error) == "RAG_QUERY_CLARIFICATION_REQUIRED"


def test_history_is_bounded_and_never_partially_copied():
    history = [message("long", "user", "甲" * 401), message("short", "user", "星桥发布计划")]
    plan = tools.plan_search("再具体一点", history)
    assert plan["query"] == "详细说明星桥发布计划"
    assert plan["historyUsed"] == ["short"]


def test_selected_material_reference_is_rewritten_without_history():
    plan = tools.plan_search(
        "比较这两份验收记录",
        material_ids=["material-01", "material-02"],
    )
    assert plan["query"] == "比较所选验收记录"
    assert plan["historyUsed"] == []
    scoped = tools.plan_search("验收要求呢？", material_ids=["material-01"])
    assert scoped["query"] == "所选材料的验收要求是什么？"
    assert scoped["historyUsed"] == []
    with pytest.raises(HTTPException) as error:
        tools.plan_search("这份材料的风险呢？", material_ids=["m1", "m2"])
    assert error_code(error) == "RAG_QUERY_CLARIFICATION_REQUIRED"


def test_current_query_is_never_tail_truncated_and_overlong_is_422():
    content = "甲" * 999 + "？"
    assert tools.plan_search(content)["query"] == content
    with pytest.raises(HTTPException) as error:
        tools.plan_search("甲" * 1001)
    assert error.value.status_code == 422
    assert error_code(error) == "RAG_QUERY_TOO_LONG"


def test_material_scope_none_and_explicit_selection_are_distinct():
    assert tools.plan_search("发布验收")["materialIds"] is None
    selected = tools.plan_search("发布验收", material_ids=["mat-1", "mat:2"])
    assert selected["materialIds"] == ["mat-1", "mat:2"]
    assert selected["scopeLabel"] == "所选资料"
    with pytest.raises(HTTPException) as error:
        tools.plan_search("发布验收", material_ids=[])
    assert error_code(error) == "RAG_MATERIAL_SCOPE_EMPTY"


@pytest.mark.parametrize("ids,code", [
    (["mat-1", "mat-1"], "RAG_MATERIAL_SCOPE_DUPLICATE"),
    ([f"mat-{index}" for index in range(101)], "RAG_MATERIAL_SCOPE_TOO_LARGE"),
    (["../../etc/passwd"], "RAG_MATERIAL_ID_INVALID"),
])
def test_material_scope_rejects_duplicate_unbounded_or_unsafe_ids(ids, code):
    with pytest.raises(HTTPException) as error:
        tools.plan_search("发布验收", material_ids=ids)
    assert error_code(error) == code


@pytest.mark.parametrize("arguments", [
    {"query": "发布验收", "url": "https://example.invalid"},
    {"query": "发布验收", "topK": 20},
    {"query": "https://example.invalid/private"},
    {"query": "api_key=sk-not-allowed"},
    {"materialIds": ["mat-1"]},
])
def test_fixed_tool_validator_rejects_unknown_fields_urls_and_credentials(arguments):
    with pytest.raises(HTTPException) as error:
        tools.validate_search_arguments(arguments)
    assert error.value.status_code == 422


def test_tool_contract_is_read_only_closed_and_not_native_model_dispatch():
    definition = tools.SEARCH_MATERIALS_TOOL
    assert definition["name"] == "search_materials"
    assert definition["readOnly"] is True
    assert definition["inputSchema"]["additionalProperties"] is False
    assert definition["inputSchema"]["properties"]["materialIds"]["maxItems"] == 100
    assert tools.NATIVE_MODEL_TOOLS_SUPPORTED is False
    assert tools.native_model_tools_supported is False


def test_execute_search_calls_only_fixed_client_with_review_context():
    plan = tools.plan_search("星桥项目怎么验收？", material_ids=["mat-1"])
    sentinel = [{"evidenceRef": "erv2_" + "a" * 32}]
    with patch("mindos.data_agent_rag.search_materials", return_value=sentinel) as search:
        assert tools.execute_search(plan, "conversation-1:turn-2") == sentinel
    search.assert_called_once_with(
        "星桥项目怎么验收？",
        ["mat-1"],
        "conversation-1:turn-2",
        top_k=5,
        require_review=True,
        review_context={"query": "星桥项目怎么验收？", "scopeLabel": "所选资料"},
    )


def test_execute_search_revalidates_plan_and_rejects_unknown_parameters():
    plan = tools.plan_search("发布验收")
    plan["url"] = "https://example.invalid"
    with patch("mindos.data_agent_rag.search_materials") as search, pytest.raises(HTTPException):
        tools.execute_search(plan, "turn-1")
    search.assert_not_called()


def test_offline_cases_are_declared_expectations_not_quality_claims():
    path = Path(__file__).parents[2] / "testdata" / "retrieval-query-cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    assert cases["description"].startswith("离线规则用例")
    for case in cases["cases"]:
        expected = case["expected"]
        if "errorCode" in expected:
            with pytest.raises(HTTPException) as error:
                tools.plan_search(
                    case["content"], case.get("allowedHistory", ()), case.get("materialIds")
                )
            assert error_code(error) == expected["errorCode"]
            continue
        plan = tools.plan_search(
            case["content"], case.get("allowedHistory", ()), case.get("materialIds")
        )
        if "query" in expected:
            assert plan["query"] == expected["query"]
        for text in expected.get("contains", []):
            assert text in plan["query"]
        assert plan["historyUsed"] == expected.get("historyUsed", [])
        assert plan["scopeLabel"] == expected.get("scopeLabel", "授权资料库")


@pytest.mark.parametrize("content", [
    "大模型自我认知微调项目复现新版.docx",
    "请帮我梳理大模型自我认知微调项目的复现步骤",
    "复现大模型自我认知微调项目",
    "阅读《实验记录 v2.pdf》",
])
def test_completed_raw_material_filename_and_named_project_open_review_search(content):
    from mindos.zhijun.context_plan import _needs_implicit_material_search
    assert _needs_implicit_material_search(content, [], {})
    assert tools.plan_search(content)["query"] == content
    assert tools.plan_search(content)["materialIds"] is None


@pytest.mark.parametrize("content", ["你好", "帮我想一个新项目", "我今天完成了课程项目", "如何创建一个项目"])
def test_ordinary_chat_does_not_search_the_material_library(content):
    assert not tools.should_search_materials(content)


def test_filename_followup_keeps_exact_extension_and_version_without_card_lookup():
    filename = "大模型自我认知微调项目复现新版.docx"
    history = [message("u-file", "user", "请帮我阅读" + filename)]
    plan = tools.plan_search("帮我梳理这个项目的复现步骤", history)
    assert plan["query"] == "梳理" + filename + "的复现步骤"
    assert plan["historyUsed"] == ["u-file"]
    with pytest.raises(HTTPException) as error:
        tools.plan_search("帮我复现这个项目")
    assert error_code(error) == "RAG_QUERY_CLARIFICATION_REQUIRED"


def test_filename_followup_never_guesses_between_two_versions():
    history = [message("u-files", "user", "阅读《实验 v1.docx》和《实验 v2.docx》")]
    with pytest.raises(HTTPException) as error:
        tools.plan_search("总结这个文件", history)
    assert error_code(error) == "RAG_QUERY_CLARIFICATION_REQUIRED"
