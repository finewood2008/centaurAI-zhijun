"""Synthetic prospective checks: expectations never see the held-out outcome.

These verify product mechanics and privacy, not real-person prediction accuracy.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations, learning_routes, growth, zhijun_home
from mindos.stores.alignment_store import AlignmentStore
from mindos.stores import ontology_store, conversation_store, growth_store
from mindos.stores.learning_store import LearningStore
from mindos.zhijun import context, consolidate, extract, growth_hooks, projection, context_pack, alignment, jobs
from mindos.zhijun.source_policy import SourcePolicy
from mindos.zhijun.provider import FakeProvider, ProviderError


BEFORE = {"situation": "熟悉主题，有准备时间，听众不熟悉", "expected": "准备后愿意分享，现场问答仍可能紧张", "alternative": "充分准备后仍完全不想参加"}
AFTER = {"comparison": "mixed", "reflection": "准备时间改变了意愿，临场问答仍紧张", "content": "在熟悉主题且有准备时间时，我愿意分享；临场问答仍会紧张", "exceptions": "不熟悉的主题还没有证据", "framing": "context_only"}


class ContextLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.onto = ontology_store.reset_for_tests(root / 'onto.db')
        self.convs = conversation_store.reset_for_tests(root / 'conv.db')
        self.growth = growth_store.reset_for_tests(root / 'growth.db')
        self.env = patch.dict(os.environ, {'ZHIJUN_PROVIDER': 'fake', 'ZHIJUN_MATERIAL_EVIDENCE': '0'})
        self.env.start()
        self.claim = self.onto.create_claim({'content': '我不喜欢公开分享', 'section': 'ways', 'layer': 'self_declared'},
            [{'kind': 'user_edit', 'quote': '我不喜欢公开分享'}], trust_state='confirmed', trust_origin='user_created')
        self.decision = self.growth.create_decision({'title': '公开分享邀请', 'context': '熟悉主题，有准备时间的公开分享',
            'options': ['参加', '不参加'], 'choice': '参加', 'rationale': '想试试看', 'confidence': 50,
            'expectedOutcome': '希望交流有帮助', 'reviewAt': None, 'relatedEntityIds': [], 'evidenceRefs': []})
        self.conv = self.convs.create_conversation(mode='review', decision_id=self.decision['id'])
        self.url = f"/api/mindos/conversations/{self.conv['id']}/learning"
        app = FastAPI()
        app.include_router(conversations.router)
        app.include_router(growth.router)
        self.client = TestClient(app)
        self.provider = Mock()
        self.provider.name, self.provider.model, self.provider.external = 'ollama', 'synthetic', False
        self.provider.complete_json.return_value = BEFORE
        self.factory = patch.object(learning_routes, 'local_provider', return_value=self.provider)
        self.factory.start()

    def tearDown(self):
        self.factory.stop()
        self.env.stop()
        self.tmp.cleanup()

    def post(self, suffix, payload, status=200):
        r = self.client.post(self.url + '/' + suffix, json=payload)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def start(self):
        return self.post('start', {**BEFORE, 'claimId': self.claim['id'], 'claimUpdatedAt': self.claim['updatedAt']})

    def outcome(self):
        return self.growth.record_outcome(self.decision['id'], {'result': '合成留出结果：准备后很愿意分享，但问答仍紧张', 'notes': '', 'evidenceRefs': []})

    def propose(self):
        self.start()
        self.outcome()
        return self.post('propose', {**AFTER, 'expectedRevision': 1})

    def assemble(self, provider, conv=None):
        return context.assemble(conversation=conv or self.conv, user_text='公开分享 邀请 准备 复盘', depth='brief',
            provider=provider, ontology=self.onto, conversation_store=self.convs, recent_messages=[], user_turns=1)

    def test_full_loop_preserves_history_and_resets_alignment(self):
        episode = self.propose()
        self.assertEqual(self.onto.get_claim(self.claim['id'])['content'], self.claim['content'])
        payload = {'expectedRevision': episode['revision'], 'action': 'apply', 'content': AFTER['content'],
                   'framing': AFTER['framing'], 'exceptions': AFTER['exceptions']}
        saved = self.post('resolve', payload)
        self.assertEqual(saved['status'], 'applied')
        self.assertEqual(self.post('resolve', payload), saved)
        old = self.onto.get_claim(self.claim['id'])
        new = self.onto.get_claim(saved['resolution']['replacementId'])
        self.assertEqual(old['trustState'], 'superseded')
        self.assertEqual(new['trustState'], 'confirmed')
        self.assertEqual(new['selfAlignment']['level'], None)
        self.assertEqual(new['contextual']['situation'], BEFORE['situation'])
        self.assertTrue(SourcePolicy().claim_local(new))
        self.assertIn(AFTER['content'], self.assemble(FakeProvider()).system)
        self.assertIn('不熟悉的主题还没有证据', self.assemble(FakeProvider()).system)
        self.assertTrue(self.assemble(FakeProvider()).provenance['localOnlyDerived'])
        self.assertEqual(ontology_store.OntologyStore(self.onto.db_path).get_claim(new['id'])['content'], AFTER['content'])
        self.assertEqual(LearningStore(ontology_store.OntologyStore(self.onto.db_path)).get(self.decision['id']), saved)

    def test_model_draft_is_readonly_and_prediction_does_not_see_result(self):
        response = self.post('suggest', {'claimId': self.claim['id']})
        self.assertFalse(response['external'])
        request = self.provider.complete_json.call_args.args[0]
        self.assertNotIn('合成留出结果', str(request.messages))
        self.assertNotIn('实际结果', str(request.messages))
        self.assertIsNone(LearningStore(self.onto).get(self.decision['id']))
        self.assertEqual(self.onto.get_claim(self.claim['id']), self.claim)
        self.start()
        self.outcome()
        self.provider.complete_json.return_value = AFTER
        self.post('suggest', {'expectedRevision': 1})
        self.assertIn('合成留出结果', str(self.provider.complete_json.call_args.args[0].messages))
        self.assertEqual(LearningStore(self.onto).get(self.decision['id'])['status'], 'watching')

    def test_freeze_idempotency_and_no_posthoc_prediction(self):
        first = self.start()
        self.assertEqual(first, self.start())
        self.post('start', {**BEFORE, 'expected': '重写预期', 'claimId': self.claim['id'], 'claimUpdatedAt': self.claim['updatedAt']}, 409)
        self.outcome()
        self.assertEqual(first, self.start())
        self.assertEqual(self.onto.get_claim(self.claim['id']), self.claim)

    def test_no_start_after_outcome(self):
        self.outcome()
        self.post('start', {**BEFORE, 'claimId': self.claim['id'], 'claimUpdatedAt': self.claim['updatedAt']}, 409)
        self.post('suggest', {'claimId': self.claim['id']}, 409)

    def test_old_revision_and_newer_claim_cannot_be_overwritten(self):
        episode = self.propose()
        self.post('resolve', {'action': 'apply', 'expectedRevision': 1, 'content': AFTER['content']}, 409)
        updated = self.onto.transition(self.claim['id'], 'partial', edited_content='我的新修改优先于旧提议', surface='ontology_page')
        self.post('resolve', {'action': 'apply', 'expectedRevision': episode['revision'], 'content': AFTER['content']}, 409)
        self.assertEqual(self.onto.get_claim(updated['replacedBy']['id'])['trustState'], 'confirmed')

    def test_reaffirm_during_proposal_requires_recheck(self):
        episode = self.propose()
        self.onto.transition(self.claim['id'], 'reaffirm', surface='ontology_page')
        self.post('resolve', {'action': 'apply', 'expectedRevision': episode['revision'], 'content': AFTER['content']}, 409)

    def test_new_alignment_and_new_evidence_also_invalidate_old_proposal(self):
        episode = self.propose()
        a = self.onto.get_claim(self.claim['id'])['selfAlignment']
        AlignmentStore(self.onto).review(self.claim['id'], {'requestId': 'synthetic-alignment-newer', 'action': 'calibrate',
            'level': 0, 'framing': 'long_term', 'expectedRevision': a['revision'], 'claimVersion': a['claimVersion'], 'evidenceVersion': a['evidenceVersion']})
        self.post('resolve', {'action': 'apply', 'expectedRevision': episode['revision'], 'content': AFTER['content']}, 409)
        self.assertEqual(self.onto.get_claim(self.claim['id'])['selfAlignment']['level'], 0)

    def test_other_device_claim_and_deleted_material_fail_closed(self):
        foreign = self.convs.create_conversation(device_scope='foreign-device')
        c = self.onto.create_claim({'content': '另一设备的分享理解', 'section': 'ways', 'layer': 'self_declared'},
            [{'kind': 'conversation_turn', 'conversation_id': foreign['id'], 'quote': '分享'}], trust_state='confirmed')
        self.post('start', {**BEFORE, 'claimId': c['id'], 'claimUpdatedAt': c['updatedAt']}, 409)
        self.onto.add_evidence(self.claim['id'], [{'kind': 'material_span', 'material_id': 'missing-material', 'quote': '分享'}])
        self.post('suggest', {'claimId': self.claim['id']}, 409)
        self.provider.complete_json.assert_not_called()

    def test_missing_result_and_invalid_model_fail_safely(self):
        self.start()
        self.post('propose', {**AFTER, 'expectedRevision': 1}, 409)
        self.post('resolve', {'action': 'apply', 'expectedRevision': 1, 'content': AFTER['content']}, 409)
        self.outcome()
        self.provider.complete_json.side_effect = ProviderError('offline')
        self.post('suggest', {'expectedRevision': 1}, 503)
        self.post('propose', {**AFTER, 'expectedRevision': 1})  # manual still works

    def test_keep_and_defer_do_not_grade_or_repeat(self):
        e = self.propose()
        self.post('resolve', {'action': 'keep', 'expectedRevision': e['revision']})
        self.assertEqual(self.onto.get_claim(self.claim['id']), self.claim)
        self.post('suggest', {'expectedRevision': 3}, 409)
        self.assertEqual(self.client.get(self.url).json()['episode']['status'], 'kept')

    def test_slow_model_cannot_attach_to_new_state(self):
        def concurrent(_):
            self.outcome()
            return BEFORE
        self.provider.complete_json.side_effect = concurrent
        self.post('suggest', {'claimId': self.claim['id']}, 409)
        self.assertIsNone(LearningStore(self.onto).get(self.decision['id']))

    def test_synthetic_contexts_dont_collapse_or_become_desires(self):
        examples = [('被安排负责工作', 'current'), ('真心认同自主决定', 'long_term'), ('尚未实现的表达愿望', 'aspirational'), ('熟悉主题才愿意分享', 'context_only')]
        for text, framing in examples:
            with self.subTest(framing=framing):
                a = {**self.claim, 'content': text, 'contextual': {'framing': framing, 'situation': '合成情境'}}
                b = {**a, 'contextual': {'framing': 'other', 'situation': '另一情境'}}
                self.assertEqual(consolidate.judge_pair(self.provider, a, b)[0], 'unrelated')
        self.provider.complete_json.assert_not_called()

    def test_unknown_scope_is_not_lifetime_trait(self):
        raw = {'claims': [{'section': 'ways', 'layer': 'self_declared', 'predicate': 'prefers', 'content': '我今天不想公开分享', 'quote': '我今天不想公开分享', 'confidence': .9, 'scope_hint': 'unknown'}]}
        items = extract.validate(raw, user_text='我今天不想公开分享', prev_assistant=None)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].scope, 'context_only')

    def test_legacy_protected_review_cannot_egress_in_any_state(self):
        with self.growth._connect() as db:
            db.execute('UPDATE growth_decisions SET evidence_refs_json=? WHERE id=?',
                (json.dumps([json.dumps({'kind': 'local_only_decision', 'conversationId': self.conv['id']})]), self.decision['id']))
        # Simulate the old bug: private/long-term candidate with only review ancestry.
        derivative = self.onto.create_claim({'content': '公开分享复盘应该坚持充分准备', 'section': 'principles', 'layer': 'aspirational', 'export_allowed': True},
            [{'kind': 'review', 'decision_id': self.decision['id'], 'quote': '公开分享复盘应该坚持充分准备'}])
        external = FakeProvider()
        external.external = True
        clean = self.convs.create_conversation()
        for status in ('working', 'confirmed', 'retracted'):
            with self.subTest(status=status):
                if status == 'confirmed':
                    self.onto.transition(derivative['id'], 'confirm', surface='ontology_page')
                if status == 'retracted':
                    self.onto.transition(derivative['id'], 'retract', surface='ontology_page')
                assembled = self.assemble(external, clean)
                self.assertNotIn(derivative['content'], assembled.system)
                self.assertNotIn(derivative['content'], projection.render(self.onto)[1])
                self.assertNotIn(derivative['id'], [c['id'] for c in context_pack.exportable_claims(self.onto)])

    def test_new_review_stays_contextual_with_local_ancestry(self):
        self.start()
        decision = self.outcome()
        result = growth_hooks.on_review({'decisionId': decision['id'], 'lessons': ['公开分享复盘先看准备条件']}, decision, store=self.onto)
        candidate = self.onto.get_claim(result['created'][0])
        self.assertEqual(candidate['layer'], 'hypothesis')
        self.assertEqual(candidate['scope'], 'context_only')
        self.assertEqual(candidate['privacyLevel'], 'restricted')
        self.assertTrue(candidate['evidence'][0]['locator']['localOnly'])
        retry = growth_hooks.on_review({'decisionId': decision['id'], 'lessons': ['公开分享复盘先看准备条件']}, decision, store=self.onto)
        self.assertEqual(retry['created'], [])
        self.assertEqual(len(self.onto.get_claim(candidate['id'])['evidence']), 1)

    def test_global_consolidation_does_not_send_private_derivative(self):
        self.start()
        for text in ('我喜欢公开分享准备', '我不喜欢公开分享准备'):
            self.onto.create_claim({'content': text, 'section': 'ways', 'layer': 'self_declared'},
                [{'kind': 'review', 'decision_id': self.decision['id'], 'quote': text}], trust_state='confirmed')
        self.provider.external = True
        consolidate.run(store=self.onto, conv_store=self.convs, provider=self.provider)
        self.provider.complete_json.assert_not_called()

    def test_home_background_brief_excludes_protected_decision(self):
        self.start()
        overview = zhijun_home.build_home_overview(enqueue=False, ontology=self.onto, conversations=self.convs, growth=self.growth)
        self.provider.external = True
        self.provider.complete_json.return_value = {'headline': '合成来信', 'message': '仅公开素材', 'focusIds': ['claim:' + self.claim['id']]}
        from mindos.stores.routing_store import RoutingStore
        from mindos.chat_imports import service_info
        RoutingStore(self.onto).set_mode('default:global', 'online', service_info(self.provider)['id'])
        with patch('mindos.zhijun.provider.build_provider', return_value=self.provider):
            zhijun_home.generate_home_brief(overview['sourceHash'], store=self.onto, conv_store=self.convs)
        self.provider.complete_json.assert_not_called()
        self.assertTrue(RoutingStore(self.onto).pending('scope:global'))

    def test_history_and_background_work_remain_local_after_start(self):
        self.start()
        self.assertTrue(alignment.protected(self.conv['id'], self.convs, self.onto))
        self.assertTrue(SourcePolicy().decision_local(self.decision))
        report = jobs.run_job({'kind': 'summary', 'targetId': self.conv['id'], 'payload': {'conversationId': self.conv['id']}}, store=self.onto, conv_store=self.convs)
        self.assertEqual(report['state'], 'skipped')


if __name__ == '__main__':
    unittest.main()
