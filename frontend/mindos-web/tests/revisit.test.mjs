import { test } from 'node:test'
import assert from 'node:assert/strict'
import { revisitEntries, readRevisitPreferences } from '../src/shared/revisit.ts'
import { enableDesktopProduct, setProductScope, createProductLocalStorage } from '../src/shared/productScope.ts'

const now = Date.parse('2026-09-17T12:00:00Z')
const prefs = () => ({ selected: {}, deferred: {} })
const chat = (id, more = {}) => ({ id, title: '同名经历', messageCount: 2, decisionId: null,
  status: 'active', updatedAt: '2026-09-17T10:00:00Z', lastMessageAt: null, ...more })
const choice = (id, more = {}) => ({ id, title: '同名经历', createdAt: '2026-09-15T10:00:00Z',
  status: 'open', reviewAt: '2026-09-17T11:00:00Z', evidenceRefs: [], outcome: null, review: null, ...more })
const ready = entries => entries.filter(e => e.reason && !e.deferred)

test('default selection is due/result/review, never ordinary chats or unplanned/future choices', () => {
  const items = revisitEntries([choice('due'), choice('future', { reviewAt: '2026-09-18T00:00:00Z' }),
    choice('unplanned', { reviewAt: null }), choice('bad-date', { reviewAt: 'invalid' }),
    choice('result', { reviewAt: null, outcome: { recordedAt: '2026-09-16T12:00:00Z' } }),
    choice('done', { reviewAt: null, review: { createdAt: '2026-09-16T13:00:00Z' } })],
  [chat('ordinary'), chat('archived', { status: 'archived' }), chat('empty', { messageCount: 0 })], prefs(), now)
  assert.deepEqual(ready(items).map(e => [e.id, e.reason]), [['done', 'reviewed'], ['result', 'outcome'], ['due', 'due']])
  assert.equal(items.some(e => e.id === 'empty'), false)
  assert.equal(items.some(e => e.id === 'ordinary'), true, 'ordinary chats remain available to choose')
})

test('manual selection includes an ordinary or archived experience without a manufactured decision', () => {
  const settings = prefs()
  settings.selected['conversation:old'] = '2026-09-17T09:00:00Z'
  settings.selected['decision:future'] = '2026-09-17T10:00:00Z'
  const items = revisitEntries([choice('future', { reviewAt: '2099-01-01T00:00:00Z' })],
    [chat('old', { status: 'archived' }), chat('ordinary')], settings, now)
  assert.deepEqual(ready(items).map(e => e.id), ['future', 'old'])
  assert.equal(items.find(e => e.id === 'old').reason, 'selected')
})

test('explicit sources and review conversations group reliably; identical titles do not merge', () => {
  const settings = prefs()
  settings.selected['conversation:source'] = '2026-09-17T09:00:00Z'
  const items = revisitEntries([choice('d', { reviewAt: null, evidenceRefs: [JSON.stringify({ conversationId: 'source' }), JSON.stringify({ conversationId: 'outside-page' })] })],
    [chat('source'), chat('review', { decisionId: 'd' }), chat('unrelated'), chat('missing-decision-review', { decisionId: 'not-loaded' })], settings, now)
  assert.equal(items.length, 2)
  const decision = items.find(e => e.kind === 'decision')
  assert.deepEqual(decision.conversations.map(c => c.id), ['source', 'review'])
  assert.deepEqual(decision.sourceIds, ['source', 'outside-page', 'review'])
  assert.equal(decision.reason, 'selected', 'selection follows an explicit later-created decision link')
  assert.equal(items.find(e => e.kind === 'conversation').id, 'unrelated')
})

test('defer survives chat activity; a changed result restores eligibility, manual restore works', () => {
  const settings = prefs(), original = choice('d')
  settings.deferred['decision:d'] = revisitEntries([original], [], settings, now)[0].revision
  assert.equal(ready(revisitEntries([original], [chat('new-review', { decisionId: 'd' })], settings, now)).length, 0)
  assert.equal(ready(revisitEntries([choice('d', { status: 'outcome_recorded', outcome: { recordedAt: '2026-09-18T14:00:00Z' } })], [], settings, now)).length, 1)
  delete settings.deferred['decision:d']
  assert.equal(ready(revisitEntries([original], [], settings, now)).length, 1)
  settings.selected['conversation:c'] = '2026-09-17T10:00:00Z'
  settings.deferred['conversation:c'] = 'selected'
  assert.equal(ready(revisitEntries([], [chat('c', { updatedAt: '2026-09-19T10:00:00Z' })], settings, now)).length, 0)
})

test('malformed evidence and stored preferences never create guessed links', () => {
  const items = revisitEntries([choice('d', { evidenceRefs: ['old evidence', 'null', '{', '{"conversationId":4}'] })], [chat('c')], prefs(), now)
  assert.equal(items.length, 2)
  assert.equal(items.find(e => e.kind === 'decision').sourceIds.length, 0)
  assert.deepEqual(readRevisitPreferences('{'), prefs())
  assert.deepEqual(readRevisitPreferences('{"selected":{"conversation:c":"date","x":"bad","decision:d":3},"deferred":[]}'), { selected: { 'conversation:c': 'date' }, deferred: {} })
})

test('device-local arrangements survive reconnect, isolate account/box and reject stale-owner writes', () => {
  const memory = new Map()
  globalThis.localStorage = { getItem: k => memory.get(k) ?? null, setItem: (k, v) => memory.set(k, v), removeItem: k => memory.delete(k) }
  enableDesktopProduct()
  setProductScope('generation-a', 'account-one-box-a')
  const a = createProductLocalStorage()
  a.setItem('revisit.v1', 'arrangement-a')
  setProductScope(null)
  assert.equal(a.getItem('revisit.v1'), null)
  assert.throws(() => a.setItem('revisit.v1', 'late-write'))
  setProductScope('generation-b', 'account-one-box-a')
  assert.equal(createProductLocalStorage().getItem('revisit.v1'), 'arrangement-a')
  assert.throws(() => a.setItem('revisit.v1', 'late-write'), 'old component cannot write even on reconnect to same box')
  setProductScope('generation-c', 'account-one-box-b')
  assert.equal(createProductLocalStorage().getItem('revisit.v1'), null)
  createProductLocalStorage().setItem('revisit.v1', 'arrangement-b')
  setProductScope('generation-d', 'account-two-box-a')
  assert.equal(createProductLocalStorage().getItem('revisit.v1'), null)
  setProductScope('generation-e', 'account-one-box-a')
  assert.equal(createProductLocalStorage().getItem('revisit.v1'), 'arrangement-a')
  setProductScope(null)
  assert.throws(() => createProductLocalStorage().setItem('revisit.v1', 'offline'))
  delete globalThis.localStorage
})
