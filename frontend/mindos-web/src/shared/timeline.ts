// 判断时间线的纯几何：时间 → x、把握 → y、碰撞下移、摘要。无 DOM 依赖，可在 node 里直接测试。
import type { GrowthDecision } from '@/services/api'

export type TimelineDecision = Pick<GrowthDecision, 'id' | 'title' | 'status' | 'confidence' | 'createdAt' | 'reviewAt'>

export const CHART = { width: 960, height: 260, left: 48, right: 24, top: 20, bottom: 40 } as const
export const COLLISION_PX = 12
export const COLLISION_STEP = 8
const DAY = 24 * 3600 * 1000

export function plotWidth(width = CHART.width): number {
  return width - CHART.left - CHART.right
}

export function plotHeight(height = CHART.height): number {
  return height - CHART.top - CHART.bottom
}

function parseTime(value: string | null | undefined): number | null {
  if (!value) return null
  const t = Date.parse(value)
  return Number.isNaN(t) ? null : t
}

export function isOverdue(d: Pick<TimelineDecision, 'status' | 'reviewAt'>, now: number): boolean {
  if (d.status !== 'open') return false
  const t = parseTime(d.reviewAt)
  return t !== null && t < now
}

/** 时间域：最早的 createdAt → max(reviewAt, now)，两侧各留 5%；范围至少一天。 */
export function domain(decisions: TimelineDecision[], now: number): { min: number; max: number } {
  let min = Number.POSITIVE_INFINITY
  let max = now
  for (const d of decisions) {
    const c = parseTime(d.createdAt)
    if (c !== null && c < min) min = c
    const r = parseTime(d.reviewAt)
    if (r !== null && r > max) max = r
  }
  if (!Number.isFinite(min)) min = now - DAY
  if (max - min < DAY) max = min + DAY
  const pad = (max - min) * 0.05
  return { min: min - pad, max: max + pad }
}

/** 线性时间比例尺：返回 t → [0, width] 的函数（超出域也按线性外推）。 */
export function timeScale(min: number, max: number, width: number): (t: number) => number {
  const span = max - min || 1
  return (t: number) => ((t - min) / span) * width
}

/** 4–6 个「整天」刻度。 */
export function ticks(min: number, max: number, count = 5): number[] {
  const span = max - min
  const rawStep = span / Math.max(1, count - 1)
  const dayStep = Math.max(1, Math.round(rawStep / DAY))
  const first = new Date(min)
  first.setHours(0, 0, 0, 0)
  let t = first.getTime()
  if (t < min) t += dayStep * DAY
  const out: number[] = []
  while (t <= max && out.length < 8) {
    out.push(t)
    t += dayStep * DAY
  }
  return out
}

export function formatTick(t: number): string {
  const d = new Date(t)
  return `${d.getMonth() + 1}/${d.getDate()}`
}

export interface DotPosition {
  id: string
  x: number
  y: number
  offset: number
  reviewX: number | null
  overdue: boolean
  status: TimelineDecision['status']
  confidence: number
}

/** 按 createdAt 排序后布点；与已放置的点距离 < 12px 时下移 8px（确定性）。 */
export function dotPositions(decisions: TimelineDecision[], now: number, width = CHART.width, height = CHART.height): DotPosition[] {
  const { min, max } = domain(decisions, now)
  const sx = timeScale(min, max, plotWidth(width))
  const ph = plotHeight(height)
  const sorted = [...decisions].sort((a, b) => (parseTime(a.createdAt) ?? 0) - (parseTime(b.createdAt) ?? 0) || a.id.localeCompare(b.id))
  const placed: DotPosition[] = []
  for (const d of sorted) {
    const created = parseTime(d.createdAt) ?? min
    const conf = Math.max(0, Math.min(100, Number(d.confidence) || 0))
    const x = CHART.left + sx(created)
    const baseY = CHART.top + ((100 - conf) / 100) * ph
    // 与已放置的点距离 < 12px → 下移一档 8px（只做一次，保持可预期）
    const hit = placed.some((p) => Math.hypot(p.x - x, p.y - baseY) < COLLISION_PX)
    const offset = hit ? COLLISION_STEP : 0
    const review = parseTime(d.reviewAt)
    placed.push({
      id: d.id,
      x,
      y: baseY + offset,
      offset,
      reviewX: review === null ? null : CHART.left + sx(review),
      overdue: isOverdue(d, now),
      status: d.status,
      confidence: conf,
    })
  }
  return placed
}

export function summary(decisions: TimelineDecision[], now: number): { count: number; avgConfidence: number; overdue: number } {
  const count = decisions.length
  const avg = count ? Math.round(decisions.reduce((s, d) => s + (Number(d.confidence) || 0), 0) / count) : 0
  const overdue = decisions.filter((d) => isOverdue(d, now)).length
  return { count, avgConfidence: avg, overdue }
}

export function statusText(status: TimelineDecision['status']): string {
  if (status === 'reviewed') return '已复盘'
  if (status === 'outcome_recorded') return '已记结果'
  return '等结果'
}
