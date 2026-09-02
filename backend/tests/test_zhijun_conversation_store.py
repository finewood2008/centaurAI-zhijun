"""知君对话存储：会话、消息序号、标题、摘要、回执、级联删除。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mindos.stores.conversation_store import ConversationError, ConversationNotFoundError, ConversationStore
from runtime_paths import CONVERSATIONS_DB_PATH, DB_ROOT


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ConversationStore(Path(self._tmp.name) / "conversations.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_path_under_db_root(self) -> None:
        self.assertEqual(CONVERSATIONS_DB_PATH.parent, DB_ROOT)

    def test_create_and_list(self) -> None:
        a = self.store.create_conversation(mode="chat")
        b = self.store.create_conversation(mode="onboarding", title="  建档  ")
        self.assertEqual(b["title"], "建档")
        self.assertEqual({c["id"] for c in self.store.list_conversations()}, {a["id"], b["id"]})
        with self.assertRaises(ConversationError):
            self.store.create_conversation(mode="weird")

    def test_messages_seq_title_and_recent(self) -> None:
        conv = self.store.create_conversation()
        with self.assertRaises(ConversationNotFoundError):
            self.store.append_message("conv_missing", "user", "x")
        u = self.store.append_message(conv["id"], "user", "我在做远川项目，压力很大。\n第二行")
        a = self.store.append_message(conv["id"], "assistant", "记下了", provider="fake", model="fake-zhijun")
        self.assertEqual((u["seq"], a["seq"]), (1, 2))
        refreshed = self.store.get_conversation(conv["id"])
        self.assertEqual(refreshed["messageCount"], 2)
        self.assertEqual(refreshed["title"], "我在做远川项目，压力很大。 第二行")
        self.assertIsNotNone(refreshed["lastMessageAt"])
        self.assertEqual([m["role"] for m in self.store.recent_messages(conv["id"], 1)], ["assistant"])
        self.assertEqual(self.store.count_messages(conv["id"], role="user"), 1)
        self.assertEqual([m["seq"] for m in self.store.list_messages(conv["id"], before_seq=2)], [1])

    def test_update_message_status_and_meta(self) -> None:
        conv = self.store.create_conversation()
        m = self.store.append_message(conv["id"], "assistant", "部分", message_id="msg_fixed")
        updated = self.store.update_message("msg_fixed", status="aborted", meta={"stopReason": None})
        self.assertEqual(updated["status"], "aborted")
        self.assertEqual(updated["meta"], {"stopReason": None})
        self.assertEqual(self.store.get_message(m["id"])["status"], "aborted")
        with self.assertRaises(ConversationError):
            self.store.update_message("msg_fixed", status="weird")

    def test_summary_revisions(self) -> None:
        conv = self.store.create_conversation()
        self.assertIsNone(self.store.latest_summary(conv["id"]))
        self.store.save_summary(conv["id"], up_to_seq=2, summary="第一版", key_points=["a"])
        second = self.store.save_summary(conv["id"], up_to_seq=4, summary="第二版", key_points=["a", "b"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.store.latest_summary(conv["id"])["summary"], "第二版")

    def test_receipt_upsert_and_cascade_delete(self) -> None:
        conv = self.store.create_conversation()
        msg = self.store.append_message(conv["id"], "assistant", "x")
        receipt = self.store.save_receipt(
            message_id=msg["id"],
            conversation_id=conv["id"],
            provider="fake",
            model="fake-zhijun",
            external=False,
            confirmed_claim_ids=["clm_a"],
            working_claim_ids=[],
            material_chunk_keys=[],
            retracted_notice_count=1,
            prompt_chars=1200,
        )
        self.assertEqual(receipt["confirmedClaimIds"], ["clm_a"])
        again = self.store.save_receipt(
            message_id=msg["id"],
            conversation_id=conv["id"],
            provider="fake",
            model="fake-zhijun",
            external=False,
            confirmed_claim_ids=["clm_a", "clm_b"],
            working_claim_ids=["clm_c"],
            material_chunk_keys=["k"],
            retracted_notice_count=0,
            prompt_chars=1300,
        )
        self.assertEqual(again["workingClaimIds"], ["clm_c"])
        self.assertTrue(self.store.delete_conversation(conv["id"]))
        self.assertIsNone(self.store.get_conversation(conv["id"]))
        self.assertIsNone(self.store.get_message(msg["id"]))
        self.assertIsNone(self.store.get_receipt(msg["id"]))
        self.assertFalse(self.store.delete_conversation(conv["id"]))


if __name__ == "__main__":
    unittest.main()
