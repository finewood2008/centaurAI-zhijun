import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import ts from 'typescript'

const src = fileURLToPath(new URL('../src/', import.meta.url))
function modules() {
  const cache = new Map()
  function load(filename) {
    if (!extname(filename)) filename += '.ts'
    if (filename.endsWith('.json')) return JSON.parse(readFileSync(filename, 'utf8'))
    if (cache.has(filename)) return cache.get(filename).exports
    const module = { exports: {} }; cache.set(filename, module)
    const code = ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText
    const native = createRequire(filename)
    const req = name => name.startsWith('.') ? load(resolve(dirname(filename), name)) : name.startsWith('@/') ? load(resolve(src, name.slice(2))) : native(name)
    new Function('require', 'module', 'exports', code)(req, module, module.exports)
    return module.exports
  }
  return name => load(resolve(src, name))
}
const bytes = value => new TextEncoder().encode(value)

test('navigation reuses only ready hints and invalidates on box changes and onboarding writes', async () => {
  const load = modules(), scope = load('shared/productScope.ts')
  scope.enableDesktopProduct(); scope.setProductScope('box-a')
  let gets = 0, writesFail = false, state = 'ready', conversationId = null
  load('services/transport.ts').installProductTransport(async (_path, init) => {
    if (init.method === 'POST') {
      if (writesFail) throw new Error('lost write response')
      state = 'not_started'
    } else gets++
    return Response.json({ state, conversationId })
  })
  const api = load('services/api.ts')
  let guard
  load('router/guards.ts').installProductGuards({ beforeEach(fn) { guard = fn }, afterEach() {} })
  const navigate = path => guard({ path, meta: {} })
  assert.equal(await navigate('/chat'), true)
  assert.equal(await navigate('/me'), true)
  assert.equal(await navigate('/judgment'), true)
  assert.equal(gets, 1, 'subsequent navigation must not await another start/poll pair')
  await api.getOnboardingProgress()
  assert.equal(gets, 2, 'explicit page reads remain fresh')
  scope.setProductScope('box-b')
  await navigate('/chat')
  assert.equal(gets, 3)
  await api.updateOnboarding('restart')
  assert.deepEqual(await navigate('/chat'), { path: '/onboarding/chat', replace: true })
  await navigate('/me')
  assert.equal(gets, 5, 'incomplete progress must be read again')
  state = 'ready'
  await navigate('/chat')
  writesFail = true
  await assert.rejects(api.updateOnboarding('restart'), /lost write response/)
  await navigate('/chat')
  assert.equal(gets, 7, 'failed writes also invalidate hints')
  scope.setProductScope(null)
  await navigate('/chat')
  assert.equal(gets, 7, 'offline navigation cannot issue business requests')
  scope.setProductScope('box-c')
  conversationId = 'old-onboarding-conversation'
  await navigate('/chat')
  const changedBox = guard({ path: '/onboarding', meta: { onboardingFlow: true } })
  queueMicrotask(() => scope.setProductScope('box-d'))
  assert.equal(await changedBox, true, 'guard must not consume an already-resolved old-box hint')
  await navigate('/chat')
  writesFail = false
  let write
  const restarted = guard({ path: '/onboarding', meta: { onboardingFlow: true } })
  queueMicrotask(() => { write = api.updateOnboarding('restart') })
  assert.equal(await restarted, true, 'guard must not redirect to a stale conversation during restart')
  await write
})

const ok = data => ({ ok: true, data, generation: 7 })
function host(events, status = 200) {
  const calls = []
  const product = {
    start: async (context, request) => { calls.push(['start', request]); return ok({ id: 'a'.repeat(32), state: 'queued', cursor: 0 }) },
    poll: async () => ok({ id: 'a'.repeat(32), state: 'succeeded', cursor: 3, hasMore: false, events: [
      { seq: 1, kind: 'headers', status, headers: { 'content-type': 'application/json' } },
      { seq: 2, kind: 'chunk', data: bytes(events) }, { seq: 3, kind: 'end' },
    ] }),
    cancel: async (...args) => { calls.push(['cancel', ...args]); return ok({ id: 'a'.repeat(32), state: 'cancelled', cancelRequested: true }) },
    uploadCancel: async (...args) => { calls.push(['uploadCancel', ...args]); return ok({}) },
  }
  return { product, calls }
}
function desktop(product) {
  const load = modules(), scope = load('shared/productScope.ts')
  scope.enableDesktopProduct(); scope.setProductScope('synthetic-workspace-7')
  const client = load('desktop/productClient.ts').createDesktopProductClient(product, () => ({ generation: 7, workspaceId: 'synthetic-workspace' }))
  load('services/transport.ts').installProductTransport(client.request)
  return { load, scope, client }
}

