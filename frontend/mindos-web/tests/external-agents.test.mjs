import assert from 'node:assert/strict'
import test from 'node:test'
import { editableGrant, externalAgentsUnavailable, grantLabel, PERSONAL_SECTIONS } from '../src/services/externalAgents.ts'

const grant = { id: 'g', revision: 2, agentId: 'workbuddy', agentName: 'WorkBuddy', sections: ['ways'],
  materialIds: ['doc'], excludedClaimIds: ['deny'], acknowledgedLegacyIds: ['old'], days: 30,
  disclosureAccepted: true, state: 'active', expiresAt: 200, createdAt: 1 }

test('status compatibility recognizes missing routes and exact desktop catalog rejection', () => {
  for (const error of [{ status: 404 }, { status: 501 },
    { status: 400, code: 'WORKER_OPERATION_INVALID' },
    { status: 503, code: 'PRODUCT_OPERATION_UNSUPPORTED' },
    { status: 403, code: 'WORKSPACE_OPERATION_DENIED' },
    { status: 403, code: 'ACCESS_DENIED', remoteCode: 'WORKSPACE_OPERATION_DENIED' }]) {
    assert.equal(externalAgentsUnavailable(Object.assign(new Error('synthetic'), error)), true)
  }
})

test('authorization, storage, transport and missing-job failures stay visible as errors', () => {
  for (const error of [null, 'WORKSPACE_OPERATION_DENIED', new TypeError('network'),
    new DOMException('cancelled', 'AbortError'), { status: 403, code: 'ACCESS_DENIED' },
    { status: 401, code: 'ACCESS_DENIED', remoteCode: 'WORKSPACE_OPERATION_DENIED' },
    { status: 403, code: 'ACCESS_DENIED', remoteCode: 'WORKSPACE_AUTHORIZATION_REVOKED' },
    { status: 503, code: 'EXTERNAL_AGENTS_UNAVAILABLE' },
    { status: 500, code: 'WORKSPACE_OPERATION_DENIED' },
    { status: 404, code: 'REMOTE_ERROR' }]) {
    assert.equal(externalAgentsUnavailable(error), false)
  }
})

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
