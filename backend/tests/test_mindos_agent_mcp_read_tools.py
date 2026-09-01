"""MindOS Agent 只读 MCP 工具测试（AG-02-05）。

覆盖：
- 四个只读工具注册（mindos_search / mindos_get_evidence / mindos_get_material /
  mindos_get_knowledge），readOnlyHint；
- 每个工具调用 Agent Service（mock 底层检索/生命周期/详情），不复制 REST 逻辑；
- 鉴权：凭证缺失/无效返回 ToolError；scope 不足返回 ToolError；
- 审计：每次调用写入与 REST 等价的审计（clientId + action + outcome）；
- 脱敏：工具结果不含本地路径、token、模型密钥或内部字段。

隔离环境：临时 agent DB；用创建的 token 解析 AgentPrincipal 注入网关。
"""
import os
import sys
import tempfile
import unittest
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos.agent import auth as agent_auth
from mindos.agent import evidence as agent_evidence
from mindos.agent import mcp_server as agent_mcp
from mindos.agent import store as agent_store


class AgentMCPReadToolsTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["MINDOS_AGENT_GATEWAY_ENABLED"] = "true"
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "agent.db"
        agent_store.reset_for_tests(self.db_path)
        agent_evidence.reset_for_tests()
        agent_mcp.reset_for_tests()

    def tearDown(self):
        agent_store.reset_for_tests()
        agent_evidence.reset_for_tests()
        agent_mcp.reset_for_tests()
        self._tmp.cleanup()

    # ---- 辅助 ----------------------------------------------------

    def _create_principal(self, scopes=None):
        name = "MCP客户端"
        client, token = agent_store.instance().create_client(
            name, scopes or ["mindos.read", "mindos.search"]
        )
        principal = agent_auth.AgentPrincipal(
            client_id=client["client_id"],
            name=client["name"],
            scopes=frozenset(client["scopes"]),
            workspace_id="default",
        )
        return principal, token

    def _bootstrap(self, principal):
        agent_mcp.create_agent_mcp_server(principal=principal)

    def test_tools_registered_read_only(self):
        principal, _ = self._create_principal()
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        tools = asyncio.run(server.list_tools())
        names = [t.name for t in tools]
        self.assertEqual(
            set(names),
            {"mindos_capabilities", "mindos_search", "mindos_get_evidence",
             "mindos_get_material", "mindos_get_knowledge", "mindos_answer"},
        )
        for tool in tools:
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)

    # ---- AG-04：协议层端到端（等价 MCP Client 的 tools/list + tools/call）----

    def test_mcp_protocol_tools_list_and_call(self):
        """经 MCP 协议层 list_tools / call_tool 端到端调用（等价 MCP Inspector）。"""
        principal, _ = self._create_principal()
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        tools = asyncio.run(server.list_tools())
        self.assertIn("mindos_capabilities", {t.name for t in tools})
        # tools/call：搜索
        with patch("mindos.services.search_service.search_unified",
                   return_value={"items": [], "total": 0}):
            unstructured, structured = asyncio.run(
                server.call_tool("mindos_search", {"query": "排期计划"})
            )
        self.assertTrue(unstructured)  # 非空 ContentBlock
        self.assertEqual(structured["total"], 0)

    def test_mcp_capabilities_via_protocol_matches_rest(self):
        """mindos_capabilities 经协议层调用，结果与 REST service.capabilities 一致。"""
        from mindos.agent import service as agent_service
        principal, _ = self._create_principal(
            scopes=["mindos.read", "mindos.search", "mindos.answer"]
        )
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        _, structured = asyncio.run(server.call_tool("mindos_capabilities", {}))
        self.assertEqual(structured["apiVersion"], "v1")
        self.assertEqual(structured["workspaceId"], "default")
        self.assertIn("search", structured["tools"])
        self.assertIn("getEvidence", structured["tools"])
        self.assertIn("answer", structured["tools"])
        rest_data = agent_service.capabilities(principal)
        self.assertEqual(structured["tools"], rest_data["tools"])
        self.assertEqual(structured["writeModes"], rest_data["writeModes"])
        # 未授予 mindos.read 的凭证不可调 capabilities
        read_only_missing, _ = self._create_principal(scopes=["mindos.search"])
        server2 = agent_mcp.create_agent_mcp_server(principal=read_only_missing)
        with self.assertRaises(Exception):
            asyncio.run(server2.call_tool("mindos_capabilities", {}))

    # ---- AG-04：只读 Resources（读取别名） ----------------------------

    def test_mcp_resources_registered_and_readable(self):
        """mindos://materials/{id} 与 mindos://knowledge/{id} 模板可列出并可读取。"""
        principal, _ = self._create_principal(scopes=["mindos.read"])
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        templates = asyncio.run(server.list_resource_templates())
        uris = {t.uriTemplate for t in templates}
        self.assertIn("mindos://materials/{materialId}", uris)
        self.assertIn("mindos://knowledge/{knowledgeId}", uris)
        # 读取材料 resource
        detail = {
            "materialId": "mindos_x", "fileName": "排期表.docx", "fileType": "document",
            "status": "available", "folderPath": "需求/排期",
            "createdAt": "2026-08-18T00:00:00+00:00", "materialFamilyId": "fam",
            "versionNumber": 1, "supersedesMaterialId": None, "supersededByMaterialId": None,
            "metadata": {"modifiedAt": "2026-08-18T01:00:00+00:00"},
            "summary": {"status": "pending", "text": ""}, "tags": [], "contentParts": [],
            "embeddedImages": [], "transcript": [], "previewUrl": "/api/...", "readOnly": True,
        }
        with patch("mindos.services.ingestion.detail_of", return_value=detail), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("mindos.derived.entities_of", return_value={"status": "pending", "items": []}):
            gov.return_value.archived_material_ids.return_value = set()
            contents = asyncio.run(server.read_resource("mindos://materials/mindos_x"))
        parsed = json.loads(contents[0].content)
        self.assertEqual(parsed["materialId"], "mindos_x")
        self.assertEqual(parsed["status"], "available")
        # Resource 不泄露 previewUrl（内部字段不进入读取内容）
        self.assertNotIn("previewUrl", parsed)

    def test_mcp_resource_rejects_missing_read_scope(self):
        principal, _ = self._create_principal(scopes=["mindos.search"])  # 无 mindos.read
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        with self.assertRaises(Exception):
            asyncio.run(server.read_resource("mindos://materials/mindos_x"))

    # ---- AG-04：真实 stdio transport 端到端 ---------------------------

    def test_mcp_stdio_end_to_end(self):
        """子进程经 stdio 运行 MCP server：tools/list + tools/call 正常。

        stdout 只输出 MCP 协议消息（若被日志污染，JSON-RPC 解析会失败）；凭证经
        CENTAURAI_DATABASE_DATA_ROOT + MINDOS_AGENT_MCP_TOKEN 在子进程内解析。
        """
        from mcp import ClientSession, StdioServerParameters, stdio_client

        backend_root = Path(__file__).resolve().parent.parent
        data_root = Path(self._tmp.name) / "shared_data"
        shared_db = data_root / "db" / "agent_gateway.db"
        agent_store.reset_for_tests(shared_db)
        principal, token = self._create_principal(scopes=["mindos.read", "mindos.search"])
        env = dict(os.environ)
        env["MINDOS_AGENT_MCP_TOKEN"] = token
        env["CENTAURAI_DATABASE_DATA_ROOT"] = str(data_root)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mindos.agent.mcp_server"],
            env=env,
            cwd=str(backend_root),
        )

        async def _run():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    self.assertIn("mindos_capabilities", names)
                    self.assertIn("mindos_search", names)
                    result = await session.call_tool("mindos_capabilities", {})
                    sc = result.structuredContent or {}
                    self.assertEqual(sc.get("apiVersion"), "v1")
                    self.assertIn("search", sc.get("tools", []))

        asyncio.run(_run())

    def test_mcp_protocol_error_carries_trace_id(self):
        """协议层失败调用：错误文本携带 traceId + 稳定错误码（scope 不足场景）。"""
        principal, _ = self._create_principal(scopes=["mindos.read"])  # 无 mindos.search
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        with self.assertRaises(Exception) as ctx:
            asyncio.run(server.call_tool("mindos_search", {"query": "排期计划"}))
        message = str(ctx.exception)
        self.assertIn("atr_", message)          # traceId
        self.assertIn("SCOPE_DENIED", message)  # 稳定错误码
        self.assertIn("mindos.search", message)  # 可读消息
        # 不泄露内部信息
        self.assertNotIn("D:\\", message)
        self.assertNotIn("Traceback", message)

    def test_mcp_protocol_internal_error_carries_trace_id(self):
        """协议层内部异常：错误文本携带 traceId + INTERNAL_ERROR，不泄露原始异常。"""
        principal, _ = self._create_principal()
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        with patch("mindos.services.search_service.search_unified",
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(Exception) as ctx:
                asyncio.run(server.call_tool("mindos_search", {"query": "排期计划"}))
        message = str(ctx.exception)
        self.assertIn("atr_", message)
        self.assertIn("INTERNAL_ERROR", message)
        self.assertNotIn("boom", message)

    def test_mcp_protocol_validation_error_carries_trace_id_and_audit(self):
        """输入校验失败（limit=0 / 超量 source_ids / 空 evidence_refs）纳入统一链路：
        错误携带 traceId + VALIDATION_ERROR，并写入 400 审计。"""
        principal, _ = self._create_principal()
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        cases = [
            ("mindos_search", {"query": "排期计划", "limit": 0}),
            ("mindos_search", {"query": "排期计划", "source_ids": [f"id_{i}" for i in range(21)]}),
            ("mindos_get_evidence", {"evidence_refs": []}),
        ]
        for tool, args in cases:
            with self.assertRaises(Exception) as ctx:
                asyncio.run(server.call_tool(tool, args))
            message = str(ctx.exception)
            self.assertIn("atr_", message, f"{tool} {args}: 缺少 traceId")
            self.assertIn("VALIDATION_ERROR", message, f"{tool} {args}: 缺少稳定错误码")
            self.assertNotIn("D:\\", message)
        # 三次失败均写入 400 审计
        audit = agent_store.instance().list_audit(client_id=principal.client_id)
        failed = [a for a in audit if a["status_code"] == 400]
        self.assertEqual(len(failed), 3)
        self.assertTrue(all(a["outcome"] == "error" for a in failed))

    def test_mcp_protocol_type_mismatch_carries_trace_id_and_audit(self):
        """类型不匹配（limit="bad" / source_ids="not-list" / evidence_refs="not-list" /
        question 非字符串 / materialId 非字符串）→ 统一 VALIDATION_ERROR + traceId + 400 审计。"""
        principal, _ = self._create_principal(
            scopes=["mindos.read", "mindos.search", "mindos.answer"]
        )
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        cases = [
            ("mindos_search", {"query": "排期计划", "limit": "bad"}),
            ("mindos_search", {"query": "排期计划", "source_ids": "not-list"}),
            ("mindos_get_evidence", {"evidence_refs": "not-list"}),
            ("mindos_answer", {"question": 12345}),
            ("mindos_get_material", {"material_id": {"x": 1}}),
        ]
        for tool, args in cases:
            with self.assertRaises(Exception) as ctx:
                asyncio.run(server.call_tool(tool, args))
            message = str(ctx.exception)
            self.assertIn("atr_", message, f"{tool} {args}: 缺少 traceId")
            self.assertIn("VALIDATION_ERROR", message, f"{tool} {args}: 缺少稳定错误码")
            self.assertNotIn("D:\\", message)
        audit = agent_store.instance().list_audit(client_id=principal.client_id)
        failed = [a for a in audit if a["status_code"] == 400]
        self.assertEqual(len(failed), len(cases))
        self.assertTrue(all(a["outcome"] == "error" for a in failed))

    def test_mcp_source_ids_falsy_not_silently_dropped(self):
        """source_ids 传 0 / false / "" 必须返回 VALIDATION_ERROR，不能静默变为无限定检索。"""
        principal, _ = self._create_principal(
            scopes=["mindos.read", "mindos.search", "mindos.answer"]
        )
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        cases = [
            ("mindos_search", {"query": "排期计划", "source_ids": 0}),
            ("mindos_search", {"query": "排期计划", "source_ids": False}),
            ("mindos_search", {"query": "排期计划", "source_ids": ""}),
            ("mindos_answer", {"question": "排期计划", "source_ids": 0}),
        ]
        for tool, args in cases:
            with self.assertRaises(Exception) as ctx:
                asyncio.run(server.call_tool(tool, args))
            message = str(ctx.exception)
            self.assertIn("VALIDATION_ERROR", message, f"{tool} {args}")
            self.assertIn("atr_", message)
        audit = agent_store.instance().list_audit(client_id=principal.client_id)
        failed = [a for a in audit if a["status_code"] == 400]
        self.assertEqual(len(failed), len(cases))

    def test_mcp_tools_list_exposes_input_schema(self):
        """tools/list 的 inputSchema 携带完整类型/约束（与 REST 契约一致）。"""
        principal, _ = self._create_principal()
        server = agent_mcp.create_agent_mcp_server(principal=principal)
        tools = asyncio.run(server.list_tools())
        by_name = {t.name: t for t in tools}
        search = by_name["mindos_search"].inputSchema["properties"]
        self.assertEqual(search["query"]["type"], "string")
        self.assertEqual(search["query"]["minLength"], 2)
        self.assertEqual(search["query"]["maxLength"], 500)
        self.assertEqual(search["limit"]["type"], "integer")
        self.assertEqual(search["limit"]["minimum"], 1)
        self.assertEqual(search["limit"]["maximum"], 20)
        self.assertEqual(search["source_ids"]["type"], "array")
        self.assertEqual(search["source_ids"]["maxItems"], 20)
        self.assertEqual(search["types"]["items"]["enum"], ["knowledge", "material"])
        answer = by_name["mindos_answer"].inputSchema["properties"]
        self.assertEqual(answer["question"]["type"], "string")
        self.assertEqual(answer["question"]["minLength"], 2)
        self.assertEqual(answer["question"]["maxLength"], 500)
        self.assertEqual(answer["source_ids"]["type"], "array")
        self.assertEqual(answer["source_ids"]["maxItems"], 20)
        evidence = by_name["mindos_get_evidence"].inputSchema["properties"]
        self.assertEqual(evidence["evidence_refs"]["type"], "array")
        self.assertEqual(evidence["evidence_refs"]["minItems"], 1)
        self.assertEqual(evidence["evidence_refs"]["maxItems"], 10)
        material = by_name["mindos_get_material"].inputSchema["properties"]
        self.assertEqual(material["material_id"]["type"], "string")

    def test_mcp_search_calls_agent_service(self):
        principal, _ = self._create_principal()
        self._bootstrap(principal)
        with patch("mindos.services.search_service.search_unified") as mock_unified:
            mock_unified.return_value = {
                "items": [{
                    "source_type": "material",
                    "source_id": "mindos_x",
                    "title": "排期表.docx",
                    "file_type": "document",
                    "snippet": "阶段\t日程\tP0",
                    "score": 0.8,
                    "chunk_id": "ck_internal",
                    "source_path": r"D:\watch\排期表.docx",
                    "metadata": {},
                    "locator": None,
                    "evidence_eligible": True,
                }],
                "total": 1,
            }
            result = agent_mcp.mindos_search("排期计划", types=["material"], limit=5)
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["sourceType"], "material")
        self.assertEqual(item["id"], "mindos_x")
        self.assertTrue(item["evidenceRef"].startswith("ev_"))
        # 不返回内部字段/路径
        raw = str(result)
        for banned in ("source_path", "D:\\", "ck_internal", "chunk_id"):
            self.assertNotIn(banned, raw)

    def test_mcp_get_evidence_calls_service(self):
        principal, _ = self._create_principal(scopes=["mindos.read"])
        self._bootstrap(principal)
        ref = agent_evidence.sign_evidence_ref(
            client_id=principal.client_id, source_type="material",
            source_id="mindos_x", chunk_key="ck_audio", source_path="audio.mp3",
        )
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_x"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            get_chunks.return_value = [{
                "id": "ck_audio", "source_path": "audio.mp3",
                "text": "第一阶段从三月份开始",
                "metadata": {"modality": "transcript", "start_time": 5.0, "end_time": 9.0},
            }]
            result = agent_mcp.mindos_get_evidence([ref], max_chars_per_item=200)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["sourceId"], "mindos_x")
        self.assertEqual(item["locator"], {"kind": "transcript", "start": 5.0, "end": 9.0})

    def test_mcp_get_material_calls_service(self):
        principal, _ = self._create_principal(scopes=["mindos.read"])
        self._bootstrap(principal)
        detail = {
            "materialId": "mindos_x", "fileName": "排期表.docx", "fileType": "document",
            "status": "available", "folderPath": "需求/排期",
            "createdAt": "2026-08-18T00:00:00+00:00",
            "materialFamilyId": "fam_x", "versionNumber": 1,
            "supersedesMaterialId": None, "supersededByMaterialId": None,
            "metadata": {"fileSize": 100, "modifiedAt": "2026-08-18T01:00:00+00:00"},
            "summary": {"status": "ok", "text": "排期"},
            "tags": ["MindOS"], "contentParts": [], "transcript": [],
            "previewUrl": "/api/mindos/materials/mindos_x/file", "readOnly": True,
        }
        with patch("mindos.services.ingestion.detail_of", return_value=detail), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("mindos.derived.entities_of", return_value={"status": "pending", "items": []}):
            gov.return_value.archived_material_ids.return_value = set()
            result = agent_mcp.mindos_get_material("mindos_x")
        self.assertEqual(result["materialId"], "mindos_x")
        self.assertEqual(result["status"], "available")
        self.assertNotIn("previewUrl", str(result))

    def test_mcp_get_knowledge_calls_service(self):
        principal, _ = self._create_principal(scopes=["mindos.read"])
        self._bootstrap(principal)
        view = {
            "knowledgeId": "knowledge_x", "title": "排期摘要", "body": "P0 阶段推进",
            "tags": ["MindOS"], "sources": [], "evidenceEligible": True,
            "updatedAt": "2026-08-18T00:00:00+00:00",
        }
        with patch("mindos.knowledge.knowledge_view", return_value=view):
            result = agent_mcp.mindos_get_knowledge("knowledge_x")
        self.assertEqual(result["knowledgeId"], "knowledge_x")
        self.assertEqual(result["content"], "P0 阶段推进")
        self.assertTrue(result["evidenceEligible"])

    def test_mcp_answer_calls_service(self):
        principal, _ = self._create_principal(scopes=["mindos.answer", "mindos.read"])
        self._bootstrap(principal)
        with patch("mindos.qa.answer_question") as mock_qa:
            mock_qa.return_value = {
                "status": "ANSWERED", "question": "排期计划", "answer": "P0 阶段",
                "citations": [{
                    "citationId": "m1", "sourceType": "material",
                    "materialId": "mindos_x", "knowledgeId": None,
                    "title": "排期表.docx", "snippet": "P0 排期",
                    "_chunkKey": "schedule::ck1", "_sourcePath": "schedule.docx",
                    "locator": {"kind": "table", "partId": "part_1", "tableIndex": 1},
                }],
                "correctionNotices": [],
                "meta": {"model": "internal", "retrievedCount": 1, "usedEvidenceCount": 1},
            }
            result = agent_mcp.mindos_answer("排期计划")
        self.assertEqual(result["status"], "ANSWERED")
        self.assertEqual(result["citations"][0]["id"], "mindos_x")
        self.assertTrue(result["citations"][0]["evidenceRef"].startswith("ev_"))
        self.assertEqual(result["citations"][0]["locator"]["kind"], "table")
        # 不泄露内部模型名
        self.assertNotIn("internal", str(result))
        self.assertTrue(result["traceId"].startswith("atr_"))

    def test_mcp_answer_passes_source_ids(self):
        principal, _ = self._create_principal(scopes=["mindos.answer", "mindos.read"])
        self._bootstrap(principal)
        with patch("mindos.qa.answer_question") as mock_qa:
            mock_qa.return_value = {
                "status": "ANSWERED", "question": "排期计划", "answer": "P0 阶段",
                "citations": [], "correctionNotices": [],
                "meta": {"retrievedCount": 0, "usedEvidenceCount": 0},
            }
            agent_mcp.mindos_answer("排期计划", source_ids=["mindos_x"])
        _, kwargs = mock_qa.call_args
        self.assertEqual(kwargs["source_ids"], {"mindos_x"})

    # ---- 鉴权与审计 ------------------------------------------------

    def test_tool_without_principal_raises(self):
        # 未 bootstrap（无网关单例/无凭证）
        with self.assertRaises(Exception) as ctx:
            agent_mcp.mindos_search("排期计划")
        # ToolError（mcp.server.fastmcp.exceptions.ToolError）或默认无凭证消息
        self.assertIn("凭证", str(ctx.exception))

    def test_tool_missing_scope_raises(self):
        principal, _ = self._create_principal(scopes=["mindos.read"])  # 无 mindos.search
        self._bootstrap(principal)
        with self.assertRaises(Exception) as ctx:
            agent_mcp.mindos_search("排期计划")
        self.assertIn("mindos.search", str(ctx.exception))

    def test_scope_denied_writes_audit(self):
        """scope 不足的拒绝也要写入与 REST 等价的审计（403，绑定 clientId）。"""
        principal, _ = self._create_principal(scopes=["mindos.read"])
        self._bootstrap(principal)
        with self.assertRaises(Exception):
            agent_mcp.mindos_search("排期计划")
        audit = agent_store.instance().list_audit(client_id=principal.client_id)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "mcp_search")
        self.assertEqual(audit[0]["outcome"], "error")
        self.assertEqual(audit[0]["status_code"], 403)
        self.assertEqual(audit[0]["client_id"], principal.client_id)

    def test_no_principal_writes_401_audit(self):
        """凭证缺失的拒绝也写入审计（401，clientId 为空，不泄露内部信息）。"""
        with self.assertRaises(Exception):
            agent_mcp.mindos_search("排期计划")
        audit = agent_store.instance().list_audit(client_id="")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "mcp_search")
        self.assertEqual(audit[0]["outcome"], "error")
        self.assertEqual(audit[0]["status_code"], 401)

    def test_mcp_audit_recorded_like_rest(self):
        principal, _ = self._create_principal()
        self._bootstrap(principal)
        with patch("mindos.services.search_service.search_unified",
                   return_value={"items": [], "total": 0}):
            agent_mcp.mindos_search("排期计划")
        audit = agent_store.instance().list_audit(client_id=principal.client_id)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "mcp_search")
        self.assertEqual(audit[0]["client_id"], principal.client_id)
        self.assertEqual(audit[0]["outcome"], "ok")
        self.assertEqual(audit[0]["scope"], "mindos.search,mindos.read")

    def test_mcp_result_carries_trace_id_matching_audit(self):
        """工具结果顶层携带 traceId，且与本次审计记录的 trace_id 一致。"""
        principal, _ = self._create_principal()
        self._bootstrap(principal)
        with patch("mindos.services.search_service.search_unified",
                   return_value={"items": [], "total": 0}):
            result = agent_mcp.mindos_search("排期计划")
        self.assertTrue(result["traceId"].startswith("atr_"))
        audit = agent_store.instance().list_audit(client_id=principal.client_id)
        self.assertEqual(audit[0]["trace_id"], result["traceId"])

    def test_mcp_audit_records_error(self):
        principal, _ = self._create_principal()
        self._bootstrap(principal)
        with patch("mindos.services.search_service.search_unified", side_effect=RuntimeError("boom")):
            with self.assertRaises(Exception):
                agent_mcp.mindos_search("排期计划")
        audit = agent_store.instance().list_audit(client_id=principal.client_id)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["outcome"], "error")
        self.assertEqual(audit[0]["status_code"], 500)

    def test_mcp_error_does_not_leak_internals(self):
        principal, _ = self._create_principal()
        self._bootstrap(principal)
        with patch("mindos.services.search_service.search_unified", side_effect=RuntimeError("boom")):
            try:
                agent_mcp.mindos_search("排期计划")
                self.fail("应抛出 ToolError")
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
        self.assertNotIn("boom", message)
        self.assertNotIn("Traceback", message)


if __name__ == "__main__":
    unittest.main()