test('catalog is finite, unique, bounded and resolves only exact method/parameters', () => {
  const catalog = JSON.parse(readFileSync(new URL('../../shared/product-operations.json', import.meta.url), 'utf8'))
  assert.equal(catalog.schemaVersion, 1)
  assert.equal(new Set(catalog.operations.map(o => o.id)).size, catalog.operations.length)
  assert.equal(catalog.operations.filter(o => o.body === 'multipart').length, 4)
  assert.equal(catalog.operations.filter(o => o.response === 'sse').length, 1)
  for (const operation of catalog.operations) {
    assert.deepEqual(operation.pathParams, [...operation.path.matchAll(/\{(\w+)\}/g)].map(m => m[1]))
    assert.ok(operation.maxRequestBytes <= 524288)
  }
  const { resolveProductOperation } = modules()('services/productCatalog.ts')
  assert.equal(resolveProductOperation('/api/mindos/conversations/routing/default').operation.path, '/api/mindos/conversations/routing/default')
  assert.deepEqual(resolveProductOperation('/api/mindos/materials/m_test').params, { materialId: 'm_test' })
  const review = resolveProductOperation('/api/mindos/materials/m_test/redaction/review?kind=body')
  assert.equal(review.operation.id, 'get_api_mindos_materials_material_id_redaction_review')
  assert.equal(review.operation.sensitiveResponse, true)
  assert.deepEqual(review.query, { kind: 'body' })
  for (const route of [
    '/api/mindos/materials/m_test/redaction',
    '/api/mindos/materials/m_test/redaction/review',
    '/api/mindos/materials/m_test/redaction/correct',
    '/api/mindos/materials/m_test/redaction/retry',
  ]) assert.ok(catalog.operations.some(operation => operation.path.replace('{materialId}', 'm_test') === route))
  for (const path of ['https://host/api/mindos/materials', '/api/mindos/materials?extra=1', '/api/mindos/materials?type=image&type=audio', '/api/mindos/materials/%2e%2e', '/api/mindos/materials/a%2fb']) assert.throws(() => resolveProductOperation(path))
  assert.throws(() => resolveProductOperation('/api/mindos/materials/m_test/redaction/review?extra=body'))
})

test('redaction review uses the bounded gateway route and correction idempotency key', async () => {
  const payload = { text: 'candidate', originalText: 'private', reasons: [], inputHash: 'b'.repeat(64), replacements: [], versionId: 'v'.repeat(32), attemptId: 'a'.repeat(32), kind: 'body', canCorrect: true }
  const { product, calls } = host(JSON.stringify(payload))
  const { load, client } = desktop(product)

  assert.deepEqual(await load('services/api.ts').api.getRedactionReview('m_test', 'body'), payload)
  product.poll = host('{"state":"processing"}').product.poll
  await load('services/api.ts').api.correctRedaction('m_test', {
    versionId: 'v'.repeat(32), attemptId: 'a'.repeat(32), kind: 'body', expectedInputHash: 'b'.repeat(64),
    spans: [{ start: 0, end: 1, type: 'person' }], reason: 'checked',
  }, 'redaction-correction-safe-1')

  const starts = calls.filter(call => call[0] === 'start').map(call => call[1])
  assert.equal(starts[0].operationId, 'get_api_mindos_materials_material_id_redaction_review')
  assert.equal(starts[1].operationId, 'post_api_mindos_materials_material_id_redaction_correct')
  assert.equal(starts[1].requestId, 'redaction-correction-safe-1')
  client.dispose()
})

test('nested redaction errors retain their safe message and code', async () => {
  const failed = host('{"error":{"code":"REDACTION_PERMISSION_DENIED","message":"此操作需要单独授权"}}', 403)
  const { load, client } = desktop(failed.product)
  await assert.rejects(load('services/api.ts').api.getRedactionReview('m_test', 'body'), error =>
    error.status === 403 && error.code === 'REDACTION_PERMISSION_DENIED' && error.message === '此操作需要单独授权')
  client.dispose()
})

