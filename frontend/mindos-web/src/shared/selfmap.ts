// 本体全景（SelfMap）的纯几何与布局规则：确定性哈希、扇区、信任环带、极坐标。
// 无 DOM 依赖，可在 node 里直接测试。角度单位为度，-90 为正上方，顺时针为正（SVG y 轴向下）。
import type { Claim, Section } from '@/services/api'

export const SECTOR_ORDER: readonly Section[] = ['who', 'people', 'matters', 'principles', 'ways', 'direction']
export const SECTOR_DEG = 60
export const SECTOR_START_DEG = -90
/** 节点在扇区内允许偏离中心线的最大角度（避开分隔线） */
export const SECTOR_SPREAD_DEG = 24

export const CENTER = 360
export const RINGS = { core: 150, reaffirm: 230, boundary: 300 } as const
export const SECTOR_INNER = 60
export const SECTOR_OUTER = 330
export const BANDS = {
  calibrated: [80, 140] as const,
  uncalibrated: [175, 210] as const,
  contextual: [250, 280] as const,
  working: [310, 330] as const,
  challenged: 345,
} as const
export const STALE_DAYS = 60

/** FNV-1a 32 位哈希 → [0, 1)，同一 id 永远得到同一个数。 */
export function hashToUnit(id: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < id.length; i += 1) {
    h ^= id.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return (h >>> 0) / 0x100000000
}

export function sectorIndex(section: string): number {
  const i = SECTOR_ORDER.indexOf(section as Section)
  return i < 0 ? 0 : i
}

export function sectorStartDeg(index: number): number {
  return SECTOR_START_DEG + index * SECTOR_DEG
}

export function sectorCenterDeg(index: number): number {
  return sectorStartDeg(index) + SECTOR_DEG / 2
}

export function polar(angleDeg: number, r: number, cx = CENTER, cy = CENTER): { x: number; y: number } {
  const rad = (angleDeg * Math.PI) / 180
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
}

export function isStale(claim: Pick<Claim, 'lastReaffirmed'>, now: number = Date.now()): boolean {
  const t = Date.parse(claim.lastReaffirmed)
  if (Number.isNaN(t)) return false
  return now - t > STALE_DAYS * 24 * 3600 * 1000
}

export type Band = 'calibrated' | 'uncalibrated' | 'contextual' | 'working' | 'challenged'
type MapClaim = Pick<Claim, 'trustState' | 'challenged' | 'lastReaffirmed'> & Partial<Pick<Claim, 'selfAlignment' | 'scope'>>

export function nodeBand(claim: MapClaim, _now: number = Date.now()): Band {
  if (claim.trustState === 'working') return claim.challenged ? 'challenged' : 'working'
  if (claim.scope === 'context_only' || claim.selfAlignment?.framing === 'context_only') return 'contextual'
  return claim.selfAlignment?.level == null ? 'uncalibrated' : 'calibrated'
}

/** 正式校准决定圈内远近；哈希仅用于未校准、情境和待确认区域排布。 */
export function nodeRadius(claim: MapClaim & { id: string }, now: number = Date.now()): number {
  const band = nodeBand(claim, now)
  if (band === 'challenged') return BANDS.challenged
  if (band === 'calibrated') return 140 - 15 * Math.max(0, Math.min(4, claim.selfAlignment?.level ?? 0))
  const [lo, hi] = BANDS[band]
  return lo + (hi - lo) * hashToUnit(`${claim.id}:r`)
}

/** 角度 = 所属扇区中心 ± SPREAD，偏移由 id 哈希决定。 */
export function nodeAngle(claim: Pick<Claim, 'id' | 'section'>): number {
  const center = sectorCenterDeg(sectorIndex(claim.section))
  const offset = (hashToUnit(`${claim.id}:a`) * 2 - 1) * SECTOR_SPREAD_DEG
  return center + offset
}

export function nodeSize(claim: Pick<Claim, 'evidence'>): number {
  const n = Array.isArray(claim.evidence) ? claim.evidence.length : 0
  return 5 + Math.min(n, 3) * 1.5
}

/** 环形扇区路径（用于扇区底色）。 */
export function annularSectorPath(startDeg: number, endDeg: number, rInner: number, rOuter: number, cx = CENTER, cy = CENTER): string {
  const a = polar(startDeg, rOuter, cx, cy)
  const b = polar(endDeg, rOuter, cx, cy)
  const c = polar(endDeg, rInner, cx, cy)
  const d = polar(startDeg, rInner, cx, cy)
  const large = endDeg - startDeg > 180 ? 1 : 0
  return `M ${a.x.toFixed(2)} ${a.y.toFixed(2)} A ${rOuter} ${rOuter} 0 ${large} 1 ${b.x.toFixed(2)} ${b.y.toFixed(2)} L ${c.x.toFixed(2)} ${c.y.toFixed(2)} A ${rInner} ${rInner} 0 ${large} 0 ${d.x.toFixed(2)} ${d.y.toFixed(2)} Z`
}

export function truncateLabel(text: string | null | undefined, max = 8): string {
  const t = (text ?? '').trim()
  if (!t) return ''
  return t.length > max ? `${t.slice(0, max)}…` : t
}

/** 已确认占比（0..1），用于侧栏小环与出处条小环。 */
export function confirmedFraction(confirmed: number, working: number): number {
  const total = confirmed + working
  if (total <= 0) return 0
  return Math.max(0, Math.min(1, confirmed / total))
}
