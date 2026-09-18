"""Immersive shell acceptance: synthetic SQLite and recorded providers only (8778).

Copy of chat_simplification_fixture with what the day stream needs: today's chat (local mode with
protected history so the first send asks for confirmation), one conversation backdated to yesterday and
one to last week (day headers), sixty-plus older ones so the first page of thirty has more to load on
scroll-up, seeded working claims so the list's outcomes brief is non-zero (留 seal), a three-paragraph
reply so the paced reveal has several steps, and the home router for today's letter.
Run from backend/: .venv/bin/python -m tests.immersive_fixture
"""
import os
import tempfile

private = tempfile.TemporaryDirectory(prefix='zhijun-immersive-secrets-')
for key in ('CENTAUR_METADATA_DB', 'CENTAUR_GBRAIN_HOME', 'CENTAUR_MCP_DATA_DIR', 'CENTAUR_MCP_CONFIG_DIR'):
    os.environ.pop(key, None)
os.environ['CENTAUR_SECRET_STORE_DIR'] = private.name

from datetime import datetime, timedelta, timezone  # noqa: E402
from tests.chat_send_fixture import app, convs, online, local, store, service, cases, onto  # noqa: E402
from mindos import zhijun_status, zhijun_home  # noqa: E402
from mindos.zhijun.provider import TextDelta, Done, Usage  # noqa: E402
from pathlib import Path  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
import uvicorn  # noqa: E402

PARAGRAPHS = [
    '合成回答：先核对这次的目标。这一段解释为什么要先看目标，再谈别的。',
    '第二段：把可以调整的部分和不能承诺的部分分开写，避免把工作安排当成长期追求。',
    '第三段：最后留一个可以撤回的空间，等你补充之后再收口。',
]


def reply(request):
    online.requests.append(request)
    if online.error:
        raise online.error
    for index, paragraph in enumerate(PARAGRAPHS):
        yield TextDelta(('\n\n' if index else '') + paragraph)
    yield Usage(20, 30)
    yield Done('stop')


online.stream = reply
local.stream = reply
zhijun_status.provider_status = lambda: {'provider': 'openai', 'model': 'synthetic', 'external': True, 'configured': True, 'error': None}
# 今日来信只用模板：不让后台润色去抢模型通道
zhijun_home.generate_home_brief = lambda *args, **kwargs: {'state': 'skipped', 'reason': 'immersive-fixture'}

now = datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def backdate(conversation_id: str, moment: datetime) -> str:
    stamp = iso(moment)
    with convs._connect() as db:
        db.execute('UPDATE conversations SET created_at=?, updated_at=?, last_message_at=? WHERE id=?', (stamp, stamp, stamp, conversation_id))
        db.execute('UPDATE messages SET created_at=? WHERE conversation_id=?', (stamp, conversation_id))
    return stamp


def seed_outcome(conversation_id: str, message_id: str, content: str, quote: str) -> str:
    claim = onto.create_claim({'content': content, 'section': 'matters', 'layer': 'self_declared'},
                              [{'kind': 'conversation_turn', 'conversation_id': conversation_id, 'message_id': message_id, 'quote': quote}],
                              trust_state='working', trust_origin='model')
    return claim['id']


seeded = {}
backdated = {}

# 昨天：一来一回，带一条待确认理解 → 列表里的产出摘要非零 → 块尾留印
yesterday = convs.create_conversation(title='合成：昨天聊过的事')
y_user = convs.append_message(yesterday['id'], 'user', '昨天我在想要不要接那个项目，主要担心顾不上现有客户。', meta={'routingSources': []})
convs.append_message(yesterday['id'], 'assistant', '你更在意能不能照顾好现有客户。要不要先列出现有客户需要的投入？', meta={'routingSources': []})
seeded['yesterday'] = '合成理解：我更在意能不能照顾好现有客户'
seed_outcome(yesterday['id'], y_user['id'], seeded['yesterday'], y_user['content'])
backdated['yesterday'] = backdate(yesterday['id'], now - timedelta(days=1))
cases['yesterday'] = yesterday['id']

# 上周：一来一回
lastweek = convs.create_conversation(title='合成：上周的一段')
convs.append_message(lastweek['id'], 'user', '上周我想先把团队分工理一理。', meta={'routingSources': []})
convs.append_message(lastweek['id'], 'assistant', '分工可以从每个人已经在做的事开始写。', meta={'routingSources': []})
backdated['lastweek'] = backdate(lastweek['id'], now - timedelta(days=7))
cases['lastweek'] = lastweek['id']

# 更早的 64 段（每天两段）：第一页 30 条装不下，上滑才读
older = []
for n in range(64):
    conversation = convs.create_conversation(title=f'合成：更早的第 {n + 1} 段')
    convs.append_message(conversation['id'], 'user', f'更早的一句话，第 {n + 1} 段。', meta={'routingSources': []})
    stamp = backdate(conversation['id'], now - timedelta(days=8 + n // 2, hours=n % 2))
    older.append(conversation['id'])
backdated['oldest'] = stamp

# 今天的对话：本地模式 + 受保护的旧历史 → 第一次发送要先确认资料使用；带一条待确认理解 → 一轮后留印
today = convs.create_conversation(title='合成：今天的对话')
store.set_mode(today['id'], 'local', '')
t_msg = convs.append_message(today['id'], 'assistant', 'PROTECTED_TODAY_HISTORY', meta={'localOnlyDerived': True})
seeded['today'] = '合成理解：我在准备与合伙人的沟通'
seed_outcome(today['id'], t_msg['id'], seeded['today'], '我在准备与合伙人的沟通')
cases['today'] = today['id']

app.include_router(zhijun_home.router)
app.router.routes[:] = [r for r in app.router.routes if getattr(r, 'path', '') not in ('/api/health', '/__fixture', '/mindos/{path:path}')]


@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': 'immersive-fixture'}


@app.get('/__fixture')
def info():
    return {'synthetic': True, 'cases': cases, 'seeded': seeded, 'backdated': backdated, 'older': len(older), 'paragraphs': PARAGRAPHS,
            'onlineRequests': len(online.requests), 'localRequests': len(local.requests),
            'payloads': [{'system': r.system, 'messages': r.messages} for r in online.requests], 'defaultMode': store.mode('default:global')}


@app.get('/mindos/{path:path}')
def preview(path: str):
    dist = Path(__file__).resolve().parents[2] / 'frontend/mindos-web/dist/index.html'
    banner = '<div role="note" style="position:fixed;bottom:4px;right:8px;z-index:9999;background:#f4f1e9;padding:4px;font:11px sans-serif">隔离验收 · 合成数据</div>'
    return HTMLResponse(dist.read_text().replace('</body>', banner + '</body>'))


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8778, log_level='warning')