test('all three original product network entries use installed transport, preserving HTTP errors and SSE', async () => {
  const { product, calls } = host('{"items":[]}')
  const { load, client } = desktop(product)
  const originalFetch = globalThis.fetch
  globalThis.fetch = () => { throw new Error('renderer fetch forbidden') }
  try {
    assert.deepEqual(await load('services/api.ts').api.listMaterials(), { items: [] })
    assert.deepEqual(await load('services/taskRouting.ts').routingRequest('/mindos/conversations/c_test/routing'), { items: [] })
    product.poll = async () => ok({ id: 'a'.repeat(32), state: 'succeeded', cursor: 3, hasMore: false, events: [
      { seq: 1, kind: 'headers', status: 200, headers: { 'content-type': 'text/event-stream' } },
      { seq: 2, kind: 'chunk', data: bytes('event: token\ndata: {"text":"真实传输"}\n\n') }, { seq: 3, kind: 'end' },
    ] })
    const seen = []
    await load('services/sse.ts').streamPost('/mindos/conversations/c_test/messages', { content: '合成文本' }, { token: value => seen.push(value) })
    assert.deepEqual(seen, [{ text: '真实传输' }])
    const failed = host('{"detail":{"code":"REVISION_CONFLICT","detail":"请核对新版本"}}', 409)
    product.poll = failed.product.poll
    await assert.rejects(load('services/api.ts').api.getKnowledge('k_test'), e => e.status === 409 && e.code === 'REVISION_CONFLICT')
    assert.equal(calls.filter(c => c[0] === 'start').length, 4)
  } finally { globalThis.fetch = originalFetch; client.dispose() }
})

test('chat domain terminal events finish without HTTP EOF and cancel the remote operation', { timeout: 2000 }, async () => {
  for (const terminalEvent of ['message_done', 'error']) {
    const { product, calls } = host('{}')
    let polls = 0
    product.poll = async () => {
      polls++
      if (polls === 1) return ok({ id: 'a'.repeat(32), state: 'running', cursor: 2, hasMore: false, events: [
        { seq: 1, kind: 'headers', status: 200, headers: { 'content-type': 'text/event-stream' } },
        { seq: 2, kind: 'chunk', data: bytes(`event: ${terminalEvent}\ndata: {"status":"complete"}\n\n`) },
      ] })
      return new Promise(() => {})
    }
    const { load, client } = desktop(product)
    const seen = []
    await load('services/sse.ts').streamPost('/mindos/conversations/c_test/messages', { content: '合成文本' }, {
      [terminalEvent]: value => seen.push(value),
    }, undefined, { terminalEvents: ['message_done', 'error'] })
    assert.deepEqual(seen, [{ status: 'complete' }])
    assert.equal(calls.filter(call => call[0] === 'cancel').length, 1)
    assert.ok(polls <= 2)
    client.dispose()
  }
})

test('preview and final message keep the domain action id but use distinct Gateway job ids', async () => {
  const { product, calls } = host('{}')
  const { client } = desktop(product)
  const actionId = 'same-domain-action-1234'
  await (await client.request('/api/mindos/conversations/c_test/routing/preview', {
    method: 'POST', body: JSON.stringify({ requestId: actionId, content: '合成问题' }),
  })).json()
  await (await client.request('/api/mindos/conversations/c_test/messages', {
    method: 'POST', body: JSON.stringify({ requestId: actionId, content: '合成问题', routeRevision: 'r1' }),
  })).text()
  const starts = calls.filter(call => call[0] === 'start').map(call => call[1])
  assert.equal(starts.length, 2)
  assert.notEqual(starts[0].requestId, starts[1].requestId)
  assert.deepEqual(starts.map(request => request.body.requestId), [actionId, actionId])
  client.dispose()
})

