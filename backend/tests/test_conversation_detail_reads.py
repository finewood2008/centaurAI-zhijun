"""Conversation detail reads keep scope checks without attachment schema locks."""
from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from mindos import conversations
from mindos.stores.conversation_store import ConversationStore


class ConversationDetailReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ConversationStore(Path(temporary.name) / "conversations.db")
        for replacement in (
            patch.object(conversations, "_store", return_value=self.store),
            patch.dict(os.environ, {"ZHIJUN_WORKSPACE_ID": ""}),
            patch("mindos.chat_imports.ChatImportStore", side_effect=AssertionError("read must not initialize attachment schema")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    @staticmethod
    def request(device_id):
        request = Request({"type": "http"})
        request.state.mindos_device_context = SimpleNamespace(device_id=device_id)
        return request

    def test_scope_check_precedes_message_and_receipt_reads(self):
        conv = self.store.create_conversation(device_scope="device:a")
        with patch.object(self.store, "list_messages", side_effect=AssertionError("unauthorized message read")), \
                patch.object(self.store, "list_receipts", side_effect=AssertionError("unauthorized receipt read")):
            for cid, request in ((conv["id"], self.request("b")), (conv["id"], None),
                                 ("conv_missing", self.request("a"))):
                with self.assertRaises(HTTPException) as error:
                    conversations.get_conversation(cid, request)
                self.assertEqual(error.exception.status_code, 404)
                self.assertEqual(error.exception.detail["code"], "CONVERSATION_NOT_FOUND")
        result = conversations.get_conversation(conv["id"], self.request("a"))
        self.assertEqual(result, {"conversation": conv, "messages": []})

    def test_default_scope_and_read_do_not_wait_for_python_writer_lock(self):
        conv = self.store.create_conversation()
        with ThreadPoolExecutor(max_workers=1) as executor:
            # A separate thread must not need the attachment initializer's
            # shared writer lock merely to verify and display a conversation.
            with self.store._lock:
                future = executor.submit(conversations.get_conversation, conv["id"])
                result = future.result(timeout=2)
        self.assertEqual(result, {"conversation": conv, "messages": []})

    def test_worker_scope_still_requires_active_runtime(self):
        conv = self.store.create_conversation()
        with patch.dict(os.environ, {"ZHIJUN_WORKSPACE_ID": "test-workspace"}), \
                patch("zhijun_worker.capabilities.current", return_value=None):
            with self.assertRaises(HTTPException) as error:
                conversations.get_conversation(conv["id"])
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.detail["code"], "WORKER_NOT_RUNNING")

    def test_worker_request_still_requires_matching_workspace_proof(self):
        conv = self.store.create_conversation()
        workspace = object()
        request = Request({"type": "http"})
        with patch.dict(os.environ, {"ZHIJUN_WORKSPACE_ID": "test-workspace"}), \
                patch("zhijun_worker.capabilities.current", return_value=(workspace, None)):
            with self.assertRaises(HTTPException) as error:
                conversations.get_conversation(conv["id"], request)
            self.assertEqual(error.exception.status_code, 401)
            self.assertEqual(error.exception.detail["code"], "WORKER_PROOF_REQUIRED")
            request.state.zhijun_workspace = workspace
            self.assertEqual(conversations.get_conversation(conv["id"], request)["conversation"], conv)

    def test_batched_receipts_provenance_draft_and_decision_are_preserved(self):
        conv = self.store.create_conversation(device_scope="device:a", decision_id="decision_test")
        user = self.store.append_message(conv["id"], "user", "Synthetic test input")
        claim = {"id": "claim_test", "content": "Synthetic fact", "section": "who",
                 "layer": "self_declared", "trustState": "confirmed"}
        assistant_ids = []
        for _ in range(12):
            message = self.store.append_message(conv["id"], "assistant", "Synthetic reply")
            assistant_ids.append(message["id"])
            self.store.save_receipt(message_id=message["id"], conversation_id=conv["id"],
                provider="fake", model="fake", external=False, confirmed_claim_ids=[claim["id"]],
                working_claim_ids=[], material_chunk_keys=[], retracted_notice_count=0, prompt_chars=42)
        routed = self.store.append_message(conv["id"], "assistant", "Synthetic routed reply",
            meta={"routingProvenance": {"channel": "local"}, "alignmentSources": [{"id": "alignment_test"}]})
        draft = self.store.upsert_draft(conv["id"], {"title": "Synthetic draft"})
        decision = {"id": "decision_test", "title": "Synthetic decision"}
        onto, growth = Mock(), Mock()
        onto.get_claims.return_value = [claim]
        growth.get_decision.return_value = decision
        with patch.object(conversations, "_ontology_store", return_value=onto), \
                patch.object(conversations, "_growth_store", return_value=growth), \
                patch.object(self.store, "get_conversation", wraps=self.store.get_conversation) as metadata, \
                patch.object(self.store, "list_receipts", wraps=self.store.list_receipts) as receipts, \
                patch.object(self.store, "get_receipt", side_effect=AssertionError("per-message receipt read")):
            result = conversations.get_conversation(conv["id"], self.request("a"))
        metadata.assert_called_once_with(conv["id"], device_scope="device:a")
        receipts.assert_called_once_with(conv["id"])
        onto.get_claims.assert_called_once_with([claim["id"]], with_evidence=False)
        self.assertEqual(result["decisionDraft"], draft)
        self.assertEqual(result["decision"], decision)
        self.assertEqual([m["id"] for m in result["messages"]], [user["id"], *assistant_ids, routed["id"]])
        for message in result["messages"][1:-1]:
            self.assertEqual(message["provenance"]["confirmedClaims"], [claim])
            self.assertEqual(message["provenance"]["promptChars"], 42)
        self.assertEqual(result["messages"][-1]["provenance"],
                         {"channel": "local", "alignmentSources": [{"id": "alignment_test"}]})


if __name__ == "__main__":
    unittest.main()
