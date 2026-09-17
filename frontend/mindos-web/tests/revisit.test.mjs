import { test } from 'node:test'
import assert from 'node:assert/strict'
import { revisitEntries } from '../src/shared/revisit.ts'

const chat = (id, more = {}) => ({ id, title: '同名经历', messageCount: 2, decisionId: null,
  status: 'active', updatedAt: '2026-09-17T10:00:00Z', lastMessageAt: null, ...more })
const choice = (id, more = {}) => ({ id, title: '同名经历', createdAt: '2026-09-15T10:00:00Z',
  evidenceRefs: [], outcome: null, review: null, ...more })

test('explicit sources and review conversations share one record; matching titles do not merge unrelated experiences', () => {
  const items = revisitEntries([choice('d', { evidenceRefs: [JSON.stringify({ kind: 'message', conversationId: 'source' })] })],
    [chat('source'), chat('review', { decisionId: 'd' }), chat('unrelated')])
  assert.equal(items.length, 2)
  assert.deepEqual(items.find(e => e.kind === 'decision').conversations.map(c => c.id), ['source', 'review'])
  assert.equal(items.find(e => e.kind === 'conversation').id, 'unrelated')
})

test('ordinary and archived conversations remain available; empty conversations do not become experiences', () => {
  const items = revisitEntries([], [chat('empty', { messageCount: 0 }), chat('old', { status: 'archived' }), chat('ordinary')])
  assert.deepEqual(items.map(e => e.id).sort(), ['old', 'ordinary'])
})

test('new results and reviews move an existing choice forward without duplicating it', () => {
  const items = revisitEntries([choice('old', { review: { createdAt: '2026-09-18T00:00:00+08:00' } }),
    choice('recent', { outcome: { recordedAt: '2026-09-17T14:00:00Z' } })], [chat('conversation')])
  assert.deepEqual(items.map(e => e.id), ['old', 'recent', 'conversation'])
})

test('malformed or legacy evidence does not hide conversations or create guessed links', () => {
  const items = revisitEntries([choice('d', { evidenceRefs: ['old evidence', 'null', '{', '{"conversationId":4}'] })], [chat('c')])
  assert.equal(items.length, 2)
  assert.equal(items.find(e => e.kind === 'decision').conversations.length, 0)
})
