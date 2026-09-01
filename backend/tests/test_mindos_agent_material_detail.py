"""MindOS Agent 材料详情接口测试（AG-02-04）。

覆盖：
- GET /v1/agent/materials/{id} 正常返回（状态/版本/summary/tags/实体/contentParts/transcript）；
- contentParts 投影为真实定位（表格行/列/页码），图片不返回 artifact key；
- 归档/回收/不存在/失败 → 统一 404；
- 脱敏：不返回 source_path / previewUrl / 全文 text / artifact key；
- 鉴权：需要 mindos.read；审计记录 material 类型。

隔离环境：临时 agent DB + 独立 FastAPI app；mock detail_of / 实体 / 生命周期。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos.agent import admin as agent_admin
from mindos.agent import router as agent_router
from mindos.agent import store as agent_store


def _make_app() -> FastAPI:
    app = FastAPI()

    def _allow():
        return None

    agent_router.install(app)
    app.include_router(agent_router.router)
    agent_admin.configure_admin_guards(_allow, _allow)
    app.include_router(agent_admin.admin_router)
    return app


def _detail_payload(**overrides) -> dict:
    payload = {
        "materialId": "mindos_x",
        "fileName": "MindOSV1.0 开发排期管理表.docx",
        "fileType": "document",
        "status": "available",
        "jobId": "job_x",
        "errorMessage": None,
        "folder": "需求/排期",
        "folderId": 1,
        "createdAt": "2026-08-18T00:00:00+00:00",
        "materialFamilyId": "mindos_family_x",
        "versionNumber": 1,
        "supersedesMaterialId": None,
        "supersededByMaterialId": None,
        "versionNote": None,
        "recycled": False,
        "folderPath": "需求/排期",
        "previewUrl": "/api/mindos/materials/mindos_x/file",
        "metadata": {"fileSize": 12345, "modifiedAt": "2026-08-18T01:00:00+00:00"},
        "summary": {"status": "ok", "text": "排期表", "generatedAt": "2026-08-18T00:00:00+00:00"},
        "topic": "",
        "text": "阶段\t日程\t核心目标\nP0 排期",
        "textLabel": "解析文本",
        "transcript": [{"start": 1.0, "end": 2.0, "text": "转写片段"}],
        "contentParts": [
            {
                "partId": "mindos_x::table::1", "partType": "table", "ordinal": 1,
                "text": "阶段\t日程\nP0\tQ1", "location": {"table": 1, "page": 2},
                "rows": [["阶段", "日程"], ["P0", "Q1"]],
            },
            {
                "partId": "mindos_x::paragraph::2", "partType": "paragraph", "ordinal": 2,
                "text": "说明段落", "location": {"page": 2},
            },
        ],
        "tableCount": 1,
        "embeddedImages": [
            {
                "partId": "mindos_x::image::7",
                "previewUrl": "/api/mindos/materials/mindos_x/parts/mindos_x::image::7/file",
                "location": {"page": 3},
                "ocrText": "架构图说明",
                "ocrStatus": "ok",
                "mime": "image/png",
                "width": 1280,
                "height": 720,
            }
        ],
        "excerpt": "阶段\t日程",
        "tags": ["MindOS"],
        "readOnly": True,
    }
    payload.update(overrides)
    return payload


class AgentMaterialDetailTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["MINDOS_AGENT_GATEWAY_ENABLED"] = "true"
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "agent.db"
        agent_store.reset_for_tests(self.db_path)
        self.app = _make_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        agent_store.reset_for_tests()
        self._tmp.cleanup()

    def _create(self, name="材料详情客户端", scopes=None) -> str:
        res = self.client.post(
            "/api/agent/clients",
            json={"name": name, "scopes": scopes or ["mindos.read"]},
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def _bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _get_material(self, token: str, material_id: str = "mindos_x"):
        return self.client.get(
            f"/v1/agent/materials/{material_id}", headers=self._bearer(token)
        )

    def test_material_detail_shape(self):
        with patch("mindos.services.ingestion.detail_of", return_value=_detail_payload()), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("mindos.derived.entities_of") as entities:
            gov.return_value.archived_material_ids.return_value = set()
            entities.return_value = {
                "status": "ok",
                "items": [{"type": "organization", "name": "MindOS"}],
                "source": None,
                "generatedAt": "2026-08-18T00:00:00+00:00",
            }
            token = self._create()
            res = self._get_material(token)
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()["data"]
            self.assertEqual(data["materialId"], "mindos_x")
            self.assertEqual(data["fileName"], "MindOSV1.0 开发排期管理表.docx")
            self.assertEqual(data["fileType"], "document")
            self.assertEqual(data["status"], "available")
            self.assertEqual(data["folderPath"], "需求/排期")
            self.assertEqual(data["updatedAt"], "2026-08-18T01:00:00+00:00")
            self.assertEqual(data["version"]["materialFamilyId"], "mindos_family_x")
            self.assertEqual(data["version"]["versionNumber"], 1)
            self.assertEqual(data["version"]["supersedesMaterialId"], None)
            self.assertEqual(data["summary"]["status"], "ok")
            self.assertEqual(data["summary"]["text"], "排期表")
            self.assertEqual(data["tags"], ["MindOS"])
            self.assertEqual(data["entities"], [{"type": "organization", "text": "MindOS"}])
            self.assertEqual(data["transcript"], [{"start": 1.0, "end": 2.0, "text": "转写片段"}])
            self.assertTrue(data["readOnly"])
            # contentParts：表格投影为真实定位（页码/表格索引/行列范围）
            table_part = data["contentParts"][0]
            self.assertEqual(table_part["partType"], "table")
            self.assertEqual(table_part["rows"], [["阶段", "日程"], ["P0", "Q1"]])
            self.assertEqual(table_part["location"]["kind"], "table")
            self.assertEqual(table_part["location"]["tableIndex"], 1)
            self.assertEqual(table_part["location"]["page"], 2)
            self.assertEqual(table_part["location"]["rowStart"], 1)
            self.assertEqual(table_part["location"]["rowEnd"], 2)
            self.assertEqual(table_part["location"]["columnEnd"], 2)
            para = data["contentParts"][1]
            self.assertEqual(para["location"]["kind"], "paragraph")
            self.assertEqual(para["location"]["page"], 2)
            # embeddedImages：安全投影（partId / OCR 状态 / 尺寸 / 定位），不返回路径。
            images = data["embeddedImages"]
            self.assertEqual(len(images), 1)
            img = images[0]
            self.assertEqual(img["partId"], "mindos_x::image::7")
            self.assertEqual(img["partType"], "image")
            self.assertEqual(img["ocrStatus"], "ok")
            self.assertEqual(img["width"], 1280)
            self.assertEqual(img["height"], 720)
            self.assertEqual(img["location"]["kind"], "embedded_image")
            self.assertEqual(img["location"]["page"], 3)
            self.assertNotIn("previewUrl", img)
            self.assertNotIn("ocrText", img)

    def test_material_detail_pending_entities_not_fabricated(self):
        with patch("mindos.services.ingestion.detail_of", return_value=_detail_payload()), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("mindos.derived.entities_of") as entities:
            gov.return_value.archived_material_ids.return_value = set()
            entities.return_value = {"status": "pending", "items": [], "source": None, "generatedAt": None}
            token = self._create()
            res = self._get_material(token)
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["data"]["entities"], [])

    def test_material_detail_no_internal_fields(self):
        with patch("mindos.services.ingestion.detail_of", return_value=_detail_payload()), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("mindos.derived.entities_of", return_value={"status": "pending", "items": []}):
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            res = self._get_material(token)
            self.assertEqual(res.status_code, 200, res.text)
            raw = res.text
            for banned in ("source_path", "previewUrl", "/api/mindos", "D:\\",
                           "watch_folder", "artifact_key", "jobId"):
                self.assertNotIn(banned, raw, f"响应泄露禁止字段: {banned}")

    def test_material_detail_archived_is_404(self):
        with patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov:
            gov.return_value.archived_material_ids.return_value = {"mindos_x"}
            token = self._create()
            res = self._get_material(token)
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_material_detail_recycled_is_404(self):
        with patch("mindos.services.ingestion.recycled_material_ids", return_value={"mindos_x"}), \
             patch("mindos.stores.governance_store.instance") as gov:
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            res = self._get_material(token)
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_material_detail_not_found_is_404(self):
        with patch("mindos.services.ingestion.detail_of", return_value=None), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov:
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            res = self._get_material(token, "mindos_missing")
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_material_detail_processing_forces_empty_content(self):
        """处理中材料即使 detail_of 残留旧派生内容，也只返回元数据/状态/摘要状态。"""
        with patch("mindos.services.ingestion.detail_of",
                   return_value=_detail_payload(status="processing")), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("mindos.derived.entities_of", return_value={"status": "pending", "items": []}):
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            res = self._get_material(token)
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()["data"]
            self.assertEqual(data["status"], "processing")
            # 残留 contentParts / embeddedImages / transcript 被强制清空
            self.assertEqual(data["contentParts"], [])
            self.assertEqual(data["embeddedImages"], [])
            self.assertEqual(data["transcript"], [])
            # 元数据与摘要状态仍返回
            self.assertEqual(data["materialId"], "mindos_x")
            self.assertEqual(data["status"], "processing")
            self.assertEqual(data["summary"]["status"], "ok")

    def test_material_detail_failed_forces_empty_content(self):
        with patch("mindos.services.ingestion.detail_of",
                   return_value=_detail_payload(status="failed")), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("mindos.derived.entities_of", return_value={"status": "pending", "items": []}):
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            res = self._get_material(token)
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()["data"]
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["contentParts"], [])
            self.assertEqual(data["embeddedImages"], [])
            self.assertEqual(data["transcript"], [])

    def test_material_detail_requires_read_scope(self):
        token = self._create("无只读", scopes=["mindos.search"])
        res = self._get_material(token)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")

    def test_material_detail_audit_recorded(self):
        with patch("mindos.services.ingestion.detail_of", return_value=_detail_payload()), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("mindos.derived.entities_of", return_value={"status": "pending", "items": []}):
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            supplied = "atr_material-detail-222"
            self.client.get(
                "/v1/agent/materials/mindos_x",
                headers={**self._bearer(token), "X-Request-Id": supplied},
            )
            audit = self.client.get("/api/agent/audit", params={"traceId": supplied}).json()
            self.assertEqual(audit["total"], 1)
            self.assertEqual(audit["items"][0]["action"], "material_detail")
            self.assertEqual(audit["items"][0]["resource_type"], "material")
            self.assertEqual(audit["items"][0]["resource_id"], "mindos_x")

    def test_capabilities_declares_get_material(self):
        token = self._create("声明", scopes=["mindos.read"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("getMaterial", res.json()["data"]["tools"])


if __name__ == "__main__":
    unittest.main()
