const CLAIM_TOKEN_PATTERN = /^[0123456789ABCDEFGHJKMNPQRSTVWXYZ]{16}$/

export function normalizeClaimToken(value: string): string {
  return value.trim().toUpperCase().replace(/[\s-]/gu, '')
    .replace(/O/gu, '0').replace(/[IL]/gu, '1')
}

export function isValidClaimToken(value: string): boolean {
  return CLAIM_TOKEN_PATTERN.test(normalizeClaimToken(value))
}
