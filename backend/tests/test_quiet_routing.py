"""Remembered handling never grants data access; assert actual outbound payloads."""
import json
import unittest
from unittest.mock import patch

from tests.test_task_routing import RoutingTests as Fixture
from mindos.stores.routing_store import RoutingStore
from mindos.stores.alignment_store import AlignmentStore
from mindos.zhijun.routing import Router, GuardedProvider, service_info
from mindos.zhijun.provider import ChatRequest, ProviderError


class QuietRoutingTests(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    enable = Fixture.enable
    claim = Fixture.claim
    preview = Fixture.preview
    send = Fixture.send
    grant = Fixture.grant

    def preference(self, action='omit', enabled=True):
        r = self.client.put(self.url + '/routing/handling', json={
            'enabled': enabled, 'action': action, 'serviceId': service_info(self.online)['id'],
            'expectedRevision': self.store.handling('global')['revision']})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['handlingPreference']

    def test_default_off_and_persistent_scoped_preference(self):
        self.assertFalse(self.store.handling('global')['enabled'])
        saved = self.preference()
        reopened = RoutingStore(self.onto)
        self.assertEqual(reopened.handling('global')['revision'], saved['revision'])
        self.assertFalse(reopened.handling('other-device')['enabled'])
        self.assertEqual(self.client.put(self.url + '/routing/handling', json={
            'enabled': False, 'expectedRevision': 0}).status_code, 409)
        self.assertTrue(reopened.handling('global')['enabled'])

    def test_repeated_invalid_legacy_history_is_excluded_without_requesting_consent(self):
        self.enable()
        m = self.convs.append_message(self.cid, 'assistant', 'SECRET_LEGACY_TEXT', meta={'localOnlyDerived': True})
        AlignmentStore(self.onto).status(self.cid, local_only=True, status='paused')
        for text in ('刚才那件事怎样继续？', '接着说一下'):
            body, preview = self.preview(text)
            self.assertFalse(preview['missing'])
            self.assertTrue(any(e['id'] == m['id'] for e in preview['excluded']))
            self.assertTrue(preview['handlingNotice'])
            self.assertEqual(self.send(body, preview).status_code, 200)
        self.assertNotIn('SECRET_LEGACY_TEXT', json.dumps([r.messages for r in self.online.requests]))
        self.assertIsNotNone(self.convs.get_message(m['id']))

    def test_changed_ancestor_excludes_whole_derived_answer(self):
        c = self.claim(); self.enable()
        r = Router(self.onto, self.convs, self.cid)
        ref = r.resolve(r.ref('claim', c['id']))[0]['ref']
        self.convs.append_message(self.cid, 'assistant', 'SECRET_DERIVED_ANSWER', meta={'routingSources': [ref]})
        self.onto.add_evidence(c['id'], [{'kind': 'user_edit', 'quote': '变更来源'}])
        body, preview = self.preview('刚才怎么理解？')
        self.assertFalse(preview['missing'])
        self.send(body, preview)
        self.assertNotIn('SECRET_DERIVED_ANSWER', json.dumps(self.online.requests[-1].messages))

    def test_omit_skips_unapproved_sources_but_keeps_approved_sources(self):
        c = self.claim(); self.enable(); self.preference()
        allowed = self.convs.append_message(self.cid, 'assistant', 'ALLOWED_RECENT_TEXT', meta={
            'routingOrigin': {'service': service_info(self.online)['id']}, 'routingSources': []})
        body, preview = self.preview('星桥项目工作安排')
        self.assertFalse(preview['missing'])
        self.assertTrue(any(e['id'] == c['id'] for e in preview['excluded']))
        self.send(body, preview)
        self.assertIn('ALLOWED_RECENT_TEXT', json.dumps(self.online.requests[-1].messages))
        self.assertNotIn(c['content'], self.online.requests[-1].system)
        self.assertIn('先简短说明看不到该部分', self.online.requests[-1].system)
        with self.onto._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM routing_grants').fetchone()[0], 0)

    def test_disabled_preference_asks_again_for_valid_unapproved_content(self):
        self.claim(); self.enable(); self.preference(); self.preference(enabled=False)
        self.assertTrue(self.preview('星桥项目工作安排')[1]['missing'])

    def test_local_preference_uses_local_only_when_needed(self):
        self.claim(); self.enable(); self.preference('local')
        body, plain = self.preview('怎样打包行李')
        self.assertTrue(plain['service']['external'])
        body, restricted = self.preview('星桥项目工作安排')
        self.assertFalse(restricted['service']['external'])
        self.assertIn('默认方式在本地处理', restricted['handlingNotice'])
        self.assertEqual(self.send(body, restricted).status_code, 200)
        self.assertEqual(len(self.local.requests), 1)
        self.assertEqual(self.online.requests, [])
        self.assertIn('星桥项目只是工作安排', self.local.requests[-1].system)

    def test_unrelated_old_history_does_not_force_local(self):
        self.enable(); self.preference('local')
        self.convs.append_message(self.cid, 'assistant', 'SECRET_OLD_PROFILE', meta={'localOnlyDerived': True})
        AlignmentStore(self.onto).status(self.cid, local_only=True, status='paused')
        self.assertTrue(self.preview('怎样打包行李')[1]['service']['external'])
        self.assertFalse(self.preview('接着讨论刚才的事情')[1]['service']['external'])

    def test_local_unavailable_does_not_fall_back_online(self):
        self.claim(); self.enable(); self.preference('local')
        self.local.error = ProviderError('synthetic local unavailable')
        body, preview = self.preview('星桥项目工作安排')
        self.assertIn('event: error', self.send(body, preview).text)
        self.assertEqual(self.online.requests, [])

    def test_preference_change_invalidates_already_prepared_external_request(self):
        self.enable()
        body, preview = self.preview('怎样打包行李')
        self.preference()
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(self.online.requests, [])

    def test_service_change_requires_reselection(self):
        self.claim(); self.enable(); self.preference()
        self.online._base_url = 'https://another-synthetic.invalid/v1'
        self.enable()
        state = self.client.get(self.url + '/routing').json()
        self.assertTrue(state['handlingPreference']['serviceChanged'])
        self.assertFalse(state['handlingPreference']['active'])
        self.assertTrue(self.preview('星桥项目工作安排')[1]['missing'])

    def test_omit_does_not_strip_assisted_text_ancestry(self):
        self.enable(); self.preference()
        r = Router(self.onto, self.convs, self.cid)
        m = self.convs.append_message(self.cid, 'user', 'SENSITIVE_SOURCE', meta={'routingSources': []})
        with patch('mindos.zhijun.reply_assistance.resolve_input', return_value=({'kind': 'assisted'}, [r.ref('message', m['id'])])):
            body, preview = self.preview('我想先理清需求')
            self.assertTrue(preview['missing'])
            self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(self.online.requests, [])

    def test_background_requests_do_not_reuse_handling_as_consent(self):
        c = self.claim(); self.enable(); self.preference()
        r = Router(self.onto, self.convs, self.cid)
        guard = GuardedProvider(r, self.online, 'summarize_conversation', [r.ref('claim', c['id'])], background=True)
        from fastapi import HTTPException
        with self.assertRaises(HTTPException): guard.complete_json(ChatRequest(system='summary', messages=[]))
        self.assertEqual(self.online.requests, [])


if __name__ == '__main__': unittest.main()
