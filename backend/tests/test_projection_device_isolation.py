"""Shared legacy exports may not mix personal profiles across devices."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mindos.stores.ontology_store import OntologyStore
from mindos.stores.conversation_store import ConversationStore
from mindos.zhijun import projection, context_pack


class ProjectionDeviceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.onto = OntologyStore(root / "ontology.db")
        self.convs = ConversationStore(root / "conversations.db")
        self.patch = patch.object(ConversationStore, "_instance", self.convs)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def claim(self, text, scope="global", evidence=None):
        return self.onto.create_claim({"content": text, "section": "who", "layer": "self_declared",
            "device_scope": scope, "export_allowed": True}, evidence or [],
            trust_state="confirmed", trust_origin="user_created")

    def test_device_handwritten_and_legacy_origin_claims_do_not_enter_global_exports(self):
        self.claim("GLOBAL_SAFE")
        self.claim("DEVICE_PRIVATE", "device:lin")
        cid = self.convs.create_conversation(device_scope="device:lin")["id"]
        self.claim("LEGACY_DEVICE_PRIVATE", evidence=[{"kind": "conversation_turn", "conversation_id": cid, "quote": "LEGACY_DEVICE_PRIVATE"}])
        self.claim("DELETED_ORIGIN_PRIVATE", evidence=[{"kind": "conversation_turn", "conversation_id": "missing", "quote": "DELETED_ORIGIN_PRIVATE"}])
        full, export = projection.render(self.onto)
        self.assertIn("GLOBAL_SAFE", full)
        self.assertIn("GLOBAL_SAFE", export)
        for forbidden in ("DEVICE_PRIVATE", "LEGACY_DEVICE_PRIVATE", "DELETED_ORIGIN_PRIVATE"):
            self.assertNotIn(forbidden, full)
            self.assertNotIn(forbidden, export)
        device, _ = projection.render(self.onto, scope="device:lin")
        self.assertIn("DEVICE_PRIVATE", device)
        self.assertIn("LEGACY_DEVICE_PRIVATE", device)
        self.assertNotIn("GLOBAL_SAFE", device)
        self.assertNotIn("DELETED_ORIGIN_PRIVATE", device)
        self.assertEqual([x["content"] for x in context_pack.exportable_claims(self.onto)], ["GLOBAL_SAFE"])

    def test_foreign_claims_never_overwrite_handwritten_global_user_file(self):
        self.claim("DEVICE_PRIVATE", "device:lin")
        with patch.object(projection, "_write") as write:
            result = projection.write_projection(self.onto)
        self.assertIsNone(result["user"])
        self.assertEqual([call.args[0] for call in write.call_args_list], [projection.PROFILE_FILE])
        self.assertNotIn("DEVICE_PRIVATE", write.call_args.args[1])
