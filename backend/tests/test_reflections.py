"""V3 core loop with isolated real stores and a recording model boundary."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import test_task_routing as harness
from mindos import reflection_routes
from mindos.stores.reflection_store import ReflectionStore
from mindos.stores.memory_store import MemoryStore
from mindos.zhijun import reflections, memory, jobs
from mindos.zhijun.routing import Router, prepare_chat


class ReflectionTests(TestCase):
    tearDown = harness.RoutingTests.tearDown

    def setUp(self):
        harness.RoutingTests.setUp(self)
        self.ledger = ReflectionStore(self.onto)
        app = FastAPI()
        @app.middleware('http')
        async def scope(request, call_next):
            request.state.mindos_device_context = SimpleNamespace(device_id=request.headers.get('x-test-device'))
            return await call_next(request)
        app.include_router(reflection_routes.build_router())
        self.client = TestClient(app)
        self.old_cid = self.convs.create_conversation()['id']
        self.old = self.convs.append_message(self.old_cid, 'user', '上周谈新的渠道合作，对方主动讲清楚交付风险，我愿意继续推进合作。', meta={'routingSources':[]})
        with self.convs._connect() as db:
            db.execute('UPDATE messages SET created_at=? WHERE id=?', ((datetime.now(timezone.utc)-timedelta(days=8)).isoformat(), self.old['id']))
        self.old = self.convs.get_message(self.old['id'])
        self.current = self.convs.append_message(self.cid, 'user', '今天谈另一个新的供应合作，对方把交付问题讲清楚，我决定先做小范围合作。', meta={'routingSources':[]})
        self.reply = self.convs.append_message(self.cid, 'assistant', '我们可以先核对这次合作的具体范围。', meta={'routingSources':[]})
        self.raw = {'type':'pattern', 'title':'建立新合作时，你留意对方是否坦诚',
                    'observation':'你似乎会留意新合作伙伴是否愿意把问题讲清楚，再决定是否进一步投入。这个理解贴近你吗？',
                    'alternative':'也可能只是这两次合作的交付风险较大，需要额外核对。',
                    'confidence':.86, 'sensitivity':'ordinary', 'evidence':[
                        {'messageId':self.old['id'], 'quote':self.old['content']},
                        {'messageId':self.current['id'], 'quote':self.current['content']}]}
        self.local.result = {'candidate':self.raw}
        self.payload = {'conversationId':self.cid, 'messageId':self.current['id'], 'assistantId':self.reply['id'], 'localOnly':True}

    def generate(self):
        result = jobs.run_job({'kind':'reflection','payload':self.payload}, store=self.onto, conv_store=self.convs)
        self.assertEqual(result['state'], 'done', result)
        return self.ledger.get(result['reflectionId'])

    def review(self, item, action, note='', **over):
        payload = {'action':action,'note':note,'expectedRevision':item['revision'],'requestId':str(uuid.uuid4()),**over}
        return self.client.post('/api/mindos/reflections/'+item['id']+'/feedback',json=payload)

    def test_full_loop_correction_is_in_next_real_chat_prompt_and_survives_restart(self):
        item = self.generate()
        state = memory.attention(self.onto,self.convs,self.cid)
        self.assertEqual(state['reflection']['status'],'surfaced')
        self.assertIsNone(state['candidate'])
        note = '主要针对新合作伙伴，熟悉的人不用反复确认。'
        result = self.review(item,'contextual',note)
        self.assertEqual(result.status_code,200,result.text)
        updated = ReflectionStore(self.onto).get(item['id'])
        self.assertEqual(updated['feedback']['note'],note)
        router = Router(self.onto,self.convs,self.cid,provider=self.local)
        plan = prepare_chat(router,'我准备和熟悉的老伙伴继续合作，要注意哪些问题？')
        self.assertIn(note,plan.assembled.system)
        from mindos.zhijun.turn import run_turn
        events = list(run_turn(conversation_id=self.cid, content='和熟悉的老伙伴合作，有什么需要核对？',
            provider=self.local, request_id='reflection-next-turn', conv_store=self.convs, ontology=self.onto))
        self.assertFalse(any(kind == 'error' for kind, _ in events), events)
        self.assertIn(note,self.local.requests[-1].system)
        self.assertIn('不把观察泛化成人格',plan.assembled.system)
        self.assertTrue(any(r['kind']=='reflection' for r in plan.refs))
        self.assertEqual(self.onto.list_claims(),[], 'accepting a reflection must not create a Claim')
        self.assertEqual(memory.attention(self.onto,self.convs,self.cid)['reflection']['feedback']['note'],note)

    def test_accept_then_reject_invalidates_old_derived_history_and_next_prompt(self):
        item = self.generate()
        accepted = self.review(item,'accepted').json()
        ref = {'kind':'reflection','id':item['id'],'version':reflections.version(self.ledger.get(item['id']))}
        old = self.convs.append_message(self.cid,'assistant','OLD_REFLECTION_DERIVED_TEXT',meta={'routingSources':[ref]})
        self.assertEqual(self.review(accepted,'rejected','这个理解不贴近我').status_code,200)
        router = Router(self.onto,self.convs,self.cid,provider=self.local)
        self.assertTrue(any(s['blocked'] for s in router.resolve({'kind':'message','id':old['id']})))
        plan = prepare_chat(router,'新合作伙伴的沟通问题怎么处理？')
        self.assertNotIn(item['observation'],plan.assembled.system)
        self.assertFalse(any('OLD_REFLECTION_DERIVED_TEXT' in m['content'] for m in plan.assembled.messages))

    def test_evidence_edit_or_delete_invalidates_card_and_context(self):
        item=self.generate()
        self.review(item,'accepted')
        self.convs.update_message(self.old['id'],content='原来的记载需要修正')
        self.assertEqual(self.client.get('/api/mindos/reflections').json()['items'],[])
        self.assertEqual(self.review(self.ledger.get(item['id']),'accepted').status_code,409)
        self.assertEqual(reflections.chat_context(Router(self.onto,self.convs,self.cid),self.current['content'],self.local),('',[]))
        self.assertEqual(self.review(self.ledger.get(item['id']),'retired').status_code,200)

    def test_scope_isolation_for_read_and_feedback(self):
        item=self.generate()
        headers={'x-test-device':'other'}
        self.assertEqual(self.client.get('/api/mindos/reflections',headers=headers).json()['items'],[])
        self.assertEqual(self.client.post('/api/mindos/reflections/'+item['id']+'/feedback',headers=headers,
            json={'action':'accepted','expectedRevision':1,'requestId':'another-request'}).status_code,404)

    def test_feedback_idempotency_revision_and_condition_required(self):
        item=self.generate()
        self.assertEqual(self.review(item,'contextual').status_code,400)
        request='same-request-for-retry'
        first=self.review(item,'contextual','只限新伙伴',requestId=request)
        second=self.review(item,'contextual','只限新伙伴',requestId=request)
        self.assertEqual(first.json(),second.json())
        self.assertEqual(len(self.ledger.history(item['id'])),1)
        self.assertEqual(self.review(item,'rejected').status_code,409)
        self.assertEqual(self.review(item,'rejected',requestId=request).status_code,409)

    def test_observe_reject_retire_are_not_prompt_facts(self):
        item=self.generate()
        for action in ['observing','rejected','retired']:
            result=self.review(self.ledger.get(item['id']),action)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(reflections.chat_context(Router(self.onto,self.convs,self.cid),self.current['content'],self.local),('',[]))

    def test_manual_policy_and_insufficient_evidence_do_not_call_model(self):
        MemoryStore(self.onto).set_policy('global','manual',0)
        result=reflections.run_job(self.payload,self.onto,self.convs)
        self.assertEqual(result['reason'],'memory_policy')
        self.assertEqual(len(self.local.requests),0)
        MemoryStore(self.onto).set_policy('global','important',1)
        with self.convs._connect() as db:
            db.execute('UPDATE messages SET created_at=? WHERE id=?',(self.current['createdAt'],self.old['id']))
        result=reflections.run_job(self.payload,self.onto,self.convs)
        self.assertEqual(result['reason'],'insufficient_independent_evidence')
        self.assertEqual(len(self.local.requests),0)

    def test_ai_assisted_evidence_and_unauthorized_online_sources_are_excluded(self):
        self.convs.update_message(self.old['id'],meta={'replyAssistance':{'kind':'assisted'}})
        result=reflections.run_job(self.payload,self.onto,self.convs)
        self.assertEqual(result['reason'],'insufficient_independent_evidence')
        router=Router(self.onto,self.convs,self.cid,provider=self.online)
        self.assertEqual(reflections.eligible_messages(router,self.current['content'],self.online),[])

    def test_rejected_topic_not_rephrased_after_cooldown(self):
        item=self.generate()
        self.review(item,'rejected')
        with self.onto._connect() as db:
            db.execute('UPDATE reflections SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(days=3)).isoformat(),item['id']))
        later=self.convs.append_message(self.cid,'user','我想继续讨论新的供应合作中，对方把交付问题讲清楚的重要性。')
        reply=self.convs.append_message(self.cid,'assistant','可以。')
        result=reflections.run_job({**self.payload,'messageId':later['id'],'assistantId':reply['id']},self.onto,self.convs)
        self.assertEqual(result['reason'],'known_topic')
        self.assertEqual(len(self.local.requests),1)

    def test_quality_gate_rejects_fabricated_single_event_and_sensitive_candidates(self):
        router=Router(self.onto,self.convs,self.cid)
        inputs=reflections.eligible_messages(router,self.current['content'],self.local)
        self.assertIsNotNone(reflections.validate_candidate(self.raw,inputs))
        for change in [{'confidence':.4},{'sensitivity':'sensitive'},{'type':'tension'},
                       {'evidence':[self.raw['evidence'][0]]},
                       {'evidence':[self.raw['evidence'][0]]*2},
                       {'evidence':[self.raw['evidence'][0],{'messageId':self.current['id'],'quote':'并不存在的原话不能作为证据'}]}]:
            with self.subTest(change=change):
                self.assertIsNone(reflections.validate_candidate({**self.raw,**change},inputs))

    def test_duplicate_generation_and_attention_refresh_are_stable(self):
        item=self.generate()
        self.assertEqual(reflections.run_job(self.payload,self.onto,self.convs)['reason'],'duplicate_turn')
        first=memory.attention(self.onto,self.convs,self.cid)
        self.assertEqual(first,memory.attention(self.onto,self.convs,self.cid))
        self.assertEqual(len(self.ledger.list('global')),1)

    def test_purge_clears_observations_and_feedback(self):
        item=self.generate();self.review(item,'accepted')
        self.onto.purge_all()
        self.assertEqual(self.ledger.list('global'),[])
        self.assertEqual(self.ledger.history(item['id']),[])

    def test_revoking_online_permission_stops_reflection_and_its_ancestors(self):
        item=self.generate();self.review(item,'contextual','只限新合作伙伴')
        from mindos.zhijun.routing import service_info
        service=service_info(self.online)['id']
        self.store.set_policy('global',enabled=True,service=service,service_name='synthetic',include_files=False,
                              purposes=['alignment','chat'],expected_revision=0)
        router=Router(self.onto,self.convs,self.cid,provider=self.online)
        self.assertTrue(reflections.chat_context(router,self.current['content'],self.online)[1])
        self.store.set_policy('global',enabled=False,service=service,service_name='synthetic',include_files=False,
                              purposes=['alignment','chat'],expected_revision=1)
        self.assertEqual(reflections.chat_context(router,self.current['content'],self.online),('',[]))

    def test_no_proactive_policy_prevents_generation_and_hides_pending_card(self):
        item=self.generate()
        policy={'controls':[{'id':'pause','control':'no_proactive','text':'不要主动提醒'}]}
        with patch('mindos.zhijun.charter_policy.scope_policy',return_value=policy):
            self.assertFalse(reflections.automatic_allowed(self.onto,self.convs,self.cid))
            self.assertEqual(reflections.run_job(self.payload,self.onto,self.convs)['reason'],'memory_policy')
            self.assertIsNone(memory.attention(self.onto,self.convs,self.cid)['reflection'])

    def test_opaque_legacy_evidence_does_not_produce_an_unusable_observation(self):
        with self.convs._connect() as db:
            db.execute("UPDATE messages SET meta_json='{}' WHERE id=?",(self.old['id'],))
        result=reflections.run_job(self.payload,self.onto,self.convs)
        self.assertEqual(result['reason'],'insufficient_independent_evidence')
        self.assertEqual(self.local.requests,[])

    def test_optional_reflection_is_queued_only_after_a_successful_turn(self):
        from mindos.zhijun.turn import run_turn
        from mindos.zhijun.provider import ProviderError
        with patch.dict('os.environ',{'ZHIJUN_EXTRACTION':'1'}):
            events=list(run_turn(conversation_id=self.cid,content='我在新的合作里一直重视把真实问题坦诚讲清楚，这能帮助我判断是否继续投入。',
                provider=self.local,request_id='reflection-trigger-turn',conv_store=self.convs,ontology=self.onto))
            self.assertFalse(any(kind=='error' for kind,_ in events),events)
            with self.onto._connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM ontology_jobs WHERE kind='reflection'").fetchone()[0],1)
            self.local.error=ProviderError('synthetic failure',code='PROVIDER_TIMEOUT')
            list(run_turn(conversation_id=self.cid,content='这次新的合作里我仍然重视坦诚讲清楚具体交付风险，便于决定是否推进。',
                provider=self.local,request_id='reflection-failed-turn',conv_store=self.convs,ontology=self.onto))
            with self.onto._connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM ontology_jobs WHERE kind='reflection'").fetchone()[0],1)