test('multipart uploads use bounded ordered chunks and only upload handles in operation body', async () => {
  const { product, calls } = host('{"materialId":"m_test"}')
  let received = 0
  product.uploadCreate = async (_context, input) => { calls.push(['uploadCreate', input]); return ok({ id: 'b'.repeat(32), state: 'open', size: input.size, received: 0, nextIndex: 0 }) }
  product.uploadChunk = async (_context, input) => { received += input.bytes.length; calls.push(['chunk', input.bytes.length]); return ok({ id: input.id, state: 'open', size: 600000, received, nextIndex: input.index + 1 }) }
  product.uploadComplete = async (_context, input) => ok({ id: input.id, state: 'complete', size: received, received, nextIndex: 2 })
  const { client } = desktop(product)
  const form = new FormData(); form.append('file', new File([new Uint8Array(600000)], 'synthetic.txt')); form.append('folderId', '12')
  const response = await client.request('/api/mindos/uploads', { method: 'POST', body: form })
  await response.json()
  assert.deepEqual(calls.filter(c => c[0] === 'chunk').map(c => c[1]), [524288, 75712])
  assert.deepEqual(calls.find(c => c[0] === 'start')[1].body, { kind: 'multipart', fields: { folderId: '12' }, files: [{ field: 'file', uploadId: 'b'.repeat(32) }] })
  client.dispose()
})

test('abort settles pending headers and requests remote cancellation once without replay', async () => {
  const { product, calls } = host('{}')
  let finishPoll
  product.poll = () => new Promise(resolve => { finishPoll = resolve })
  const { client } = desktop(product)
  const abort = new AbortController()
  const pending = client.request('/api/mindos/materials', { signal: abort.signal })
  await new Promise(resolve => setImmediate(resolve))
  abort.abort()
  await assert.rejects(pending, e => e.name === 'AbortError')
  assert.equal(calls.filter(c => c[0] === 'cancel').length, 1)
  assert.equal(calls.filter(c => c[0] === 'start').length, 1)
  finishPoll(ok({ id: 'a'.repeat(32), state: 'cancelled', cursor: 0, hasMore: false, events: [] }))
  client.dispose()
})

test('abort after response headers still reaches the desktop stream and remote operation', { timeout: 2000 }, async () => {
  const { product, calls } = host('{}')
  let polls = 0
  product.poll = async () => {
    polls++
    if (polls === 1) return ok({ id: 'a'.repeat(32), state: 'running', cursor: 1, hasMore: false, events: [
      { seq: 1, kind: 'headers', status: 200, headers: { 'content-type': 'application/json' } },
    ] })
    return new Promise(() => {})
  }
  const { load, client } = desktop(product)
  const abort = new AbortController()
  const response = await load('services/transport.ts').transportRequest('/api/mindos/materials', { signal: abort.signal })
  const reading = response.text()
  abort.abort()
  await assert.rejects(reading, error => error.name === 'AbortError')
  assert.equal(calls.filter(call => call[0] === 'cancel').length, 1)
  assert.equal(calls.filter(call => call[0] === 'start').length, 1)
  client.dispose()
})

test('workspace changes cancel streams and prevent late writes or legacy draft reuse', async () => {
  const { product } = host('{}')
  let count = 0
  product.poll = () => ++count === 1 ? Promise.resolve(ok({ id: 'a'.repeat(32), state: 'running', cursor: 1, hasMore: false, events: [{ seq: 1, kind: 'headers', status: 200, headers: {} }] })) : new Promise(() => {})
  const { client, scope } = desktop(product)
  const storage = new Map([['zhijun.reply-input.c_test', 'legacy-other-owner']])
  globalThis.sessionStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  const owned = scope.createProductSessionStorage()
  assert.equal(owned.getItem('zhijun.reply-input.c_test'), null)
  owned.setItem('zhijun.reply-input.c_test', 'synthetic-secret')
  const response = await client.request('/api/mindos/materials')
  const reading = response.text()
  scope.setProductScope('synthetic-other-workspace')
  await assert.rejects(reading, e => e.name === 'AbortError')
  owned.setItem('zhijun.reply-input.c_test', 'late-old-secret')
  assert.equal([...storage.values()].includes('synthetic-secret'), false)
  assert.equal([...storage.values()].includes('late-old-secret'), false)
  assert.equal(storage.get('zhijun.reply-input.c_test'), 'legacy-other-owner')
  client.dispose(); delete globalThis.sessionStorage
})

test('desktop has no network fallback before connection or transport installation', async () => {
  const load = modules(), scope = load('shared/productScope.ts')
  scope.enableDesktopProduct()
  await assert.rejects(load('services/transport.ts').transportRequest('/api/health'), /先连接/)
  assert.equal(await load('services/api.ts').provisionMindosSession(), null)
})

