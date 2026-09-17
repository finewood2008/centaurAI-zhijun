import assert from 'node:assert/strict'
import test from 'node:test'
import { editableGrant, grantLabel, PERSONAL_SECTIONS } from '../src/services/externalAgents.ts'

const grant = { id: 'g', revision: 2, agentId: 'workbuddy', agentName: 'WorkBuddy', sections: ['ways'],
  materialIds: ['doc'], excludedClaimIds: ['deny'], acknowledgedLegacyIds: ['old'], days: 30,
  disclosureAccepted: true, state: 'active', expiresAt: 200, createdAt: 1 }

test('editing scope keeps independent arrays, revision and original term', () => {
  const draft = editableGrant(grant)
  draft.sections.push('who'); draft.materialIds.push('new'); draft.excludedClaimIds.length = 0
  assert.deepEqual(grant.sections, ['ways']); assert.deepEqual(grant.materialIds, ['doc'])
  assert.deepEqual(grant.excludedClaimIds, ['deny'])
  assert.equal(draft.expectedRevision, 2); assert.equal(draft.days, 30)
})
test('expired and revoked grants never display as active', () => {
  assert.equal(grantLabel(grant, 100000), '授权中')
  assert.equal(grantLabel(grant, 201000), '已到期')
  assert.equal(grantLabel({ ...grant, state: 'revoked' }, 201000), '已撤销')
  assert.equal(grantLabel({ ...grant, state: 'paused' }, 100000), '已暂停')
  assert.equal(PERSONAL_SECTIONS.length, 6)
})
