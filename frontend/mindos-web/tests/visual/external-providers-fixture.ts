import { createApp, defineComponent, h, ref } from 'vue'
import ExternalProvidersPanel from '../../src/components/conversation/ExternalProvidersPanel.vue'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'

// Every API request is handled in memory; unknown requests fail closed. No proxy, real settings or model calls.
let version = 4
let providers = [
  { id: 'alpha', name: '团队模型服务', revision: 1, baseUrl: 'https://alpha.example.invalid/v1', model: 'alpha-small', apiKeyConfigured: true, active: true, pendingActivation: false },
  { id: 'beta', name: '备用模型服务', revision: 1, baseUrl: 'https://beta.example.invalid/v1', model: 'beta-fast', apiKeyConfigured: true, active: false, pendingActivation: false },
]
const log = ref<string[]>([]), held = ref<Array<() => void>>([])
let delayList = false, delayModels = false, failModels = false
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
const reject = (message: string, status = 409) => response({ detail: { code: status === 409 ? 'conflict' : 'models_unavailable', message } }, status)
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin)
  const prefix = '/api/system/models/external-providers'
  if (!url.pathname.startsWith(prefix)) return reject('隔离测试拒绝未模拟请求', 400)
  const method = init?.method || 'GET'
  const body = init?.body ? JSON.parse(String(init.body)) : {}
  const tail = url.pathname.slice(prefix.length).split('/').filter(Boolean)
  const profile = providers.find(p => p.id === tail[0])
  const activeId = providers.find(p => p.active)?.id || null
  log.value.unshift(`${method} ${tail.join('/') || '供应商'}${body.apiKey ? ' · Token 已提交（不记录内容）' : ''}`)
  if (!tail.length && method === 'GET') {
    const snapshot = JSON.parse(JSON.stringify({ providers, activeProviderId: activeId, chatRevision: version }))
    if (delayList) { delayList = false; await new Promise<void>(resolve => held.value.push(resolve)) }
    return response(snapshot)
  }
  if (!tail.length && method === 'POST') {
    if (!body.apiKey) return reject('需要 Token', 422)
    const created = { id: 'custom-' + (providers.length + 1), name: body.name, baseUrl: body.baseUrl, model: '', apiKeyConfigured: true, active: false, pendingActivation: false, revision: 1 }
    providers.push(created)
    return response(created)
  }
  if (!profile) return reject('供应商不存在', 404)
  if ((body.revision ?? Number(url.searchParams.get('revision'))) !== profile.revision) return reject('供应商版本已变化')
  if (tail[1] === 'models') {
    const value = profile.id === 'alpha' ? ['alpha-small', 'alpha-strong'] : profile.id === 'beta' ? ['beta-fast', 'beta-pro'] : ['custom-fast', 'custom-thinking']
    const snapshot = { models: value, providerId: profile.id, revision: profile.revision }
    if (delayModels) { delayModels = false; await new Promise<void>(resolve => held.value.push(resolve)) }
    return failModels ? reject('此供应商没有模型列表', 503) : response(snapshot)
  }
  if (tail[1] === 'activate') {
    if (body.chatRevision !== version) return reject('默认配置已变化')
    providers.forEach(p => { p.active = p.id === profile.id })
    profile.model = body.model; profile.revision += 1; profile.pendingActivation = false; version += 1
    return response({ provider: profile, chat: { revision: version, provider: 'openai', externalEnabled: true, baseUrl: profile.baseUrl, model: profile.model, apiKeyConfigured: true, apiKeyHint: null, timeoutSeconds: 60, totalBudgetSeconds: 90, fallbackOllama: false, source: 'runtime_settings', effectiveProvider: 'openai' } })
  }
  if (method === 'PUT') {
    if (body.baseUrl !== profile.baseUrl && !body.apiKey) return reject('地址改变需要重新输入 Token', 422)
    Object.assign(profile, { name: body.name, baseUrl: body.baseUrl, revision: profile.revision + 1, pendingActivation: profile.active })
    return response(profile)
  }
  if (method === 'DELETE') {
    if (profile.active) return reject('默认项不可删除')
    providers = providers.filter(p => p.id !== profile.id)
    return response({ deleted: true })
  }
  return reject('隔离测试拒绝未模拟请求', 400)
}

createApp(defineComponent({ setup() {
  const chatRevision = ref(4)
  return () => h('main', { style: 'max-width:740px;margin:24px auto;padding:20px;background:var(--ws-bg-color,#fffcf6);font:14px/1.6 sans-serif' }, [
    h('h1', { style: 'font-size:22px;margin:0 0 14px' }, '在线供应商 · 隔离测试'),
    h('p', { style: 'font-size:12px;color:#777' }, '全部接口在当前页面模拟，不会修改真实设置或联系任何供应商。'),
    h(ExternalProvidersPanel, { chatRevision: chatRevision.value, onActivated: (chat: { revision: number }) => { chatRevision.value = chat.revision } }),
    h('details', { style: 'margin-top:24px;font-size:12px' }, [h('summary', '测试控制与请求记录'),
      h('div', { style: 'display:flex;flex-wrap:wrap;gap:8px;margin:12px 0' }, [
        h('button', { onClick: () => { version += 1; log.value.unshift('模拟其他窗口更改默认配置') } }, '制造版本冲突'),
        h('button', { onClick: () => { delayList = true } }, '下一次供应商读取延迟'),
        h('button', { onClick: () => { delayModels = true } }, '下一次模型列表延迟'),
        h('button', { onClick: () => { failModels = !failModels; log.value.unshift(failModels ? '列表失败已开启' : '列表失败已关闭') } }, '切换模型列表失败'),
        h('button', { onClick: () => { held.value.splice(0).forEach(resolve => resolve()) } }, `释放延迟响应（${held.value.length}）`),
      ]),
      h('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, log.value.join('\n')),
    ]),
  ])
} })).mount('#app')