test('operation budgets and event order reject malformed delivery and cancel once', async () => {
  for (const events of [
    [{ seq: 2, kind: 'headers', status: 200, headers: {} }],
    [{ seq: 1, kind: 'headers', status: 200, headers: {} }, { seq: 2, kind: 'chunk', data: new Uint8Array(524289) }],
  ]) {
    const { product, calls } = host('{}')
    product.poll = async () => ok({ id: 'a'.repeat(32), state: 'running', cursor: events.at(-1).seq, events, hasMore: false })
    const { client } = desktop(product)
    await assert.rejects(async () => { const response = await client.request('/api/mindos/materials'); await response.text() }, /顺序|大小/)
    assert.equal(calls.filter(c => c[0] === 'cancel').length, 1)
    client.dispose()
  }
})

test('idempotency keys are stable only for catalog-declared confirmation operations', async () => {
  const { product, calls } = host('{}')
  const { client } = desktop(product)
  const init = { method: 'POST', headers: { 'Idempotency-Key': 'synthetic-confirm-1' }, body: '{}' }
  await (await client.request('/api/mindos/knowledge/k_test/confirm', init)).json()
  assert.equal(calls.find(c => c[0] === 'start')[1].requestId, 'synthetic-confirm-1')
  await assert.rejects(client.request('/api/mindos/conversations', init), /编号/)
  assert.equal(calls.filter(c => c[0] === 'start').length, 1)
  client.dispose()
})

test('save cancellation is not reported as an export and preview cannot escape the host scheme', async () => {
  const { product } = host('{}')
  const closed = []
  product.save = async () => ok({ saved: false })
  product.openMedia = async () => ok({ handle: 'b'.repeat(32), url: 'http://127.0.0.1/private', contentType: 'image/png' })
  product.closeMedia = async (_context, input) => { closed.push(input.handle); return ok({ closed: true }) }
  const { client } = desktop(product)
  await assert.rejects(client.saveText('合成.md', '# 合成', 'text/markdown'), e => e.name === 'AbortError')
  await assert.rejects(client.preview('/api/mindos/materials/m_test/file'), /地址无效/)
  assert.deepEqual(closed, ['b'.repeat(32)])
  client.dispose()
})

test('desktop preview preserves the authorized range-capable host URL and closes its handle', async () => {
  const closed = []
  const { product } = host('{}')
  product.openMedia = async () => ok({ handle: 'c'.repeat(32), url: `zhijun-media://session/${'d'.repeat(32)}`, contentType: 'application/pdf', size: 14 })
  product.closeMedia = async (_context, input) => { closed.push(input.handle); return ok({ closed: true }) }
  const { client } = desktop(product)
  const url = await client.preview('/api/mindos/materials/m_test/file')
  assert.equal(url, `zhijun-media://session/${'d'.repeat(32)}`)
  client.releasePreview(url)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(closed, ['c'.repeat(32)])
  client.dispose()
})

