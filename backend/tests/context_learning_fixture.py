"""Disposable API fixture for the conversational experience-calibration UI."""
# ruff: noqa: E402 -- isolated data root must be configured before app imports.
import os
import tempfile

root = tempfile.TemporaryDirectory(prefix='zhijun-learning-e2e-')
os.environ.update(CENTAURAI_DATABASE_DATA_ROOT=root.name, ZHIJUN_PROVIDER='fake',
                  ZHIJUN_MATERIAL_EVIDENCE='0', ZHIJUN_EXTRACTION='0')

from fastapi import FastAPI
import uvicorn
from mindos import ontology, conversations, zhijun_status, zhijun_onboarding, chat_import_routes, growth, learning_routes
from mindos.stores.ontology_store import OntologyStore
from mindos.stores.conversation_store import ConversationStore
from mindos.stores.growth_store import GrowthStore
from mindos.zhijun.provider import FakeProvider, OllamaProvider
from tests.test_context_learning import BEFORE, AFTER

zhijun_onboarding.apply_action(zhijun_onboarding.OnboardingCommand(action='skip'))
onto, convs, gs = OntologyStore.instance(), ConversationStore.instance(), GrowthStore.instance()
claim = onto.create_claim({'content': '我不喜欢公开分享', 'section': 'ways', 'layer': 'self_declared'},
                         [{'kind': 'user_edit', 'quote': '我不喜欢公开分享'}], trust_state='confirmed', trust_origin='user_created')
decision = gs.create_decision({'title': '合成案例：公开分享邀请', 'context': '熟悉主题，有准备时间的公开分享', 'choice': '参加',
    'options': ['参加', '不参加'], 'rationale': '尝试看看', 'confidence': 50, 'expectedOutcome': '希望交流有所帮助',
    'reviewAt': None, 'relatedEntityIds': [], 'evidenceRefs': []})
conv = convs.create_conversation(title='情境校准测试（合成记录）', mode='review', decision_id=decision['id'])
convs.append_message(conv['id'], 'assistant', '这次可以用一个具体经历，核对过去的理解。', provider='template', model='template')

class FixtureProvider(FakeProvider):
    def complete_json(self, request):
        return AFTER if '事后比较' in request.messages[0]['content'] else BEFORE

if os.environ.get('ZHIJUN_TEST_REAL_LOCAL') == '1':
    learning_routes.local_provider = lambda **kw: OllamaProvider('http://127.0.0.1:11434', 'qwen3.5:9b', timeout=55, keep_alive=60, num_ctx=8192)
else:
    learning_routes.local_provider = lambda **kw: FixtureProvider()

app = FastAPI()
for router in (ontology.router, conversations.router, zhijun_status.router, zhijun_onboarding.router, growth.router, chat_import_routes.build_router(lambda: None)):
    app.include_router(router)

@app.get('/api/mindos/access-context')
def access():
    return {'mode': 'local_debug', 'localDebug': True}

@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': 'context-learning-fixture', 'model': 'synthetic'}

@app.get('/__fixture')
def info():
    return {'conversationId': conv['id'], 'decisionId': decision['id'], 'claimId': claim['id'], 'claimUpdatedAt': claim['updatedAt']}

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8771, log_level='warning')
