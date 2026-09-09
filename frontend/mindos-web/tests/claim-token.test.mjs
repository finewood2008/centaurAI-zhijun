import assert from 'node:assert/strict'
import test from 'node:test'
import { isValidClaimToken, normalizeClaimToken } from '../src/desktop/claimToken.ts'

test('normalizes human-readable claim tokens to the canonical wire value', () => {
  assert.equal(normalizeClaimToken('abcd-efgh jk2m np3q'), 'ABCDEFGHJK2MNP3Q')
  assert.equal(normalizeClaimToken('oili-2345-6789-abcd'), '011123456789ABCD')
})

test('only accepts the 16-character Consumer claim token alphabet', () => {
  assert.equal(isValidClaimToken('ABCD-EFGH-JK2M-NP3Q'), true)
  assert.equal(isValidClaimToken('AMD-A2A-248'), false)
  assert.equal(isValidClaimToken('ABCD-EFGH-JK2M-NP3U'), false)
})
