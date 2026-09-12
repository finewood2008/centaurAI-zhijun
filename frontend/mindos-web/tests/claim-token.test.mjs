import assert from 'node:assert/strict'
import test from 'node:test'
import { isValidClaimToken, normalizeClaimToken } from '../src/desktop/claimToken.ts'

test('normalizes only surrounding whitespace from a six-digit claim code', () => {
  assert.equal(normalizeClaimToken(' 123456\n'), '123456')
  assert.equal(normalizeClaimToken('12 3456'), '12 3456')
})

test('only accepts a six-digit Consumer claim code', () => {
  assert.equal(isValidClaimToken('123456'), true)
  assert.equal(isValidClaimToken(' 123456 '), true)
  assert.equal(isValidClaimToken('123 456'), false)
  assert.equal(isValidClaimToken('12345'), false)
  assert.equal(isValidClaimToken('1234567'), false)
  assert.equal(isValidClaimToken('１２３４５６'), false)
  assert.equal(isValidClaimToken('AMD-A2A-248'), false)
  assert.equal(isValidClaimToken('ABCD-EFGH-JK2M-NP3Q'), false)
})
