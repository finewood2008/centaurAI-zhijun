// 判断草稿的纯逻辑：字段合并、缺失项、拿主意意图提示。无 DOM，供 node 测试。
export interface DraftFieldsLike {
  title?: string
  context?: string
  options?: string[]
  leaning?: string | null
  choice?: string | null
  rationale?: string | null
  confidence?: number | null
  expectedOutcome?: string | null
  reviewAt?: string | null
  keyQuestion?: string | null
  zhijunView?: string | null
  relatedEntityIds?: string[]
  evidenceRefs?: string[]
  userQuotes?: string[]
}

export const EMPTY_DRAFT_FIELDS: Required<DraftFieldsLike> = {
  title: '',
  context: '',
  options: [],
  leaning: null,
  choice: null,
  rationale: null,
  confidence: null,
  expectedOutcome: null,
  reviewAt: null,
  keyQuestion: null,
  zhijunView: null,
  relatedEntityIds: [],
  evidenceRefs: [],
  userQuotes: [],
}

/** 用户必须亲自填写的四项（后端同样校验；知君的看法永远不算）。 */
export const USER_REQUIRED_FIELDS = ['choice', 'rationale', 'confidence', 'expectedOutcome'] as const
export type UserRequiredField = (typeof USER_REQUIRED_FIELDS)[number]

export const FIELD_LABELS: Record<UserRequiredField, string> = {
  choice: '选择',
  rationale: '理由',
  confidence: '把握',
  expectedOutcome: '预期结果',
}

/**
 * 合并新到的草稿：incoming 里为 null / undefined 的字段不覆盖已有值，
 * 数组为空时也不覆盖；返回全新对象。
 */
export function mergeDraftFields(prev: DraftFieldsLike | null | undefined, incoming: DraftFieldsLike | null | undefined): Required<DraftFieldsLike> {
  const base: Required<DraftFieldsLike> = { ...EMPTY_DRAFT_FIELDS, ...(prev ?? {}) } as Required<DraftFieldsLike>
  if (!incoming) return base
  const out: Required<DraftFieldsLike> = { ...base }
  for (const key of Object.keys(EMPTY_DRAFT_FIELDS) as (keyof DraftFieldsLike)[]) {
    const value = incoming[key]
    if (value === null || value === undefined) continue
    if (Array.isArray(value)) {
      if (value.length) (out as Record<string, unknown>)[key] = [...value]
      continue
    }
    if (typeof value === 'string' && !value.trim()) continue
    ;(out as Record<string, unknown>)[key] = value
  }
  return out
}

/** 还需要用户亲自填的字段（choice / rationale / confidence / expectedOutcome）。 */
export function draftMissingFields(fields: DraftFieldsLike | null | undefined): UserRequiredField[] {
  const f = fields ?? {}
  const missing: UserRequiredField[] = []
  if (!(f.choice ?? '').trim()) missing.push('choice')
  if (!(f.rationale ?? '').trim()) missing.push('rationale')
  const c = f.confidence
  if (c === null || c === undefined || !Number.isFinite(c) || c < 0 || c > 100) missing.push('confidence')
  if (!(f.expectedOutcome ?? '').trim()) missing.push('expectedOutcome')
  return missing
}

const INTENT_RE = /我在考虑|要不要|该不该|纠结|还是/

/** 便宜的「像是在拿主意」提示；只提示、不自动切换。 */
export function intentHint(text: string | null | undefined): boolean {
  if (!text) return false
  return INTENT_RE.test(text)
}

/** 默认回访日：今天 + 14 天，返回 YYYY-MM-DD（本地日期）。 */
export function defaultReviewDate(from: Date = new Date(), days = 14): string {
  const d = new Date(from.getTime() + days * 86400000)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** 把 YYYY-MM-DD 转成当天 10:00 本地时间的 ISO（带时区），供 reviewAt。 */
export function reviewDateToIso(date: string): string | undefined {
  if (!date) return undefined
  const local = new Date(`${date}T10:00:00`)
  if (Number.isNaN(local.getTime())) return undefined
  return local.toISOString()
}
