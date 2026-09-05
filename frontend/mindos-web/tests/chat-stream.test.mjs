// Execute the actual orchestration helper with transport-only doubles.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

class ApiError extends Error {
  constructor(code, status = 409) { super(code); this.code = code; this.status = status }
}
const original = { requestId: 'same-user-message', routeRevision: 'old-preview', content: '我想准备一次重要沟通',
  replyAssistance: { messageId: 'assistant-1', selections: [{ batchId: 'batch-1', candidateId: 'choice-1' }] } }
const code = ts.transpileModule(await readFile(new URL('../src/services/chatStream.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
function harness(send, preview = async (_cid, body) => ({ ...body, routeRevision: 'new-preview' })) {
  const calls = [], previews = [], failures = [], exports = {}
  new Function('require', 'exports', code)(id => {
    if (id === './api') return { ApiError }
    if (id === './sse') return { streamPost: async (...args) => { calls.push(args); return send(...args) } }
    if (id === './taskRouting') return {
      canRefreshRoute: e => ['ROUTE_CHANGED', 'PREVIEW_EXPIRED'].includes(e.code),
      prepareChatRoute: async (...args) => { previews.push(args); return preview(...args) },
    }
    if (id.includes('useReplyRecovery')) return { reportReplyFailure: (...args) => failures.push(args) }
    throw new Error('Unmocked import: ' + id)
  }, exports)
  return { ...exports, calls, previews, failures }
}
{
  let attempts = 0
  const tokens = []
  const h = harness(async (_path, _body, handlers) => {
    if (++attempts === 1) throw new ApiError('ROUTE_CHANGED')
    handlers.meta({ messageId: 'reply-1' }); handlers.token({ t: '可以先明确沟通目标。' })
  }, async (_cid, body, signal) => {
    assert.ok(signal); return { ...body, routeRevision: 'new-preview', localOnly: true }
  })
  assert.equal(await h.streamChat('conversation-1', original, { meta() {}, token: d => tokens.push(d.t) }, new AbortController().signal), true)
  assert.equal(h.calls.length, 2); assert.equal(h.previews.length, 1)
  assert.deepEqual(tokens, ['可以先明确沟通目标。'])
  assert.equal(h.calls[0][1].routeRevision, 'old-preview'); assert.equal(h.calls[1][1].routeRevision, 'new-preview')
  assert.equal(h.calls[1][1].localOnly, true, 'explicit choices from fresh authorization are honored')
  for (const [, body] of h.calls) {
    assert.equal(body.requestId, original.requestId)
    assert.equal(body.content, original.content)
    assert.deepEqual(body.replyAssistance, original.replyAssistance, 'refresh never strips candidate ancestry')
  }
}
{
  const h = harness(async () => { throw new ApiError('ROUTE_CHANGED') })
  await assert.rejects(h.streamChat('conversation-1', original, {}), { code: 'ROUTE_CHANGED' })
  assert.equal(h.calls.length, 2); assert.equal(h.previews.length, 1, 'a repeated race stops after one refresh')
}
for (const error of [new ApiError('SOURCE_CHANGED'), new ApiError('SOURCE_UNAVAILABLE'), new ApiError('REPLY_CONTEXT_CHANGED'), new ApiError('FAILED', 500), new TypeError('Failed to fetch')]) {
  const h = harness(async () => { throw error })
  await assert.rejects(h.streamChat('conversation-1', original, {}), e => e === error)
  assert.equal(h.calls.length, 1); assert.equal(h.previews.length, 0)
  assert.equal(h.failures[0][1], original.replyAssistance)
}
{
  const h = harness(async (_p, _b, handlers) => { handlers.meta({}); throw new ApiError('ROUTE_CHANGED') })
  await assert.rejects(h.streamChat('conversation-1', original, { meta() {} }), { code: 'ROUTE_CHANGED' })
  assert.equal(h.previews.length, 0, 'never replay after stream delivery starts')
}
{
  const errors = []
  const h = harness(async (_p, _b, handlers) => handlers.error({ code: 'ROUTE_CHANGED' }))
  assert.equal(await h.streamChat('conversation-1', original, { error: e => errors.push(e) }), true)
  assert.equal(errors.length, 1); assert.equal(h.calls.length, 1); assert.equal(h.previews.length, 0)
}
{
  const h = harness(async () => { throw new ApiError('ROUTE_CHANGED') }, async () => null)
  assert.equal(await h.streamChat('conversation-1', original, {}), false, 'cancel returns ownership of unchanged draft to caller')
  assert.equal(h.calls.length, 1)
  const changed = harness(async () => { throw new ApiError('ROUTE_CHANGED') })
  assert.equal(await changed.streamChat('conversation-1', original, {}, undefined, () => false), false)
  assert.equal(changed.previews.length, 0, 'switching conversation does not open old consent or send again')
}
{
  const abort = new AbortController(); abort.abort()
  const h = harness(async () => {})
  await assert.rejects(h.streamChat('conversation-1', original, {}, abort.signal), { name: 'AbortError' })
  assert.equal(h.calls.length, 0)
}
console.log('PASS chat stream: stable identity, finite preview refresh, authorization, source failures, cancellation, and no replay after SSE')
