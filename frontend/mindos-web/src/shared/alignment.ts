import type { Claim } from '@/services/api'

export const ALIGNMENT_LEVELS = ['不代表我', '较少代表', '部分代表', '比较代表', '很能代表'] as const
export const ALIGNMENT_FRAMES = [
  { value: 'long_term', label: '代表现在的我' },
  { value: 'context_only', label: '只适用于当时' },
  { value: 'aspirational', label: '这是我想成为的样子' },
] as const

export function alignmentLabel(claim: Pick<Claim, 'selfAlignment'>): string {
  const a = claim.selfAlignment
  if (a?.level == null) return a?.needsRecalibration ? '内容已变化，待重新校准' : '尚未校准'
  return ALIGNMENT_LEVELS[a.level] ?? '尚未校准'
}

export function alignmentFrame(claim: Pick<Claim, 'selfAlignment' | 'scope' | 'layer'>): string {
  if (claim.scope === 'context_only' || claim.selfAlignment?.framing === 'context_only') return '仅适用于当时情境'
  if (claim.layer === 'aspirational' || claim.selfAlignment?.framing === 'aspirational') return '认同这个愿望，不表示已经做到'
  return '对当前自我的认同'
}
