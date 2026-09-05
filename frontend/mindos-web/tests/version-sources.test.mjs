import assert from 'node:assert/strict'
import { applyVersionSourceAction } from '../src/shared/versionSources.ts'

const refs = [
  { sourceType: 'knowledge', id: 'knowledge_a' },
  { sourceType: 'material', id: 'mindos_v1' },
  { sourceType: 'material', id: 'mindos_other' },
]

assert.deepEqual(
  applyVersionSourceAction(refs, 'mindos_v1', 'mindos_v2', 'replace'),
  [
    { sourceType: 'knowledge', id: 'knowledge_a' },
    { sourceType: 'material', id: 'mindos_v2' },
    { sourceType: 'material', id: 'mindos_other' },
  ],
)
assert.deepEqual(
  applyVersionSourceAction(refs, 'mindos_v1', 'mindos_v2', 'keepBoth'),
  [
    { sourceType: 'knowledge', id: 'knowledge_a' },
    { sourceType: 'material', id: 'mindos_v1' },
    { sourceType: 'material', id: 'mindos_v2' },
    { sourceType: 'material', id: 'mindos_other' },
  ],
)
console.log('version-sources: 2 tests OK')
