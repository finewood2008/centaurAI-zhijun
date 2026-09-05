import { createApp, defineComponent, h, ref } from 'vue'
import RoutingPanel from '../../src/components/conversation/RoutingPanel.vue'
import MemoryPending from '../../src/components/conversation/MemoryPending.vue'
import RoutingConsent from '../../src/components/conversation/RoutingConsent.vue'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'

// Fail closed: all APIs live in memory; no fallback to the running application.
const cid = 'fixture-memory-recovery', log = ref<string[]>([]), count = ref(2)
let revision = 1, includeCharter = false, granted = false, paused = true, expired = false, failReview = false
const claims = [
  { id: 'fixture-c1', content: '我做重大决定前，希望先了解风险。', section: 'principles', layer: 'self_declared', trustState: 'working', confidence: .9, evidence: [{ conversationId: cid, messageId: 'u1', quote: '我希望先了解风险，再做决定。' }] },
  { id: 'fixture-c2', content: '我希望为陪伴家人留出稳定的时间。', section: 'direction', layer: 'aspirational', trustState: 'working', confidence: .9, evidence: [{ conversationId: cid, messageId: 'u2', quote: '我希望之后每周能有固定的家庭时间。' }] },
]
const state = () => ({ mode: { mode: 'online', revision: 1, service: 'fixture-service' }, service: { id: 'fixture-service', name: '合成服务（不会联网）', model: 'fixture', external: true }, defaultAuthorization: { active: true, revision, serviceName: '合成服务', includeFiles: false, includeCharter }, handlingPreference: { active: false, revision: 0, action: 'omit' }, pending: paused ? [{ task_key: 'extract_turn', preview_id: 'fixture-preview', count: 8, reason: 'consent_required', detail: '人生章程与草稿尚未授权用于个人理解，8 轮对话待整理。', previewExpired: expired }] : [] })
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin)
  const path = url.pathname, method = init?.method || 'GET', body = init?.body ? JSON.parse(String(init.body)) : {}
  log.value.unshift(`${method} ${path}`)
  const base = `/api/mindos/conversations/${cid}`
  if (method === 'GET' && path === base + '/routing') return response(state())
  if (method === 'PUT' && path === base + '/routing/default-consent') {
    includeCharter = body.includeCharter === true; revision++; return response(state())
  }
  if (method === 'GET' && path.endsWith('/routing/pending/fixture-preview')) return response({ revision: 'fixture-preview', conversationId: cid, purpose: 'extract_turn', purposeLabel: '个人理解', service: state().service, missing: includeCharter || granted ? [] : ['charter'], blocked: [], excluded: [], reason: '合成授权预览', sources: [{ key: 'charter', title: '合成人生章程与草稿', text: '先了解风险，再做决定。', kind: 'charter', version: 'version-one', blocked: '' }], request: { system: '合成人生章程：先了解风险，再做决定。', messages: [{ role: 'user', content: '我希望先了解风险，再做决定。' }] } })
  if (method === 'POST' && path === base + '/routing/grant') { granted = true; return response({}) }
  if (method === 'POST' && path === base + '/routing/resume') {
    if (expired) { expired = false; return response({ queuedCount: 8, pendingCount: 0 }) }
    if (granted || includeCharter || body.localOnly) paused = false
    return response({ queuedCount: 8, pendingCount: paused ? 8 : 0 })
  }
  if (method === 'GET' && path === base + '/memory/pending') return response({ items: claims.map((claim, i) => ({ topicId: 'topic-' + i, claim })), total: claims.length })
  if (method === 'POST' && (path === base + '/memory/pending-dismiss' || /^\/api\/mindos\/ontology\/claims\/fixture-c[12]\/review$/.test(path))) {
    if (failReview) { failReview = false; return response({ detail: { code: 'fixture_failure', detail: '合成保存失败，你修改的文字仍保留。' } }, 503) }
    const id = body.claimId || path.split('/')[5]
    const index = claims.findIndex(c => c.id === id)
    const claim = claims[index]
    if (index >= 0) claims.splice(index, 1)
    count.value = claims.length; return response({ claim: { ...claim, trustState: 'confirmed' } })
  }
  return response({ detail: { code: 'UNMOCKED_REQUEST', detail: '隔离测试拒绝未模拟请求' } }, 400)
}
const Root = defineComponent({ setup() {
  const input = ref('我的未发送文字一直保留。'), panel = ref<InstanceType<typeof RoutingPanel> | null>(null)
  return () => h('main', { class: 'memory-fixture' }, [
    h('p', { class: 'fixture-note' }, '隔离验收 · 全部接口由内存模拟，不修改个人记录，不调用模型。'),
    h('header', [h('h1', '聊聊最近的选择'), h('div', { class: 'fixture-tools' }, [h(RoutingPanel, { ref: panel, conversationId: cid }), h(MemoryPending, { conversationId: cid, pendingCount: count.value })])]),
    h('article', [h('p', '你：我最近在考虑怎样兼顾事业与家庭。'), h('p', '知君：我们可以先从眼下最需要决定的一件事聊起。你现在面临什么具体选择？'), h('p', '阅读正文仍是主区域。只有你主动点击工具入口时，才打开待办详情。')]),
    h('textarea', { 'aria-label': '合成输入框', value: input.value, rows: 4, onInput: (e: Event) => { input.value = (e.target as HTMLTextAreaElement).value } }),
    h('details', [h('summary', '测试控制与请求记录'), h('button', { onClick: () => { expired = true; paused = true; void panel.value?.refresh() } }, '模拟过期预览'), h('button', { onClick: () => { failReview = true } }, '下次核对保存失败'), h('pre', log.value.slice(0, 20).join('\n'))]),
    h(RoutingConsent),
  ])
} })
const style = document.createElement('style')
style.textContent = '.memory-fixture{max-width:980px;margin:30px auto;padding:0 18px;font:14px/1.8 sans-serif;box-sizing:border-box}.fixture-note{font-size:12px;color:#777}.memory-fixture header{display:flex;flex-wrap:wrap;align-items:start;justify-content:space-between;gap:14px}.memory-fixture h1{font-size:22px}.fixture-tools{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.memory-fixture article{min-height:290px;padding:25px 0}.memory-fixture textarea{box-sizing:border-box;width:100%;font:inherit;padding:14px;border:1px solid #ddd3c9;border-radius:12px;background:white}.memory-fixture details{margin-top:24px;font-size:12px}.memory-fixture pre{white-space:pre-wrap;overflow-wrap:anywhere}.memory-fixture button{cursor:pointer}.memory-fixture details button{margin:10px 10px 0 0}@media(max-width:520px){.memory-fixture{margin:14px auto;padding:0 12px}.memory-fixture article{min-height:220px}}'
document.head.append(style)
createApp(Root).mount('#app')