test('desktop default egress mints an exact receipt without a dialog and falls back to explicit review outside scope', async () => {
  const load = modules(), scope = load('shared/productScope.ts')
  scope.enableDesktopProduct(); scope.setProductScope('de-consent-test')
  const routing = load('services/taskRouting.ts')
  const calls = [], receipts = new Set()
  let sourceKeys = [], missing = [], autoApplies = false, rejectDefault = false, lastPrompt = ''
  const preview = prompt => ({ revision: receipts.has(prompt) ? 'approved-' + prompt : 'pending-' + prompt,
    conversationId: 'c_test', purpose: 'chat', purposeLabel: '对话', service: { id: 'online-test', name: '合成在线服务', model: 'synthetic', external: true },
    missing, blocked: [], reason: '', sources: sourceKeys.map(key => ({ key, title: key, text: '合成依据', version: 'v1', blocked: '', kind: 'material' })), excluded: [],
    defaultAuthorization: { enabled: true, revision: 7, includeFiles: true, includeCharter: true, autoEgress: autoApplies, applies: autoApplies },
    request: { system: '合成系统提示', messages: [{ role: 'user', content: prompt }] }, deConsentRequired: !receipts.has(prompt) })
  load('services/transport.ts').installProductTransport(async (path, init) => {
    const body = JSON.parse(init.body); calls.push([path, body])
    if (path.endsWith('/grant')) {
      assert.deepEqual(body.keys, sourceKeys)
      if (body.defaultPolicyRevision && rejectDefault) return Response.json({ detail: { code: 'WORKSPACE_DEFAULT_CONSENT_CHANGED' } }, { status: 409 })
      receipts.add(lastPrompt); return Response.json({ ok: true })
    }
    lastPrompt = body.content
    if (path.endsWith('/preview')) return Response.json(preview(body.content))
    if (body.previewOnly) return Response.json({ routePreview: preview(body.content) })
    assert.ok(receipts.has(body.content), 'an actual external task must follow explicit consent')
    return Response.json({ complete: true })
  })
  const tick = () => new Promise(resolve => setImmediate(resolve))
  let pending = routing.prepareChatRoute('c_test', { content: 'prompt-one' })
  await tick()
  assert.equal(routing.routeQuestion.value.preview.sources.length, 0)
  assert.equal(calls.some(([path]) => path.endsWith('/grant')), false, 'legacy source policy must not auto-grant')
  routing.routeQuestion.value.done({ action: 'allow', keys: [] })
  assert.equal((await pending).routeRevision, 'approved-prompt-one')
  autoApplies = true
  sourceKeys = ['already-granted-a', 'already-granted-b']
  pending = routing.prepareChatRoute('c_test', { content: 'prompt-two' })
  assert.equal((await pending).routeRevision, 'approved-prompt-two')
  assert.equal(routing.routeQuestion.value, null)
  assert.deepEqual(calls.filter(([path]) => path.endsWith('/grant')).at(-1)[1], { revision: 'pending-prompt-two', keys: sourceKeys, defaultPolicyRevision: 7 })
  pending = routing.routedTask('c_test', '/mindos/synthetic-task', { content: 'task-prompt' })
  assert.deepEqual(await pending, { complete: true })
  assert.equal(routing.routeQuestion.value, null)
  missing = ['outside-standing-range']; sourceKeys = [...missing]; autoApplies = false
  pending = routing.prepareChatRoute('c_test', { content: 'range-review' })
  await tick(); assert.ok(routing.routeQuestion.value)
  routing.routeQuestion.value.done({ action: 'cancel' })
  assert.equal(await pending, null); assert.equal(receipts.has('range-review'), false)
  missing = []; autoApplies = true; rejectDefault = true
  pending = routing.prepareChatRoute('c_test', { content: 'gateway-policy-mismatch' })
  await tick(); assert.ok(routing.routeQuestion.value, 'a DE policy mismatch must fall back to exact visible review')
  routing.routeQuestion.value.done({ action: 'cancel' })
  assert.equal(await pending, null)
  scope.setProductScope(null)
})

test('Web routing keeps its existing source authorization and desktop speech never promises an unconfigured service', async () => {
  const load = modules()
  const scope = load('shared/productScope.ts')
  const routing = load('services/taskRouting.ts')
  const originalWindow = globalThis.window
  globalThis.window = { webkitSpeechRecognition: function () {} }
  try {
    const speech = load('shared/speech.ts')
    assert.equal(speech.speechSupported(), true)
    assert.equal(routing.needsDeConsent({ service: { external: true }, deConsentRequired: true }), false)
    scope.enableDesktopProduct()
    assert.equal(speech.speechSupported(), false)
    assert.equal(speech.createRecognizer(), null)
    assert.equal(routing.needsDeConsent({ service: { external: true }, deConsentRequired: true }), true)
  } finally { globalThis.window = originalWindow }
})

function voiceHarness(overrides = {}) {
  const load = modules(), scope = load('shared/productScope.ts')
  scope.enableDesktopProduct(); scope.setProductScope('voice-owner')
  const states = [], texts = [], errors = [], calls = []
  let stopped = 0
  const track = { stop() { stopped++ }, async applyConstraints(value) { calls.push(['constraints', value]) } }
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] }
  const audio = { sampleRate: 16000, numberOfChannels: 1, length: 1600, getChannelData: () => new Float32Array(1600).fill(0.2) }
  const recorder = { state: 'inactive', ondataavailable: null, onstop: null, onerror: null,
    start() { this.state = 'recording' }, stop() { this.state = 'inactive'; this.ondataavailable?.({ data: new Blob(['synthetic encoded audio']) }); this.onstop?.() } }
  const api = load('services/voiceRecording.ts')
  const voice = api.createVoiceRecording({ onState: value => states.push(value), onText: value => texts.push(value), onError: value => errors.push(value) }, {
    permission: async () => { calls.push(['permission']); return true },
    media: async () => { calls.push(['media']); return stream },
    recorder: () => recorder, decode: async () => audio,
    transcribe: async (file, signal) => { calls.push(['transcribe', file, signal]); return '盒子识别出的合成文字' }, ...overrides,
  })
  return { voice, scope, api, states, texts, errors, calls, recorder, audio, stream, stopped: () => stopped }
}

