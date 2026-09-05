"""Deleted origin conversations cannot reclassify retained claims as global."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mindos.stores.conversation_store import ConversationStore
from mindos.stores.growth_store import GrowthStore
from mindos.stores.ontology_store import OntologyStore
from mindos.zhijun.alignment import visible
from mindos.zhijun import nudges
from mindos import zhijun_home


class AlignmentScopeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.convs = ConversationStore(root / "conversations.db")
        self.onto = OntologyStore(root / "ontology.db")
        self.growth = GrowthStore(root / "growth.db")

    def tearDown(self):
        self.tmp.cleanup()

    def origin(self, scope="device:alpha", text="仅甲设备知道的合成选择"):
        conv = self.convs.create_conversation(device_scope=scope)
        message = self.convs.append_message(conv["id"], "user", text, meta={"routingSources": []})
        claim = self.onto.create_claim({"subject_entity_id": "ent_me", "section": "ways",
            "layer": "self_declared", "predicate": "prefers", "content": text, "confidence": .9},
            [{"kind": "conversation_turn", "conversation_id": conv["id"], "message_id": message["id"], "quote": text}],
            trust_state="confirmed", trust_origin="utterance", conversation_id=conv["id"], message_id=message["id"])
        return conv, claim

    def test_deleted_device_origin_is_not_visible_in_any_device(self):
        conv, claim = self.origin()
        self.assertTrue(visible(claim, self.convs, "device:alpha"))
        self.assertFalse(visible(claim, self.convs, "global"))
        self.convs.delete_conversation(conv["id"])
        for scope in ("global", "device:alpha", "device:beta"):
            self.assertFalse(visible(claim, self.convs, scope))
        self.assertEqual(self.onto.get_claim(claim["id"]), claim)

    def test_deleted_global_origin_does_not_become_originless_legacy(self):
        conv, claim = self.origin("global")
        self.assertTrue(visible(claim, self.convs, "global"))
        self.convs.delete_conversation(conv["id"])
        self.assertFalse(visible(claim, self.convs, "global"))

    def test_originless_legacy_remains_global_only(self):
        claim = {"evidence": [{"kind": "user_edit", "quote": "合成旧记录"}]}
        self.assertTrue(visible(claim, self.convs, "global"))
        self.assertFalse(visible(claim, self.convs, "device:alpha"))

    def test_one_missing_origin_blocks_claim_even_with_valid_global_evidence(self):
        conv = self.convs.create_conversation()
        claim = {"evidence": [{"conversationId": conv["id"]}, {"conversationId": "deleted-origin"}]}
        self.assertFalse(visible(claim, self.convs, "global"))

    def test_archiving_preserves_scope_and_visibility(self):
        conv, claim = self.origin()
        self.convs.update_metadata(conv["id"], expected_revision=0, status="archived", device_scope="device:alpha")
        self.assertTrue(visible(claim, self.convs, "device:alpha"))
        self.assertFalse(visible(claim, self.convs, "global"))

    def test_deleted_origin_is_not_exposed_by_home_or_weekly_review(self):
        marker = "PRIVATE_ALPHA_DELETED_ORIGIN"
        origins = [self.origin(text=marker + str(i)) for i in range(3)]
        for conv, _ in origins:
            self.convs.delete_conversation(conv["id"])
        before = self.onto.list_claims()
        home = zhijun_home.build_home_overview(ontology=self.onto, conversations=self.convs,
            growth=self.growth, enqueue=False, scope="global")
        self.assertNotIn(marker, json.dumps(home, ensure_ascii=False))
        with patch.object(OntologyStore, "instance", return_value=self.onto):
            weekly = nudges.weekly_review_candidate(conv_store=self.convs, growth=self.growth,
                now=datetime.now(timezone.utc))
        self.assertIsNone(weekly)
        self.assertEqual(self.onto.list_claims(), before)


if __name__ == "__main__":
    unittest.main()
