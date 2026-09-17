import test from 'node:test'
import assert from 'node:assert/strict'
import { persistedReply, reconcileReply } from '../src/shared/replyReconciliation.ts'

const identity = { conversationId: 'conv-1', messageId: 'assistant-1', userMessageId: 'user-1', requestId: 'request-1' }
const reply = () => ({ id: 'assistant-1', conversationId: 'conv-1', role: 'assistant', content: '已保存的完整回复', status: 'complete', meta: { replyTo: 'user-1', requestId: 'request-1' } })
const detail = message => ({ conversation: { id: 'conv-1' }, messages: [message] })

test('only exact persisted successful message may reconcile', () => {
  const message = reply()
  assert.equal(persistedReply(detail(message), identity), message)
  for (const change of [{ id: 'other' }, { conversationId: 'other' }, { role: 'user' }, { status: 'error' }, { status: 'aborted' }, { content: '  ' }, { meta: {} }, { meta: { replyTo: 'other', requestId: 'request-1' } }, { meta: { replyTo: 'user-1', requestId: 'old' } }]) {
    assert.equal(persistedReply(detail({ ...reply(), ...change }), identity), null)
  }
  assert.equal(persistedReply({ conversation: { id: 'other' }, messages: [reply()] }, identity), null)
  assert.equal(persistedReply({ conversation: { id: 'conv-1' }, messages: [reply(), reply()] }, identity), null)
  assert.equal(persistedReply(detail(reply()), { ...identity, requestId: '' }), null)
})

test('bounded reads recover completion without a write or full conversation replacement', async () => {
  let reads = 0
  const result = await reconcileReply(identity, async (cid, signal) => {
    assert.equal(cid, identity.conversationId); assert.equal(signal.aborted, false)
    return detail(++reads === 1 ? { ...reply(), status: 'generating' } : reply())
  }, () => true, undefined, [0, 0, 0])
  assert.equal(reads, 2); assert.equal(result.content, '已保存的完整回复')
})

test('pending result uses at most three reads; a read failure is not retried', async () => {
  let reads = 0
  assert.equal(await reconcileReply(identity, async () => { reads++; return detail({ ...reply(), status: 'generating' }) }, () => true, undefined, [0, 0, 0, 0]), null)
  assert.equal(reads, 3)
  reads = 0
  assert.equal(await reconcileReply(identity, async () => { reads++; throw Error('offline') }, () => true, undefined, [0, 0, 0]), null)
  assert.equal(reads, 1)
})

test('cancelled, local placeholder and inactive ownership never read', async () => {
  const abort = new AbortController(); abort.abort()
  const noRead = async () => { assert.fail('unexpected read') }
  assert.equal(await reconcileReply(identity, noRead, () => true, abort.signal), null)
  assert.equal(await reconcileReply(identity, noRead, () => false), null)
  assert.equal(await reconcileReply({ ...identity, messageId: 'local-assistant-1' }, noRead, () => true), null)
})

test('in-flight ownership change or cancellation discards late success', async () => {
  let current = true
  assert.equal(await reconcileReply(identity, async () => { current = false; return detail(reply()) }, () => current, undefined, [0]), null)
  const abort = new AbortController()
  assert.equal(await reconcileReply(identity, async () => { abort.abort(); return detail(reply()) }, () => true, abort.signal, [0]), null)
})

test('cancel also bounds a misbehaving read and delay', async () => {
  const abort = new AbortController()
  const pending = reconcileReply(identity, async () => new Promise(() => {}), () => true, abort.signal, [0])
  abort.abort()
  assert.equal(await pending, null)
})
