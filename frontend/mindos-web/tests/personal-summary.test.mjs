import assert from 'node:assert/strict'
import { ontologySummary, preferredOntologyView, summaryStatus } from '../src/components/ontology/summary.ts'

const claim = (id, extras = {}) => ({ id, content: id, section: 'who', trustState: 'confirmed', layer: 'self_declared', scope: 'long_term', firstSeen: '2026-09-01T00:00:00Z', lastReaffirmed: '2026-09-01T00:00:00Z', evidence: [], ...extras })
const input = [
  claim('current-role'), claim('old-role', { validTo: '2026-09-03T00:00:00Z' }),
  claim('wish', { layer: 'aspirational' }), claim('temporary-role', { scope: 'context_only' }),
  claim('uncertain', { trustState: 'working' }), claim('challenged', { challenged: true }),
  claim('retracted', { retractedAt: '2026-09-04T00:00:00Z' }), claim('superseded', { supersededById: 'replacement' }),
  claim('future-role', { validFrom: '2026-10-01T00:00:00Z' }),
  claim('principle', { section: 'principles', firstSeen: '2026-09-04T00:00:00Z' }),
  claim('project', { section: 'matters', scope: 'context_only' }),
]
const original = structuredClone(input)
const groups = ontologySummary(input, Date.parse('2026-09-05T00:00:00Z'))
const ids = key => groups.find(g => g.key === key).items.map(c => c.id)
assert.deepEqual(ids('roles'), ['current-role'])
assert.deepEqual(ids('principles'), ['principle'])
assert.deepEqual(ids('matters'), ['project'])
assert.deepEqual(ids('directions'), ['wish'])
assert.deepEqual(ids('uncertain'), ['uncertain', 'challenged'])
assert.equal(ids('recent')[0], 'principle')
assert.ok(groups.every(g => g.items.length <= 3))
assert.ok(groups.every(g => !g.items.some(c => ['retracted', 'superseded'].includes(c.id))))
assert.deepEqual(input, original, 'summary does not mutate or confirm source records')
assert.equal(summaryStatus(input.find(c => c.id === 'wish')), '理想方向，不等同于已实现')
assert.equal(summaryStatus(input.find(c => c.id === 'project')), '只适用于当时情境')
assert.equal(summaryStatus(input.find(c => c.id === 'uncertain')), '待你确认')
assert.equal(preferredOntologyView(null), 'summary')
assert.equal(preferredOntologyView('list'), 'list')
assert.equal(preferredOntologyView('map'), 'map')
assert.equal(preferredOntologyView('summary'), 'summary')
console.log('personal-summary: exact source text, status separation, lifecycle/time filtering, immutable records and existing view preferences passed')
