// Consumer ownership claim code; unrelated to SMS or BLE physical confirmation.
const CLAIM_TOKEN_PATTERN = /^(?:[A-Z2-7]{10}|[A-Z2-7]{20})$/

export function normalizeClaimToken(value: string): string {
  // Claim codes are exact credentials: do not silently trim or change case.
  return value
}

export function isValidClaimToken(value: string): boolean {
  return CLAIM_TOKEN_PATTERN.test(normalizeClaimToken(value))
}
