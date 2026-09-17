import assert from 'node:assert/strict'
import test from 'node:test'
import { isValidClaimToken, normalizeClaimToken } from '../src/desktop/claimToken.ts'

test('claim credentials are never normalized, truncated or repaired', () => {
  for (const value of [' ABCDEFGH23\n', 'abcdefgh23', 'ABCDE-FGH23', 'ABCDEFGHIJABCDEFGHIJA']) {
    assert.equal(normalizeClaimToken(value), value)
    assert.equal(isValidClaimToken(value), false)
  }
})

test('accepts exact ten-character Base32 codes and compatible twenty-character codes', () => {
  for (const value of ['ABCDEFGH23', '234567ABCD', 'ABCDEFGHIJKLMNOP2345']) {
    assert.equal(isValidClaimToken(value), true)
  }
  for (const invalid of ['', '123456', '0123456789', 'ABCDEFGHI', 'ABCDEFGHIJK',
    'ABCDEFGH20', 'ABCDEFGH21', 'ABCDEFGH28', 'ABCDEFGH29', 'ABCDEFGH２３',
    'ABCDEFGH23\n', '\nABCDEFGH23', 'ＡBCDEFGH23', 'AMD-A2A-248', 'ABCD-EFGH-JK2M-NP3Q']) {
    assert.equal(isValidClaimToken(invalid), false, JSON.stringify(invalid))
  }
})
