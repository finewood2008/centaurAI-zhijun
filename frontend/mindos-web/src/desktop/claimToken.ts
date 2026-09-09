const CLAIM_TOKEN_PATTERN = /^\d{6}$/

export function normalizeClaimToken(value: string): string {
  return value.trim()
}

export function isValidClaimToken(value: string): boolean {
  return CLAIM_TOKEN_PATTERN.test(normalizeClaimToken(value))
}
