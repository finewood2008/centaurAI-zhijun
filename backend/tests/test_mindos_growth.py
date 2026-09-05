"""知君成长闭环 MVP：持久化、状态机、今日聚合与 HTTP 合同。"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from mindos import growth
import mindos.stores.growth_store as growth_store_module
from mindos.stores.growth_store import GrowthConflictError, GrowthStore, reset_for_tests
from runtime_paths import DB_ROOT, GROWTH_DB_PATH


def _charter(version_label: str = "第一版") -> dict:
    return {
        "vision": f"成为清醒而有担当的人（{version_label}）",
        "roles": ["创业者", "父亲"],
        "principles": ["长期主义", "如实面对证据"],
        "boundaries": ["不替我作出医疗决定"],
        "goals": ["建立稳定的复盘习惯"],
        "challengeStyle": "先提问，再给可逆的小建议",
        "quietDomains": ["家庭隐私"],
    }


def _decision(
    *, title: str = "是否启动新产品线", review_at: str | None = "2026-09-08T10:00:00Z"
) -> dict:
    return {
        "title": title,
        "context": "当前团队资源有限，但客户需求已经出现。",
        "options": ["现在启动", "延后三个月"],
        "choice": "先做小范围验证",
        "rationale": "控制下行风险，同时保留学习速度。",
        "confidence": 72,
        "expectedOutcome": "两周内得到三个真实客户反馈",
        "reviewAt": review_at,
        "relatedEntityIds": ["entity_product"],
        "evidenceRefs": ["ev_customer_interview"],
    }


def _outcome() -> dict:
    return {
        "result": "获得四个客户反馈，其中三个愿意继续试用。",
        "notes": "验证范围足够小，没有打断主线交付。",
        "evidenceRefs": ["ev_pilot_notes"],
    }


class GrowthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "growth.db"
        self.store = GrowthStore(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_database_path_uses_runtime_data_root(self) -> None:
        self.assertEqual(GROWTH_DB_PATH.parent, DB_ROOT)

    def test_charter_is_immutable_version_history_and_survives_restart(self) -> None:
        first = self.store.create_charter(_charter("v1"))
        second = self.store.create_charter(_charter("v2"))

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.store.current_charter()["id"], second["id"])
        self.assertEqual(
            [item["version"] for item in self.store.list_charters()], [2, 1]
        )

        reopened = GrowthStore(self.db_path)
        self.assertEqual(reopened.current_charter(), second)
        self.assertEqual(len(reopened.list_charters()), 2)

    def test_charter_api_uses_single_snapshot_contract(self) -> None:
        first = self.store.create_charter(_charter("v1"))
        second = self.store.create_charter(_charter("v2"))
        expected = self.store.charter_history()
        self.assertEqual(expected["currentCharter"], second)
        self.assertEqual(expected["versions"], [second, first])

        # API 不得重新拆成两个独立查询，否则并发新增版本时会返回不一致快照。
        with patch.object(GrowthStore, "instance", return_value=self.store), patch.object(
            self.store, "current_charter", side_effect=AssertionError("separate read")
        ), patch.object(
            self.store, "list_charters", side_effect=AssertionError("separate read")
        ):
            self.assertEqual(growth.get_charter(), {**expected, "workspace": None})

    def test_concurrent_charter_versions_do_not_reverse_timestamps(self) -> None:
        older_entered = threading.Event()
        release_older = threading.Event()

        def controlled_now() -> str:
            if threading.current_thread().name == "growth-older":
                older_entered.set()
                release_older.wait(timeout=2)
                return "2026-09-01T00:00:00Z"
            return "2026-09-01T00:00:01Z"

        def create_named(name: str) -> dict:
            threading.current_thread().name = name
            return self.store.create_charter(_charter(name))

        with patch.object(growth_store_module, "utc_now", side_effect=controlled_now):
            with ThreadPoolExecutor(max_workers=2) as pool:
                older = pool.submit(create_named, "growth-older")
                self.assertTrue(older_entered.wait(timeout=2))
                newer = pool.submit(create_named, "growth-newer")
                # 旧实现会让 newer 绕过尚未拿锁的 older 并先提交；新实现中 older
                # 已持有写锁，newer 必须等待。给调度器一个短窗口后再释放 older。
                time.sleep(0.05)
                release_older.set()
                first = older.result(timeout=2)
                second = newer.result(timeout=2)

        self.assertEqual((first["version"], first["createdAt"]), (1, "2026-09-01T00:00:00Z"))
        self.assertEqual((second["version"], second["createdAt"]), (2, "2026-09-01T00:00:01Z"))

    def test_decision_binds_current_charter_and_normalizes_utc(self) -> None:
        charter = self.store.create_charter(_charter())
        payload = _decision(review_at="2026-09-08T18:00:00+08:00")
        decision = self.store.create_decision(payload)

        self.assertEqual(decision["status"], "open")
        self.assertIsNone(decision["review"])
        self.assertEqual(decision["charterId"], charter["id"])
        self.assertEqual(decision["charterVersion"], 1)
        self.assertEqual(decision["reviewAt"], "2026-09-08T10:00:00Z")

        self.store.create_charter(_charter("v2"))
        reopened = GrowthStore(self.db_path)
        historical = reopened.get_decision(decision["id"])
        self.assertEqual(historical["charterId"], charter["id"])
        self.assertEqual(historical["charterVersion"], 1)

    def test_store_rejects_naive_review_time(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_decision(_decision(review_at="2026-09-08T10:00:00"))
        self.assertEqual(self.store.list_decisions(), [])

    def test_state_machine_and_restart_persistence(self) -> None:
        decision = self.store.create_decision(_decision())
        updated = self.store.record_outcome(decision["id"], _outcome())
        self.assertEqual(updated["status"], "outcome_recorded")
        self.assertEqual(updated["outcome"]["result"], _outcome()["result"])
        self.assertIsNone(updated["review"])

        with self.assertRaises(GrowthConflictError):
            self.store.record_outcome(decision["id"], _outcome())

        result = self.store.create_review(
            {
                "decisionId": decision["id"],
                "reflection": "小实验比一次性下注更适合不确定阶段。",
                "lessons": ["先取得真实反馈，再扩大投入"],
                "nextAction": "把试用反馈整理成进入条件",
            }
        )
        self.assertEqual(result["decision"]["status"], "reviewed")
        self.assertEqual(result["review"]["decisionId"], decision["id"])
        self.assertEqual(result["decision"]["review"], result["review"])

        reopened = GrowthStore(self.db_path)
        persisted = reopened.get_decision(decision["id"])
        self.assertEqual(persisted["status"], "reviewed")
        self.assertEqual(persisted["outcome"]["evidenceRefs"], ["ev_pilot_notes"])
        self.assertEqual(persisted["review"], result["review"])
        self.assertEqual(
            reopened.list_decisions("reviewed")[0]["review"], result["review"]
        )
        self.assertEqual(reopened.latest_review(), result["review"])

    def test_review_failure_does_not_partially_advance_decision(self) -> None:
        decision = self.store.create_decision(_decision())
        with self.assertRaises(GrowthConflictError):
            self.store.create_review(
                {
                    "decisionId": decision["id"],
                    "reflection": "尚未记录结果",
                    "lessons": ["不能跳过结果"],
                    "nextAction": "先记录结果",
                }
            )
        self.assertEqual(self.store.get_decision(decision["id"])["status"], "open")
        self.assertIsNone(self.store.latest_review())

    def test_today_prioritizes_and_caps_three_items_but_stats_are_full(self) -> None:
        self.store.create_charter(_charter())
        overdue_one = self.store.create_decision(
            _decision(title="逾期一", review_at="2026-08-20T00:00:00Z")
        )
        overdue_two = self.store.create_decision(
            _decision(title="逾期二", review_at="2026-08-25T00:00:00Z")
        )
        pending = self.store.create_decision(
            _decision(title="待复盘", review_at="2026-10-01T00:00:00Z")
        )
        self.store.record_outcome(pending["id"], _outcome())
        self.store.create_decision(
            _decision(title="即将到期一", review_at="2026-09-02T00:00:00Z")
        )
        self.store.create_decision(
            _decision(title="即将到期二", review_at="2026-09-03T00:00:00Z")
        )

        today = self.store.today(now=datetime(2026, 9, 1, tzinfo=timezone.utc))
        visible = today["dueDecisions"] + today["pendingReviews"]
        self.assertEqual(len(visible), 3)
        self.assertEqual(len(today["todayItems"]), 3)
        self.assertEqual(
            [item["urgency"] for item in today["todayItems"]],
            ["overdue", "overdue", "pending_review"],
        )
        self.assertEqual(
            [item["type"] for item in today["todayItems"]],
            ["decision_due", "decision_due", "pending_review"],
        )
        self.assertEqual(
            {item["id"] for item in visible},
            {overdue_one["id"], overdue_two["id"], pending["id"]},
        )
        self.assertTrue(
            all(item["dueState"] == "overdue" for item in today["dueDecisions"])
        )
        self.assertEqual(today["stats"]["totalDecisions"], 5)
        self.assertEqual(today["stats"]["overdueDecisions"], 2)
        self.assertEqual(today["stats"]["dueSoonDecisions"], 2)
        self.assertEqual(today["stats"]["pendingReviews"], 1)

    def test_today_compares_mixed_precision_utc_as_instants(self) -> None:
        at_now = self.store.create_decision(
            _decision(title="同秒已到期", review_at="2026-09-01T00:00:00Z")
        )
        at_horizon = self.store.create_decision(
            _decision(title="精确七天边界", review_at="2026-09-08T00:00:00Z")
        )

        today = self.store.today(
            now=datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
        )
        by_id = {item["id"]: item for item in today["dueDecisions"]}
        self.assertEqual(by_id[at_now["id"]]["dueState"], "overdue")
        self.assertEqual(by_id[at_horizon["id"]]["dueState"], "due_soon")
        self.assertEqual(today["stats"]["overdueDecisions"], 1)
        self.assertEqual(today["stats"]["dueSoonDecisions"], 1)


class GrowthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        reset_for_tests(Path(self._tmp.name) / "growth-api.db")

        def _csrf_guard(x_requested_by: str | None = Header(default=None)) -> None:
            if x_requested_by != "centaur-vdb":
                raise HTTPException(403, "missing csrf")

        growth.configure_write_guard(_csrf_guard)
        app = FastAPI()
        app.include_router(growth.router)
        self.client = TestClient(app)
        self.headers = {"X-Requested-By": "centaur-vdb"}

    def tearDown(self) -> None:
        self.client.close()
        reset_for_tests()
        self._tmp.cleanup()

    def test_charter_and_decision_http_contract(self) -> None:
        blocked = self.client.post(
            "/api/mindos/growth/charter", json=_charter()
        )
        self.assertEqual(blocked.status_code, 403)

        created = self.client.post(
            "/api/mindos/growth/charter", json=_charter(), headers=self.headers
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["version"], 1)
        charter_get = self.client.get("/api/mindos/growth/charter")
        self.assertEqual(charter_get.json()["currentCharter"]["version"], 1)
        self.assertEqual(len(charter_get.json()["versions"]), 1)

        decision = self.client.post(
            "/api/mindos/growth/decisions",
            json=_decision(),
            headers=self.headers,
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        body = decision.json()
        self.assertEqual(body["status"], "open")
        self.assertEqual(body["charterVersion"], 1)
        self.assertIsNone(body["review"])

        listed = self.client.get(
            "/api/mindos/growth/decisions", params={"status": "open"}
        )
        self.assertEqual(listed.json()["total"], 1)
        self.assertEqual(listed.json()["items"][0]["id"], body["id"])

    def test_validation_404_and_409(self) -> None:
        invalid = _decision()
        invalid["confidence"] = 101
        response = self.client.post(
            "/api/mindos/growth/decisions", json=invalid, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)

        boolean_confidence = _decision()
        boolean_confidence["confidence"] = True
        response = self.client.post(
            "/api/mindos/growth/decisions",
            json=boolean_confidence,
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)

        naive = _decision(review_at="2026-09-08T10:00:00")
        response = self.client.post(
            "/api/mindos/growth/decisions", json=naive, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)

        missing = self.client.post(
            "/api/mindos/growth/decisions/decision_missing/outcome",
            json=_outcome(),
            headers=self.headers,
        )
        self.assertEqual(missing.status_code, 404)

        decision = self.client.post(
            "/api/mindos/growth/decisions",
            json=_decision(),
            headers=self.headers,
        ).json()
        review_too_early = self.client.post(
            "/api/mindos/growth/reviews",
            json={
                "decisionId": decision["id"],
                "reflection": "反思",
                "lessons": ["经验"],
                "nextAction": "下一步",
            },
            headers=self.headers,
        )
        self.assertEqual(review_too_early.status_code, 409)

        first = self.client.post(
            f"/api/mindos/growth/decisions/{decision['id']}/outcome",
            json=_outcome(),
            headers=self.headers,
        )
        self.assertEqual(first.status_code, 200)
        duplicate = self.client.post(
            f"/api/mindos/growth/decisions/{decision['id']}/outcome",
            json=_outcome(),
            headers=self.headers,
        )
        self.assertEqual(duplicate.status_code, 409)

        bad_status = self.client.get(
            "/api/mindos/growth/decisions", params={"status": "closed"}
        )
        self.assertEqual(bad_status.status_code, 400)

    def test_review_and_today_contract(self) -> None:
        self.client.post(
            "/api/mindos/growth/charter", json=_charter(), headers=self.headers
        )
        decision = self.client.post(
            "/api/mindos/growth/decisions",
            json=_decision(review_at="2026-08-01T00:00:00Z"),
            headers=self.headers,
        ).json()
        self.client.post(
            f"/api/mindos/growth/decisions/{decision['id']}/outcome",
            json=_outcome(),
            headers=self.headers,
        )
        review = self.client.post(
            "/api/mindos/growth/reviews",
            json={
                "decisionId": decision["id"],
                "reflection": "验证了小步实验的价值",
                "lessons": ["先验证，再扩大"],
                "nextAction": "更新产品进入标准",
            },
            headers=self.headers,
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["decision"]["status"], "reviewed")
        self.assertEqual(
            review.json()["decision"]["review"], review.json()["review"]
        )

        refreshed = self.client.get(
            "/api/mindos/growth/decisions", params={"status": "reviewed"}
        ).json()["items"][0]
        self.assertEqual(refreshed["review"], review.json()["review"])

        today = self.client.get("/api/mindos/growth/today")
        self.assertEqual(today.status_code, 200)
        body = today.json()
        self.assertEqual(body["currentCharter"]["version"], 1)
        self.assertEqual(body["latestReview"]["decisionId"], decision["id"])
        self.assertEqual(body["stats"]["reviewedDecisions"], 1)


class GrowthEvidenceReceiptTests(unittest.TestCase):
    def refs(self, count=8):
        return [{"kind": "charter_clause", "id": "charter_synthetic:clause_" + str(i), "version": "a" * 64} for i in range(count)]

    def validate(self, value):
        return growth.DecisionCreate(**{**_decision(), "evidenceRefs": [value]}).evidenceRefs

    def test_structured_lineage_over_500_chars_is_preserved_exactly(self):
        value = json.dumps({"kind": "routing", "routingSources": self.refs()}, ensure_ascii=False)
        self.assertGreater(len(value), 500)
        self.assertEqual(self.validate(value), [value])

    def test_helper_receipt_preserves_sources_and_basis(self):
        value = json.dumps({"kind": "helper_lineage", "conversationId": "conversation-synthetic",
            "task": "decision_suggestions", "revision": 3, "possibleAssistance": True,
            "charterBasis": {"charterId": "charter_synthetic", "version": 2, "scope": "global", "clauseIds": ["guidance"]},
            "routingSources": self.refs()}, ensure_ascii=False)
        self.assertEqual(self.validate(value), [value])

    def test_plain_evidence_still_has_500_character_limit(self):
        self.assertEqual(self.validate("x" * 500), ["x" * 500])
        with self.assertRaises(ValueError): self.validate("x" * 501)
        with self.assertRaises(ValueError): self.validate(json.dumps({"kind": "not-routing", "data": "x" * 501}))

    def test_structured_shape_cannot_smuggle_free_text_or_malformed_refs(self):
        invalid = [
            {"kind": "routing", "routingSources": self.refs(), "hiddenBody": "x" * 600},
            {"kind": "routing", "routingSources": "not-a-list"},
            {"kind": "routing", "routingSources": [{"kind": "unknown", "id": "x"}]},
            {"kind": "routing", "routingSources": [{"kind": "message", "id": "x", "version": ["bad"]}]},
            {"kind": "routing", "routingSources": [{"kind": "material", "id": "x", "materialVersion": False}]},
            {"kind": "helper_lineage", "routingSources": [], "charterBasis": [{}]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.validate(json.dumps(value))

    def test_per_receipt_and_aggregate_limits_reject_without_truncation(self):
        with self.assertRaises(ValueError):
            self.validate(json.dumps({"kind": "routing", "routingSources": self.refs(1025)}))
        large = [{"kind": "message", "id": "x" * 250 + str(i), "version": "a" * 128} for i in range(400)]
        with self.assertRaises(ValueError):
            self.validate(json.dumps({"kind": "routing", "routingSources": large}))
        receipts = [json.dumps({"kind": "routing", "routingSources": [{"kind": "message", "id": str(offset + i)} for i in range(600)]}) for offset in (0, 600)]
        with self.assertRaises(ValueError):
            growth.DecisionCreate(**{**_decision(), "evidenceRefs": receipts})


class GrowthServerWiringTests(unittest.TestCase):
    def test_server_applies_access_gate_and_local_write_guard(self) -> None:
        """防止路由可用但绕过现有 MindOS 会话与本机写保护。"""
        server_source = (
            Path(__file__).resolve().parents[1] / "server.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "mindos_growth.configure_write_guard(require_local)", server_source
        )
        self.assertIn(
            "app.include_router(mindos_growth.router, "
            "dependencies=_MINDOS_WEB_DEPENDENCIES)",
            server_source,
        )


if __name__ == "__main__":
    unittest.main()
