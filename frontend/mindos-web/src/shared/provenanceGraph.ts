// 区分实际提供、明确引用与旧回执的关联信息；不从权限来源链推断阅读。
import type { ContextItem, ContextPlan, ProvenanceEvent } from '@/services/api'

export const MAX_SHOWN = 8

/** Display only server-recorded IDs that resolve to one unambiguous, versioned item. */
export function normalizeContextPlan(value: unknown): ContextPlan | undefined {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Partial<ContextPlan>
  if (typeof raw.revision !== 'string' || !['initial', 'supplemented', 'lookup_unavailable'].includes(raw.stage || '') || !Array.isArray(raw.providedRefs)) return undefined
  const validItem = (item: ContextItem) => item && /^(?:p|m)[1-9]\d*$/.test(item.citationId) && typeof item.kind === 'string' && typeof item.id === 'string' && typeof item.version === 'string' && typeof item.title === 'string' && typeof item.text === 'string'
  const background = Array.isArray(raw.background) ? raw.background.filter(validItem) : []
  const evidence = Array.isArray(raw.evidence) ? raw.evidence.filter(validItem) : []
  const counts = new Map<string, number>()
  for (const item of [...background, ...evidence]) counts.set(item.citationId, (counts.get(item.citationId) ?? 0) + 1)
  const unique = (values: unknown[]) => [...new Set(values.filter((id): id is string => typeof id === 'string' && counts.get(id) === 1))]
  const providedRefs = unique(raw.providedRefs)
  const citedRefs = unique(Array.isArray(raw.citedRefs) ? raw.citedRefs : []).filter(id => providedRefs.includes(id))
  const delivery = ['prepared', 'provided', 'awaiting_authorization', 'paused'].includes(raw.delivery || '') ? raw.delivery : undefined
  const lookupNotice = delivery === 'provided'
    ? typeof raw.lookupNotice === 'string' && raw.lookupNotice.trim() ? raw.lookupNotice.trim() : '额外补查暂未完成，本轮使用已读取且已授权的信息回答。'
    : delivery === 'prepared' ? '额外补查暂未完成，正在继续处理本轮回答。'
      : delivery === 'awaiting_authorization' ? '额外补查暂未完成，核对授权后可继续回答。'
        : '额外补查暂未完成。'
  return {
    revision: raw.revision, stage: raw.stage as ContextPlan['stage'], focus: raw.focus,
    ...(delivery ? { delivery } : {}),
    ...(raw.stage === 'lookup_unavailable' ? {
      lookupNotice,
      ...(typeof raw.lookupAttempts === 'number' && Number.isInteger(raw.lookupAttempts) && raw.lookupAttempts >= 0 ? { lookupAttempts: raw.lookupAttempts } : {}),
    } : {}),
    background: background.filter(item => counts.get(item.citationId) === 1), evidence: evidence.filter(item => counts.get(item.citationId) === 1),
    providedRefs, citedRefs,
    excluded: Array.isArray(raw.excluded) ? raw.excluded.filter(item => item && typeof item.reason === 'string') : [],
    citationAudit: { invalidRefs: Array.isArray(raw.citationAudit?.invalidRefs) ? [...new Set(raw.citationAudit.invalidRefs.filter(id => typeof id === 'string'))] : [] },
  }
}
export function contextItems(plan: ContextPlan, field: 'providedRefs' | 'citedRefs'): ContextItem[] {
  const byId = new Map([...plan.background, ...plan.evidence].map(item => [item.citationId, item]))
  return plan[field].filter(id => field !== 'citedRefs' || plan.providedRefs.includes(id)).flatMap(id => byId.has(id) ? [byId.get(id)!] : [])
}

export type ProvGroupKey = 'confirmed' | 'working' | 'materials' | 'retracted'

export interface ProvItem {
  id: string
  label: string
  kind: 'claim' | 'working' | 'material' | 'retracted'
  section?: string
  layer?: string
  materialId?: string
}

export interface ProvGroup {
  key: ProvGroupKey
  label: string
  note?: string
  items: ProvItem[]
  shown: ProvItem[]
  extra: number
  count: number
}

/**
 * 历史回执和被中断的流式事件可能来自较旧的后端，缺少后来补充的集合字段。
 * 在展示边界统一补齐，避免一个不完整的出处对象拖垮整张对话页。
 */