test('desktop voice is user-initiated, produces bounded mono WAV, stops the microphone before transcription, and never sends a chat', async () => {
  const h = voiceHarness()
  assert.deepEqual(h.calls, [])
  await h.voice.start()
  assert.deepEqual(h.calls.slice(0, 2).map(c => c[0]), ['permission', 'media'])
  assert.equal(h.states.at(-1), 'recording')
  const finish = h.voice.finish(); assert.equal(h.stopped(), 1)
  await Promise.all([finish, h.voice.finish()])
  const entries = h.calls.filter(c => c[0] === 'transcribe')
  assert.equal(entries.length, 1)
  const file = entries[0][1], view = new DataView(await file.arrayBuffer())
  assert.equal(file.type, 'audio/wav'); assert.equal(file.size, 44 + 3200)
  assert.equal(view.getUint16(22, true), 1); assert.equal(view.getUint32(24, true), 16000)
  assert.deepEqual(h.texts, ['盒子识别出的合成文字']); assert.equal(h.states.at(-1), 'idle')
  h.voice.dispose()
})

test('microphone denial never opens a media stream and silent recordings never call transcription', async () => {
  const denied = voiceHarness({ permission: async () => false })
  await denied.voice.start(); assert.equal(denied.calls.length, 0); assert.match(denied.errors[0], /权限/); denied.voice.dispose()
  const silent = voiceHarness({ decode: async () => ({ sampleRate: 16000, numberOfChannels: 1, length: 100, getChannelData: () => new Float32Array(100) }) })
  await silent.voice.start(); await silent.voice.finish()
  assert.equal(silent.calls.some(c => c[0] === 'transcribe'), false); assert.match(silent.errors[0], /没有检测到声音/)
  assert.deepEqual(silent.texts, []); silent.voice.dispose()
})

test('scope changes stop recording immediately and discard a late microphone or transcription result', async () => {
  const h = voiceHarness()
  await h.voice.start(); h.scope.setProductScope('other-owner')
  assert.equal(h.stopped(), 1); assert.equal(h.states.at(-1), 'idle'); assert.equal(h.calls.some(c => c[0] === 'transcribe'), false)
  h.voice.dispose()
  let grantStream
  const late = voiceHarness({ media: () => new Promise(resolve => { grantStream = resolve }) })
  const starting = late.voice.start(); await new Promise(resolve => setImmediate(resolve))
  late.voice.cancel(); grantStream(late.stream); await starting
  assert.equal(late.stopped(), 1); assert.equal(late.states.includes('recording'), false); late.voice.dispose()
  let complete, uploadingSignal
  const processing = voiceHarness({ transcribe: (_file, signal) => { uploadingSignal = signal; return new Promise(resolve => { complete = resolve }) } })
  await processing.voice.start(); const pending = processing.voice.finish()
  await new Promise(resolve => setImmediate(resolve))
  processing.scope.setProductScope('new-account')
  assert.equal(uploadingSignal.aborted, true); complete('旧账号识别文字'); await pending
  assert.deepEqual(processing.texts, []); processing.voice.dispose()
})

test('recording enforces a 120-second timer and 2 MiB compressed budget', async () => {
  const h = voiceHarness()
  const originalTimer = globalThis.setTimeout
  let delay
  globalThis.setTimeout = (_fn, value) => { delay = value; return originalTimer(() => {}, 1000000) }
  try { await h.voice.start() } finally { globalThis.setTimeout = originalTimer }
  assert.equal(delay, 120000)
  h.recorder.ondataavailable({ data: new Blob([new Uint8Array(2 * 1024 * 1024 + 1)]) })
  assert.equal(h.stopped(), 1); assert.match(h.errors[0], /大小限制/)
  assert.deepEqual(h.texts, []); h.voice.dispose()
  assert.throws(() => h.api.pcmWave({ ...h.audio, length: 16000 * 122 }), /时长/)
})
