"""Worker retrieval versions: strict capability contract, no network or models."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mindos.stores import conversation_store, ontology_store
from mindos.zhijun import memory_index, memory_retrieval
from zhijun_worker.capabilities import CapabilityError


class WorkerMemoryIndexContractTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.port = SimpleNamespace(call=Mock(side_effect=self.strict_score))
        self.environment = patch.dict(os.environ, {"ZHIJUN_WORKSPACE_ID": "synthetic-workspace"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.capability = patch("zhijun_worker.capabilities.require", return_value=self.port)
        self.capability.start()
        self.addCleanup(self.capability.stop)
        self.encoder = patch.object(memory_index, "_local_encoder", side_effect=AssertionError("worker must not load an encoder"))
        self.encoder.start()
        self.addCleanup(self.encoder.stop)
        memory_index.CACHE.clear()
        self.addCleanup(memory_index.CACHE.clear)

    def strict_score(self, name, payload):
        self.assertEqual(name, "retrieval.score")
        documents, versions = payload["documents"], payload["sourceVersions"]
        self.assertEqual(set(documents), set(versions))
        self.assertTrue(1 <= len(documents) <= 32)
        self.assertTrue(1 <= len(payload["query"]) <= 1000)
        self.assertLessEqual(sum(len(text.encode("utf-8")) for text in documents.values()), 250 * 1024)
        for ident, text in documents.items():
            self.assertLessEqual(len(text), 8000)
            if not isinstance(versions[ident], str) or not 1 <= len(versions[ident]) <= 256:
                raise CapabilityError("RETRIEVAL_REQUEST_INVALID", 422)
        self.calls.append(payload)
        return dict.fromkeys(documents, 0.91)

    @staticmethod
    def expected_version(text, version=""):
        return hashlib.sha256((str(version) + "\0" + text).encode("utf-8")).hexdigest()

    def test_long_evidence_and_authorization_metadata_is_an_opaque_digest(self):
        metadata = json.dumps({"evidence": [{"quote": "合成证据" * 200}], "privacyLevel": "private"}, ensure_ascii=False)
        self.assertGreater(len(metadata), 256)
        result = memory_index.scores("test", "技术架构", {"claim": "我是程序员"}, {"claim": metadata})
        self.assertEqual(result, {"claim": 0.91})
        version = self.calls[0]["sourceVersions"]["claim"]
        self.assertEqual(version, self.expected_version("我是程序员", metadata))
        self.assertRegex(version, r"^[0-9a-f]{64}$")
        self.assertNotIn(metadata, json.dumps(self.calls, ensure_ascii=False))

    def test_missing_empty_and_non_string_versions_are_bounded_and_stable(self):
        for versions in (None, {}, {"claim": ""}, {"claim": 0}, {"claim": None}):
            with self.subTest(versions=versions):
                self.calls.clear()
                for _ in range(2):
                    memory_index.scores("test", "问题", {"claim": "内容"}, versions)
                expected = self.expected_version("内容", (versions or {}).get("claim", ""))
                self.assertEqual([call["sourceVersions"]["claim"] for call in self.calls], [expected, expected])

    def test_version_hash_includes_text_after_transport_truncation_boundary(self):
        prefix = "字" * 8000
        for tail in ("甲", "乙"):
            memory_index.scores("test", "问题", {"claim": prefix + tail}, {"claim": "same"})
        self.assertEqual(self.calls[0]["documents"], self.calls[1]["documents"])
        self.assertNotEqual(self.calls[0]["sourceVersions"], self.calls[1]["sourceVersions"])
        self.assertEqual(self.calls[1]["sourceVersions"]["claim"], self.expected_version(prefix + "乙", "same"))

    def test_changed_metadata_invalidates_same_text(self):
        for version in ("confirmed-permitted", "working-revoked"):
            memory_index.scores("test", "问题", {"claim": "不变的内容"}, {"claim": version})
        self.assertNotEqual(self.calls[0]["sourceVersions"], self.calls[1]["sourceVersions"])

    def test_worker_and_local_cache_use_identical_version_identity(self):
        docs, versions = {"claim": "完整内容"}, {"claim": "metadata" * 100}
        memory_index.scores("test", "问题", docs, versions)
        model = SimpleNamespace(encode=Mock(side_effect=lambda texts, **kwargs: [[1., 0.] for _ in texts]))
        with patch.dict(os.environ, {"ZHIJUN_WORKSPACE_ID": ""}), patch.object(memory_index, "_local_encoder", return_value=(SimpleNamespace(), model)):
            memory_index.scores("local", "问题", docs, versions)
        self.assertEqual(memory_index.CACHE["local"]["rows"]["claim"][0], self.calls[0]["sourceVersions"]["claim"])

    def test_document_count_batching_preserves_all_candidates_and_versions(self):
        docs = {f"claim-{index}": f"内容-{index}" for index in range(65)}
        versions = {ident: "源版本" * 200 for ident in docs}
        result = memory_index.scores("test", "问题" * 1000, docs, versions)
        self.assertEqual([len(call["documents"]) for call in self.calls], [32, 32, 1])
        self.assertEqual(set(result), set(docs))
        for call in self.calls:
            for ident in call["documents"]:
                self.assertEqual(call["sourceVersions"][ident], self.expected_version(docs[ident], versions[ident]))

    def test_utf8_byte_budget_splits_before_count_limit(self):
        docs = {f"claim-{index}": "😀" * 8001 for index in range(17)}
        self.assertEqual(set(memory_index.scores("test", "问题", docs)), set(docs))
        self.assertEqual([len(call["documents"]) for call in self.calls], [8, 8, 1])
        self.assertEqual(sum(len(call["documents"]) for call in self.calls), 17)

    def test_real_errors_are_not_silently_converted_to_empty_recall(self):
        for code, status in (("RETRIEVAL_REQUEST_INVALID", 422), ("ACCESS_DENIED", 403), ("WORKSPACE_OBJECT_LIMIT", 429)):
            with self.subTest(code=code):
                failure = CapabilityError(code, status)
                self.port.call.side_effect = failure
                with self.assertRaises(CapabilityError) as caught:
                    memory_index.scores("test", "问题", {"claim": "内容"})
                self.assertIs(caught.exception, failure)

    def test_only_explicit_unavailable_capabilities_allow_lexical_fallback(self):
        for code in ("EMBEDDER_UNAVAILABLE", "RETRIEVAL_UNAVAILABLE", "CAPABILITY_UNAVAILABLE"):
            with self.subTest(code=code):
                self.port.call.side_effect = CapabilityError(code, 503)
                self.assertEqual(memory_index.scores("test", "问题", {"claim": "内容"}), {})

    def test_invalid_score_responses_remain_contract_errors(self):
        for values in ([], {"foreign": 1.}, {"claim": True}, {"claim": float("nan")}, {"claim": "0.9"}):
            with self.subTest(values=values):
                self.port.call.side_effect = None
                self.port.call.return_value = values
                with self.assertRaises(CapabilityError) as caught:
                    memory_index.scores("test", "问题", {"claim": "内容"})
                self.assertEqual(caught.exception.code, "CAPABILITY_RETRIEVAL_CONTRACT")

    def test_real_claim_retrieval_long_provenance_and_overview_bypass(self):
        with TemporaryDirectory() as directory:
            ontology = ontology_store.reset_for_tests(Path(directory) / "ontology.db")
            conversations = conversation_store.reset_for_tests(Path(directory) / "conversations.db")
            conv = conversations.create_conversation(device_scope="global")
            content = "我是一个程序员，希望提升技术架构能力。" + "合成背景说明。" * 50
            message = conversations.append_message(conv["id"], "user", content)
            claim = ontology.create_claim(
                {"section": "who", "layer": "self_declared", "content": "我是一个程序员，希望提升技术架构能力。",
                 "subject_entity_id": "ent_me", "device_scope": "global", "scope": "long_term"},
                [{"kind": "conversation_turn", "conversation_id": conv["id"], "message_id": message["id"], "quote": content}],
                trust_state="confirmed", trust_origin="user_created")
            provenance = json.dumps({key: claim.get(key) for key in ("trustState", "scope", "contextRef", "evidence", "selfAlignment", "privacyLevel", "updatedAt")}, ensure_ascii=False, sort_keys=True)
            self.assertGreater(len(provenance), 256)
            other = conversations.create_conversation(device_scope="another-device")
            other_message = conversations.append_message(other["id"], "user", "其他设备的技术架构资料")
            foreign = ontology.create_claim(
                {"section": "who", "layer": "self_declared", "content": "其他设备的技术架构资料",
                 "subject_entity_id": "ent_me", "device_scope": "another-device", "scope": "long_term"},
                [{"kind": "conversation_turn", "conversation_id": other["id"], "message_id": other_message["id"], "quote": "其他设备的技术架构资料"}],
                trust_state="confirmed", trust_origin="user_created")
            results = memory_retrieval.retrieve_claims(ontology, "请帮我准备技术架构沟通提纲", [], conversations=conversations, scope="global")
            self.assertIn(claim["id"], [row["id"] for row in results])
            self.assertTrue(self.calls)
            self.assertTrue(all(foreign["id"] not in call["documents"] for call in self.calls))
            self.assertRegex(self.calls[-1]["sourceVersions"][claim["id"]], r"^[0-9a-f]{64}$")
            self.port.call.reset_mock()
            overview = memory_retrieval.retrieve_claims(ontology, "我是谁？", [], intent="self_overview", conversations=conversations, scope="global")
            self.assertIn(claim["id"], [row["id"] for row in overview])
            self.port.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