export function normalizeProvenance(p: Partial<ProvenanceEvent> | null | undefined): ProvenanceEvent {
  const memory = p?.memoryContext
  const contextPlan = normalizeContextPlan(p?.contextPlan)
  const count = (value: unknown) => Number.isFinite(Number(value)) ? Math.max(0, Math.floor(Number(value))) : 0
  return {
    ...(contextPlan ? { contextPlan } : {}),
    confirmedClaims: Array.isArray(p?.confirmedClaims) ? p.confirmedClaims : [],
    workingClaims: Array.isArray(p?.workingClaims) ? p.workingClaims : [],
    materials: Array.isArray(p?.materials) ? p.materials : [],
    retractedNotices: Number.isFinite(Number(p?.retractedNotices)) ? Number(p?.retractedNotices) : 0,
    charterVersion: typeof p?.charterVersion === 'number' ? p.charterVersion : null,
    ...(p?.charterBasis && Array.isArray(p.charterBasis.clauseIds) ? { charterBasis: p.charterBasis } : {}),
    promptChars: Number.isFinite(Number(p?.promptChars)) ? Number(p?.promptChars) : 0,
    pastDecisions: Array.isArray(p?.pastDecisions) ? p.pastDecisions : [],
    anchorClaimIds: Array.isArray(p?.anchorClaimIds) ? p.anchorClaimIds : [],
    ...(memory && ['direct', 'inherited', 'restricted', 'none'].includes(memory.status) &&
      ['charter', 'self_overview', 'conversation'].includes(memory.intent) ? {
        memoryContext: {
          intent: memory.intent,
          status: memory.status,
          directCount: count(memory.directCount),
          inheritedCount: count(memory.inheritedCount),
          excludedCount: count(memory.excludedCount),
          searched: memory.searched === true,
          charterChecked: memory.charterChecked === true,
          charterComplete: memory.charterComplete === true,
        },
      } : {}),
  }
}

export function provenanceCharterSummary(p: ProvenanceEvent): string {
  if (p.charterBasis?.version && p.charterBasis.clauseIds.length) return `遵循人生章程第 ${p.charterBasis.version} 版 · ${p.charterBasis.clauseIds.length} 条约定`
  const memory = p.memoryContext
  const version = p.charterVersion ? `第 ${p.charterVersion} 版` : ''
  if (memory?.charterChecked) return `旧回执关联人生章程${version || '填写状态'}`
  return version ? `旧回执关联人生章程${version}` : ''
}

/** 只根据本轮回执说明使用情况；没有新字段的历史消息不推断继承或权限状态。 */
export function provenanceMemorySummary(raw: Partial<ProvenanceEvent> | null | undefined): string {
  const p = normalizeProvenance(raw)
  if (p.contextPlan) {
    const provided = contextItems(p.contextPlan, 'providedRefs').length
    const cited = contextItems(p.contextPlan, 'citedRefs').length
    const lookupStatus = p.contextPlan.stage === 'supplemented' ? ' · 已补查一次' : p.contextPlan.stage === 'lookup_unavailable' ? ' · 补查暂未完成' : ''
    return `提供给模型 ${provided} 项信息 · 回答明确引用 ${cited} 项${lookupStatus}`
  }
  const memory = p.memoryContext
  const recorded = p.confirmedClaims.length + p.workingClaims.length + p.materials.length
  if (recorded) return `旧回执记录了 ${recorded} 项关联信息 · 未区分提供与明确引用`
  if (memory?.inheritedCount) return '旧回执保留了历史来源关联 · 不代表本轮读取或引用'
  if (memory?.excludedCount) return `旧回执记录了 ${memory.excludedCount} 项未纳入信息`
  if (p.charterBasis?.version || p.charterVersion || memory?.charterChecked) return '旧回执保留了章程记录 · 未区分信息提供与引用'
  return '旧回执未记录可核验的信息提供与引用'
}

export function lineWidth(count: number): number {
  if (count <= 0) return 1
  return Math.max(1, Math.min(3, Math.ceil(count / 2)))
}

export function truncateTitle(text: string | null | undefined, max = 10): string {
  const t = (text ?? '').trim()
  if (!t) return ''
  return t.length > max ? `${t.slice(0, max)}…` : t
}

function makeGroup(key: ProvGroupKey, label: string, items: ProvItem[], count: number, note?: string): ProvGroup {
  return { key, label, note, items, shown: items.slice(0, MAX_SHOWN), extra: Math.max(0, items.length - MAX_SHOWN), count }
}

/** 只返回有内容的组（顺序：已确认 → 工作理解 → 资料 → 避开的旧理解）。 */
export function groups(p: ProvenanceEvent | null | undefined): ProvGroup[] {
  if (!p) return []
  const out: ProvGroup[] = []
  const confirmed = (p.confirmedClaims ?? []).map<ProvItem>((c) => ({ id: c.id, label: c.content, kind: 'claim', section: c.section, layer: c.layer }))
  if (confirmed.length) out.push(makeGroup('confirmed', '已确认的理解', confirmed, confirmed.length))
  const working = (p.workingClaims ?? []).map<ProvItem>((c) => ({ id: c.id, label: c.content, kind: 'working', section: c.section, layer: c.layer }))
  if (working.length) out.push(makeGroup('working', '工作理解', working, working.length, '带保留语气'))
  const materials = (p.materials ?? []).map<ProvItem>((m, i) => ({ id: `${m.materialId}-${i}`, label: m.title || m.materialId, kind: 'material', materialId: m.materialId }))
  if (materials.length) out.push(makeGroup('materials', '资料片段', materials, materials.length))
  const retracted = Number(p.retractedNotices) || 0
  if (retracted > 0) out.push(makeGroup('retracted', '避开的旧理解', [{ id: 'retracted', label: `避开 ${retracted} 条被你纠正的`, kind: 'retracted' }], retracted))
  return out
}

export function isEmpty(p: ProvenanceEvent | null | undefined): boolean {
  return groups(p).length === 0
}
