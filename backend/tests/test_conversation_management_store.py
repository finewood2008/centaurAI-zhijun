"""Conversation organization/search persistence; all data is synthetic and local."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mindos.stores.chat_import_store import ChatImportStore
from mindos.stores.conversation_store import (
    ConversationConflictError, ConversationError, ConversationNotFoundError, ConversationStore,
)


class ConversationManagementStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "conversations.db"
        self.store = ConversationStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, title="合成对话", *, time="2026-01-01T01:00:00Z", scope="device-a", **kwargs):
        with patch("mindos.stores.conversation_store.utc_now", return_value=time):
            return self.store.create_conversation(title=title, device_scope=scope, **kwargs)

    def edit(self, conversation, **kwargs):
        latest = self.store.get_conversation(conversation["id"])
        return self.store.update_metadata(conversation["id"], expected_revision=latest["metadataRevision"], **kwargs)

    def test_new_defaults_and_incremental_migration_preserve_existing_data(self):
        fresh = self.create()
        self.assertIsNone(fresh["pinnedAt"])
        self.assertEqual(fresh["metadataRevision"], 0)
        legacy_path = Path(self.tmp.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as db:
            db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,mode TEXT NOT NULL,"
                       "status TEXT NOT NULL,device_scope TEXT NOT NULL,message_count INTEGER NOT NULL,"
                       "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_message_at TEXT)")
            db.execute("INSERT INTO conversations VALUES('legacy','旧标题','review','archived','device-a',0,'2025-01-01','2025-02-01',NULL)")
        migrated = ConversationStore(legacy_path)
        row = migrated.get_conversation("legacy")
        self.assertEqual((row["title"], row["status"], row["createdAt"], row["updatedAt"]),
                         ("旧标题", "archived", "2025-01-01", "2025-02-01"))
        self.assertIsNone(row["pinnedAt"])
        self.assertEqual(row["metadataRevision"], 0)
        self.assertEqual(ConversationStore(legacy_path).get_conversation("legacy"), row)

    def test_metadata_updates_do_not_change_activity(self):
        conv = self.create()
        self.store.append_message(conv["id"], "user", "合成正文")
        before = self.store.get_conversation(conv["id"])
        renamed = self.edit(conv, title="  新标题  ")
        pinned = self.edit(conv, pinned=True)
        archived = self.edit(conv, status="archived")
        self.assertEqual(renamed["title"], "新标题")
        self.assertTrue(pinned["pinnedAt"].endswith("Z"))
        self.assertEqual(archived["pinnedAt"], pinned["pinnedAt"])
        self.assertEqual(archived["metadataRevision"], 3)
        for row in (renamed, pinned, archived):
            for key in ("createdAt", "updatedAt", "lastMessageAt", "messageCount"):
                self.assertEqual(row[key], before[key])

    def test_metadata_validation_never_truncates(self):
        conv = self.create()
        for payload in ({}, {"title": "  "}, {"title": "长" * 81}, {"title": 42},
                        {"status": "deleted"}, {"pinned": "true"}):
            with self.subTest(payload=payload), self.assertRaises(ConversationError):
                self.store.update_metadata(conv["id"], expected_revision=0, **payload)
        for revision in (-1, True, "0"):
            with self.subTest(revision=revision), self.assertRaises(ConversationError):
                self.store.update_metadata(conv["id"], expected_revision=revision, title="新标题")
        self.assertEqual(self.store.get_conversation(conv["id"]), conv)
        self.assertEqual(len(self.edit(conv, title="长" * 80)["title"]), 80)

    def test_archive_restore_pin_unpin_retries_are_idempotent(self):
        conv = self.create()
        revision = 0
        for payload in ({"pinned": True}, {"status": "archived"}, {"status": "active"}, {"pinned": False}):
            with self.subTest(payload=payload):
                first = self.store.update_metadata(conv["id"], expected_revision=revision, **payload)
                repeated = self.store.update_metadata(conv["id"], expected_revision=revision, **payload)
                no_op = self.store.update_metadata(conv["id"], expected_revision=revision + 1, **payload)
                self.assertEqual(first, repeated)
                self.assertEqual(first, no_op)
                self.assertEqual(first["metadataRevision"], revision + 1)
                revision += 1

    def test_conflicting_stale_payload_cannot_overwrite_newer_update(self):
        conv = self.create()
        first = self.edit(conv, title="更新后的名称", pinned=True)
        other_instance = ConversationStore(self.path)
        with self.assertRaises(ConversationConflictError):
            other_instance.update_metadata(conv["id"], expected_revision=0, title="过期名称")
        with self.assertRaises(ConversationConflictError):
            other_instance.update_metadata(conv["id"], expected_revision=0, pinned=False)
        with self.assertRaises(ConversationConflictError):
            other_instance.update_metadata(conv["id"], expected_revision=99, title=first["title"])
        self.assertEqual(other_instance.get_conversation(conv["id"]), first)

    def test_metadata_device_scope_is_enforced_inside_transaction(self):
        conv = self.create()
        with self.assertRaises(ConversationNotFoundError):
            self.store.update_metadata(conv["id"], expected_revision=0, title="越界", device_scope="device-b")
        self.assertEqual(self.store.get_conversation(conv["id"]), conv)
        with self.assertRaises(ConversationNotFoundError):
            self.store.update_metadata("missing", expected_revision=0, pinned=True)

    def test_concurrent_connections_cannot_both_commit_the_same_revision(self):
        conv = self.create()
        other_instance = ConversationStore(self.path)
        def rename(store, title):
            try:
                return store.update_metadata(conv["id"], expected_revision=0, title=title)
            except ConversationConflictError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(rename, self.store, "第一位编辑"), pool.submit(rename, other_instance, "第二位编辑")]
            results = [attempt.result() for attempt in attempts]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.get_conversation(conv["id"])["metadataRevision"], 1)

    def test_management_pins_do_not_change_legacy_chronology(self):
        older = self.create("旧", time="2026-01-01T01:00:00Z")
        newer = self.create("新", time="2026-01-02T01:00:00Z")
        self.edit(older, pinned=True)
        managed = self.store.query_conversations()["items"]
        chronological = self.store.list_conversations()
        self.assertEqual([x["id"] for x in managed], [older["id"], newer["id"]])
        self.assertEqual([x["id"] for x in chronological], [newer["id"], older["id"]])

    def test_pins_order_by_pin_time_and_archive_preserves_pin_without_ranking_it(self):
        first = self.create("一", time="2026-01-01T01:00:00Z")
        second = self.create("二", time="2026-01-02T01:00:00Z")
        with patch("mindos.stores.conversation_store.utc_now", return_value="2026-02-01T00:00:00Z"):
            second_pin = self.edit(second, pinned=True)
        with patch("mindos.stores.conversation_store.utc_now", return_value="2026-02-02T00:00:00Z"):
            first_pin = self.edit(first, pinned=True)
        self.assertEqual([x["id"] for x in self.store.query_conversations()["items"]], [first["id"], second["id"]])
        self.edit(first, status="archived")
        self.edit(second, status="archived")
        archived = self.store.query_conversations(status="archived")["items"]
        self.assertEqual([x["id"] for x in archived], [second["id"], first["id"]])
        self.assertEqual([x["pinnedAt"] for x in archived], [second_pin["pinnedAt"], first_pin["pinnedAt"]])

    def test_search_queries_full_database_and_returns_paginated_totals(self):
        target = self.create("旧标题", time="2020-01-01T00:00:00Z")
        message = self.store.append_message(target["id"], "user", "合成 Needle 正文")
        for index in range(55):
            self.create(f"较新对话 {index}", time="2027-01-01T00:00:00Z")
        self.assertNotIn(target["id"], [x["id"] for x in self.store.list_conversations(limit=50)])
        result = self.store.query_conversations(q="needle", device_scope="device-a")
        self.assertEqual((result["total"], result["hasMore"]), (1, False))
        self.assertEqual(result["items"][0]["id"], target["id"])
        self.assertEqual(result["items"][0]["searchMatch"]["messageId"], message["id"])
        page = self.store.query_conversations(limit=20, offset=20)
        self.assertEqual((len(page["items"]), page["total"], page["hasMore"]), (20, 56, True))
        last = self.store.query_conversations(limit=20, offset=40)
        self.assertEqual((len(last["items"]), last["total"], last["hasMore"]), (16, 56, False))

    def test_search_title_first_then_activity_and_latest_matching_message(self):
        title = self.create("Needle 标题", time="2020-01-01T00:00:00Z")
        body = self.create("正文匹配")
        self.store.append_message(body["id"], "user", "旧 needle 正文")
        latest = self.store.append_message(body["id"], "assistant", "新 NEEDLE 正文")
        self.edit(body, pinned=True)
        result = self.store.query_conversations(q="nEeDlE")
        self.assertEqual([x["id"] for x in result["items"]], [title["id"], body["id"]])
        self.assertEqual(result["items"][0]["searchMatch"], {"field": "title", "messageId": None, "snippet": "Needle 标题"})
        self.assertEqual(result["items"][1]["searchMatch"]["messageId"], latest["id"])

    def test_search_ignores_system_messages_and_other_devices(self):
        visible = self.create()
        self.store.append_message(visible["id"], "system", "needle 系统备注")
        hidden = self.create("needle 隔离", scope="device-b")
        self.store.append_message(hidden["id"], "user", "needle 隔离正文")
        self.assertEqual(self.store.query_conversations(q="needle", device_scope="device-a")["total"], 0)

    def test_search_sql_wildcards_are_literals(self):
        for query in ("%", "_", "\\", "' OR 1=1 --"):
            with self.subTest(query=query):
                target = self.create("标点目标")
                self.store.append_message(target["id"], "user", "仅命中这个符号：" + query)
                result = self.store.query_conversations(q=query)
                self.assertEqual([x["id"] for x in result["items"]], [target["id"]])

    def test_search_snippet_is_plain_bounded_and_centred_on_match(self):
        conv = self.create()
        self.store.append_message(conv["id"], "assistant", "开头" * 150 + "<tag>needle</tag>" + "结尾" * 150)
        snippet = self.store.query_conversations(q="needle")["items"][0]["searchMatch"]["snippet"]
        self.assertIsInstance(snippet, str)
        self.assertLessEqual(len(snippet), 140)
        self.assertIn("needle", snippet)
        self.assertTrue(snippet.startswith("…"))
        self.assertTrue(snippet.endswith("…"))

    def test_active_archived_all_queries_and_scoped_decision_reuse(self):
        active = self.create(mode="review", decision_id="decision-1")
        archived = self.create("归档", mode="review", decision_id="decision-2")
        self.edit(archived, status="archived")
        self.assertEqual(self.store.query_conversations()["total"], 1)
        self.assertEqual(self.store.query_conversations(status="archived")["total"], 1)
        self.assertEqual(self.store.query_conversations(status="all")["total"], 2)
        self.assertEqual(len(self.store.list_conversations(status="all")), 2)
        self.assertIsNone(self.store.find_conversation_by_decision("decision-2"))
        self.assertEqual(self.store.find_conversation_by_decision("decision-2", status="all", device_scope="device-a")["id"], archived["id"])
        self.assertIsNone(self.store.find_conversation_by_decision("decision-2", status="all", device_scope="device-b"))
        self.assertEqual(self.store.find_conversation_by_decision("decision-1")["id"], active["id"])

    def test_search_validation_and_empty_pages(self):
        for kwargs in ({"status": "deleted"}, {"q": "a" * 101}, {"limit": 0}, {"limit": 201},
                       {"offset": -1}, {"offset": 2**63}, {"offset": 10**99}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConversationError):
                self.store.query_conversations(**kwargs)
        self.assertEqual(self.store.query_conversations(offset=500), {"items": [], "total": 0, "hasMore": False})
        self.assertEqual(self.store.query_conversations(offset=2**63 - 1), {"items": [], "total": 0, "hasMore": False})

    def test_legacy_api_page_limit_two_hundred_is_supported(self):
        for index in range(201):
            self.create(f"合成批量 {index}")
        result = self.store.query_conversations(limit=200)
        self.assertEqual((len(result["items"]), result["total"], result["hasMore"]), (200, 201, True))

    def test_archived_onboarding_lookup_filters_before_limit(self):
        onboarding = self.create("初识", time="2020-01-01T00:00:00Z", mode="onboarding")
        self.edit(onboarding, status="archived")
        for index in range(55):
            self.create(f"普通对话 {index}")
        result = self.store.list_conversations(status="all", mode="onboarding", limit=1, device_scope="device-a")
        self.assertEqual([row["id"] for row in result], [onboarding["id"]])
        with self.assertRaises(ConversationError):
            self.store.list_conversations(mode="unknown")

    def test_only_new_complete_user_message_restores_archive(self):
        conv = self.create()
        pinned = self.edit(conv, pinned=True)
        archived = self.edit(conv, status="archived")
        self.store.append_message(conv["id"], "assistant", "后台回答")
        self.store.append_message(conv["id"], "system", "后台备注")
        aborted = self.store.append_message(conv["id"], "user", "失败请求", status="aborted")
        self.store.update_message(aborted["id"], status="complete")
        still = self.store.get_conversation(conv["id"])
        self.assertEqual((still["status"], still["metadataRevision"]), ("archived", archived["metadataRevision"]))
        self.store.append_message(conv["id"], "user", "新的主动消息")
        restored = self.store.get_conversation(conv["id"])
        self.assertEqual((restored["status"], restored["metadataRevision"]), ("active", archived["metadataRevision"] + 1))
        self.assertEqual(restored["pinnedAt"], pinned["pinnedAt"])
        self.store.append_message(conv["id"], "user", "后续消息")
        self.assertEqual(self.store.get_conversation(conv["id"])["metadataRevision"], restored["metadataRevision"])

    def test_duplicate_user_insert_cannot_restore_archive(self):
        conv = self.create()
        message = self.store.append_message(conv["id"], "user", "原消息")
        archived = self.edit(conv, status="archived")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_message(conv["id"], "user", "重试", message_id=message["id"])
        self.assertEqual(self.store.get_conversation(conv["id"]), archived)

    def test_new_import_restores_archive_atomically_but_batch_retry_does_not(self):
        conv = self.create()
        self.edit(conv, pinned=True)
        archived = self.edit(conv, status="archived")
        imports = ChatImportStore(self.store)
        files = [{"id": "file-1", "name": "合成资料.txt", "size": 10}]
        first = imports.create(conv["id"], "request-1", "", files)
        restored = self.store.get_conversation(conv["id"])
        self.assertEqual((restored["status"], restored["metadataRevision"]), ("active", archived["metadataRevision"] + 1))
        self.assertEqual(restored["pinnedAt"], archived["pinnedAt"])
        rearchived = self.edit(conv, status="archived")
        repeated = imports.create(conv["id"], "request-1", "", files)
        self.assertEqual(repeated["message_id"], first["message_id"])
        self.assertEqual(self.store.get_conversation(conv["id"]), rearchived)
        self.assertEqual(self.store.count_messages(conv["id"]), 1)

    def test_import_failure_rolls_back_message_and_restore(self):
        conv = self.create()
        archived = self.edit(conv, status="archived")
        imports = ChatImportStore(self.store)
        files = [{"id": "duplicate", "name": "一.txt"}, {"id": "duplicate", "name": "二.txt"}]
        with self.assertRaises(sqlite3.IntegrityError):
            imports.create(conv["id"], "request-bad", "合成导入", files)
        self.assertEqual(self.store.get_conversation(conv["id"]), archived)
        self.assertEqual(self.store.count_messages(conv["id"]), 0)
        self.assertEqual(imports.batches(conv["id"]), [])


if __name__ == "__main__":
    unittest.main()
